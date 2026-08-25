"""Dill reducers for values that SpaceTimePy captures in the DSPy test suite.

The reducers preserve durable Python state. They do not copy interpreter,
thread, parser, or event-loop resources that cannot operate after restoration.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import contextvars
import copy
import csv
import dataclasses
import enum
import importlib
import inspect
import io
import logging
import sys
import tempfile
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeAlias
from unittest.mock import MagicMock, Mock, _Call, _CallList

from _pytest._code.code import ExceptionInfo, Traceback
from _pytest.capture import CaptureFixture
from _pytest.config import Config
from _pytest.fixtures import TopRequest
from _pytest.logging import (
    LogCaptureFixture,
    LogCaptureHandler,
    caplog_handler_key,
    caplog_records_key,
)
from _pytest.monkeypatch import MonkeyPatch
from _pytest.stash import Stash
from dill.detect import globalvars
from pydantic._internal._model_construction import ModelMetaclass
from pydantic.fields import FieldInfo

Reduction: TypeAlias = tuple[Any, ...]
Reducer: TypeAlias = Callable[[Any], Reduction | str]


_PYDANTIC_GENERATED_ATTRIBUTES = frozenset(
    {
        "__abstractmethods__",
        "__class_vars__",
        "__dict__",
        "__firstlineno__",
        "__private_attributes__",
        "__signature__",
        "__static_attributes__",
        "__weakref__",
        "_abc_impl",
    }
)

_PYTHON_CLASS_GENERATED_ATTRIBUTES = frozenset(
    {
        "__abstractmethods__",
        "__dict__",
        "__firstlineno__",
        "__static_attributes__",
        "__weakref__",
        "_abc_impl",
    }
)


class _ModuleReference:
    """Durable reference that excludes a loaded module's mutable dictionary."""

    def __init__(self, name: str) -> None:
        self.name = name


class _FunctionSelfReference:
    """Temporary globals value that restoration replaces with a function."""


class _PydanticSelfReference:
    """Temporary closure value that restoration replaces with a new class."""


class _BoundModuleMethod:
    """Durable function for a method that was bound to its owning module."""

    def __init__(self, function: types.FunctionType) -> None:
        self.function = function


def _restore_imported_module(name: str) -> types.ModuleType:
    return importlib.import_module(name)


def _restore_runtime_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


def _reduce_module(value: types.ModuleType) -> Reduction:
    """Preserve runtime module state and import installed modules by name.

    A module registered in ``sys.modules`` restores through the import system.
    A disconnected runtime module, such as the Flex sandbox shim, restores
    from its durable dictionary. The reducer excludes ``__builtins__``, the
    loader, the import specification, and cached bytecode path. These values
    belong to the source interpreter's import runtime. A restored runtime
    module keeps its graph cycles through reduction state.
    """
    name = value.__name__
    if sys.modules.get(name) is value:
        return (_restore_imported_module, (name,))
    state = vars(value).copy()
    for attribute in ("__builtins__", "__loader__", "__spec__", "__cached__"):
        state.pop(attribute, None)
    return (_restore_runtime_module, (name,), state)


def _make_cell(value: Any) -> Any:
    def capture() -> Any:
        return value

    assert capture.__closure__ is not None
    return capture.__closure__[0]


def _clone_function_without_class_cycle(
    function: types.FunctionType,
    owner: type[Any],
    memo: dict[int, types.FunctionType] | None = None,
    visiting: set[int] | None = None,
) -> types.FunctionType:
    memo = {} if memo is None else memo
    visiting = set() if visiting is None else visiting
    if id(function) in memo:
        return memo[id(function)]
    if id(function) in visiting:
        return function
    visiting.add(id(function))
    changed = False
    cells = []
    for cell in function.__closure__ or ():
        try:
            contents = cell.cell_contents
        except ValueError:
            cells.append(cell)
            continue
        if contents is owner:
            cells.append(_make_cell(_PydanticSelfReference()))
            changed = True
        elif isinstance(contents, types.FunctionType):
            cloned = _clone_function_without_class_cycle(contents, owner, memo, visiting)
            cells.append(_make_cell(cloned))
            changed |= cloned is not contents
        else:
            cells.append(cell)
    attributes = {}
    for name, value in function.__dict__.items():
        if isinstance(value, types.FunctionType):
            cloned = _clone_function_without_class_cycle(value, owner, memo, visiting)
            attributes[name] = cloned
            changed |= cloned is not value
        else:
            attributes[name] = value
    visiting.remove(id(function))
    if not changed:
        memo[id(function)] = function
        return function
    restored = types.FunctionType(
        function.__code__,
        function.__globals__,
        function.__name__,
        function.__defaults__,
        tuple(cells) if function.__closure__ is not None else None,
    )
    memo[id(function)] = restored
    restored.__dict__.update(attributes)
    restored.__kwdefaults__ = function.__kwdefaults__
    restored.__annotations__ = function.__annotations__
    restored.__doc__ = function.__doc__
    restored.__qualname__ = function.__qualname__
    restored.__module__ = function.__module__
    return restored


def _remove_pydantic_class_cycle(member: Any, owner: type[Any]) -> Any:
    if isinstance(member, classmethod):
        return classmethod(_clone_function_without_class_cycle(member.__func__, owner))
    if isinstance(member, staticmethod):
        return staticmethod(_clone_function_without_class_cycle(member.__func__, owner))
    if isinstance(member, types.FunctionType):
        return _clone_function_without_class_cycle(member, owner)
    if isinstance(member, property):
        return property(
            _clone_function_without_class_cycle(member.fget, owner) if member.fget else None,
            _clone_function_without_class_cycle(member.fset, owner) if member.fset else None,
            _clone_function_without_class_cycle(member.fdel, owner) if member.fdel else None,
            member.__doc__,
        )
    return member


