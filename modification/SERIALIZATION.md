# Serialization policy and benchmark results

**Scope:** Beets, CherryPy, Discord.py, DSPy, and PyMISP benchmarks with SpaceTimePy and Dill.  
**Purpose:** A consolidated basis for presenting the serialization decisions, exclusions, measured results, and remaining limits.

## 1. Main result

The repairs address two main causes of serialization failure: missing reducer registration and unsupported objects nested inside otherwise useful values.
Beets capture errors decreased from 738 to zero in the final comparison within the same temporary environment.
The affected CherryPy and PyMISP tests also record zero capture errors after the mock repair.

The selected policy is to **preserve useful data and exclude runtime state whose behavior cannot be restored**.
The user approved partial test-object snapshots when each omitted runtime field has a recorded reason.
These snapshots support trace inspection. They are not complete execution checkpoints.

There is no general ban on test objects, mocks, plugins, dictionaries, lists, or tuples.
A failure reported for one of these types can originate from a deeply nested object.
The repair must address that nested object instead of discarding the entire outer value.

## 2. General decisions

| Decision | Application | Reason |
| --- | --- | --- |
| Preserve ordinary data | Keep application fields, test inputs, expected values, results, and supported object relationships. | These values explain the recorded execution. |
| Use a custom reducer when restoration is meaningful | Add reducers for supported mocks, mock call records, and database data. | Dill cannot infer a correct representation for every runtime type. |
| Exclude the smallest confirmed runtime component | Remove a patch controller field while retaining the configured mock and test data. | A whole-object exclusion would discard useful information. |
| Record omissions | New Beets partial-object reducers record omitted field types and reasons. Capture-site exclusions record their policy. | Readers must know which parts of the trace are incomplete. |
| Preserve mock behavior | Keep mocked methods, results, side effects, call records, and supported relationships. | Deleting a mocked method can silently reactivate the real implementation. |
| Do not treat replacement objects as complete restoration | Label configuration, result, and diagnostic snapshots according to their limits. | An empty generator or disconnected driver does not preserve the original behavior. |
| Validate capture and restoration separately | Check capture errors, round trips, and selected behavior after restoration. | A successful write does not prove that loading or subsequent use is correct. |
| Keep benchmark environments separate | Run repositories in separate pytest processes. | Provider hooks can change classes and Dill behavior across the process. |

The policy is agreed. Its implementation is not yet complete across every older reducer.
Section 5 identifies existing partial replacements that still require review.

## 3. Why custom registration was necessary

A reducer describes how to save an object and construct its restored representation.
SpaceTimePy calls the provider's `get_dispatch_table()` when it opens the runtime.
That table maps exact types to reducers. An entry for a base class does not automatically cover all subclasses.

Beets originally built this table before pytest imported the test classes.
The table therefore omitted classes that later appeared in the trace.
Beets now initializes SpaceTimePy after collection and recognizes test modules named both `test.*` and `test_*`.

This was a Beets configuration defect, not a general Dill requirement to initialize after collection.
The other providers use different mechanisms:

| Repository | Mechanism | Relevant limit |
| --- | --- | --- |
| Beets | Enumerates loaded test types after collection. Installs an inherited mock reducer. | New non-mock classes created later can still need support. |
| CherryPy | Enumerates types after collection. Installs an inherited mock reducer. | Other later subclasses can miss exact-type entries. |
| Discord.py | Adds subclass lookup to Dill for selected base classes. | Coverage is limited to the selected bases. |
| PyMISP | Installs inherited reduction methods on selected classes and mocks. | A subclass can override the inherited method. |
| DSPy | Registers fixed types and metaclasses. Installs inherited methods on mocks and DSPy modules. | Unrelated classes and custom metaclasses can need separate support. |

### Nested mocks

Mocks can create a new concrete class for each instance.
An exact-type table built earlier cannot enumerate these future classes.
The failure can appear under an outer type such as a Beets plugin, a CherryPy bus, or a PyMISP object.

Beets, CherryPy, and PyMISP now install a reducer inherited from `NonCallableMock`.
The reducer preserves instance state and restores generated special methods.
A separate `_Call` reducer preserves call data and parent relationships after object memoization.
Regression checks cover cycles, shared references, specifications, magic methods, and remaining iterable side effects.

