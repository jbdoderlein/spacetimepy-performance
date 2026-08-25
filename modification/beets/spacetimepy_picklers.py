"""Dill reducers for values that Beets tests expose to SpaceTimePy.

The reducers preserve durable Python state. They do not copy live database
connections, operating-system threads, processes, sockets, native libraries,
pytest nodes, generator frames, Python frames, or raw tracebacks.
"""

from __future__ import annotations

import importlib
import inspect
import io
import logging
import os
import socket
import sqlite3
import subprocess
import threading
import traceback
import types
import unittest
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast

from _pytest._code.code import ExceptionInfo, Traceback
from _pytest.capture import CaptureFixture, EncodedFile
from _pytest.logging import (
    LogCaptureFixture,
    LogCaptureHandler,
    caplog_handler_key,
    caplog_records_key,
)
from _pytest.monkeypatch import MonkeyPatch
from _pytest.stash import Stash
from requests.adapters import HTTPAdapter

from beets.dbcore.db import Database
from beets.test.helper import TestHelper

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


_DATABASE_RUNTIME_ATTRIBUTES = frozenset(
    {
        # Connections belong to threads in the original process.
        "_connections",
        # Transaction stacks contain active thread context.
        "_tx_stacks",
        # These locks protect resources in the original process.
        "_shared_map_lock",
        "_db_lock",
    }
)
_TEST_RUNTIME_ATTRIBUTES = frozenset(
    {
        # The request owns the active pytest node and plugin graph.
        "request",
        # The patcher owns active process-wide environment changes.
        "env_patcher",
        # These fields belong to the active unittest execution.
        "_outcome",
        "_cleanups",
        "_subtest",
    }
)
_THREAD_RUNTIME_ATTRIBUTES = frozenset(
    {
        # The handle refers to one operating-system thread.
        "_handle",
        # The event contains a lock from the original execution.
        "_started",
        # These values do not identify a thread in the restore process.
        "_ident",
        "_native_id",
        # The stream can be a live pytest capture stream.
        "_stderr",
        # The hook refers to the original thread and error stream.
        "_invoke_excepthook",
    }
)
_STARTED_THREAD_CALL_ATTRIBUTES = frozenset(
    {
        # A started thread cannot execute its target again after restoration.
        "_target",
        # The arguments can contain locks from the original execution.
        "_args",
        "_kwargs",
    }
)


def _subclasses(python_type: type) -> set[type]:
    pending = list(python_type.__subclasses__())
    selected: set[type] = set()
    while pending:
        child_type = pending.pop()
        pending.extend(child_type.__subclasses__())
        selected.add(child_type)
    return selected


def _beets_test_types() -> set[type]:
    selected = {
        python_type
        for python_type in _subclasses(TestHelper)
        if python_type.__module__.startswith("test.")
    }
    selected.update(
        python_type
        for python_type in _subclasses(unittest.TestCase)
        if python_type.__module__.startswith("test.")
    )
    selected.add(TestHelper)
    return selected


def _construct_test_object(python_type: type) -> object:
    return object.__new__(python_type)


def _reduce_test_object(value: object):
    """Keep test data and remove the active pytest execution context."""
    state = {
        name: child
        for name, child in vars(value).items()
        if name not in _TEST_RUNTIME_ATTRIBUTES
    }
    return _construct_test_object, (type(value),), state


def _database_snapshot(value: Database) -> bytes | None:
    connections = tuple(value._connections.values())
    if connections:
        return connections[0].serialize()

    if value.path != Path(":memory:") and value.path.exists():
        connection = sqlite3.connect(value.path)
        try:
            return connection.serialize()
        finally:
            connection.close()
    return None


def _construct_database(python_type: type[Database]) -> Database:
    return object.__new__(python_type)


def _apply_database_state(value: Database, state: dict[str, Any]) -> None:
    data = state.pop("_spacetimepy_database")
    vars(value).update(state)
    value._connections = {}
    value._tx_stacks = defaultdict(list)
    value._shared_map_lock = threading.Lock()
    value._db_lock = threading.Lock()

    if data is not None:
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.deserialize(data)
        value.add_functions(connection)
        connection.row_factory = sqlite3.Row
        thread_id = threading.current_thread().ident
        assert thread_id is not None
        value._connections[thread_id] = connection


