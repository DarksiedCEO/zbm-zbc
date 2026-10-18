# toy-ts — fixture package for the fix engine's Node toolchain adapter (ADR 0011 toolchain amendment)

A tiny dependency-free TypeScript package (Node 22 type stripping, `node --test`, a lockfile with no packages)
with the toy-py defects the deterministic fake model fixes: `src/calc.ts::add` returns a difference instead of a
sum (`tests/calc.test.ts::add_returns_sum` fails on the untouched tree — the suite's one pre-existing failure);
`percent` throws on a zero `whole`. A skipped test exercises the junit/TAP cross-check. Nothing here is production
code.