CherryPy now retains complete supported mock listener graphs, including mock priority values.
PyMISP now retains mocked request methods and mock Neo4j drivers.
Unsupported mock structures can still fail. The repair does not claim support for every possible custom mock subclass.

## 4. What is explicitly excluded from the Beets trace?

There are two exclusion levels: a field inside an object, or a named value at a specific capture site.
Neither mechanism removes the original value from the running application.

### 4.1 Fields omitted from partial objects

| Object | Fields omitted | Reason | Data retained |
| --- | --- | --- | --- |
| Supported test instances | `request` | References the active pytest node and plugin graph. | Other test fields. |
| Supported test instances | `env_patcher` | Controls the original process environment. | Other test fields. |
| Supported test instances | `_outcome`, `_cleanups`, `_subtest` | Belong to the current unittest execution. | Test inputs, expected values, results, and other fields. |
| Supported test instances | Direct fields containing `unittest.mock._patch` or `_patch_dict` | Patch controllers mutate objects in the original process. | Separately stored mocks and their supported state. |
| Supported test instances | `client`, when its concrete type comes from `flask.testing` | Owns live application and request contexts. | Other stored test data. The reducer does not extract additional response data. |
| `AutotagStub` | `patchers` | Active patchers control methods in the original process. | Matching configuration and candidate-generation data. |
| `MonkeyPatch` | `_setattr`, `_setitem` | Undo records refer to objects in the original process. | Other fields, including path data. The restored object is not a usable undo controller. |

Each actual field omission is stored on the restored object in `_spacetimepy_excluded_attributes`.
Each entry contains the field's qualified type and its exclusion reason.
The reducer copies the source dictionary before removing fields.

This metadata applies to these new Beets partial-object reducers.
It is not a universal omission ledger for all five providers or all older Beets reducers.

### 4.2 Values omitted at specific capture sites

| Capture site | Named value excluded | Reason |
| --- | --- | --- |
| `test.autotag.test_hooks.test_correct_list_fields` | `dispatch_table` | The value is the process-wide `copyreg` serialization registry, reached through `deepcopy`. |
| `test.ui.test_ui_init.ParentalDirCreation.test_create_no` | `dispatch_table` | Same registry exclusion. |
| `test.ui.test_ui_init.ParentalDirCreation.test_create_yes` | `dispatch_table` | Same registry exclusion. |
| `test.test_logging.TestConcurrentEvents.test_concurrent_events` | `t1`, `t2` | Live operating-system threads cannot be restored from ordinary object fields. |

These exclusions use `ignored_names` only on the specified capture declarations.
The function and line records contain `capture_exclusion_policy` with the scope and reason.
This field records an applicable policy, not proof that the excluded value existed at every recorded line.

The final trace contains 277 records with the registry policy and 39 records with each thread policy.
These numbers are not counts of distinct objects removed from the trace.
The previous Beets reducer that created a replacement thread was removed.

### 4.3 Database resources reconstructed instead of copied

Beets database snapshots retain supported database data and instance state.
They reconstruct connections, locks, and transaction-stack containers instead of copying resources from the original process.
The affected internal fields are `_connections`, `_tx_stacks`, `_shared_map_lock`, and `_db_lock`.
These resource substitutions do not use the field-omission metadata described above.

Closed file databases are read through a read-only SQLite connection.
The restored database retains the closed state. Serialization does not reopen the source object's connection.
Closed in-memory databases also remain closed, but discarded database contents cannot be recovered.
Transactions, external connections, callbacks, and all connection settings are outside the validated snapshot contract.

## 5. Which runtime state is generally unsuitable for replay?

The following categories explain the general exclusion boundary.
**They are not a global type filter currently enforced by SpaceTimePy.**
Several existing providers still serialize these values as partial replacements.