def _reduce_database(value: Database):
    """Keep database contents and replace thread-local SQLite resources."""
    state = {
        name: child
        for name, child in vars(value).items()
        if name not in _DATABASE_RUNTIME_ATTRIBUTES
    }
    state["_spacetimepy_database"] = _database_snapshot(value)
    return (
        _construct_database,
        (type(value),),
        state,
        None,
        None,
        _apply_database_state,
    )


def _restore_sqlite_connection(
    data: bytes,
    isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None,
    row_factory: Callable[..., Any] | None,
    text_factory: Callable[[bytes], Any],
) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.deserialize(data)
    connection.isolation_level = isolation_level
    connection.row_factory = row_factory
    connection.text_factory = text_factory
    return connection


def _restore_closed_sqlite_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.close()
    return connection


def _reduce_sqlite_connection(value: sqlite3.Connection):
    """Keep database contents and replace the native SQLite handle."""
    try:
        arguments = (
            value.serialize(),
            value.isolation_level,
            value.row_factory,
            value.text_factory,
        )
    except sqlite3.ProgrammingError:
        return _restore_closed_sqlite_connection, ()
    return _restore_sqlite_connection, arguments


def _restore_sqlite_cursor(
    connection: sqlite3.Connection,
    arraysize: int,
    row_factory: Callable[..., Any] | None,
) -> sqlite3.Cursor:
    cursor = connection.cursor()
    cursor.arraysize = arraysize
    cursor.row_factory = row_factory
    return cursor


def _restore_closed_sqlite_cursor() -> sqlite3.Cursor:
    connection = sqlite3.connect(":memory:")
    cursor = connection.cursor()
    connection.close()
    return cursor


def _reduce_sqlite_cursor(value: sqlite3.Cursor):
    """Keep cursor configuration and remove the native query cursor."""
    try:
        connection = value.connection
        connection.total_changes
    except sqlite3.ProgrammingError:
        return _restore_closed_sqlite_cursor, ()
    return _restore_sqlite_cursor, (
        connection,
        value.arraysize,
        value.row_factory,
    )


def _restore_sqlite_row(
    keys: tuple[str, ...], values: tuple[Any, ...]
) -> sqlite3.Row:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    columns = ", ".join(
        f'? AS "{key.replace(chr(34), chr(34) * 2)}"' for key in keys
    )
    row = connection.execute(f"SELECT {columns}", values).fetchone()
    assert row is not None
    return row


def _reduce_sqlite_row(value: sqlite3.Row):
    """Keep the column names and values of one SQLite result row."""
    return _restore_sqlite_row, (tuple(value.keys()), tuple(value))


@dataclass(frozen=True)
class GeneratorState:
    """Durable metadata for a generator frame that cannot be restored."""

    code_name: str
    file_name: str
    first_line: int
    runtime_state: str


def _restore_safe_generator(state: GeneratorState):
    # Keep ``state`` in frame locals for inspection. The generator yields no
    # value because the original instruction and value stack cannot resume.
    if False:
        yield state


def _reduce_generator(value: types.GeneratorType[Any, Any, Any]):
    """Keep code identity and replace the live frame with an inert frame."""
    state = GeneratorState(
        code_name=value.gi_code.co_qualname,
        file_name=value.gi_code.co_filename,
        first_line=value.gi_code.co_firstlineno,
        runtime_state=inspect.getgeneratorstate(value),
    )
    return _restore_safe_generator, (state,)


@dataclass(frozen=True)
class TracebackState:
    """Durable text and locations from a raw traceback."""

    formatted_text: str
    locations: tuple[tuple[str, str, int], ...]


def _reduce_traceback(value: types.TracebackType):
    """Replace raw frames with formatted text and code locations."""
    locations = []
    current: types.TracebackType | None = value
    while current is not None:
        code = current.tb_frame.f_code
        locations.append((code.co_filename, code.co_name, current.tb_lineno))
        current = current.tb_next
    state = TracebackState(
        formatted_text="".join(traceback.format_tb(value)),
        locations=tuple(locations),
    )
    return TracebackState, (state.formatted_text, state.locations)


