"""Pursuits: creation rules, qualification, the bid decision (Andre's), values, deadlines, loss and withdrawal."""

from datetime import timedelta

from clock import iso
from helpers import DEADLINE, T0, rid


def _body(**over):
    b = {"request_id": rid(), "brand": "zbm", "kind": "rfp", "title": "OOH RFP",
         "counterparty": {"ref": "org:acme", "name": "Acme Inc", "domain": "acme.test"}, "value": "5000.00",
         "deadline": DEADLINE}
    b.update(over)
    return b


def test_create_records_typed_event_without_names(h):
    p = h.pursuit()
    assert p["stage"] == "identified" and p["value"] == "5000.00"
    ev = h.ledger.of_type("pursuit_opened")
    assert len(ev) == 1 and "Acme" not in str(ev[0]["_payload"]) and "5000" not in str(ev[0]["_payload"])


def test_deadline_required_for_bids_not_pitches(h):
    for kind in ("rfp", "rfq", "enterprise_bid", "government_bid"):
        b = _body(kind=kind)
        b.pop("deadline")
        h.refused(h.post("/pursuits", b), 422, "DEADLINE_REQUIRED")
    b = _body(kind="formal_pitch")
    b.pop("deadline")
    assert h.ok(h.post("/pursuits", b), 201)["deadline"] is None


def test_deadline_in_past_or_now_refused(h):
    h.refused(h.post("/pursuits", _body(deadline=iso(T0))), 422, "DEADLINE_PASSED")
    h.refused(h.post("/pursuits", _body(deadline=iso(T0 - timedelta(seconds=1)))), 422, "DEADLINE_PASSED")


def test_naive_deadline_refused(h):
    h.refused(h.post("/pursuits", _body(deadline="2026-12-01T10:00:00")), 422)


def test_money_must_be_canonical_string(h):
    for bad in (5000.0, 5000, "5000", "5000.0", "-1.00", "1e4", "5,000.00", "05000.00", " 5000.00"):
        h.refused(h.post("/pursuits", _body(value=bad)), 422)


def test_checklist_only_on_government_bids(h):
    h.refused(h.post("/pursuits", _body(checklist=[{"code": "insurance_certificate"}])), 422, "NOT_A_GOVERNMENT_BID")


def test_counterparty_name_needs_letters(h):
    h.refused(h.post("/pursuits", _body(counterparty={"ref": "org:x", "name": "Inc.", "domain": "x.test"})), 422,
              "COUNTERPARTY_INVALID")


def test_forbidden_keys_anywhere(h):
    for key in ("phone", "ssn", "tin", "ein", "tax_id", "bank_account", "card_number", "date_of_birth"):
        b = _body()
        b["counterparty"] = {**b["counterparty"], key: "probe-value-7731"}
        r = h.post("/pursuits", b)
        assert r.status_code == 422 and "probe-value-7731" not in r.text


def test_qualification_recommendation(h):
    p = h.pursuit()
    q = h.qualify(p["pursuit_id"])["qualification"]
    assert q["result"]["recommendation"] == "bid" and q["result"]["score"] == 7
    q = h.qualify(p["pursuit_id"], capacity="no")["qualification"]
    assert q["result"]["recommendation"] == "no_bid" and q["result"]["blockers"] == ["capacity"]
    q = h.qualify(p["pursuit_id"], scope_fit="unknown")["qualification"]
    assert q["result"]["recommendation"] == "needs_andre"
    q = h.qualify(p["pursuit_id"], relationship="no", price_competitive="no", payment_terms_acceptable="unknown")
    assert q["qualification"]["result"]["recommendation"] == "needs_andre"


def test_bid_is_andre_only(h):
    p = h.pursuit()
    q = h.qualify(p["pursuit_id"])["qualification"]
    body = {"request_id": rid(), "decision": "bid", "qualification_sha256": q["qualification_sha256"]}
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/bid-decision", body), 403, "ANDRE_APPROVAL_REQUIRED")
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/bid-decision", body, caller="dashboard"), 403,
              "ANDRE_APPROVAL_REQUIRED")
    r = h.client.post(f"/nbd/v1/pursuits/{p['pursuit_id']}/bid-decision", json=body,
                      headers={**h.headers("dashboard"), "X-Andre-Approval-Token": "x" * 40})
    h.refused(r, 403, "ANDRE_APPROVAL_INVALID")
    r = h.client.post(f"/nbd/v1/pursuits/{p['pursuit_id']}/bid-decision", json=body,
                      headers={**h.headers("bizdev_agent"), "X-Andre-Approval-Token": h.headers(andre=True)[
                          "X-Andre-Approval-Token"]})
    h.refused(r, 403, "CALLER_NOT_ALLOWED")
    assert h.ok(h.post(f"/pursuits/{p['pursuit_id']}/bid-decision", body, andre=True))["stage"] == "responding"
    assert h.ledger.of_type("bid_decided")[0]["actor"] == "andre"