def _bind_pydantic_class_closures(member: Any, owner: type[Any]) -> None:
    functions: list[types.FunctionType] = []
    if isinstance(member, classmethod | staticmethod):
        functions.append(member.__func__)
    elif isinstance(member, types.FunctionType):
        functions.append(member)
    elif isinstance(member, property):
        functions.extend(function for function in (member.fget, member.fset, member.fdel) if function is not None)
    pending = list(functions)
    seen: set[int] = set()
    while pending:
        function = pending.pop()
        if id(function) in seen:
            continue
        seen.add(id(function))
        for cell in function.__closure__ or ():
            try:
                contents = cell.cell_contents
                if isinstance(contents, _PydanticSelfReference):
                    cell.cell_contents = owner
                elif isinstance(contents, types.FunctionType):
                    pending.append(contents)
            except ValueError:
                continue
        pending.extend(value for value in function.__dict__.values() if isinstance(value, types.FunctionType))


def _is_importable_class(value: type[Any]) -> bool:
    """Return true when the module resolves the class by its qualified name."""
    if value.__module__ is None or "<locals>" in value.__qualname__ or value.__module__.startswith("test_"):
        return False
    try:
        resolved: Any = importlib.import_module(value.__module__)
    except ImportError:
        return False
    for part in value.__qualname__.split("."):
        if resolved is None:
            return False
        resolved = getattr(resolved, part, None)
    return resolved is value


def _restore_pydantic_class(
    metaclass: type[Any],
    name: str,
    bases: tuple[type[Any], ...],
    namespace: dict[str, Any],
) -> type[Any]:
    """Create a Pydantic class and regenerate its compiled schema resources."""
    restored = metaclass(name, bases, namespace)
    for member in namespace.values():
        _bind_pydantic_class_closures(member, restored)
    return restored


def _reduce_pydantic_class(value: type[Any]) -> Reduction | str:
    """Preserve declarations and exclude generated Pydantic schema resources.

    The excluded ``__pydantic_*`` attributes contain compiled validators,
    serializers, schemas, and recursive links to the original class. Pydantic
    regenerates these attributes when the restored metaclass creates the class.

    The other excluded attributes are runtime products of the Python and ABC
    metaclasses. The restored metaclass regenerates them too.
    """
    if _is_importable_class(value):
        return value.__qualname__

    namespace = {
        name: _remove_pydantic_class_cycle(member, value)
        for name, member in vars(value).items()
        if name not in _PYDANTIC_GENERATED_ATTRIBUTES and not name.startswith("__pydantic_")
    }
    annotations = dict(getattr(value, "__annotations__", {}))
    namespace["__annotations__"] = annotations

    # Pydantic removes field declarations from the class namespace. Rebuild
    # each declaration from durable attributes and metadata. Do not copy the
    # resolved annotation because it can refer to the source class.
    for name in annotations:
        field = value.model_fields.get(name)
        if field is not None:
            field_state = field.asdict()
            restored_field = FieldInfo(**field_state["attributes"])
            restored_field.metadata = copy.copy(field_state["metadata"])
            namespace[name] = restored_field

    for name, private_attribute in getattr(value, "__private_attributes__", {}).items():
        namespace[name] = private_attribute

    return (
        _restore_pydantic_class,
        (type(value), value.__name__, value.__bases__, namespace),
    )


def _restore_python_class(
    metaclass: type[Any],
    name: str,
    bases: tuple[type[Any], ...],
    namespace: dict[str, Any],
) -> type[Any]:
    """Create a local Python class without importing its pytest module."""
    restored = metaclass(name, bases, namespace)
    for attribute_name, member in namespace.items():
        if isinstance(member, _PydanticSelfReference):
            setattr(restored, attribute_name, restored)
        else:
            _bind_pydantic_class_closures(member, restored)
    return restored


def _restore_dataclass_class(
    metaclass: type[Any],
    name: str,
    bases: tuple[type[Any], ...],
    namespace: dict[str, Any],
    fields: list[tuple[str, bool, Any, bool, Any, bool, bool, bool | None, bool, dict[Any, Any], bool]],
    options: dict[str, bool],
) -> type[Any]:
    for (
        field_name,
        has_default,
        default,
        has_factory,
        default_factory,
        init,
        repr_value,
        hash_value,
        compare,
        metadata,
        kw_only,
    ) in fields:
        field_options: dict[str, Any] = {
            "init": init,
            "repr": repr_value,
            "hash": hash_value,
            "compare": compare,
            "metadata": metadata,
            "kw_only": kw_only,
        }
        if has_default:
            field_options["default"] = default
        if has_factory:
            field_options["default_factory"] = default_factory
        namespace[field_name] = dataclasses.field(**field_options)
    restored = metaclass(name, bases, namespace)
    restored = dataclasses.dataclass(restored, **options)
    for member in namespace.values():
        _bind_pydantic_class_closures(member, restored)
    return restored


def _reduce_dataclass_class(value: type[Any]) -> Reduction:
    generated = {
        "__dataclass_fields__",
        "__dataclass_params__",
        "__delattr__",
        "__eq__",
        "__ge__",
        "__gt__",
        "__hash__",
        "__init__",
        "__le__",
        "__lt__",
        "__match_args__",
        "__replace__",
        "__repr__",
        "__setattr__",
        "__slots__",
    }
    slots = value.__dict__.get("__slots__", ())
    if isinstance(slots, str):
        slots = (slots,)
    excluded = _PYTHON_CLASS_GENERATED_ATTRIBUTES | generated | set(slots)
    namespace = {
        name: _PydanticSelfReference() if member is value else _remove_pydantic_class_cycle(member, value)
        for name, member in vars(value).items()
        if name not in excluded
    }
    field_states = []
    for field in dataclasses.fields(value):
        has_default = field.default is not dataclasses.MISSING
        has_factory = field.default_factory is not dataclasses.MISSING
        field_states.append(
            (
                field.name,
                has_default,
                field.default if has_default else None,
                has_factory,
                field.default_factory if has_factory else None,
                field.init,
                field.repr,
                field.hash,
                field.compare,
                dict(field.metadata),
                field.kw_only,
            )
        )
    params = value.__dataclass_params__
    options = {
        name: getattr(params, name)
        for name in (
            "init",
            "repr",
            "eq",
            "order",
            "unsafe_hash",
            "frozen",
            "match_args",
            "kw_only",
            "slots",
            "weakref_slot",
        )
    }
    return (
        _restore_dataclass_class,
        (type(value), value.__name__, value.__bases__, namespace, field_states, options),
    )