def _construct_exception_info(
    exception_type: type[BaseException],
    exception_value: BaseException,
    strip_text: str,
    formatted_traceback: str,
) -> ExceptionInfo[BaseException]:
    restored: Any = ExceptionInfo(
        cast(Any, (exception_type, exception_value, None)),
        strip_text,
        Traceback([]),
        _ispytest=True,
    )
    restored._spacetimepy_traceback_text = formatted_traceback
    return restored


def _reduce_exception_info(value: ExceptionInfo[Any]):
    """Keep exception behavior and text without raw traceback frames."""
    if value._excinfo is None:
        return ExceptionInfo.for_later, ()
    exception_type, exception_value, raw_traceback = value._excinfo
    formatted = "".join(
        traceback.format_exception(
            exception_type, exception_value, raw_traceback
        )
    )
    return _construct_exception_info, (
        exception_type,
        exception_value,
        value._striptext,
        formatted,
    )


class _RestoredCaptureBuffer(io.BytesIO):
    """Provide the mode that ``EncodedFile.mode`` requires."""

    mode = "w+b"


def _restore_encoded_file(
    content: bytes, position: int, encoding: str, errors: str | None
) -> EncodedFile:
    buffer = _RestoredCaptureBuffer(content)
    restored = EncodedFile(buffer, encoding=encoding, errors=errors)
    restored.seek(position)
    return restored


def _reduce_encoded_file(value: EncodedFile):
    """Keep buffered data and replace the live pytest file descriptor."""
    value.flush()
    position = value.tell()
    buffer = cast(Any, value.buffer)
    buffer_position = buffer.tell()
    buffer.seek(0)
    try:
        content = buffer.read()
    finally:
        buffer.seek(buffer_position)
    return _restore_encoded_file, (
        content,
        position,
        value.encoding,
        value.errors,
    )


def _construct_capture_fixture() -> CaptureFixture[Any]:
    return object.__new__(CaptureFixture)


def _apply_capture_fixture_state(
    value: CaptureFixture[Any], state: dict[str, Any]
) -> None:
    vars(value).update(state)
    # ``request`` owns the live pytest node and plugin graph.
    setattr(value, "request", None)
    # ``_capture`` owns active file descriptors and capture streams.
    value._capture = None


