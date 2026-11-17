"""core-spec/2 AL-2 / TI-1 end to end through ``citation_run_view`` (card C2-15c; F15b-4, F18-1, F14d-3).

Two tenants (acme / globex), allowlist ON, each with 1,000 synthetic catalog tokens synced through
``cv_allowlist_sync``. Each tenant has TWO recorded runs read by ``citation_run_view``: one recorded before sink job C
(inline result, every row's evidence a per-row vault record: ``vault_id``) and one with a ``result_ref`` vaulted
document (``evidence_id``). Every run holds the owner's 1,000 tokens AND the other tenant's 1,000 tokens (one per row,
in the excerpt). Half the rows carry a recorded ``value_shape`` client-view finding (``excerpt_view``, F15b-4): the
excerpt then goes through ``cv_shape.display`` even when every span is allowlisted.

Measured (counts only): own tokens shown in the owner's view = 1000 per run (and 500 / 500 on the recorded rows); the
other tenant's tokens shown in the owner's view = 0; the other tenant's run read on this tenant's path = 404 (0 shown).
Negative control: the same runs read with the allowlist out of scope (setting OFF) show ~0 own tokens, so the test goes
red when ``svc_citations`` drops ``_cv_scope`` (mutation check, card C2-15c, run on a copy). The HTTP GET body is also
asserted: 1000 owner / 0 other, and the
other tenant's run on this tenant's HTTP path is 404 with 0 shown (F18-1: a run whose rows hold evidence renders the
excerpts in the GET body). Never prints a token: messages carry counts only."""
from __future__ import annotations

import json
import os
import random
import string

TA, TB = "acme", "globex"
N = 1000
BASIS = "excerpt"


def _toks(seed: int, n: int) -> list:
    rnd, out, seen = random.Random(seed), [], set()
    while len(out) < n:
        t = "".join(rnd.choice(string.ascii_uppercase + string.digits) for _ in range(14))
        if any(c.isdigit() for c in t) and any(c.isalpha() for c in t) and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _cid(n: int) -> str:
    return "seo-cit-" + f"{n:040x}"


def _record(tok: str, recorded: bool) -> dict:
    rec = {"v": 1, "cited_url": None, "final_url": None, "canonical_url": None,
           "excerpt": f"ref {tok} here", "title": None, "hint": None}
    if recorded:
        rec["excerpt_view"] = ["value_shape"]                  # F15b-4: recorded finding kept with the value
    return rec


def _env(rows: list) -> dict:
    return {"facts": {"rows": rows, "mnc_priority": {"items": []}}}


def _plant_inline(svc, tid: str, cid: str, toks: list) -> None:
    """A run recorded BEFORE sink job C: inline result, each row's evidence a per-row vault record (``vault_id``)."""
    import vault as vault_mod
    rows = [{"evidence": {"vault_id": vault_mod.ref(svc.vault.put(tid, _record(t, i % 2 == 1))), "basis": BASIS,
                          "fields": {}}} for i, t in enumerate(toks)]
    with svc.lock:
        svc.citation_runs[cid] = {"citation_run_id": cid, "tenant_id": tid, "status": "completed",
                                  "requested_at": "2026-10-09T18:00:00Z", "completed_at": "2026-10-09T18:01:00Z",
                                  "result": _env(rows), "result_sha256": None, "result_ref": None,
                                  "result_hmac": None, "facts": None}


def _plant_doc(svc, tid: str, cid: str, toks: list) -> None:
    """A run with a ``result_ref`` vaulted document (sink job C): rows name ``evidence_id`` keys of the document."""
    from svc_citations import RESULT_FIELD
    from svc_vaulted import normalised
    evidence = {f"e{i}": _record(t, i % 2 == 1) for i, t in enumerate(toks)}
    rows = [{"evidence": {"evidence_id": f"e{i}", "basis": BASIS, "fields": {}}} for i in range(len(toks))]
    doc = {"v": 1, "result": _env(rows), "evidence": evidence}
    sealed = svc._vaulted_seal(tid, RESULT_FIELD, normalised(doc))
    with svc.lock:
        svc._vaulted_commit(sealed, lambda: None)
        svc.citation_runs[cid] = {"citation_run_id": cid, "tenant_id": tid, "status": "recorded",
                                  "requested_at": "2026-10-09T18:00:00Z", "result": None,
                                  "result_ref": sealed.ref, "result_hmac": sealed.hmac}


def _shown(view: dict, toks: list) -> tuple:
    """(tokens shown anywhere in the rows' displayed excerpts, shown among the rows with a recorded finding)."""
    rows = ((view.get("result") or {}).get("facts") or {}).get("rows") or []
    allv, rec = [], []
    for i, r in enumerate(rows):
        ex = (r.get("evidence") or {}).get("excerpt") or {}
        v = ex.get("value") if isinstance(ex, dict) and isinstance(ex.get("value"), str) else ""
        allv.append(v)
        if i % 2 == 1:
            rec.append(v)
    a, b = "\n".join(allv), "\n".join(rec)
    return sum(t in a for t in toks), sum(t in b for t in toks[1::2]), len(rows)


