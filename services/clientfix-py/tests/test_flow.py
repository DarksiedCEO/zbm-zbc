"""The whole flow, success paths (ADR 0017 decisions 8-20): finding -> quote -> payment -> plan -> client approval ->
deterministic apply with snapshot, dry run, read-back -> re-detection -> dated report -> refund decision. One test per
verified connector (Shopify, GA4, Tag Manager, Business Profile) and the guided-manual Yelp path."""

from __future__ import annotations

from helpers import (CLIENT_A, PRODUCT, PRODUCT2, SHOP_A, FakeDetection, Harness, rid, wired_ports)


def test_starts_verified_and_healthy(h):
    st = h.ok(h.get("/status"))
    assert st["integrity"]["ok"] is True and st["status"] == "ok"
    assert h.ok(h.client.get("/health")) == {"status": "ok"}


def test_shopify_seo_fix_end_to_end_is_proven_and_nothing_refunded(h):
    conn, job = h.seo_job()
    jid = job["job_id"]
    assert job["status"] == "approved" and job["team"] == "alpha"
    out = h.ok(h.apply(jid))
    it = out["items"][0]
    assert it["status"] == "applied_verified", it
    shop = h.t.shop(SHOP_A)
    assert shop.products[PRODUCT]["seo"]["title"] == "Blue Hoodie | Warm Winter Wear"
    assert it["result"]["snapshot"] == [[PRODUCT, "seo.description", None], [PRODUCT, "seo.title", None]]
    assert it["result"]["readback"] == [[PRODUCT, "seo.description", None],
                                        [PRODUCT, "seo.title", "Blue Hoodie | Warm Winter Wear"]]
    assert it["result"]["dry_run"] == {"outcome": "applied", "mode": "offline"}
    assert out["status"] == "applied"
    # applied and verified is NOT fixed: only re-detection clears it
    assert h.ok(h.tick("redetect"))["cleared"] == 1
    j = h.ok(h.get(f"/jobs/{jid}"))
    assert j["items"][0]["status"] == "fixed_proven"
    assert j["status"] == "closed"                              # nothing unfixed: payment kept, no refund
    rep = j["report"]
    assert rep["issued_at"] == "2026-10-06T18:00:00Z" and rep["items"][0]["fixed"] is True
    assert rep["items"][0]["before"] == [[PRODUCT, "seo.description", None], [PRODUCT, "seo.title", None]]
    assert rep["items"][0]["after"] == [[PRODUCT, "seo.description", None],
                                        [PRODUCT, "seo.title", "Blue Hoodie | Warm Winter Wear"]]
    assert rep["items"][0]["evidence"] and all(e["seq"] for e in rep["items"][0]["evidence"])
    assert not h.ok(h.get("/refunds"))
    assert h.ok(h.get("/leases", params={"status": "active"})) == []


def test_shopify_page_redirect_and_metafield_each_bound_to_its_finding(h):
    conn = h.connection()
    shop = h.t.shop(SHOP_A)
    shop.product(PRODUCT)
    shop.page("gid://shopify/Page/77")
    fr = h.finding(conn, check="broken_link", target="redirect:/old-hoodie")
    fp = h.finding(conn, check="page_content_error", target="gid://shopify/Page/77")
    fm = h.finding(conn, check="product_metafield_wrong", target=PRODUCT)
    j = h.job([fr, fp, fm])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    items = {i["finding_id"]: i["item_id"] for i in h.ok(h.get(f"/jobs/{j['job_id']}"))["items"]}
    ops = {fr["finding_id"]: {"op": "shopify.redirect.set", "target": "redirect:/old-hoodie", "field": "target",
                              "before": None, "after": "/products/blue-hoodie"},
           fp["finding_id"]: {"op": "shopify.page.update", "target": "gid://shopify/Page/77", "field": "body",
                              "before": "<p>Old text.</p>",
                              "after": "<p>New <a href=\"/products/blue-hoodie\">link</a>.</p>"},
           fm["finding_id"]: {"op": "shopify.metafield.set", "target": PRODUCT, "field": "metafield:custom.care",
                              "before": None, "after": {"type": "single_line_text_field", "value": "Machine wash cold"}}}
    h.ok(h.plan(j, [{"item_id": items[f], "connection_id": conn["connection_id"], "ops": [op]}
                    for f, op in ops.items()]))
    h.ok(h.approve(j["job_id"]))
    out = h.ok(h.apply(j["job_id"]))
    assert {i["status"] for i in out["items"]} == {"applied_verified"}, out["items"]
    assert [r for r in shop.redirects.values()][0]["target"] == "/products/blue-hoodie"
    assert shop.pages["gid://shopify/Page/77"]["body"].startswith("<p>New")
    assert shop.products[PRODUCT]["metafields"][("custom", "care")]["value"] == "Machine wash cold"


