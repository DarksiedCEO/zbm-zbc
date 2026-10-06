"""
Shopify — GraphQL Admin API, version 2026-10, through an OAuth app (founder decision 4). VERIFIED.

Every operation below was validated against Shopify's live Admin schema (shopify.dev ``validate_graphql_codeblocks``,
Oct 6 2026: all VALID) and is cited to its reference page:

  endpoint   POST https://{shop}.myshopify.com/admin/api/2026-10/graphql.json, header X-Shopify-Access-Token
             https://shopify.dev/docs/api/admin-graphql/2026-10
  OAuth      authorization code grant, expiring offline access token (``shpat_``), revoked by uninstalling the app
             https://shopify.dev/docs/apps/build/authentication-authorization/authenticate-standalone-apps
             https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens
  product    query product(id) { id title handle descriptionHtml updatedAt seo { title description } }  (read_products)
             mutation productUpdate(product: ProductUpdateInput!) { product {...} userErrors { field message } }
             (write_products) https://shopify.dev/docs/api/admin-graphql/2026-10/mutations/productUpdate
  page       query page(id) { id title handle body isPublished updatedAt }  (read_content, read_online_store_pages)
             mutation pageUpdate(id: ID!, page: PageUpdateInput!)  (write_content, write_online_store_pages)
             https://shopify.dev/docs/api/admin-graphql/2026-10/mutations/pageUpdate
  redirects  query urlRedirects(first, query: "path:<path>") { nodes { id path target } }
             mutation urlRedirectCreate(urlRedirect: UrlRedirectInput!) / urlRedirectUpdate(id, urlRedirect) /
             urlRedirectDelete(id)  (write_online_store_navigation)
             https://shopify.dev/docs/api/admin-graphql/2026-10/objects/UrlRedirect
  metafields query product(id) { metafield(namespace, key) { id namespace key type value compareDigest } }
             mutation metafieldsSet(metafields: [MetafieldsSetInput!]!) — with ``compareDigest`` (compare-and-swap)
             mutation metafieldsDelete(metafields: [MetafieldIdentifierInput!]!)
             https://shopify.dev/docs/api/admin-graphql/2026-10/mutations/metafieldsSet

Dry run: Shopify offers none for these mutations, so the dry run is the offline validation ("offline"). Concurrency:
metafields are compare-and-swap through ``compareDigest``; products, pages and redirects have no server-side CAS, so the
executor re-reads immediately before applying and refuses on drift (the residual window is in ADR 0017 known limits).
Not in the allowlist (version 1): theme files (site speed), checkout settings, product create / delete, price,
inventory, any other resource. Redirect targets are relative paths on the same store only.
"""

from __future__ import annotations

import re
from typing import Any

from connectors.base import (APPLIED, REFUSED, UNKNOWN, Call, Connector, HttpAnswer, HttpRequest, OpSpec,
                             UnknownState, html, one_of, text)

API_VERSION = "2026-10"
SHOP = re.compile(r"[a-z0-9][a-z0-9-]{0,60}\.myshopify\.com")
PRODUCT = re.compile(r"gid://shopify/Product/[1-9][0-9]{0,19}")
PAGE = re.compile(r"gid://shopify/Page/[1-9][0-9]{0,19}")
REDIRECT_TARGET = re.compile(r"redirect:/[\x21-\x7e]{0,1023}")
REL_PATH = re.compile(r"/(?!/)[\x21-\x7e]{0,254}")
METAFIELD = re.compile(r"metafield:([A-Za-z0-9_-]{3,255})\.([A-Za-z0-9_-]{2,64})")
MF_TYPES = ("single_line_text_field", "multi_line_text_field", "boolean", "number_integer", "url")

PRODUCT_READ = ("query ProductRead($id: ID!) { product(id: $id) { id title handle descriptionHtml updatedAt "
                "seo { title description } } }")
PRODUCT_UPDATE = ("mutation ProductUpdate($product: ProductUpdateInput!) { productUpdate(product: $product) { "
                  "product { id title handle descriptionHtml seo { title description } } userErrors { field message } } }")
