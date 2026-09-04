"""Combine HumanEval result files and print a compact summary."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
INDIVIDUAL_RESULTS = HERE / "results" / "individual"
COMBINED_RESULTS = HERE / "results" / "humaneval_results.json"
PERFORMANCE_VARIANTS = ("reference", "function", "line")


def _load_results() -> list[dict[str, Any]]:
    results = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in INDIVIDUAL_RESULTS.glob("result_*.json")
    ]
    return sorted(results, key=lambda result: result["problem_index"])


def _mean_internal_ms(run: dict[str, Any]) -> float | None:
    timings = run.get("internal_times_ns", [])
    return statistics.fmean(timings) / 1_000_000 if timings else None


def _print_summary(results: list[dict[str, Any]]) -> None:
    print(f"Combined problem count: {len(results)}")
    for variant in PERFORMANCE_VARIANTS:
        successful = [
            result[variant]
            for result in results
            if result.get(variant, {}).get("success")
        ]
        means = [
            value for run in successful if (value := _mean_internal_ms(run)) is not None
        ]
        mean_text = (
            f", mean call time {statistics.fmean(means):.3f} ms" if means else ""
        )
        print(f"{variant}: {len(successful)}/{len(results)} successful{mean_text}")

    quality_runs = [result["quality"] for result in results if "quality" in result]
    if quality_runs:
        passed = sum(run.get("success", False) for run in quality_runs)
        replayed = sum(
            run.get("quality", {}).get("replayed_call_count", 0) for run in quality_runs
        )
        failures = sum(
            run.get("quality", {}).get("failure_count", 0) for run in quality_runs
        )
        print(
            f"quality: {passed}/{len(quality_runs)} successful, "
            f"{replayed} calls replayed, {failures} mismatches"
        )


def main() -> None:
    results = _load_results()
    if not results:
        raise SystemExit("No individual results exist. Run run.py first.")
    COMBINED_RESULTS.parent.mkdir(parents=True, exist_ok=True)
    COMBINED_RESULTS.write_text(json.dumps(results, indent=2), encoding="utf-8")
    _print_summary(results)
    print(f"Wrote {COMBINED_RESULTS}.")


if __name__ == "__main__":
    main()
