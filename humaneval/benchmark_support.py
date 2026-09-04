"""Runtime support for generated HumanEval programs."""

from __future__ import annotations

import inspect
import json
import resource
import time
from pathlib import Path
from typing import Any

from spacetimepy import SpaceTime

RESULT_MARKER = "__HUMANEVAL_RESULT__="


def _emit(payload: dict[str, Any]) -> None:
    """Write one result record that the benchmark runner can parse."""

    payload["max_rss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(f"{RESULT_MARKER}{json.dumps(payload, default=repr)}")


def _timed_check(check: Any, target: Any) -> list[int]:
    timings_ns: list[int] = []

    def timed_target(*args: Any, **kwargs: Any) -> Any:
        started_ns = time.perf_counter_ns()
        try:
            return target(*args, **kwargs)
        finally:
            timings_ns.append(time.perf_counter_ns() - started_ns)

    check(timed_target)
    return timings_ns


def run_reference(check: Any, target: Any) -> None:
    """Run one HumanEval check without SpaceTimePy capture."""

    _emit({"internal_times_ns": _timed_check(check, target)})


def _capture_profile(space: SpaceTime) -> dict[str, int]:
    rows = space.data.list_function_call_performances()
    return {
        "function_call_count": len(rows),
        "start_capture_ns": sum(row.start_capture_ns for row in rows),
        "return_capture_ns": sum(row.return_capture_ns for row in rows),
        "unwind_capture_ns": sum(row.unwind_capture_ns for row in rows),
        "line_capture_ns": sum(row.line_capture_ns for row in rows),
        "direct_capture_ns": sum(row.direct_capture_ns for row in rows),
        "inclusive_capture_ns": sum(row.inclusive_capture_ns for row in rows),
        "line_event_count": sum(row.line_event_count for row in rows),
        "line_snapshot_count": sum(row.line_snapshot_count for row in rows),
        "filtered_line_event_count": sum(row.filtered_line_event_count for row in rows),
    }


def run_capture(
    check: Any,
    target: Any,
    *,
    mode: str,
    database_path: str | Path,
    task_id: str,
) -> None:
    """Run one HumanEval check with v2 function or line capture."""

    with SpaceTime.open(database_path, profile_capture=True) as space:
        captured = (
            space.capture.function(target)
            if mode == "function"
            else space.capture.line(target)
        )
        with space.capture.recording(
            mode=mode,
            name=f"HumanEval {task_id} {mode}",
            attributes={"benchmark": "humaneval", "task_id": task_id},
        ):
            timings_ns = _timed_check(check, captured)
        profile = _capture_profile(space)
        statistics = space.data.get_statistics()

    _emit(
        {
            "internal_times_ns": timings_ns,
            "capture_profile": profile,
            "trace": {
                "function_call_count": statistics.function_call_count,
                "stack_snapshot_count": statistics.stack_snapshot_count,
                "stored_value_count": statistics.stored_value_count,
            },
        }
    )


def _invoke_from_locals(target: Any, local_values: dict[str, Any]) -> Any:
    """Call a function with argument values from a captured frame."""

    positional: list[Any] = []
    keywords: dict[str, Any] = {}
    for parameter in inspect.signature(target).parameters.values():
        value = local_values[parameter.name]
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            positional.extend(value)
        elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
            keywords.update(value)
        elif parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional.append(value)
        else:
            keywords[parameter.name] = value
    return target(*positional, **keywords)


def _same_value(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    try:
        comparison = left == right
        return comparison if isinstance(comparison, bool) else bool(comparison)
    except Exception:  # noqa: BLE001 - user-defined equality can raise any error
        return False


def run_quality_check(
    check: Any,
    target: Any,
    *,
    database_path: str | Path,
    task_id: str,
) -> None:
    """Replay each invocation from its first snapshot and compare outputs."""

    failures: list[dict[str, Any]] = []
    replayed_call_ids: set[int] = set()
    with SpaceTime.open(database_path) as space:
        captured = space.capture.line(target)
        with space.capture.recording(
            mode="line",
            name=f"HumanEval {task_id} quality",
            attributes={"benchmark": "humaneval", "task_id": task_id},
        ) as recording:
            check(captured)

        root = space.data.get_branch(recording.branch_id)
        first_steps: dict[int, Any] = {}
        for step in root.steps:
            snapshot = step.stack_snapshot
            if snapshot is not None:
                first_steps.setdefault(snapshot.function_call_id, step)

        for call_id, step in first_steps.items():
            call = space.data.get_function_call(call_id)
            if call.return_reference is None:
                failures.append(
                    {"call_id": call_id, "reason": f"outcome={call.outcome}"}
                )
                continue
            try:
                replay = space.replay.run(
                    lambda context: _invoke_from_locals(captured, context.locals),
                    parent_branch_id=root.id,
                    forked_from_step_id=step.id,
                    name=f"quality replay {call_id}",
                    recipe={"benchmark_check": "snapshot-output-equality"},
                )
                expected = space.data.load_value(call.return_reference)
                if not _same_value(replay.value, expected):
                    failures.append(
                        {
                            "call_id": call_id,
                            "reason": "result mismatch",
                            "expected": repr(expected),
                            "actual": repr(replay.value),
                        }
                    )
                replayed_call_ids.add(call_id)
            except Exception as error:  # noqa: BLE001 - benchmark code can raise any error
                failures.append(
                    {
                        "call_id": call_id,
                        "reason": f"{type(error).__name__}: {error}",
                    }
                )

        statistics = space.data.get_statistics()

    _emit(
        {
            "quality": {
                "captured_call_count": len(first_steps),
                "replayed_call_count": len(replayed_call_ids),
                "failure_count": len(failures),
                "failures": failures,
                "root_snapshot_count": len(root.steps),
                "total_stored_value_count": statistics.stored_value_count,
            }
        }
    )
    if failures:
        raise SystemExit(1)