def _reduce_python_class(value: type[Any]) -> Reduction | str:
    """Preserve non-importable Python classes by value.

    Pytest gives test modules short names such as ``test_callback``. These
    names are not importable after pytest exits. Dill otherwise stores their
    classes as module references. The reducer stores bases, methods, class
    attributes, annotations, and self-referential method closures. The Python
    metaclass regenerates dictionary, weak-reference, slot, and ABC runtime
    descriptors.
    """
    if value in {type(None), type(Ellipsis), type(NotImplemented)}:
        return (type, ({type(None): None, type(Ellipsis): Ellipsis, type(NotImplemented): NotImplemented}[value],))
    if _is_importable_class(value):
        return value.__qualname__
    if dataclasses.is_dataclass(value):
        return _reduce_dataclass_class(value)
    slots = value.__dict__.get("__slots__", ())
    if isinstance(slots, str):
        slots = (slots,)
    excluded = _PYTHON_CLASS_GENERATED_ATTRIBUTES | frozenset(slots)
    namespace = {
        name: _PydanticSelfReference() if member is value else _remove_pydantic_class_cycle(member, value)
        for name, member in vars(value).items()
        if name not in excluded
    }
    return (
        _restore_python_class,
        (type(value), value.__name__, value.__bases__, namespace),
    )


def _context_value(value: contextvars.ContextVar[Any]) -> tuple[bool, Any]:
    sentinel = object()
    current = value.get(sentinel)
    return current is not sentinel, current


def _restore_context_var(name: str, has_effective_value: bool, effective_value: Any) -> contextvars.ContextVar[Any]:
    """Create a disconnected context variable with the captured effective value."""
    if has_effective_value:
        return contextvars.ContextVar(name, default=effective_value)
    return contextvars.ContextVar(name)


def _reduce_context_var(value: contextvars.ContextVar[Any]) -> Reduction:
    """Preserve the name and effective value, but exclude context bindings.

    Python does not expose a ContextVar default separately from the current
    context. The restored effective value becomes the new variable's default.
    """
    has_effective_value, effective_value = _context_value(value)
    return (
        _restore_context_var,
        (value.name, has_effective_value, effective_value),
    )


def _restore_context_token(
    variable: contextvars.ContextVar[Any],
    old_value_is_missing: bool,
    old_value: Any,
    current_value_is_missing: bool,
    current_value: Any,
) -> contextvars.Token[Any]:
    """Create a new active token that has the captured transition state."""
    if not old_value_is_missing:
        variable.set(old_value)
    if current_value_is_missing:
        current_value = None
    return variable.set(current_value)


def _reduce_context_token(value: contextvars.Token[Any]) -> Reduction:
    """Preserve the variable transition, but exclude token interpreter state.

    Python does not expose whether a token was already reset. Restoration
    always creates a new active token. Its variable and old value stay usable.
    """
    old_value_is_missing = value.old_value is contextvars.Token.MISSING
    has_current_value, current_value = _context_value(value.var)
    return (
        _restore_context_token,
        (
            value.var,
            old_value_is_missing,
            None if old_value_is_missing else value.old_value,
            not has_current_value,
            current_value,
        ),
    )


def _generator_for_state(state: str) -> types.GeneratorType:
    """Create a safe generator that has the requested lifecycle state."""

    def safe_generator():
        yield None

    restored = safe_generator()
    if state == inspect.GEN_SUSPENDED:
        next(restored)
    elif state in {inspect.GEN_CLOSED, inspect.GEN_RUNNING}:
        restored.close()
    return restored


def _reduce_generator(value: types.GeneratorType) -> Reduction:
    """Preserve lifecycle state, but exclude the live frame and value stream.

    Python does not provide a safe API to copy a generator instruction pointer
    or frame locals. The restored generator does not replay source code or
    yield source values.
    """
    return (_generator_for_state, (inspect.getgeneratorstate(value),))


async def _safe_async_generator():
    yield None


def _restore_async_generator(state: str) -> types.AsyncGeneratorType:
    """Create a disconnected async generator with a safe lifecycle state."""
    restored = _safe_async_generator()
    if state == "AGEN_SUSPENDED":
        try:
            restored.__anext__().send(None)
        except StopIteration:
            pass
    elif state in {"AGEN_CLOSED", "AGEN_RUNNING"}:
        try:
            restored.aclose().send(None)
        except StopIteration:
            pass
    return restored


def _reduce_async_generator(value: types.AsyncGeneratorType) -> Reduction:
    """Preserve lifecycle state, but exclude the live async frame and awaiter.

    The restored generator yields only a safe null value. It does not replay
    source code or preserve source-frame locals.
    """
    return (_restore_async_generator, (inspect.getasyncgenstate(value),))


async def _safe_coroutine() -> None:
    return None


def _restore_coroutine(state: str) -> types.CoroutineType:
    restored = _safe_coroutine()
    if state in {inspect.CORO_CLOSED, inspect.CORO_RUNNING}:
        restored.close()
    return restored


def _reduce_coroutine(value: types.CoroutineType) -> Reduction:
    """Preserve lifecycle state, but exclude the frame and awaited resource.

    A suspended source coroutine restores as a safe created coroutine because
    Python cannot set a coroutine instruction pointer. The restored coroutine
    returns null and does not execute source code.
    """
    return (_restore_coroutine, (inspect.getcoroutinestate(value),))


async def _restored_task_result(result: Any) -> Any:
    return result


async def _restored_task_exception(error: BaseException) -> None:
    raise error


async def _restored_pending_task() -> None:
    await asyncio.Event().wait()


def _restore_task(
    state: str,
    name: str,
    result: Any,
    error: BaseException | None,
) -> asyncio.Task[Any]:
    loop = asyncio.get_running_loop()
    if state == "result":
        coroutine = _restored_task_result(result)
    elif state == "exception":
        assert error is not None
        coroutine = _restored_task_exception(error)
    else:
        coroutine = _restored_pending_task()
    restored = loop.create_task(coroutine, name=name)
    if state == "cancelled":
        restored.cancel()
    return restored


