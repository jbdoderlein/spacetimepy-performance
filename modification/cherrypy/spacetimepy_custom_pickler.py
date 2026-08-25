"""Dill reducers for values that CherryPy tests expose to SpaceTimePy.

The reducers keep durable Python state. They do not copy live threads,
network sockets, network streams, pytest nodes, frames, or tracebacks.
"""

from __future__ import annotations

import datetime as _datetime_module
import importlib
import io
import itertools
import logging
import threading
import traceback
import unittest
import warnings
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPResponse
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import DEFAULT, MagicMock, call

if TYPE_CHECKING:
    from collections.abc import Callable

from _pytest.capture import EncodedFile
from _pytest.logging import (
    LogCaptureFixture,
    LogCaptureHandler,
    caplog_handler_key,
    caplog_records_key,
)
from _pytest.monkeypatch import MonkeyPatch
from _pytest.stash import Stash

from cherrypy._cpserver import Server
from cherrypy.lib.sessions import FileSession
from cherrypy.process.plugins import Monitor
from cherrypy.process.wspbus import Bus


_DATETIME_TYPE = _datetime_module.datetime
_THREAD_RUNTIME_ATTRIBUTES = frozenset(
    {
        # The handle refers to one operating-system thread.
        '_handle',
        # The event contains a lock and represents the original execution.
        '_started',
        # These identifiers do not identify a thread in the restore process.
        '_ident',
        '_native_id',
        # The stream can be a live pytest capture stream.
        '_stderr',
        # The hook closes over the original thread and stderr stream.
        '_invoke_excepthook',
    },
)
_STARTED_THREAD_CALL_ATTRIBUTES = frozenset(
    {
        # A started thread cannot execute its target again after restoration.
        '_target',
        # Arguments can contain locks and events from the original execution.
        '_args',
        '_kwargs',
    },
)
_CONNECTION_RUNTIME_ATTRIBUTES = frozenset(
    {
        # The socket is a live connection to the server.
        'sock',
        # The response can contain the same socket through its input stream.
        '_HTTPConnection__response',
        # An active transaction state is not valid without its socket.
        '_HTTPConnection__state',
        '_method',
        # The buffer can contain part of an active request.
        '_buffer',
    },
)
_TEST_CASE_RUNTIME_ATTRIBUTES = frozenset(
    {
        # The outcome contains the active pytest result and plugin graph.
        '_outcome',
        # Cleanup callbacks belong to the active test execution.
        '_cleanups',
        # The subtest context can contain active exception state.
        '_subtest',
    },
)


class _DateTimeTypeReference:
    """Represent the original datetime type in a MonkeyPatch undo stack."""


_DATETIME_TYPE_REFERENCE = _DateTimeTypeReference()


@dataclass(frozen=True)
class _ModuleReference:
    """Identify a MonkeyPatch module target without copying its graph."""

    module_name: str


class _MagicMockSnapshot:
    """Store the durable call state of one Bus callback mock."""

    def __init__(self, value: MagicMock) -> None:
        self.name = value._mock_name
        # Python 3.13 cannot restore the runtime-generated ``_Call`` class.
        self.calls = [
            (record.args, record.kwargs) for record in value.call_args_list
        ]
        self.side_effect = value._mock_side_effect
        self.return_value = value._mock_return_value
        if isinstance(self.return_value, MagicMock):
            if self.return_value._mock_new_parent is value:
                # This child has a runtime-generated class and no user state.
                self.return_value = DEFAULT


def _restore_magic_mock(state: _MagicMockSnapshot) -> MagicMock:
    restored = MagicMock(name=state.name, side_effect=state.side_effect)
    calls = [call(*args, **keywords) for args, keywords in state.calls]
    restored._mock_called = bool(calls)
    restored._mock_call_args = calls[-1] if calls else None
    restored._mock_call_count = len(calls)
    restored._mock_call_args_list = calls.copy()
    restored._mock_mock_calls = calls.copy()
    restored.method_calls = []
    if state.return_value is not DEFAULT:
        restored.return_value = state.return_value
    return restored


def _construct_bus(python_type: type[Bus]) -> Bus:
    return object.__new__(python_type)


def _apply_bus_state(value: Bus, state: dict[str, Any]) -> None:
    restored_mocks: dict[_MagicMockSnapshot, MagicMock] = {}

    def restore_listener(listener: object) -> object:
        if not isinstance(listener, _MagicMockSnapshot):
            return listener
        if listener not in restored_mocks:
            restored_mocks[listener] = _restore_magic_mock(listener)
        return restored_mocks[listener]

    state['listeners'] = {
        channel: {restore_listener(listener) for listener in listeners}
        for channel, listeners in state['listeners'].items()
    }
    state['_priorities'] = {
        (channel, restore_listener(listener)): priority
        for (channel, listener), priority in state['_priorities'].items()
    }
    vars(value).update(state)


