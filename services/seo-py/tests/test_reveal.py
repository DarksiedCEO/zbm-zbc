"""core-spec/2 RV-1..RV-4, V-4, V-5 (card C2-14): audited click-to-reveal of the tenant's OWN text. Synthetic values."""
from __future__ import annotations

import json
import random
import string
from datetime import timedelta

import pytest

import sink_fields as sf
import sink_guard
import svc_reveal
from agents import callum
from errors import NotFound, Throttled, Unavailable
from helpers import T0, Harness
from svc_citations import RESULT_FIELD
from svc_vaulted import normalised

N = 1000
A, B = "acme", "globex"                                   # tenants with hub tenant tokens (helpers.TENANT_TOKENS)


def _tokens(seed: int, n: int) -> list:
    rnd, out = random.Random(seed), []
    while len(out) < n:
        t = "".join(rnd.choice(string.ascii_uppercase + string.digits) for _ in range(14))
        if any(c.isdigit() for c in t) and any(c.isalpha() for c in t) and t not in out:
            out.append(t)
    return out


def _cid(n: int) -> str:
    return "seo-cit-" + f"{n:040x}"


def _run(svc, tid: str, cid: str, toks: list) -> dict:
    """A recorded citation run of ``tid`` whose vaulted document holds ``toks`` in evidence excerpts."""
    doc = {"v": 1, "result": {"facts": {"rows": []}},
           "evidence": {f"e{i}": {"excerpt": f"ref {t} here"} for i, t in enumerate(toks)}}
    sealed = svc._vaulted_seal(tid, RESULT_FIELD, normalised(doc))
    with svc.lock:
        svc._vaulted_commit(sealed, lambda: None)
        # C2-10d (F10b-3): the shape a recorded run always has (svc_citations writes citation_run_id and
        # requested_at on request), so the run list sorts it like a real run
        svc.citation_runs[cid] = {"citation_run_id": cid, "tenant_id": tid, "status": "recorded",
                                  "requested_at": "2026-10-09T18:00:00Z", "result": None,
                                  "result_ref": sealed.ref, "result_hmac": sealed.hmac}
    return _codes(svc, tid, toks)


def _codes(svc, tid: str, toks: list) -> dict:
    """C2-14d (V-3): the span codes the OWNER's view lists = computed under the tenant's coder (svc._owner_coding,
    the same context citation_run_view and the reveal recompute use; was: the process-random fallback coder)."""
    with svc._owner_coding(tid):
        return {t: callum._display("excerpt", f"ref {t} here")["spans"][0]["code"] for t in toks}


def _rows(h) -> list:
    out = []
    for e in h.ledger.of_type(svc_reveal.AUDIT_EVENT):
        for v in e.values():
            d = json.loads(v) if isinstance(v, str) and v.startswith("{") else v
            if isinstance(d, dict) and "outcome" in d:
                out.append(d)
    return out