def _reduce_task(value: asyncio.Task[Any]) -> Reduction:
    """Preserve task outcome and exclude event-loop runtime resources.

    The reducer excludes the source coroutine, loop, waiter future, callbacks,
    and execution context. Restoration requires a running event loop. A
    pending task restores as a safe pending task. A completed task preserves
    its result or exception. A cancelled task restores as cancelled.
    """
    if not value.done():
        state, result, error = "pending", None, None
    elif value.cancelled():
        state, result, error = "cancelled", None, None
    elif (error := value.exception()) is not None:
        state, result = "exception", None
    else:
        state, result, error = "result", value.result(), None
    return (_restore_task, (state, value.get_name(), result, error))


def _restore_thread_pool_executor(
    max_workers: int,
    thread_name_prefix: str,
    initializer: Callable[..., Any] | None,
    initargs: tuple[Any, ...],
    is_shutdown: bool,
    broken: Any,
) -> ThreadPoolExecutor:
    restored = ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix=thread_name_prefix,
        initializer=initializer,
        initargs=initargs,
    )
    restored._broken = broken
    if is_shutdown:
        restored.shutdown(wait=False, cancel_futures=True)
    return restored


def _reduce_thread_pool_executor(value: ThreadPoolExecutor) -> Reduction:
    """Preserve executor configuration, shutdown state, and broken state.

    The reducer excludes ``_work_queue``, ``_idle_semaphore``, ``_threads``,
    and ``_shutdown_lock``. These attributes contain pending work, live
    threads, semaphores, queues, and locks. The constructor creates new empty
    runtime resources.
    """
    return (
        _restore_thread_pool_executor,
        (
            value._max_workers,
            value._thread_name_prefix,
            value._initializer,
            value._initargs,
            value._shutdown,
            value._broken,
        ),
    )


def _restore_event(is_set: bool) -> asyncio.Event:
    restored = asyncio.Event()
    if is_set:
        restored.set()
    return restored


def _reduce_event(value: asyncio.Event) -> Reduction:
    """Preserve the flag and exclude the event loop and waiter futures."""
    return (_restore_event, (value.is_set(),))


def _restore_capacity_limiter(total_tokens: float) -> Any:
    from anyio._backends._asyncio import CapacityLimiter

    return CapacityLimiter(total_tokens)


def _reduce_capacity_limiter(value: Any) -> Reduction:
    """Preserve capacity and exclude borrowers and pending task waiters.

    Borrowers and the wait queue contain live tasks and event-loop resources.
    The restored limiter has the same total capacity and no active loans.
    """
    return (_restore_capacity_limiter, (value.total_tokens,))


def _restore_thread(
    target: Callable[..., Any] | None,
    name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    daemon: bool,
    original_state: str,
    ident: int | None,
    native_id: int | None,
) -> threading.Thread:
    restored = threading.Thread(
        target=target,
        name=name,
        args=args,
        kwargs=kwargs,
        daemon=daemon,
    )
    restored._spacetime_original_state = original_state
    restored._spacetime_original_ident = ident
    restored._spacetime_original_native_id = native_id
    return restored


def _reduce_thread(value: threading.Thread) -> Reduction:
    """Preserve thread configuration and identity metadata.

    The reducer excludes the thread handle, started event, standard-error
    stream, exception hook, and live operating-system thread. A source target
    is preserved only before the source thread starts. The restored thread is
    always safe and unstarted.
    """
    started = value._started.is_set()
    original_state = "running" if value.is_alive() else "completed" if started else "initial"
    target = getattr(value, "_target", None) if not started else None
    args = getattr(value, "_args", ()) if not started else ()
    kwargs = getattr(value, "_kwargs", {}) if not started else {}
    return (
        _restore_thread,
        (
            target,
            value.name,
            args,
            kwargs,
            value.daemon,
            original_state,
            value.ident,
            value.native_id,
        ),
    )


def _restore_dict_reader(
    fieldnames: list[str] | None,
    restkey: str | None,
    restval: Any,
    dialect: Any,
    line_number: int,
) -> csv.DictReader:
    stream = io.StringIO("\n" * line_number)
    restored = csv.DictReader(
        stream,
        fieldnames=fieldnames,
        restkey=restkey,
        restval=restval,
        dialect=dialect,
    )
    for _ in range(line_number):
        next(restored.reader, None)
    return restored


def _reduce_dict_reader(value: csv.DictReader) -> Reduction:
    """Preserve parser configuration and line position.

    The reducer excludes the native CSV parser and its input stream. Python
    does not expose the unconsumed parser buffer without consuming the source.
    The restored reader has an empty disconnected stream.
    """
    return (
        _restore_dict_reader,
        (
            value._fieldnames,
            value.restkey,
            value.restval,
            value.dialect,
            value.line_num,
        ),
    )


def _restore_exception_info(
    exception_type: type[BaseException] | None,
    exception_value: BaseException | None,
    striptext: str,
    formatted_traceback: str,
) -> ExceptionInfo[BaseException]:
    if exception_type is None or exception_value is None:
        restored = ExceptionInfo.for_later()
        restored._striptext = striptext
        restored._spacetime_formatted_traceback = formatted_traceback
        return restored
    restored = ExceptionInfo(
        (exception_type, exception_value, None),
        striptext,
        Traceback([]),
        _ispytest=True,
    )
    restored._spacetime_formatted_traceback = formatted_traceback
    return restored


def _reduce_exception_info(value: ExceptionInfo[Any]) -> Reduction:
    """Preserve exception data and text, but exclude traceback frames.

    ``type``, ``value``, and ``exconly()`` keep their normal behavior. The
    restored ``traceback`` is empty. ``_spacetime_formatted_traceback`` keeps
    the source traceback text for display and diagnostics.
    """
    if value._excinfo is None:
        return (_restore_exception_info, (None, None, value._striptext, ""))
    return (
        _restore_exception_info,
        (
            value.type,
            value.value,
            value._striptext,
            str(value.getrepr(style="long", tbfilter=False)),
        ),
    )


def _restore_exception(
    cls: type[BaseException],
    arguments: tuple[Any, ...],
    state: dict[str, Any],
) -> BaseException:
    restored = BaseException.__new__(cls)
    BaseException.__init__(restored, *arguments)
    restored.__dict__.update(state)
    return restored


def _reduce_exception(value: BaseException) -> Reduction:
    """Preserve exception arguments and attributes without calling its constructor.

    The reducer excludes ``__traceback__``, ``__context__``, and ``__cause__``.
    These attributes can contain raw traceback frames. An enclosing
    ``ExceptionInfo`` or ``LogRecord`` keeps formatted traceback text.
    """
    return (_restore_exception, (type(value), value.args, vars(value).copy()))


