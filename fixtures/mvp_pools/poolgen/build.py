"""Build, verify and summarise the MVP fixture pools.

    python3 -B -m poolgen.build build  --seed dev-a --out <dir>   # writes every subject and answer JSONL file + manifest.json
    python3 -B -m poolgen.build verify                            # regenerates every DEV seed, compares to MANIFEST
    python3 -B -m poolgen.build stats  --seed dev-a               # counts per class, for the spec freeze

Run from fixtures/mvp_pools. Standard library only; no network; writes only under --out.

Seeds: ``seeds/dev.json`` lists the DEV seeds (builders use them freely) and their pinned output hashes. Held-out
seeds are NEVER committed (the repository is public): the reviewer lane draws them privately at certification
(``secrets.token_hex(16)``), records only their SHA-256 commitment in the certification record, and runs this same
generator with them. Because generation is a pure function of the seed, the commitment proves which exam was used
without revealing it in advance.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import (
    call_bank,
    ci_watch,
    common,
    f3_transcripts,
    frontdesk_sets,
    local_ops,
    pool_b,
    review_replies,
    seo_local,
)

HERE = Path(__file__).resolve().parent
SEEDS_FILE = HERE.parent / "seeds" / "dev.json"

# (subject file, answer-key file, generator). Answer-key files belong to the reviewer lane.
SETS = (
    ("pool_b_world.jsonl", "pool_b_answers.jsonl", pool_b.generate),
    ("call_bank_calls.jsonl", "call_bank_answers.jsonl", call_bank.generate),
    ("f2_missed_calls.jsonl", "f2_answers.jsonl", frontdesk_sets.generate_f2),
    ("f4_transfer_cases.jsonl", "f4_answers.jsonl", frontdesk_sets.generate_f4),
    ("f3_transcripts.jsonl", "f3_answers.jsonl", f3_transcripts.generate),
    ("f5_consent_events.jsonl", "f5_answers.jsonl", frontdesk_sets.generate_f5),
    ("seo_l4_citations.jsonl", "seo_l4_answers.jsonl", seo_local.generate_l4),
    ("seo_l5_listing_pairs.jsonl", "seo_l5_answers.jsonl", seo_local.generate_l5),
    ("ci_watch_snapshots.jsonl", "ci_watch_answers.jsonl", ci_watch.generate),
    ("seo_l1_listings.jsonl", "seo_l1_answers.jsonl", local_ops.generate_l1),
    ("seo_l6_review_requests.jsonl", "seo_l6_answers.jsonl", local_ops.generate_l6),
    ("seo_l7_reviews.jsonl", "seo_l7_answers.jsonl", review_replies.generate),
)


def render(seed: str) -> dict[str, str]:
    """Every file as text, in memory. Pure function of the seed."""
    out = {}
    for subject_name, key_name, gen in SETS:
        subject, key = gen(seed)
        out[subject_name] = "".join(common.canonical_json(r) + "\n" for r in subject)
        out[key_name] = "".join(common.canonical_json(r) + "\n" for r in key)
    return out


def digests(seed: str) -> dict[str, str]:
    return {name: common.sha256_text(text) for name, text in render(seed).items()}


def load_seeds() -> dict:
    return json.loads(SEEDS_FILE.read_text(encoding="utf-8"))


def cmd_build(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        print(f"refusing: {out} is not empty", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    files = render(args.seed)
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
    manifest = {"generator": common.GENERATOR_VERSION, "seed_sha256": common.sha256_text(args.seed),
                "files": {n: common.sha256_text(t) for n, t in files.items()},
                "note": "answers files belong to the reviewer lane; agents under test never read them"}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def cmd_verify(_args: argparse.Namespace) -> int:
    seeds = load_seeds()
    bad = 0
    for seed, pinned in seeds["dev_seeds"].items():
        got = digests(seed)
        for name, want in pinned.items():
            ok = got.get(name) == want
            bad += not ok
            print(f"{'OK  ' if ok else 'DIFF'} {seed} {name} {got.get(name)}")
    if seeds.get("generator") != common.GENERATOR_VERSION:
        print(f"DIFF generator version: pinned {seeds.get('generator')} vs {common.GENERATOR_VERSION}")
        bad += 1
    return 1 if bad else 0


def cmd_stats(args: argparse.Namespace) -> int:
    from collections import Counter
    world, bkey = pool_b.generate(args.seed)
    calls, ckey = call_bank.generate(args.seed)
    report = {
        "pool_b": {
            "businesses": len(world), "controls": sum(k["control"] for k in bkey),
            "per_vertical": dict(Counter(b["vertical"] for b in world)),
            "findings_per_class": dict(sorted(Counter(f["class"] for k in bkey for f in k["expected_findings"]).items())),
            "must_refuse": dict(Counter(r["agent"] for k in bkey for r in k["must_refuse"])),
        },
        "call_bank": {
            "calls": len(calls), "per_vertical": dict(Counter(c["vertical"] for c in calls)),
            "spanish": sum(c["language"] == "es" for c in calls),
            "per_scenario": dict(sorted(Counter(k["scenario"] for k in ckey).items())),
            "emergencies": dict(Counter(k["emergency"] for k in ckey if k["emergency"])),
            "routes": dict(sorted(Counter(k["expected_route"] for k in ckey).items())),
        },
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="poolgen.build")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--seed", required=True)
    b.add_argument("--out", required=True)
    sub.add_parser("verify")
    st = sub.add_parser("stats")
    st.add_argument("--seed", required=True)
    a = ap.parse_args(argv)
    return {"build": cmd_build, "verify": cmd_verify, "stats": cmd_stats}[a.cmd](a)


if __name__ == "__main__":
    os.umask(0o022)
    sys.exit(main())
