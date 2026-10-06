"""AEGIS round 3 on 407d2db (not blocking; GTM stays unwired until M1-M3 are fixed). The reviewer's ``test_defect_*``
probes asserted each defect; these tests assert the FIX. 16 of the first 17 fail on 407d2db; the 17th,
``test_m2_coverage_*``, is the positive half of M2 (our own exact version is still recognised). The five Finance-path
tests at the end (the follow-up: every payment the service cannot take is recorded) all fail on 67c1eb8."""

from __future__ import annotations

import copy
import re
from pathlib import Path

from connectors.base import HttpAnswer
from connectors.tag_manager import RUN_PREFIX, NotOurs, TagManagerConnector
from helpers import GOOGLE_SCOPES, PRODUCT, SHOP_A, rid

import pytest

CONT = "accounts/1/containers/2"
ADR = Path(__file__).resolve().parents[3] / "docs" / "adr" / "0017-client-fix-lane-architecture.md"


def _paid(h, conn, check, target, field=None):
    f = h.finding(conn, check=check, target=target, field=field)
    j = h.job([f])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    return j, h.item(j["job_id"])


def _gtm_job(h, conn=None, check="gtm_tag_paused", tag="11", field="paused", after=False):
    conn = conn or h.connection(connector="gtm", account=CONT, scopes=GOOGLE_SCOPES["gtm"])
    target = f"{CONT}/tags/{tag}"
    j, it = _paid(h, conn, check, target)
    h.ok(h.plan(j, [{"item_id": it["item_id"], "connection_id": conn["connection_id"],
                     "ops": [{"op": "gtm.tag.update", "target": target, "field": field, "after": after}]}]))
    h.ok(h.approve(j["job_id"]))
    return conn, j


def _setup(h):
    gtm = h.t.gtm
    gtm.tag("11", paused=True)
    gtm.tag("12", paused=False)
    gtm.publish_initial()
    return gtm


def _live_tags(gtm):
    return {t["tagId"]: t for t in gtm.versions[gtm.live].get("tag", [])}


def _lose_first_publish(h):
    st = {"done": False}

    def act(c, r, real):
        st["done"] = True
        real()
        raise TimeoutError("lost")
    h.t.rules.append((lambda c, r: not st["done"] and ":publish?" in r.url, act))


def _tasks(h):
    return h.ok(h.get("/tasks"))


def _client_draft_synced_into(h, gtm, which: str) -> dict:
    """At OUR create_version (``which``: "fix" or "revert" run workspace), the client creates — does not publish — a
    version pausing tag 12, and the documented sync of create_version ("syncing the workspace to the latest
    container version") carries it into our workspace's unmodified entities."""
    st: dict = {}

    def pred(c, r):
        if "v" in st or not r.url.endswith(":create_version"):
            return False
        ws = int(re.search(r"/workspaces/([0-9]+):create_version", r.url).group(1))
        return f"-{which}-" in gtm.workspaces[ws]["name"]

    def act(c, r, real):
        gtm.tags["12"]["paused"] = True
        st["v"] = gtm._version("client draft awaiting sign-off", gtm.tags)
        ws = int(re.search(r"/workspaces/([0-9]+):create_version", r.url).group(1))
        w = gtm.workspaces[ws]
        latest = {t["tagId"]: t for t in gtm.versions[st["v"]]["tag"]}
        base = gtm._base_tags(ws)
        for tid, t in list(w["tags"].items()):
            if gtm._strip(t) == base.get(tid):
                w["tags"][tid] = {**copy.deepcopy(latest[tid]), "path": t["path"], "fingerprint": gtm._fp()}
        w["base"] = st["v"]
        return real()
    h.t.rules.append((pred, act))
    return st


