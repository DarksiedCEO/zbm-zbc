"""Connector unit tests: each connector builds exactly the documented requests (method, host, path, API version, query
parameters), parses only the documented answer shapes, and treats everything else as unknown (never success). Plus
the registry's statuses (verified / verified_gated / guided_manual / not_built) that ADR 0017 reports."""

from __future__ import annotations

import pytest

import connectors
from connectors import shopify as shp
from connectors.base import APPLIED, REFUSED, UNKNOWN, HttpAnswer, HttpRequest, OpRefused, UnknownState
from connectors.business_profile import BusinessProfileConnector
from connectors.google_analytics import GA4Connector
from connectors.shopify import ShopifyConnector
from connectors.tag_manager import TagManagerConnector
from connectors.yelp import YelpConnector
from platforms import FakeTransport

SHOP = "zbest-a.myshopify.com"
P = "gid://shopify/Product/1001"


def caller(platform_fn, log):
    def call(req: HttpRequest) -> HttpAnswer:
        log.append(req)
        return platform_fn(req)
    return call


def test_registry_statuses_match_the_adr():
    st = {c["connector"]: c["status"] for c in connectors.describe()}
    assert st == {"shopify": "verified", "ga4": "verified", "gtm": "verified", "gbp": "verified_gated",
                  "yelp": "guided_manual", "woocommerce": "not_built", "shopify_checkout": "not_built",
                  "shopify_theme": "not_built", "ad_pixels": "not_built", "service_automations": "not_built",
                  "sales_automations": "not_built", "crm": "not_built"}
    for c in connectors.describe():
        if c["status"] == "not_built":
            assert c["operations"] == [] and c["why_not_built"]
        else:
            assert c["docs"] and all(d.startswith("https://") for d in c["docs"])


def test_shopify_sends_only_validated_documents_to_the_versioned_endpoint():
    t = FakeTransport()
    t.shop(SHOP).product(P)
    log: list = []
    c = ShopifyConnector()
    ctx: dict = {}
    vals = c.read(SHOP, [(P, "seo.title"), (P, "title"), (P, "metafield:custom.care"),
                         ("redirect:/old", "target")], ctx, caller(t.shop(SHOP).handle, log))
    assert vals == {(P, "seo.title"): None, (P, "title"): "Blue Hoodie", (P, "metafield:custom.care"): None,
                    ("redirect:/old", "target"): None}
    ctx["state"] = dict(vals)
    assert c.write(SHOP, (P, "seo.title"), "New", ctx, caller(t.shop(SHOP).handle, log))[0] == APPLIED
    assert c.write(SHOP, ("redirect:/old", "target"), "/new", ctx, caller(t.shop(SHOP).handle, log))[0] == APPLIED
    for req in log:
        assert req.method == "POST" and req.url == f"https://{SHOP}/admin/api/2026-10/graphql.json"
        assert req.body["query"] in shp.DOCUMENTS
        assert req.is_write == req.body["query"].startswith("mutation ")
    # the seo update sends BOTH seo fields (the other from the current state), so nothing is cleared by accident
    upd = [r for r in log if r.body["query"] == shp.PRODUCT_UPDATE][0]
    assert upd.body["variables"]["product"] == {"id": P, "seo": {"title": "New", "description": None}}


@pytest.mark.parametrize("answer,outcome", [
    (HttpAnswer(200, {"data": {"productUpdate": {"product": {"id": P}, "userErrors": []}}}), APPLIED),
    (HttpAnswer(200, {"data": {"productUpdate": {"product": None, "userErrors": [{"field": ["x"], "message": "m"}]}}}),
     REFUSED),
    (HttpAnswer(401, {"errors": "unauthorized"}), REFUSED),
    (HttpAnswer(429, {"errors": "throttled"}), REFUSED),
    (HttpAnswer(200, {"errors": [{"message": "Throttled"}]}), UNKNOWN),
    (HttpAnswer(200, {"data": {"productUpdate": {"product": {"id": P}}}}), UNKNOWN),       # no userErrors key
    (HttpAnswer(200, {"data": {"productUpdate": None}}), UNKNOWN),
    (HttpAnswer(200, "<html>"), UNKNOWN),
    (HttpAnswer(502, None), UNKNOWN),
    (HttpAnswer(0, None), UNKNOWN),
    (HttpAnswer(408, None), UNKNOWN),
])
def test_shopify_write_outcomes_are_documented_shapes_only(answer, outcome):
    got = ShopifyConnector().write(SHOP, (P, "title"), "x", {"state": {}}, lambda req: answer)
    assert got[0] == outcome


