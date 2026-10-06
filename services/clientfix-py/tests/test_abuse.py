"""Failure, boundary and abuse cases the founder decisions require (ADR 0017): out-of-allowlist operations, plan-hash
tampering, apply-then-mismatch rollback, rollback failure (Andre alerted, resource frozen), revocation mid-run, tenant
crossover, double apply, refunds only after Andre, no work before payment, re-detection disagreeing with the claim."""

from __future__ import annotations

import threading

from connectors.base import HttpAnswer
from helpers import (ANDRE, CLIENT_A, CLIENT_B, PRODUCT, PRODUCT2, SHOP_A, SHOP_B, FakeDetection, FakeFinance,
                     Harness, rid, wired_ports)
import json

from connectors import shopify as shp
from platforms import shopify_op

SEO = "Blue Hoodie | Warm Winter Wear"


def _seo_op(before=None, after=SEO, target=PRODUCT, field="seo.title", op="shopify.product.update"):
    return {"op": op, "target": target, "field": field, "before": before, "after": after}


def _paid_job(h, conn=None, target=PRODUCT, check="product_seo_missing"):
    conn = conn or h.connection()
    h.t.shop(conn["account_ref"]).product(target)
    f = h.finding(conn, check=check, target=target)
    j = h.job([f])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    return conn, j, h.item(j["job_id"])


def _plan_item(conn, item, ops):
    return [{"item_id": item["item_id"], "connection_id": conn["connection_id"], "ops": ops}]


# ------------------------------------------------------------------------------------------- 1. allowlist

def test_out_of_allowlist_operations_are_refused_and_nothing_is_stored_or_sent(h):
    conn, j, item = _paid_job(h)
    cases = [
        (_seo_op(op="shopify.theme.write"), "OP_NOT_ALLOWED"),
        (_seo_op(op="shopify.product.delete"), "OP_NOT_ALLOWED"),
        (_seo_op(field="vendor", before="Old", after="New"), "OP_FIELD_NOT_ALLOWED"),
        (_seo_op(field="variants.price", before="10.00", after="1.00"), "OP_FIELD_NOT_ALLOWED"),
        (_seo_op(field="descriptionHtml", before="<p>Warm.</p>", after="<p>x</p><script>alert(1)</script>"),
         "OP_VALUE_INVALID"),
        (_seo_op(field="descriptionHtml", before="<p>Warm.</p>", after='<img src=x onerror="steal()">'),
         "OP_VALUE_INVALID"),
        (_seo_op(field="descriptionHtml", before="<p>Warm.</p>", after='<a href="javascript:go()">x</a>'),
         "OP_VALUE_INVALID"),
        (_seo_op(field="title", before=None, after="New"), "OP_NOT_FOR_CHECK"),
        (_seo_op(after=None), "OP_VALUE_INVALID"),                         # product fields are never removed
        (_seo_op(target="gid://shopify/Order/5"), "OP_TARGET_INVALID"),
        ({"op": "shopify.redirect.set", "target": "redirect:/a", "field": "target", "before": None,
          "after": "https://evil.test/phish"}, "OP_VALUE_INVALID"),        # off-store redirect
        ({"op": "shopify.redirect.set", "target": "redirect:/a", "field": "target", "before": None,
          "after": "//evil.test/x"}, "OP_VALUE_INVALID"),
        ({"op": "shopify.metafield.set", "target": PRODUCT, "field": "metafield:custom.care", "before": None,
          "after": {"type": "json", "value": "{}"}}, "OP_VALUE_INVALID"),
        ({"op": "gtm.tag.update", "target": "accounts/1/containers/2/workspaces/3/tags/4", "field": "paused",
          "before": True, "after": False}, "OP_NOT_ALLOWED"),             # another connector's op on Shopify
    ]
    for op, code in cases:
        h.refused(h.plan(j, _plan_item(conn, item, [op])), 422, code)
    assert h.ok(h.get(f"/jobs/{j['job_id']}"))["status"] == "paid"
    assert not h.t.writes()


def test_gtm_tag_code_and_parameters_are_not_editable(h):
    gtm = h.t.gtm
    path = gtm.tag("12", ttype="html")
    conn = h.connection(connector="gtm", account="accounts/1/containers/2")
    f = h.finding(conn, check="gtm_tag_paused", target=path)
    j = h.job([f])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    item = h.item(j["job_id"])
    for field, before, after in (("parameter", [], [{"key": "html", "value": "<script>x</script>"}]),
                                 ("type", "html", "gaawe"), ("firingTriggerId", ["7"], ["9", "8"])):
        h.refused(h.plan(j, _plan_item(conn, item, [{"op": "gtm.tag.update", "target": path, "field": field,
                                                    "before": before, "after": after}])), 422)