def _restore_log_record(state: dict[str, Any]) -> logging.LogRecord:
    return logging.makeLogRecord(state)


def _reduce_log_record(value: logging.LogRecord) -> Reduction:
    """Preserve log data and exclude raw exception and stack frames.

    The reducer stores the exception type, value, and formatted traceback text
    in ``spacetime_exception_type``, ``spacetime_exception_value``, and
    ``spacetime_formatted_traceback``. The restored ``exc_info`` is null
    because Python cannot restore the source traceback frames.
    """
    state = vars(value).copy()
    if value.exc_info is not None:
        exception_type, exception_value, exception_traceback = value.exc_info
        state["spacetime_exception_type"] = exception_type
        state["spacetime_exception_value"] = exception_value
        state["spacetime_formatted_traceback"] = "".join(
            __import__("traceback").format_exception(exception_type, exception_value, exception_traceback)
        )
        state["exc_info"] = None
    return (_restore_log_record, (state,))


class _CapturedLogItem:
    """Minimal pytest item state that supports restored caplog properties."""

    def __init__(
        self,
        records_by_phase: dict[str, list[logging.LogRecord]],
        records: list[logging.LogRecord],
        text: str,
    ) -> None:
        handler = LogCaptureHandler()
        handler.records[:] = records
        handler.stream.write(text)
        self.stash = {
            caplog_handler_key: handler,
            caplog_records_key: records_by_phase,
        }


def _restore_log_capture_fixture(
    records_by_phase: dict[str, list[logging.LogRecord]],
    records: list[logging.LogRecord],
    text: str,
    initial_handler_level: int | None,
    initial_logger_levels: dict[str | None, int],
    initial_disabled_logging_level: int | None,
) -> LogCaptureFixture:
    restored = LogCaptureFixture(_CapturedLogItem(records_by_phase, records, text), _ispytest=True)
    restored._initial_handler_level = initial_handler_level
    restored._initial_logger_levels = initial_logger_levels
    restored._initial_disabled_logging_level = initial_disabled_logging_level
    return restored


def _reduce_log_capture_fixture(value: LogCaptureFixture) -> Reduction:
    """Preserve captured logs and exclude the live pytest item and session.

    The restored ``records``, ``record_tuples``, ``messages``, ``text``,
    ``get_records()``, and ``clear()`` behaviors remain available. Logger-level
    context managers cannot restore changes to the source pytest session.
    """
    records_by_phase = {phase: list(value.get_records(phase)) for phase in ("setup", "call", "teardown")}
    return (
        _restore_log_capture_fixture,
        (
            records_by_phase,
            list(value.records),
            value.text,
            value._initial_handler_level,
            value._initial_logger_levels.copy(),
            value._initial_disabled_logging_level,
        ),
    )


def _restore_lazy_module(module_name: str) -> Any:
    from dspy.utils.lazy_import import require

    return require(module_name)


def _reduce_lazy_module(value: Any) -> Reduction:
    """Preserve the module name and exclude the import specification and lock."""
    return (_restore_lazy_module, (value.__name__,))


def _is_importable_function(value: types.FunctionType) -> bool:
    if value.__module__ is None or "<locals>" in value.__qualname__ or value.__module__.startswith("test_"):
        return False
    try:
        resolved: Any = importlib.import_module(value.__module__)
    except ImportError:
        return False
    for part in value.__qualname__.split("."):
        if resolved is None:
            return False
        resolved = getattr(resolved, part, None)
    return resolved is value


def _restore_function(
    code: types.CodeType,
    globals_state: dict[str, Any],
    name: str,
    defaults: tuple[Any, ...] | None,
    closure: tuple[Any, ...] | None,
    attributes: dict[str, Any],
    keyword_defaults: dict[str, Any] | None,
    annotations: dict[str, Any],
    doc: str | None,
    qualified_name: str,
    module_name: str | None,
) -> types.FunctionType:
    globals_state = {
        key: importlib.import_module(item.name) if isinstance(item, _ModuleReference) else item
        for key, item in globals_state.items()
    }
    globals_state.setdefault("__builtins__", __builtins__)
    restored = types.FunctionType(code, globals_state, name, defaults, closure)
    for key, item in tuple(globals_state.items()):
        if isinstance(item, _FunctionSelfReference):
            globals_state[key] = restored
    restored.__dict__.update(attributes)
    restored.__kwdefaults__ = keyword_defaults
    restored.__annotations__ = annotations
    restored.__doc__ = doc
    restored.__qualname__ = qualified_name
    restored.__module__ = module_name
    return restored


def _reduce_function(value: types.FunctionType) -> Reduction | str:
    """Preserve a function without copying unused module globals.

    Dill copies a complete globals dictionary when it cannot import the source
    module. Pytest loads many test files as non-importable top-level modules.
    Those dictionaries contain patched mocks and live pytest resources that
    the function does not use. This reducer stores only bytecode-referenced
    globals. It preserves the code, defaults, closure, annotations, function
    attributes, qualified name, and module name.
    """
    if _is_importable_function(value):
        return value.__qualname__
    globals_state = globalvars(value, recurse=False, builtin=False)
    globals_state = {
        key: (
            _ModuleReference(item.__name__)
            if isinstance(item, types.ModuleType)
            else _FunctionSelfReference()
            if item is value
            else item
        )
        for key, item in globals_state.items()
    }
    globals_state["__name__"] = value.__module__
    return (
        _restore_function,
        (
            value.__code__,
            globals_state,
            value.__name__,
            value.__defaults__,
            value.__closure__,
            value.__dict__,
            value.__kwdefaults__,
            value.__annotations__,
            value.__doc__,
            value.__qualname__,
            value.__module__,
        ),
    )


def _restore_python_instance(cls: type[Any], state: dict[str, Any]) -> Any:
    restored = object.__new__(cls)
    restored.__dict__.update(state)
    return restored


def _restore_module_instance(cls: type[Any]) -> Any:
    return object.__new__(cls)


