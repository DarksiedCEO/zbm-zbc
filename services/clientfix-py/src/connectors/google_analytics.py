"""
GA4 — Google Analytics Admin API v1beta, key events (conversion tracking). VERIFIED (docs fetched Oct 6 2026).

  OAuth      Google OAuth 2.0 for web server apps: https://accounts.google.com/o/oauth2/v2/auth, token at
             https://oauth2.googleapis.com/token, ``access_type=offline`` for a refresh token, revocation at
             https://oauth2.googleapis.com/revoke — https://developers.google.com/identity/protocols/oauth2/web-server
  list       GET https://analyticsadmin.googleapis.com/v1beta/{parent=properties/*}/keyEvents  (pageSize 1-200,
             pageToken; answer {keyEvents[], nextPageToken}; analytics.readonly or analytics.edit)
             https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties.keyEvents/list
  create     POST https://analyticsadmin.googleapis.com/v1beta/{parent=properties/*}/keyEvents, body KeyEvent, answer
             the new KeyEvent (analytics.edit)
             https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties.keyEvents/create
  delete     DELETE https://analyticsadmin.googleapis.com/v1beta/{name=properties/*/keyEvents/*}, answer {} (edit)
             https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties.keyEvents/delete
  KeyEvent   name (output), eventName (immutable), createTime, deletable (output), custom, countingMethod
             (ONCE_PER_EVENT | ONCE_PER_SESSION), defaultValue {numericValue, currencyCode}
             https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties.keyEvents

Version 1 allowlist: mark an event as a key event (create) and unmark one (delete, only when ``deletable``). A
deleted key event's whole snapshot is kept, so a rollback re-creates it with its ``defaultValue`` (AEGIS round 1 H2
audit: no partial-object loss). Changing a
counting method in place is not in the allowlist (``patch`` exists but its update mask was not verified here). No dry
run exists: offline validation only. The field is ``key_event:<eventName>``; its value is ``{"countingMethod": ...}``
or None (not a key event).
"""

from __future__ import annotations

import re
from urllib.parse import quote

from connectors.base import (APPLIED, REFUSED, UNKNOWN, Call, Connector, HttpAnswer, HttpRequest, OpRefused, OpSpec,
                             UnknownState, one_of)

BASE = "https://analyticsadmin.googleapis.com/v1beta"
PROPERTY = re.compile(r"properties/[1-9][0-9]{0,19}")
KEY_EVENT_FIELD = re.compile(r"key_event:([A-Za-z][A-Za-z0-9_]{0,39})")
KEY_EVENT_NAME = re.compile(r"properties/[1-9][0-9]{0,19}/keyEvents/[0-9]{1,30}")
MAX_PAGES = 10


def _ke_value(v) -> bool:
    return isinstance(v, dict) and set(v) == {"countingMethod"} and one_of("ONCE_PER_EVENT", "ONCE_PER_SESSION")(
        v["countingMethod"])


OPS = {"ga4.key_event.set": OpSpec("ga4.key_event.set", PROPERTY, {}, ((KEY_EVENT_FIELD, _ke_value),), create=True,
                                   remove=True, target_check=lambda account, target: account == target)}


