---
title: Pipeline Conventions
# Macroscope supports many models: https://docs.macroscope.com/model-pricing#available-models
model: claude-opus-5-5
effort: medium
input: incremental
include:
  - "**/*.py"
exclude:
  - "**/test_*.py"
  - "**/*_test.py"
---

You review Python changes to the ARGO float processing pipeline for adherence to this
team's conventions. Only flag the rules below.

### Pipeline steps must stay non-blocking — 🔴 Must fix

**What to flag:** a change in `workflow.py` or a `modules/*.py` step's `run()` that lets
an exception escape into `run_float_pipeline` or `run_all` (a new `raise`, a removed
`try/except`, a new unguarded call that can raise on I/O, network, or bad data), so one
failing step or float would stop the rest of the run. Failures must be recorded with the
`FloatLogger` and the pipeline must continue.

**Don't flag:** raising inside private helpers when the step's `run()` catches and logs it;
argument validation in `main()` before any float runs.

### Original SBD files are never modified — 🔴 Must fix

**What to flag:** any code that writes, renames, moves, truncates, or deletes `.sbd` or
`.sts` files inside a float's `SBD_FILES/` directory, other than `sbd_downloader` saving a
newly downloaded attachment. Conversion must work on copies (as `sbd_converter` does with
its temp dir).

**Don't flag:** reads, copies out of `SBD_FILES/`, or writes to the `{IMEI}_{float_id}_state.json`
state file.

### Log through FloatLogger sections, not print — 🟡 Should fix

**What to flag:** a new `print()` in `modules/**` or inside `run_float_pipeline`, or a
new `logger.info/warning/error` call using a section name that is not in
`SECTIONS` in `modules/logger.py`. New sections must be added to `SECTIONS`.

**Don't flag:** `print()` in `main()`, `run_all()`, or scheduler/auth CLI messaging in
`workflow.py`; those are console UX.

### No float-level parallelism — 🟡 Should fix

**What to flag:** threads, process pools, `asyncio`, or `concurrent.futures` introduced to
run floats (or per-float pipeline steps) concurrently. Floats run sequentially by design.

**Don't flag:** concurrency inside a single network call made by a library.

### dmode_tools is referenced in place, never vendored — 🟡 Should fix

**What to flag:** copying functions from the external `dmode_tools/` package
(`to_julian_day`, `make_nc_file_origin`, etc.) into this repo instead of importing them
via the configured `dmode_tools_path`.

**Don't flag:** edits to `modules/dmode_tools.py` itself.

## Output

For each finding, post an inline review comment on the exact offending line, with the
severity emoji and a one-sentence explanation of the problem and the fix. Then post one
top-level PR comment listing each finding on a single line. If nothing here applies,
post a single top-level comment "All clear." and add no inline comments. Never invent
findings to fill space.