def test_bid_binds_the_exact_qualification(h):
    p = h.pursuit()
    q1 = h.qualify(p["pursuit_id"])["qualification"]
    h.qualify(p["pursuit_id"], relationship="no")
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/bid-decision",
                     {"request_id": rid(), "decision": "bid", "qualification_sha256": q1["qualification_sha256"]},
                     andre=True), 409, "QUALIFICATION_STALE")


def test_bid_needs_qualification(h):
    p = h.pursuit()
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/bid-decision",
                     {"request_id": rid(), "decision": "bid", "qualification_sha256": "a" * 64}, andre=True), 409,
              "QUALIFICATION_REQUIRED")


def test_agent_may_record_no_bid(h):
    p = h.pursuit()
    q = h.qualify(p["pursuit_id"], capacity="no")["qualification"]
    p = h.ok(h.post(f"/pursuits/{p['pursuit_id']}/bid-decision",
                    {"request_id": rid(), "decision": "no_bid", "qualification_sha256": q["qualification_sha256"]}))
    assert p["stage"] == "no_bid"
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/value", {"request_id": rid(), "value": "1.00"}), 409,
              "PURSUIT_CLOSED")


def test_deadline_moves_only_with_andre(h):
    p = h.pursuit()
    later = iso(T0 + timedelta(days=30))
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/deadline", {"request_id": rid(), "deadline": later}), 403,
              "CALLER_NOT_ALLOWED")
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/deadline", {"request_id": rid(), "deadline": later},
                     caller="dashboard"), 403, "ANDRE_APPROVAL_REQUIRED")
    assert h.ok(h.post(f"/pursuits/{p['pursuit_id']}/deadline", {"request_id": rid(), "deadline": later},
                       andre=True))["deadline"] == later
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/deadline", {"request_id": rid(), "deadline": iso(T0)},
                     andre=True), 422, "DEADLINE_PASSED")


def test_lost_and_withdraw(h):
    p = h.pursuit()
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/lost", {"request_id": rid(), "reason_code": "price"}), 409,
              "STAGE_NOT_ALLOWED")
    h.bid(p["pursuit_id"])
    assert h.ok(h.post(f"/pursuits/{p['pursuit_id']}/lost", {"request_id": rid(), "reason_code": "price"}))[
        "stage"] == "lost"
    p2 = h.pursuit(ref="org:other", domain="other.test", name="Other Co")
    h.refused(h.post(f"/pursuits/{p2['pursuit_id']}/withdraw", {"request_id": rid()}), 403)
    assert h.ok(h.post(f"/pursuits/{p2['pursuit_id']}/withdraw", {"request_id": rid()}, andre=True))[
        "stage"] == "withdrawn"


def test_import_is_not_built(h):
    h.refused(h.post("/pursuits/import", {"request_id": rid(), "limit": 5}), 503, "SOURCE_NOT_WIRED")


def test_request_id_scoped_and_reuse_conflicts(h):
    b = _body()
    first = h.ok(h.post("/pursuits", b), 201)
    assert h.ok(h.post("/pursuits", b), 201)["pursuit_id"] == first["pursuit_id"]
    h.refused(h.post("/pursuits", {**b, "value": "6000.00"}), 409, "REQUEST_ID_REUSED")
    other = h.ok(h.post("/pursuits", {**b, "counterparty": {"ref": "org:b", "name": "Bee Co",
                                                            "domain": "bee.test"}}), 201)
    assert other["pursuit_id"] != first["pursuit_id"]       # same request_id, another target: another request


def test_unknown_fields_and_ids_refused(h):
    h.refused(h.post("/pursuits", {**_body(), "extra": 1}), 422)
    h.refused(h.get("/pursuits/not-an-id"), 422)
    h.refused(h.get("/pursuits/nb-pur-" + "0" * 40), 404, "PURSUIT_NOT_FOUND")