class GA4Connector(Connector):
    def __init__(self):
        super().__init__(name="ga4", status="verified", account_ref=PROPERTY, ops=OPS,
                         scopes=("https://www.googleapis.com/auth/analytics.edit",),
                         docs=("https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/"
                               "properties.keyEvents",
                               "https://developers.google.com/identity/protocols/oauth2/web-server"))

    def extra_checks(self, op: dict) -> None:
        if op["before"] is not None and op["after"] is not None:
            raise OpRefused("OP_VALUE_INVALID")          # create or delete only (no in-place patch in v1)

    def read(self, account: str, keys: list, ctx: dict, call: Call) -> dict:
        events: dict = {}
        token = None
        for _ in range(MAX_PAGES):
            url = f"{BASE}/{account}/keyEvents?pageSize=200" + (f"&pageToken={quote(token, safe='')}" if token else "")
            ans = call(HttpRequest("GET", url, None))
            if not isinstance(ans, HttpAnswer) or ans.status != 200 or not isinstance(ans.body, dict):
                raise UnknownState("keyEvents.list answer")
            for ke in ans.body.get("keyEvents", []) or []:
                if not isinstance(ke, dict) or not isinstance(ke.get("eventName"), str) \
                        or not isinstance(ke.get("name"), str) or not KEY_EVENT_NAME.fullmatch(ke["name"]):
                    raise UnknownState("KeyEvent shape")
                events[ke["eventName"]] = ke
            token = ans.body.get("nextPageToken")
            if not token:
                break
            if not isinstance(token, str):
                raise UnknownState("nextPageToken")
        else:
            raise UnknownState("too many key events to read")
        names = ctx.setdefault("ke_name", {})
        deletable = ctx.setdefault("ke_deletable", {})
        first = ctx.setdefault("ke_snapshot", {})
        out = {}
        for target, fld in keys:
            ev = KEY_EVENT_FIELD.fullmatch(fld).group(1)
            ke = events.get(ev)
            if (target, fld) not in first:            # the FIRST read is the snapshot: kept whole (H2 audit)
                first[(target, fld)] = dict(ke) if ke else None
            names[(target, fld)] = ke["name"] if ke else None
            deletable[(target, fld)] = bool(ke.get("deletable")) if ke else False
            if ke is None:
                out[(target, fld)] = None
            else:
                cm = ke.get("countingMethod")
                if cm not in ("ONCE_PER_EVENT", "ONCE_PER_SESSION"):
                    raise UnknownState("countingMethod")
                out[(target, fld)] = {"countingMethod": cm}
        return out

    def dry_run(self, account: str, ops: list, ctx: dict, call: Call) -> tuple[str, str]:
        for op in ops:                                   # a key event Google marks non-deletable is refused up front
            if op["after"] is None and not ctx.get("ke_deletable", {}).get((op["target"], op["field"])):
                return REFUSED, "offline"
        return APPLIED, "offline"

    def write(self, account: str, key, value, ctx: dict, call: Call) -> tuple[str, dict]:
        target, fld = key
        ev = KEY_EVENT_FIELD.fullmatch(fld).group(1)
        names = ctx.setdefault("ke_name", {})
        try:
            if value is None:
                name = names.get(key)
                if name is None:
                    return APPLIED, {}
                ans = call(HttpRequest("DELETE", f"{BASE}/{name}", None))
                if isinstance(ans, HttpAnswer) and ans.status == 200 and ans.body in ({}, None):
                    names[key] = None
                    return APPLIED, {}
            else:
                body = {"eventName": ev, "countingMethod": value["countingMethod"]}
                snap = ctx.get("ke_snapshot", {}).get(key)
                if snap and snap.get("countingMethod") == value["countingMethod"] \
                        and isinstance(snap.get("defaultValue"), dict):
                    body["defaultValue"] = dict(snap["defaultValue"])   # restoring a deleted key event restores it whole
                ans = call(HttpRequest("POST", f"{BASE}/{account}/keyEvents", body))
                b = ans.body if isinstance(ans, HttpAnswer) else None
                if isinstance(ans, HttpAnswer) and ans.status == 200 and isinstance(b, dict) \
                        and b.get("eventName") == ev and isinstance(b.get("name"), str) \
                        and KEY_EVENT_NAME.fullmatch(b["name"]):
                    names[key] = b["name"]
                    return APPLIED, {"name": b["name"]}
            if isinstance(ans, HttpAnswer) and 400 <= ans.status < 500 and ans.status not in (408,):
                return REFUSED, {}
        except UnknownState:
            pass
        return UNKNOWN, {}