def _reduce_module_instance(value: Any) -> Reduction:
    """Preserve DSPy module state and memoize before bound-method state.

    ``bootstrap_trace_data`` stores the class ``forward`` method as a bound
    method in the instance dictionary. That value duplicates descriptor
    lookup and creates a cycle to the instance. The reducer excludes only this
    duplicate. The restored class descriptor creates the same bound method.
    A custom instance-bound ``forward`` stores its function and binds it to the
    restored module. Other instance state is applied after memoization, which
    preserves graph cycles.
    """
    state = vars(value).copy()
    forward = state.get("forward")
    class_forward = getattr(type(value), "forward", None)
    if isinstance(forward, types.MethodType) and forward.__self__ is value:
        if forward.__func__ is class_forward:
            del state["forward"]
        else:
            state["forward"] = _BoundModuleMethod(forward.__func__)
    return (_restore_module_instance, (type(value),), state)


def _module_reduce_ex(value: Any, protocol: int) -> Reduction:
    return _reduce_module_instance(value)


def _restore_module_instance_state(value: Any, state: dict[str, Any]) -> None:
    forward = state.get("forward")
    if isinstance(forward, _BoundModuleMethod):
        state["forward"] = types.MethodType(forward.function, value)
    value.__dict__.update(state)
    value.__dict__.setdefault("history", [])
    value.__dict__.setdefault("callbacks", [])


def _install_module_instance_reducer() -> None:
    """Install one inherited reducer for current and future Module subclasses."""
    from dspy.primitives.module import Module

    Module.__reduce_ex__ = _module_reduce_ex
    Module.__setstate__ = _restore_module_instance_state


def _reduce_better_together(value: Any) -> Reduction:
    """Preserve optimizer state and exclude a patched method mock.

    DSPy does not store ``_models_changed`` on a normal BetterTogether
    instance. ``unittest.mock.patch.object`` adds it during tests. A restored
    instance uses the concrete class method instead of the source test mock.
    """
    state = vars(value).copy()
    if isinstance(state.get("_models_changed"), Mock):
        del state["_models_changed"]
    return (_restore_python_instance, (type(value), state))


def _restore_mock(kind: str) -> Mock:
    return MagicMock() if kind == "magic" else Mock()


def _reduce_mock(value: Mock) -> Reduction:
    """Preserve mock behavior, call history, children, and graph references."""
    kind = "magic" if isinstance(value, MagicMock) else "mock"
    state = vars(value).copy()
    call_args = state.get("_mock_call_args")
    state["_mock_call_args"] = None if call_args is None else tuple(call_args)
    for name in ("_mock_call_args_list", "_mock_mock_calls", "method_calls"):
        state[name] = [tuple(item) for item in state[name]]
    return (_restore_mock, (kind,), state)


def _restore_mock_state(value: Mock, state: dict[str, Any]) -> None:
    """Apply state without invoking Mock attribute lookup during Dill BUILD."""
    call_args = state["_mock_call_args"]
    state["_mock_call_args"] = None if call_args is None else _Call(call_args, two=True)
    for name in ("_mock_call_args_list", "_mock_mock_calls", "method_calls"):
        state[name] = _CallList([_Call(item, two=len(item) == 2) for item in state[name]])
    value.__dict__.update(state)


def _restore_call(value: tuple[Any, ...]) -> _Call:
    return _Call(value, two=len(value) == 2)


def _reduce_call(value: _Call) -> Reduction:
    """Preserve a mock call as its durable name, arguments, and keywords."""
    return (_restore_call, (tuple(value),))


def _restore_call_list(values: list[tuple[Any, ...]]) -> _CallList:
    return _CallList([_restore_call(value) for value in values])


def _reduce_call_list(value: _CallList) -> Reduction:
    """Preserve ordered mock-call records without Mock's recursive lookup."""
    return (_restore_call_list, ([tuple(item) for item in value],))


def _mock_reduce_ex(value: Mock, protocol: int) -> Reduction:
    return _reduce_mock(value)


def _install_dynamic_mock_reducer() -> None:
    """Install one reducer for future per-instance Mock subclasses.

    SpaceTimePy dispatch applies only to exact types. ``Mock`` creates a new
    subclass for each instance after the dispatch table is built. Inherited
    ``__reduce_ex__`` is the smallest scope that can handle these future types.
    """
    Mock.__reduce_ex__ = _mock_reduce_ex
    MagicMock.__reduce_ex__ = _mock_reduce_ex
    Mock.__setstate__ = _restore_mock_state
    MagicMock.__setstate__ = _restore_mock_state


def _restore_enum_class(
    name: str,
    bases: tuple[type[Any], ...],
    members: list[tuple[str, Any]],
    module_name: str | None,
    qualified_name: str,
    attributes: dict[str, Any],
) -> type[enum.Enum]:
    enum_base = next(base for base in bases if isinstance(base, enum.EnumType) and issubclass(base, enum.Enum))
    mixin = next(
        (base for base in bases if not (isinstance(base, enum.EnumType) and issubclass(base, enum.Enum))),
        None,
    )
    options: dict[str, Any] = {
        "module": module_name,
        "qualname": qualified_name,
    }
    if mixin is not None:
        options["type"] = mixin
    restored = enum_base(name, members, **options)
    for attribute_name, attribute_value in attributes.items():
        setattr(restored, attribute_name, attribute_value)
    return restored


def _reduce_enum_class(value: type[enum.Enum]) -> Reduction | str:
    """Preserve local enum members and custom class attributes."""
    if _is_importable_class(value):
        return value.__qualname__
    member_names = set(value.__members__)
    excluded = {
        "__dict__",
        "__weakref__",
        "__new__",
        "_member_names_",
        "_member_map_",
        "_value2member_map_",
        "_unhashable_values_",
        "_member_type_",
        "_value_repr_",
        "_boundary_",
        "_new_member_",
        "_use_args_",
    }
    attributes = {
        name: member
        for name, member in vars(value).items()
        if name not in excluded and name not in member_names and not name.startswith("__")
    }
    members = [(name, member.value) for name, member in value.__members__.items()]
    return (
        _restore_enum_class,
        (
            value.__name__,
            value.__bases__,
            members,
            value.__module__,
            value.__qualname__,
            attributes,
        ),
    )


