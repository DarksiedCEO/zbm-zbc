# core-spec/2 test lanes (G-2), PF-1 / PF-3 tool, G-6 mutant list

The lanes are defined in ONE place: `tests/conformance/lanes_core2.py` (`CI`, `HEAVY`). The conformance
`conftest.py` puts the pytest marker `core2_ci` on every test of a `CI` file. No CI workflow file is changed by
this definition (`.github/workflows/**` is protected); a workflow that wants the lane calls the command below.

## CI lane (every push; G-2 budget <= 10 min on a quiet box)

    cd services/seo-py
    python tests/conformance/tools/ci_lane.py ci          # prints {"wall_s", "rc", "summary"}; exit code = pytest's

Equivalent raw command: `python -m pytest -q -p no:cacheprovider -m core2_ci <every path of lanes_core2.CI>`.
Heavy tests in those files skip (no `CONFORMANCE_HEAVY`).

Two workers (C2-G2b/C2-G2c, the default): `ci_lane.py ci` splits the CI files into N static sets (`--workers N`,
default 2) by a longest-first greedy split on `MEASURED_S` (per-file seconds measured inside ONE conformance
session with `--durations=0`, C2-G2c; files not listed count 3 s). Each set holds at least one `tests/conformance/`
file, because that directory's conftest.py puts the `core2_ci` marker on the CI tests (a lone `tests/test_*.py`
process never loads it and would deselect them). Each set is ONE pytest process with the same arguments
(`-q -rfE -p no:cacheprovider -m core2_ci <set>`), all sets at once. The sets' counts are summed into one `summary`
(same tests, same assertions, same counts as one process: 2654 passed / 12 skipped); `per_file_s` lists each set's
file count and wall time; exit code = the worst set's. `--workers 1` = the old single pytest process. No dependency
added (pytest-xdist is not in the venv).

| Group | Spec | What the CI lane runs |
|---|---|---|
| T | T-1..T-6 | sink field table, typed log / error / outbound sinks, T-4 static + dynamic (recorder), T-2 withhold, T-5 smuggling, T-6 canary over every route |
| V | V-1..V-5 | vault, scope, v1 proof, v2 refusal (`VAULT_UNAVAILABLE`), wiring |
| CV-10 | CV-10a/b/c/d/e, CV-8 | VM: >= 10 cells per (entry, SP, SEP) stratum (seed 20261114) + ALL of CV-10b; FS 20% (vi % 5 == 0); CV-10d CI; CV-10e; CV-8 sample; shape / exempt / monotonic units |
| BL | BL-2, BL-3 | BL-3 CI sample; BL-2 CI sample plus (F16b-2) the full BL-2 rows of every L-4 script entry: EVERY SEP-3b verb x placement x NF x 3 seeds |
| AL | AL-1, AL-2 | allowlist units, merged spans, 2 tenants x 1,000 e2e |
| RV | RV-1..RV-4 | audited reveal |
| LP | LP-1 sample, TI-1 | the CV-10a CI subset (10 per stratum, same seed and shards) |

## Heavy lane (lead gate; sharded)

    for i in 0 1; do CONFORMANCE_HEAVY=1 CONFORMANCE_SHARD=$i/2 python tests/conformance/tools/ci_lane.py heavy; done

Full CV-10a (VM) + CV-10b, full FS (CV-10c) + CV-10d full, CV-8 full, LP-1 full + TI, V-6 upgrade, and the SP matrix
(`test_core_sp_report.py`: report job, no pass/fail). The BL full matrix (14,487,390 cells) is the lead's job
script (`core-v2/blfull/bl_seed.py`, 3 seeds), not a pytest file.

## PF (tools/pf.py)

- `pf1`: two runs in opposite orders over 14 languages x 2 lanes. Third-run rule (`pf1_verdict`): both runs over
  +10% -> fail; both within -> pass; the runs disagree -> a third run (build first) is made for that cell and decides.
  Output: `fail`, `decided_by_third`, `fail_both_runs`. Unit-tested by `test_core_PF_1_third_run_rule`.
- `pf3`: every shape of `perf_shapes.SHAPES`, 2k..64k, both lanes. core-spec/2 adds three CV-4 shapes:
  `cv4_one_char_fragments` (1-char fragments joined by single J characters), `cv4_fragment_word_alternating`
  (fragment / word pieces alternating), `cv4_24char_fragments` (24-char fragments joined to 64k).

## G-6 mutants (tools/mutants_core2.py)

33 mutants (the 8 named in G-6, J minus each of its 11 characters, plus CV-3/CV-4, AL, RV, BL and T-5/T-2 withhold
edits). `--check` verifies every edit applies exactly once; a run copies the service to a temp dir per mutant and
runs the CI lane (mutant's group first, `-x`). Every behavioural mutant must be KILLED; a survivor must be shown
output-identical on VM, FS and the CV-8 corpora.
