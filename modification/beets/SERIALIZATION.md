# Beets serialization audit

The policy is to preserve data and exclude objects whose behavior cannot be restored.
Do not count an empty replacement object as a successful restoration.

## Confirmed registration defect

`DillSerializer` evaluates custom providers once. Its dispatch table uses exact types.
The benchmark initialized it in `pytest_configure`, before pytest imported the test classes.
Thus, `_beets_test_types()` missed classes that the benchmark later captured.

The benchmark now initializes SpaceTimePy in `pytest_collection_finish`.
This change preserves the existing reducer interface.
Classes created during a test still require separate analysis. Collection cannot discover them in advance.

This defect is specific to Beets' registration strategy, not a general requirement of Dill.
CherryPy already initializes after collection. Discord.py adds subclass lookup to Dill.
PyMISP and DSPy install inherited reduction methods for selected base classes.
Those mechanisms can handle later subclasses without rebuilding the exact-type table.
See the [comparison of all five repositories](../SERIALIZATION.md) for the differences and limits.

## Initial findings and decisions

| Object or group | Evidence | Recommended treatment |
| --- | --- | --- |
| `test.*` test instances | An import-order probe confirms missing reducer entries. Late registration removes several failures. | Keep useful test data. Do not exclude every test class. Check each remaining nested failure. |
| `tuple`, `dict`, `list` | Stored errors identify the outer type. Tuple failures mainly concern wrapper arguments. Dictionary failures concern `dispatch_table`. | Keep ordinary containers. Diagnose their contents. Do not add a blanket reducer or exclusion. |
| `unittest.mock.MagicMock`, `MagicProxy` | Reproduced inside all four plugin types in the supplied list. Dill reports class identity errors. | Candidate for a dedicated mock reducer. Require preservation of specifications, children, calls, return values, side effects, and cycles. Until then, exclude the affected mock-bearing value at its capture site. |
| `unittest.mock._Call` | Late-registration runs raise `RecursionError` in `_Call.__getattr__` during restoration. This affects FtInTitle, logging, plugin registration, and several Tidal tests. | Candidate for a reducer that preserves call data. A successful dump is insufficient. Check equality, chained calls, and parent references after restoration. |
| `DiscogsPlugin`, `MusicBrainzPlugin`, `ListenBrainzPlugin`, `AcoustidPlugin` | Targeted tests reproduce nested `MagicMock` failures. Clean MusicBrainz and Acoustid instances pass a round trip. | Keep plugin types. Fix or exclude the specific mocked state. A plugin-wide exclusion would remove valid data. |
| `MonkeyPatch` | Chroma tests reproduce an undo record that references a plugin containing `MagicMock`. | Exclude the live patch controller from replay. Its undo operations refer to the original process. Preserve relevant application values separately. |
| `TestWebXSS` | The instance contains `FlaskClient`, then a Flask application, `LocalProxy`, and `ContextVar`. | Exclude the live client/context graph. Preserve request inputs and response data. Do not replace all `ContextVar` objects with their current values. |
| `AutotagStub` / active `unittest.mock._patch` | A prompt-choice test reaches a module graph and `ContextVar` through active patchers. | Exclude active patch controllers. Their process-wide effects cannot be reproduced by restoring ordinary instance fields. |
| `threading.Thread` | The current reducer restores a completed thread with a different identity. Live execution state is absent. | Exclude live thread objects. Capture arguments, results, and explicit status data when needed. |
| `sqlite3.Row` | The existing reducer records column names and values. | Retain as a serializer candidate. Test duplicate names, NULL, text, numbers, and BLOB values. |
| `Database`, `Library`, `sqlite3.Connection` | The reducers copy SQLite contents into new connections. | Suitable only for a defined database-data snapshot. They do not preserve transactions, external connections, callbacks, or all connection settings. Do not claim general replay fidelity. |
| `sqlite3.Cursor` | A direct check returns `(1,)` from the original cursor and `None` from the restored cursor. | Exclude live cursors. Capture fetched rows instead. The current reducer loses query position and remaining results. |
| Generator, raw traceback | The existing reducers replace execution frames with an empty generator or a metadata object. | Exclude live frames. Store metadata separately if requested. These reducers do not preserve behavior or, for traceback, type. |
| `Popen`, socket, `GioURI` | Code review shows replacement with a completed process, closed socket, or unavailable native library. | Exclude live handles. The current reducers change behavior. |
| Capture and logging fixtures | The reducers remove the pytest request or replace the pytest node with a partial object. | Treat these as inspection-only snapshots. Under the selected policy, exclude live fixture controllers and retain output or log records separately. |
| HTTP adapter | The reducer creates new pools and locks. | Candidate for a configuration snapshot only. Active connections and rate-limit history need separate treatment. |