def test_not_built_lanes_are_refused_never_quoted(h):
    r = h.post("/connections", {"request_id": rid(), "client_id": CLIENT_A, "connector": "woocommerce",
                                "account_ref": "shop.example.test", "token_ref": "vault:delivery_28.woo-1"},
               caller="hub")
    h.refused(r, 422, "CONNECTOR_NOT_BUILT")
    conn = h.connection()
    for check in ("missed_call_followup_off", "abandoned_cart_flow_off", "site_speed", "checkout_setting_wrong",
                  "ad_pixel_missing"):
        r = h.post("/findings", {"request_id": rid(), "finding_id": f"rr:{check}", "agent_id": "a",
                                 "client_id": CLIENT_A, "check_code": check,
                                 "resource": {"connection_id": conn["connection_id"], "target": PRODUCT}},
                   caller="orchestrator")
        h.refused(r, 422, "CONNECTOR_NOT_BUILT")
    # a check whose connector does not match the connection
    h.refused(h.post("/findings", {"request_id": rid(), "finding_id": "rr:x", "agent_id": "a", "client_id": CLIENT_A,
                                   "check_code": "ga4_key_event_missing",
                                   "resource": {"connection_id": conn["connection_id"], "target": "properties/1"}},
                     caller="orchestrator"), 422, "CHECK_CONNECTOR_MISMATCH")


# ------------------------------------------------------------------------------------------- 2. plan hash

def test_plan_hash_tampering_is_refused_at_approval_and_at_apply(h):
    conn, j, item = _paid_job(h)
    h.ok(h.plan(j, _plan_item(conn, item, [_seo_op()])))
    h.refused(h.approve(j["job_id"], sha="0" * 64), 409, "PLAN_HASH_MISMATCH")
    v1 = h.ok(h.get(f"/jobs/{j['job_id']}"))["plan_sha256"]
    h.ok(h.plan(j, _plan_item(conn, item, [_seo_op(after="Another title")])))         # a new version
    h.refused(h.approve(j["job_id"], sha=v1), 409, "PLAN_HASH_MISMATCH")               # the old hash binds nothing
    h.ok(h.approve(j["job_id"]))
    # the stored plan is altered after approval (a tampered record, a bug): the recomputed hash differs, nothing runs
    with h.svc.lock:
        h.svc.jobs[j["job_id"]]["items"][item["item_id"]]["ops"][0]["after"] = "Injected by an attacker"
    h.refused(h.apply(j["job_id"]), 409, "APPROVAL_STALE")
    assert not h.t.writes()


def test_a_new_plan_after_approval_voids_the_approval(h):
    conn, j, item = _paid_job(h)
    h.ok(h.plan(j, _plan_item(conn, item, [_seo_op()])))
    h.ok(h.approve(j["job_id"]))
    h.refused(h.plan(j, _plan_item(conn, item, [_seo_op(after="Changed")])), 409, "JOB_STATE")
    # only paid / planned jobs take a plan; an approved plan is changed only by cancelling (Andre)
    h.refused(h.post(f"/jobs/{j['job_id']}/cancel", {"request_id": rid()}), 403, "ANDRE_APPROVAL_REQUIRED")
    h.ok(h.post(f"/jobs/{j['job_id']}/cancel", {"request_id": rid()}, andre=True))
    assert not h.t.writes()


def test_client_approval_needs_the_clients_own_live_session(h):
    conn, j, item = _paid_job(h)
    h.ok(h.plan(j, _plan_item(conn, item, [_seo_op()])))
    sha = h.ok(h.get(f"/jobs/{j['job_id']}"))["plan_sha256"]
    body = {"request_id": rid(), "sha256": sha}
    h.refused(h.post(f"/jobs/{j['job_id']}/plan/approve", body, caller="hub"), 403, "CLIENT_SESSION_REQUIRED")
    h.refused(h.post(f"/jobs/{j['job_id']}/plan/approve", body, caller="hub", session="f" * 64), 403,
              "CLIENT_SESSION_INVALID")
    h.refused(h.post(f"/jobs/{j['job_id']}/plan/approve", body, caller="hub", session=h.session(CLIENT_B)), 403,
              "CLIENT_MISMATCH")
    h.refused(h.post(f"/jobs/{j['job_id']}/plan/approve", body, caller="dashboard", session=h.session()), 403,
              "CALLER_NOT_ALLOWED")                                        # Andre's console cannot approve for them
    h.clock.advance(minutes=31)
    h.refused(h.post(f"/jobs/{j['job_id']}/plan/approve", body, caller="hub", session=h.session()), 403,
              "CLIENT_SESSION_EXPIRED")


# ------------------------------------------------------------------------------------------- 3. mismatch -> rollback

def test_apply_then_mismatch_rolls_back_from_the_snapshot(h):
    conn, j, item = _paid_job(h)
    shop = h.t.shop(SHOP_A)
    shop.products[PRODUCT]["seo"]["title"] = "Original"
    seen = {"wrote": False, "lied": False}

    def lie_once(conn_, req, real):               # the verify read after our write shows a wrong value, once
        ans = real()
        if seen["wrote"] and not seen["lied"]:
            seen["lied"] = True
            body = json.loads(json.dumps(ans.body))
            body["data"]["product"]["seo"]["title"] = "Mangled"
            return HttpAnswer(200, body)
        return ans

    def mark(conn_, req):
        if req.is_write:
            seen["wrote"] = True
    h.t.before = mark
    h.t.rules.append((lambda c, r: isinstance(r.body, dict) and r.body.get("query") == shp.PRODUCT_READ, lie_once))
    h.ok(h.plan(j, _plan_item(conn, item, [_seo_op(before="Original")])))
    h.ok(h.approve(j["job_id"]))
    out = h.ok(h.apply(j["job_id"]))
    it = out["items"][0]
    assert it["status"] == "rolled_back" and it["result"]["failure"] == "verify_mismatch"
    assert it["result"]["rollback"] == {"outcome": "applied", "proven": True}
    assert shop.products[PRODUCT]["seo"]["title"] == "Original"                     # restored from the snapshot
    assert h.ok(h.get("/frozen"))["resources"] == []
    refunds = h.ok(h.get("/refunds"))
    assert refunds[0]["amount"] == "150.00" and refunds[0]["status"] == "proposed"


