# toy-go — fixture module for the fix engine's Go toolchain adapter (ADR 0011 toolchain amendment)

A tiny Go module (`example.invalid/toy`, no dependencies, no `go.sum`) with the toy-py defects the deterministic
fake model fixes: `calc.Add` returns a difference instead of a sum (`calc/calc_test.go::TestAddReturnsSum` fails
on the untouched tree through a sub-test — the suite's one pre-existing failure); `calc.Percent` panics on a zero
`whole`. Two packages (`calc`, `cmd/toy`) and a skipped test exercise the engine's per-package cross-check of
`go test -json` against `go test -json -list`. Nothing here is production code.