def test_shopify_read_refuses_anything_undocumented():
    c = ShopifyConnector()
    for ans in (HttpAnswer(200, {"data": {"product": None}}), HttpAnswer(200, {"errors": ["x"]}),
                HttpAnswer(500, None), HttpAnswer(200, {"data": {"product": {"id": "gid://shopify/Product/2",
                                                                              "seo": {}}}})):
        with pytest.raises(UnknownState):
            c.read(SHOP, [(P, "title")], {}, lambda req, a=ans: a)


def test_shopify_metafield_writes_are_compare_and_swap():
    t = FakeTransport()
    shop = t.shop(SHOP)
    shop.product(P)
    c = ShopifyConnector()
    ctx: dict = {"state": {}}
    key = (P, "metafield:custom.care")
    assert c.write(SHOP, key, {"type": "single_line_text_field", "value": "a"}, ctx, shop.handle)[0] == APPLIED
    # someone else changes it: our next write carries the old digest and is refused, not applied over theirs
    shop.products[P]["metafields"][("custom", "care")]["digest"] = "theirs"
    assert c.write(SHOP, key, {"type": "single_line_text_field", "value": "b"}, ctx, shop.handle)[0] == REFUSED


def test_ga4_requests_follow_the_admin_v1beta_routes():
    t = FakeTransport()
    log: list = []
    c = GA4Connector()
    ctx: dict = {}
    key = ("properties/123", "key_event:purchase")
    assert c.read("properties/123", [key], ctx, caller(t.ga4.handle, log)) == {key: None}
    assert c.write("properties/123", key, {"countingMethod": "ONCE_PER_SESSION"}, ctx, caller(t.ga4.handle, log))[0] \
        == APPLIED
    assert c.write("properties/123", key, None, ctx, caller(t.ga4.handle, log))[0] == APPLIED
    assert [(r.method, r.url) for r in log] == [
        ("GET", "https://analyticsadmin.googleapis.com/v1beta/properties/123/keyEvents?pageSize=200"),
        ("POST", "https://analyticsadmin.googleapis.com/v1beta/properties/123/keyEvents"),
        ("DELETE", "https://analyticsadmin.googleapis.com/v1beta/properties/123/keyEvents/101")]
    assert log[1].body == {"eventName": "purchase", "countingMethod": "ONCE_PER_SESSION"}


def test_ga4_create_or_delete_only_and_non_deletable_is_refused():
    c = GA4Connector()
    with pytest.raises(OpRefused):
        c.validate("properties/1", {"op": "ga4.key_event.set", "target": "properties/1", "field": "key_event:x",
                                    "before": {"countingMethod": "ONCE_PER_EVENT"},
                                    "after": {"countingMethod": "ONCE_PER_SESSION"}})
    op = {"op": "ga4.key_event.set", "target": "properties/1", "field": "key_event:purchase",
          "before": {"countingMethod": "ONCE_PER_EVENT"}, "after": None}
    assert c.dry_run("properties/1", [op], {"ke_deletable": {}}, lambda r: None) == (REFUSED, "offline")


