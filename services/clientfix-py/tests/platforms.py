"""Recorded-fixture platforms (test-only, never importable from src/). Each fake answers ONLY the documented request
shapes the connector modules cite — the exact Shopify GraphQL documents of connectors/shopify.py (validated against the
2026-10 Admin schema), the GA4 Admin v1beta key-event routes, the Tag Manager v2 routes, the Business Information v1
location get / patch, Yelp Fusion business details — with answers in the documented shapes, and keeps state so a
write can be read back. Anything else answers 400 with an ``undocumented`` marker the tests look for. No network.

Fault injection: ``FakeTransport.rules`` is a list of ``(predicate(conn, req) -> bool, action)``; the first matching
rule's action runs INSTEAD of the platform: an ``HttpAnswer`` (returned as is, nothing applied), an Exception instance
(raised: a lost answer), or a callable ``(conn, req, platform_call) -> HttpAnswer`` (may apply, then lie)."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

from connectors import shopify as shp
from connectors.base import HttpAnswer, HttpRequest

UNDOCUMENTED = HttpAnswer(400, {"undocumented": True})


def _digest(v) -> str:
    return hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()


class FakeShopify:
    def __init__(self, shop: str):
        self.shop = shop
        self.products: dict = {}
        self.pages: dict = {}
        self.redirects: dict = {}
        self.next_id = 9000

    def product(self, gid: str, title="Blue Hoodie", desc="<p>Warm.</p>", seo_title=None, seo_desc=None) -> None:
        self.products[gid] = {"id": gid, "title": title, "handle": "blue-hoodie", "descriptionHtml": desc,
                              "updatedAt": "2026-10-01T00:00:00Z", "seo": {"title": seo_title, "description": seo_desc},
                              "metafields": {}}

    def page(self, gid: str, title="About", body="<p>Old text.</p>") -> None:
        self.pages[gid] = {"id": gid, "title": title, "handle": "about", "body": body, "isPublished": True,
                           "updatedAt": "2026-10-01T00:00:00Z"}

    def handle(self, req: HttpRequest) -> HttpAnswer:
        if req.method != "POST" or req.url != f"https://{self.shop}/admin/api/2026-10/graphql.json":
            return UNDOCUMENTED
        q, v = req.body.get("query"), req.body.get("variables") or {}
        if q not in shp.DOCUMENTS:
            return UNDOCUMENTED
        if q == shp.PRODUCT_READ:
            p = self.products.get(v["id"])
            return HttpAnswer(200, {"data": {"product": None if p is None else
                                             {k: copy.deepcopy(p[k]) for k in ("id", "title", "handle",
                                                                              "descriptionHtml", "updatedAt", "seo")}}})
        if q == shp.PRODUCT_UPDATE:
            inp = v["product"]
            p = self.products.get(inp["id"])
            if p is None:
                return HttpAnswer(200, {"data": {"productUpdate": {"product": None, "userErrors": [
                    {"field": ["id"], "message": "Product does not exist"}]}}})
            for k, val in inp.items():
                if k == "seo":
                    p["seo"] = {"title": val.get("title"), "description": val.get("description")}
                elif k != "id":
                    p[k] = val
            return HttpAnswer(200, {"data": {"productUpdate": {"product": {k: p[k] for k in (
                "id", "title", "handle", "descriptionHtml", "seo")}, "userErrors": []}}})
        if q == shp.PAGE_READ:
            p = self.pages.get(v["id"])
            return HttpAnswer(200, {"data": {"page": copy.deepcopy(p)}})
        if q == shp.PAGE_UPDATE:
            p = self.pages.get(v["id"])
            if p is None:
                return HttpAnswer(200, {"data": {"pageUpdate": {"page": None, "userErrors": [
                    {"field": ["id"], "message": "Page not found"}]}}})
            p.update(v["page"])
            return HttpAnswer(200, {"data": {"pageUpdate": {"page": {k: p[k] for k in (
                "id", "title", "handle", "body", "isPublished")}, "userErrors": []}}})
        if q == shp.REDIRECT_BY_PATH:
            path = v["q"].removeprefix("path:")
            nodes = [dict(r) for r in self.redirects.values() if r["path"] == path]
            return HttpAnswer(200, {"data": {"urlRedirects": {"nodes": nodes}}})
        if q == shp.REDIRECT_CREATE:
            self.next_id += 1
            rid = f"gid://shopify/UrlRedirect/{self.next_id}"
            self.redirects[rid] = {"id": rid, **v["urlRedirect"]}
            return HttpAnswer(200, {"data": {"urlRedirectCreate": {"urlRedirect": dict(self.redirects[rid]),
                                                                   "userErrors": []}}})
        if q == shp.REDIRECT_UPDATE:
            r = self.redirects.get(v["id"])
            if r is None:
                return HttpAnswer(200, {"data": {"urlRedirectUpdate": {"urlRedirect": None, "userErrors": [
                    {"field": ["id"], "message": "not found"}]}}})
            r.update(v["urlRedirect"])
            return HttpAnswer(200, {"data": {"urlRedirectUpdate": {"urlRedirect": dict(r), "userErrors": []}}})
        if q == shp.REDIRECT_DELETE:
            if self.redirects.pop(v["id"], None) is None:
                return HttpAnswer(200, {"data": {"urlRedirectDelete": {"deletedUrlRedirectId": None, "userErrors": [
                    {"field": ["id"], "message": "not found"}]}}})
            return HttpAnswer(200, {"data": {"urlRedirectDelete": {"deletedUrlRedirectId": v["id"], "userErrors": []}}})
        if q == shp.METAFIELD_READ:
            p = self.products.get(v["id"])
            if p is None:
                return HttpAnswer(200, {"data": {"product": None}})
            mf = p["metafields"].get((v["namespace"], v["key"]))
            return HttpAnswer(200, {"data": {"product": {"id": p["id"], "metafield": None if mf is None else {
                "id": "gid://shopify/Metafield/1", "namespace": v["namespace"], "key": v["key"], "type": mf["type"],
                "value": mf["value"], "compareDigest": mf["digest"]}}}})
        if q == shp.METAFIELDS_SET:
            out = []
            for m in v["metafields"]:
                p = self.products[m["ownerId"]]
                cur = p["metafields"].get((m["namespace"], m["key"]))
                if "compareDigest" in m and (cur is None or cur["digest"] != m["compareDigest"]):
                    return HttpAnswer(200, {"data": {"metafieldsSet": {"metafields": [], "userErrors": [
                        {"field": ["metafields", "0", "compareDigest"], "message": "stale", "code": "STALE_OBJECT"}]}}})
                new = {"type": m["type"], "value": m["value"]}
                new["digest"] = _digest([new, (cur or {}).get("digest")])
                p["metafields"][(m["namespace"], m["key"])] = new
                out.append({"id": "gid://shopify/Metafield/1", "namespace": m["namespace"], "key": m["key"],
                            "type": new["type"], "value": new["value"], "compareDigest": new["digest"]})
            return HttpAnswer(200, {"data": {"metafieldsSet": {"metafields": out, "userErrors": []}}})
        if q == shp.METAFIELDS_DELETE:
            gone = []
            for m in v["metafields"]:
                p = self.products[m["ownerId"]]
                if p["metafields"].pop((m["namespace"], m["key"]), None) is not None:
                    gone.append({"ownerId": m["ownerId"], "namespace": m["namespace"], "key": m["key"]})
            return HttpAnswer(200, {"data": {"metafieldsDelete": {"deletedMetafields": gone, "userErrors": []}}})
        return UNDOCUMENTED


class FakeGA4:
    BASE = "https://analyticsadmin.googleapis.com/v1beta/"

    def __init__(self):
        self.props: dict = {}
        self.n = 100

    def handle(self, req: HttpRequest) -> HttpAnswer:
        u = urlsplit(req.url)
        path = u.path.removeprefix("/v1beta/")
        m = re.fullmatch(r"(properties/[0-9]+)/keyEvents", path)
        if m and req.method == "GET":
            evs = list(self.props.get(m.group(1), {}).values())
            return HttpAnswer(200, {"keyEvents": copy.deepcopy(evs)} if evs else {})
        if m and req.method == "POST":
            evs = self.props.setdefault(m.group(1), {})
            name = req.body["eventName"]
            if name in evs:
                return HttpAnswer(409, {"error": {"code": 409, "status": "ALREADY_EXISTS"}})
            self.n += 1
            ke = {"name": f"{m.group(1)}/keyEvents/{self.n}", "eventName": name, "createTime": "2026-10-06T18:00:00Z",
                  "deletable": True, "custom": True, "countingMethod": req.body["countingMethod"]}
            evs[name] = ke
            return HttpAnswer(200, dict(ke))
        m = re.fullmatch(r"(properties/[0-9]+)/keyEvents/([0-9]+)", path)
        if m and req.method == "DELETE":
            evs = self.props.get(m.group(1), {})
            for k, ke in list(evs.items()):
                if ke["name"] == path:
                    del evs[k]
                    return HttpAnswer(200, {})
            return HttpAnswer(404, {"error": {"code": 404}})
        return UNDOCUMENTED


class FakeGTM:
    """One container: a workspace of tags, numbered versions, a live version."""

    def __init__(self, container: str = "accounts/1/containers/2"):
        self.c = container
        self.ws = 3
        self.tags: dict = {}
        self.versions: dict = {}
        self.live: Optional[str] = None
        self.n = 0
        self.fp = 0
        self.compile_error = False
        self._version("initial")

    def _fp(self) -> str:
        self.fp += 1
        return f"fp{self.fp}"

    def tag(self, tag_id: str, paused=True, triggers=("7",), ttype="gaawe") -> str:
        path = f"{self.c}/workspaces/{self.ws}/tags/{tag_id}"
        self.tags[tag_id] = {"path": path, "tagId": tag_id, "name": f"Purchase {tag_id}", "type": ttype,
                             "parameter": [{"type": "template", "key": "eventName", "value": "purchase"}],
                             "firingTriggerId": list(triggers), "paused": paused, "fingerprint": self._fp()}
        return path

    def _version(self, name: str) -> str:
        self.n += 1
        path = f"{self.c}/versions/{self.n}"
        tags = []
        for t in self.tags.values():
            tags.append({k: copy.deepcopy(v) for k, v in t.items() if k != "path"})
        self.versions[path] = {"path": path, "containerVersionId": str(self.n), "name": name, "tag": tags,
                               "fingerprint": self._fp()}
        if self.live is None:
            self.live = path
        return path

    def publish_initial(self) -> None:
        self.live = self._version("baseline")

    def handle(self, req: HttpRequest) -> HttpAnswer:
        u = urlsplit(req.url)
        path = u.path.removeprefix("/tagmanager/v2/")
        qs = parse_qs(u.query)
        if req.method == "GET" and path == f"{self.c}/versions:live":
            return HttpAnswer(200, copy.deepcopy(self.versions[self.live]))
        m = re.fullmatch(re.escape(self.c) + r"/workspaces/([0-9]+)/tags/([0-9]+)", path)
        if m and int(m.group(1)) != self.ws:
            return HttpAnswer(404, {"error": {"code": 404}})
        if m and req.method == "GET":
            t = self.tags.get(m.group(2))
            return HttpAnswer(200, copy.deepcopy(t)) if t else HttpAnswer(404, {"error": {"code": 404}})
        if m and req.method == "PUT":
            t = self.tags.get(m.group(2))
            if t is None:
                return HttpAnswer(404, {"error": {"code": 404}})
            if qs.get("fingerprint", [None])[0] not in (None, t["fingerprint"]):
                return HttpAnswer(412, {"error": {"code": 412, "status": "FAILED_PRECONDITION"}})
            new = {k: copy.deepcopy(v) for k, v in req.body.items() if k not in ("fingerprint", "path", "tagId")}
            t.update(new)
            t["fingerprint"] = self._fp()
            return HttpAnswer(200, copy.deepcopy(t))
        if req.method == "POST" and path == f"{self.c}/workspaces/{self.ws}:quick_preview":
            return HttpAnswer(200, {"compilerError": self.compile_error, "containerVersion": {"name": "preview"},
                                    "syncStatus": {"mergeConflict": False, "syncError": False}})
        if req.method == "POST" and path == f"{self.c}/workspaces/{self.ws}:create_version":
            if self.compile_error:
                return HttpAnswer(200, {"compilerError": True})
            v = self._version(req.body.get("name", ""))
            self.ws += 1                                   # "deletes the workspace"
            for t in self.tags.values():
                t["path"] = f"{self.c}/workspaces/{self.ws}/tags/{t['tagId']}"
            return HttpAnswer(200, {"containerVersion": copy.deepcopy(self.versions[v]), "compilerError": False,
                                    "newWorkspacePath": f"{self.c}/workspaces/{self.ws}"})
        m = re.fullmatch(re.escape(self.c) + r"/versions/([0-9]+):publish", path)
        if m and req.method == "POST":
            vp = f"{self.c}/versions/{m.group(1)}"
            if vp not in self.versions:
                return HttpAnswer(404, {"error": {"code": 404}})
            fp = qs.get("fingerprint", [None])[0]
            if fp is not None and fp != self.versions[vp]["fingerprint"]:
                return HttpAnswer(412, {"error": {"code": 412}})
            self.live = vp
            return HttpAnswer(200, {"containerVersion": copy.deepcopy(self.versions[vp]), "compilerError": False})
        return UNDOCUMENTED


class FakeGBP:
    def __init__(self):
        self.locations: dict = {}

    def location(self, name: str, phone="(213) 555-0100", website="https://oldsite.test/",
                 address=None) -> None:
        self.locations[name] = {"name": name, "title": "Z Test Shop",
                                "phoneNumbers": {"primaryPhone": phone},
                                "websiteUri": website,
                                "storefrontAddress": address or {"regionCode": "US", "postalCode": "90001",
                                                                 "administrativeArea": "CA", "locality": "Los Angeles",
                                                                 "addressLines": ["1 Old St"]}}

    def handle(self, req: HttpRequest) -> HttpAnswer:
        u = urlsplit(req.url)
        name = u.path.removeprefix("/v1/")
        qs = parse_qs(u.query)
        loc = self.locations.get(name)
        if loc is None:
            return HttpAnswer(404, {"error": {"code": 404}})
        if req.method == "GET" and qs.get("readMask"):
            return HttpAnswer(200, {k: copy.deepcopy(loc[k]) for k in ("phoneNumbers", "websiteUri",
                                                                         "storefrontAddress")})
        if req.method == "PATCH" and qs.get("updateMask"):
            mask = qs["updateMask"][0]
            if mask not in ("phoneNumbers.primaryPhone", "websiteUri", "storefrontAddress"):
                return HttpAnswer(400, {"error": {"code": 400}})
            if qs.get("validateOnly", ["false"])[0] == "true":
                return HttpAnswer(200, {})
            if mask == "phoneNumbers.primaryPhone":
                loc["phoneNumbers"]["primaryPhone"] = req.body["phoneNumbers"]["primaryPhone"]
            else:
                loc[mask] = copy.deepcopy(req.body[mask])
            return HttpAnswer(200, copy.deepcopy(loc))
        return UNDOCUMENTED


class FakeYelp:
    def __init__(self):
        self.businesses: dict = {}

    def business(self, bid: str, phone="+12135550100") -> None:
        self.businesses[bid] = {"id": bid, "alias": "z-test-shop", "name": "Z Test Shop", "phone": phone,
                                "display_phone": "(213) 555-0100", "url": "https://www.yelp.com/biz/z-test-shop",
                                "is_closed": False,
                                "location": {"address1": "1 Old St", "address2": "", "address3": "",
                                             "city": "Los Angeles", "zip_code": "90001", "country": "US",
                                             "state": "CA", "display_address": ["1 Old St", "Los Angeles, CA 90001"]}}

    def handle(self, req: HttpRequest) -> HttpAnswer:
        if req.method != "GET" or req.auth != "yelp_app":
            return UNDOCUMENTED
        bid = urlsplit(req.url).path.removeprefix("/v3/businesses/")
        b = self.businesses.get(bid)
        return HttpAnswer(200, copy.deepcopy(b)) if b else HttpAnswer(404, {"error": {"code": "BUSINESS_NOT_FOUND"}})


class FakeTransport:
    """The test transport: routes by host to the fakes, records every call, applies fault rules. It is also the place
    the tests assert a connector never puts a credential in a request."""
    wired = True

    def __init__(self):
        self.shops: dict = {}
        self.ga4 = FakeGA4()
        self.gtm = FakeGTM()
        self.gbp = FakeGBP()
        self.yelp = FakeYelp()
        self.calls: list = []
        self.rules: list = []
        self.before: Optional[Callable] = None

    def shop(self, domain: str) -> FakeShopify:
        return self.shops.setdefault(domain, FakeShopify(domain))

    def platform(self, conn, req: HttpRequest) -> HttpAnswer:
        host = urlsplit(req.url).hostname or ""
        if host.endswith(".myshopify.com"):
            if host != conn.account_ref:                   # a request to another tenant's shop
                return HttpAnswer(403, {"errors": "wrong shop"})
            return self.shop(host).handle(req)
        if host == "analyticsadmin.googleapis.com":
            return self.ga4.handle(req)
        if host == "tagmanager.googleapis.com":
            return self.gtm.handle(req)
        if host == "mybusinessbusinessinformation.googleapis.com":
            return self.gbp.handle(req)
        if host == "api.yelp.com":
            return self.yelp.handle(req)
        return UNDOCUMENTED

    def call(self, conn, req: HttpRequest) -> HttpAnswer:
        blob = json.dumps([req.url, req.body], default=str)
        assert "vault:" not in blob and "shpat_" not in blob and "ya29." not in blob, "a credential in a request"
        self.calls.append((conn, req))
        if self.before is not None:
            self.before(conn, req)
        for pred, action in list(self.rules):
            if pred(conn, req):
                if isinstance(action, HttpAnswer):
                    return action
                if isinstance(action, BaseException):
                    raise action
                return action(conn, req, lambda: self.platform(conn, req))
        return self.platform(conn, req)

    def writes(self) -> list:
        return [(c, r) for c, r in self.calls if r.is_write]


def is_write(conn, req) -> bool:
    return req.is_write


def shopify_op(name: str) -> Callable:
    doc = {"productUpdate": shp.PRODUCT_UPDATE, "pageUpdate": shp.PAGE_UPDATE, "urlRedirectCreate": shp.REDIRECT_CREATE,
           "urlRedirectUpdate": shp.REDIRECT_UPDATE, "urlRedirectDelete": shp.REDIRECT_DELETE,
           "metafieldsSet": shp.METAFIELDS_SET, "metafieldsDelete": shp.METAFIELDS_DELETE}[name]
    return lambda conn, req: isinstance(req.body, dict) and req.body.get("query") == doc
