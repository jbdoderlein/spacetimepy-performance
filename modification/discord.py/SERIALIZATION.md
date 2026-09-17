# Discord.py serialization summary

This is a source audit. The current checkout contains no Discord.py profiling database or test log.
The policy is to preserve data and exclude values whose behavior cannot be restored.

## Registration

The benchmark opens SpaceTimePy in `pytest_configure`.
The provider calls `_install_subclass_dispatch()` before SpaceTimePy copies Dill's dispatch table.
This function replaces `__missing__` on the dispatch table's class.
For selected subclasses, it returns the registered callback for their base class.

The selected bases are `Group`, `Cog`, `NonCallableMock`, `BaseView`, `ActionRow`, and `Container`.
Thus, subclasses created after collection can receive these reducers.
This differs from the original Beets provider, which only enumerated classes already loaded.

The hook changes a class shared by Dill dispatch tables. It is process-wide, not runtime-local.
Coverage remains limited to the listed bases. It does not establish support for every later type.

## Serializer and exclusion decisions

| Value | Current treatment | Assessment under the policy |
| --- | --- | --- |
| Command groups and Cogs | Saves instance state and repairs owner references. Local Cog classes have a metaclass reducer. | Serializer candidates. Check ownership, nested groups, callbacks, and cycles after restoration. |
| Mock instances | Selects a stable mock base type and restores instance fields. | A useful candidate for Beets mock failures. Check specifications, magic methods, children, side effects, and custom mock subclasses before reuse. |
| Mock `_Call` records | Saves the tuple and instance fields. | A useful candidate for Beets restoration failures. Check chained calls and cyclic references before reuse. |
| Local `ActionRow` and `Container` classes | Reconstructs a class from its namespace. Only direct supported bases are accepted for local classes. | Serializer candidates with an explicit limit. Test methods, descriptors, child identity, and nested local subclasses. |
| Active views | Removes timeout tasks, cancellation callbacks, and timeout expiry. A pending stopped-future becomes `None`. | Exclude live view execution state. This does not resume the original view lifecycle. Preserve UI data separately. |
| Completed view futures | Replaces the original future with `_CompletedViewFuture`. | An outcome snapshot. It preserves selected methods, but not future type or event-loop identity. Do not count it as full future restoration. |
| Pytest `ExceptionInfo` | Saves formatted traceback text. Restoration raises the exception again to obtain a new traceback. | Preserve exception data and text separately. The new traceback does not preserve the original frames. |

## Checks to perform next

- Create a supported subclass after runtime initialization and verify that its reducer is called.
- Restore mocks with specifications, magic methods, side effects, and shared children.
- Verify group and Cog ownership after restoration.
- Compare pending and completed view behavior before any view reducer is accepted for replay.

The provider already contains mock support that Beets lacks.
That explains a possible difference in failures without proving general mock fidelity.
Do not copy its process-wide dispatch patch into Beets without a separate architectural decision.

Sources: [provider](spacetimepy_custom_pickler.py), [benchmark configuration](conftest.py), [comparison](../SERIALIZATION.md).
