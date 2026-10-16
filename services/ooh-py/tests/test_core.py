"""The service skeleton (ADR 0019 decisions 1-8): start, auth, Andre's gate, callers, tenants with cross-tenant negative
tests, every kill switch, idempotency, personal-data refusal, NOT_BUILT settings, the constitution and the roster as
data, restart / replay from the log, and the R6-M1 evidence view."""

from __future__ import annotations

import json

import pytest

import config as config_mod
import policy
from helpers import ANDRE, CALLERS, SERVICE_TOKEN, TENANT_TOKENS, Harness, base_env, rid


def test_starts_verified_with_every_port_not_connected(h):
    st = h.ok(h.get("/status"))
    assert st["integrity"]["ok"] is True and st["status"] == "ok" and st["andre_approvals_configured"] is True
    assert set(st["ports"].values()) == {"NOT_CONNECTED"}
    assert st["funds_verification"] == {"mode": "andre_gate", "sources_connected": ["ANDRE_GATE"],
                                        "finance_31": "NOT_CONNECTED", "policy": "CLIENT_PREPAY_REQUIRED"}
    assert st["commercial"] == {"suggested_markup_pct": "15.00", "suggested_markup_applied": False}
    assert st["agents_built"] == 0
    assert h.client.get("/health").json() == {"status": "ok"}