def _orphan(h, gtm, conn=None):
    """A real run whose fix workspace is left behind with NO change in it: the tag PUT is refused (nothing written)
    and every DELETE fails, so rollback and cleanup cannot remove it. Its name was recorded before its create."""
    st = {"on": True}
    h.t.rules.append((lambda c, r: st["on"] and r.method == "PUT" and "tagmanager" in r.url,
                      HttpAnswer(400, {"error": {"code": 400}})))
    h.t.rules.append((lambda c, r: st["on"] and r.method == "DELETE", HttpAnswer(500, {"error": {"code": 500}})))
    conn, j = _gtm_job(h, conn=conn)
    it = h.ok(h.apply(j["job_id"]))["items"][0]
    st["on"] = False
    assert it["status"] == "rolled_back", it
    ws = [k for k, w in gtm.workspaces.items() if w["name"].startswith(RUN_PREFIX)]
    assert len(ws) == 1
    return conn, j, ws[0]


# ============================================================================== M1 create_version syncs to latest

def test_m1_a_version_carrying_a_synced_client_draft_is_never_published(h):
    gtm = _setup(h)
    baseline = gtm.live
    conn, j = _gtm_job(h)
    st = _client_draft_synced_into(h, gtm, "fix")
    it = h.ok(h.apply(j["job_id"]))["items"][0]
    assert gtm.live == baseline                                   # nothing of ours or theirs went live
    assert _live_tags(gtm)["12"].get("paused", False) is False    # the client's unpublished draft is NOT live
    assert not [r for _, r in h.t.calls if ":publish" in r.url]   # not published at all
    assert it["status"] == "rollback_failed"
    ours = gtm.latest
    assert ours != st["v"] and gtm.versions[ours]["name"].startswith("zbm-clientfix ")
    poisoned = [t for t in _tasks(h) if t["code"] == "GTM_VERSION_POISONED"]
    assert [t["ref"] for t in poisoned] == [ours]
    # M1: no revert is ever built on a version holding unplanned content
    assert not [r for _, r in h.t.calls if r.method == "POST" and r.url.endswith("/workspaces")
                and "-revert-" in (r.body or {}).get("name", "")]


def test_m1_a_revert_carrying_a_synced_client_draft_is_never_published(h):
    gtm = _setup(h)
    baseline = gtm.live
    conn, j = _gtm_job(h)
    _lose_first_publish(h)                                        # our version goes live; rollback builds a revert
    _client_draft_synced_into(h, gtm, "revert")
    it = h.ok(h.apply(j["job_id"]))["items"][0]
    assert gtm.live == baseline                                   # the snapshot re-published, nothing after it
    assert _live_tags(gtm)["12"].get("paused", False) is False
    assert it["status"] == "rollback_failed"
    revert = gtm.latest
    assert gtm.versions[revert]["name"].startswith("zbm-clientfix revert ")
    assert [t["ref"] for t in _tasks(h) if t["code"] == "GTM_VERSION_POISONED"] == [revert]


# ============================================================================== M2 exact name and exact values

def test_m2_a_client_release_is_never_taken_for_ours(h):
    gtm = _setup(h)
    conn, j = _gtm_job(h)
    rel = {}

    def act(c, r, real):
        gtm.tags["11"].pop("paused", None)          # the client's own unpause of tag 11: the same value as our plan
        rel["v"] = gtm._version("client release 2026-10-06", gtm.tags)
        gtm.live = rel["v"]
        raise TimeoutError("lost")                  # our create_version never landed
    h.t.rules.append((lambda c, r: r.url.endswith(":create_version") and "v" not in rel, act))
    it = h.ok(h.apply(j["job_id"]))["items"][0]
    assert gtm.live == rel["v"]                                   # their release stays live
    assert _live_tags(gtm)["11"].get("paused", False) is False
    assert it["status"] == "rollback_failed" and it["result"]["rollback"]["outcome"] == "conflict"
    assert not [r for _, r in h.t.calls if ":publish" in r.url]


def _ctx_for_identify():
    c = TagManagerConnector()
    snap = {"path": f"{CONT}/versions/2", "containerVersionId": "2",
            "tag": [{"tagId": "11", "name": "t", "type": "gaawe", "paused": True, "firingTriggerId": ["7"]}]}
    ctx = {"live_before_id": "2", "live_before_version": snap, "planned": {"11": {"paused"}},
           "planned_after": {("11", "paused"): False}, "version_name": "zbm-clientfix itm 0a0b0c0d0e0f"}
    return c, ctx