@pytest.fixture
def two(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"))
    h.tenant(A)
    h.tenant(B)
    h.svc.settings.reveal = True
    clock = [T0]
    h.svc.now = lambda: clock[0]
    h.clock = clock
    return h


def test_wiring_defaults_off_nothing_read_or_audited(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"))
    h.tenant(A)
    codes = _run(h.svc, A, _cid(1), _tokens(1, 3))
    with pytest.raises(Unavailable):
        h.svc.reveal_span("hub", A, "citation_run", _cid(1), next(iter(codes.values())))
    assert _rows(h) == []


def test_rv4_1000_reveals_audited_1_to_1_and_values_found_v5(two):
    toks = _tokens(1401, N)
    codes = _run(two.svc, A, _cid(1), toks)
    got = []
    for i, t in enumerate(toks):
        two.clock[0] = T0 + timedelta(hours=i // svc_reveal.RATE_CAP, seconds=i % svc_reveal.RATE_CAP)
        got.append(two.svc.reveal_span("hub", A, "citation_run", _cid(1), codes[t])["value"])
    assert got == toks                                    # V-5: 100% found for the owner
    rows = _rows(two)
    assert len(rows) == N and all(r["outcome"] == "revealed" for r in rows)       # RV-4: 1:1, audit-first
    assert sorted(r["span"] for r in rows) == sorted(codes[t][1:-1] for t in toks)   # T-2 brace-less
    # C2-14c: checked under the kind the ledger itself enforces (ledger.py / service.py: LEDGER + event_type; T-2 ref
    # waiver is declared for ledger:span_revealed /span, ruling 21:45 PT). The bare event name is not a sink kind and
    # strict-refused every brace-less code. Now ALL rows (was rows[:50]) - stronger, never weaker (G-7).
    assert all(sink_guard.violations(sink_guard.LEDGER + svc_reveal.AUDIT_EVENT, r) == [] for r in rows)
    blob = json.dumps(two.ledger.entries())
    data = "".join(p.read_text(errors="replace") for p in (two.tmp / "data").rglob("*") if p.is_file())
    assert [t for t in toks if t in blob or t in data] == []                      # RV-3: no value in any sink


def test_cross_tenant_0_and_404_identical_to_unknown(two):
    toks = _tokens(1402, 200)
    codes = _run(two.svc, A, _cid(1), toks)
    _run(two.svc, B, _cid(2), _tokens(1403, 5))
    wins = 0
    for i, t in enumerate(toks[:99]):
        two.clock[0] = T0 + timedelta(minutes=30 * i)    # C2-14b: 3 attempts per i stay under the RV-1 cap
        for rid in (_cid(1), _cid(2), _cid(3)):           # A's run, B's own run, a run that does not exist
            try:
                two.svc.reveal_span("hub", B, "citation_run", rid, codes[t])
                wins += 1
            except NotFound:
                pass
    assert wins == 0
    other = two.post("/tenants/globex/reveals", {"surface": "citation_run", "run_id": _cid(1), "span": codes[toks[0]]},
                     caller="hub", tenant=B)
    unknown = two.post("/tenants/globex/reveals", {"surface": "citation_run", "run_id": _cid(2),
                                                   "span": "{c:text:0123456789abcdef}"}, caller="hub", tenant=B)
    assert other.status_code == unknown.status_code == 404 and other.content == unknown.content
    assert {r["outcome"] for r in _rows(two)} == {"not_found"}


def test_audit_store_failing_returns_0_values(two):
    toks = _tokens(1404, 50)
    codes = _run(two.svc, A, _cid(1), toks)
    two.ledger.fail = True
    values = 0
    for t in toks:
        try:
            values += bool(two.svc.reveal_span("hub", A, "citation_run", _cid(1), codes[t]).get("value"))
        except Exception:  # noqa: BLE001 - any refusal is a pass; a returned value is the failure
            pass
    assert values == 0
    r = two.post("/tenants/acme/reveals", {"surface": "citation_run", "run_id": _cid(1), "span": codes[toks[0]]},
                 caller="hub", tenant=A)
    assert r.status_code == 503 and toks[0] not in r.text


def test_rate_cap_101st_in_the_hour_refused_and_audited(two):
    toks = _tokens(1405, 101)
    codes = _run(two.svc, A, _cid(1), toks)
    for i, t in enumerate(toks[:100]):
        two.clock[0] = T0 + timedelta(seconds=i)
        assert two.svc.reveal_span("hub", A, "citation_run", _cid(1), codes[t])["value"] == t
    two.clock[0] = T0 + timedelta(minutes=59)
    with pytest.raises(Throttled):
        two.svc.reveal_span("hub", A, "citation_run", _cid(1), codes[toks[100]])
    assert _rows(two)[-1]["outcome"] == "rate_limited"
    two.clock[0] = T0 + timedelta(hours=1, seconds=1)    # the first attempt left the rolling hour
    assert two.svc.reveal_span("hub", A, "citation_run", _cid(1), codes[toks[100]])["value"] == toks[100]


def test_route_owner_with_tenant_token_only_no_store(two):
    toks = _tokens(1406, 2)
    codes = _run(two.svc, A, _cid(1), toks)
    req = {"surface": "citation_run", "run_id": _cid(1), "span": codes[toks[0]]}
    ok = two.post("/tenants/acme/reveals", req, caller="hub", tenant=A)
    assert ok.status_code == 200 and ok.json() == {"value": toks[0]}
    assert ok.headers["cache-control"] == "no-store"
    for caller in ("dashboard", "seo_agent", "finance_31", "compliance_38"):     # RV-2 / V-4: never these
        r = two.post("/tenants/acme/reveals", req, caller=caller)
        assert r.status_code in (403, 404) and toks[0] not in r.text
    assert two.post("/tenants/acme/reveals", req, caller="hub", tenant=B).status_code == 404
    assert two.post("/tenants/acme/reveals", {**req, "surface": "export"}, caller="hub",
                    tenant=A).status_code == 422
    assert len(_rows(two)) == 1


def test_sink_table_enums_equal_the_module_and_paths_typed():
    assert sf.RV_ENUMS["/surface"] == frozenset(svc_reveal.SURFACES)
    assert sf.RV_ENUMS["/outcome"] == frozenset(svc_reveal.OUTCOMES)
    for p in ("/tenant_id", "/actor", "/at", "/span", "/surface", "/outcome", "/rk", "/seq"):
        assert ("ledger", svc_reveal.AUDIT_EVENT, p) in sf.SINK_FIELDS
        assert ("seo_log", svc_reveal.LOG_KIND, "/evidence[]/payload" + p) in sf.SINK_FIELDS
    assert not any("value" in p for (_s, k, p) in sf.SINK_FIELDS if k in (svc_reveal.AUDIT_EVENT,
                                                                           svc_reveal.LOG_KIND))


# ---------------------------------------------------------------- C2-14d: V-3 span codes per tenant, stable on restart

def test_v3_identical_tokens_two_tenants_get_different_span_codes_and_never_cross(two):
    toks = _tokens(1404, 50)
    ca = _run(two.svc, A, _cid(1), toks)
    cb = _run(two.svc, B, _cid(2), toks)                  # the SAME tokens in B's own run
    assert sum(ca[t] == cb[t] for t in toks) == 0        # V-3 (B-CD-01): 2 tenants x values -> 0 equal codes
    assert len(set(ca.values())) == len(toks)            # distinct values -> distinct codes
    wins = 0
    for i, t in enumerate(toks[:40]):
        two.clock[0] = T0 + timedelta(minutes=30 * i)
        for rid in (_cid(1), _cid(2)):                     # A's code tried by B on A's run and on B's own run
            try:
                two.svc.reveal_span("hub", B, "citation_run", rid, ca[t])
                wins += 1
            except NotFound:
                pass
    assert wins == 0                                     # RV-4 / TI-1: an A code finds nothing for B, even same text
    two.clock[0] = T0 + timedelta(days=2)
    assert two.svc.reveal_span("hub", A, "citation_run", _cid(1), ca[toks[0]])["value"] == toks[0]
    assert two.svc.reveal_span("hub", B, "citation_run", _cid(2), cb[toks[0]])["value"] == toks[0]


def test_v3_same_tenant_same_code_across_restart_and_reveal_after_restart_finds_it(two):
    toks = _tokens(1405, 20)
    before = _run(two.svc, A, _cid(1), toks)
    again = _codes(two.svc, A, toks)
    assert again == before                               # V-3: 1 input -> 1 code (not per call)
    rec = dict(two.svc.citation_runs[_cid(1)])
    two.svc.close()                                      # restart (the fixture's clock is a list: not passed on)
    h = Harness(two.tmp, two.settings.data_dir, two.ledger, None, two.ports)
    try:
        h.svc.settings.reveal = True                     # tenants replay from the same ledger
        h.svc.now = lambda: T0 + timedelta(days=3)
        with h.svc.lock:
            h.svc.citation_runs.setdefault(_cid(1), rec)  # the recorded run (its blob is in the durable vault)
        after = _codes(h.svc, A, toks)
        assert after == before                           # stable across a service restart (durable key)
        got = [h.svc.reveal_span("hub", A, "citation_run", _cid(1), before[t])["value"] for t in toks]
        assert got == toks                               # a pre-restart code still reveals its span
    finally:
        h.svc.close()


def test_v3_owner_view_runs_under_the_tenant_coder(two, monkeypatch):
    _run(two.svc, A, _cid(1), _tokens(1406, 2))
    from primitives import codes
    seen = []
    real = callum.client_view

    def spy(obj, rec):
        c = codes._ctx()
        seen.append((getattr(c.coder, "tenant_id", None), codes.key_id() is not None, c.evidence is not None))
        return real(obj, rec)

    monkeypatch.setattr(callum, "client_view", spy)
    two.svc.citation_run_view(A, _cid(1))
    assert seen == [(A, True, True)]                     # the tenant's coder, not the process fallback (key_id None)
    with two.svc._owner_coding(A) as ev:
        callum._display("excerpt", "ref " + _tokens(1407, 1)[0] + " here")
        assert len(ev) == 0                              # throw-away evidence: no raw value kept by the view