def test_gtm_update_carries_the_fingerprint_and_publish_needs_a_clean_preview():
    t = FakeTransport()
    path = t.gtm.tag("12")
    t.gtm.publish_initial()
    log: list = []
    c = TagManagerConnector()
    ctx: dict = {"item_id": "i"}
    call = caller(t.gtm.handle, log)
    snap = c.read("accounts/1/containers/2", [(path, "paused")], ctx, call)
    assert snap == {(path, "paused"): True} and ctx["live_before"].endswith("/versions/2")
    assert c.write("accounts/1/containers/2", (path, "paused"), False, ctx, call)[0] == APPLIED
    put = [r for r in log if r.method == "PUT"][0]
    assert put.url.startswith(f"https://tagmanager.googleapis.com/tagmanager/v2/{path}?fingerprint=fp")
    t.gtm.compile_error = True
    assert c.stage_check("accounts/1/containers/2", ctx, call) == REFUSED
    t.gtm.compile_error = False
    assert c.stage_check("accounts/1/containers/2", ctx, call) == APPLIED
    assert c.finalize("accounts/1/containers/2", ctx, call) == APPLIED
    assert ctx["published"] and t.gtm.live == ctx["published_path"]
    urls = [r.url for r in log]
    assert any(u.endswith("/workspaces/3:quick_preview") for u in urls)
    assert any(u.endswith("/workspaces/3:create_version") for u in urls)


def test_gbp_dry_run_is_validate_only_and_writes_name_their_update_mask():
    t = FakeTransport()
    t.gbp.location("locations/9")
    log: list = []
    c = BusinessProfileConnector()
    op = {"op": "gbp.location.patch", "target": "locations/9", "field": "websiteUri", "before": "https://oldsite.test/",
          "after": "https://newsite.test/"}
    assert c.dry_run("locations/9", [op], {}, caller(t.gbp.handle, log)) == (APPLIED, "validateOnly")
    assert log[0].method == "PATCH" and log[0].url == ("https://mybusinessbusinessinformation.googleapis.com/v1/"
                                                      "locations/9?updateMask=websiteUri&validateOnly=true")
    assert t.gbp.locations["locations/9"]["websiteUri"] == "https://oldsite.test/"        # validate only: unchanged
    assert c.write("locations/9", ("locations/9", "websiteUri"), "https://newsite.test/", {}, caller(t.gbp.handle,
                                                                                                    log))[0] == APPLIED
    assert log[1].url.endswith("locations/9?updateMask=websiteUri")
    with pytest.raises(OpRefused):                 # a URL carrying credentials or a non-https URL is never written
        c.validate("locations/9", {**op, "after": "http://newsite.test/"})


def test_yelp_never_writes_and_reads_with_the_apps_own_key():
    c = YelpConnector()
    assert c.manual and c.status == "guided_manual"
    assert c.write("x" * 22, ("x" * 22, "phone"), "+12135550199", {}, lambda r: HttpAnswer(200, {}))[0] == UNKNOWN
    t = FakeTransport()
    t.yelp.business("abcdefghijklmnopqrstuv")
    log: list = []
    c.read("abcdefghijklmnopqrstuv", [("abcdefghijklmnopqrstuv", "phone")], {}, caller(t.yelp.handle, log))
    assert log[0].method == "GET" and log[0].auth == "yelp_app"
    assert log[0].url == "https://api.yelp.com/v3/businesses/abcdefghijklmnopqrstuv"
    steps = c.instructions("abcdefghijklmnopqrstuv", [{"field": "phone", "before": "+1", "after": "+12135550199"}])
    assert steps[0]["required"] == "+12135550199"


@pytest.mark.parametrize("value", [
    "<p>ok</p><script>x</script>", "<p onclick=go()>x</p>", '<a href="javascript:alert(1)">x</a>',
    "<iframe src=x></iframe>", "<form action=/x></form>", "<style>body{}</style>", '<a href="data:text/html,x">x</a>',
    "<p>\x00</p>", '<img srcdoc="x">'])
def test_rich_text_refuses_script_frames_forms_and_handlers(value):
    with pytest.raises(OpRefused):
        ShopifyConnector().validate(SHOP, {"op": "shopify.page.update", "target": "gid://shopify/Page/1",
                                           "field": "body", "before": "<p>a</p>", "after": value})


def test_not_built_connectors_refuse_everything():
    for name, c in connectors.registry().items():
        if c.status != "not_built":
            continue
        with pytest.raises(OpRefused) as e:
            c.validate("x", {"op": f"{name}.anything", "target": "x", "field": "y", "before": "a", "after": "b"})
        assert e.value.code == "CONNECTOR_NOT_BUILT"
        with pytest.raises(UnknownState):
            c.read("x", [], {}, lambda r: None)