def test_ga4_key_event_created_and_proven(h):
    conn = h.connection(connector="ga4", account="properties/123")
    f = h.finding(conn, check="ga4_key_event_missing", target="properties/123")
    j = h.job([f], ["90.00"])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    item = h.item(j["job_id"])
    h.ok(h.plan(j, [{"item_id": item["item_id"], "connection_id": conn["connection_id"],
                     "ops": [{"op": "ga4.key_event.set", "target": "properties/123", "field": "key_event:purchase",
                              "before": None, "after": {"countingMethod": "ONCE_PER_EVENT"}}]}]))
    h.ok(h.approve(j["job_id"]))
    out = h.ok(h.apply(j["job_id"]))
    assert out["items"][0]["status"] == "applied_verified"
    assert "purchase" in h.t.ga4.props["properties/123"]
    h.ok(h.tick("redetect"))
    assert h.item(j["job_id"])["status"] == "fixed_proven"


def test_gtm_tag_unpaused_previewed_published_and_verified_live(h):
    gtm = h.t.gtm
    path = gtm.tag("12", paused=True)
    gtm.publish_initial()
    before_live = gtm.live
    conn = h.connection(connector="gtm", account="accounts/1/containers/2")
    f = h.finding(conn, check="gtm_tag_paused", target=path)
    j = h.job([f])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    item = h.item(j["job_id"])
    h.ok(h.plan(j, [{"item_id": item["item_id"], "connection_id": conn["connection_id"],
                     "ops": [{"op": "gtm.tag.update", "target": path, "field": "paused", "before": True,
                              "after": False}]}]))
    h.ok(h.approve(j["job_id"]))
    out = h.ok(h.apply(j["job_id"]))
    assert out["items"][0]["status"] == "applied_verified", out["items"][0]
    assert gtm.live != before_live
    assert gtm.versions[gtm.live]["tag"][0].get("paused", False) is False      # proto3: false is omitted
    urls = [r.url for _, r in h.t.calls]
    assert any(u.endswith(":quick_preview") for u in urls) and any(":publish?fingerprint=" in u for u in urls)
    assert any("?fingerprint=" in u and "/tags/12" in u for u in urls)          # the tag write is compare-and-swap
    assert any(u.endswith("/workspaces") for u in urls) and any(u.endswith("/status") for u in urls)
    assert len(gtm.workspaces) == 1                    # only the client's own default workspace is left


def test_gbp_phone_fixed_with_validate_only_dry_run(h):
    h.t.gbp.location("locations/555")
    conn = h.connection(connector="gbp", account="locations/555")
    f = h.finding(conn, check="listing_phone_wrong", target="locations/555")
    j = h.job([f])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    item = h.item(j["job_id"])
    h.ok(h.plan(j, [{"item_id": item["item_id"], "connection_id": conn["connection_id"],
                     "ops": [{"op": "gbp.location.patch", "target": "locations/555",
                              "field": "phoneNumbers.primaryPhone", "before": "(213) 555-0100",
                              "after": "(213) 555-0199"}]}]))
    h.ok(h.approve(j["job_id"]))
    out = h.ok(h.apply(j["job_id"]))
    assert out["items"][0]["status"] == "applied_verified"
    assert out["items"][0]["result"]["dry_run"] == {"outcome": "applied", "mode": "validateOnly"}
    assert any("validateOnly=true" in r.url for _, r in h.t.calls)
    assert h.t.gbp.locations["locations/555"]["phoneNumbers"]["primaryPhone"] == "(213) 555-0199"


def test_yelp_is_guided_manual_never_written_and_counted_only_when_read_back(h):
    bid = "z-test-shop-los-angeles"
    h.t.yelp.business(bid)
    conn = h.connection(connector="yelp", account=bid)
    assert conn["has_token_ref"] is False                          # no client credential, ever
    f = h.finding(conn, check="listing_phone_wrong", target=bid)
    j = h.job([f])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    item = h.item(j["job_id"])
    h.ok(h.plan(j, [{"item_id": item["item_id"], "connection_id": conn["connection_id"],
                     "ops": [{"op": "yelp.business.set", "target": bid, "field": "phone", "before": "+12135550100",
                              "after": "+12135550199"}]}]))
    h.ok(h.approve(j["job_id"]))
    out = h.ok(h.apply(j["job_id"]))
    it = out["items"][0]
    assert it["status"] == "awaiting_manual"
    assert it["result"]["instructions"][0]["required"] == "+12135550199"
    assert not h.t.writes()                                        # nothing but reads ever left
    h.ok(h.post(f"/jobs/{j['job_id']}/manual-done", {"request_id": rid(), "item_id": it["item_id"]},
                caller="hub", session=h.session()))
    assert h.ok(h.tick("manual-verify"))["unverified"] == 1        # not done on Yelp yet: the read says so
    h.t.yelp.businesses[bid]["phone"] = "+12135550199"              # the client made the change on Yelp
    assert h.ok(h.tick("manual-verify"))["verified"] == 1
    h.ok(h.tick("redetect"))
    assert h.item(j["job_id"])["status"] == "fixed_proven"
    assert not h.t.writes()


