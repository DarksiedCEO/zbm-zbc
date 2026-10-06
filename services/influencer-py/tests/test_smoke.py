"""The happy path end to end with every port wired to a fake: application -> outreach -> campaign, brief, deal ->
contract -> content (FTC) -> live -> tax reference -> verified payee -> payout request handed to Finance."""

from helpers import rid


def test_health_is_open_and_says_nothing_else(h):
    r = h.client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert r.headers["cache-control"] == "no-store"


def test_status_needs_the_service_token_and_the_dashboard(h):
    assert h.client.get("/inf/v1/status").status_code == 401
    h.code(h.get("/status", caller="influencer_agent"), 403, "CALLER_NOT_ALLOWED")
    st = h.ok(h.get("/status"))
    assert st["integrity"]["ok"] is True and st["andre_approvals_configured"] is True
    assert not any(st["ports_wired"].values())


def test_intelligences_listed(h):
    names = [x["name"] for x in h.ok(h.get("/intelligences"))]
    assert len(names) == 11 and "ftc_disclosure" in names and "deal_approval" in names


def test_end_to_end_with_wired_fakes(w):
    inf, d, content = w.paid_ready(fee="1200.00")
    assert d["status"] == "approved" and d["approved_by"] == "auto"
    p = w.ok(w.payout(d, "1200.00", [content["content_id"]]), 201)
    assert p["status"] == "submitted" and "payee_ref" not in p
    assert w.ports.finance.payouts[0][2] == "1200.00"
    assert w.ok(w.get(f"/deals/{d['deal_id']}"))["status"] == "completed"
    camp = w.ok(w.get(f"/campaigns/{d['campaign_id']}"))
    assert camp["live_content"] == 1
    types = {e["event_type"] for e in w.ledger.events}
    assert {"influencer_recorded", "age_attestation_recorded", "brief_approved", "deal_recorded",
            "material_connection_recorded", "contract_sent", "contract_in_force", "content_approved", "content_live",
            "tax_profile_recorded", "payee_status_recorded", "payout_requested", "payout_result",
            "log_anchor"} <= types
    mc = w.ok(w.get("/material-connections", caller="compliance_38"))
    assert mc[0]["deal_id"] == d["deal_id"] and mc[0]["kinds"] == ["payment"] and mc[0]["disclosure"] == "#ad"


def test_nothing_personal_reaches_the_ledger(w):
    inf, d, content = w.paid_ready()
    w.ok(w.payout(d, "1000.00", [content["content_id"]]), 201)
    blob = str([e["_payload"] for e in w.ledger.events]) + str([e["summary"] for e in w.ledger.events])
    for raw in ("creator@example.test", "creator.one", "Creator One", "Casey", "stripe:acct_TESTabcdef",
                "Loving this new tool"):
        assert raw not in blob


def test_audit_export_minimises(w):
    w.paid_ready()
    out = w.ok(w.get("/audit/export", caller="compliance_38"))
    blob = str(out)
    for raw in ("creator@example.test", "creator.one", "Creator One", "Casey", "acct_TESTabcdef"):
        assert raw not in blob
    assert out["log_length"] == len(w.svc.log)


def test_request_id_replay_and_reuse(h):
    body = {"request_id": rid(), "brand": "zbm", "name": "Fall", "kind": "influencer"}
    a = h.ok(h.post("/campaigns", body, caller="influencer_agent"), 201)
    b = h.ok(h.post("/campaigns", body, caller="influencer_agent"), 201)
    assert a["campaign_id"] == b["campaign_id"] and len(h.svc.campaigns) == 1
    h.code(h.post("/campaigns", {**body, "name": "Other"}, caller="influencer_agent"), 409, "REQUEST_ID_REUSED")