def _restore_temporary_file_wrapper(
    content: bytes | str | None,
    position: int,
    name: str,
    mode: str,
    was_closed: bool,
    source_delete: bool,
    source_delete_on_close: bool,
) -> tempfile._TemporaryFileWrapper:
    buffer: Any = io.BytesIO(content or b"") if "b" in mode else io.StringIO(content or "")
    buffer.name = name
    buffer.mode = mode
    buffer.seek(position)
    restored = tempfile._TemporaryFileWrapper(
        buffer,
        name,
        delete=False,
        delete_on_close=False,
    )
    restored._spacetime_source_delete = source_delete
    restored._spacetime_source_delete_on_close = source_delete_on_close
    if was_closed:
        restored.close()
    return restored


def _reduce_temporary_file_wrapper(value: tempfile._TemporaryFileWrapper) -> Reduction:
    """Preserve file content and position, but exclude the file descriptor.

    The restored wrapper uses an in-memory stream. It never removes the source
    path. The source delete flags remain available in ``_spacetime_source_*``.
    The reducer reads the durable path instead of the source descriptor. It
    excludes unflushed userspace buffers so serialization does not flush or
    seek the source stream.
    """
    file = value.file
    was_closed = file.closed
    content: bytes | str | None = None
    position = 0
    if not was_closed and file.seekable():
        position = file.tell()
    try:
        with open(value.name, "rb") as durable_file:
            durable_content = durable_file.read()
    except OSError:
        durable_content = b""
    content = (
        durable_content
        if "b" in file.mode
        else durable_content.decode(file.encoding or "utf-8", errors=file.errors or "strict")
    )
    mode = getattr(file, "mode", "b" if isinstance(content, bytes) else "w+")
    return (
        _restore_temporary_file_wrapper,
        (
            content,
            position,
            value.name,
            mode,
            was_closed,
            value._closer.delete,
            value._closer.delete_on_close,
        ),
    )


def _restore_parallel_executor(state: dict[str, Any]) -> Any:
    from dspy.utils.parallelizer import ParallelExecutor

    restored = object.__new__(ParallelExecutor)
    restored.__dict__.update(state)
    restored.error_lock = threading.Lock()
    restored.cancel_jobs = threading.Event()
    if state["cancel_jobs_was_set"]:
        restored.cancel_jobs.set()
    del restored.cancel_jobs_was_set
    return restored


def _reduce_parallel_executor(value: Any) -> Reduction:
    """Preserve executor counters and options, but exclude locks and method mocks."""
    state = vars(value).copy()
    state.pop("error_lock", None)
    cancel_jobs = state.pop("cancel_jobs")
    state["cancel_jobs_was_set"] = cancel_jobs.is_set()
    if isinstance(state.get("_update_progress"), Mock):
        del state["_update_progress"]
    return (_restore_parallel_executor, (state,))


def _reduce_bootstrap_finetune(value: Any) -> Reduction:
    """Preserve teleprompter state and exclude a patched finetune method mock."""
    state = vars(value).copy()
    if isinstance(state.get("finetune_lms"), Mock):
        del state["finetune_lms"]
    return (_restore_python_instance, (type(value), state))


def _restore_cache(
    enable_disk_cache: bool,
    enable_memory_cache: bool,
    disk_cache_dir: str | None,
    memory_max_entries: int,
    memory_items: list[tuple[Any, Any]],
) -> Any:
    from dspy.clients.cache import Cache

    restored = Cache(
        enable_disk_cache=enable_disk_cache,
        enable_memory_cache=enable_memory_cache,
        disk_cache_dir=disk_cache_dir,
        memory_max_entries=memory_max_entries,
    )
    restored.memory_cache.update(memory_items)
    return restored


def _reduce_cache(value: Any) -> Reduction:
    """Preserve cache configuration and memory entries.

    The reducer excludes ``_lock`` and the live ``FanoutCache`` object. The
    constructor creates a new lock and reopens the configured cache directory.
    A temporary method mock such as ``get`` is not part of normal cache state
    and is also excluded.
    """
    max_entries = getattr(value.memory_cache, "maxsize", 0)
    return (
        _restore_cache,
        (
            value.enable_disk_cache,
            value.enable_memory_cache,
            value.disk_cache_dir,
            max_entries,
            list(value.memory_cache.items()),
        ),
    )


def _restore_capture_fixture(
    captureclass: type[Any],
    config: dict[str, Any],
    captured_out: Any,
    captured_err: Any,
) -> CaptureFixture[Any]:
    restored = object.__new__(CaptureFixture)
    restored.captureclass = captureclass
    restored.request = None
    restored._config = config
    restored._capture = None
    restored._captured_out = captured_out
    restored._captured_err = captured_err
    return restored


def _reduce_capture_fixture(value: CaptureFixture[Any]) -> Reduction:
    """Preserve captured buffers and exclude the active capture manager.

    The reducer excludes ``request`` and ``_capture``. They contain the pytest
    session, file descriptors, temporary files, and stream proxies. The source
    fixture can contain unread live output. Reading that output would change
    the source fixture, so only its durable captured buffers are preserved.
    """
    return (
        _restore_capture_fixture,
        (
            value.captureclass,
            value._config,
            value._captured_out,
            value._captured_err,
        ),
    )


def _restore_config(state: dict[str, Any]) -> Config:
    restored = object.__new__(Config)
    restored.__dict__.update(state)
    restored._parser = None
    restored.pluginmanager = None
    restored.stash = Stash()
    restored._store = restored.stash
    restored.trace = None
    restored.hook = None
    restored._cleanup_stack = contextlib.ExitStack()
    return restored


def _reduce_config(value: Config) -> Reduction:
    """Preserve parsed options and paths, but exclude pytest runtime services.

    The reducer excludes ``_parser``, ``pluginmanager``, ``stash``, ``trace``,
    ``hook``, and ``_cleanup_stack``. These attributes contain plugins,
    callbacks, finalizers, streams, and session resources. Cached options and
    cached INI values remain available for inspection.
    """
    invocation = Config.InvocationParams(
        args=value.invocation_params.args,
        plugins=None,
        dir=value.invocation_params.dir,
    )
    state = {
        "option": value.option,
        "invocation_params": invocation,
        "_inicache": value._inicache,
        "_inicfg": value._inicfg,
        "_configured": value._configured,
        "args_source": value.args_source,
        "args": value.args,
        "_rootpath": value._rootpath,
        "_inipath": value._inipath,
        "_ignored_config_files": value._ignored_config_files,
        "known_args_namespace": value.known_args_namespace,
    }
    return (_restore_config, (state,))


