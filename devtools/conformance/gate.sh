#!/bin/bash
# SUITE_PLAN section 3: the lead gate for the Priya conformance suite (priya-spec/1.1). Counts-only report.
#   devtools/conformance/gate.sh OUTDIR [PYTHON]
# Runs, each into OUTDIR (never into the source tree): the CI lane, the heavy lane in shards, the full suite twice,
# the mutants (devtools/conformance/mutants.py), ruff, and - in a tar copy - the war room seeds; gitleaks when a
# verified binary is given in GITLEAKS (the release tarball's sha256 is checked by the caller, G-4). Every step
# records its exit code; the report holds counts and ids only.
set -u
OUT=${1:?usage: gate.sh OUTDIR [PYTHON]}
PY=${2:-python3}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
SHARDS=${CONFORMANCE_SHARDS:-2}
mkdir -p "$OUT"
REPORT="$OUT/report.txt"
: > "$REPORT"
step() { local name=$1; shift; "$@" > "$OUT/$name.log" 2>&1; local rc=$?; echo "$name rc=$rc $(tail -1 "$OUT/$name.log")" >> "$REPORT"; }
cd "$REPO/services/seo-py" || exit 2
step conformance_ci "$PY" -m pytest -q -p no:cacheprovider -m conformance tests/conformance
for i in $(seq 0 $((SHARDS - 1))); do
  CONFORMANCE_HEAVY=1 CONFORMANCE_SHARD="$i/$SHARDS" step "conformance_heavy_$i" "$PY" -m pytest -q -p no:cacheprovider \
    -m conformance_heavy tests/conformance &
done
wait
# the per-pair (<= 5%) and table checks need one unsharded heavy run of the reviewer corpora
CONFORMANCE_HEAVY=1 step conformance_heavy_tables "$PY" -m pytest -q -p no:cacheprovider -m conformance_heavy \
  tests/conformance/test_priya_reviewer_corpora.py tests/conformance/test_priya_composed.py
step suite_1 "$PY" -m pytest -q -p no:cacheprovider tests
step suite_2 "$PY" -m pytest -q -p no:cacheprovider tests
cd "$REPO" || exit 2
step mutants "$PY" devtools/conformance/mutants.py --jobs 2 --out "$OUT/mutants.json" --python "$PY"
if command -v ruff > /dev/null; then step ruff ruff check --output-format concise services/seo-py; fi
if [ -n "${GITLEAKS:-}" ]; then step gitleaks "$GITLEAKS" detect --no-banner --source "$REPO" --config "$REPO/.gitleaks.toml" \
  --report-path "$OUT/gitleaks.json"; fi
if [ -f devtools/warroom/run.py ]; then
  WR="$OUT/warroom-copy"; rm -rf "$WR"; mkdir -p "$WR"
  tar -C "$REPO" --exclude=./.git -cf - . | tar -C "$WR" -xf -
  (cd "$WR" && for s in 1 2 3 4 5; do "$PY" devtools/warroom/run.py --department seo-py-priya --seed $s --python "$PY" \
     --out "$OUT/wr-priya-$s" > "$OUT/wr-priya-$s.log" 2>&1; echo "warroom seo-py-priya seed $s rc=$?" >> "$REPORT"; done
   "$PY" devtools/warroom/run.py --department seo-py --seed 1 --python "$PY" --out "$OUT/wr-seo-py-1" \
     > "$OUT/wr-seo-py-1.log" 2>&1; echo "warroom seo-py seed 1 rc=$?" >> "$REPORT")
fi
cat "$REPORT"
