# DSPy serialization summary

This is a source audit. The current checkout contains no DSPy profiling database or test log.
The policy is to preserve data and exclude values whose behavior cannot be restored.

## Registration

The benchmark opens SpaceTimePy in `pytest_configure`.
The provider imports specific DSPy and dependency types while it builds the table.
It does not rely on enumerating test classes after they appear.

It also installs inherited `__reduce_ex__` and `__setstate__` methods on:

- `Mock` and `MagicMock`, including their later per-instance subclasses.
- DSPy `Module`, including later user-defined module subclasses.

The table registers `ProgramMeta` and `SignatureMeta` explicitly because dispatch uses exact metaclass types.
Thus, the covered late-created classes use mechanisms absent from the original Beets provider.
New custom metaclasses and unrelated subclasses can still need registration.
The inherited method changes affect the process, not only one SpaceTimePy runtime.

## Serializer and exclusion decisions

| Value | Current treatment | Assessment under the policy |
| --- | --- | --- |
| DSPy modules | Preserves instance state and repairs bound `forward` methods after allocation. | Serializer candidate. Check shared parameters, graph cycles, and custom bound methods. |
| Python, dataclass, enum, and Pydantic classes | Reconstructs class state through several specialized reducers. | Serializer candidates. Check validators, descriptors, defaults, closure references, and custom metaclasses. |
| Functions and modules | Imports available references. For other functions, saves referenced globals, defaults, closures, and attributes. | Candidate with limits. Dynamic global lookup can require names absent from bytecode-based analysis. Importing a module does not restore historical mutations. |
| Mocks | Normalizes call histories and restores mock state through inherited methods. | Candidate for Beets reuse after validation. A subclass can restore as plain `Mock` or `MagicMock`, so custom subclass behavior needs a check. |
| `_Call` and `_CallList` | Saves tuple data and reconstructs records. | Candidate for simple call records. Additional `_Call` instance fields and chained-call parent relationships need validation. |
| Patched optimizer methods | Removes selected method mocks from `BetterTogether`, `BootstrapFinetune`, and `ParallelExecutor`. | Changes behavior. Preserve the mocked method or exclude the affected state. |
| Cache | Saves configuration and memory entries. Reopens the configured disk-cache directory. | Candidate for explicit memory-cache data. External disk state is not captured. Method mocks and other state can be lost. |
| `ContextVar` and token | Recreates variables with effective values as defaults. Always creates a new active token. | Exclude live context identity and tokens. Context bindings, original defaults, and already-reset token state do not survive. |
| Generator, async generator, coroutine | Creates replacement execution objects without original frames or value streams. | Exclude live execution objects. These are not behavior-preserving serializers. |
| `asyncio.Task` | Creates a new task with an outcome or an indefinitely waiting replacement coroutine. | Preserve results or exceptions as data. Exclude live tasks. Restoration needs a running loop and does not preserve immediate completion state. |
| Thread and thread pool | Recreates runtime resources without active execution or pending work. | Exclude live execution state. Save task inputs, completed results, and configuration separately. |
| Async event and capacity limiter | Preserves a flag or total capacity. Drops waiters and active borrowers. | Configuration snapshots only. Exclude active synchronization state. |
| `csv.DictReader` | Restores configuration and line position over an empty replacement stream. | Exclude the active reader. Preserve source data or parsed rows. Remaining records are lost. |
| Temporary file wrapper | Reads the durable path and restores an in-memory stream. Omits unflushed buffers and disables source deletion behavior. | Does not preserve general file behavior. Capture explicit bytes after an application-controlled flush if required. |
| Pytest fixtures, config, requests, and monkeypatch | Replaces active runtime services with partial state. Monkeypatch undo actions are discarded. | Exclude live controllers. Preserve relevant options, output, logs, and application values separately. |
| Exceptions and log records | Keeps selected exception data and formatted text without original traceback frames. | Candidate for explicit diagnostic data. Do not claim frame or full exception-chain restoration without checks. |

## Checks to perform next

- Create mocks and DSPy module subclasses after runtime initialization.
- Verify mock specifications, magic methods, chained calls, custom subclasses, and shared children.
- Check module cycles and Pydantic validation after restoration.
- Check functions that use `globals()` or dynamic imports.
- Compare remaining CSV rows and unflushed file bytes to expose data loss.

The provider covers many types, but several reducers explicitly provide inspection-only replacements.
A low dump-failure count would not establish compliance with the selected preservation policy.

Sources: [provider](spacetime_dill.py), [benchmark configuration](conftest.py), [comparison](../SERIALIZATION.md).
