"""core-spec/1 PF-1 / PF-2 / PF-3 under the spec's protocol (the gate runs it; counts and ratios only):

    python tests/conformance/tools/pf.py pf1 [passes]     both modules (ffd28ca8, the build) in ONE process, timed
                                                          alternately, best of >= 7 passes, two runs in opposite orders;
                                                          third run decides a cell the two disagree on
    python tests/conformance/tools/pf.py pf2 [n]          cold start, >= 24 fresh interleaved processes each
    python tests/conformance/tools/pf.py pf3              17+ shapes 2k..64k, both lanes, best of 3, vs ffd28ca8
"""
import json
import os
import statistics
import subprocess
import sys
import time

TESTS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(os.path.dirname(TESTS), "src")
sys.path.insert(0, TESTS)
sys.path.insert(0, SRC)
from agents import secret_guard as build  # noqa: E402

from conformance import corpora as C  # noqa: E402
from conformance import perf_shapes as P  # noqa: E402
from conformance import releases as RL  # noqa: E402

PF1_LANGS = ("ja", "hi", "th", "ar", "fa", "ru", "el", "pl", "de", "en", "ko", "zh", "he", "uk")


def _real_words(lang: str) -> list:
    """The language's 5,000 most frequent words (wordfreq 3.1.1, in the gate's venv), else the embedded sample."""
    try:
        import wordfreq
    except ImportError:
        return list(C.EN_WORDS if lang == "en" else C.WORDS[lang])
    if lang == "th":
        from pythainlp.corpus import tnc
        return [w for w, _c in sorted(tnc.word_freqs(), key=lambda x: -x[1]) if w.strip() and " " not in w][:5000]
    return [w for w in wordfreq.top_n_list(lang, 5200) if w.strip() and " " not in w][:5000]


def sentences(lang: str, n: int = 1000) -> list:
    import random
    r = random.Random(f"pf1:{lang}")
    words = _real_words(lang)
    joiner = "" if lang in ("ja", "zh", "th") else " "
    return [joiner.join(r.choice(words) for _ in range(r.randint(6, 14))) + "." for _ in range(n)]


def _pass(mod, lane, texts) -> float:
    fn = mod.check if lane == "strict" else mod.client_view_findings
    t = time.perf_counter()
    for x in texts:
        fn(x)
    return time.perf_counter() - t


def _pf1_run(order: str, keys, passes: int, build, ffd) -> dict:
    """One PF-1 run: best-of-``passes`` ratio build / ffd28ca8 - 1 per ``lang|lane`` key, timed alternately."""
    mods = (build, ffd) if order == "build_first" else (ffd, build)
    out = {}
    for key in keys:
        lang, lane = key.split("|")
        texts = sentences(lang)
        best = {id(m): float("inf") for m in mods}
        for _ in range(passes):
            for m in mods:
                best[id(m)] = min(best[id(m)], _pass(m, lane, texts))
        out[key] = round(best[id(build)] / best[id(ffd)] - 1, 4)
    return out


def pf1_verdict(cells: dict, bar: float = 0.10) -> dict:
    """PF-1 third-run rule (core-spec/2 section 7; certification row PF "two runs, third decides"): a cell fails when
    both runs are over ``bar``, passes when both are within it; when the two runs DISAGREE the third run decides.
    ``cells`` = {key: {"build_first": r, "ffd_first": r, ["third": r]}}. Returns fails / need_third / decided_by_third."""
    fails, need, third = [], [], []
    for k, v in sorted(cells.items()):
        over = [v[o] > bar for o in ("build_first", "ffd_first")]
        if all(over):
            fails.append(k)
        elif any(over):
            if "third" not in v:
                need.append(k)
                continue
            third.append(k)
            if v["third"] > bar:
                fails.append(k)
    return {"fails": fails, "need_third": need, "decided_by_third": third}


def pf1(passes: int = 7, build_mod=None, ffd_mod=None) -> dict:
    """Two runs in opposite orders over every cell; a third run (build first) only for the cells they disagree on."""
    b = build_mod or build
    ffd = ffd_mod or RL.load_guard(RL.export("ffd28ca8"), "ffd_secret_guard")
    keys = [f"{lang}|{lane}" for lang in PF1_LANGS for lane in ("strict", "cv")]
    out = {k: {} for k in keys}
    for order in ("build_first", "ffd_first"):
        for k, r in _pf1_run(order, keys, passes, b, ffd).items():
            out[k][order] = r
    need = pf1_verdict(out)["need_third"]
    for k, r in _pf1_run("build_first", need, passes, b, ffd).items():
        out[k]["third"] = r
    v = pf1_verdict(out)
    return {"cells": out, "fail": v["fails"], "decided_by_third": v["decided_by_third"],
            "fail_both_runs": [k for k in v["fails"] if k not in v["decided_by_third"]]}


COLD = ("import sys, time; t = time.perf_counter(); sys.path[:0] = [sys.argv[1] + '/tests', sys.argv[1] + '/src']; "
        "import tempfile; from helpers import Harness; h = Harness(tempfile.mkdtemp()); "
        "from agents import secret_guard as s; s.check('ordinary text'); s.client_view_findings('ordinary text'); "
        "print(time.perf_counter() - t)")


def pf2(n: int = 24) -> dict:
    trees = {"build": os.path.dirname(TESTS), "ffd28ca8": str(RL.export("ffd28ca8"))}
    times = {k: [] for k in trees}
    for _ in range(n):
        for k, t in trees.items():
            p = subprocess.run([sys.executable, "-c", COLD, t], capture_output=True, text=True,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            times[k].append(float(p.stdout.strip().splitlines()[-1]))
    med = {k: statistics.median(v) for k, v in times.items()}
    return {"median_s": med, "ratio": round(med["build"] / med["ffd28ca8"], 4), "n": n}


def pf3() -> dict:
    sizes = (2048, 4096, 8192, 16384, 32768, 65536)
    ffd = RL.load_guard(RL.export("ffd28ca8"), "ffd_secret_guard")
    b, f = P.linearity(build, sizes), P.linearity(ffd, sizes[-1:])
    out, fails = {}, []
    for shape, lanes in b.items():
        for lane, ts in lanes.items():
            dbl = [round(ts[i + 1] / max(ts[i], 1e-6), 2) for i in range(2, len(ts) - 1)]
            rel = round(ts[-1] / max(f[shape][lane][0], 1e-6), 2)
            out[f"{shape}|{lane}"] = {"64k_s": round(ts[-1], 4), "doublings_from_8k": dbl, "vs_ffd_64k": rel}
            if max(dbl) > 2.5 or ts[-1] > 1.0 or (rel > 2.5 and ts[-1] > 0.01):
                fails.append(f"{shape}|{lane}")
    return {"cells": out, "fails": fails}


if __name__ == "__main__":
    what = sys.argv[1]
    arg = int(sys.argv[2]) if len(sys.argv) > 2 else None
    res = {"pf1": lambda: pf1(arg or 7), "pf2": lambda: pf2(arg or 24), "pf3": pf3}[what]()
    print(json.dumps(res, indent=1, sort_keys=True))