def test_a_value_nobody_planned_is_never_overwritten_by_a_rollback(h):
    """M5: the platform (or the merchant) holds a value that is neither the snapshot nor ours: it is left alone, the
    rollback is not proven, the resource frozen and Andre alerted."""
    conn, j, item = _paid_job(h)
    shop = h.t.shop(SHOP_A)
    shop.products[PRODUCT]["seo"]["title"] = "Original"

    def lie(conn_, req, real):
        ans = real()
        shop.products[PRODUCT]["seo"]["title"] = "Someone else's"
        h.t.rules.clear()
        return ans
    h.t.rules.append((shopify_op("productUpdate"), lie))
    h.ok(h.plan(j, _plan_item(conn, item, [_seo_op(before="Original")])))
    h.ok(h.approve(j["job_id"]))
    it = h.ok(h.apply(j["job_id"]))["items"][0]
    assert it["status"] == "rollback_failed" and it["result"]["rollback"] == {"outcome": "conflict", "proven": False}
    assert shop.products[PRODUCT]["seo"]["title"] == "Someone else's"
    assert h.ok(h.get("/frozen"))["resources"]
    assert any(t["code"] == "ROLLBACK_FAILED" for t in h.ok(h.get("/tasks")))


def test_an_unknown_answer_is_never_success_and_is_rolled_back(h):
    conn, j, item = _paid_job(h)
    shop = h.t.shop(SHOP_A)

    def lost(conn_, req, real):                   # applied on the platform, answer lost on the way back
        real()
        h.t.rules.clear()
        raise TimeoutError("answer lost")
    h.t.rules.append((shopify_op("productUpdate"), lost))
    h.ok(h.plan(j, _plan_item(conn, item, [_seo_op()])))
    h.ok(h.approve(j["job_id"]))
    it = h.ok(h.apply(j["job_id"]))["items"][0]
    assert it["status"] == "rolled_back" and it["result"]["failure"] == "write_unknown"
    assert shop.products[PRODUCT]["seo"]["title"] is None


def test_an_undocumented_success_shape_counts_as_unknown(h):
    conn, j, item = _paid_job(h)
    h.t.rules.append((shopify_op("productUpdate"), HttpAnswer(200, {"data": {"productUpdate": {"ok": True}}})))
    h.ok(h.plan(j, _plan_item(conn, item, [_seo_op()])))
    h.ok(h.approve(j["job_id"]))
    it = h.ok(h.apply(j["job_id"]))["items"][0]
    assert it["result"]["failure"] == "write_unknown" and it["status"] == "rolled_back"


def test_a_failed_second_write_rolls_back_the_first_in_reverse_order(h):
    conn, j, item = _paid_job(h, check="product_content_error")
    shop = h.t.shop(SHOP_A)
    n = {"w": 0}

    def second_refused(conn_, req, real):
        n["w"] += 1
        if n["w"] == 2:
            return HttpAnswer(200, {"data": {"productUpdate": {"product": None,
                                                               "userErrors": [{"field": ["x"], "message": "nope"}]}}})
        return real()
    h.t.rules.append((shopify_op("productUpdate"), second_refused))
    h.ok(h.plan(j, _plan_item(conn, item, [
        _seo_op(field="title", before="Blue Hoodie", after="Blue Hoodie 2"),
        _seo_op(field="descriptionHtml", before="<p>Warm.</p>", after="<p>Warmer.</p>")])))
    h.ok(h.approve(j["job_id"]))
    it = h.ok(h.apply(j["job_id"]))["items"][0]
    assert it["status"] == "rolled_back" and it["result"]["failure"] == "write_refused"
    assert shop.products[PRODUCT]["title"] == "Blue Hoodie"


def test_gtm_failure_after_publish_republishes_the_previous_live_version(h):
    gtm = h.t.gtm
    path = gtm.tag("12", paused=True)
    gtm.publish_initial()
    before = gtm.live
    conn = h.connection(connector="gtm", account="accounts/1/containers/2")
    f = h.finding(conn, check="gtm_tag_paused", target=path)
    j = h.job([f])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    item = h.item(j["job_id"])

    def live_lies(conn_, req, real):              # the live version read after publishing shows the old tag
        ans = real()
        if gtm.live != before:
            body = dict(ans.body)
            body["tag"] = [{**t, "paused": True} for t in body["tag"]]
            return HttpAnswer(200, body)
        return ans
    h.t.rules.append((lambda c, r: r.method == "GET" and r.url.endswith("versions:live"), live_lies))
    h.ok(h.plan(j, _plan_item(conn, item, [{"op": "gtm.tag.update", "target": path, "field": "paused",
                                            "before": True, "after": False}])))
    h.ok(h.approve(j["job_id"]))
    it = h.ok(h.apply(j["job_id"]))["items"][0]
    assert it["status"] == "rolled_back", it
    # AEGIS round 2 R2-1: a REVERT version with the snapshot's content is now both latest and live
    assert gtm.live == gtm.latest and gtm.live != before
    tags = {t["tagId"]: t for t in gtm.versions[gtm.live]["tag"]}
    assert tags["12"]["paused"] is True