def test_al2_ti1_e2e_citation_run_view_inline_and_result_ref_1000_owner_0_other(tmp_path):
    from errors import NotFound
    from helpers import Harness
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"), SEO_CV_ALLOWLIST="1")
    h.tenant(TA)
    h.tenant(TB)
    svc = h.svc
    pool = _toks(1515, 2 * N)
    own = {TA: pool[:N], TB: pool[N:]}
    other = {TA: own[TB], TB: own[TA]}
    runs = {}                                                  # (tid, kind) -> cid
    k = 0
    for tid in (TA, TB):
        for kind, plant in (("inline", _plant_inline), ("doc", _plant_doc)):
            k += 1
            mixed = [x for pair in zip(own[tid], other[tid]) for x in pair]   # own on even, other on odd positions
            plant(svc, tid, _cid(k), mixed)
            runs[(tid, kind)] = _cid(k)
    res: dict = {"n": N, "runs": {}}

    def measure(label: str) -> dict:
        out = {}
        for (tid, kind), cid in runs.items():
            v = svc.citation_run_view(tid, cid)
            mixed = [x for pair in zip(own[tid], other[tid]) for x in pair]
            shown_all, _r, nrows = _shown(v, mixed)
            own_shown = sum(t in json.dumps(v) for t in own[tid])
            oth_shown = sum(t in json.dumps(v) for t in other[tid])
            # odd positions (recorded value_shape rows) hold the OTHER tenant's tokens here; F15b-4 is measured below
            # on runs where the owner's own tokens sit on the recorded rows
            out[f"{tid}/{kind}"] = {"rows": nrows, "own_shown": own_shown, "other_shown": oth_shown,
                                    "shown_any": shown_all}
        res["runs"][label] = out
        return out

    for tid in (TA, TB):                                       # AL-1: each tenant lists its own catalog
        svc.cv_allowlist_sync(tid, {"source": "shopify", "integration_tenant": tid, "tokens": own[tid]}, "dashboard")
    on = measure("allowlist_on")
    svc.settings.cv_allowlist = False                          # negative control: allowlist out of scope
    off = measure("allowlist_off")
    svc.settings.cv_allowlist = True
    # F15b-4: a run whose EVERY row carries the recorded value_shape finding and holds an owner token
    rec_cnt = {}
    for tid in (TA, TB):
        for kind, plant in (("inline", _plant_inline), ("doc", _plant_doc)):
            k += 1
            toks = own[tid][:200]
            plant(svc, tid, _cid(k), [t for t in toks for _ in (0, 1)])   # rows i, i+1: odd i = recorded finding
            v = svc.citation_run_view(tid, _cid(k))
            _a, rec_shown, nrows = _shown(v, [t for t in toks for _ in (0, 1)])
            rec_cnt[f"{tid}/{kind}"] = {"rows": nrows, "recorded_rows_shown": rec_shown, "of": len(toks)}
    res["value_shape_recorded"] = rec_cnt
    cross = {}                                                 # the other tenant's run on this tenant's path
    for tid in (TA, TB):
        o = TB if tid == TA else TA
        n404 = 0
        for kind in ("inline", "doc"):
            try:
                svc.citation_run_view(tid, runs[(o, kind)])
            except NotFound:
                n404 += 1
        cross[tid] = n404
    res["cross_tenant_404"] = cross
    http = {}                                                  # F18-1: is the excerpt in the HTTP GET body?
    for (tid, kind), cid in runs.items():
        body = h.get(f"/tenants/{tid}/citation-runs/{cid}", caller="hub", tenant=tid)
        o = TB if tid == TA else TA
        xb = h.get(f"/tenants/{tid}/citation-runs/{runs[(o, kind)]}", caller="hub", tenant=tid)
        http[f"{tid}/{kind}"] = {"status": body.status_code, "own_shown": sum(t in body.text for t in own[tid]),
                                 "other_shown": sum(t in body.text for t in other[tid]),
                                 "cross_status": xb.status_code, "cross_shown": sum(t in xb.text for t in own[o])}
    res["http_get"] = http
    d = os.environ.get("C215C_EVIDENCE")
    if d:
        with open(os.path.join(d, "al_e2e.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1, sort_keys=True)
    print("C2-15c", json.dumps({"on": on, "off": off, "rec": rec_cnt, "cross404": cross, "http": http}))
    for key, m in on.items():
        assert m["rows"] == 2 * N, f"{key}: rows {m['rows']}"
        assert m["own_shown"] == N, f"{key}: owner tokens shown {m['own_shown']} of {N}"
        assert m["other_shown"] == 0, f"{key}: other tenant's tokens shown {m['other_shown']}"
    for key, m in off.items():                                 # the allowlist is what shows them (mutation target)
        assert m["own_shown"] < N // 10, f"{key}: own shown without allowlist {m['own_shown']} (control)"
        assert m["other_shown"] == 0, f"{key}: other tenant's tokens shown {m['other_shown']} (off)"
    for key, m in rec_cnt.items():
        assert m["recorded_rows_shown"] == m["of"], f"{key}: recorded value_shape rows shown {m['recorded_rows_shown']}"
    assert cross == {TA: 2, TB: 2}, f"cross-tenant run reads 404: {cross}"
    for key, m in http.items():
        assert m["status"] == 200 and m["other_shown"] == 0, f"{key}: http {m['status']} other {m['other_shown']}"
        assert m["own_shown"] == N, f"{key}: http owner tokens shown {m['own_shown']} of {N}"
        assert m["cross_status"] == 404 and m["cross_shown"] == 0, f"{key}: cross {m['cross_status']} {m['cross_shown']}"
