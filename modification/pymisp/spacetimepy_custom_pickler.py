"""Dill reducers for PyMISP values that SpaceTimePy captures.

The reducers exclude only the runtime attributes listed below.

* ``FileObject.magic_db`` is a native ``MagicDb`` resource. The restored
  object creates a new database when it next calculates file attributes.
* ``Neo4j.driver`` owns network connections and synchronization objects.
  PyMISP does not retain the connection arguments that could create a new
  driver. The restored object is disconnected.
* Mocked methods and mocked Neo4j drivers retain their behavior and state.
* ``_AssertRaisesContext.test_case._outcome`` refers to the active test
  runner. It can contain generators, frames, and raw traceback objects.
* A bound test method that pytest installs on a ``TestCase`` instance is
  runtime state. The method on the concrete class becomes active after restore.

The hash reducer preserves the algorithm name and the current digest. CPython
does not expose the internal state or input history of ``_hashlib.HASH``.
Thus, a nonempty hash restores as a read-only hash snapshot.
"""

from __future__ import annotations

import copy
import hashlib
import traceback
from dataclasses import dataclass
from typing import Any, Callable
from unittest import mock
from unittest.case import _AssertRaisesContext

import dill  # type: ignore[import-untyped]

from pymisp.api import PyMISP
from pymisp.tools.fileobject import FileObject
from pymisp.tools.neo4j import Neo4j

