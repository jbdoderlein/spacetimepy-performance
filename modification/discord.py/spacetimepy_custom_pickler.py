"""Dill reducers for Discord objects captured by SpacetimePy."""

import asyncio
import importlib
import traceback
from typing import Any
from unittest import mock

import dill
from _pytest._code.code import ExceptionInfo

from . import app_commands
from .ext import commands
from .ui.action_row import ActionRow
from .ui.container import Container
from .ui.view import BaseView


_SUBCLASS_REDUCER_TYPES = (
    app_commands.Group,
    commands.Cog,
    mock.NonCallableMock,
    BaseView,
    ActionRow,
    Container,
)


class _CompletedViewFuture:
    def __init__(self, status: str, value: Any) -> None:
        self.status = status
        self.value = value

    def done(self) -> bool:
        return True

    def cancelled(self) -> bool:
        return self.status == 'cancelled'

    def cancel(self) -> bool:
        return False

    def result(self) -> Any:
        if self.status == 'cancelled':
            raise asyncio.CancelledError
        if self.status == 'exception':
            raise self.value
        return self.value

    def exception(self) -> BaseException | None:
        if self.status == 'cancelled':
            raise asyncio.CancelledError
        return self.value if self.status == 'exception' else None

    def set_result(self, value: Any) -> None:
        raise asyncio.InvalidStateError('The view is already finished')

    def __await__(self) -> Any:
        async def completed() -> Any:
            return self.result()

        return completed().__await__()


def _install_subclass_dispatch() -> None:
    dispatch_type = type(dill.Pickler.dispatch)
    original_attribute = '_discord_original_missing'
    if hasattr(dispatch_type, original_attribute):
        return

    original_missing = dispatch_type.__missing__
    setattr(dispatch_type, original_attribute, original_missing)

    def missing(dispatch: dict[type[Any], Any], python_type: type[Any]) -> Any:
        if isinstance(python_type, type):
            for base_type in _SUBCLASS_REDUCER_TYPES:
                if issubclass(python_type, base_type):
                    try:
                        return dict.__getitem__(dispatch, base_type)
                    except KeyError:
                        break
        return original_missing(dispatch, python_type)

    dispatch_type.__missing__ = missing


def _is_global_reference(value: type[Any]) -> bool:
    try:
        resolved: Any = importlib.import_module(value.__module__)
    except (ImportError, ValueError):
        return False

    for part in value.__qualname__.split('.'):
        if part == '<locals>':
            return False
        try:
            resolved = getattr(resolved, part)
        except AttributeError:
            return False
    return resolved is value


def _set_group_state(group: app_commands.Group, state: dict[str, Any]) -> None:
    group.__dict__.update(state)
    if group.parent is not None:
        group._owner_cls = type(group.parent)


def _reduce_group(group: app_commands.Group) -> tuple[Any, ...]:
    state = group.__dict__.copy()
    state['_owner_cls'] = None
    return object.__new__, (type(group),), state, None, None, _set_group_state


def _stable_mock_type(value: mock.NonCallableMock) -> type[mock.NonCallableMock]:
    for candidate in type(value).__mro__[1:]:
        if issubclass(candidate, mock.NonCallableMock) and _is_global_reference(candidate):
            return candidate
    raise TypeError(f'Cannot find a stable type for {type(value)!r}')


def _set_mock_state(value: mock.NonCallableMock, state: dict[str, Any]) -> None:
    value.__dict__.update(state)


def _reduce_mock(value: mock.NonCallableMock) -> tuple[Any, ...]:
    return _stable_mock_type(value), (), value.__dict__.copy(), None, None, _set_mock_state


def _create_mock_call(values: tuple[Any, ...], state: dict[str, Any]) -> mock._Call:
    value = mock._Call(values)
    value.__dict__.update(state)
    return value


def _reduce_mock_call(value: mock._Call) -> tuple[Any, ...]:
    return _create_mock_call, (tuple(value), value.__dict__.copy())


def _create_cog_class(
    metaclass: type[Any],
    name: str,
    bases: tuple[type[Any], ...],
    namespace: dict[str, Any],
    qualname: str,
) -> type[Any]:
    cog_class = metaclass(name, bases, namespace)
    cog_class.__qualname__ = qualname
    return cog_class