def _answers(version):
    def call(req):
        if req.url.endswith("version_headers:latest"):
            return HttpAnswer(200, {"path": version["path"], "containerVersionId": version["containerVersionId"]})
        return HttpAnswer(200, copy.deepcopy(version))
    return call


def test_m2_identify_ours_requires_the_exact_name_and_every_planned_value():
    c, ctx = _ctx_for_identify()
    ours = {"path": f"{CONT}/versions/3", "containerVersionId": "3", "name": ctx["version_name"],
            "tag": [{"tagId": "11", "name": "t", "type": "gaawe", "firingTriggerId": ["7"]}]}
    for other in ({**ours, "name": "client release"},                                       # same content, not ours
                  {**ours, "name": ctx["version_name"] + " "},
                  {**ours, "tag": [{**ours["tag"][0], "paused": True}]},                      # planned value not ours
                  {**ours, "tag": [{**ours["tag"][0], "firingTriggerId": ["8"]}]}):           # unplanned content
        with pytest.raises(NotOurs):
            c._identify_ours(CONT, dict(ctx), _answers(other))


def test_m2_coverage_identify_ours_accepts_our_exact_version():
    c, ctx = _ctx_for_identify()
    ours = {"path": f"{CONT}/versions/3", "containerVersionId": "3", "name": ctx["version_name"],
            "tag": [{"tagId": "11", "name": "t", "type": "gaawe", "firingTriggerId": ["7"]}]}
    assert c._identify_ours(CONT, ctx, _answers(ours)) == ours["path"]


# ============================================================================== M3 the reaper

def test_m3_a_run_prefixed_workspace_not_recorded_on_the_ledger_is_never_deleted(h):
    gtm = _setup(h)
    h.connection(connector="gtm", account=CONT, scopes=GOOGLE_SCOPES["gtm"])
    tags = {t["tagId"]: t for t in gtm.versions[gtm.live]["tag"]}
    plain = gtm._new_ws(RUN_PREFIX + "fix-abc123-0f0f0f0f0f0f", tags)
    edited = gtm._new_ws(RUN_PREFIX + "fix-abc124-0f0f0f0f0f0f", tags)
    gtm.workspaces[edited]["tags"]["12"]["paused"] = True
    out = h.ok(h.tick("recover"))
    assert out["workspaces_reaped"] == 0
    assert plain in gtm.workspaces and edited in gtm.workspaces
    assert not [r for _, r in h.t.calls if r.method == "DELETE"]


def test_m3_coverage_a_recorded_orphan_of_a_settled_run_with_no_change_is_reaped(h):
    gtm = _setup(h)
    conn, j, ws = _orphan(h, gtm)
    theirs = gtm._new_ws("Marketing team workspace", {})
    out = h.ok(h.tick("recover"))
    assert out["workspaces_reaped"] == 1
    assert ws not in gtm.workspaces and theirs in gtm.workspaces and gtm.default_ws in gtm.workspaces
    assert h.ledger.of_type("reaper_request_sending")
    status_reads = [r for _, r in h.t.calls if r.method == "GET" and r.url.endswith(f"/workspaces/{ws}/status")]
    assert status_reads                                           # getStatus asked before the DELETE


def test_m3_a_recorded_orphan_someone_worked_in_is_held_for_andre_not_deleted(h):
    gtm = _setup(h)
    conn, j, ws = _orphan(h, gtm)
    gtm.workspaces[ws]["tags"]["12"]["paused"] = True             # the client's staff picked it up and edited
    out = h.ok(h.tick("recover"))
    assert out["workspaces_reaped"] == 0 and ws in gtm.workspaces
    held = [t for t in _tasks(h) if t["code"] == "GTM_RUN_WORKSPACE_CHANGED"]
    assert [t["ref"] for t in held] == [gtm.ws_path(ws)]
    h.ok(h.tick("recover"))                                       # held: never deleted, the task not duplicated
    assert ws in gtm.workspaces
    assert len([t for t in _tasks(h) if t["code"] == "GTM_RUN_WORKSPACE_CHANGED"]) == 1