type Reducer = Callable[[Any], tuple[Any, ...]]
type InstanceState = tuple[dict[str, Any], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class HashSnapshot:
    """Read-only durable state from a nonempty OpenSSL hash."""

    name: str
    _digest: bytes
    digest_size: int
    block_size: int

    def digest(self) -> bytes:
        """Return the captured binary digest."""
        return self._digest

    def hexdigest(self) -> str:
        """Return the captured hexadecimal digest."""
        return self._digest.hex()

    def copy(self) -> HashSnapshot:
        """Return this immutable hash snapshot."""
        return self

    def update(self, data: bytes) -> None:
        """Reject updates because the OpenSSL continuation state is absent."""
        del data
        raise TypeError("A restored hash snapshot cannot accept more input")


class _LazyMagicDatabase:
    """Create a new native magic database at its first use."""

    def __init__(self) -> None:
        self._database: Any | None = None

    def best_magic_buffer(self, data: bytes) -> Any:
        if self._database is None:
            from pymisp.tools import fileobject

            self._database = getattr(fileobject, "MagicDb")()
        return self._database.best_magic_buffer(data)


class _DisconnectedNeo4jDriver:
    """Represent a restored Neo4j object without its live driver."""

    def close(self) -> None:
        """Keep close safe for a restored disconnected object."""

    def session(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise RuntimeError(
            "The restored Neo4j object has no driver. Create a new Neo4j object."
        )


def _slot_names(python_type: type[Any]) -> tuple[str, ...]:
    names: list[str] = []
    for base in python_type.__mro__:
        declared = vars(base).get("__slots__", ())
        if isinstance(declared, str):
            declared = (declared,)
        for name in declared:
            if name not in {"__dict__", "__weakref__"}:
                names.append(name)
    return tuple(names)


def _instance_state(value: Any) -> InstanceState:
    dictionary = vars(value).copy() if hasattr(value, "__dict__") else {}
    slots = {
        name: getattr(value, name)
        for name in _slot_names(type(value))
        if hasattr(value, name)
    }
    return dictionary, slots


def _new_without_init(python_type: type[Any]) -> Any:
    """Create an instance of a pure Python target class without initialization."""
    return object.__new__(python_type)


def _set_instance_state(value: Any, state: InstanceState) -> None:
    dictionary, slots = state
    if hasattr(value, "__dict__"):
        vars(value).update(dictionary)
    for name, item in slots.items():
        setattr(value, name, item)


def _reduce_file_object(value: FileObject) -> tuple[Any, ...]:
    dictionary, slots = _instance_state(value)
    had_magic_database = "magic_db" in dictionary
    dictionary.pop("magic_db", None)
    state = (dictionary, slots, had_magic_database)
    return (
        _new_without_init,
        (type(value),),
        state,
        None,
        None,
        _set_file_object_state,
    )


def _set_file_object_state(
    value: FileObject, state: tuple[dict[str, Any], dict[str, Any], bool]
) -> None:
    dictionary, slots, had_magic_database = state
    _set_instance_state(value, (dictionary, slots))
    if had_magic_database:
        value.magic_db = _LazyMagicDatabase()


def _reduce_neo4j(value: Neo4j) -> tuple[Any, ...]:
    dictionary, slots = _instance_state(value)
    had_driver = "driver" in dictionary and not isinstance(
        dictionary["driver"], mock.NonCallableMock
    )
    if had_driver:
        dictionary.pop("driver")
    state = (dictionary, slots, had_driver)
    return (
        _new_without_init,
        (type(value),),
        state,
        None,
        None,
        _set_neo4j_state,
    )


def _set_neo4j_state(
    value: Neo4j, state: tuple[dict[str, Any], dict[str, Any], bool]
) -> None:
    dictionary, slots, had_driver = state
    _set_instance_state(value, (dictionary, slots))
    if had_driver:
        value.driver = _DisconnectedNeo4jDriver()


def _reduce_pymisp(value: PyMISP) -> tuple[Any, ...]:
    dictionary, slots = _instance_state(value)
    return (
        _new_without_init,
        (type(value),),
        (dictionary, slots),
        None,
        None,
        _set_instance_state,
    )


def _copy_test_case(test_case: Any) -> Any:
    copied = copy.copy(test_case)
    copied_state = vars(copied).copy()
    copied_state["_outcome"] = None

    method_name = copied_state.get("_testMethodName")
    bound_method = copied_state.get(method_name)
    if getattr(bound_method, "__self__", None) is test_case:
        copied_state.pop(method_name)

    vars(copied).clear()
    vars(copied).update(copied_state)
    return copied


def _exception_data(
    exception: BaseException | None,
) -> tuple[type[BaseException], tuple[Any, ...], dict[str, Any], str] | None:
    if exception is None:
        return None
    formatted = "".join(
        traceback.format_exception(type(exception), exception, exception.__traceback__)
    )
    return type(exception), exception.args, vars(exception).copy(), formatted


def _restore_exception(
    data: tuple[type[BaseException], tuple[Any, ...], dict[str, Any], str] | None,
) -> BaseException | None:
    if data is None:
        return None
    exception_type, arguments, state, formatted = data
    exception = BaseException.__new__(exception_type, *arguments)
    exception.args = arguments
    vars(exception).update(state)
    exception.__traceback__ = None
    exception.__cause__ = None
    exception.__context__ = None
    setattr(exception, "_spacetimepy_formatted_traceback", formatted)
    return exception


def _reduce_assert_raises_context(
    value: _AssertRaisesContext[Any],
) -> tuple[Any, ...]:
    dictionary, slots = _instance_state(value)
    dictionary["test_case"] = _copy_test_case(value.test_case)
    dictionary["_spacetimepy_exception_data"] = _exception_data(
        dictionary.pop("exception", None)
    )
    return (
        _new_without_init,
        (type(value),),
        (dictionary, slots),
        None,
        None,
        _set_assert_raises_context_state,
    )


def _set_assert_raises_context_state(
    value: _AssertRaisesContext[Any], state: InstanceState
) -> None:
    dictionary, slots = state
    exception_data = dictionary.pop("_spacetimepy_exception_data")
    dictionary["exception"] = _restore_exception(exception_data)
    _set_instance_state(value, (dictionary, slots))
    setattr(
        value,
        "_spacetimepy_formatted_traceback",
        exception_data[3] if exception_data is not None else "",
    )


def _reduce_hash(value: Any) -> tuple[Any, ...]:
    return (
        _restore_hash,
        (value.name, value.digest(), value.digest_size, value.block_size),
    )


def _save_hash_with_dill(pickler: Any, value: Any) -> None:
    """Apply the hash reduction through Dill's exact-type callback."""
    pickler.save_reduce(*_reduce_hash(value), obj=value)


def _restore_hash(
    name: str, digest: bytes, digest_size: int, block_size: int
) -> Any:
    empty_hash = hashlib.new(name)
    if empty_hash.digest() == digest:
        return empty_hash
    return HashSnapshot(name, digest, digest_size, block_size)


def _subclasses(python_type: type[Any]) -> set[type[Any]]:
    found: set[type[Any]] = set()
    pending = list(python_type.__subclasses__())
    while pending:
        subclass = pending.pop()
        if subclass in found:
            continue
        found.add(subclass)
        pending.extend(subclass.__subclasses__())
    return found


def _install_subclass_reducers() -> None:
    """Install inherited reducers for subclasses created after this call."""
    _AssertRaisesContext.__reduce__ = _reduce_assert_raises_context
    FileObject.__reduce__ = _reduce_file_object
    Neo4j.__reduce__ = _reduce_neo4j
    PyMISP.__reduce__ = _reduce_pymisp


def _apply_mock_state(value: object, state: dict[str, Any]) -> None:
    vars(value).update(state)
    if isinstance(value, mock.MagicMixin):
        value._mock_set_magics()
        # Special methods belong to each generated class, not just its instance.
        for name, child in state.items():
            if name in mock._all_magics:
                setattr(value, name, child)


def _reduce_mock(value: mock.NonCallableMock, protocol: int):
    """Keep mock state without serializing its per-instance generated class."""
    python_type = type(value).__bases__[0]
    if not issubclass(python_type, mock.NonCallableMock):
        raise TypeError("This mock has an unsupported generated base class")
    return python_type, (), vars(value).copy(), None, None, _apply_mock_state


def _construct_mock_call(values: tuple[Any, ...]) -> mock._Call:
    return mock._Call(values, two=len(values) == 2)


def _reduce_mock_call(value: mock._Call):
    # Restore fields after memoization to preserve shared parent references.
    return (
        _construct_mock_call,
        (tuple(value),),
        vars(value).copy(),
        None,
        None,
        _apply_mock_state,
    )


def get_dispatch_table() -> dict[type[Any], Reducer]:
    """Return exact target types and their copyreg-style Dill reducers."""
    # Mocks create concrete subclasses after the dispatch table is built.
    mock.NonCallableMock.__reduce_ex__ = _reduce_mock
    _install_subclass_reducers()
    hash_type = type(hashlib.new("sha256"))
    dill.register(hash_type)(_save_hash_with_dill)
    reducers: dict[type[Any], Reducer] = {
        mock._Call: _reduce_mock_call,
        hash_type: _reduce_hash,
        _AssertRaisesContext: _reduce_assert_raises_context,
        FileObject: _reduce_file_object,
        Neo4j: _reduce_neo4j,
        PyMISP: _reduce_pymisp,
    }
    for base_type, reducer in tuple(reducers.items()):
        for subclass in _subclasses(base_type):
            reducers[subclass] = reducer
    return reducers


__all__ = ["HashSnapshot", "get_dispatch_table"]