def _reduce_capture_fixture(value: CaptureFixture[Any]):
    """Keep accumulated output and restore a closed capture fixture."""
    state = {
        "captureclass": value.captureclass,
        "_config": value._config,
        "_captured_out": value._captured_out,
        "_captured_err": value._captured_err,
    }
    return (
        _construct_capture_fixture,
        (),
        state,
        None,
        None,
        _apply_capture_fixture_state,
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
            state["exc_text"] = "".join(
                traceback.format_exception(
                    exception_type, exception_value, raw_traceback
                )
            )
        state["exc_info"] = (exception_type, exception_value, None)
    return (_construct_log_record, (), state, None, None, _apply_object_state)


def _construct_log_capture_fixture() -> LogCaptureFixture:
    return object.__new__(LogCaptureFixture)


def _apply_log_capture_state(
    value: LogCaptureFixture, state: dict[str, Any]
) -> None:
    handler = LogCaptureHandler()
    handler.records = state.pop("handler_records")
    handler.stream = io.StringIO(state.pop("handler_text"))
    handler.setLevel(state.pop("handler_level"))
    handler.setFormatter(state.pop("handler_formatter"))
    handler.filters = state.pop("handler_filters")

    stash = Stash()
    stash[caplog_handler_key] = handler
    stash[caplog_records_key] = state.pop("phase_records")
    vars(value).update(state)
    setattr(value, "_item", SimpleNamespace(stash=stash))


def _reduce_log_capture_fixture(value: LogCaptureFixture):
    """Keep public log data and remove the live pytest node graph."""
    handler = value.handler
    state = {
        name: child for name, child in vars(value).items() if name != "_item"
    }
    state.update(
        {
            "handler_records": handler.records,
            "handler_text": handler.stream.getvalue(),
            "handler_level": handler.level,
            "handler_formatter": handler.formatter,
            "handler_filters": handler.filters,
            "phase_records": value._item.stash.get(caplog_records_key, {}),
        }
    )
    return (
        _construct_log_capture_fixture,
        (),
        state,
        None,
        None,
        _apply_log_capture_state,
    )


@dataclass(frozen=True)
class _ModuleReference:
    """Identify a MonkeyPatch module target without copying its graph."""

    module_name: str


def _safe_reference(value: object) -> object:
    if isinstance(value, ModuleType):
        return _ModuleReference(value.__name__)
    if value is os.environ:
        return _ModuleReference("os.environ")
    return value


def _restore_safe_reference(value: object) -> object:
    if isinstance(value, _ModuleReference):
        if value.module_name == "os.environ":
            return os.environ
        return importlib.import_module(value.module_name)
    return value


def _construct_monkeypatch() -> MonkeyPatch:
    return object.__new__(MonkeyPatch)


def _apply_monkeypatch_state(value: MonkeyPatch, state: dict[str, Any]) -> None:
    state["_setattr"] = [
        tuple(_restore_safe_reference(child) for child in operation)
        for operation in state["_setattr"]
    ]
    state["_setitem"] = [
        tuple(_restore_safe_reference(child) for child in operation)
        for operation in state["_setitem"]
    ]
    vars(value).update(state)


def _reduce_monkeypatch(value: MonkeyPatch):
    """Keep undo operations without copying complete module graphs."""
    state = vars(value).copy()
    state["_setattr"] = [
        tuple(_safe_reference(child) for child in operation)
        for operation in value._setattr
    ]
    state["_setitem"] = [
        tuple(_safe_reference(child) for child in operation)
        for operation in value._setitem
    ]
    return (
        _construct_monkeypatch,
        (),
        state,
        None,
        None,
        _apply_monkeypatch_state,
    )


def _restore_thread(python_type: type[threading.Thread], started: int):
    restored = object.__new__(python_type)
    threading.Thread.__init__(restored)
    if started:
        restored_runtime = cast(Any, restored)
        restored_runtime._handle = cast(Any, threading)._make_thread_handle(0)
        restored_runtime._started.set()
        restored_runtime._handle._set_done()
    return restored


def _reduce_thread(value: threading.Thread):
    """Restore a new thread or a safe completed view of a started thread."""
    started = cast(Any, value)._started.is_set()
    excluded = _THREAD_RUNTIME_ATTRIBUTES
    if started:
        excluded |= _STARTED_THREAD_CALL_ATTRIBUTES
    state = {
        name: child
        for name, child in vars(value).items()
        if name not in excluded
    }
    return _restore_thread, (type(value), int(started)), state


def _construct_process(python_type: type[subprocess.Popen[Any]]):
    return object.__new__(python_type)


def _apply_process_state(
    value: subprocess.Popen[Any], state: dict[str, Any]
) -> None:
    vars(value).update(state)
    setattr(value, "_waitpid_lock", threading.Lock())
    value.stdin = None
    value.stdout = None
    value.stderr = None
    setattr(value, "pid", None)
    setattr(value, "_child_created", False)


def _reduce_process(value: subprocess.Popen[Any]):
    """Keep process configuration and restore an inert completed handle."""
    state = {
        name: child
        for name, child in vars(value).items()
        if name
        not in {
            # This lock coordinates the original child process.
            "_waitpid_lock",
            # These streams own operating-system pipe descriptors.
            "stdin",
            "stdout",
            "stderr",
            # This buffer can contain an active communication operation.
            "_input",
        }
    }
    state["_spacetimepy_original_pid"] = value.pid
    state["_spacetimepy_original_returncode"] = value.returncode
    state["_spacetimepy_was_running"] = value.returncode is None
    if value.returncode is None:
        state["returncode"] = 0
    return (
        _construct_process,
        (type(value),),
        state,
        None,
        None,
        _apply_process_state,
    )


def _restore_socket(
    python_type: type[socket.socket],
    family: int,
    socket_type: int,
    protocol: int,
    timeout: float | None,
) -> socket.socket:
    restored = python_type(family, socket_type, protocol)
    restored.settimeout(timeout)
    restored.close()
    return restored


def _reduce_socket(value: socket.socket):
    """Keep socket configuration and restore a closed socket."""
    return _restore_socket, (
        type(value),
        value.family,
        value.type,
        value.proto,
        value.gettimeout(),
    )


def _restore_http_adapter(
    python_type: type[HTTPAdapter],
    pool_connections: int,
    pool_maxsize: int,
    max_retries: Any,
    pool_block: bool,
    rate_limit: float | None,
) -> HTTPAdapter:
    keywords: dict[str, Any] = {
        "pool_connections": pool_connections,
        "pool_maxsize": pool_maxsize,
        "max_retries": max_retries,
        "pool_block": pool_block,
    }
    if rate_limit is not None:
        keywords["rate_limit"] = rate_limit
    return python_type(**keywords)


def _reduce_http_adapter(value: HTTPAdapter):
    """Keep adapter configuration and replace pools, locks, and sockets."""
    arguments = (
        type(value),
        getattr(value, "_pool_connections"),
        getattr(value, "_pool_maxsize"),
        value.max_retries,
        getattr(value, "_pool_block"),
        getattr(value, "rate_limit", None),
    )
    state = {
        name: child
        for name, child in vars(value).items()
        if name
        not in {
            # Pool managers own connection pools, locks, and sockets.
            "poolmanager",
            "proxy_manager",
            # The Beets rate-limit adapter uses this live lock.
            "_lock",
        }
    }
    return _restore_http_adapter, arguments, state


def _construct_gio_uri(python_type: type) -> object:
    restored: Any = object.__new__(python_type)
    # ``libgio`` is a live ctypes library handle.
    restored.libgio = False
    restored.available = False
    return restored


def _reduce_gio_uri(value: object):
    """Keep the concrete type and remove the live native-library handle."""
    state = {
        name: child for name, child in vars(value).items() if name != "libgio"
    }
    state["available"] = False
    return _construct_gio_uri, (type(value),), state


def get_dispatch_table() -> Mapping[type[Any], Callable[[Any], Any]]:
    """Return exact Python types mapped to copyreg-style Dill reducers."""
    dispatch: dict[type[Any], Callable[[Any], Any]] = {
        CaptureFixture: _reduce_capture_fixture,
        Database: _reduce_database,
        EncodedFile: _reduce_encoded_file,
        ExceptionInfo: _reduce_exception_info,
        HTTPAdapter: _reduce_http_adapter,
        LogCaptureFixture: _reduce_log_capture_fixture,
        MonkeyPatch: _reduce_monkeypatch,
        logging.LogRecord: _reduce_log_record,
        socket.socket: _reduce_socket,
        sqlite3.Connection: _reduce_sqlite_connection,
        sqlite3.Cursor: _reduce_sqlite_cursor,
        sqlite3.Row: _reduce_sqlite_row,
        subprocess.Popen: _reduce_process,
        threading.Thread: _reduce_thread,
        types.GeneratorType: _reduce_generator,
        types.TracebackType: _reduce_traceback,
    }
    dispatch.update(dict.fromkeys(_beets_test_types(), _reduce_test_object))
    dispatch.update(dict.fromkeys(_subclasses(Database), _reduce_database))
    dispatch.update(
        dict.fromkeys(_subclasses(HTTPAdapter), _reduce_http_adapter)
    )
    dispatch.update(dict.fromkeys(_subclasses(socket.socket), _reduce_socket))
    dispatch.update(
        dict.fromkeys(_subclasses(subprocess.Popen), _reduce_process)
    )
    dispatch.update(
        dict.fromkeys(_subclasses(threading.Thread), _reduce_thread)
    )

    try:
        from beetsplug.thumbnails import GioURI
    except ImportError:
        pass
    else:
        dispatch[GioURI] = _reduce_gio_uri
    return dispatch


__all__ = ["get_dispatch_table"]