These are decisions about specific object graphs. A failed outer type does not prove that every instance needs exclusion.
The existing test reducer also drops runtime fields. This is a partial test-data snapshot, not a complete test execution checkpoint.

## Scope and limits

The saved database contains 6,834 line-capture errors across 109 outer types.
The adjacent CSV lists every type, its count, and one location.
These counts exclude entry and return errors. They do not measure restoration failures.
The database stores the wrapper error but omits its chained cause. Historical nested causes cannot be recovered from this database alone.

Diagnosis used Python 3.13.11, Dill 0.4.1, and pytest 9.1.1 in a temporary environment.
The original benchmark can use different dependency versions.

Targeted diagnostic runs completed as follows:

- FtInTitle, plugin infrastructure, and logging: 180 passed, 79 skipped, with both early and late registration.
- Tidal, Discogs, web, and library: 324 passed, 2 skipped, with late registration.
- Chroma candidate and ListenBrainz tests: 25 passed.
- An isolated benchmark test confirmed registration after collection and restoration of its test instance.
- The final diagnostic plugin passed 125 FtInTitle tests. It recorded 96 restoration failures and 11,134 successful round trips.

Test success does not mean serialization success. The probe records serialization errors without failing the application test.
The diagnostic tracer also inspects Beets test helpers. Its observations are broader than the benchmark's decorated functions.
At the initial audit stage, no full benchmark rerun was performed. Later validation results appear below.

## Reproduce nested failures

Use the original upstream Beets `test/conftest.py` for the diagnostic run.
The environment must contain Beets test dependencies, SpaceTimePy, and Dill.
From the `beets` directory, run:

```sh
PYTHONPATH=../modification/beets python -m pytest \
  -p diagnose_serialization \
  test/plugins/test_chroma.py::TestChromaCandidates \
  --beets-serialization-report=/tmp/beets-serialization.json
```

Add `--beets-serialization-early` to evaluate the reducer provider before collection.
Use `test/plugins/test_ftintitle.py` to inspect restoration failures.

The JSON report separates dump failures from load failures.
It records chained exceptions, nested types from pickler frames, and the first test location.
It does not prove semantic equivalence after restoration.
Do not load this plugin during performance measurement. It serializes locals at each traced test line.
Reducers and restored objects can have side effects. Use an isolated test environment.

## Applying exclusions

SpaceTimePy currently provides `ignored_names` on each capture declaration.
It does not provide a general nested-type exclusion policy through `get_dispatch_table()`.
Use a function-specific exclusion for a confirmed irrelevant local, such as a live patch controller.
Do not globally exclude names such as `self`, `args`, or `plugin`.
If an unsupported resource is nested inside a required value, first define which data must survive.

The initial change fixed registration and added diagnosis. The repair batches below add mock reducers and explicit runtime exclusions.
The existing lossy reducers remain in `spacetimepy_picklers.py` for review. They do not satisfy the selected policy.
Their removal must accompany explicit capture-site exclusions or a supported data representation, not an empty replacement.

## First repair batch

The provider now reduces `unittest.mock._Call` explicitly.
It preserves tuple data and instance fields, including chained-call parent references.
State is applied after memoization, without triggering mock attribute lookup during restoration.
Two regression tests check chained calls and shared parents.

The benchmark also directs SpaceTimePy diagnostics to `serialization.log`.
These warnings no longer enter the application logs that Beets tests compare.
The run script retains this file as `results/raw/beets_serialization.log`.
Capture errors remain present in the database.

Validation used the temporary Python 3.13.11 environment from the investigation:

| Check | Result |
| --- | --- |
| Reducer regression tests | 2 passed |
| FtInTitle diagnostic run | 125 passed, 11,230 successful round trips, no dump or load failures |
| Previous failing test groups | 14 passed, no capture errors, including all 12 previously failing tests |
| Full baseline in the temporary environment | 2,632 passed, 12 failed, 143 skipped |
| Full instrumented run in the same environment | 2,632 passed, 12 failed, 143 skipped |
| Difference in failing test names | None |
| Remaining instrumented capture errors | 907 occurrences across 18 outer types |

The 12 failures in this comparison concern color-output expectations and also occur without SpaceTimePy.
The temporary environment has fewer optional dependencies than the supplied benchmark run.
Its error count must not be used to calculate a direct improvement percentage against that run.
Logs and remaining-error counts are in `.reports/serialization-fixes` at the repository root.
The supplied result files were not overwritten.

Run the focused reducer checks with:

```sh
python -m pytest --noconftest modification/beets/test_picklers.py -q
```

The remaining CherryPy and PyMISP failures also reproduce through nested mocks.
A prototype inherited mock reducer passes checks for cycles, call history, specifications, magic methods, and remaining iterable side effects.
The user approved that process-wide mechanism. The second repair batch installs it.


