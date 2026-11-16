"""core-spec/2 CV-10c: the frozen split-set generator FS (FROZEN; a change is a spec change).

Candidates: the VG values of the three seeds (PER_SEED = 2,000 each, 6,000 in all) x k in {2, 3, 4} pieces x the 11
separator characters of J (CV-4) x 4 placements = 792,000 cells. Per value one seeded draw picks the separator mode:
  uniform  every one of the k - 1 separators is the cell's J character
  mixed    the first separator is the cell's J character, the others are drawn uniformly from J
Per (value, k) the k - 1 cut points are drawn once (distinct, uniform over 1..len-1) and shared by the 11 separators.
Placements: bare (the run alone) / after SEP-1 / after SEP-2 (an ordinary lead word + a seeded SEP-1 / SEP-2 form of
core_matrix.SEPS) / mid-sentence (two ordinary words, the run, two ordinary words). The lead is an ordinary word
(CV-10c names no label; consumers may re-render ``lead`` + ``sep_form`` + run with any label).
Rejection sampling (``select`` / ``accepted``) keeps exactly the cells where the run splits on J into the intended
k pieces with single J characters between them, every piece is a FRAGMENT and the joined value is VALUEISH. The
two predicates are INJECTED (the CV-3 / CV-4 rule lives in the CV module, card C2-7; no copy here). Rejected cells
= R-split. ``MANIFEST`` pins (N, sha256) of the predicate-independent candidate set."""
import hashlib
import json
import random
import re

from conformance import gen_values_v2 as VG
from conformance.core_matrix import SEPS
from conformance.corpora import EN_WORDS

J = (" ", " ", "\t", "-", "_", ".", "/", ":", "+", "|", ",")
KS = (2, 3, 4)
N_VALUES = 6_000
PER_SEED = N_VALUES // len(VG.SEEDS)
PLACEMENTS = ("bare", "sep1", "sep2", "sentence")
MODES = ("uniform", "mixed")
N_CANDIDATES = N_VALUES * len(KS) * len(J) * len(PLACEMENTS)
_JRX = re.compile("([" + re.escape("".join(J)) + "]+)")


def _rng(seed: int) -> random.Random:
    return random.Random(f"core-spec/2:FS:{seed}")


def _render(r: random.Random, placement: str, run: str) -> tuple:
    """(text, span start, lead, sep_id, sep_form)."""
    if placement == "bare":
        return run, 0, "", "bare", ""
    if placement in ("sep1", "sep2"):
        sid, form = r.choice(SEPS[placement[-1]])
        lead = r.choice(EN_WORDS)
        pre = lead + form
        return pre + run, len(pre), lead, sid, form
    pre = f"{r.choice(EN_WORDS)} {r.choice(EN_WORDS)} "
    return pre + run + f" {r.choice(EN_WORDS)} {r.choice(EN_WORDS)}", len(pre), "", "sentence", " "


def candidates(seeds: tuple = VG.SEEDS):
    """Every candidate cell of ``seeds`` in a fixed order (a generator; values only in memory)."""
    for seed in seeds:
        r = _rng(seed)
        for vi, v in enumerate(VG.values(seed, PER_SEED)):
            mode = r.choice(MODES)
            for k in KS:
                cuts = sorted(r.sample(range(1, len(v)), k - 1))
                pieces = [v[a:b] for a, b in zip([0] + cuts, cuts + [len(v)])]
                for si, s in enumerate(J):
                    seps = [s] + ([r.choice(J) for _ in range(k - 2)] if mode == "mixed" else [s] * (k - 2))
                    run = pieces[0] + "".join(x + p for x, p in zip(seps, pieces[1:]))
                    for pl in PLACEMENTS:
                        text, st, lead, sid, form = _render(r, pl, run)
                        yield {"id": f"{seed}:{vi}:{k}:{si}:{pl}", "seed": seed, "vi": vi, "k": k, "sep": si,
                               "mode": mode, "placement": pl, "lead": lead, "sep_id": sid, "sep_form": form,
                               "text": text, "span": [st, st + len(run)], "pieces": pieces, "seps": seps}


def structural(c: dict) -> bool:
    """The run splits on J into exactly the intended pieces with ONE J character between adjacent pieces (a value
    character in J would add a piece or a second separator character)."""
    run = c["text"][c["span"][0]:c["span"][1]]
    parts = _JRX.split(run)
    return parts[0::2] == c["pieces"] and parts[1::2] == c["seps"] and all(len(x) == 1 for x in parts[1::2])


def keep(c: dict, fragment, valueish) -> tuple:
    """(kept, structural ok)."""
    if not structural(c):
        return False, False
    return all(fragment(p) for p in c["pieces"]) and bool(valueish("".join(c["pieces"]))), True


def accepted(fragment, valueish, seeds: tuple = VG.SEEDS):
    """The FS cells (CV-10c) under the injected predicates."""
    for c in candidates(seeds):
        if keep(c, fragment, valueish)[0]:
            yield c


def digest(cells) -> tuple:
    """(N, sha256) over the cells as canonical JSON lines."""
    h, n = hashlib.sha256(), 0
    for c in cells:
        h.update(json.dumps(c, ensure_ascii=True, sort_keys=True).encode("ascii") + b"\n")
        n += 1
    return n, h.hexdigest()


def select(fragment, valueish, seeds: tuple = VG.SEEDS) -> dict:
    """Counts of one rejection-sampling pass: candidates, accepted (FS), rejected (R-split; of them structural =
    a separator next to a J character of the value), and the sha256 of the accepted set."""
    h = hashlib.sha256()
    n = acc = rs = 0
    for c in candidates(seeds):
        n += 1
        ok, st = keep(c, fragment, valueish)
        rs += not st
        if ok:
            acc += 1
            h.update(json.dumps(c, ensure_ascii=True, sort_keys=True).encode("ascii") + b"\n")
    return {"candidates": n, "accepted": acc, "rejected": n - acc, "rejected_structural": rs,
            "sha256_accepted": h.hexdigest()}


def manifest() -> dict:
    """(N, sha256) of the predicate-independent candidate set, per seed and in all."""
    out = {}
    for s in VG.SEEDS:
        n, sha = digest(candidates((s,)))
        out[f"candidates:{s}"] = {"n": n, "sha256": sha}
    n, sha = digest(candidates())
    out["candidates"] = {"n": n, "sha256": sha}
    return out


MANIFEST = {
    "candidates:20261114": {"n": 264000, "sha256": "efa0ec2df7f7366571e58cce277acfe44609e05c877ae0c51f00d410f6e254f4"},
    "candidates:51": {"n": 264000, "sha256": "99371262851c22fe417265d1655ba689fbd65a98d941b03055e6b5e4477432d0"},
    "candidates:7": {"n": 264000, "sha256": "fd304f61354964808e1d6ed139f14e61c3550114060eaa901d197d178c1dd77b"},
    "candidates": {"n": 792000, "sha256": "a2361d5f8a786aad0c36ca2f268c26cb29ec131d6e0caa0fe3f2bf67a4ff859a"},
}