# ------------------------------------------------------------------------------------------- 4. rollback failure

def test_rollback_failure_freezes_the_resource_and_alerts_andre(h):
    conn, j, item = _paid_job(h)
    shop = h.t.shop(SHOP_A)
    state = {"n": 0}

    def first_lies_then_refuse(conn_, req, real):
        state["n"] += 1
        if state["n"] == 1:
            ans = real()
            shop.products[PRODUCT]["seo"]["title"] = "Mangled"
            return ans
        return HttpAnswer(200, {"data": {"productUpdate": {"product": None,
                                                           "userErrors": [{"field": ["seo"], "message": "locked"}]}}})
    h.t.rules.append((shopify_op("productUpdate"), first_lies_then_refuse))
    h.ok(h.plan(j, _plan_item(conn, item, [_seo_op()])))
    h.ok(h.approve(j["job_id"]))
    it = h.ok(h.apply(j["job_id"]))["items"][0]
    assert it["status"] == "rollback_failed" and it["result"]["rollback"]["proven"] is False
    frozen = h.ok(h.get("/frozen"))["resources"]
    assert [f["resource_key"] for f in frozen] == [f"shopify|{SHOP_A}|{PRODUCT}"]
    tasks = h.ok(h.get("/tasks", params={"status": "open"}))
    assert any(t["code"] == "ROLLBACK_FAILED" for t in tasks)
    assert h.ledger.of_type("resource_frozen")
    # nothing touches the frozen resource again: a new job on it is refused before any request
    h.t.rules.clear()
    h.sessions.clear()
    f2 = h.finding(conn, check="product_content_error", target=PRODUCT)
    j2 = h.job([f2])
    h.ok(h.accept(j2))
    h.ok(h.pay(j2))
    it2 = h.item(j2["job_id"])
    h.ok(h.plan(j2, _plan_item(conn, it2, [_seo_op(field="title", before="Blue Hoodie", after="X")])))
    h.ok(h.approve(j2["job_id"]))
    n = len(h.t.calls)
    h.refused(h.apply(j2["job_id"]), 409, "RESOURCE_FROZEN")
    assert len(h.t.calls) == n
    # only Andre unfreezes, and only by the exact freeze hash
    h.refused(h.post("/frozen/unfreeze", {"request_id": rid(), "state_sha256": frozen[0]["freeze_sha256"]}), 403)
    h.refused(h.post("/frozen/unfreeze", {"request_id": rid(), "state_sha256": "a" * 64}, andre=True), 409,
              "STATE_HASH_MISMATCH")
    h.ok(h.post("/frozen/unfreeze", {"request_id": rid(), "state_sha256": frozen[0]["freeze_sha256"]}, andre=True))
    assert h.ok(h.apply(j2["job_id"]))["items"][0]["status"] == "applied_verified"


def test_andre_freezes_a_client_and_a_running_apply_stops(h):
    conn, j, item = _paid_job(h)
    shop = h.t.shop(SHOP_A)

    def freeze_on_first_write(conn_, req):
        if req.is_write and not h.svc.frozen_clients:
            h.ok(h.post("/clients/freeze", {"request_id": rid(), "client_id": CLIENT_A}, andre=True))
    h.t.before = freeze_on_first_write
    h.ok(h.plan(j, _plan_item(conn, item, [_seo_op(), _seo_op(field="seo.description", after="Cosy.")])))
    h.ok(h.approve(j["job_id"]))
    it = h.ok(h.apply(j["job_id"]))["items"][0]
    # Low (round 1): the first write had landed — a guarded rollback undoes it, the resource is frozen, Andre told
    assert it["status"] == "halted_frozen" and it["result"]["rollback"] == {"outcome": "applied", "proven": True}
    assert shop.products[PRODUCT]["seo"] == {"title": None, "description": None}
    assert h.ok(h.get("/frozen"))["resources"]
    assert any(t["code"] == "FROZEN_MID_APPLY" for t in h.ok(h.get("/tasks")))


# ------------------------------------------------------------------------------------------- 5. revocation

def test_revocation_mid_run_stops_all_work_for_the_client_at_once(h):
    conn, j, item = _paid_job(h)
    shop = h.t.shop(SHOP_A)

    def revoke_after_first_write(conn_, req):
        if req.is_write and conn["connection_id"] not in h.svc.revoked_now:
            h.ok(h.post(f"/connections/{conn['connection_id']}/revoke", {"request_id": rid()}, caller="hub"))
    h.t.before = revoke_after_first_write
    h.ok(h.plan(j, _plan_item(conn, item, [_seo_op(), _seo_op(field="seo.description", after="Cosy.")])))
    h.ok(h.approve(j["job_id"]))
    it = h.ok(h.apply(j["job_id"]))["items"][0]
    assert it["status"] == "halted_revoked" and it["result"]["failure"] == "CONNECTION_REVOKED"
    writes = h.t.writes()
    assert len(writes) == 1                                   # the second op was never sent, nor any rollback
    assert shop.products[PRODUCT]["seo"]["description"] is None
    assert any(t["code"] == "REVOKED_MID_APPLY" for t in h.ok(h.get("/tasks")))
    assert h.ok(h.get(f"/connections/{conn['connection_id']}"))["status"] == "revoked"
    # a revoked connection takes no new work
    h.refused(h.post("/findings", {"request_id": rid(), "finding_id": "rr:late", "agent_id": "a",
                                   "client_id": CLIENT_A, "check_code": "product_seo_missing",
                                   "resource": {"connection_id": conn["connection_id"], "target": PRODUCT}},
                     caller="orchestrator"), 409, "CONNECTION_REVOKED")