## Second repair batch: nested mocks

The provider now installs an inherited reducer on `NonCallableMock`.
Concrete mock classes created later can use it without a new dispatch-table entry.
Restoration keeps mock fields and repairs special methods on the new generated class.
Unsupported generated base classes raise an error instead of silently changing type.

The same implementation is present in the standalone CherryPy and PyMISP providers.
No shared deployment module is required.
The mock regressions cover late construction, cycles, calls, specifications, magic methods, iterable side effects, and chained-call parents.

The focused Beets plugin run passed 22 tests and 1,230 round trips without errors.
Five file-import tests were excluded from this round-trip diagnostic run.
An earlier broad diagnostic stalled while inspecting live ZIP-writing locals, so that run was terminated.
These file-import tests remain included in the ordinary full capture run.

The full instrumented Beets run completed with 2,632 passed, 12 failed, and 143 skipped.
The failing test names match the baseline from the first repair batch.
Capture errors decreased from 907 to 738 in the same environment, across 13 remaining outer types.
No plugin-instance errors remain in this run.

Remaining errors include active patch controllers, Flask contexts, NumPy's internal metaclass in `copyreg.dispatch_table`, threads, and database fixtures.
The importer probe confirms `AutotagStub -> _patch -> module -> ContextVar` as one remaining path.
The NumPy class error was reproduced separately by serializing the entries of `copyreg.dispatch_table`.
These values were not replaced with incomplete mock snapshots to suppress errors.


## Third repair batch: remaining Beets capture errors

The user approved partial snapshots that omit runtime fields and record each omission.
The full validation run now records **zero capture errors**, compared with 738 after the second batch in the same environment.
This result applies to benchmark capture. It does not prove complete replay support.

| Change | Data retained | Runtime state excluded or reconstructed |
| --- | --- | --- |
| Test objects | Ordinary test fields and mocked methods | Pytest request, unittest execution fields, direct patch controllers, and the Flask client |
| `AutotagStub` | Matching configuration and candidate generation | Active patchers |
| `MonkeyPatch` | Other fields, including path data | Attribute and dictionary undo stacks |
| Closed database fixtures | File database contents and closed state | Connections and locks reconstructed for the snapshot |
| Test-class discovery | Test objects from both `test.*` and `test_*` modules | None added by this discovery fix |
| Three deepcopy capture sites | Ordinary local and global data | `copyreg.dispatch_table`, which contains the process serialization registry |
| Concurrent logging test | Test data | Live thread locals `t1` and `t2` |

Each omitted object field appears in `_spacetimepy_excluded_attributes` on the restored object.
Each entry records the field type and the reason for omission.
The reducer copies the source state before removing fields. It does not remove fields from the running test.
A restored patch-controller snapshot cannot undo changes in the source process.
The former reducer that created a replacement thread was removed.

Capture-site exclusions appear in record attributes under `capture_exclusion_policy`.
This metadata describes the policy for that function. It does not count actual omitted values at each line.
The final trace has 277 records with the registry policy and 39 records with each thread policy.
No general exclusion applies to `self`, containers, or plugin instances.

Closed file databases are read through a read-only SQLite connection.
The restored database retains a closed connection. It does not silently become an open database.
Closed in-memory databases also retain the closed state. Their discarded contents cannot be recovered.
These snapshots do not preserve transactions or live connection behavior.

### Validation

The final run used Python 3.13.11, Dill 0.4.1, and pytest 9.1.1.
The environment has fewer optional dependencies than the user's full benchmark environment.

| Check | Result |
| --- | --- |
| Full Beets test suite with capture | 2,632 passed, 12 failed, 143 skipped |
| Difference from baseline failing test names | None |
| Capture errors across function and stack records | 0 |
| Function records / stack records | 2,679 / 18,820 |
| Beets reducer and applicable mock regressions | 16 passed |
| CherryPy and PyMISP mock regressions in their dependency environment | 18 passed, 2 skipped |

The 12 test failures concern color-output expectations and also occur without capture.
A combined regression attempt in the Beets-only environment lacked CherryPy and PyMISP dependencies.
The separate runs above use the appropriate dependency environments.

The focused diagnostic probe passed seven application tests and completed 3,179 round trips.
It also recorded 40 dump failures outside the successful benchmark capture result:

- 37 observations of a Flask response that references its live JSON provider and application context.
- Three observations of a locally defined plugin class or its instance.

The diagnostic probe inspects more execution frames and locals than the benchmark capture declarations.
These unresolved observations remain visible in its JSON report.
Legacy resource reducers also retain the replay limits described in the initial audit.

Logs, diagnostic details, and trace counts are in `.reports/serialization-fixes/beets-final`.
The supplied raw benchmark results were not overwritten.
