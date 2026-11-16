"""core-spec/2 LP-1 (lost protection) and TI-1 (tenant isolation), card C2-18.

LP-1: every VM cell (vm_cells: rv-sink8 E1 / E1b / E1c, rv-sink9 p9) with its ORIGINAL label and its value replaced by a
VG value (CV-9). A reference release (3120f0b0 / ffd28ca8 / cbdf5e78) masked the value when its client view had a
finding on the cell text (``client_view_findings`` != []: callum masked the whole field). The build masks it when the
owner view (``callum._display``) hides the field whole, or its CV-5 spans cover every code point of the value's CV-2
core. lost = masked by at least one reference, not masked at the build: must be 0. The same cells with a prose value
(CV-9 prose pool) are measured the same way and REPORTED (R-prose count), never judged here (BL-3 judges BL scope).
CI lane (G-2): the CV-10a CI subset of test_core_vm (10 cells per stratum, seed 20261114), same shards. Heavy lane:
CONFORMANCE_HEAVY=1 CONFORMANCE_SHARD=i/n, every cell. Never prints a label or a value: counts and cell ids only.

TI-1: one service, two tenants (acme / globex), allowlist ON and reveal ON, end to end: own runs vaulted per tenant,
synthetic tokens UNIQUE per tenant (C2-18b, ruling 15:25 PT: a token both tenants hold is revealable by both, a false
red), allowlist sync per tenant, owner run views over HTTP, reveal attempts across tenants with the other tenant's span
codes computed as its owner view lists them (``svc._owner_coding(tid)``, C2-14d). Measured: the other tenant's values
visible anywhere (B's responses, B's scope) = 0; cross-tenant reveals succeeded = 0. V-3 identical tokens across
tenants: tests/test_reveal.py (C2-14d). Assertion messages carry counts only, never a token."""
from __future__ import annotations

import collections
import json
import os
import random
import string
import unicodedata as u

import pytest

from conformance import releases
from conformance import vm_cells as V

REFS = ("3120f0b0", "ffd28ca8", "cbdf5e78")
CI_PER_STRATUM, CI_SEED = 10, 20261114
CI_SHARDS = {"e1": 24, "e1b": 1, "e1c": 2, "p9": 6}
_SUBSETS: dict = {}
_GUARDS: dict = {}


def _stratum(corpus: str, meta: list) -> tuple:
    # identical to test_core_vm._stratum (G-2 stratum; lead ruling 21:50 PT F12b-2)
    if corpus == "p9":
        return V.group_key(corpus, meta)
    return (corpus, meta[0], meta[1].split("|")[0], V.E_SEP_CLASS[int(meta[2])])


def _subset(corpus: str) -> frozenset:
    """The CV-10a CI subset (same seed / sampling as test_core_vm._subset)."""
    if corpus not in _SUBSETS:
        strata: dict = collections.defaultdict(list)
        for i, meta, parts in V.cells(corpus):
            if parts is not None:
                strata[_stratum(corpus, meta)].append(i)
        r = random.Random(CI_SEED)
        keep = set()
        for k in sorted(strata):
            ids = strata[k]
            keep.update(ids if len(ids) <= CI_PER_STRATUM else r.sample(ids, CI_PER_STRATUM))
        _SUBSETS[corpus] = frozenset(keep)
    return _SUBSETS[corpus]


def _guards() -> dict:
    """Each reference release's secret_guard (git archive, read-only), host rule as the current sink_guard registers
    it (as cv8_tool.old_cv: the releases' lazy ``import sink_guard`` cannot reach the build's renamed module)."""
    if not _GUARDS:
        import sink_guard  # noqa: F401 - registers the host rule on the current secret_guard
        from agents import secret_guard as sg
        for sha in REFS:
            tree = releases.export(sha)
            if tree is None:
                return {}
            mod = releases.load_guard(tree, f"lp1_sg_{sha}")
            mod.HOST_RULE, mod.HOST_LIKE = sg.HOST_RULE, sg.HOST_LIKE
            _GUARDS[sha] = mod
    return _GUARDS


def _build_masked(text: str, a: int, b: int) -> bool:
    """The owner view hides text[a:b] (the value core): whole-field mask, or every point under a CV-5 span."""
    from agents import callum
    d = callum._display("excerpt", text)
    if not d["alert"]:
        return False
    if "spans" not in d:
        return True
    return all(any(s["start"] <= k < s["end"] for s in d["spans"]) for k in range(a, b))