def _reduce_bus(value: Bus):
    """Keep Bus configuration and its listener/plug-in reference graph."""
    snapshots: dict[int, _MagicMockSnapshot] = {}

    def snapshot_listener(listener: object) -> object:
        if not isinstance(listener, MagicMock):
            return listener
        identity = id(listener)
        if identity not in snapshots:
            snapshots[identity] = _MagicMockSnapshot(listener)
        return snapshots[identity]

    state = vars(value).copy()
    state['listeners'] = {
        channel: {snapshot_listener(listener) for listener in listeners}
        for channel, listeners in value.listeners.items()
    }
    state['_priorities'] = {
        (channel, snapshot_listener(listener)): priority
        for (channel, listener), priority in value._priorities.items()
    }
    return _construct_bus, (type(value),), state, None, None, _apply_bus_state


def _construct_monitor(python_type: type[Monitor]) -> Monitor:
    return object.__new__(python_type)


def _reduce_monitor(value: Monitor):
    """Keep monitor configuration without its live background task.

    ``thread`` is excluded because it is the task executing the callback in
    the original process.  Restoring it as ``None`` lets ``start()`` create a
    new task while keeping the callback, frequency, name, and parent Bus.
    """
    durable_state = {
        name: child for name, child in vars(value).items() if name != 'thread'
    }
    durable_state['thread'] = None
    return _construct_monitor, (type(value),), durable_state


def _construct_file_session(
    python_type: type[FileSession],
) -> FileSession:
    return object.__new__(python_type)


def _reduce_file_session(value: FileSession):
    """Keep file-session data without a process-local file lock.

    ``lock`` owns filelock's thread-local context and possibly an open file
    descriptor.  ``locked`` describes ownership by the original process, so
    it is restored as false.  The session id and storage path remain in the
    durable state; the next ``acquire_lock()`` creates the correct new lock.
    """
    durable_state = {
        name: child for name, child in vars(value).items() if name != 'lock'
    }
    durable_state['locked'] = False
    return _construct_file_session, (type(value),), durable_state


def _restore_test_case(python_type: type[unittest.TestCase]):
    return object.__new__(python_type)


def _reduce_test_case(value: unittest.TestCase):
    """Keep test data and remove the active unittest execution context."""
    durable_state = {
        name: child
        for name, child in vars(value).items()
        if name not in _TEST_CASE_RUNTIME_ATTRIBUTES
    }
    return _restore_test_case, (type(value),), durable_state


def _restore_server(python_type: type[Server]):
    restored = object.__new__(python_type)
    # The original HTTP server owns live listener sockets and worker threads.
    restored.httpserver = None
    # A server without those resources cannot remain in the running state.
    restored.running = False
    return restored


def _reduce_server(value: Server):
    """Keep server configuration and remove the active socket server."""
    durable_state = {
        name: child
        for name, child in vars(value).items()
        if name not in {'httpserver', 'running'}
    }
    return _restore_server, (type(value),), durable_state


def _test_case_types() -> set[type[unittest.TestCase]]:
    return {
        python_type
        for python_type in _subclasses(unittest.TestCase)
        if python_type.__module__.startswith('cherrypy.test.')
    }


def _subclasses(python_type: type) -> set[type]:
    pending = list(python_type.__subclasses__())
    selected = set()
    while pending:
        child_type = pending.pop()
        pending.extend(child_type.__subclasses__())
        selected.add(child_type)
    return selected


class _RestoredCaptureBuffer(io.BytesIO):
    """Provide the file mode that ``EncodedFile.mode`` requires."""

    mode = 'w+b'


def _restore_encoded_file(
    content: bytes,
    position: int,
    encoding: str,
    errors: str | None,
    line_buffering: int,
    write_through: int,
) -> EncodedFile:
    buffer = _RestoredCaptureBuffer(content)
    restored = EncodedFile(
        buffer,
        encoding=encoding,
        errors=errors,
        line_buffering=bool(line_buffering),
        write_through=bool(write_through),
    )
    restored.seek(position)
    return restored


