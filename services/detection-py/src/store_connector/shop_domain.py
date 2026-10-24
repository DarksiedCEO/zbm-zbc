"""The only host the connector ever names: ``<handle>.myshopify.com`` (ADR 0020 section 4, SSRF).

A shop is registered as its handle or as its full ``<handle>.myshopify.com`` domain. Everything else is refused:
schemes, ports, paths, userinfo, query strings, trailing dots, other domains (``evil.com``, ``myshopify.com.evil``),
nested labels (``a.b.myshopify.com``), address literals, IDN/punycode, and any non-ASCII character (checked BEFORE
lowercasing, so a Unicode look-alike such as the Kelvin sign cannot case-fold into ASCII).
"""

from __future__ import annotations

import re

SHOPIFY_SUFFIX = ".myshopify.com"
_HANDLE = re.compile(r"[a-z0-9][a-z0-9-]{1,58}[a-z0-9]")


class ShopDomainError(ValueError):
    pass


def shop_handle(raw: object) -> str:
    """The validated handle (lowercase), or ShopDomainError. The handle never contains a dot, a colon or a slash."""
    if not isinstance(raw, str) or not raw or len(raw) > 80 or not raw.isascii():
        raise ShopDomainError("shop must be an ASCII handle or <handle>.myshopify.com")
    text = raw.lower()
    if text.endswith(SHOPIFY_SUFFIX):
        text = text[: -len(SHOPIFY_SUFFIX)]
    if not _HANDLE.fullmatch(text) or text.startswith("xn--") or "--" in text[2:4]:
        raise ShopDomainError("shop must be an ASCII handle or <handle>.myshopify.com")
    return text


def shop_host(handle: str) -> str:
    return f"{shop_handle(handle)}{SHOPIFY_SUFFIX}"


def graphql_url(handle: str, api_version: str) -> str:
    return f"https://{shop_host(handle)}/admin/api/{api_version}/graphql.json"


def token_url(handle: str) -> str:
    return f"https://{shop_host(handle)}/admin/oauth/access_token"
