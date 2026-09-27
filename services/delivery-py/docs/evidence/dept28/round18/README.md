# AEGIS round 18 → fix wave 19 evidence (`services/delivery-py`, branch `fix19-delivery` from `delivery-department` @ 07846d1)

Files here are the raw captures the wave-19 report cites. Nothing in them was edited.

| File | What |
|---|---|
| `suite-before-07846d1.log` | the full suite at 07846d1 before any change: 388 passed, 3 skipped (202.9 s) |
| `test_round18-before-07846d1.log` | `tests/test_round18.py` (one failing-first test per finding N18-S-1..9 / N18-E-1..7) run in a detached checkout of 07846d1: every test fails on the unfixed code |
| `suite-after.log` | the full suite on `fix19-delivery`: 422 passed, 3 skipped |
| `probes-engine-after.log` | the reviewers' engine probes (`review18/engine/probes/`) re-run unmodified against the fixed code; a probe's assertion describes the ATTACK succeeding, so a failing probe is a blocked attack (14 failed = blocked; the 6 that "pass" have no assertion or, for P7, a fence-count heuristic that cannot see fence length — see the report) |
| `p*.after.log` | the reviewers' seam probes (`review18/seams/p1…p10`) re-run unmodified |
| `../live-launcher-run-wave19.log` | the hardened launcher as a real process on port 18800 (clean env): `/health`, `/policy`, a fix-run refused 503 `SANDBOX_UNAVAILABLE` (no daemon), and the three round-18 switches refused by `config.load` in a real process |

Docker live properties (uid, seccomp, `--network none`, no curl/wget, no `.git`, labels, deadline kill, volume gone)
remain **not provable here** — `tests/test_live_docker.py` skips with the reason and lists them.