def test_revocation_cancels_planned_items_and_is_never_refused(h):
    conn, j, item = _paid_job(h)
    h.ok(h.plan(j, _plan_item(conn, item, [_seo_op()])))
    h.ok(h.approve(j["job_id"]))
    h.ledger.fail = True                                      # the ledger is down: the kill switch still holds
    r = h.post(f"/connections/{conn['connection_id']}/revoke", {"request_id": rid()}, caller="hub")
    assert r.status_code == 503 and r.json()["work_stopped"] is True
    h.ledger.fail = False
    h.ok(h.post(f"/connections/{conn['connection_id']}/revoke", {"request_id": rid()}, caller="hub"))
    h.ok(h.post(f"/connections/{conn['connection_id']}/revoke", {"request_id": rid()}, caller="hub"))   # repeat: ok
    jj = h.ok(h.get(f"/jobs/{j['job_id']}"))
    assert jj["items"][0]["status"] == "cancelled_revoked"
    assert jj["status"] == "refund_pending"                  # paid, nothing fixed: a full refund is proposed
    h.refused(h.apply(j["job_id"]), 409)
    assert not h.t.writes()


# ------------------------------------------------------------------------------------------- 6. tenants

def test_tenant_crossover_is_refused_everywhere(h):
    a = h.connection(client=CLIENT_A, account=SHOP_A)
    b = h.connection(client=CLIENT_B, account=SHOP_B)
    h.t.shop(SHOP_A).product(PRODUCT)
    h.t.shop(SHOP_B).product(PRODUCT2)
    # a finding for B on A's connection
    h.refused(h.post("/findings", {"request_id": rid(), "finding_id": "rr:x1", "agent_id": "a", "client_id": CLIENT_B,
                                   "check_code": "product_seo_missing",
                                   "resource": {"connection_id": a["connection_id"], "target": PRODUCT}},
                     caller="orchestrator"), 409, "TENANT_MISMATCH")
    # a job for B quoting A's finding
    fa = h.finding(a)
    h.refused(h.post("/jobs", {"request_id": rid(), "client_id": CLIENT_B,
                               "items": [{"finding_id": fa["finding_id"], "price": "10.00"}]},
                     caller="clientfix_agent"), 409, "TENANT_MISMATCH")
    # B's paid job whose plan names A's connection
    fb = h.finding(b, target=PRODUCT2)
    jb = h.job([fb])
    h.ok(h.accept(jb))
    h.ok(h.pay(jb))
    ib = h.item(jb["job_id"])
    h.refused(h.plan(jb, [{"item_id": ib["item_id"], "connection_id": a["connection_id"], "ops": [_seo_op()]}]), 409,
              "TENANT_MISMATCH")
    # A's shop connected again for B, or A's vault reference reused
    h.refused(h.post("/connections", {"request_id": rid(), "client_id": CLIENT_B, "connector": "shopify",
                                      "account_ref": SHOP_A, "token_ref": "vault:delivery_28.other-ref",
                                      "scopes": a["scopes"]}, caller="hub"), 409, "ACCOUNT_BOUND_ELSEWHERE")
    tok = h.svc.connections[a["connection_id"]]["token_ref"]
    h.refused(h.post("/connections", {"request_id": rid(), "client_id": CLIENT_B, "connector": "shopify",
                                      "account_ref": "zbest-c.myshopify.com", "token_ref": tok,
                                      "scopes": a["scopes"]}, caller="hub"), 409, "TOKEN_REF_IN_USE")
    # B's own plan runs only against B's shop
    h.ok(h.plan(jb, [{"item_id": ib["item_id"], "connection_id": b["connection_id"],
                      "ops": [_seo_op(target=PRODUCT2)]}]))
    h.ok(h.approve(jb["job_id"], client=CLIENT_B))
    h.ok(h.apply(jb["job_id"]))
    assert {c.account_ref for c, _ in h.t.calls} == {SHOP_B}
    assert {c.client_id for c, _ in h.t.calls} == {CLIENT_B}
    assert all(SHOP_B in r.url for _, r in h.t.calls)
    assert h.t.shop(SHOP_A).products[PRODUCT]["seo"]["title"] is None