def lp_run(corpus: str, select=None, shard=(0, 1)) -> dict:
    from conformance import gen_values_v2 as VG
    refs = _guards()
    vals = {s: VG.values(s, 10_000) for s in VG.SEEDS}
    words = VG.prose_pool()
    out = {"corpus": corpus, "evaluated": 0, "ref_masked": dict.fromkeys(REFS, 0), "ref_any": 0, "lost": 0,
           "lost_ids": [], "prose_ref_any": 0, "prose_lost": 0, "prose_lost_by_sep": {}}
    for i, meta, parts in V.cells(corpus):
        if i % shard[1] != shard[0] or parts is None or (select is not None and not select(i, meta)):
            continue
        pre, label, sep, _v, suf, nf = parts
        seed = VG.SEEDS[i % 3]
        val = vals[seed][(i // 3) % len(vals[seed])]
        word = words[random.Random(f"lp:{corpus}:{i}").randrange(len(words))]
        head, tail = u.normalize(nf, pre + label + sep), u.normalize(nf, suf)
        out["evaluated"] += 1
        for kind, v in (("vg", val), ("prose", word)):
            t = head + v + tail
            a0, b0 = _core(v)
            hit = [sha for sha in REFS if refs[sha].client_view_findings(t)]
            if not hit:
                continue
            lost = not _build_masked(t, len(head) + a0, len(head) + b0)
            if kind == "vg":
                out["ref_any"] += 1
                for sha in hit:
                    out["ref_masked"][sha] += 1
                if lost:
                    out["lost"] += 1
                    if len(out["lost_ids"]) < 50:
                        out["lost_ids"].append([i] + meta[:4] + [hit])
            else:
                out["prose_ref_any"] += 1
                if lost:
                    out["prose_lost"] += 1
                    sid = _stratum(corpus, meta)[-1]
                    out["prose_lost_by_sep"][sid] = out["prose_lost_by_sep"].get(sid, 0) + 1
    return out


def _core(v: str) -> tuple:
    from agents import cv_shape
    return cv_shape.core_bounds(v)


def _need():
    if not V.available():
        pytest.skip("VM corpora not present (RV_SINK8_WORK / RV_SINK9_WORK)")
    if not _guards():
        pytest.skip("reference releases not in the git history (shallow checkout)")


def _evidence(name: str, res: dict) -> None:
    d = os.environ.get("LP_EVIDENCE_DIR")
    if d:
        with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(res, f)


def _check(res: dict) -> None:
    print(res["corpus"], "evaluated", res["evaluated"], "ref_masked", res["ref_masked"], "ref_any", res["ref_any"],
          "lost", res["lost"], "R-prose: prose_ref_any", res["prose_ref_any"], "prose_lost", res["prose_lost"],
          res["prose_lost_by_sep"])
    assert res["evaluated"] > 0
    assert res["lost"] == 0, (res["corpus"], "lost protection", res["lost"], res["lost_ids"][:10])


@pytest.mark.conformance
@pytest.mark.parametrize(("corpus", "shard"), [(c, k) for c, n in CI_SHARDS.items() for k in range(n)])
def test_lp1_ci_subset_no_lost_protection_vs_reference_releases(corpus, shard):
    _need()
    sub = _subset(corpus)
    res = lp_run(corpus, select=lambda i, _m: i in sub, shard=(shard, CI_SHARDS[corpus]))
    _evidence(f"lp_ci_{corpus}_{shard}", res)
    _check(res)


@pytest.mark.conformance_heavy
@pytest.mark.parametrize("corpus", list(V.SIZES))
def test_lp1_full_no_lost_protection_vs_reference_releases(corpus):
    _need()
    i, n = (int(x) for x in os.environ.get("CONFORMANCE_SHARD", "0/1").split("/"))
    res = lp_run(corpus, shard=(i, n))
    _evidence(f"lp_full_{corpus}_{i}_{n}", res)
    _check(res)


# ---- TI-1: 2-tenant end to end (codes V-3, vault V-4, allowlist AL-2, reveal RV-4); synthetic tokens only ----
TA, TB = "acme", "globex"                                 # tenants with hub tenant tokens (helpers.TENANT_TOKENS)
_CODE = __import__("re").compile(r"\{c:text:[0-9a-f]{16}\}")


def _toks(seed: int, n: int) -> list:
    rnd, out = random.Random(seed), []
    while len(out) < n:
        t = "".join(rnd.choice(string.ascii_uppercase + string.digits) for _ in range(14))
        if any(c.isdigit() for c in t) and any(c.isalpha() for c in t) and t not in out:
            out.append(t)
    return out


def _cid(n: int) -> str:
    return "seo-cit-" + f"{n:040x}"


def _plant(svc, tid: str, cid: str, toks: list) -> None:
    """A recorded run of ``tid`` whose vaulted document holds ``toks`` in evidence excerpts (as test_reveal._run)."""
    from svc_citations import RESULT_FIELD
    from svc_vaulted import normalised
    doc = {"v": 1, "result": {"facts": {"rows": []}},
           "evidence": {f"e{i}": {"excerpt": f"ref {t} here"} for i, t in enumerate(toks)}}
    sealed = svc._vaulted_seal(tid, RESULT_FIELD, normalised(doc))
    with svc.lock:
        svc._vaulted_commit(sealed, lambda: None)
        svc.citation_runs[cid] = {"citation_run_id": cid, "tenant_id": tid, "status": "recorded",
                                  "requested_at": "2026-10-09T18:00:00Z", "result": None,
                                  "result_ref": sealed.ref, "result_hmac": sealed.hmac}


def test_ti1_two_tenants_end_to_end_other_tenants_values_visible_0(tmp_path):
    from datetime import timedelta

    from agents import callum
    from errors import NotFound
    from helpers import T0, Harness
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"), SEO_CV_ALLOWLIST="1")
    h.tenant(TA)
    h.tenant(TB)
    h.svc.settings.reveal = True
    clock = [T0]
    h.svc.now = lambda: clock[0]
    # C2-18b: every token belongs to ONE tenant (lists = catalog, allowlisted; priv = masked, carries span codes)
    pool = _toks(1801, 800)
    list_a, priv_a, list_b, priv_b = pool[:200], pool[200:400], pool[400:600], pool[600:]
    _plant(h.svc, TA, _cid(1), list_a + priv_a)
    _plant(h.svc, TB, _cid(2), list_b + priv_b)
    for tid, toks in ((TA, list_a), (TB, list_b)):        # AL-2: each tenant lists its own catalog
        h.svc.cv_allowlist_sync(tid, {"source": "shopify", "integration_tenant": tid, "tokens": toks}, "dashboard")

    # the HTTP run view of a planted run renders `result` only (F18-1), so own-view controls and span codes come from
    # the owner display under the context citation_run_view / the reveal recompute use (C2-14d)
    def _view(tid, t):
        with h.svc._owner_coding(tid), h.svc._cv_scope(tid):
            return callum._display("excerpt", f"ref {t} here")

    def _codes(tid, toks):                                # (token, code) of the span-masked ones (a token whose
        out = []                                          # field is hidden whole carries no span code; it stays in
        for t in toks:                                    # the visibility count below)
            d = _view(tid, t)
            if d["alert"] and "spans" in d:
                out.append((t, d["spans"][0]["code"]))
        return out
    own_shown = sum(not _view(TA, t)["alert"] for t in list_a)
    pairs_a, pairs_b = _codes(TA, priv_a), _codes(TB, priv_b)
    codes_a, codes_b = [c for _t, c in pairs_a], [c for _t, c in pairs_b]
    view_b = h.get(f"/tenants/{TB}/citation-runs/{_cid(2)}", caller="hub", tenant=TB)
    assert view_b.status_code == 200, "own run view status"
    assert own_shown == len(list_a), f"positive control: owner list shown {own_shown} of {len(list_a)}"
    assert len(codes_a) >= 190 and len(codes_b) >= 190, f"span-coded tokens {len(codes_a)} / {len(codes_b)}"
    same = len(set(codes_a) & set(codes_b))
    assert len(set(codes_a)) == len(codes_a) and same == 0, f"span codes: equal across tenants {same}"
    # every response tenant B can obtain: own run + list, A's run / list / reveal with B's token
    b_bodies = [view_b.text, h.get(f"/tenants/{TB}/citation-runs", caller="hub", tenant=TB).text]
    for p in (f"/tenants/{TA}/citation-runs/{_cid(1)}", f"/tenants/{TA}/citation-runs",
              f"/tenants/{TB}/citation-runs/{_cid(1)}"):
        b_bodies.append(h.get(p, caller="hub", tenant=TB).text)
    wins = tries = 0
    for i, c in enumerate(codes_a[:40]):                  # RV-4: B reveals A's codes on A's run, B's run (rate cap kept)
        clock[0] = T0 + timedelta(minutes=30 * i)
        for rid in (_cid(1), _cid(2)):
            tries += 1
            try:
                b_bodies.append(json.dumps(h.svc.reveal_span("hub", TB, "citation_run", rid, c)))
                wins += 1
            except NotFound:
                pass
        r = h.post(f"/tenants/{TB}/reveals", {"surface": "citation_run", "run_id": _cid(1), "span": c},
                   caller="hub", tenant=TB)
        b_bodies.append(r.text)
        tries += 1
        wins += r.status_code == 200
    with h.svc._owner_coding(TB), h.svc._cv_scope(TB):   # AL-2 across tenants: A's list never shows in B's scope
        al_cross = sum(not callum._display("excerpt", f"ref {t} here")["alert"] for t in list_a)
    clock[0] = T0 + timedelta(days=2)                     # positive controls: each owner reveals its own span
    own_a = h.svc.reveal_span("hub", TA, "citation_run", _cid(1), pairs_a[0][1])["value"] == pairs_a[0][0]
    clock[0] = T0 + timedelta(days=4)
    own_b = h.svc.reveal_span("hub", TB, "citation_run", _cid(2), pairs_b[0][1])["value"] == pairs_b[0][0]
    blob = "\n".join(b_bodies)
    visible = sum(t in blob for t in list_a + priv_a)
    print("TI-1 visible_to_other", visible, "cross_reveal_wins", wins, "of", tries, "allowlist_cross_shown", al_cross,
          "codes_a", len(codes_a), "codes_b", len(codes_b), "own_reveal_ok", int(own_a) + int(own_b))
    assert own_a and own_b, f"positive control: own reveals ok {int(own_a) + int(own_b)} of 2"
    assert visible == 0, f"other tenant's values visible: {visible}"
    assert wins == 0, f"cross-tenant reveal wins: {wins} of {tries}"
    assert al_cross == 0, f"other tenant's allowlist shown in B's scope: {al_cross}"