def test_m3_a_recorded_workspace_of_a_run_that_has_not_settled_is_left_alone(h):
    gtm = _setup(h)
    conn, j, ws = _orphan(h, gtm)
    h.svc._running.add(j["job_id"])                               # that run is still going in this process
    try:
        assert h.ok(h.tick("recover"))["workspaces_reaped"] == 0
    finally:
        h.svc._running.discard(j["job_id"])
    assert ws in gtm.workspaces


def test_m3_the_adr_states_the_narrowed_reaper():
    text = ADR.read_text()
    assert "recorded on the ledger BEFORE the create request" in text
    assert "getStatus" in text and "GTM_RUN_WORKSPACE_CHANGED" in text


# ============================================================================== L1 lease re-checked before DELETE

def test_l1_a_run_that_starts_while_the_reaper_lists_keeps_its_workspace(h):
    gtm = _setup(h)
    conn, j, ws = _orphan(h, gtm)
    svc = h.svc
    key = svc.connectors["gtm"].lease_key(CONT, "")
    st = {}

    def act(c, r, real):
        st["x"] = 1
        with svc.lock:                                            # a concurrent apply commits its lease now
            svc.lease_by_resource[key] = "lse-concurrent"
        return real()
    h.t.rules.append((lambda c, r: r.method == "GET" and r.url.endswith(f"{CONT}/workspaces") and "x" not in st,
                      act))
    n = len(h.t.calls)
    try:
        svc.reap_run_workspaces()
    finally:
        with svc.lock:
            svc.lease_by_resource.pop(key, None)
    assert ws in gtm.workspaces
    assert not [r for _, r in h.t.calls[n:] if r.method == "DELETE"]


def test_l1_an_apply_is_refused_while_the_reaper_holds_the_container(h):
    gtm = _setup(h)
    conn, j = _gtm_job(h)
    key = h.svc.connectors["gtm"].lease_key(CONT, "")
    h.svc._reaper_holding.add(key)
    try:
        h.refused(h.apply(j["job_id"]), 409, "RESOURCE_LEASED")
    finally:
        h.svc._reaper_holding.discard(key)
    assert gtm.live == gtm.latest


# ============================================================================== L2 a halt still names the version

def test_l2_a_revocation_during_the_revert_keeps_the_poisoned_version_task(h):
    gtm = _setup(h)
    conn, j = _gtm_job(h)
    _lose_first_publish(h)
    st = {}

    def act(c, r, real):
        st["x"] = 1
        ans = real()
        h.svc.revoked_now.add(conn["connection_id"])
        return ans
    h.t.rules.append((lambda c, r: r.method == "POST" and r.url.endswith(f"{CONT}/workspaces") and r.body
                      and "-revert-" in r.body.get("name", "") and "x" not in st, act))
    it = h.ok(h.apply(j["job_id"]))["items"][0]
    assert it["status"] == "halted_revoked"
    assert gtm.latest != gtm.live
    poisoned = [t for t in _tasks(h) if t["code"] == "GTM_VERSION_POISONED"]
    assert [t["ref"] for t in poisoned] == [gtm.latest]


# ============================================================================== L3 a mismatched payment

def test_l3_a_mismatched_payment_is_recorded_and_its_refund_proposed(h):
    conn = h.connection()
    h.t.shop(SHOP_A).product(PRODUCT)
    f = h.finding(conn, target=PRODUCT)
    j = h.job([f])
    h.ok(h.accept(j))
    out = h.ok(h.pay(j, amount="1.00"))
    assert out["status"] == "accepted" and out["payment"] is None          # the job is NOT paid by it
    assert [p["amount"] for p in out["orphan_payments"]] == ["1.00"]
    refunds = h.ok(h.get("/refunds"))
    assert len(refunds) == 1 and refunds[0]["kind"] == "orphaned_payment" and refunds[0]["amount"] == "1.00"
    assert refunds[0]["reason"] == "payment_mismatch" and refunds[0]["status"] == "proposed"
    assert "ORPHANED_PAYMENT" in [t["code"] for t in _tasks(h)]
    h.ok(h.pay(j))                                                # the right payment still pays the job
    assert h.ok(h.get(f"/jobs/{j['job_id']}"))["status"] == "paid"


