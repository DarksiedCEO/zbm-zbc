"""core-spec/1 PF-1 / PF-2 / PF-3 under the spec's protocol (the gate runs it; counts and ratios only):

    python tests/conformance/tools/pf.py pf1 [passes]     both modules (ffd28ca8, the build) in ONE process, timed
                                                          alternately, best of >= 7 passes, two runs in opposite orders
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


def pf1(passes: int = 7) -> dict:
    ffd = RL.load_guard(RL.export("ffd28ca8"), "ffd_secret_guard")
    out = {}
    for order in ("build_first", "ffd_first"):
        mods = (build, ffd) if order == "build_first" else (ffd, build)
        for lang in PF1_LANGS:
            texts = sentences(lang)
            for lane in ("strict", "cv"):
                best = {id(m): float("inf") for m in mods}
                for _ in range(passes):
                    for m in mods:
                        best[id(m)] = min(best[id(m)], _pass(m, lane, texts))
                out.setdefault(f"{lang}|{lane}", {})[order] = round(best[id(build)] / best[id(ffd)] - 1, 4)
    fails = [k for k, v in out.items() if all(x > 0.10 for x in v.values())]
    return {"cells": out, "fail_both_runs": fails}


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
