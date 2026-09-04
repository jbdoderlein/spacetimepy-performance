# HumanEval experiment

This experiment measures SpaceTimePy on the canonical HumanEval+ programs.
It measures the programs directly. It does not measure a project test suite.

The experiment creates these variants:

- `reference` runs each program without SpaceTimePy.
- `function` records function-call steps with SpaceTimePy v2.
- `line` records line snapshots with SpaceTimePy v2.
- `quality` checks the data from line capture through v2 replay.

## Prepare the programs

Run this command from this directory:

```shell
uv run python prepare.py
```

Use `--problem 0` to generate one problem for a short test.

## Measure performance

Run the three performance variants:

```shell
uv run python run.py
```

Use `--processes`, `--timeout`, or repeated `--problem` options to limit a run.
The runner keeps successful results. Use `--rerun` to replace those results.

## Check capture quality

Run the quality variant:

```shell
uv run python run.py --variant quality
```

The quality variant records each canonical program with line capture. It finds
the first snapshot for each captured invocation. It then gives the stored
locals to a v2 replay callback. The callback rebuilds the function arguments
and runs the function again. The check compares the replay result with the
stored return value.

This check validates argument serialization, snapshot state, return-value
serialization, and replay. It does not continue a suspended Python frame.
SpaceTimePy v2 requires an integration to execute the restart callback.

## Combine results

```shell
uv run python combine_results.py
```

The command writes `results/humaneval_results.json`. Open
`analyze_results.ipynb` for additional analysis.
