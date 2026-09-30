# Fix wave 23 evidence (AEGIS round 22; founder design change D1-D3)

Commits: 39e9d9c (ledger-rust B7), e96bf0b (fulfillment B6/B8), 0d198be (B5, ten services), c71e363 (delivery
D1-D3, B1-B4), c827329 (docs). Commit times 2026-09-30T12:53:10Z-12:53:35Z (docs amended ~13:3xZ). Every `after-*`
file below was produced after 12:53:35Z; the code under test in every `after-*` run is c71e363's (c827329 changes
docs only). "Before" runs are on a `git archive 540a64e` tree with ONLY the new test files copied in.

| File | What it shows |
|---|---|
| `before-test_round23.log` | delivery `tests/test_round23.py` on 540a64e: 16 failed (D1 ×5, D2 ×2, D3 ×3, B1, B2 — GET took 15.2 s behind the admission's container, B3, B4 ×2, B5) |
| `before-ful-test_fix23.log` | fulfillment `tests/test_fix23_no_mmap_threshold.py` on 540a64e: 2 failed, 1 passed |
| `before-ledger-port-file.log` | ledger-rust `tests/server_port_file.rs` on the 540a64e server: 3 of the 4 new tests fail |
| `after-ledger-test-clippy.log` | ledger-rust `cargo test --locked` (60 unit + 53 integration, 0 failed) and clippy `-D warnings` clean |
| `after-g7-probes.log` | the reviewers' `g7_portfile.py` / `g7_dirlink.py` against the fresh release binary (sha256 in the log) |
| `after-ful-128senders-20x-single-busy.txt`, `after-ful-fix8-module-10x-busy.txt` | B6: 20/20 and 10/10 under three busy loops with the mmap call removed |
| `after-g4-50x-busy-py313.txt`, `after-g4-50x-busy-py312.txt` | B3: the G4 test 50/50 on each Python under three busy loops |
| `probes/` | every round-22 delivery probe (and the round-21 probes it carried) re-pointed at the worktree; `test_w23_flags_of_r22.out` is an engineer-written supplement (the flags of the detectors that pass both runners) |
| `after-live5-summary.txt` | the five live runs (compliance, verification, clipper-network, finance, legal) against the fresh release ledger |
| `suites/` | full suites of every touched service, Python 3.13.13 and 3.12.3, with the three non-delivery failures' output |
| `ab-suite-failures-head-vs-540a64e-py31*.txt` | the three non-delivery failures A/B'd, HEAD vs 540a64e, same box, alternating: present on 540a64e at the same or a higher rate |

Cleanup: the delivery suites leave four `dlv-git-*` dirs and one `go-build*` dir in `/tmp` per run (the wave-22
residual) and the ledger integration tests leave `ledger_test_auth_*.jsonl` files there; those this wave's runs
created were removed by hand after the runs (listed by creation time; nothing older was touched).
