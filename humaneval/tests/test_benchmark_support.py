"""Tests for HumanEval benchmark support."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HUMANEVAL_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUMANEVAL_DIRECTORY))

from benchmark_support import _invoke_from_locals
from run import _parse_stdout


class BenchmarkSupportTest(unittest.TestCase):
    def test_invoke_from_locals_rebuilds_all_parameter_kinds(self) -> None:
        def target(
            first: int,
            /,
            second: int,
            *items: int,
            scale: int,
            **options: int,
        ) -> tuple[object, ...]:
            return first, second, items, scale, options

        result = _invoke_from_locals(
            target,
            {
                "first": 1,
                "second": 2,
                "items": (3, 4),
                "scale": 5,
                "options": {"extra": 6},
            },
        )

        self.assertEqual(result, (1, 2, (3, 4), 5, {"extra": 6}))

    def test_parse_stdout_removes_the_machine_result(self) -> None:
        payload, stdout = _parse_stdout(
            'program output\n__HUMANEVAL_RESULT__={"value": 3}\n'
        )

        self.assertEqual(payload, {"value": 3})
        self.assertEqual(stdout, "program output\n")


if __name__ == "__main__":
    unittest.main()