| Category | Examples across the providers | Why full restoration is unavailable | Current implementation status |
| --- | --- | --- | --- |
| Active execution | Threads, generators, async generators, coroutines, tasks, thread pools | Native execution, frames, pending work, and scheduling state are absent. | Beets excludes two thread locals. Older Beets, CherryPy, and DSPy reducers still create some replacement execution objects. |
| Process controllers | Pytest requests and fixtures, unittest execution state, patchers, monkeypatch undo records | These objects act on a particular running test process. | Beets records selected omissions. Other providers contain partial fixture snapshots with different limits. |
| Live network resources | Sockets, HTTP connections, response streams, servers, real Neo4j drivers | Open connections, unread streams, and remote state are not captured by ordinary fields. | Providers still contain closed, disconnected, or configuration-only replacements. |
| Synchronization and context | Locks, waiters, context variables and tokens, view timeout tasks | Ownership, context identity, event loops, and waiting tasks belong to the original execution. | Resources are reconstructed or discarded in several providers. DSPy context replacements do not preserve full binding semantics. |
| Execution diagnostics | Raw traceback frames, pytest exception contexts | Formatted text cannot reconstruct original frames and local state. | Providers retain selected exception data or text. Some exception chains are incomplete. |
| External or native resources | Native magic database, session files, disk caches, temporary-file handles | The trace does not contain all external state or native resource internals. | PyMISP recreates its magic resource lazily. Other providers reopen paths or substitute streams. |
| Stateful consumers | SQLite cursors, CSV readers, incremental hashes | Remaining rows, stream content, or continuation state can be unavailable. | Existing reducers can lose cursor/reader progress or expose only a hash digest. These are not full restorations. |

Useful alternatives include explicit inputs, fetched rows, captured bytes, completed results, configuration, and formatted diagnostic data.
Such data must be described as a snapshot of that information, not as the original live resource.

### Repository-specific limits still present

| Repository | Existing partial behavior requiring care |
| --- | --- |
| Beets | Older cursor, generator, process, socket, fixture, and traceback reducers retain limits identified in the audit. |
| CherryPy | Monitor threads, session locks, server resources, HTTP state, and response streams are removed or reset. Started threads become completed views. |
| Discord.py | Views lose timeout tasks and cancellation state. Pending stopped-futures become `None`. Completed futures become outcome snapshots. |
| PyMISP | Real Neo4j drivers become disconnected replacements. Nonempty hashes become read-only digest snapshots. Assertion snapshots omit original traceback frames. |
| DSPy | Execution objects, contexts, synchronization, readers, and fixtures have partial replacements. Some optimizer and executor reducers still discard method mocks. |

These older paths were not all rewritten during the capture-error repairs.
In particular, zero DSPy capture errors does not establish compliance with the preservation policy.

## 6. Measured results

### 6.1 User-supplied full benchmark

These figures come from the supplied benchmark results, before the final repairs.

| Repository | Tests passed without SpaceTimePy | Tests passed with SpaceTimePy | Serialization problem occurrences |
| --- | ---: | ---: | ---: |
| Beets | 2,733 | 2,721 | 7,651 |
| CherryPy | 289 | 289 | 4 |
| Discord.py | 294 | 294 | 0 |
| DSPy | 1,263 | 1,263 | 0 |
| PyMISP | 118 | 118 | 2 |

A problem occurrence is not a distinct object or an affected-record count.
One record can contain multiple failed values. Repeated observations can concern the same object.

### 6.2 Repair validation

| Repository or check | Validation scope | Result |
| --- | --- | --- |
| Beets | Full suite in the temporary environment | 2,632 passed, 143 skipped, and the same 12 failures as the baseline without capture. Zero capture errors. |
| CherryPy | All 12 bus tests, including the affected test | 12 passed. Zero capture errors. The full suite was not rerun in this repair batch. |
| PyMISP | Broader instrumented suite in the temporary environment | 96 passed, 25 skipped. Zero capture errors. |
| Discord.py | Supplied full benchmark and source audit | Zero supplied capture errors. No new full repair-validation run. |
| DSPy | Supplied full benchmark and source audit | Zero supplied capture errors. No new full repair-validation run. |
| Reducer regressions | Beets checks and applicable mock checks across three providers | 34 passed, 2 skipped in the appropriate dependency environments. |

Beets validation used Python 3.13.11, Dill 0.4.1, and pytest 9.1.1.
Its temporary environment has fewer optional dependencies than the user-supplied benchmark environment.
The 12 baseline failures concern color-output expectations. The final instrumented run has the same failing test names.
PyMISP validation used `pure-magic-rs==0.4.3`. No dependency pin was added to the benchmark project.

The comparable Beets repair sequence in the temporary environment was:

| Stage | Capture error occurrences |
| --- | ---: |
| First repair batch, before the inherited mock repair | 907 |
| After the inherited mock repair | 738 |
| After runtime-field exclusions, database fixes, and test-module discovery fixes | 0 |

