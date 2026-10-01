# Open findings (Medium / Low)

Founder ruling R-GATE (Oct 1 2026): Critical/High findings block a merge; Medium/Low findings are tracked here, one
line each, and the very next wave fixes them. Nothing is parked: a line leaves this file only in the commit that
fixes it (or that records the founder's ruling to close it). Format: `id | severity | owner (wave that must fix it) |
opened | what`.

Ids: `C*` scout C (wave 25 hygiene catalogue), `H9-*` the wave-25 hygiene check's own findings on the tree.

| id | severity | owner | opened | what |
|---|---|---|---|---|
| H9-L1 | Medium | E-A / E-B, wave 26 | 2026-10-01 | Python tests assert upper bounds on wall-clock deltas against literals (hygiene rule L1): creative-py 11, delivery-py 9, onboarding-py 14, fulfillment-py 13, verification-py 2, legal-py 2, finance-py 2, detection-py 1 — `python3 devtools/hygiene_check.py lint --rules L1` lists each; convert to CPU-time / same-work ratios / events, or allowlist with a reviewed reason |
| H9-L2 | Medium | E-A / E-B, wave 26 | 2026-10-01 | Hard-coded test ports/ranges (rule L2): detection-py `test_request_limits_live.py` (19960-19969), onboarding-py `conftest.py` (19920-19939), creative-py `test_fix_wave_5.py` / `test_fix_wave_6.py` (20110-20119, 20300-20319), delivery-py `helpers.py` (18800-18849) and two parser tests — move the live tests onto the shared helper in `tests/_procinfo.py` (`start_owned` / `assigned_port_range`); parser-only literals need an allowlist entry with a reason |
| H9-L3 | Low | E-A / E-B, wave 26 | 2026-10-01 | Hand-written test counts in service READMEs and ADRs 0002, 0004, 0005, 0010, 0011 (rule L3) — link docs/test-counts.md or tie each historical count to its commit |
| C5-6 | Medium | E-A / E-B, wave 26 | 2026-10-01 | The Python live tests still use five per-service pick-then-bind port pickers (the race wave 21 removed from ledger-rust); the shared replacement with an owner check exists (`tests/_procinfo.py`, wave 25) but no call site uses it yet |
| C6-2 | Medium | E-A (compliance) / E-B (verification, clipper-network, finance, legal), wave 26 | 2026-10-01 | `devtools/live_run.py` leaves its `mkdtemp` work dir behind on every run; under the CI live-runs job's hygiene check (R3) that fails the job; finance uses `FIN_LIVE_WORKDIR` where the others use `LIVE_WORK_DIR` (C5-7) |
| C6-3 | Medium | E-B, wave 26 | 2026-10-01 | delivery-py `gitport.py` creates its `dlv-git-*` isolation dir at import time; a scrubbed-environment child killed by a signal leaves it in /tmp — the hygiene run of delivery-py (R3) is the test |
| C1-1 | Low | E-C, wave 26 | 2026-10-01 | ledger-rust tests write fixed-prefix files straight into the temp dir (`ledger_*`, `zbm_ledger_test_*`); contained by the private TMPDIR under the hygiene wrapper, but a crashed test outside it leaves top-level /tmp entries — move them into one per-process directory |
| C3-11 | Low | E-C, wave 26 | 2026-10-01 | docs/ci.md's runner-image tool list for ubuntu-24.04 ("image 20260920.314.1", Docker 28.0.4, rustup 1.29) is an unverified statement from wave 21; re-verify against the image README or drop the version numbers |
| C3-12 | Low | E-C, wave 26 | 2026-10-01 | `rustup toolchain install stable` floats in every Rust-using job while docs/ci.md calls the pins exhaustive; pin a Rust version or say "stable floats" plainly |
| C4-4 | Low | E-B, wave 26 | 2026-10-01 | Root README delivery section: heading says "(fix wave 20 applied)" while the body cites wave 23, and the "/tmp leftovers known, not fixed" sentence was not re-verified after wave 24 — the delivery hygiene run settles it; E-B to correct the text (E-C owns the file: send the sentence) |
| C4-5 | Low | founder ruling needed | 2026-10-01 | Dangling references to documents that live only in a session scratchpad: `revenue-recovery-founder-decisions.md`, `revenue-recovery-roadmap.md`, `BUILD_CONTRACTS.md`, `CLIPPER_NETWORK_SPEC.md`, `LEGAL_SPEC.md`, `DEPT28_SPEC.md`; and ADR "evidence" citing reviewer probes outside the repo — commit them under docs/ or mark each reference as external |
| C5-3 | Low | E-A / E-B, wave 26 | 2026-10-01 | Seven launchers accept any positive switch interval (`_positive`); detection-py's range check is not shared |
| C5-4 | Low | E-A / E-B, wave 26 | 2026-10-01 | Only delivery-py's launcher converts SIGTERM into a normal exit; atexit handlers in the other eight launchers would be skipped (latent: none registered today) |
| C2-8 | Low | E-C, wave 26 | 2026-10-01 | dashboard `tests/load-outcome.test.ts` holds the event loop with a 5 s timer around a 50 ms abort; benign, listed for the inventory |
| H9-R3m | Low | E-C, wave 26 | 2026-10-01 | Hygiene rule limits, stated: R3 sees the checkout, the private TMPDIR and /tmp only (not ~/.cache etc.); R4 on macOS has no subreaper (a setsid'd, env-scrubbed grandchild there is not found); the doc-count lint (L3) recognises English count phrases only |
