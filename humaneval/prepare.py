"""Generate standalone programs from the HumanEval+ dataset."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DEFAULT_DATASET = HERE / "humanevalplus.parquet"
DEFAULT_OUTPUT = HERE / "generated"
REQUIRED_COLUMNS = {"prompt", "canonical_solution", "test", "entry_point"}


def _footer(variant: str, entry_point: str, task_id: str) -> str:
    common = f"""\n\nfrom pathlib import Path as _Path
import sys as _sys

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from benchmark_support import {"run_quality_check" if variant == "quality" else "run_capture" if variant != "reference" else "run_reference"} as _run

_target = {entry_point}
"""
    if variant == "reference":
        return common + "_run(check, _target)\n"
    if variant == "quality":
        return common + (
            "_run(check, _target, database_path=_Path(__file__).with_suffix('.db'), "
            f"task_id={task_id!r})\n"
        )
    return common + (
        "_run(check, _target, "
        f"mode={variant!r}, "
        "database_path=_Path(__file__).with_suffix('.db'), "
        f"task_id={task_id!r})\n"
    )


def _source(row: pd.Series, variant: str, task_id: str) -> str:
    parts = [row["prompt"], row["canonical_solution"], row["test"]]
    body = "".join(part if part.endswith("\n") else f"{part}\n" for part in parts)
    return body + _footer(variant, row["entry_point"], task_id)


def prepare(
    dataset_path: Path,
    output_path: Path,
    selected_indices: set[int] | None = None,
) -> int:
    """Generate all requested benchmark variants and return their count."""

    frame = pd.read_parquet(dataset_path)
    missing = REQUIRED_COLUMNS.difference(frame.columns)
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"Dataset does not contain required columns: {names}")

    variants = ("reference", "function", "line", "quality")
    for variant in variants:
        (output_path / variant).mkdir(parents=True, exist_ok=True)

    generated = 0
    for position, (_, row) in enumerate(frame.iterrows()):
        if selected_indices is not None and position not in selected_indices:
            continue
        task_id = str(row.get("task_id", position))
        for variant in variants:
            destination = output_path / variant / f"{position}.py"
            destination.write_text(
                _source(row, variant, task_id),
                encoding="utf-8",
            )
        generated += 1
    return generated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate reference, capture, and quality programs."
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--problem",
        type=int,
        action="append",
        help="Generate only this zero-based dataset index. Repeat as needed.",
    )
    arguments = parser.parse_args()
    selected = set(arguments.problem) if arguments.problem else None
    count = prepare(arguments.dataset, arguments.output, selected)
    print(f"Generated {count} HumanEval problem(s) in {arguments.output}.")


if __name__ == "__main__":
    main()