PAGE_READ = "query PageRead($id: ID!) { page(id: $id) { id title handle body isPublished updatedAt } }"
PAGE_UPDATE = ("mutation PageUpdate($id: ID!, $page: PageUpdateInput!) { pageUpdate(id: $id, page: $page) { "
               "page { id title handle body isPublished } userErrors { field message } } }")
REDIRECT_BY_PATH = "query RedirectByPath($q: String!) { urlRedirects(first: 10, query: $q) { nodes { id path target } } }"
REDIRECT_CREATE = ("mutation RedirectCreate($urlRedirect: UrlRedirectInput!) { urlRedirectCreate(urlRedirect: "
                   "$urlRedirect) { urlRedirect { id path target } userErrors { field message } } }")
REDIRECT_UPDATE = ("mutation RedirectUpdate($id: ID!, $urlRedirect: UrlRedirectInput!) { urlRedirectUpdate(id: $id, "
                   "urlRedirect: $urlRedirect) { urlRedirect { id path target } userErrors { field message } } }")
REDIRECT_DELETE = ("mutation RedirectDelete($id: ID!) { urlRedirectDelete(id: $id) { deletedUrlRedirectId "
                   "userErrors { field message } } }")
METAFIELD_READ = ("query MetafieldRead($id: ID!, $namespace: String!, $key: String!) { product(id: $id) { id "
                  "metafield(namespace: $namespace, key: $key) { id namespace key type value compareDigest } } }")
METAFIELDS_SET = ("mutation MetafieldsSet($metafields: [MetafieldsSetInput!]!) { metafieldsSet(metafields: "
                  "$metafields) { metafields { id namespace key type value compareDigest } userErrors { field message "
                  "code } } }")
METAFIELDS_DELETE = ("mutation MetafieldsDelete($metafields: [MetafieldIdentifierInput!]!) { metafieldsDelete("
                     "metafields: $metafields) { deletedMetafields { ownerId namespace key } userErrors { field "
                     "message } } }")

# every operation document this connector can send (tests assert nothing else is ever sent)
DOCUMENTS = (PRODUCT_READ, PRODUCT_UPDATE, PAGE_READ, PAGE_UPDATE, REDIRECT_BY_PATH, REDIRECT_CREATE, REDIRECT_UPDATE,
             REDIRECT_DELETE, METAFIELD_READ, METAFIELDS_SET, METAFIELDS_DELETE)


def _mf_value(v: Any) -> bool:
    return (isinstance(v, dict) and set(v) == {"type", "value"} and one_of(*MF_TYPES)(v["type"])
            and isinstance(v["value"], str) and len(v["value"]) <= 10_000 and html(10_000)(v["value"]))


def _rel_path(v: Any) -> bool:
    return isinstance(v, str) and bool(REL_PATH.fullmatch(v))


OPS = {
    "shopify.product.update": OpSpec(
        "shopify.product.update", PRODUCT,
        {"title": text(255), "descriptionHtml": html(65_535), "seo.title": text(255), "seo.description": text(320)},
        create=True),                                  # create: only the seo fields may be absent (extra_checks)
    "shopify.page.update": OpSpec("shopify.page.update", PAGE, {"title": text(255), "body": html(512_000)}),
    "shopify.redirect.set": OpSpec("shopify.redirect.set", REDIRECT_TARGET, {"target": _rel_path}, create=True),
    "shopify.metafield.set": OpSpec("shopify.metafield.set", PRODUCT, {}, ((METAFIELD, _mf_value),), create=True),
}


