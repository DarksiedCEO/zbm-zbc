# toy-rs — fixture crate for the fix engine's Rust toolchain adapter (ADR 0011 toolchain amendment)

A tiny stable-Rust crate with the toy-py defects the deterministic fake model fixes: `src/lib.rs::add` returns a
difference instead of a sum (`tests/calc.rs::add_returns_sum` fails on the untouched tree — the suite's one
pre-existing failure); `percent` panics on a zero `whole` instead of answering `0.0`. It has three test binaries
(the lib's unit tests with one `#[ignore]`, the `calc` integration test, and one doc-test) so the engine's
per-binary cross-check is exercised. `Cargo.lock` is committed (`--locked`). Nothing here is production code.
