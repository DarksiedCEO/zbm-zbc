"""
Google Tag Manager — Tag Manager API v2. VERIFIED (docs fetched Oct 6 2026). Base https://tagmanager.googleapis.com/
tagmanager/v2/; OAuth as for GA4 (https://developers.google.com/identity/protocols/oauth2/web-server).

  tags.get        GET  {base}{path=accounts/*/containers/*/workspaces/*/tags/*}  (tagmanager.edit.containers |
                  tagmanager.readonly) — .../rest/v2/accounts.containers.workspaces.tags/get
  tags.update     PUT  {base}{path}?fingerprint=<fp>, body Tag; "When provided, this fingerprint must match the
                  fingerprint of the tag in storage" (compare-and-swap; tagmanager.edit.containers)
                  — .../rest/v2/accounts.containers.workspaces.tags/update
  quick_preview   POST {base}{workspace path}:quick_preview, empty body; answer {containerVersion, syncStatus,
                  compilerError} (tagmanager.edit.containerversions) — .../accounts.containers.workspaces/quick_preview
  create_version  POST {base}{workspace path}:create_version, body {name, notes}; answer {containerVersion, syncStatus,
                  compilerError, newWorkspacePath}; it "deletes the workspace" (edit.containerversions)
                  — .../accounts.containers.workspaces/create_version
  versions.live   GET  {base}{accounts/*/containers/*}/versions:live; answer ContainerVersion {containerVersionId,
                  fingerprint, path, tag[] ...} (edit.containers | readonly) — .../accounts.containers.versions/live
  versions.publish POST {base}{accounts/*/containers/*/versions/*}:publish?fingerprint=<fp>; answer {containerVersion,
                  compilerError} (tagmanager.publish) — .../accounts.containers.versions/publish
  (all under https://developers.google.com/tag-platform/tag-manager/api/reference)

Version 1 allowlist: a tag's ``paused`` flag and its ``firingTriggerId`` list (a paused or mis-triggered conversion
tag). The tag's code, type and parameters are NOT editable (a Custom HTML tag would let an agent inject script into a
client's site). The workspace edit is staged; ``quick_preview`` must compile (the dry run of this platform) before a
version is created and published. Verification reads the LIVE version. Rollback after publishing re-publishes the
previously live version (snapshotted before any write); before publishing, the tag is written back in the workspace.
"""

from __future__ import annotations

import re

from connectors.base import (APPLIED, REFUSED, UNKNOWN, Call, Connector, HttpAnswer, HttpRequest, OpSpec,
                             UnknownState, boolean, same)

BASE = "https://tagmanager.googleapis.com/tagmanager/v2"
CONTAINER = re.compile(r"accounts/[0-9]{1,20}/containers/[0-9]{1,20}")
TAG = re.compile(r"(accounts/[0-9]{1,20}/containers/[0-9]{1,20})/workspaces/([0-9]{1,20})/tags/([0-9]{1,20})")
VERSION_PATH = re.compile(r"accounts/[0-9]{1,20}/containers/[0-9]{1,20}/versions/[0-9]{1,20}")


def _triggers(v) -> bool:
    return (isinstance(v, list) and len(v) <= 50 and all(isinstance(x, str) and re.fullmatch(r"[0-9]{1,20}", x)
                                                          for x in v) and v == sorted(set(v)))


def _in_container(account: str, target: str) -> bool:
    m = TAG.fullmatch(target)
    return bool(m) and m.group(1) == account


OPS = {"gtm.tag.update": OpSpec("gtm.tag.update", TAG, {"paused": boolean, "firingTriggerId": _triggers},
                                target_check=_in_container)}


def _value(tag: dict, fld: str):
    if fld == "paused":
        v = tag.get("paused", False)
        if not isinstance(v, bool):
            raise UnknownState("paused")
        return v
    ids = tag.get("firingTriggerId", [])
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        raise UnknownState("firingTriggerId")
    return sorted(set(ids))


