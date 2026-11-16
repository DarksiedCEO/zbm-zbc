"""core-spec/2 CV-8 (O cap, client-view mask rate) measurement tool. Counts and shape classes only: no corpus text is
ever printed or written.

CV-8 hit = a text with >= 1 span masked by CV-3 / CV-4 (``value_shape`` in ``secret_guard.client_view_findings``)
that 3120f0b0's client view did not mask (its ``client_view_findings`` == []). 3120f0b0's secret_guard is read with
``git show`` into a temporary directory and imported under its own name (never edited, never on the repo path).
Judged: (a) O-4 N neutral corpus <= 2.0%; (b) word classes plain_text / question / local / multilingual of rv-priya18
``ord.jsonl`` and rv-priya19 ``oq19.jsonl`` <= 2.0% each. Disclosed: (c) every other class + masked tokens per 1,000
tokens; (d) the same with the corpus's own codes (the exact input text of every CV-3 / CV-4 span of its code-class
texts, capped at AL-1 CAP) loaded as one tenant's AL-1 catalog: reported.

Corpora are frozen files outside the repo, pinned by md5; their paths come from CV8_N / CV8_ORD / CV8_OQ19.
CLI: python -m conformance.cv8_tool [--sample N] [--out result.json]   (cwd tests/, sys.path incl. ../src)
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CAP_PCT = 2.0                                            # Q2-2 [OWNER DEFAULT]
RELEASE = "3120f0b0"
SG_PATH = "services/seo-py/src/agents/secret_guard.py"
WORD_CLASSES = ("plain_text", "question", "local", "multilingual")
CORPORA = {                                              # name: (env var, md5, line count)
    "N": ("CV8_N", "a36b8282650fdecf5ecda84031ce58d8", 45_600),
    "ord18": ("CV8_ORD", "e48a1ff64b78e36dd8257843fa294949", 22_600),
    "oq19": ("CV8_OQ19", "3aff5e0ae64ff802db953cca7312a66f", 24_400),
}
REPO = Path(__file__).resolve().parents[4]


def corpus_paths() -> dict | None:
    """{name: Path} when every corpus env var is set and its file matches the frozen md5; None when one is unset."""
    out = {}
    for name, (env, md5, _n) in CORPORA.items():
        p = os.environ.get(env)
        if not p:
            return None
        p = Path(p)
        got = hashlib.md5(p.read_bytes()).hexdigest()
        if got != md5:
            raise AssertionError(f"corpus {name} drifted from its frozen md5 (a spec change)")
        out[name] = p
    return out


# CI lane (lead ruling 21:05 PT): a FIXED 1,000-line sample of each frozen corpus, committed under data/cv8/ so CV-8 runs
# on a clean checkout. Deterministic draw = ``sample(load(full), 1000)`` (every k-th line, k = len // 1000, first 1,000),
# raw lines copied byte for byte; md5-pinned here. The full corpora stay the lead-gate row (C2-G5, CV8_* env).
SAMPLE_DIR = Path(__file__).resolve().parent / "data" / "cv8"
SAMPLES = {                                              # name: (file, md5, line count)
    "N": ("N.sample1000.jsonl", "64e57e82ce1bec036b716f0b5bbd17b5", 1_000),
    "ord18": ("ord18.sample1000.jsonl", "4e6a3bff93a99f310fb6fc6c1cf731cf", 1_000),
    "oq19": ("oq19.sample1000.jsonl", "b267699a5c4f7f6c22d45cf23a2dfeb6", 1_000),
}


def sample_paths() -> dict:
    """{name: Path} of the committed samples; a missing or drifted file fails (never a skip)."""
    out = {}
    for name, (fn, md5, _n) in SAMPLES.items():
        p = SAMPLE_DIR / fn
        assert p.is_file(), f"CV-8 sample {name} missing: {fn}"
        if hashlib.md5(p.read_bytes()).hexdigest() != md5:
            raise AssertionError(f"CV-8 sample {name} drifted from its frozen md5 (a spec change)")
        out[name] = p
    return out


def load(name: str, path: Path, size: int | None = None) -> list:
    """[(class, text)] in file order. N lines are strings (class "N"); query lines are {"c": class, "q": text}."""
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            items.append(("N", o) if isinstance(o, str) else (o["c"], o["q"]))
    assert len(items) == (size or CORPORA[name][2]), f"corpus {name}: unexpected size"
    return items


def sample(items: list, n: int | None) -> list:
    """Fixed sample: every k-th item (k = len // n), the first n of them. None = all."""
    if not n or n >= len(items):
        return items
    k = len(items) // n
    return items[::k][:n]


def old_cv(tmp: str):
    """3120f0b0's secret_guard as module ``cv8_sg_3120f0b0`` (read via git show; the host rule is the one the
    current sink_guard registers, as 3120f0b0's lazy ``import sink_guard`` cannot reach a renamed module)."""
    src = subprocess.run(["git", "-C", str(REPO), "show", f"{RELEASE}:{SG_PATH}"], check=True,
                         capture_output=True).stdout
    p = Path(tmp) / "cv8_sg_3120f0b0.py"
    p.write_bytes(src)
    spec = importlib.util.spec_from_file_location("cv8_sg_3120f0b0", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cv8_sg_3120f0b0"] = mod
    spec.loader.exec_module(mod)
    import sink_guard  # noqa: F401 - registers the host rule on the current secret_guard
    from agents import secret_guard as sg
    mod.HOST_RULE, mod.HOST_LIKE = sg.HOST_RULE, sg.HOST_LIKE
    return mod, hashlib.md5(src).hexdigest()


def shape(s: str) -> str:
    """Shape class of a masked span: L / D / S present (letter, digit, other) + length band. No value."""
    cls = ("L" if any(c.isalpha() for c in s) else "") + ("D" if any(c.isdigit() for c in s) else "") + (
        "S" if any(not c.isalnum() and not c.isspace() for c in s) else "")
    n = len(s)
    band = "<8" if n < 8 else "8-15" if n < 16 else "16-31" if n < 32 else "32+"
    return f"{cls or '-'} {band}"


def _masked_tokens(text: str, spans: list) -> int:
    n, pos = 0, 0
    for tok in text.split():
        a = text.index(tok, pos)
        b = pos = a + len(tok)
        n += any(s < b and a < e for s, e in spans)
    return n


def _new_masks(sg, t: str) -> bool:
    return "value_shape" in sg.client_view_findings(t)


def _catalog_store(tokens: list):
    """One tenant's AL-1 store holding ``tokens`` (test-fixture coder: ephemeral vault key)."""
    import test_cv_allowlist as T
    from agents import cv_allowlist as al
    coder = T.vault.Coder(T.vault.ephemeral_vault_key(), T.TA)
    store = al.CatalogAllowlist(lambda _t: coder, lambda _k, _r: None)
    import sink_guard   # C2-6h (ruling 02:15 PT F6f3-1): the tenant exists (registered) before its audit row is written
    was = T.TA in sink_guard._REGISTERED
    sink_guard.register_tenant(T.TA)
    try:
        store.replace(T.TA, tokens[:al.CAP], actor="seo", source="shopify", integration_tenant=T.TA)
    finally:
        if not was:
            sink_guard._REGISTERED.discard(T.TA)
    return store, T.TA


def measure(items: list, old, with_catalog: bool = True) -> dict:
    """Per class counts for one corpus (list of (class, text))."""
    from agents import cv_allowlist as al
    from agents import cv_shape
    from agents import secret_guard as sg
    per: dict = {}
    hits = []                                            # (index, spans) of CV-8 hits, for the catalog pass
    catalog: dict = {}
    for i, (c, t) in enumerate(items):
        r = per.setdefault(c, {"texts": 0, "hit": 0, "also_old": 0, "old_masked": 0, "tokens": 0,
                               "masked_tokens": 0, "shapes": {}})
        r["texts"] += 1
        r["tokens"] += len(t.split())
        o = bool(old.client_view_findings(t))
        r["old_masked"] += o
        if not _new_masks(sg, t):
            continue
        spans = cv_shape.spans_and_urls(t)[0]
        r["masked_tokens"] += _masked_tokens(t, spans)
        if o:
            r["also_old"] += 1
            continue
        r["hit"] += 1
        hits.append(i)
        for s, e in spans:
            k = shape(t[s:e])
            r["shapes"][k] = r["shapes"].get(k, 0) + 1
            if c != "N" and c not in WORD_CLASSES:
                catalog.setdefault(t[s:e], None)
    if with_catalog:
        for r in per.values():
            r["hit_catalog"] = 0
        if catalog:
            store, tid = _catalog_store(list(catalog))
            with al.scope(store, tid):
                for i in hits:
                    c, t = items[i]
                    per[c]["hit_catalog"] += _new_masks(sg, t)
        else:
            for i in hits:
                per[items[i][0]]["hit_catalog"] += 1
    for r in per.values():
        r["pct"] = round(100.0 * r["hit"] / r["texts"], 3)
        r["per_1000_tokens"] = round(1000.0 * r["masked_tokens"] / max(r["tokens"], 1), 3)
        r["shapes"] = dict(sorted(r["shapes"].items(), key=lambda kv: -kv[1])[:8])
    return {"classes": per, "catalog_tokens": len(catalog)}


def judged(result: dict) -> dict:
    """{row: pct} of the judged rows: (a) N, (b) each word class of each query corpus."""
    out = {}
    for name, res in result["corpora"].items():
        for c, r in res["classes"].items():
            if c == "N" or c in WORD_CLASSES:
                out[f"{name}/{c}"] = r["pct"]
    return out


def run(n: int | None, samples: bool = False) -> dict:
    """``samples`` = the committed data/cv8 samples (n ignored: each is already the fixed 1,000-line draw)."""
    paths = sample_paths() if samples else corpus_paths()
    if paths is None:
        raise SystemExit("set CV8_N, CV8_ORD, CV8_OQ19 to the frozen corpora")
    with tempfile.TemporaryDirectory(prefix="cv8_") as tmp:
        old, md5 = old_cv(tmp)
        res = {"release": RELEASE, "release_sg_md5": md5, "sample": "data/cv8" if samples else n,
               "cap_pct": CAP_PCT, "corpora": {}}
        for name, p in paths.items():
            items = load(name, p, SAMPLES[name][2]) if samples else sample(load(name, p), n)
            res["corpora"][name] = measure(items, old)
    res["judged"] = judged(res)
    res["over_cap"] = {k: v for k, v in res["judged"].items() if v > CAP_PCT}
    return res


def table(res: dict) -> str:
    rows = [f"CV-8 sample={res['sample']} release={res['release']} cap={res['cap_pct']}%",
            "corpus/class | texts | hit | pct | also_old | old_masked | hit_with_AL1 | masked_tok/1000 | top shapes"]
    for name, cr in res["corpora"].items():
        for c in sorted(cr["classes"], key=lambda x: (x not in WORD_CLASSES and x != "N", x)):
            r = cr["classes"][c]
            tag = " [judged]" if c == "N" or c in WORD_CLASSES else ""
            top = ", ".join(f"{k}:{v}" for k, v in list(r["shapes"].items())[:3])
            rows.append(f"{name}/{c}{tag} | {r['texts']} | {r['hit']} | {r['pct']}% | {r['also_old']} | "
                        f"{r['old_masked']} | {r.get('hit_catalog', '-')} | {r['per_1000_tokens']} | {top}")
        rows.append(f"{name}: AL-1 catalog tokens loaded = {cr['catalog_tokens']}")
    rows.append(f"over cap: {res['over_cap'] or 'none'}")
    return "\n".join(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    res = run(a.sample)
    print(table(res))
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1, sort_keys=True), encoding="ascii")
    return 1 if res["over_cap"] else 0


if __name__ == "__main__":
    sys.exit(main())
