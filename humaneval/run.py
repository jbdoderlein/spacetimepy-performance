"""Run generated HumanEval programs and collect process measurements."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
GENERATED = HERE / "generated"
INDIVIDUAL_RESULTS = HERE / "results" / "individual"
RESULT_MARKER = "__HUMANEVAL_RESULT__="
PERFORMANCE_VARIANTS = ("reference", "function", "line")
ALL_VARIANTS = (*PERFORMANCE_VARIANTS, "quality")


def _remove_database(program: Path) -> None:
    database = program.with_suffix(".db")
    for path in (
        database,
        database.with_name(f"{database.name}-wal"),
        database.with_name(f"{database.name}-shm"),
    ):
        path.unlink(missing_ok=True)


def _parse_stdout(stdout: str) -> tuple[dict[str, Any] | None, str]:
    payload = None
    retained_lines: list[str] = []
    for line in stdout.splitlines():
        if line.startswith(RESULT_MARKER):
            payload = json.loads(line.removeprefix(RESULT_MARKER))
        else:
            retained_lines.append(line)
    retained = "\n".join(retained_lines)
    if stdout.endswith("\n") and retained:
        retained += "\n"
    return payload, retained


def _run_program(program: Path, timeout_s: float) -> dict[str, Any]:
    _remove_database(program)
    started = time.perf_counter()
    process = subprocess.Popen(
        [sys.executable, str(program)],
        cwd=program.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
    wall_time_s = time.perf_counter() - started

    try:
        payload, retained_stdout = _parse_stdout(stdout)
    except json.JSONDecodeError as error:
        payload = None
        retained_stdout = stdout
        stderr += f"\nInvalid benchmark result JSON: {error}\n"

    result: dict[str, Any] = {
        "success": not timed_out and process.returncode == 0 and payload is not None,
        "timeout": timed_out,
        "timeout_limit_s": timeout_s,
        "return_code": process.returncode,
        "wall_time_s": wall_time_s,
        "stdout": retained_stdout,
        "stderr": stderr,
    }
    if payload is not None:
        result.update(payload)
    database = program.with_suffix(".db")
    result["database_size_bytes"] = database.stat().st_size if database.exists() else 0
    return result


def _is_complete(result: dict[str, Any], variants: tuple[str, ...]) -> bool:
    return all(result.get(variant, {}).get("success") for variant in variants)


def _run_problem(
    problem_index: int,
    variants: tuple[str, ...],
    timeout_s: float,
    rerun: bool,
) -> dict[str, Any]:
    result_path = INDIVIDUAL_RESULTS / f"result_{problem_index}.json"
    result: dict[str, Any] = {"problem_index": problem_index}
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not rerun and _is_complete(result, variants):
            return result

    for variant in variants:
        if not rerun and result.get(variant, {}).get("success"):
            continue
        program = GENERATED / variant / f"{problem_index}.py"
        result[variant] = (
            _run_program(program, timeout_s)
            if program.exists()
            else {"success": False, "error": f"Missing program: {program}"}
        )

    temporary_path = result_path.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    temporary_path.replace(result_path)
    return result


def _available_problems() -> list[int]:
    directory = GENERATED / "reference"
    if not directory.exists():
        return []
    return sorted(
        int(path.stem) for path in directory.glob("*.py") if path.stem.isdigit()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the HumanEval benchmark.")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--processes", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--problem", type=int, action="append")
    parser.add_argument(
        "--variant",
        choices=ALL_VARIANTS,
        action="append",
        help="Run this variant. The default runs the three performance variants.",
    )
    parser.add_argument("--rerun", action="store_true")
    arguments = parser.parse_args()

    variants = tuple(dict.fromkeys(arguments.variant or PERFORMANCE_VARIANTS))
    available = _available_problems()
    problems = arguments.problem or available
    if not available:
        parser.error("No generated programs exist. Run prepare.py first.")
    unavailable = sorted(set(problems).difference(available))
    if unavailable:
        parser.error(f"These problem indices are not generated: {unavailable}")

    INDIVIDUAL_RESULTS.mkdir(parents=True, exist_ok=True)
    work = [(index, variants, arguments.timeout, arguments.rerun) for index in problems]
    with ProcessPoolExecutor(max_workers=arguments.processes) as executor:
        results = list(executor.map(_run_problem_from_tuple, work))

    failed = [
        result["problem_index"]
        for result in results
        if not _is_complete(result, variants)
    ]
    print(f"Completed {len(results) - len(failed)}/{len(results)} problem(s).")
    if failed:
        print(f"Failed problem indices: {failed}")
        raise SystemExit(1)


def _run_problem_from_tuple(
    arguments: tuple[int, tuple[str, ...], float, bool],
) -> dict[str, Any]:
    return _run_problem(*arguments)


if __name__ == "__main__":
    main()
