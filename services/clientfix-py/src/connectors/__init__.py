"""
The connector registry (ADR 0017 decisions 11-13). Four platforms are implemented against officially documented APIs
(each module cites its pages); Yelp is a guided manual fix; everything else is NOT_BUILT and has an empty allowlist,
so a plan naming it is refused ``CONNECTOR_NOT_BUILT``. No connector is WIRED: the transport that would carry its
requests (CFX_CONNECTOR_TRANSPORT) is not built, so every apply answers ``CONNECTOR_NOT_WIRED`` before anything is
touched (ports.NotWiredTransport).
"""

from __future__ import annotations

from connectors.base import Connector
from connectors.business_profile import BusinessProfileConnector
from connectors.google_analytics import GA4Connector
from connectors.shopify import ShopifyConnector
from connectors.tag_manager import TagManagerConnector
from connectors.yelp import YelpConnector

NOT_BUILT_WHY = {
    "woocommerce": "official app flow issues long-lived REST API keys, not OAuth tokens "
                   "(https://woocommerce.github.io/woocommerce-rest-api-docs/#authentication-endpoint)",
    "shopify_checkout": "checkout settings are not in the version-1 allowlist (checkout extensibility not verified)",
    "shopify_theme": "theme file writes (site speed) are not in the version-1 allowlist (too broad to allowlist)",
    "ad_pixels": "Meta / TikTok / other ad pixels: no connector verified yet",
    "service_automations": "Customer Service & Success (29-30): service-py has no client-automation route",
    "sales_automations": "Sales (27): sales-py has no client-automation route",
    "crm": "third-party CRMs: none built",
}


def registry() -> dict[str, Connector]:
    reg: dict[str, Connector] = {c.name: c for c in (ShopifyConnector(), GA4Connector(), TagManagerConnector(),
                                                      BusinessProfileConnector(), YelpConnector())}
    for name in NOT_BUILT_WHY:
        reg[name] = Connector(name=name, status="not_built")
    return reg


def describe() -> list[dict]:
    out = []
    for name, c in sorted(registry().items()):
        out.append({"connector": name, "status": c.status, "operations": sorted(c.ops), "dry_run": c.max_dry_run,
                    "docs": list(c.docs), "why_not_built": NOT_BUILT_WHY.get(name)})
    return out