# ============================================================================== L4 accepted risk, detected

def test_l4_a_release_un_published_by_our_re_publish_is_named_for_andre(h):
    gtm = _setup(h)
    baseline = gtm.live
    conn, j = _gtm_job(h)
    _lose_first_publish(h)
    st = {}

    def act(c, r, real):
        gtm.tags["12"]["paused"] = True
        st["rel"] = gtm._version("client release", gtm.tags)
        gtm.live = st["rel"]                                      # the client publishes right before our publish
        return real()
    h.t.rules.append((lambda c, r: r.url.endswith(f"{baseline}:publish") and "rel" not in st, act))
    it = h.ok(h.apply(j["job_id"]))["items"][0]
    assert it["status"] == "rollback_failed"                      # detected and frozen
    named = [t for t in _tasks(h) if t["code"] == "GTM_RELEASE_MAY_BE_UNPUBLISHED"]
    assert [t["ref"] for t in named] == [st["rel"]]
    assert "no compare-and-swap" in ADR.read_text()


# ============================================================================== L5 the brief cache and revocation

def test_l5_no_cached_client_content_after_a_revocation_even_uncommitted(h):
    conn = h.connection()
    h.t.shop(SHOP_A).product(PRODUCT, desc="<p>Client private copy.</p>")
    j, it = _paid(h, conn, "product_content_error", PRODUCT)
    b1 = h.ok(h.get(f"/jobs/{j['job_id']}/brief", caller="fire_team"))
    assert b1["items"][0]["untrusted_client_content"]["rows"]
    h.ledger.fail_types.add("connection_revoked")                 # the revocation commit cannot land yet
    h.refused(h.post(f"/connections/{conn['connection_id']}/revoke", {"request_id": rid()}, caller="hub"), 503)
    assert it["item_id"] not in h.svc._brief_cache                # dropped on revoke
    b2 = h.ok(h.get(f"/jobs/{j['job_id']}/brief", caller="fire_team"))
    content = b2["items"][0]["untrusted_client_content"]
    assert content["rows"] is None and content["status"] == "revoked"


# ============================================================================== Info punycode hosts

def test_info_a_punycode_host_is_shown_with_its_unicode_form_and_flagged(h):
    conn = h.connection()
    h.t.shop(SHOP_A).product(PRODUCT, desc="<p>Warm.</p>")
    j, it = _paid(h, conn, "product_content_error", PRODUCT)
    h.ok(h.plan(j, [{"item_id": it["item_id"], "connection_id": conn["connection_id"],
                     "ops": [{"op": "shopify.product.update", "target": PRODUCT, "field": "descriptionHtml",
                              "after": '<p>x <a href="https://xn--pple-43d.com/">y</a> '
                                       '<a href="https://xn--caf-dma.fr/">z</a></p>'}]}]))
    v = h.ok(h.get(f"/jobs/{j['job_id']}"))
    assert sorted(v["new_external_hosts"]) == ["xn--caf-dma.fr", "xn--pple-43d.com"]
    detail = {d["host"]: d for d in v["new_external_hosts_detail"]}
    assert detail["xn--pple-43d.com"]["unicode"] == "аpple.com" and detail["xn--pple-43d.com"]["confusable"]
    assert detail["xn--caf-dma.fr"]["unicode"] == "café.fr" and not detail["xn--caf-dma.fr"]["confusable"]
    assert v["items"][0]["new_external_hosts_detail"] == v["new_external_hosts_detail"]


# ============================================================================== every Finance payment path records

def _quoted(h):
    conn = h.connection()
    h.t.shop(SHOP_A).product(PRODUCT)
    return h.job([h.finding(conn, target=PRODUCT)])


def _refund_for(h, ev):
    return [r for r in h.ok(h.get("/refunds")) if r["finance_event_id"] == ev]


