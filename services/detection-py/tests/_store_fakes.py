"""Fakes for the store connector tests (ADR 0020): a fake Shopify GraphQL Admin API behind httpx.MockTransport, an
injected clock, an in-memory ledger, and secret/connection file writers. No network, no wall clock."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import httpx

from store_connector import shopify_models as m

TOKEN = "shpat_TESTONLY_9f8e7d6c5b4a39281706f5e4d3c2b1a0"          # a fake value, never a real token
TOKEN_B = "shpat_TESTONLY_b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0"
CC_ID = "cc-client-id-TESTONLY-1234567890"
CC_SECRET = "cc-secret-TESTONLY-abcdefabcdefabcdefabcdef"
CC_TOKEN = "shpat_TESTONLY_exchanged_aaaaaaaaaaaaaaaaaaaaaaaa"
NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
SECRETS = (TOKEN, TOKEN_B, CC_ID, CC_SECRET, CC_TOKEN)


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


class MemoryLedger:
    def __init__(self, fail_types: tuple = ()):
        self.events: list[dict] = []
        self.fail_types = fail_types

    def record(self, event_id, event_type, subject_id, payload, summary) -> None:
        from store_connector.ledger import LedgerNotRecorded
        if event_type in self.fail_types:
            raise LedgerNotRecorded("down")
        self.events.append({"event_id": event_id, "event_type": event_type, "subject_id": subject_id,
                            "payload": json.loads(json.dumps(payload, default=str)), "summary": summary})

    def __repr__(self) -> str:
        return "MemoryLedger()"


def money(amount: str, cur: str = "USD") -> dict:
    return {"shopMoney": {"amount": amount, "currencyCode": cur}}


def code_app(code: str, *, pct: float | None = None, amount: str | None = None, alloc: str = "ACROSS",
             sel: str = "ALL", target: str = "LINE_ITEM") -> dict:
    value = ({"__typename": "PricingPercentageValue", "percentage": pct} if pct is not None
             else {"__typename": "MoneyV2", "amount": amount, "currencyCode": "USD"})
    return {"__typename": "DiscountCodeApplication", "allocationMethod": alloc, "targetSelection": sel,
            "targetType": target, "value": value, "code": code}


def allocation(index: int, amount: str) -> dict:
    return {"allocatedAmountSet": money(amount), "discountApplication": {"index": index}}


def shopify_allocations(apps: list, lines: list) -> list:
    """Per-line ``discountAllocations`` as Shopify records them for order-level (ALL / LINE_ITEM) discounts, per its
    combination rules (help.shopify.com/manual/discounts/discount-combinations): every percentage discount is
    computed on the same subtotal, then fixed-amount discounts apply to what is left. The whole amount is put on the
    first priced line (only the per-application sum matters to the connector). Shipping discounts get none."""
    out = [[] for _ in lines]
    priced = [i for i, li in enumerate(lines) if Decimal(li["originalUnitPriceSet"]["shopMoney"]["amount"]) > 0
              and li["quantity"] > 0]
    if not priced:
        return out
    subtotal = sum(Decimal(lines[i]["originalUnitPriceSet"]["shopMoney"]["amount"]) * lines[i]["quantity"]
                   for i in priced)
    pct_given = Decimal("0.00")
    for a in apps:
        if a["targetType"] == "LINE_ITEM" and a["value"]["__typename"] == "PricingPercentageValue":
            g = (subtotal * Decimal(str(a["value"]["percentage"])) / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
            pct_given += g
            if g > 0:
                out[priced[0]].append(allocation(a["index"], str(g)))
    remaining = subtotal - pct_given
    for a in apps:
        if a["targetType"] == "LINE_ITEM" and a["value"]["__typename"] == "MoneyV2":
            g = max(min(Decimal(a["value"]["amount"]), remaining), Decimal("0.00"))
            remaining -= g
            if g > 0:
                out[priced[0]].append(allocation(a["index"], str(g)))
    return out


def order(n: int, *, codes: list | None = None, lines: list | None = None, status: str = "PAID",
          created: datetime | None = None, test: bool = False, cancelled: bool = False,
          more_lines: bool = False, extra: dict | None = None, refunded: str = "0.00",
          allocations: list | None = None, total_discounts: str | None = None, edited: bool = False) -> dict:
    """A fake Shopify order node. Discount applications get their position as ``index`` unless they carry one;
    lines get ``currentQuantity = quantity`` unless set, and ``discountAllocations`` from ``allocations`` (one list
    per line) or, by default, as Shopify would record them (``shopify_allocations``)."""
    apps = [{"index": i, **a} for i, a in enumerate(codes or [])]
    lines = [dict(li) for li in (lines if lines is not None else
                                 [{"sku": f"SKU-{n}", "quantity": 2, "originalUnitPriceSet": money("50.00")}])]
    for li in lines:
        li.setdefault("currentQuantity", li["quantity"])
    try:
        default_allocs = shopify_allocations(apps, lines)
    except (ArithmeticError, KeyError, TypeError, ValueError):
        default_allocs = [[] for _ in lines]   # malformed money in a test of the money checks: nothing allocated
    for i, li in enumerate(lines):
        li.setdefault("discountAllocations", allocations[i] if allocations is not None else default_allocs[i])
    if total_discounts is None:     # default: what Shopify records, the sum of every line allocation
        try:
            total_discounts = str(sum((Decimal(al["allocatedAmountSet"]["shopMoney"]["amount"])
                                       for li in lines for al in li["discountAllocations"]), Decimal("0.00")))
        except (ArithmeticError, KeyError, TypeError, ValueError):
            total_discounts = "0.00"
    node = {
        "legacyResourceId": str(5000 + n),
        "createdAt": (created or (NOW - timedelta(hours=n))).isoformat().replace("+00:00", "Z"),
        "cancelledAt": (NOW - timedelta(hours=1)).isoformat() if cancelled else None,
        "test": test,
        "currencyCode": "USD",
        "displayFinancialStatus": status,
        "edited": edited,
        "totalRefundedSet": money(refunded),
        "totalDiscountsSet": money(total_discounts),
        "discountApplications": {"pageInfo": {"hasNextPage": False}, "nodes": apps},
        "lineItems": {"pageInfo": {"hasNextPage": more_lines}, "nodes": lines},
    }
    if extra:
        node.update(extra)
    return node


def cost(requested=100, actual=50, available=900, restore=100, maximum=1000) -> dict:
    return {"cost": {"requestedQueryCost": requested, "actualQueryCost": actual,
                     "throttleStatus": {"maximumAvailable": maximum, "currentlyAvailable": available,
                                        "restoreRate": restore}}}


class FakeShopify:
    """A scripted Shopify. ``script`` (optional) is a list of callables/responses consumed per GraphQL request
    before the default behaviour; each entry is an httpx.Response or a function(request) -> httpx.Response."""

    def __init__(self, shops: dict | None = None, *, scopes=("read_orders",), currency="USD", orders=None,
                 tokens=None, version=m.API_VERSION, script=None, cc_scope="read_orders"):
        self.shops = shops or {"acme-store": orders if orders is not None else []}
        self.scopes = list(scopes)
        self.currency = currency
        self.tokens = tokens or {"acme-store": TOKEN}
        self.version = version
        self.script = list(script or [])
        self.cc_scope = cc_scope
        self.requests: list[httpx.Request] = []
        self.graphql_bodies: list[dict] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def json(self, obj, status=200, headers=None) -> httpx.Response:
        h = {"content-type": "application/json", "x-shopify-api-version": self.version}
        h.update(headers or {})
        return httpx.Response(status, content=json.dumps(obj).encode(), headers=h)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        assert request.url.scheme == "https" and host.endswith(".myshopify.com"), request.url
        handle = host[: -len(".myshopify.com")]
        if request.url.path == "/admin/oauth/access_token":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            if form.get("client_id") != CC_ID or form.get("client_secret") != CC_SECRET:
                return httpx.Response(401, content=b"{}")
            self.tokens[handle] = CC_TOKEN
            return httpx.Response(200, headers={"content-type": "application/json"},
                                  content=json.dumps({"access_token": CC_TOKEN, "scope": self.cc_scope,
                                                      "expires_in": 86399}).encode())
        assert request.url.path == f"/admin/api/{m.API_VERSION}/graphql.json"
        body = json.loads(request.content)
        self.graphql_bodies.append(body)
        if self.script:
            step = self.script.pop(0)
            if step is not None:
                return step(request) if callable(step) else step
        if request.headers.get("x-shopify-access-token") != self.tokens.get(handle):
            return httpx.Response(401, content=b'{"errors":"[API] Invalid API key or access token"}')
        if body["query"] == m.PREFLIGHT_QUERY:
            return self.json({"data": {"shop": {"currencyCode": self.currency,
                                                "myshopifyDomain": f"{handle}.myshopify.com"},
                                       "currentAppInstallation": {"accessScopes":
                                                                  [{"handle": s} for s in self.scopes]}},
                              "extensions": cost(requested=2, actual=2)})
        assert body["query"] == m.ORDERS_QUERY
        nodes = self.shops[handle]
        v = body["variables"]
        start = int(v["after"].split(":")[1]) if v["after"] else 0
        page = nodes[start:start + v["first"]]
        nxt = start + len(page)
        return self.json({"data": {"orders": {"pageInfo": {"hasNextPage": nxt < len(nodes),
                                                           "endCursor": f"c:{nxt}" if page else None},
                                              "nodes": page}},
                          "extensions": cost()})


def write_secret(dirpath, name: str, value: str, mode: int = 0o600) -> str:
    p = os.path.join(str(dirpath), name)
    with open(p, "w") as f:
        f.write(value + "\n")
    os.chmod(p, mode)
    return p


def write_connections(dirpath, conns: list, name: str = "connections.json") -> str:
    p = os.path.join(str(dirpath), name)
    with open(p, "w") as f:
        json.dump({"version": 1, "connections": conns}, f)
    os.chmod(p, 0o600)
    return p


def conn(client_id: str, shop: str, token_file: str, **budget) -> dict:
    c = {"client_id": client_id, "platform": "shopify", "shop": shop,
         "credential": {"kind": "static_token", "token_file": token_file},
         "approval": {"approved_by": "andre", "approved_on": "2026-10-24", "approval_ref": "andre-approval-1"}}
    if budget:
        c["budget"] = budget
    return c
