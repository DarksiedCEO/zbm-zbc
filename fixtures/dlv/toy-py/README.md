# toy-py — fixture service for the fix engine's certification suite (DEPT28_SPEC §F S1)

A tiny pytest project with two planted defects the deterministic fake model fixes:

- `src/toy/calc.py::add` returns a difference instead of a sum (finding N1-1; `tests/test_calc.py::test_add_returns_sum`
  fails on the untouched tree — the suite's one pre-existing failure);
- `src/toy/calc.py::percent` divides by zero when `whole == 0` instead of answering `0.0` (finding N1-2).

The certification tests copy this directory into a temporary git repository (branch `integration-2026-09-24`) and
run the engine against it. Nothing here is production code.