def test_payment_before_quote_acceptance_is_recorded_and_refunded(h):
    j = _quoted(h)
    ev = "fin-evt-" + "1" * 40
    out = h.ok(h.pay(j, ev=ev))
    assert out["status"] == "quoted" and out["payment"] is None
    [r] = _refund_for(h, ev)
    assert r["kind"] == "orphaned_payment" and r["reason"] == "quote_not_accepted" and r["status"] == "proposed"
    assert r["amount"] == j["quote"]["total"]
    assert "ORPHANED_PAYMENT" in [t["code"] for t in _tasks(h)]
    h.ok(h.pay(j, ev=ev))                                         # a replay: answered, nothing new
    assert len(h.ledger.of_type("payment_orphaned")) == 1
    h.ok(h.accept(j))
    assert h.ok(h.pay(j))["status"] == "paid"                     # the job can still be paid properly


def test_payment_in_another_currency_is_recorded_and_refunded(h):
    j = _quoted(h)
    h.ok(h.accept(j))
    ev = "fin-evt-" + "2" * 40
    r = h.post("/finance/events", {"request_id": rid(), "finance_event_id": ev, "job_id": j["job_id"],
                                   "kind": "payment_confirmed", "amount": j["quote"]["total"], "currency": "EUR",
                                   "quote_sha256": j["quote_sha256"]}, caller="finance_31")
    assert h.ok(r)["payment"] is None
    [rf] = _refund_for(h, ev)
    assert rf["reason"] == "currency_not_supported" and rf["currency"] == "EUR"


def test_payment_for_an_unknown_job_is_recorded_and_refunded(h):
    ev = "fin-evt-" + "3" * 40
    ghost = {"job_id": "cfx-job-" + "f" * 40, "quote": {"total": "10.00"}, "quote_sha256": "e" * 64}
    out = h.refused(h.pay(ghost, ev=ev), 404, "JOB_NOT_FOUND")
    assert out["recorded"] is True
    [rf] = _refund_for(h, ev)
    assert rf["reason"] == "job_not_found" and rf["client_id"] is None and rf["amount"] == "10.00"
    assert out["refund_id"] == rf["refund_id"]
    h.refused(h.pay(ghost, ev=ev), 404, "JOB_NOT_FOUND")          # a replay: nothing new
    assert len(h.ledger.of_type("payment_orphaned")) == 1


def test_a_reused_finance_event_id_is_recorded_for_andre(h):
    j = _quoted(h)
    h.ok(h.accept(j))
    ev = "fin-evt-" + "4" * 40
    h.ok(h.pay(j, ev=ev))
    out = h.refused(h.pay(j, ev=ev, amount="1.00"), 409, "FINANCE_EVENT_REUSED")
    assert out["recorded"] is True
    h.refused(h.pay(j, ev=ev, amount="1.00"), 409, "FINANCE_EVENT_REUSED")
    assert len(h.ledger.of_type("finance_event_conflict")) == 1
    assert [t["code"] for t in _tasks(h)].count("FINANCE_EVENT_CONFLICT") == 1


def test_a_malformed_finance_post_is_recorded_by_hash_only(h):
    j = _quoted(h)
    bad = {"request_id": rid(), "finance_event_id": "fin-evt-" + "5" * 40, "job_id": j["job_id"],
           "kind": "payment_confirmed", "amount": 150.0, "currency": "USD", "quote_sha256": j["quote_sha256"]}
    h.refused(h.post("/finance/events", bad, caller="finance_31"), 422)
    h.refused(h.post("/finance/events", bad, caller="finance_31"), 422)
    [line] = h.ledger.of_type("finance_event_malformed")
    assert "150.0" not in str(line) and j["job_id"] not in str(line)  # the body itself is never stored
    assert [t["code"] for t in _tasks(h)].count("FINANCE_EVENT_MALFORMED") == 1
    h.refused(h.post("/finance/events", bad, caller="hub"), 403)      # not Finance: not a payment event
    assert len(h.ledger.of_type("finance_event_malformed")) == 1
