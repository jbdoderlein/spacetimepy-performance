# PyMISP serialization summary

This is a source audit. The current checkout contains no PyMISP profiling database or test log.
The policy is to preserve data and exclude values whose behavior cannot be restored.

## Registration

The benchmark opens SpaceTimePy in `pytest_configure`.
The provider installs `__reduce__` on `_AssertRaisesContext`, `FileObject`, `Neo4j`, and `PyMISP`.
Later subclasses can inherit those methods, even when they have no exact-type table entry.
The provider also registers the base types and their currently loaded subclasses.

Thus, its subclass support does not depend only on early enumeration, unlike the original Beets provider.
A subclass that overrides reduction behavior needs a separate check.

The provider changes those classes process-wide.
It also calls `dill.register` for the SHA-256 object's concrete hash type.
That hash registration changes Dill globally, in addition to the local custom table.
Other concrete hash implementation types are not automatically covered.

## Serializer and exclusion decisions

| Value | Current treatment | Assessment under the policy |
| --- | --- | --- |
| Ordinary object fields | Saves dictionaries and slots, then allocates without calling the constructor. | Serializer candidate. Check aliases, cycles, slots, and class invariants. |
| `FileObject.magic_db` | Replaces the native resource with a lazy new magic database. | Preserve file data separately. Recreated classification behavior depends on the installed native library and database. It is not a snapshot of that resource. |
| `Neo4j.driver` | Preserves mock drivers. Real drivers still restore as disconnected replacements. | The mock-driver regression passes. Exclude live network state under the selected policy. |
| Mocked `PyMISP._prepare_request` | Preserves the instance mock through the inherited mock reducer. | Focused tests verify the configured result. Restoration does not reactivate the real request method. |
| `_AssertRaisesContext` | Copies the test case without active runner state. Saves exception data and formatted traceback text. | Partial assertion-data snapshot. Traceback frames, exception cause, and context do not survive this path. |
| Empty hash of the registered type | Constructs a new empty hash when the digest matches the empty digest. | Candidate for an ordinary empty hash. Verify `update`, `copy`, and digest behavior. |
| Nonempty hash | Returns `HashSnapshot` with its name and digest. `update()` raises `TypeError`. | Exclude the live incremental hash under the selected policy. Save its digest as explicit data if that is all the trace requires. |

A digest cannot reconstruct an incremental hash's internal continuation state.
A read-only digest snapshot must not count as restoration of a mutable hash object.

## Checks to perform next

- Create subclasses after runtime initialization and verify inherited reduction behavior.
- Test empty and nonempty hashes, including their behavior after `update()`.
- Verify that restored mocked API objects do not silently invoke the original request implementation.
- Verify file-data preservation separately from native magic-database availability.

Sources: [provider](spacetimepy_custom_pickler.py), [benchmark configuration](conftest.py), [comparison](../SERIALIZATION.md).

## New benchmark evidence

The supplied profiling database now contains two errors for `Neo4j` in `test_load_events_directory_imports_json_fixture`.
The test temporarily replaces `neo4j.import_event` with a mock.
A standalone reproduction confirms that this instance mock causes Dill's class-identity error.
Deleting the mocked method would change behavior and does not satisfy the preservation policy.
The second repair batch installs an inherited mock reducer and preserves mocked methods.


## Nested-mock repair validation

The provider now preserves mocked `import_event` and `_prepare_request` methods.
It also preserves a mocked Neo4j driver instead of replacing it with a disconnected driver.
Live native/network drivers retain the existing treatment described above.
The reducer does not invoke these mocked methods during serialization or restoration.

All six tests in `tests/test_neo4j.py` passed with capture enabled.
Their database contains zero capture errors, including the two previously failing captures.
Focused regressions also verify restored method results and isolation from the original mock driver.


The broader instrumented PyMISP suite then passed 96 tests, with 25 skipped and zero capture errors.
The temporary environment used `pure-magic-rs==0.4.3` for MIME detection.
Version 0.5.0 changes the required arguments of `best_magic_buffer` and fails the existing PyMISP call independently of these reducers.
No dependency pin was added to the benchmark project.


## PyMISP failure investigated on 2026-09-17

The latest supplied logs both report `TestFileObject.test_mimeType` as failed.
Both runs finish with 118 passed, one failed, and two skipped.
The baseline without SpaceTimePy has the same traceback as the instrumented run.

The cause is an incompatible dependency API:

| Dependency version | `MagicDb.best_magic_buffer` signature | Isolated test without capture |
| --- | --- | --- |
| `pure-magic-rs==0.4.3` | `(self, /, input)` | Passed |
| `pure-magic-rs==0.5.0` | `(self, /, input, extension)` | Failed with the same missing-argument error |

PyMISP calls `best_magic_buffer(self.__data)` in `pymisp/tools/fileobject.py:73`.
Its dependency requirement, `pure-magic-rs>=0.4.3`, permits the incompatible 0.5.0 release.
The benchmark recreates environments and refreshes dependency resolution, so an unbounded requirement can introduce this failure.
The deleted benchmark environments prevent direct verification of their installed versions.
The isolated 0.5.0 reproduction confirms the API mismatch shown in both logs.

This failure is independent of serialization.
For this fixed PyMISP revision, the recommended benchmark correction is to pin `pure-magic-rs==0.4.3` in both measurement environments.
No dependency or benchmark-script change was made during this investigation.
