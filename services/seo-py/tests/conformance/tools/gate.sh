#!/bin/bash
# core-spec/1 lead gate (SUITE_PLAN section 3), run from services/seo-py. Counts-only output. Steps by spec item:
#   G-1  full suite twice, identical pass counts:   python -m pytest -q -p no:cacheprovider        (x2, compare)
#   G-2  conformance CI lane:                        python -m pytest -q -p no:cacheprovider -m conformance tests/conformance
#        heavy lane (sharded, in the gate's venv with wordfreq==3.1.1 and pythainlp==5.4.0):
#                                                    CONFORMANCE_HEAVY=1 CONFORMANCE_SHARD=$i/$N python -m pytest -q \
#                                                        -p no:cacheprovider -m conformance_heavy tests/conformance
#   G-3  ruff 0.15.11, 0 new findings vs 7c254074; no non-ASCII added line:
#                                                    ruff check src tests ; git diff 7c254074 -- . | grep -P '^\+.*[^\x00-\x7F]'
#   G-4  gitleaks 8.30.1 (linux x64 tarball sha256 551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb)
#        on the range and the tree:                  gitleaks git --log-opts "$BASE..HEAD" ; gitleaks dir <changed files>
#   G-5  war room in a tar copy:                     python devtools/warroom/run.py --department seo-py --seed 1..5,
#                                                    replays R0001-R0015
#   G-6  mutants over the changed code (every behavioural mutant killed; P-6 mutants by test_core_protection_pins)
#   G-7  test_registry.py (the suite only grows; changed assertions listed in the ADR's round entry)
#   G-8  ADR 0051 true by measurement (test_core_residuals.py::test_core_G_8_adr_corrections)
#   G-9  scope:                                      git diff --stat $BASE HEAD (secret_guard.py, tests, ADR 0050/0051)
set -euo pipefail
N=${N:-2}
python -m pytest -q -p no:cacheprovider -m conformance tests/conformance
for i in $(seq 0 $((N - 1))); do
  CONFORMANCE_HEAVY=1 CONFORMANCE_SHARD=$i/$N python -m pytest -q -p no:cacheprovider -m conformance_heavy tests/conformance
done
