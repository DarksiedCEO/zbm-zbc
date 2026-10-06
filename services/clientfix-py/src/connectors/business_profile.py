"""
Google Business Profile — Business Information API v1. VERIFIED, GATED (docs fetched Oct 6 2026).

  access     Google must approve the Cloud project first: "Submit your request using our GBP API contact form" (Basic
             API Access); the applicant must manage a profile "verified and active for 60+ days" with a website;
             "0 QPM means not approved", 300 QPM approved — https://developers.google.com/my-business/content/prereqs
  get        GET https://mybusinessbusinessinformation.googleapis.com/v1/{name=locations/*}?readMask=<fields>
             (business.manage) — https://developers.google.com/my-business/reference/businessinformation/rest/v1/locations/get
  patch      PATCH https://mybusinessbusinessinformation.googleapis.com/v1/{location.name=locations/*}?updateMask=<fields>
             [&validateOnly=true: "Validates the request without updating"]; body Location; answer the updated Location
             (business.manage) — .../rest/v1/locations/patch
  Location   name, title, phoneNumbers {primaryPhone, additionalPhones}, storefrontAddress (PostalAddress: regionCode,
             languageCode, postalCode, administrativeArea, locality, addressLines, ...), websiteUri
             — .../rest/v1/accounts.locations
  OAuth      https://developers.google.com/identity/protocols/oauth2/web-server (scope
             https://www.googleapis.com/auth/business.manage)

The dry run is real here: PATCH with ``validateOnly=true``. Version 1 allowlist: the primary phone, the website URI and
the storefront address (the subfields listed below only). A read-back that does not show the new value (for example an
edit Google holds for review) is a mismatch and is rolled back; ADR 0017 known limits.
"""

from __future__ import annotations

import re

from connectors.base import (APPLIED, REFUSED, UNKNOWN, Call, Connector, HttpAnswer, HttpRequest, OpSpec,
                             UnknownState)

BASE = "https://mybusinessbusinessinformation.googleapis.com/v1"
LOCATION = re.compile(r"locations/[0-9]{1,30}")
PHONE = re.compile(r"\+?[0-9(][0-9 ()\-.]{6,24}")
URL = re.compile(r"https://[a-z0-9.-]{1,253}(?::[0-9]{1,5})?(?:/[\x21-\x7e]{0,1800})?")
ADDRESS_FIELDS = ("regionCode", "languageCode", "postalCode", "administrativeArea", "locality", "addressLines")
FIELDS = ("phoneNumbers.primaryPhone", "websiteUri", "storefrontAddress")


def _phone(v) -> bool:
    return isinstance(v, str) and bool(PHONE.fullmatch(v)) and 7 <= sum(c.isdigit() for c in v) <= 15


def _url(v) -> bool:
    return isinstance(v, str) and bool(URL.fullmatch(v)) and "@" not in v


def _address(v) -> bool:
    if not isinstance(v, dict) or not v or not set(v) <= set(ADDRESS_FIELDS):
        return False
    for k, x in v.items():
        if k == "addressLines":
            if not (isinstance(x, list) and 1 <= len(x) <= 5 and all(isinstance(s, str) and 1 <= len(s) <= 200
                                                                      for s in x)):
                return False
        elif not (isinstance(x, str) and 1 <= len(x) <= 100 and x.isprintable()):
            return False
    return True


OPS = {"gbp.location.patch": OpSpec("gbp.location.patch", LOCATION,
                                    {"phoneNumbers.primaryPhone": _phone, "websiteUri": _url,
                                     "storefrontAddress": _address},
                                    target_check=lambda account, target: account == target)}


UNLISTED = "storefrontAddress#unlisted"      # companion key: the address subfields the allowlist does not name


def _body(fld: str, value) -> dict:
    if fld == "phoneNumbers.primaryPhone":
        return {"phoneNumbers": {"primaryPhone": value}}
    return {fld: value}