def _reduce_cog_class(cog_class: commands.CogMeta) -> str | tuple[Any, ...]:
    if _is_global_reference(cog_class):
        return cog_class.__qualname__

    namespace = dict(cog_class.__dict__)
    namespace.pop('__dict__', None)
    namespace.pop('__weakref__', None)
    if namespace.get('__doc__') is None:
        namespace['__doc__'] = ''

    return _create_cog_class, (
        type(cog_class),
        cog_class.__name__,
        cog_class.__bases__,
        namespace,
        cog_class.__qualname__,
    )


def _set_cog_state(cog: commands.Cog, state: dict[str, Any]) -> None:
    cog.__dict__.update(state)
    for value in state.values():
        if isinstance(value, app_commands.Group):
            value._owner_cls = type(cog)


def _reduce_cog(cog: commands.Cog) -> tuple[Any, ...]:
    return object.__new__, (type(cog),), cog.__dict__.copy(), None, None, _set_cog_state


def _view_future_state(future: Any) -> tuple[str, Any]:
    if future is None:
        return 'none', None
    if not future.done():
        return 'pending', None
    if future.cancelled():
        return 'cancelled', None

    exception = future.exception()
    if exception is not None:
        return 'exception', exception
    return 'result', future.result()


def _set_view_state(
    view: BaseView,
    serialized_state: tuple[dict[str, Any], tuple[str, Any]],
) -> None:
    state, (future_status, future_value) = serialized_state
    view.__dict__.update(state)
    if future_status in {'none', 'pending'}:
        stopped = None
    else:
        stopped = _CompletedViewFuture(future_status, future_value)
    view.__dict__['_BaseView__stopped'] = stopped


def _reduce_view(view: BaseView) -> tuple[Any, ...]:
    state = view.__dict__.copy()
    future_state = _view_future_state(state.pop('_BaseView__stopped', None))

    # These values belong to the event loop that owns the original view.
    state['_BaseView__timeout_task'] = None
    state['_BaseView__cancel_callback'] = None
    state['_BaseView__timeout_expiry'] = None

    return object.__new__, (type(view),), (state, future_state), None, None, _set_view_state


def _create_local_ui_item(
    base_type: type[Any],
    name: str,
    qualname: str,
    namespace: dict[str, Any],
) -> Any:
    item_type = type(name, (base_type,), namespace)
    item_type.__qualname__ = qualname
    return object.__new__(item_type)


def _reduce_ui_item(item: ActionRow[Any] | Container[Any]) -> tuple[Any, ...]:
    item_type = type(item)
    state = item.__dict__.copy()
    if _is_global_reference(item_type):
        return object.__new__, (item_type,), state

    base_type = item_type.__bases__[0]
    if base_type not in {ActionRow, Container}:
        raise TypeError(f'Cannot serialize local UI item type {item_type!r}')

    namespace = dict(item_type.__dict__)
    namespace.pop('__dict__', None)
    namespace.pop('__weakref__', None)
    namespace.pop('__firstlineno__', None)
    namespace.pop('__static_attributes__', None)
    return (
        _create_local_ui_item,
        (
            base_type,
            item_type.__name__,
            item_type.__qualname__,
            namespace,
        ),
        state,
    )


def _restore_exception_info(
    exception: BaseException,
    striptext: str,
    serialized_traceback: str,
) -> ExceptionInfo[Any]:
    try:
        raise exception
    except BaseException:
        restored = ExceptionInfo.from_current()

    restored._striptext = striptext
    restored._serialized_traceback = serialized_traceback
    return restored


def _restore_unfilled_exception_info(striptext: str) -> ExceptionInfo[Any]:
    restored = ExceptionInfo.for_later()
    restored._striptext = striptext
    return restored


def _reduce_exception_info(exception_info: ExceptionInfo[Any]) -> tuple[Any, ...]:
    if exception_info._excinfo is None:
        return _restore_unfilled_exception_info, (exception_info._striptext,)

    exception_type, exception, raw_traceback = exception_info._excinfo
    serialized_traceback = ''.join(traceback.format_exception(exception_type, exception, raw_traceback))
    return _restore_exception_info, (
        exception,
        exception_info._striptext,
        serialized_traceback,
    )


def get_dispatch_table() -> dict[type[Any], Any]:
    """Return the Discord-specific Dill reducers."""
    _install_subclass_dispatch()
    return {
        app_commands.Group: _reduce_group,
        commands.Cog: _reduce_cog,
        commands.CogMeta: _reduce_cog_class,
        ExceptionInfo: _reduce_exception_info,
        mock._Call: _reduce_mock_call,
        mock.NonCallableMock: _reduce_mock,
        BaseView: _reduce_view,
        ActionRow: _reduce_ui_item,
        Container: _reduce_ui_item,
    }