def test_auth_bearer_and_callers(h):
    assert h.client.get("/ooh/v1/status").status_code == 401
    assert h.client.get("/ooh/v1/status", headers={"Authorization": "Bearer " + "x" * 40}).status_code == 401
    r = h.client.get("/ooh/v1/status", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    h.refused(r, 403, "CALLER_UNKNOWN")
    h.refused(h.get("/status", caller="ooh_agent"), 403, "CALLER_NOT_ALLOWED")
    r = h.client.get("/ooh/v1/status", headers={**h.headers("dashboard"), "X-OOH-Tenant-Token": TENANT_TOKENS["acme"]})
    h.refused(r, 403, "CALLER_NOT_ALLOWED")              # a tenant token only travels with the hub


def test_andre_gate(h):
    body = {"request_id": rid(), "tenant_id": "acme"}
    h.refused(h.post("/tenants", body, caller="dashboard"), 403, "ANDRE_APPROVAL_REQUIRED")
    r = h.client.post("/ooh/v1/tenants", json=body, headers={**h.headers("dashboard"),
                                                              "X-Andre-Approval-Token": "y" * 64})
    h.refused(r, 403, "ANDRE_APPROVAL_INVALID")
    r = h.client.post("/ooh/v1/tenants", json=body, headers={**h.headers("dashboard"),
                                                              "X-Andre-Approval-Token": "é".encode() * 20})
    h.refused(r, 403, "ANDRE_APPROVAL_INVALID")          # non-ASCII: refused, never a 500
    r = h.client.post("/ooh/v1/tenants", json=body, headers={**h.headers("ooh_agent"), "X-Andre-Approval-Token": ANDRE})
    h.refused(r, 403, "CALLER_NOT_ALLOWED")              # Andre's token only counts through the dashboard
    h.ok(h.post("/tenants", body, andre=True), 201)


@pytest.mark.parametrize("token", ["service", "caller", "tenant", None])
def test_andre_token_equal_to_another_token_or_unset_is_not_configured(tmp_path, token):
    value = {"service": SERVICE_TOKEN, "caller": CALLERS["dashboard"], "tenant": TENANT_TOKENS["acme"],
             None: None}[token]
    hh = Harness(tmp_path, OOH_ANDRE_APPROVAL_TOKEN=value)
    r = hh.post("/tenants", {"request_id": rid(), "tenant_id": "acme"}, andre=True)
    hh.refused(r, 403, "ANDRE_NOT_CONFIGURED")
    assert hh.ok(hh.get("/status"))["andre_approvals_configured"] is False


@pytest.mark.parametrize("name", sorted(config_mod.NOT_BUILT))
def test_a_not_built_setting_refuses_start(name):
    with pytest.raises(RuntimeError, match=name):
        config_mod.load(base_env(**{name: "https://vendor.example/api"}))


def test_production_needs_a_data_dir_and_tokens_are_checked():
    with pytest.raises(RuntimeError, match="OOH_DATA_DIR"):
        config_mod.load(base_env(OOH_NON_PRODUCTION=None))
    with pytest.raises(RuntimeError, match="OOH_SERVICE_TOKEN"):
        config_mod.load(base_env(OOH_SERVICE_TOKEN="short"))
    with pytest.raises(RuntimeError, match="OOH_CALLER_TOKENS"):
        config_mod.load(base_env(OOH_CALLER_TOKENS=json.dumps({"stranger": "z" * 40})))
    with pytest.raises(RuntimeError, match="OOH_MAX_PURCHASE_AMOUNT"):
        config_mod.load(base_env(OOH_MAX_PURCHASE_AMOUNT="1e9"))


def test_tenants_are_andres_and_isolated(h):
    h.tenant("acme")
    h.tenant("globex")
    h.refused(h.post("/tenants", {"request_id": rid(), "tenant_id": "acme"}, andre=True), 409, "TENANT_EXISTS")
    acme = h.campaign("acme")
    globex = h.campaign("globex")
    # the hub sees its own tenant's campaign, and the client view never carries vendor cost or terms
    mine = h.ok(h.get(f"/tenants/acme/campaigns/{acme}", caller="hub", tenant="acme"))
    assert set(mine) == {"campaign_id", "tenant_id", "name", "state", "approved_price", "currency", "funding_state",
                         "client_funds_received"}
    other = h.refused(h.get(f"/tenants/globex/campaigns/{globex}", caller="hub", tenant="acme"), 404,
                      "TENANT_NOT_FOUND")
    missing = h.refused(h.get("/tenants/nosuch/campaigns/" + globex, caller="hub", tenant="acme"), 404,
                        "TENANT_NOT_FOUND")
    assert other == missing                                    # no existence oracle
    # another tenant's campaign through one's own tenant path: the same 404 as a campaign that does not exist
    a = h.refused(h.get(f"/tenants/acme/campaigns/{globex}"), 404, "CAMPAIGN_NOT_FOUND")
    b = h.refused(h.get("/tenants/acme/campaigns/ooh-cmp-" + "0" * 40), 404, "CAMPAIGN_NOT_FOUND")
    assert a == b
    h.refused(h.post(f"/tenants/acme/campaigns/{globex}/inventory",
                     {"request_id": rid(), "vendor": "lamar", "kind": "static", "market": "la",
                      "vendor_unit_ref": "x1"}), 404, "CAMPAIGN_NOT_FOUND")
    h.refused(h.post(f"/tenants/acme/campaigns/{globex}/transitions",
                     {"request_id": rid(), "to": "BRIEF_VALIDATED", "expected_state": "INTAKE", "expected_version": 1,
                      "reason": "CROSS_TENANT"}), 404, "CAMPAIGN_NOT_FOUND")
    assert h.view("globex", globex)["state"] == "INTAKE"
    h.refused(h.post("/tenants/acme/campaigns", {"request_id": rid(), "name": "x y", "markets": ["la"]},
                     caller="hub", tenant="acme"), 403, "CALLER_NOT_ALLOWED")   # the hub only reads


def test_cross_tenant_references_inside_bodies_are_refused_like_nothing(h):
    h.tenant("acme")
    h.tenant("globex")
    g = h.funded("globex")
    ctx = h.funded("acme")
    # a globex quote or unit named in an acme authorization: refused exactly like an id that does not exist
    h.refused(h.authorize(ctx, exact_quote_refs=[g["qid"]]), 409, "QUOTE_NOT_FOUND")
    h.refused(h.authorize(ctx, exact_quote_refs=["ooh-prc-" + "1" * 40]), 409, "QUOTE_NOT_FOUND")
    h.refused(h.authorize(ctx, inventory_ids=[g["uid"]]), 409, "INVENTORY_MISMATCH")
    h.refused(h.price("acme", ctx["cid"], "VENDOR_COST_ESTIMATE", "10.00", inventory_ids=[g["uid"]]), 404,
              "INVENTORY_NOT_FOUND")
    # a globex funds evidence reference cannot fund an acme campaign either
    c2 = h.to_proposal_sent("acme")
    h.ok(h.approve(c2))
    used = h.view("globex", g["cid"])["funds"][0]["evidence_ref"]
    h.refused(h.funds(c2, c2["sell"], ref=used), 409, "FUNDS_EVIDENCE_REUSED")
    # an acme authorization cannot be read or submitted through globex
    a = h.ok(h.authorize(ctx), 201)
    h.refused(h.get(f"/tenants/globex/campaigns/{ctx['cid']}/authorizations/{a['authorization_id']}"), 404,
              "CAMPAIGN_NOT_FOUND")
    h.refused(h.get(f"/tenants/globex/campaigns/{g['cid']}/authorizations/{a['authorization_id']}"), 404,
              "AUTHORIZATION_NOT_FOUND")


@pytest.mark.parametrize("switch,code", [("global", "KILLED_GLOBAL"), ("write", "KILLED_WRITE"),
                                         ("tenant:acme", "KILLED_TENANT"),
                                         ("capability:campaigns", "KILLED_CAPABILITY")])
def test_each_kill_switch_stops_writes_and_only_andre_releases(h, switch, code):
    h.tenant("acme")
    h.ok(h.switch(switch, caller="compliance_38"))
    h.refused(h.post("/tenants/acme/campaigns", {"request_id": rid(), "name": "x y", "markets": ["la"]}), 403, code)
    h.refused(h.switch(switch, engaged=False), 403, "ANDRE_APPROVAL_REQUIRED")
    h.refused(h.switch(switch, engaged=False, caller="compliance_38"), 403, "ANDRE_APPROVAL_REQUIRED")
    h.ok(h.switch(switch, engaged=False, andre=True))
    h.campaign("acme")


def test_purchase_and_submission_switches_stop_the_money_path(h):
    h.tenant("acme")
    ctx = h.funded()
    h.ok(h.switch("capability:purchase", caller="ooh_agent"))       # the agent runtime may engage, never release
    h.refused(h.authorize(ctx), 403, "KILLED_CAPABILITY")
    h.ok(h.switch("capability:purchase", engaged=False, andre=True))
    a = h.ok(h.authorize(ctx), 201)
    ctx["aid"] = a["authorization_id"]
    h.ok(h.switch("capability:submission"))
    h.refused(h.submit(ctx), 403, "KILLED_CAPABILITY")
    assert h.ok(h.get(f"/tenants/acme/campaigns/{ctx['cid']}/authorizations/{ctx['aid']}"))[
        "submission_state"] == "NOT_SUBMITTED"


def test_env_kill_switches_cannot_be_released(tmp_path):
    hh = Harness(tmp_path, OOH_KILL_GLOBAL="1", OOH_KILLED_CAPABILITIES="purchase")
    sw = hh.ok(hh.get("/kill-switches"))
    assert sw["global"] is True and sw["global_by_env"] is True and sw["capabilities"]["purchase"] is True
    hh.ok(hh.switch("global", engaged=False, andre=True))
    assert hh.ok(hh.get("/kill-switches"))["global"] is True
    hh.refused(hh.post("/tenants", {"request_id": rid(), "tenant_id": "acme"}, andre=True), 403, "KILLED_GLOBAL")


def test_an_engage_the_ledger_cannot_record_still_takes_effect(h):
    h.tenant("acme")
    h.ledger.fail = True
    r = h.ok(h.switch("tenant:acme"))
    assert r["recorded"] is False and r["volatile_unrecorded"] == [{"kind": "tenant", "arg": "acme"}]
    h.ledger.fail = False
    h.refused(h.post("/tenants/acme/campaigns", {"request_id": rid(), "name": "x y", "markets": ["la"]}), 403,
              "KILLED_TENANT")


def test_idempotency_same_body_same_answer_other_body_refused(h):
    h.tenant("acme")
    body = {"request_id": rid(), "name": "Fall Billboards", "markets": ["los-angeles"]}
    a = h.ok(h.post("/tenants/acme/campaigns", body), 201)
    b = h.ok(h.post("/tenants/acme/campaigns", body), 201)
    assert a["campaign_id"] == b["campaign_id"] and len(h.svc.campaigns) == 1
    h.refused(h.post("/tenants/acme/campaigns", {**body, "name": "Other Name"}), 409, "REQUEST_ID_REUSED")


def test_personal_data_keys_are_refused_and_not_echoed(h):
    h.tenant("acme")
    ctx = h.to_proposal_sent()
    h.ok(h.approve(ctx))
    for key in ("account_number", "routing_number", "card_number", "phone"):
        r = h.funds(ctx, ctx["sell"], **{key: "021000021"})
        assert r.status_code == 422 and "021000021" not in r.text, key
    r = h.funds(ctx, ctx["sell"], method="card")
    assert r.status_code == 422                                      # vendor money: ACH (or a check), never card


def test_request_limits(h):
    r = h.client.post("/ooh/v1/tenants", content=b"x" * (70 * 1024),
                      headers={**h.headers("dashboard", andre=True), "Content-Type": "application/json"})
    assert r.status_code == 413
    r = h.client.post("/ooh/v1/tenants", content=b"tenant", headers={**h.headers("dashboard", andre=True),
                                                                     "Content-Type": "text/plain"})
    assert r.status_code == 415
    deep = b"[" * 40 + b"]" * 40
    r = h.client.post("/ooh/v1/tenants", content=deep, headers={**h.headers("dashboard", andre=True),
                                                                "Content-Type": "application/json"})
    assert r.status_code == 422
    assert h.client.get("/ooh/v1/status", headers=h.headers()).headers["cache-control"] == "no-store"
    assert h.client.get("/docs").status_code == 404 and h.client.get("/openapi.json").status_code == 404


def test_constitution_and_roster_are_data(h):
    c = h.ok(h.get("/constitution", caller="ooh_agent"))
    assert c["andre_roles"] == ["DIRECTOR_PNL", "BUYER", "CONTRACT_SIGNER"] and c["agents_commit"] is False
    assert c["principal_agent_default"] == "UNDETERMINED" and c["funding_policy"] == "CLIENT_PREPAY_REQUIRED"
    assert c["suggested_markup_applied"] is False and c["decision_self_authorizes"] is False
    assert len(c["price_types"]) == 6 and len(c["evidence_classes"]) == 9
    r = h.ok(h.get("/roster", caller="compliance_38"))
    assert r["total"] == 23 and r["built"] == 0 and len({x["capability_id"] for x in r["capabilities"]}) == 23
    cell = [x for x in r["capabilities"] if x["initial_cell"]]
    rest = [x for x in r["capabilities"] if not x["initial_cell"]]
    assert cell and all(x["status"] == policy.ROSTER_CELL_STATUS for x in cell)
    assert all(x["status"] == "REGISTERED_INACTIVE_READY" for x in rest)
    assert all(x["can_commit"] is False and x["built"] is False and x["authority"] == "PREPARE_ONLY"
               for x in r["capabilities"])
    h.refused(h.get("/roster", caller="ooh_agent"), 403, "CALLER_NOT_ALLOWED")


SECTION_40 = ["Department Coordination", "Client Fit", "Market/Geography Intelligence", "Inventory/Operator Intelligence",
              "Avails/Availability", "Audience/Mobility Intelligence", "Pricing/Transaction Evidence", "Planning",
              "Proposal/Estimation", "Procurement Liaison", "Contract/Terms Control", "Purchase Authorization Control",
              "Creative Suitability/Specs", "Production/Trafficking", "Regulatory/Permit Intelligence",
              "Programmatic DOOH", "Proof-of-Posting/Delivery Verification", "Outage/Makegood Control",
              "Measurement/Attribution", "Billing/Reconciliation", "Vendor Relationship Intelligence",
              "Quality/Error/Agent Performance", "Knowledge/Documentation/Recovery"]


def test_roster_is_the_section_40_map_and_the_section_42_cell(h):
    r = h.ok(h.get("/roster"))
    assert [x["capability"] for x in r["capabilities"]] == SECTION_40
    assert r["initial_cell"] == ["C04", "C05", "C13", "C17", "C20"]
    cell = [x for x in r["capabilities"] if x["initial_cell"]]
    assert [x["capability"] for x in cell] == ["Inventory/Operator Intelligence", "Avails/Availability",
                                               "Creative Suitability/Specs", "Proof-of-Posting/Delivery Verification",
                                               "Billing/Reconciliation"]
    assert all(x["status"] == "INITIAL_CELL" and x["build_state"] == "NOT_BUILT_YET" for x in cell)
    assert "C18" in next(x for x in cell if x["capability_id"] == "C20")["note"]
    assert all("provisional" not in json.dumps(x) for x in r["capabilities"])


SECTION_4_MAIN = ["INTAKE", "BRIEF_VALIDATED", "PLAN_DRAFTED", "PLAN_APPROVED", "AVAILS_REQUESTED", "QUOTES_VALIDATED",
                  "CLIENT_APPROVED", "FUNDING_REQUIRED", "FUNDS_VERIFIED", "HOLD_REQUESTED", "HOLD_CONFIRMED",
                  "PURCHASE_AUTHORIZED", "BOOKED", "CREATIVE_PENDING", "CREATIVE_ACCEPTED", "PRODUCTION_PENDING", "LIVE",
                  "DELIVERY_REVIEWED", "RECONCILIATION_PENDING", "RECONCILED", "CLOSED"]
SECTION_4_EXCEPTION = ["EXPIRED", "WITHDRAWN", "UNAVAILABLE", "CLIENT_HOLD", "VENDOR_HOLD", "CANCELLED",
                       "SUBSTITUTION_PROPOSED", "SUBSTITUTION_REQUIRES_APPROVAL", "PARTIAL_DELIVERY", "UNDERDELIVERY",
                       "OUTAGE", "MAKEGOOD_PENDING", "CREDIT_PENDING", "DISPUTED", "REFUND_PENDING", "FAILED", "BLOCKED"]


def test_state_vocabulary_is_the_whole_of_section_4_plus_three_marked_additions(h):
    c = h.ok(h.get("/constitution"))
    assert c["section_4_main"] == SECTION_4_MAIN and c["section_4_exception"] == SECTION_4_EXCEPTION
    assert c["section_4_derived"] == ["HOLD_REQUESTED", "HOLD_CONFIRMED"]
    assert set(c["beyond_section_4"]) == {"PROPOSAL_SENT", "ORDER_SUBMITTED", "CHANGE_REQUESTED"}
    stored = set(c["campaign_states"]) | set(c["exception_states"])
    assert stored == (set(SECTION_4_MAIN) | set(SECTION_4_EXCEPTION) | set(c["beyond_section_4"])) \
        - set(c["section_4_derived"])
    assert set(c["not_built"]) == {"CREATIVE_PENDING", "CREATIVE_ACCEPTED", "PRODUCTION_PENDING", "LIVE",
                                   "DELIVERY_REVIEWED", "RECONCILIATION_PENDING", "RECONCILED", "CLOSED", "EXPIRED",
                                   "WITHDRAWN", "UNAVAILABLE", "SUBSTITUTION_PROPOSED",
                                   "SUBSTITUTION_REQUIRES_APPROVAL", "PARTIAL_DELIVERY", "UNDERDELIVERY", "OUTAGE",
                                   "MAKEGOOD_PENDING", "CREDIT_PENDING", "REFUND_PENDING", "FAILED", "BLOCKED"}
    assert set(c["exceptions_built"]) == {"CLIENT_HOLD", "VENDOR_HOLD", "DISPUTED", "CANCELLED", "CHANGE_REQUESTED"}
    assert "IN_PRODUCTION" not in stored and "COMPLETED" not in stored


def test_ledger_down_means_nothing_takes_effect(h):
    h.tenant("acme")
    h.ledger.fail = True
    h.refused(h.post("/tenants/acme/campaigns", {"request_id": rid(), "name": "x y", "markets": ["la"]}), 503,
              "LEDGER_UNAVAILABLE")
    assert h.svc.campaigns == {}


def _full_story(h) -> dict:
    h.tenant("acme")
    h.tenant("globex")
    ctx = h.authorized()
    h.ok(h.submit(ctx))
    h.ok(h.outcome(ctx))
    other = h.funded("globex")
    h.ok(h.go("globex", other["cid"], "CLIENT_HOLD"))
    h.ok(h.switch("capability:prices"))
    return ctx


def test_restart_and_replay_rebuild_identical_state(hd):
    _full_story(hd)
    before = hd.svc.snapshot()
    h2 = hd.restart()
    assert h2.ok(h2.get("/status"))["integrity"]["ok"] is True
    assert h2.svc.snapshot() == before
    # replaying the same log from scratch, twice, gives the same state again (the log is the only source)
    h3 = h2.restart()
    assert h3.svc.snapshot() == before


def test_a_tampered_or_truncated_log_refuses_to_take_writes(hd):
    _full_story(hd)
    path = hd.svc.log.path
    hd.svc.close()
    with open(path, "rb") as fh:
        lines = fh.read().splitlines(keepends=True)
    with open(path, "wb") as fh:
        fh.write(b"".join(lines[:-3]))
    h2 = Harness(hd.tmp, hd.settings.data_dir, hd.ledger, hd.clock)
    st = h2.ok(h2.get("/status"))
    assert st["integrity"]["ok"] is False and "beyond the local log" in st["integrity"]["problem"]
    h2.refused(h2.post("/tenants", {"request_id": rid(), "tenant_id": "initech"}, andre=True), 503,
               "INTEGRITY_UNVERIFIED")


def test_evidence_view_marks_each_action_committed_exactly_once(h):
    _full_story(h)
    ev = h.ok(h.get("/audit/evidence?limit=1000", caller="compliance_38"))
    assert ev["rule"] == "unanchored evidence = attempted, not done"
    done = [(e["event_type"], e["subject_id"], e["rk"]) for e in ev["evidence"] if e["status"] == "committed"]
    assert len(done) == len(set(done)) == ev["committed"] > 0
    types = {e["event_type"] for e in ev["evidence"] if e["status"] == "committed"}
    assert {"tenant_created", "campaign_created", "campaign_transition", "inventory_added", "availability_moved",
            "price_recorded", "commercial_model_set", "client_approval_recorded_by_andre",
            "client_funds_verified_by_andre", "purchase_authorized_by_andre", "purchase_order_submitted_by_andre",
            "purchase_order_outcome_recorded", "kill_switch_set"} <= types
    # the ledger never sees vendor text or an amount in the clear: only payload hashes
    blob = json.dumps(h.ledger.entries())
    assert "lamar-hold-77" not in blob and "5000.00" not in blob