class BusinessProfileConnector(Connector):
    def __init__(self):
        super().__init__(name="gbp", status="verified_gated", account_ref=LOCATION, ops=OPS, max_dry_run="validateOnly",
                         scopes=("https://www.googleapis.com/auth/business.manage",),
                         docs=("https://developers.google.com/my-business/reference/businessinformation/rest/v1/"
                               "locations/patch", "https://developers.google.com/my-business/content/prereqs"))

    def read(self, account: str, keys: list, ctx: dict, call: Call) -> dict:
        ans = call(HttpRequest("GET", f"{BASE}/{account}?readMask=phoneNumbers,websiteUri,storefrontAddress", None))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if not isinstance(ans, HttpAnswer) or ans.status != 200 or not isinstance(b, dict):
            raise UnknownState("locations.get answer")
        out = {}
        for target, fld in keys:
            if fld == "phoneNumbers.primaryPhone":
                pn = b.get("phoneNumbers") or {}
                v = pn.get("primaryPhone") if isinstance(pn, dict) else None
                if v is not None and not isinstance(v, str):
                    raise UnknownState("primaryPhone")
            elif fld == "websiteUri":
                v = b.get("websiteUri")
                if v is not None and not isinstance(v, str):
                    raise UnknownState("websiteUri")
            elif fld in ("storefrontAddress", UNLISTED):
                a = b.get("storefrontAddress")
                if a is not None and not isinstance(a, dict):
                    raise UnknownState("storefrontAddress")
                if fld == UNLISTED:
                    v = {k: a[k] for k in sorted(a) if k not in ADDRESS_FIELDS} if a else {}
                else:
                    v = {k: a[k] for k in ADDRESS_FIELDS if a and k in a} or None
            else:
                raise UnknownState("field")
            out[(target, fld)] = v
        return out

    def companions(self, keys: list) -> list:
        """``updateMask=storefrontAddress`` REPLACES the whole PostalAddress (AEGIS round 1 M3): its subfields outside
        the allowlist (sublocality, recipients, organization, sortingCode, revision, ...) are snapshotted as a
        companion, merged back into every address write, and verified unchanged. ``phoneNumbers.primaryPhone`` and
        ``websiteUri`` are masked to exactly one scalar: no sibling is touched (additionalPhones stays)."""
        out = []
        for target, fld in keys:
            if fld == "storefrontAddress" and (target, UNLISTED) not in keys and (target, UNLISTED) not in out:
                out.append((target, UNLISTED))
        return out

    def _merged(self, key, fld: str, value, ctx: dict):
        if fld != "storefrontAddress" or value is None:
            return value
        extra = ctx.get("state", {}).get((key[0], UNLISTED))
        if not isinstance(extra, dict):
            raise UnknownState("the unlisted address subfields were not snapshotted")
        return {**extra, **value}

    def _patch(self, account: str, fld: str, value, validate_only: bool) -> HttpRequest:
        mask = "phoneNumbers.primaryPhone" if fld == "phoneNumbers.primaryPhone" else fld
        q = f"?updateMask={mask}" + ("&validateOnly=true" if validate_only else "")
        return HttpRequest("PATCH", f"{BASE}/{account}{q}", _body(fld, value))

    def dry_run(self, account: str, ops: list, ctx: dict, call: Call) -> tuple[str, str]:
        for op in ops:
            try:
                value = self._merged((op["target"], op["field"]), op["field"], op["after"], ctx)
            except UnknownState:
                return UNKNOWN, "validateOnly"
            ans = call(self._patch(account, op["field"], value, True))
            if not isinstance(ans, HttpAnswer):
                return UNKNOWN, "validateOnly"
            if 400 <= ans.status < 500:
                return REFUSED, "validateOnly"
            if ans.status != 200:
                return UNKNOWN, "validateOnly"
        return APPLIED, "validateOnly"

    def write(self, account: str, key, value, ctx: dict, call: Call) -> tuple[str, dict]:
        target, fld = key
        try:
            value = self._merged(key, fld, value, ctx)
        except UnknownState:
            return UNKNOWN, {}
        ans = call(self._patch(account, fld, value, False))
        if isinstance(ans, HttpAnswer) and ans.status == 200 and isinstance(ans.body, dict):
            return APPLIED, {}
        if isinstance(ans, HttpAnswer) and 400 <= ans.status < 500 and ans.status not in (408,):
            return REFUSED, {}
        return UNKNOWN, {}
