# CherryPy serialization summary

This is a source audit. The current checkout contains no CherryPy profiling database or test log.
The policy is to preserve data and exclude values whose behavior cannot be restored.

## Registration

CherryPy starts SpaceTimePy in `pytest_collection_modifyitems`, after pytest collects the tests.
The provider enumerates loaded `unittest.TestCase` subclasses from `cherrypy.test.*`.
It also enumerates subclasses of threads, HTTP connections, responses, datetime, buses, monitors, and file sessions.

This is the closest mechanism to Beets, but its initialization already occurs after collection.
It therefore avoids the specific missing-test-class problem found in Beets.
A subclass created later can still miss an exact-type entry. Mock subclasses now inherit a reducer from `NonCallableMock`.

## Serializer and exclusion decisions

| Value | Current treatment | Assessment under the policy |
| --- | --- | --- |
| `itertools.count` | Saves its reduction arguments. | Serializer candidate. Check the next value and step. |
| Datetime values | Stores fields and avoids resolving a temporarily patched module attribute. | Serializer candidate. Check timezone, fold, and subclass state. |
| Test instances | Removes unittest execution fields and keeps the remaining state. | Partial test-data snapshot. Check nested resources and restoration. Do not treat it as a complete test checkpoint. |
| `Bus` | Preserves listener and priority mappings, including nested mock objects. | Candidate for ordinary listener graphs. Check shared identity and cycles. |
| Mock bus listeners | Preserves instance state and repairs generated special methods. | The new inherited reducer replaces the partial snapshots. Focused tests cover specifications, child data, cycles, and call behavior. |
| `Monitor` | Removes its thread and restores `thread=None`. | Configuration snapshot. Exclude active execution state. Restarting a callback is not resuming it. |
| `FileSession` | Removes its lock and sets `locked=False`. Keeps the storage path. | Candidate for session data only. Lock ownership and external file contents are not captured by this reducer. |
| `Server` | Removes the socket server and resets runtime fields. | Exclude live server state. Preserve server configuration separately. |
| `Thread` | Replaces native thread resources. A started thread becomes a completed view. | Exclude live threads. Execution and thread identity do not survive. |
| `HTTPConnection` | Removes the active transaction and socket state. | Configuration snapshot only. Exclude an active connection from replay. |
| `HTTPResponse` | Removes `fp` and restores a closed stream. | Exclude the live response stream. Preserve response metadata and captured body bytes separately. Unread body data is lost. |
| Pytest `EncodedFile` | Flushes and reads bytes, then restores an in-memory stream. | Candidate for explicit byte data. Flushing changes the source stream, so serialization is not observational. |
| Log records and capture fixtures | Preserves selected log data and replaces traceback or pytest runtime state. | Preserve log data separately. Exclude live fixture controllers and frames. |
| `MonkeyPatch` | Keeps undo operations with special references for modules and datetime. | Exclude the live patch controller. Import references do not reproduce historical process-wide patches. |

## Checks to perform next

- Verify entries for collected test classes and an intentionally late-created subclass.
- Compare mock listener specifications, return children, and call histories.
- Check unread HTTP body preservation before accepting response capture.
- Verify datetime fields and bus graph identity after restoration.

Sources: [provider](spacetimepy_custom_pickler.py), [benchmark configuration](conftest.py), [comparison](../SERIALIZATION.md).

## New benchmark evidence

The supplied profiling database now contains four errors, all for `Bus` in `test_wait_publishes_periodically`.
A standalone reproduction confirms a nested `MagicMock` serialization failure.
`Bus.subscribe()` reads `callback.priority`, which creates a child mock on an unrestricted `MagicMock`.
The existing reducer converts listener keys but leaves this mock priority value unchanged.
The original listener snapshot also omits general mock state, so replacing the priority with a number would lose behavior.
The second repair batch installs an inherited mock reducer and removes the partial listener snapshots.


## Nested-mock repair validation

The bus reducer now preserves its instance graph directly.
Listeners and priority values use the same general mock reducer.
A regression verifies that a restored priority is the restored listener's own child mock.
It also verifies child return data and callback behavior without invoking the original listener.

All 12 tests in `cherrypy/test/test_bus.py` passed with capture enabled.
Their database contains zero capture errors, including the four previously failing captures.
The full CherryPy suite was not rerun in this repair batch.


## Baseline file-resource failure investigated on 2026-09-17

The latest supplied logs both fail `CoreRequestHandlingTest.testRedirect`.
Both report 289 passed, one failed, and seven skipped, with different xfail/xpass counts.
This failure occurs with and without SpaceTimePy.

The actual error is an unclosed file warning for `cherrypy/test/static/index.html`.
Pytest converts it to `PytestUnraisableExceptionWarning` and fails the test because the configuration treats warnings as errors.
The logged `NameError('redirect_test')` is intentional test behavior and is not the cause of this failure.

A reproduction without capture gives:

| Scope | Result |
| --- | --- |
| Full `test_core.py`, with allocation tracing | 19 passed, one failed (`testRedirect`) |
| `testRedirect` alone | One passed |

Allocation tracing identifies `Ranges.slice_file()` calling `static.serve_file()`.
The file opens in `cherrypy/lib/static.py:134` during the preceding range test.
Its delayed cleanup reports the warning during the redirect test.
Thus, the reported test name identifies where the warning surfaced, not where the file opened.

The current warning filters cover selected buffered-file warnings, but not this raw `FileIO` warning.
The preferred correction is deterministic file closure in the static-serving code, applied equally to both benchmark variants.
A warning filter would hide this resource defect. No warning filter, dependency pin, or CherryPy source change was made during the investigation.
Local reproduction logs are in `.reports/serialization-fixes/cherrypy-resource-warning/`.