def test_engage_runs_the_fire_team_and_its_proposal_is_validated_like_any_plan(tmp_path):
    from helpers import FakeEngineers
    eng = FakeEngineers()
    h = Harness(tmp_path, ports=wired_ports(engineers=eng))
    conn = h.connection()
    h.t.shop(SHOP_A).product(PRODUCT)
    f = h.finding(conn)
    j = h.job([f])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    item = h.item(j["job_id"])
    eng.items = [{"item_id": item["item_id"], "connection_id": conn["connection_id"],
                  "ops": [{"op": "shopify.product.update", "target": PRODUCT, "field": "seo.title", "before": None,
                           "after": "Better title"}]}]
    out = h.ok(h.post(f"/jobs/{j['job_id']}/engage", {"request_id": rid()}, caller="clientfix_agent"))
    assert out["status"] == "planned" and out["items"][0]["ops"][0]["after"] == "Better title"
    team, brief = eng.briefs[0]
    assert team == "alpha" and brief["items"][0]["allowed_ops"]
    assert "vault:" not in str(brief) and "token" not in str(brief)  # engineers never hold client credentials


def test_two_items_share_one_resource_and_both_apply_under_one_lease(h):
    conn = h.connection()
    shop = h.t.shop(SHOP_A)
    shop.product(PRODUCT)
    shop.product(PRODUCT2)
    f1 = h.finding(conn, check="product_seo_missing", target=PRODUCT)
    f2 = h.finding(conn, check="product_content_error", target=PRODUCT)
    j = h.job([f1, f2], ["100.00", "50.00"])
    assert j["quote"]["total"] == "150.00"
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    items = h.ok(h.get(f"/jobs/{j['job_id']}"))["items"]
    by_f = {i["finding_id"]: i for i in items}
    h.ok(h.plan(j, [
        {"item_id": by_f[f1["finding_id"]]["item_id"], "connection_id": conn["connection_id"],
         "ops": [{"op": "shopify.product.update", "target": PRODUCT, "field": "seo.title", "before": None,
                  "after": "SEO"}]},
        {"item_id": by_f[f2["finding_id"]]["item_id"], "connection_id": conn["connection_id"],
         "ops": [{"op": "shopify.product.update", "target": PRODUCT, "field": "title", "before": "Blue Hoodie",
                  "after": "Blue Hoodie (Unisex)"}]}]))
    h.ok(h.approve(j["job_id"]))
    out = h.ok(h.apply(j["job_id"]))
    assert {i["status"] for i in out["items"]} == {"applied_verified"}
    leases = h.ok(h.get("/leases"))
    assert len(leases) == 1 and leases[0]["status"] == "released"


def test_partial_fix_refunds_only_unfixed_items_after_andre(tmp_path):
    det = FakeDetection("cleared")
    h = Harness(tmp_path, ports=wired_ports(detection=det))
    conn = h.connection()
    shop = h.t.shop(SHOP_A)
    shop.product(PRODUCT)
    shop.product(PRODUCT2, title="Red Cap")
    f1 = h.finding(conn, target=PRODUCT)
    f2 = h.finding(conn, target=PRODUCT2)
    j = h.job([f1, f2], ["120.00", "80.00"])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    items = {i["finding_id"]: i for i in h.ok(h.get(f"/jobs/{j['job_id']}"))["items"]}
    # item 2's "before" is stale (the store changed since the plan): it drifts and nothing is written for it
    h.ok(h.plan(j, [
        {"item_id": items[f1["finding_id"]]["item_id"], "connection_id": conn["connection_id"],
         "ops": [{"op": "shopify.product.update", "target": PRODUCT, "field": "seo.title", "before": None,
                  "after": "A"}]},
        {"item_id": items[f2["finding_id"]]["item_id"], "connection_id": conn["connection_id"],
         "ops": [{"op": "shopify.product.update", "target": PRODUCT2, "field": "seo.title", "before": "Stale",
                  "after": "B"}]}]))
    h.ok(h.approve(j["job_id"]))
    out = h.ok(h.apply(j["job_id"]))
    st = {i["finding_id"]: i["status"] for i in out["items"]}
    assert st == {f1["finding_id"]: "applied_verified", f2["finding_id"]: "drifted"}
    assert shop.products[PRODUCT2]["seo"]["title"] is None
    h.ok(h.tick("redetect"))
    refunds = h.ok(h.get("/refunds"))
    assert len(refunds) == 1 and refunds[0]["amount"] == "80.00" and refunds[0]["status"] == "proposed"
    assert refunds[0]["items"] == [items[f2["finding_id"]]["item_id"]]
    assert h.ok(h.get(f"/jobs/{j['job_id']}"))["status"] == "refund_pending"
    assert CLIENT_A == refunds[0]["client_id"]