class _CapturedPytestItem:
    """Durable pytest item data for a restored top-level request."""

    def __init__(
        self,
        name: str,
        nodeid: str,
        path: Any,
        fixturenames: list[str],
        function: Callable[..., Any],
        keywords: dict[str, Any],
        config: Config,
    ) -> None:
        self.name = name
        self.nodeid = nodeid
        self.path = path
        self.fixturenames = fixturenames
        self.obj = function
        self.keywords = keywords
        self.config = config
        self.funcargs: dict[str, Any] = {}
        self.session = None
        self.instance = None
        self._finalizers: list[Callable[[], object]] = []

    def getparent(self, cls: type[Any]) -> None:
        return None

    def add_marker(self, marker: Any) -> None:
        self.keywords[getattr(marker, "name", str(marker))] = marker

    def addfinalizer(self, finalizer: Callable[[], object]) -> None:
        self._finalizers.append(finalizer)


def _restore_top_request(
    item: _CapturedPytestItem,
    param_is_set: bool,
    param: Any,
) -> TopRequest:
    restored = object.__new__(TopRequest)
    restored.fixturename = None
    restored._pyfuncitem = item
    restored._arg2fixturedefs = {}
    restored._fixture_defs = {}
    if param_is_set:
        restored.param = param
    return restored


def _reduce_top_request(value: TopRequest) -> Reduction:
    """Preserve request identity and exclude active fixture definitions.

    The reducer excludes the pytest session, fixture manager, fixture
    definitions, evaluated fixture resources, and finalizers. The restored
    request supports identity, path, scope, function, keyword, and fixture-name
    inspection. It cannot resolve or execute source-session fixtures.
    """
    item = value.node
    captured_item = _CapturedPytestItem(
        item.name,
        item.nodeid,
        item.path,
        list(value.fixturenames),
        value.function,
        dict(value.keywords),
        value.config,
    )
    param_is_set = hasattr(value, "param")
    return (
        _restore_top_request,
        (captured_item, param_is_set, getattr(value, "param", None)),
    )


def _restore_monkeypatch(
    source_cwd: str | None,
    source_syspath: list[str] | None,
) -> MonkeyPatch:
    restored = MonkeyPatch()
    restored._spacetime_source_cwd = source_cwd
    restored._spacetime_source_syspath = source_syspath
    return restored


def _reduce_monkeypatch(value: MonkeyPatch) -> Reduction:
    """Preserve path snapshots and exclude live mutation undo operations.

    ``_setattr`` and ``_setitem`` contain source objects and values that the
    fixture must mutate during teardown. The reducer excludes these actions so
    the restored fixture cannot modify the source process. Source path state is
    available in the ``_spacetime_source_*`` attributes.
    """
    return (
        _restore_monkeypatch,
        (value._cwd, value._savesyspath),
    )


def get_dispatch_table() -> dict[type[Any], Reducer]:
    """Return exact Python types mapped to copyreg-style Dill reducers."""
    _install_dynamic_mock_reducer()
    _install_module_instance_reducer()
    table: dict[type[Any], Reducer] = {
        abc.ABCMeta: _reduce_python_class,
        asyncio.Event: _reduce_event,
        asyncio.Task: _reduce_task,
        CaptureFixture: _reduce_capture_fixture,
        Config: _reduce_config,
        contextvars.ContextVar: _reduce_context_var,
        contextvars.Token: _reduce_context_token,
        csv.DictReader: _reduce_dict_reader,
        enum.EnumType: _reduce_enum_class,
        ExceptionInfo: _reduce_exception_info,
        logging.LogRecord: _reduce_log_record,
        LogCaptureFixture: _reduce_log_capture_fixture,
        MonkeyPatch: _reduce_monkeypatch,
        ThreadPoolExecutor: _reduce_thread_pool_executor,
        threading.Thread: _reduce_thread,
        tempfile._TemporaryFileWrapper: _reduce_temporary_file_wrapper,
        type: _reduce_python_class,
        types.AsyncGeneratorType: _reduce_async_generator,
        types.CoroutineType: _reduce_coroutine,
        types.FunctionType: _reduce_function,
        types.GeneratorType: _reduce_generator,
        types.ModuleType: _reduce_module,
        ModelMetaclass: _reduce_pydantic_class,
        TopRequest: _reduce_top_request,
        _Call: _reduce_call,
        _CallList: _reduce_call_list,
    }

    # DSPy uses a ModelMetaclass subclass. SpaceTimePy dispatch is exact-type,
    # so the base metaclass registration does not apply to this subclass.
    from anyio._backends._asyncio import CapacityLimiter
    from litellm.exceptions import ContextWindowExceededError as LiteLLMContextWindowExceededError
    from litellm.exceptions import RateLimitError

    from dspy.clients.cache import Cache
    from dspy.primitives.module import ProgramMeta
    from dspy.signatures.signature import SignatureMeta
    from dspy.teleprompt.bettertogether import BetterTogether
    from dspy.teleprompt.bootstrap_finetune import BootstrapFinetune
    from dspy.utils.exceptions import AdapterParseError, ContextWindowExceededError
    from dspy.utils.lazy_import import _LazyModule
    from dspy.utils.parallelizer import ParallelExecutor

    table[AdapterParseError] = _reduce_exception
    table[CapacityLimiter] = _reduce_capacity_limiter
    table[Cache] = _reduce_cache
    table[BetterTogether] = _reduce_better_together
    table[BootstrapFinetune] = _reduce_bootstrap_finetune
    table[ParallelExecutor] = _reduce_parallel_executor
    table[ProgramMeta] = _reduce_python_class
    table[ContextWindowExceededError] = _reduce_exception
    table[LiteLLMContextWindowExceededError] = _reduce_exception
    table[RateLimitError] = _reduce_exception
    table[SignatureMeta] = _reduce_pydantic_class
    table[_LazyModule] = _reduce_lazy_module
    return table


__all__ = ["get_dispatch_table"]