def test_a_gtm_or_ga4_target_outside_the_connected_account_is_a_tenant_mismatch(h):
    conn = h.connection(connector="gtm", account="accounts/1/containers/2")
    other = "accounts/9/containers/9/tags/1"
    f = h.finding(conn, check="gtm_tag_paused", target=h.t.gtm.tag("12"))
    j = h.job([f])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    item = h.item(j["job_id"])
    h.refused(h.plan(j, _plan_item(conn, item, [{"op": "gtm.tag.update", "target": other, "field": "paused",
                                                "before": True, "after": False}])), 422, "TENANT_MISMATCH")
    g = h.connection(connector="ga4", account="properties/1")
    f2 = h.finding(g, check="ga4_key_event_missing", target="properties/1")
    j2 = h.job([f2])
    h.ok(h.accept(j2))
    h.ok(h.pay(j2))
    i2 = h.item(j2["job_id"])
    h.refused(h.plan(j2, _plan_item(g, i2, [{"op": "ga4.key_event.set", "target": "properties/2",
                                             "field": "key_event:purchase", "before": None,
                                             "after": {"countingMethod": "ONCE_PER_EVENT"}}])), 422, "TENANT_MISMATCH")


# ------------------------------------------------------------------------------------------- 7. double apply

def test_double_apply_sequential_and_concurrent(h):
    conn, j = h.seo_job()
    seen = {}

    def second_apply(conn_, req):
        if req.is_write and "again" not in seen:
            seen["again"] = h.apply(j["job_id"])
    h.t.before = second_apply
    h.ok(h.apply(j["job_id"]))
    h.refused(seen["again"], 409, "APPLY_IN_PROGRESS")
    h.refused(h.apply(j["job_id"]), 409, "JOB_STATE")
    assert len(h.t.writes()) == 1


def test_two_jobs_never_apply_to_one_resource_at_once(h):
    conn = h.connection()
    h.t.shop(SHOP_A).product(PRODUCT)
    jobs = []
    for check, field, before, after in (("product_seo_missing", "seo.title", None, "SEO"),
                                        ("product_content_error", "title", "Blue Hoodie", "Blue Hoodie 2")):
        f = h.finding(conn, check=check)
        j = h.job([f])
        h.ok(h.accept(j))
        h.ok(h.pay(j))
        it = h.item(j["job_id"])
        h.ok(h.plan(j, _plan_item(conn, it, [_seo_op(field=field, before=before, after=after)])))
        h.ok(h.approve(j["job_id"]))
        jobs.append(j)
    gate, done, other = threading.Event(), threading.Event(), {}

    def hold(conn_, req):
        if req.is_write and not gate.is_set():
            gate.set()
            other["r"] = h.apply(jobs[1]["job_id"])          # while job 1 holds the lease
    h.t.before = hold
    t = threading.Thread(target=lambda: (h.apply(jobs[0]["job_id"]), done.set()))
    t.start()
    t.join(30)
    assert done.is_set()
    h.refused(other["r"], 409, "RESOURCE_LEASED")
    assert h.ledger.of_type("lease_acquired") and h.ledger.of_type("lease_released")
    h.t.before = None
    assert h.ok(h.apply(jobs[1]["job_id"]))["items"][0]["status"] == "applied_verified"   # free again afterwards


# ------------------------------------------------------------------------------------------- 8. refunds

def test_refund_only_after_andre_approves_by_hash(tmp_path):
    fin = FakeFinance("delivered")
    h = Harness(tmp_path, ports=wired_ports(finance=fin, detection=FakeDetection("present")))
    conn, j = h.seo_job()
    h.ok(h.apply(j["job_id"]))
    h.ok(h.tick("redetect"))
    r = h.ok(h.get("/refunds"))[0]
    assert r["status"] == "proposed" and r["amount"] == "150.00"
    h.ok(h.tick("refunds"))
    assert fin.refunds == []                                  # nothing goes to Finance before Andre
    body = {"request_id": rid(), "sha256": r["refund_sha256"]}
    h.refused(h.post(f"/refunds/{r['refund_id']}/approve", body), 403, "ANDRE_APPROVAL_REQUIRED")
    h.refused(h.post(f"/refunds/{r['refund_id']}/approve", body, caller="scheduler",
                     andre=True), 403, "CALLER_NOT_ALLOWED")
    h.refused(h.post(f"/refunds/{r['refund_id']}/approve", {**body, "sha256": "b" * 64}, andre=True), 409,
              "REFUND_HASH_MISMATCH")
    bad = h.client.post(f"/cfx/v1/refunds/{r['refund_id']}/approve", json=body,
                        headers={**h.headers("dashboard"), "X-Andre-Approval-Token": ANDRE[:-1] + "0"})
    h.refused(bad, 403, "ANDRE_APPROVAL_INVALID")
    assert h.ledger.of_type("founder_approval_refused")
    h.ok(h.post(f"/refunds/{r['refund_id']}/approve", body, andre=True))
    assert fin.refunds == []
    h.ok(h.tick("refunds"))
    assert len(fin.refunds) == 1 and fin.refunds[0][1]["amount"] == "150.00"
    assert h.ok(h.get("/refunds"))[0]["status"] == "with_finance"
    assert h.ok(h.get(f"/jobs/{j['job_id']}"))["status"] == "closed"
    h.ok(h.tick("refunds"))
    assert len(fin.refunds) == 1                              # never sent twice


