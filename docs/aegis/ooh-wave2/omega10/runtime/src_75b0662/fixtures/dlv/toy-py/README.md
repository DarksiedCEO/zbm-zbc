# toy-py — fixture service for the fix engine's certification suite (DEPT28_SPEC §F S1)

A tiny pytest project with two planted defects the deterministic fake model fixes:

- `src/toy/calc.py::add` returns a difference instead of a sum (finding N1-1; `tests/test_calc.py::test_add_returns_sum`
  fails on the untouched tree);
- `src/toy/calc.py::percent` divides by zero when `whole == 0` instead of answering `0.0` (finding N1-2;
  `tests/test_percent.py::test_percent_zero_whole` is its reproduction and fails on the untouched tree).

Wave 21 (R1): every finding must name a machine-runnable reproduction that exists at the base commit, so the
fixture carries one per planted defect. A harness built with `pct_repro=False` leaves `tests/test_percent.py` out
of the committed fixture: a run about N1-1 alone then has one pre-existing failure (N1-1's own reproduction) and can
reach `awaiting_review`; with the file present, the N1-2 failure is not attributable to an N1-1-only run and blocks
`fixed`, exactly as an unrelated pre-existing failure should.

The certification tests copy this directory into a temporary git repository (branch `integration-2026-09-24`) and
run the engine against it. Nothing here is production code.
