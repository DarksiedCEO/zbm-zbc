"""AEGIS r9 R9-1 (ADR 0024): every route that sends a client's money OUT asks Compliance at that moment.

A deposit refund (scheduler proposal, Andre's approval, Andre's re-payment) is refused while the client is not a
current ALLOW (refused or unreachable) or any payment of the client is held; on a sanctions ground Andre's token gets
an OFAC warning (do not return blocked property; runbook step G), other callers a generic reason. AEGIS's probe:
deposit approved while allowed -> the client becomes an OFAC match -> the bank wire is booked -> the scheduler
proposes the refund -> Andre approves: refused.
"""

from __future__ import annotations

from helpers import ANDRE_TOKEN, Harness, rid


def _match(hr, client="client-1"):
    hr.cmp.activations = {client: {"allowed": False, "jurisdiction": "US-CA", "reasons": ["hold open: sanctions_match"],
                                   "sanctions": True, "ground": "match"}}


def _allow(hr):
    hr.cmp.activation_available = True
    hr.cmp.activations = {}


def _decide(hr, rf):
    return hr.post(f"/fin/v1/refunds/{rf['refund_id']}/decision",
                   {"request_id": rid(), "content_sha256": rf["content_sha256"], "decision": "approve"},
                   andre=ANDRE_TOKEN)


def test_aegis_r9_probe_a_sanctioned_clients_deposit_is_never_refunded():
    hr = Harness().ready()
    hr.profile(doc_id=hr.rate_card())
    inv = hr.deposit_invoice()                                  # approved while ALLOW
    _match(hr)                                                  # then an OFAC match
    hr.receive("zbc", "1020", "1000.00", inv["invoice_id"])     # the wire is booked (held, RTM-01)
    hr.recon()
    r = hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler")
    assert r.status_code == 409, r.text
    assert "OFAC" not in r.text and "sanction" not in r.text.lower()       # the scheduler gets a generic reason
    assert not hr.svc.db["refunds"]


def test_a_refund_proposed_while_allowed_is_refused_at_approval_with_an_ofac_warning_for_andre():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.recon()
    rf = hr.ok(hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler"), 201)["refund"]
    _match(hr)
    r = _decide(hr, rf)
    assert r.status_code == 409 and "OFAC" in r.text and "step G" in r.text, r.text
    assert hr.svc.db["refunds"][rf["refund_id"]]["status"] == "proposed"
    assert not [t for t in hr.svc.db["treasury_ops"].values() if t["kind"] == "refund_payment"]


def test_a_refund_is_refused_while_compliance_is_unreachable_and_paid_on_allow():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.recon()
    rf = hr.ok(hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler"), 201)["refund"]
    hr.cmp.activation_available = False
    r = _decide(hr, rf)
    assert r.status_code == 409 and "compliance_38" in r.text, r.text
    _allow(hr)
    assert hr.ok(_decide(hr, rf))["refund"]["status"] == "paid"


def test_a_refund_is_refused_while_any_payment_of_the_client_is_held():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.recon()
    with hr.svc.lock:                                          # some payment of this client is held by Compliance
        some = next(iter(i for i in hr.svc.db["invoices"].values() if i["client_id"] == "client-1"))
        hr.svc.db["invoices"][some["invoice_id"]] = {**some, "held_compliance": {"kind": "unverified",
                                                                                "sanctions": False}}
    r = hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler")
    assert r.status_code == 409 and "held" in r.text, r.text


# --- R9-2: nothing a non-Andre caller sees is unique to a sanctions upgrade -------------------------------------------

def _visible(hr):
    """What a non-Andre reader can see after the scenario: event-id prefixes (and how many), id shapes, break legs,
    the key shape of every record put (by collection), and the job summary keys."""
    import json
    import re
    from collections import Counter
    texts, shapes_rec, jobs = [], set(), set()
    cur = 0
    while cur is not None:
        page = hr.get("/fin/v1/audit/export", caller="onboarding").json() if cur == 0 else \
            hr.get("/fin/v1/audit/export", caller="onboarding", cursor=cur).json()
        texts.append(json.dumps(page))
        for e in next(v for v in page.values() if isinstance(v, list)):
            for r in e.get("records", []):
                rec = r["record"].get("rec") if isinstance(r["record"], dict) else None
                if isinstance(rec, dict):
                    shapes_rec.add((r["record"].get("coll"), tuple(sorted(rec))))
                    if isinstance(rec.get("summary"), dict):
                        jobs.add(tuple(sorted(rec["summary"])))
        cur = page["next_cursor"]
    texts.append(hr.get("/fin/v1/audit/evidence", caller="onboarding").text)
    texts.append(hr.get("/fin/v1/breaks", caller="onboarding").text)
    for b in hr.get("/fin/v1/breaks", caller="onboarding").json().get("breaks", []):
        shapes_rec.add(("breaks-view", tuple(sorted(b))))
    t = "\n".join(texts)
    ids = re.findall(r'"fin-([a-z0-9]+)-([0-9a-zA-Z]+)"', t)
    prefixes = {p for p, _ in ids}
    id_shapes = {(p, len(x)) for p, x in ids}
    legs = set(re.findall(r'"leg": ?"([a-z_]+)"', t))
    return prefixes, id_shapes, legs, shapes_rec, jobs, Counter(p for p, _ in ids if p == "hold")


def _scenario(tmp_path, upgrade: bool, refund: bool):
    from test_compliance_gate_adr0024 import _stripe_hr
    from test_compliance_hold_r4 import _job
    from test_compliance_hold_r5 import _pay_then
    from test_compliance_hold_r6 import _refuse_plain
    from test_compliance_hold_r8 import _ground
    from test_stripe_incoming import sent
    from test_sweep_followup import _charge
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _refuse_plain(hr))
    if upgrade:
        _ground(hr, "match")
    _job(hr)
    if refund:
        hr.sim.refund(pi)
        sent(hr, "charge.refunded", _charge(hr, pi))
    return _visible(hr)


def test_r9_2_the_same_prefixes_shapes_and_legs_with_and_without_a_sanctions_upgrade(tmp_path):
    names = ("event-id prefixes", "event-id shapes", "break legs", "record shapes", "job summary keys")
    for refund in (False, True):
        (tmp_path / f"a{refund}").mkdir()
        (tmp_path / f"b{refund}").mkdir()
        plain = _scenario(tmp_path / f"a{refund}", upgrade=False, refund=refund)
        sanct = _scenario(tmp_path / f"b{refund}", upgrade=True, refund=refund)
        for n, x, y in zip(names, plain, sanct):
            assert x == y, (n, "differ", refund, sorted(set(x) - set(y)), sorted(set(y) - set(x)))