def test_an_unknown_refund_answer_is_never_resent_and_is_reconciled(tmp_path):
    fin = FakeFinance(TimeoutError("lost"))
    h = Harness(tmp_path, ports=wired_ports(finance=fin, detection=FakeDetection("present")))
    conn, j = h.seo_job()
    h.ok(h.apply(j["job_id"]))
    h.ok(h.tick("redetect"))
    r = h.ok(h.get("/refunds"))[0]
    h.ok(h.post(f"/refunds/{r['refund_id']}/approve", {"request_id": rid(), "sha256": r["refund_sha256"]}, andre=True))
    h.ok(h.tick("refunds"))
    assert h.ok(h.get("/refunds"))[0]["status"] == "sending"
    h.ok(h.tick("refunds"))
    assert len(fin.refunds) == 1                              # reconciled, not resent
    fin.status_answer = "delivered"
    h.ok(h.tick("refunds"))
    assert h.ok(h.get("/refunds"))[0]["status"] == "with_finance" and len(fin.refunds) == 1


def test_refund_stays_queued_while_finance_is_not_wired(tmp_path):
    p = wired_ports(detection=FakeDetection("present"))
    from ports import NotWiredFinance
    p.finance = NotWiredFinance()
    h = Harness(tmp_path, ports=p)
    conn, j = h.seo_job()
    h.ok(h.apply(j["job_id"]))
    h.ok(h.tick("redetect"))
    r = h.ok(h.get("/refunds"))[0]
    h.ok(h.post(f"/refunds/{r['refund_id']}/approve", {"request_id": rid(), "sha256": r["refund_sha256"]}, andre=True))
    assert h.ok(h.tick("refunds"))["not_wired"] == 1
    assert h.ok(h.get("/refunds"))[0]["status"] == "queued"


# ------------------------------------------------------------------------------------------- 9. payment first

def test_no_work_before_payment(h):
    conn = h.connection()
    h.t.shop(SHOP_A).product(PRODUCT)
    f = h.finding(conn)
    j = h.job([f])
    item = h.item(j["job_id"])
    plan = _plan_item(conn, item, [_seo_op()])
    h.refused(h.plan(j, plan), 409, "PAYMENT_REQUIRED")
    h.refused(h.post(f"/jobs/{j['job_id']}/engage", {"request_id": rid()}, caller="clientfix_agent"), 409,
              "PAYMENT_REQUIRED")
    h.refused(h.get(f"/jobs/{j['job_id']}/brief", caller="fire_team"), 409, "PAYMENT_REQUIRED")
    h.refused(h.apply(j["job_id"]), 409, "PAYMENT_REQUIRED")
    h.refused(h.pay(j), 409, "QUOTE_NOT_ACCEPTED")
    h.refused(h.post(f"/jobs/{j['job_id']}/quote/accept", {"request_id": rid(), "sha256": "c" * 64}, caller="hub",
                     session=h.session()), 409, "QUOTE_HASH_MISMATCH")
    h.ok(h.accept(j))
    h.refused(h.plan(j, plan), 409, "PAYMENT_REQUIRED")
    # AEGIS round 3 L3: a payment that does not match the quote never pays the job; it is recorded and refunded
    for bad in (h.pay(j, amount="149.99"), h.pay(j, quote_sha="d" * 64)):
        assert h.ok(bad)["status"] == "accepted" and h.ok(bad)["payment"] is None
    assert sorted(r["reason"] for r in h.ok(h.get("/refunds"))) == ["payment_mismatch", "payment_mismatch"]
    r = h.post("/finance/events", {"request_id": rid(), "finance_event_id": "fin-evt-" + "a" * 40,
                                   "job_id": j["job_id"], "kind": "payment_confirmed", "amount": 150.0,
                                   "currency": "USD", "quote_sha256": j["quote_sha256"]}, caller="finance_31")
    h.refused(r, 422)                                         # a float is never money
    ev = "fin-evt-" + "b" * 40
    h.ok(h.pay(j, ev=ev))
    h.ok(h.pay(j, ev=ev))                                     # the same event again: answered, nothing new
    h.refused(h.pay(j, ev=ev, amount="1.00"), 409, "FINANCE_EVENT_REUSED")
    dup = h.ok(h.pay(j, amount=j["quote"]["total"]))          # paid twice: recorded and proposed for refund
    assert len(dup["orphan_payments"]) == 3 and len(h.ok(h.get("/refunds"))) == 3     # 2 mismatched + the duplicate
    assert not h.t.calls
    assert len(h.ledger.of_type("payment_confirmed")) == 1 and len(h.ledger.of_type("payment_orphaned")) == 3


def test_payment_from_anyone_but_finance_is_refused(h):
    conn = h.connection()
    f = h.finding(conn)
    j = h.job([f])
    h.ok(h.accept(j))
    for caller in ("hub", "dashboard", "clientfix_agent", "fire_team", "scheduler"):
        r = h.post("/finance/events", {"request_id": rid(), "finance_event_id": "fin-evt-" + "c" * 40,
                                       "job_id": j["job_id"], "kind": "payment_confirmed", "amount": "150.00",
                                       "currency": "USD", "quote_sha256": j["quote_sha256"]}, caller=caller)
        h.refused(r, 403, "CALLER_NOT_ALLOWED")


# ------------------------------------------------------------------------------------------- 10. re-detection

