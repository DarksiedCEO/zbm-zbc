"""
Yelp — GUIDED MANUAL FIX. There is no write API for business information that we may use:

  * Yelp Fusion's Business Details is read-only: GET https://api.yelp.com/v3/businesses/{business_id_or_alias}, a
    Bearer API key, answer {id, alias, name, phone, display_phone, url, is_closed, location {address1, address2,
    address3, city, zip_code, country, state, display_address}} — https://docs.developer.yelp.com/reference/v3_business_info
  * Yelp's Data Ingestion API can update phone and address, but "Access to the Data Ingestion API is reserved for
    contracted Yelp partners" and is "disabled by default" — https://docs.developer.yelp.com/docs/data-ingestion-api

So this connector NEVER writes and never claims to have written. ``instructions`` produces exact steps (field, current
value, required value) for the client or Andre to make in Yelp for Business; Andre or the client reports it done;
the change is then verified through the Fusion read (with OUR app's read key, ``auth="yelp_app"`` — not the client's
credential), and only a read showing the required value counts. Until Andre chooses a Yelp plan the read key does not
exist (CFX_YELP_API_KEY_REF is NOT_BUILT), so a guided fix stays ``manual_reported`` and is never counted fixed.
"""

from __future__ import annotations

import re

from connectors.base import Call, Connector, HttpAnswer, HttpRequest, OpSpec, UnknownState

BASE = "https://api.yelp.com/v3/businesses"
BUSINESS = re.compile(r"[A-Za-z0-9_-]{22}|[a-z0-9][a-z0-9-]{2,119}")
E164 = re.compile(r"\+[1-9][0-9]{6,14}")
LOCATION_FIELDS = ("address1", "address2", "address3", "city", "zip_code", "state", "country")


def _phone(v) -> bool:
    return isinstance(v, str) and bool(E164.fullmatch(v))


def _location(v) -> bool:
    # all seven fields, empty strings allowed: the read-back is compared field for field
    return (isinstance(v, dict) and set(v) == set(LOCATION_FIELDS)
            and all(isinstance(x, str) and len(x) <= 200 and x.isprintable() for x in v.values()))


OPS = {"yelp.business.set": OpSpec("yelp.business.set", BUSINESS, {"phone": _phone, "location": _location},
                                   target_check=lambda account, target: account == target)}


class YelpConnector(Connector):
    def __init__(self):
        super().__init__(name="yelp", status="guided_manual", account_ref=BUSINESS, ops=OPS, manual=True,
                         max_dry_run="none (manual)",
                         docs=("https://docs.developer.yelp.com/reference/v3_business_info",
                               "https://docs.developer.yelp.com/docs/data-ingestion-api"))

    def read(self, account: str, keys: list, ctx: dict, call: Call) -> dict:
        ans = call(HttpRequest("GET", f"{BASE}/{account}", None, auth="yelp_app"))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if not isinstance(ans, HttpAnswer) or ans.status != 200 or not isinstance(b, dict) \
                or not isinstance(b.get("location"), dict):
            raise UnknownState("business details answer")
        out = {}
        for target, fld in keys:
            if fld == "phone":
                v = b.get("phone")
                out[(target, fld)] = v if isinstance(v, str) and v else None
            else:
                loc = b["location"]
                if not all(loc.get(k) is None or isinstance(loc.get(k), str) for k in LOCATION_FIELDS):
                    raise UnknownState("location shape")
                out[(target, fld)] = {k: loc.get(k) or "" for k in LOCATION_FIELDS}
        return out

    def instructions(self, account: str, ops: list) -> list:
        steps = []
        for op in ops:
            label = "phone number" if op["field"] == "phone" else "address"
            steps.append({"where": "Yelp for Business (biz.yelp.com) > Business Information", "business": account,
                          "field": op["field"], "change": f"Set the {label} from the current value to the new value.",
                          "current": op["before"], "required": op["after"]})
        return steps