def _reduce_encoded_file(value: EncodedFile):
    """Keep captured bytes and remove the live pytest file descriptor."""
    value.flush()
    position = value.tell()
    buffer_position = value.buffer.tell()
    value.buffer.seek(0)
    try:
        content = value.buffer.read()
    finally:
        value.buffer.seek(buffer_position)
    arguments = (
        content,
        position,
        value.encoding,
        value.errors,
        int(value.line_buffering),
        int(value.write_through),
    )
    return _restore_encoded_file, arguments


def _reduce_count(
    value: itertools.count,
) -> tuple[Callable[..., Any], tuple[Any, ...]]:
    """Avoid the Python 3.13 deprecation warning from ``count.__reduce__``."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        _, arguments = value.__reduce__()
    return itertools.count, arguments


def _restore_thread(python_type: type[threading.Thread], started: int):
    restored = object.__new__(python_type)
    threading.Thread.__init__(restored)
    if started:
        restored._handle = threading._make_thread_handle(0)
        restored._started.set()
        restored._handle._set_done()
    return restored


def _reduce_thread(value: threading.Thread):
    """Restore a new thread or a safe completed view of a started thread."""
    excluded_attributes = _THREAD_RUNTIME_ATTRIBUTES
    if value._started.is_set():
        excluded_attributes |= _STARTED_THREAD_CALL_ATTRIBUTES
    durable_state = {
        name: child
        for name, child in vars(value).items()
        if name not in excluded_attributes
    }
    return (
        _restore_thread,
        (type(value), int(value._started.is_set())),
        durable_state,
    )


def _restore_connection(
    python_type: type[HTTPConnection],
    host: str,
    port: int | None,
    timeout: object,
    source_address: tuple[str, int] | None,
    blocksize: int,
):
    restored = object.__new__(python_type)
    HTTPConnection.__init__(
        restored,
        host,
        port=port,
        timeout=timeout,
        source_address=source_address,
        blocksize=blocksize,
    )
    return restored


def _reduce_connection(value: HTTPConnection):
    """Keep connection configuration and remove the active transaction."""
    durable_state = {
        name: child
        for name, child in vars(value).items()
        if name not in _CONNECTION_RUNTIME_ATTRIBUTES
    }
    arguments = (
        type(value),
        value.host,
        value.port,
        value.timeout,
        value.source_address,
        value.blocksize,
    )
    return _restore_connection, arguments, durable_state


def _restore_response(python_type: type[HTTPResponse]):
    restored = object.__new__(python_type)
    io.BufferedIOBase.__init__(restored)
    restored.fp = None
    io.BufferedIOBase.close(restored)
    return restored


def _reduce_response(value: HTTPResponse):
    """Keep parsed response metadata and remove the live input stream."""
    # ``fp`` owns the socket stream and can be non-seekable.
    durable_state = {
        name: child for name, child in vars(value).items() if name != 'fp'
    }
    return _restore_response, (type(value),), durable_state


def _restore_datetime(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int,
    microsecond: int,
    tzinfo: _datetime_module.tzinfo | None,
    fold: int,
) -> _datetime_module.datetime:
    return _DATETIME_TYPE(
        year,
        month,
        day,
        hour,
        minute,
        second,
        microsecond,
        tzinfo=tzinfo,
        fold=fold,
    )


def _reduce_datetime(value: _datetime_module.datetime):
    """Restore a datetime without resolving the mutable module attribute."""
    arguments = (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
        value.tzinfo,
        value.fold,
    )
    if type(value) is _DATETIME_TYPE:
        return _restore_datetime, arguments
    return _restore_datetime_subclass, (type(value), arguments), vars(value)


def _restore_datetime_subclass(
    python_type: type[_datetime_module.datetime],
    arguments: tuple[Any, ...],
) -> _datetime_module.datetime:
    return _DATETIME_TYPE.__new__(
        python_type,
        *arguments[:-1],
        fold=arguments[-1],
    )


def _construct_log_record() -> logging.LogRecord:
    return logging.makeLogRecord({})


def _apply_object_state(value: object, state: dict[str, Any]) -> None:
    vars(value).update(state)


def _reduce_log_record(value: logging.LogRecord):
    """Keep exception data and formatted text without a raw traceback."""
    state = vars(value).copy()
    if value.exc_info:
        exception_type, exception_value, raw_traceback = value.exc_info
        if raw_traceback is not None:
            state['exc_text'] = ''.join(
                traceback.format_exception(
                    exception_type,
                    exception_value,
                    raw_traceback,
                ),
            )
        state['exc_info'] = (exception_type, exception_value, None)
    return (
        _construct_log_record,
        (),
        state,
        None,
        None,
        _apply_object_state,
    )


def _construct_log_capture_fixture() -> LogCaptureFixture:
    return object.__new__(LogCaptureFixture)


def _apply_log_capture_state(
    value: LogCaptureFixture,
    state: dict[str, Any],
) -> None:
    handler = LogCaptureHandler()
    handler.records = state.pop('handler_records')
    handler.stream = io.StringIO(state.pop('handler_text'))
    handler.setLevel(state.pop('handler_level'))
    handler.setFormatter(state.pop('handler_formatter'))
    handler.filters = state.pop('handler_filters')

    stash = Stash()
    stash[caplog_handler_key] = handler
    stash[caplog_records_key] = state.pop('phase_records')

    vars(value).update(state)
    value._item = SimpleNamespace(stash=stash)


def _reduce_log_capture_fixture(value: LogCaptureFixture):
    """Keep the public caplog data without the live pytest node graph."""
    handler = value.handler
    # ``_item`` owns the active pytest node and its plugin runtime graph.
    state = {
        name: child for name, child in vars(value).items() if name != '_item'
    }
    state.update(
        {
            'handler_records': handler.records,
            'handler_text': handler.stream.getvalue(),
            'handler_level': handler.level,
            'handler_formatter': handler.formatter,
            'handler_filters': handler.filters,
            'phase_records': value._item.stash.get(caplog_records_key, {}),
        },
    )
    return (
        _construct_log_capture_fixture,
        (),
        state,
        None,
        None,
        _apply_log_capture_state,
    )


def _datetime_reference(value: object) -> object:
    if isinstance(value, ModuleType):
        return _ModuleReference(value.__name__)
    if value is _DATETIME_TYPE:
        return _DATETIME_TYPE_REFERENCE
    return value


def _restore_datetime_reference(value: object) -> object:
    if isinstance(value, _ModuleReference):
        return importlib.import_module(value.module_name)
    if isinstance(value, _DateTimeTypeReference):
        return _DATETIME_TYPE
    return value


def _construct_monkeypatch() -> MonkeyPatch:
    return object.__new__(MonkeyPatch)


def _apply_monkeypatch_state(
    value: MonkeyPatch,
    state: dict[str, Any],
) -> None:
    state['_setattr'] = [
        tuple(_restore_datetime_reference(child) for child in operation)
        for operation in state['_setattr']
    ]
    vars(value).update(state)


def _reduce_monkeypatch(value: MonkeyPatch):
    """Keep undo operations without resolving a patched datetime name."""
    state = vars(value).copy()
    state['_setattr'] = [
        tuple(_datetime_reference(child) for child in operation)
        for operation in value._setattr
    ]
    return (
        _construct_monkeypatch,
        (),
        state,
        None,
        None,
        _apply_monkeypatch_state,
    )


def get_dispatch_table() -> dict[type, Callable[[Any], Any]]:
    """Return exact Python types mapped to copyreg-style Dill reducers."""
    dispatch_table: dict[type, Callable[[Any], Any]] = {
        EncodedFile: _reduce_encoded_file,
        itertools.count: _reduce_count,
        threading.Thread: _reduce_thread,
        HTTPConnection: _reduce_connection,
        HTTPResponse: _reduce_response,
        _DATETIME_TYPE: _reduce_datetime,
        logging.LogRecord: _reduce_log_record,
        LogCaptureFixture: _reduce_log_capture_fixture,
        MonkeyPatch: _reduce_monkeypatch,
        Server: _reduce_server,
        Bus: _reduce_bus,
        Monitor: _reduce_monitor,
        FileSession: _reduce_file_session,
    }
    dispatch_table.update(
        dict.fromkeys(_test_case_types(), _reduce_test_case),
    )
    dispatch_table.update(
        dict.fromkeys(_subclasses(threading.Thread), _reduce_thread),
    )
    dispatch_table.update(
        dict.fromkeys(_subclasses(HTTPConnection), _reduce_connection),
    )
    dispatch_table.update(
        dict.fromkeys(_subclasses(HTTPResponse), _reduce_response),
    )
    dispatch_table.update(
        dict.fromkeys(_subclasses(_DATETIME_TYPE), _reduce_datetime),
    )
    dispatch_table.update(
        dict.fromkeys(_subclasses(Bus), _reduce_bus),
    )
    dispatch_table.update(
        dict.fromkeys(_subclasses(Monitor), _reduce_monitor),
    )
    dispatch_table.update(
        dict.fromkeys(_subclasses(FileSession), _reduce_file_session),
    )
    return dispatch_table


__all__ = ['get_dispatch_table']