class ShopifyConnector(Connector):
    def __init__(self):
        super().__init__(name="shopify", status="verified", account_ref=SHOP, ops=OPS,
                         scopes=("read_products", "write_products", "read_content", "write_content",
                                 "read_online_store_pages", "write_online_store_pages",
                                 "read_online_store_navigation", "write_online_store_navigation"),
                         docs=("https://shopify.dev/docs/api/admin-graphql/2026-10",
                               "https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens"))

    def extra_checks(self, op: dict) -> None:
        # the seo fields may be absent (null) before; title / description / body never
        from connectors.base import OpRefused
        if op["op"] == "shopify.product.update" and op["field"] in ("title", "descriptionHtml") and op["before"] is None:
            raise OpRefused("OP_VALUE_INVALID")

    # ---------------------------------------------------------------- transport helpers

    @staticmethod
    def _req(shop: str, query: str, variables: dict) -> HttpRequest:
        return HttpRequest("POST", f"https://{shop}/admin/api/{API_VERSION}/graphql.json",
                           {"query": query, "variables": variables}, auth="connection",
                           mutates=query.startswith("mutation "))

    @staticmethod
    def _data(ans: HttpAnswer) -> dict:
        """The ``data`` object of a documented 200 answer without top-level errors, else UnknownState."""
        if not isinstance(ans, HttpAnswer) or ans.status != 200 or not isinstance(ans.body, dict) \
                or ans.body.get("errors") or not isinstance(ans.body.get("data"), dict):
            raise UnknownState("not a documented GraphQL answer")
        return ans.body["data"]

    def _mutation(self, ans: HttpAnswer, name: str, obj: str) -> tuple[str, dict]:
        if not isinstance(ans, HttpAnswer):
            return UNKNOWN, {}
        if 400 <= ans.status < 500 and ans.status not in (408,):
            return REFUSED, {}
        try:
            data = self._data(ans)
        except UnknownState:
            return UNKNOWN, {}
        payload = data.get(name)
        if not isinstance(payload, dict):
            return UNKNOWN, {}
        errs = payload.get("userErrors")
        if not isinstance(errs, list):
            return UNKNOWN, {}
        if errs:
            return REFUSED, {}
        got = payload.get(obj)
        if obj == "deletedUrlRedirectId":
            return (APPLIED, {"id": got}) if isinstance(got, str) and got else (UNKNOWN, {})
        if obj == "deletedMetafields":
            return (APPLIED, {}) if isinstance(got, list) else (UNKNOWN, {})
        if obj == "metafields":
            return (APPLIED, {"metafield": got[0]}) if isinstance(got, list) and len(got) == 1 \
                and isinstance(got[0], dict) else (UNKNOWN, {})
        return (APPLIED, {"object": got}) if isinstance(got, dict) and isinstance(got.get("id"), str) else (UNKNOWN, {})

    # ---------------------------------------------------------------- read

    def read(self, account: str, keys: list, ctx: dict, call: Call) -> dict:
        out: dict = {}
        cache: dict = {}
        for target, fld in keys:
            if fld.startswith("metafield:"):
                ns, key = METAFIELD.fullmatch(fld).groups()
                d = self._data(call(self._req(account, METAFIELD_READ, {"id": target, "namespace": ns, "key": key})))
                prod = d.get("product")
                if not isinstance(prod, dict) or prod.get("id") != target:
                    raise UnknownState("product not found")
                mf = prod.get("metafield")
                if mf is None:
                    out[(target, fld)] = None
                    ctx.setdefault("mf", {})[(target, fld)] = None
                elif isinstance(mf, dict) and isinstance(mf.get("type"), str) and isinstance(mf.get("value"), str):
                    out[(target, fld)] = {"type": mf["type"], "value": mf["value"]}
                    ctx.setdefault("mf", {})[(target, fld)] = mf.get("compareDigest")
                else:
                    raise UnknownState("metafield shape")
            elif target.startswith("gid://shopify/Product/"):
                if target not in cache:
                    d = self._data(call(self._req(account, PRODUCT_READ, {"id": target})))
                    p = d.get("product")
                    if not isinstance(p, dict) or p.get("id") != target or not isinstance(p.get("seo"), dict):
                        raise UnknownState("product not found")
                    cache[target] = p
                p = cache[target]
                val = p["seo"].get(fld[4:]) if fld.startswith("seo.") else p.get(fld)
                if val is not None and not isinstance(val, str):
                    raise UnknownState("product field shape")
                out[(target, fld)] = val
            elif target.startswith("gid://shopify/Page/"):
                if target not in cache:
                    d = self._data(call(self._req(account, PAGE_READ, {"id": target})))
                    p = d.get("page")
                    if not isinstance(p, dict) or p.get("id") != target:
                        raise UnknownState("page not found")
                    cache[target] = p
                val = cache[target].get(fld)
                if not isinstance(val, str):
                    raise UnknownState("page field shape")
                out[(target, fld)] = val
            elif target.startswith("redirect:"):
                path = target[len("redirect:"):]
                d = self._data(call(self._req(account, REDIRECT_BY_PATH, {"q": f"path:{path}"})))
                conn = d.get("urlRedirects")
                nodes = conn.get("nodes") if isinstance(conn, dict) else None
                if not isinstance(nodes, list):
                    raise UnknownState("urlRedirects shape")
                exact = [n for n in nodes if isinstance(n, dict) and n.get("path") == path]
                if len(exact) > 1 or not all(isinstance(n.get("id"), str) and isinstance(n.get("target"), str)
                                             for n in exact):
                    raise UnknownState("ambiguous redirect")
                ctx.setdefault("redirect_id", {})[target] = exact[0]["id"] if exact else None
                out[(target, fld)] = exact[0]["target"] if exact else None
            else:
                raise UnknownState("target")
        return out

    # ---------------------------------------------------------------- write

    def write(self, account: str, key, value, ctx: dict, call: Call) -> tuple[str, dict]:
        target, fld = key
        state = ctx.setdefault("state", {})
        try:
            if fld.startswith("metafield:"):
                ns, mkey = METAFIELD.fullmatch(fld).groups()
                if value is None:
                    ans = call(self._req(account, METAFIELDS_DELETE,
                                         {"metafields": [{"ownerId": target, "namespace": ns, "key": mkey}]}))
                    out = self._mutation(ans, "metafieldsDelete", "deletedMetafields")
                    if out[0] == APPLIED:
                        ctx.setdefault("mf", {})[key] = None
                    return out
                item = {"ownerId": target, "namespace": ns, "key": mkey, "type": value["type"], "value": value["value"]}
                digest = ctx.get("mf", {}).get(key)
                if digest:
                    item["compareDigest"] = digest          # compare-and-swap against what we last read or wrote
                out = self._mutation(call(self._req(account, METAFIELDS_SET, {"metafields": [item]})),
                                     "metafieldsSet", "metafields")
                if out[0] == APPLIED:
                    ctx.setdefault("mf", {})[key] = out[1]["metafield"].get("compareDigest")
                return out
            if target.startswith("gid://shopify/Product/"):
                if fld.startswith("seo."):
                    seo = {"title": state.get((target, "seo.title")), "description": state.get((target, "seo.description"))}
                    seo[fld[4:]] = value
                    product = {"id": target, "seo": seo}
                else:
                    product = {"id": target, fld: value}
                return self._mutation(call(self._req(account, PRODUCT_UPDATE, {"product": product})),
                                      "productUpdate", "product")
            if target.startswith("gid://shopify/Page/"):
                return self._mutation(call(self._req(account, PAGE_UPDATE, {"id": target, "page": {fld: value}})),
                                      "pageUpdate", "page")
            if target.startswith("redirect:"):
                path = target[len("redirect:"):]
                rid = ctx.setdefault("redirect_id", {}).get(target)
                if value is None:
                    if rid is None:
                        return APPLIED, {}                  # nothing there: already absent
                    out = self._mutation(call(self._req(account, REDIRECT_DELETE, {"id": rid})),
                                         "urlRedirectDelete", "deletedUrlRedirectId")
                    if out[0] == APPLIED:
                        ctx["redirect_id"][target] = None
                    return out
                if rid is None:
                    out = self._mutation(call(self._req(account, REDIRECT_CREATE,
                                                        {"urlRedirect": {"path": path, "target": value}})),
                                         "urlRedirectCreate", "urlRedirect")
                else:
                    out = self._mutation(call(self._req(account, REDIRECT_UPDATE,
                                                        {"id": rid, "urlRedirect": {"path": path, "target": value}})),
                                         "urlRedirectUpdate", "urlRedirect")
                if out[0] == APPLIED:
                    ctx["redirect_id"][target] = out[1]["object"]["id"]
                return out
        except UnknownState:
            return UNKNOWN, {}
        return UNKNOWN, {}
