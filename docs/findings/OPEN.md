# Open findings (Medium / Low)

Founder ruling R-GATE (Oct 1 2026): Critical/High findings block a merge; Medium/Low findings are tracked here, one
line each, and the very next wave fixes them. Nothing is parked: a line leaves this file only in the commit that
fixes it (or that records the founder's ruling to close it). Format: `id | severity | owner (wave that must fix it) |
opened | what`.

Ids: `C*` scout C (wave 25 hygiene catalogue), `H9-*` the wave-25 hygiene check's own findings on the tree. Counts of
lint hits are as of the E-C branch (`w25c`) and are reproduced with `python3 devtools/hygiene_check.py lint`; the
E-A / E-B branches change them when they merge.

| id | severity | owner | opened | what |
|---|---|---|---|---|
| H9-L1 | Medium | E-A / E-B, wave 26 | 2026-10-01 | Python tests assert upper bounds on wall-clock deltas against literals or literal-valued names (hygiene rule L1): fulfillment-py 14, onboarding-py 14, creative-py 11, delivery-py 9, finance-py 2, legal-py 2, verification-py 2, detection-py 1 — `lint --rules L1` lists each; convert to CPU-time / same-work ratios / events, or allowlist with a reviewed reason |
| H9-L2 | Medium | E-A / E-B, wave 26 | 2026-10-01 | Hard-coded test ports/ranges (rule L2): delivery-py 5, creative-py 2, detection-py 1, onboarding-py 1 — move the live tests onto the shared helper in `tests/_procinfo.py` (`start_owned` / `wait_owned` / `assigned_port_range`); parser-only literals need an allowlist entry with a reason |
| H9-L3 | Low | E-A / E-B, wave 26 | 2026-10-01 | Hand-written test counts (rule L3): fulfillment-py README 27, ADRs 0002 / 0004 / 0005 / 0010 / 0011 (13), onboarding-py README 2, delivery-py / finance-py / legal-py README 1 each — link docs/test-counts.md or tie each historical count to its commit |
| C5-6 | Medium | E-A / E-B, wave 26 | 2026-10-01 | The Python live tests still use five per-service pick-then-bind port pickers (the race wave 21 removed from ledger-rust); the shared replacement with an owner check exists (`tests/_procinfo.py`, wave 25, identical in five services) but no live test calls it yet |
| C6-2 | Medium | E-A (compliance) / E-B (verification, clipper-network, finance, legal), wave 26 | 2026-10-01 | `devtools/live_run.py` leaves its `mkdtemp` work dir behind on every run; under the CI live-runs job's hygiene check (R3) that fails the job; finance uses `FIN_LIVE_WORKDIR` where the others use `LIVE_WORK_DIR` (C5-7) |
| C6-3 | Medium | E-B, wave 26 | 2026-10-01 | delivery-py `gitport.py` creates its `dlv-git-*` isolation dir at import time; a scrubbed-environment child killed by a signal leaves it in /tmp — the hygiene run of delivery-py (R3) is the test |
| C1-1 | Low | E-C, wave 26 | 2026-10-01 | ledger-rust tests write fixed-prefix files straight into the temp dir (`ledger_*`, `zbm_ledger_test_*`); contained by the private TMPDIR under the hygiene wrapper, but a crashed test outside it leaves top-level /tmp entries — move them into one per-process directory |
| C5-8b | Low | E-C, wave 26 | 2026-10-01 | `.gitignore` ignores `*.jsonl` repo-wide with one exception (ledger-rust test fixtures): a JSONL fixture added to any other service is silently un-addable; scope the rule to the runtime paths that write ledgers |
| C4-4 | Low | E-B, wave 26 | 2026-10-01 | Root README delivery section: heading says "(fix wave 20 applied)" while the body cites wave 23, and the "/tmp leftovers known, not fixed" sentence was not re-verified after wave 24 — the delivery hygiene run settles it; E-B to send the corrected text (E-C owns the file) |
| C4-5 | Low | founder ruling needed | 2026-10-01 | References to documents that live only in a session scratchpad: `revenue-recovery-founder-decisions.md`, `revenue-recovery-roadmap.md`, `BUILD_CONTRACTS.md`, `CLIPPER_NETWORK_SPEC.md`, `LEGAL_SPEC.md`, `DEPT28_SPEC.md`, `FIX_WAVE_23b.md`; and ADR "evidence" citing reviewer probes outside the repo — commit them under docs/ or mark each reference as external |
| C5-3 | Low | E-A / E-B, wave 26 | 2026-10-01 | Seven launchers accept any positive switch interval (`_positive`); detection-py's range check is not shared |
| C5-4 | Low | E-A / E-B, wave 26 | 2026-10-01 | Only delivery-py's launcher converts SIGTERM into a normal exit; atexit handlers in the other eight launchers would be skipped (latent: none registered today) |
| H9-R3m | Low | E-C, wave 26 | 2026-10-01 | Hygiene rule limits, stated: R3 sees the checkout, the private TMPDIR and /tmp only (not ~/.cache etc.); R4 on macOS has no subreaper (a setsid'd, env-scrubbed grandchild there is not found); L1 for Rust/Go/TS matches the names `elapsed` / `time.Since` / `open` / `Date.now()` only (a delta in another variable name is not seen), L3 English count phrases only |
| H9-R6m | Low | E-C, wave 26 | 2026-10-01 | `docs/test-counts.md` is generated locally and committed; a merge of two branches that both changed tests turns every CI test job red until the rows are regenerated (stated in docs/ci.md) — a CI step that writes the rows on `main` would remove the manual step |