class TagManagerConnector(Connector):
    def __init__(self):
        super().__init__(name="gtm", status="verified", account_ref=CONTAINER, ops=OPS, max_dry_run="quick_preview",
                         scopes=("https://www.googleapis.com/auth/tagmanager.edit.containers",
                                 "https://www.googleapis.com/auth/tagmanager.edit.containerversions",
                                 "https://www.googleapis.com/auth/tagmanager.publish"),
                         docs=("https://developers.google.com/tag-platform/tag-manager/api/reference/rest",))

    def _live(self, account: str, call: Call) -> dict:
        ans = call(HttpRequest("GET", f"{BASE}/{account}/versions:live", None))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if not isinstance(ans, HttpAnswer) or ans.status != 200 or not isinstance(b, dict) \
                or not isinstance(b.get("path"), str) or not VERSION_PATH.fullmatch(b["path"]) \
                or not isinstance(b.get("tag", []), list):
            raise UnknownState("versions.live answer")
        return b

    def read(self, account: str, keys: list, ctx: dict, call: Call) -> dict:
        out = {}
        if ctx.get("published"):
            # after publishing, the workspace is gone: verify against the LIVE version's tags, by tag id
            live = self._live(account, call)
            if live.get("path") != ctx.get("published_path"):
                raise UnknownState("the live version is not the one published")
            tags = {t.get("tagId"): t for t in live.get("tag", []) if isinstance(t, dict)}
            for target, fld in keys:
                tag = tags.get(TAG.fullmatch(target).group(3))
                if tag is None:
                    raise UnknownState("tag not in the live version")
                out[(target, fld)] = _value(tag, fld)
            return out
        if "live_before" not in ctx:
            ctx["live_before"] = self._live(account, call)["path"]
        raw = ctx.setdefault("tag", {})
        for target, fld in keys:
            ans = call(HttpRequest("GET", f"{BASE}/{target}", None))
            b = ans.body if isinstance(ans, HttpAnswer) else None
            if not isinstance(ans, HttpAnswer) or ans.status != 200 or not isinstance(b, dict) \
                    or b.get("path") != target or not isinstance(b.get("fingerprint"), str):
                raise UnknownState("tags.get answer")
            raw[target] = b
            out[(target, fld)] = _value(b, fld)
        return out

    def write(self, account: str, key, value, ctx: dict, call: Call) -> tuple[str, dict]:
        target, fld = key
        tag = ctx.get("tag", {}).get(target)
        if tag is None:
            return UNKNOWN, {}
        body = {k: v for k, v in tag.items()}
        body[fld] = value
        ans = call(HttpRequest("PUT", f"{BASE}/{target}?fingerprint={tag['fingerprint']}", body))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if isinstance(ans, HttpAnswer) and ans.status == 200 and isinstance(b, dict) and b.get("path") == target \
                and isinstance(b.get("fingerprint"), str):
            try:
                if not same(_value(b, fld), value):
                    return UNKNOWN, {}
            except UnknownState:
                return UNKNOWN, {}
            ctx["tag"][target] = b
            ctx["workspace"] = TAG.fullmatch(target).group(0).rsplit("/tags/", 1)[0]
            return APPLIED, {}
        if isinstance(ans, HttpAnswer) and 400 <= ans.status < 500 and ans.status not in (408,):
            return REFUSED, {}
        return UNKNOWN, {}

    def stage_check(self, account: str, ctx: dict, call: Call) -> str:
        ws = ctx.get("workspace")
        if ws is None:
            return UNKNOWN
        ans = call(HttpRequest("POST", f"{BASE}/{ws}:quick_preview", None))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if isinstance(ans, HttpAnswer) and ans.status == 200 and isinstance(b, dict) and b.get("compilerError") is False:
            return APPLIED
        if isinstance(b, dict) and b.get("compilerError") is True:
            return REFUSED
        return UNKNOWN

    def finalize(self, account: str, ctx: dict, call: Call) -> str:
        ws = ctx.get("workspace")
        ans = call(HttpRequest("POST", f"{BASE}/{ws}:create_version", {"name": f"zbm-clientfix {ctx.get('item_id', '')}"[:100],
                                                                      "notes": "Client-approved fix (ZBM client fix lane)."}))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        cv = b.get("containerVersion") if isinstance(b, dict) else None
        if not (isinstance(ans, HttpAnswer) and ans.status == 200 and isinstance(b, dict) and b.get("compilerError") is False
                and isinstance(cv, dict) and isinstance(cv.get("path"), str) and VERSION_PATH.fullmatch(cv["path"])
                and isinstance(cv.get("fingerprint"), str)):
            ctx["finalize_maybe"] = True
            return UNKNOWN if not (isinstance(ans, HttpAnswer) and 400 <= ans.status < 500) else REFUSED
        ctx["version_created"] = cv["path"]
        ans = call(HttpRequest("POST", f"{BASE}/{cv['path']}:publish?fingerprint={cv['fingerprint']}", None))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        pv = b.get("containerVersion") if isinstance(b, dict) else None
        if isinstance(ans, HttpAnswer) and ans.status == 200 and isinstance(b, dict) and b.get("compilerError") is False \
                and isinstance(pv, dict) and pv.get("path") == cv["path"]:
            ctx["published"] = True
            ctx["published_path"] = cv["path"]
            return APPLIED
        ctx["publish_maybe"] = True
        return UNKNOWN if not (isinstance(ans, HttpAnswer) and 400 <= ans.status < 500) else REFUSED

    def rollback(self, account: str, written: list, ctx: dict, call: Call) -> str:
        if ctx.get("published") or ctx.get("publish_maybe"):
            prev = ctx.get("live_before")
            if not prev:
                return UNKNOWN
            try:
                live = self._live(account, call)
            except UnknownState:
                return UNKNOWN
            if live["path"] == prev:
                ctx["restored_live"] = prev
                return APPLIED
            ans = call(HttpRequest("POST", f"{BASE}/{prev}:publish", None))
            b = ans.body if isinstance(ans, HttpAnswer) else None
            pv = b.get("containerVersion") if isinstance(b, dict) else None
            if isinstance(ans, HttpAnswer) and ans.status == 200 and isinstance(pv, dict) and pv.get("path") == prev:
                ctx["restored_live"] = prev
                ctx["published"] = False
                return APPLIED
            return UNKNOWN
        if ctx.get("version_created") or ctx.get("finalize_maybe"):
            # a version may have been created (the workspace deleted) but nothing published: the live state is the
            # snapshot's; prove it by the live version path
            try:
                live = self._live(account, call)
            except UnknownState:
                return UNKNOWN
            ctx["restored_live"] = live["path"] if live["path"] == ctx.get("live_before") else None
            return APPLIED if ctx["restored_live"] else UNKNOWN
        return super().rollback(account, written, ctx, call)

    def rollback_keys(self, keys: list, ctx: dict) -> list:
        # after a version was created the workspace no longer exists: the live version path is the proof (executor)
        return [] if ctx.get("restored_live") else keys