def test_redetection_that_disagrees_with_the_claimed_fix_is_not_fixed(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(detection=FakeDetection("present")))
    conn, j = h.seo_job()
    assert h.ok(h.apply(j["job_id"]))["items"][0]["status"] == "applied_verified"
    assert h.ok(h.tick("redetect"))["present"] == 1
    jj = h.ok(h.get(f"/jobs/{j['job_id']}"))
    it = jj["items"][0]
    assert it["status"] == "not_cleared" and it["redetection"]["verdict"] == "present"
    assert jj["report"]["items"][0]["fixed"] is False
    assert any(t["code"] == "REDETECTION_DISAGREES" for t in h.ok(h.get("/tasks")))
    assert h.ok(h.get("/refunds"))[0]["amount"] == "150.00"


def test_unknown_redetection_never_counts_fixed_and_opens_one_task(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(detection=FakeDetection("unknown")), CFX_UNKNOWN_TICKS_BEFORE_TASK="3")
    conn, j = h.seo_job()
    h.ok(h.apply(j["job_id"]))
    for _ in range(6):
        h.ok(h.tick("redetect"))
    it = h.item(j["job_id"])
    assert it["status"] == "applied_verified" and it["unknown_ticks"] == 3       # counting stops at the task
    tasks = [t for t in h.ok(h.get("/tasks")) if t["code"] == "REDETECTION_UNKNOWN"]
    assert len(tasks) == 1
    assert h.ok(h.get(f"/jobs/{j['job_id']}"))["report"] is None
    # Andre ends it, by its state hash: unfixed, refundable
    sha = h.svc.item_state_sha(h.svc.jobs[j["job_id"]]["items"][it["item_id"]])
    h.refused(h.post(f"/jobs/{j['job_id']}/items/{it['item_id']}/close", {"request_id": rid(), "state_sha256": "e" * 64},
                     andre=True), 409, "STATE_HASH_MISMATCH")
    h.ok(h.post(f"/jobs/{j['job_id']}/items/{it['item_id']}/close", {"request_id": rid(), "state_sha256": sha},
                andre=True))
    assert h.ok(h.get("/refunds"))[0]["amount"] == "150.00"


def test_the_default_detection_port_answers_unknown(tmp_path):
    from ports import NotWiredDetection
    p = wired_ports()
    p.detection = NotWiredDetection()
    h = Harness(tmp_path, ports=p)
    conn, j = h.seo_job()
    h.ok(h.apply(j["job_id"]))
    assert h.ok(h.tick("redetect"))["unknown"] == 1
    assert h.item(j["job_id"])["status"] == "applied_verified"


# ------------------------------------------------------------------------------------------- stand-ins

def test_default_ports_fail_closed_before_anything_is_touched(tmp_path):
    from ports import Ports
    h = Harness(tmp_path, ports=Ports.default())
    conn = h.connection()
    f = h.finding(conn)
    j = h.job([f])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    h.refused(h.post(f"/jobs/{j['job_id']}/engage", {"request_id": rid()}, caller="clientfix_agent"), 503,
              "MODEL_NOT_WIRED")
    item = h.item(j["job_id"])
    n = len(h.ledger.events)
    # a plan needs the store's current values (AEGIS round 2 R2-4): with no transport there is no plan at all
    h.refused(h.plan(j, _plan_item(conn, item, [_seo_op()])), 503, "CONNECTOR_NOT_WIRED")
    h.refused(h.apply(j["job_id"]), 409, "PLAN_REQUIRED")
    assert len(h.ledger.events) == n                          # nothing recorded, nothing touched
    assert h.ok(h.tick("apply-queue"))["not_wired"] == 0                 # no approved job exists to apply
    st = h.ok(h.get("/status"))
    assert not any(st["ports_wired"].values())


def test_engineer_failures_are_unavailable_not_success(tmp_path):
    from helpers import FakeEngineers, NotWiredButNamedEngineers
    for eng, code in ((NotWiredButNamedEngineers(), "MODEL_NOT_WIRED"),
                      (FakeEngineers(raises=RuntimeError("boom")), "ENGINEERS_FAILED")):
        h = Harness(tmp_path / code, ports=wired_ports(engineers=eng))
        conn, j, item = _paid_job(h)
        h.refused(h.post(f"/jobs/{j['job_id']}/engage", {"request_id": rid()}, caller="clientfix_agent"), 503, code)
        assert h.ok(h.get(f"/jobs/{j['job_id']}"))["status"] == "paid"


def test_a_malicious_engineer_proposal_is_validated_like_any_plan(tmp_path):
    from helpers import FakeEngineers
    eng = FakeEngineers()
    h = Harness(tmp_path, ports=wired_ports(engineers=eng))
    conn, j, item = _paid_job(h)
    theme = {k: v for k, v in _seo_op(op="shopify.theme.write").items() if k != "before"}
    eng.items = [{"item_id": item["item_id"], "connection_id": conn["connection_id"], "ops": [theme]}]
    h.refused(h.post(f"/jobs/{j['job_id']}/engage", {"request_id": rid()}, caller="clientfix_agent"), 422,
              "OP_NOT_ALLOWED")
    eng.items = [{"item_id": item["item_id"], "connection_id": conn["connection_id"],
                  "ops": [_seo_op()], "password": "x"}]
    h.refused(h.post(f"/jobs/{j['job_id']}/engage", {"request_id": rid()}, caller="clientfix_agent"), 422)
    assert not h.t.writes()                                   # the brief READ the store; nothing was written