Do not calculate a repair percentage by comparing 7,651 from the supplied run with zero from the temporary environment.
The environments and executed test sets differ.
A final run in the original benchmark environment is needed for that comparison.

### 6.3 Latest PyMISP test failure

The supplied September 16 logs each report 118 passed, one failed, and two skipped.
`TestFileObject.test_mimeType` fails identically with and without SpaceTimePy.
The failure comes from a dependency API mismatch, independently of serialization.

An isolated check without capture passes with `pure-magic-rs==0.4.3` and fails with version 0.5.0.
Version 0.5.0 requires an `extension` argument that this PyMISP revision does not supply.
The declared requirement `pure-magic-rs>=0.4.3` permits that incompatible release.
Pinning 0.4.3 in both benchmark environments is the recommended correction for this revision.
This investigation did not change dependencies or the benchmark script.

### 6.4 Latest CherryPy test failure

The supplied logs both fail `CoreRequestHandlingTest.testRedirect`, with and without SpaceTimePy.
An unclosed `static/index.html` file causes a warning that pytest treats as an error.
Allocation tracing identifies the file opened by the preceding range test through `static.serve_file()`.
The warning surfaces later, during the redirect test.

Without capture, the core test file reproduces the failure with 19 passed and one failed.
The redirect test alone passes.
This is a baseline resource-lifecycle defect. No warning suppression or source correction was applied during this investigation.

## 7. What zero capture errors proves

The final Beets database contains 2,679 function records and 18,820 stack records without recorded capture errors.
It demonstrates successful capture under the configured reducers and exclusion policy for that run.
It does not demonstrate complete serialization coverage, full restoration, or resumable execution.

Three measures must remain separate:

1. **Application test outcome:** Did the test pass with capture enabled?
2. **Capture success:** Did the configured capture operation write its selected values without an error?
3. **Restoration fidelity:** Did loading preserve the required data, relationships, and behavior?

The broader Beets diagnostic completed 3,179 round trips while seven application tests passed.
It also recorded 40 dump failures: 37 observations of Flask responses and three observations of a local plugin class or instance.
That diagnostic inspects more execution frames and locals than the benchmark capture declarations.
These failures remain unresolved and are not included in the zero-error benchmark claim.

A previous broad diagnostic stalled while inspecting live ZIP-writing locals and was terminated.
The file-import tests remain part of the ordinary full capture run.
Diagnostic round trips can have side effects and must remain separate from performance measurement.

## 8. Basis for presenting the results

The supported conclusion is that targeted reducers and explicit exclusions removed the observed capture failures in the validated runs.
The repairs retain useful test and application data while identifying selected runtime state that the trace cannot restore.

Present capture-error reduction together with the exclusion boundary and the validation scope.
Do not describe a partial snapshot as a complete checkpoint.
Do not use test pass counts or zero capture errors alone as evidence of behavioral equivalence after restoration.

The next validation priorities are the original full benchmark environment, unresolved diagnostic objects, and older reducers that discard data or behavior.
No benchmark performance improvement is claimed by this serialization report.

## 9. Evidence and implementation references

This report is self-contained. The following files provide implementation details and the investigation history:

- [Beets provider](beets/spacetimepy_picklers.py), [capture configuration](beets/conftest.py), and [detailed audit](beets/SERIALIZATION.md).
- [CherryPy provider](cherrypy/spacetimepy_custom_pickler.py) and [detailed audit](cherrypy/SERIALIZATION.md).
- [Discord.py provider](discord.py/spacetimepy_custom_pickler.py) and [detailed audit](discord.py/SERIALIZATION.md).
- [DSPy provider](dspy/spacetime_dill.py) and [detailed audit](dspy/SERIALIZATION.md).
- [PyMISP provider](pymisp/spacetimepy_custom_pickler.py) and [detailed audit](pymisp/SERIALIZATION.md).
- [Beets regression tests](beets/test_picklers.py) and [mock regression tests](tests/test_mock_picklers.py).
- [Diagnostic probe](beets/diagnose_serialization.py), which is separate from performance measurement.

Local validation logs and counts are stored under `.reports/serialization-fixes/`, including `beets-final/` and `mock-reducers/`.
These local artifacts are ignored by Git. The original raw benchmark results were not overwritten.
