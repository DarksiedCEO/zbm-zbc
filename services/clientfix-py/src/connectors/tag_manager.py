"""
Google Tag Manager — Tag Manager API v2. VERIFIED (docs fetched Oct 6 2026). Base https://tagmanager.googleapis.com/
tagmanager/v2/; OAuth as for GA4 (https://developers.google.com/identity/protocols/oauth2/web-server). All pages under
https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/ :

  versions.live           GET  {base}{accounts/*/containers/*}/versions:live -> ContainerVersion {path, containerVersionId,
                          fingerprint, tag[], trigger[], variable[], ...} (edit.containers | readonly)
                          — accounts.containers.versions/live ; ContainerVersion fields — accounts.containers.versions
  version_headers.latest  GET  {base}{accounts/*/containers/*}/version_headers:latest -> ContainerVersionHeader {path,
                          containerVersionId, name, deleted, num*} — accounts.containers.version_headers/latest ;
                          ContainerVersionHeader
  workspaces.create       POST {base}{accounts/*/containers/*}/workspaces, body Workspace {name, description} -> the
                          Workspace {path, workspaceId, fingerprint, ...} (edit.containers) — accounts.containers.workspaces/create
  tags.get / tags.update  GET / PUT {base}{workspace path}/tags/{tag}[?fingerprint=] ("When provided, this fingerprint
                          must match the fingerprint of the tag in storage") — accounts.containers.workspaces.tags/get, /update
  workspaces.getStatus    GET  {base}{workspace path}/status -> {workspaceChange[] (Entity: one of tag / trigger / variable /
                          folder / client / transformation / zone / customTemplate / builtInVariable / gtagConfig, plus
                          changeStatus none | added | deleted | updated), mergeConflict[]} — accounts.containers.workspaces/getStatus ; Entity
  quick_preview           POST {base}{workspace path}:quick_preview -> {containerVersion, syncStatus, compilerError}
                          (edit.containerversions) — accounts.containers.workspaces/quick_preview
  create_version          POST {base}{workspace path}:create_version, body {name, notes} -> {containerVersion, syncStatus
                          {mergeConflict, syncError}, compilerError, newWorkspacePath}; it "deletes the workspace"
                          (edit.containerversions) — accounts.containers.workspaces/create_version
  versions.publish        POST {base}{accounts/*/containers/*/versions/*}:publish[?fingerprint=] -> {containerVersion,
                          compilerError} (tagmanager.publish) — accounts.containers.versions/publish
  workspaces.delete       DELETE {base}{workspace path} -> {} (tagmanager.delete.containers) — accounts.containers.workspaces/delete

Proto3 JSON (https://protobuf.dev/programming-guides/json/): a field at its default "should" be omitted and a parser
leaves a missing field unset — so a missing ``compilerError`` / ``syncError`` / ``paused`` / ``deleted`` is false and a
missing list is empty (AEGIS round 1 M6).

How a run works (AEGIS round 1 H3: round 0 edited the client's default workspace and its ``create_version`` shipped any
unpublished edit in it):
  1. snapshot  the LIVE version (whole) and the latest version header; tag values are read from the live version;
  2. prepare   refuse unless the latest version IS the live one (a workspace is based on the latest version — the docs
               do not say which; a new workspace from an unpublished version would carry it); create a DEDICATED run
               workspace; its copy of every planned tag must equal the live snapshot;
  3. write     the planned tag fields in the run workspace, fingerprint compare-and-swap;
  4. stage     ``getStatus`` must list ONLY the planned tags as ``updated`` and no merge conflict; the latest version
               must still be the snapshot; ``quick_preview`` must compile;
  5. finalize  ``create_version`` (no sync error, no merge conflict, compiles) then ``publish`` with its fingerprint;
  6. verify    the live version is ours, every planned field reads back, and EVERY other entity of the container
               equals the snapshot's (nothing unplanned went live);
  7. rollback  after publishing: re-publish the snapshot version; before it: delete the run workspace (nothing live
               changed) and prove the live version is still the snapshot's; cleanup deletes a run workspace left behind.
The lease is the whole CONTAINER (one run per container at a time). Allowlist: a tag's ``paused`` flag and its
``firingTriggerId`` list; never a tag's code, type or parameters. Target shape: ``accounts/A/containers/C/tags/T``.
"""

from __future__ import annotations

import re

from connectors.base import (APPLIED, REFUSED, UNKNOWN, Call, Connector, HttpAnswer, HttpRequest, OpSpec,
                             UnknownState, boolean, canonical, same)

BASE = "https://tagmanager.googleapis.com/tagmanager/v2"
CONTAINER = re.compile(r"accounts/[0-9]{1,20}/containers/[0-9]{1,20}")
TAG = re.compile(r"(accounts/[0-9]{1,20}/containers/[0-9]{1,20})/tags/([0-9]{1,20})")
VERSION_PATH = re.compile(r"accounts/[0-9]{1,20}/containers/[0-9]{1,20}/versions/[0-9]{1,20}")
WORKSPACE = re.compile(r"accounts/[0-9]{1,20}/containers/[0-9]{1,20}/workspaces/[0-9]{1,20}")
ENTITY_LISTS = {"tag": "tagId", "trigger": "triggerId", "variable": "variableId", "folder": "folderId",
                "builtInVariable": "type", "zone": "zoneId", "customTemplate": "templateId", "client": "clientId",
                "gtagConfig": "gtagConfigId", "transformation": "transformationId"}
VOLATILE = ("fingerprint", "path", "workspaceId", "tagManagerUrl", "containerVersionId")


def _triggers(v) -> bool:
    return (isinstance(v, list) and len(v) <= 50 and all(isinstance(x, str) and re.fullmatch(r"[0-9]{1,20}", x)
                                                          for x in v) and v == sorted(set(v)))


def _in_container(account: str, target: str) -> bool:
    m = TAG.fullmatch(target)
    return bool(m) and m.group(1) == account


OPS = {"gtm.tag.update": OpSpec("gtm.tag.update", TAG, {"paused": boolean, "firingTriggerId": _triggers},
                                target_check=_in_container)}


def _value(tag: dict, fld: str):
    """Proto3 defaults: a missing ``paused`` is false, a missing ``firingTriggerId`` is empty."""
    if fld == "paused":
        v = tag.get("paused", False)
        if not isinstance(v, bool):
            raise UnknownState("paused")
        return v
    ids = tag.get("firingTriggerId", [])
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        raise UnknownState("firingTriggerId")
    return sorted(set(ids))


def _flag(body: dict, name: str):
    """A proto3 boolean: missing = false; anything but a bool = None (unknown)."""
    v = body.get(name, False)
    return v if isinstance(v, bool) else None


def _ok(ans) -> bool:
    return isinstance(ans, HttpAnswer) and ans.status == 200 and isinstance(ans.body, dict)


def _refused(ans) -> bool:
    return isinstance(ans, HttpAnswer) and 400 <= ans.status < 500 and ans.status != 408


class TagManagerConnector(Connector):
    def __init__(self):
        super().__init__(name="gtm", status="verified", account_ref=CONTAINER, ops=OPS, max_dry_run="quick_preview",
                         scopes=("https://www.googleapis.com/auth/tagmanager.edit.containers",
                                 "https://www.googleapis.com/auth/tagmanager.edit.containerversions",
                                 "https://www.googleapis.com/auth/tagmanager.publish",
                                 "https://www.googleapis.com/auth/tagmanager.delete.containers"),
                         docs=("https://developers.google.com/tag-platform/tag-manager/api/reference/rest",
                               "https://protobuf.dev/programming-guides/json/"))

    def lease_key(self, account: str, target: str) -> str:
        return f"gtm|{account}|container"                 # H3: one run per container, never per tag

    # ---------------------------------------------------------------- reads

    def _live(self, account: str, call: Call) -> dict:
        ans = call(HttpRequest("GET", f"{BASE}/{account}/versions:live", None))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if not _ok(ans) or not isinstance(b.get("path"), str) or not VERSION_PATH.fullmatch(b["path"]) \
                or not isinstance(b.get("containerVersionId"), str) or not isinstance(b.get("tag", []), list):
            raise UnknownState("versions.live answer")
        return b

    def _latest_id(self, account: str, call: Call) -> str:
        ans = call(HttpRequest("GET", f"{BASE}/{account}/version_headers:latest", None))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if not _ok(ans) or not isinstance(b.get("containerVersionId"), str) or _flag(b, "deleted") is not False:
            raise UnknownState("version_headers.latest answer")
        return b["containerVersionId"]

    @staticmethod
    def _tags_by_id(version: dict) -> dict:
        return {t.get("tagId"): t for t in version.get("tag", []) if isinstance(t, dict)}

    def read(self, account: str, keys: list, ctx: dict, call: Call) -> dict:
        live = self._live(account, call)
        if ctx.get("published"):
            if live["path"] != ctx.get("published_path"):
                raise UnknownState("the live version is not the one published")
            unexpected = self._unplanned_differences(ctx["live_before_version"], live, ctx.get("planned", {}))
            if unexpected:
                raise UnknownState(f"the published version differs beyond the plan: {unexpected[:3]}")
        elif "live_before" not in ctx:
            ctx["live_before"] = live["path"]
            ctx["live_before_id"] = live["containerVersionId"]
            ctx["live_before_fingerprint"] = live.get("fingerprint")
            ctx["live_before_version"] = live
            ctx["latest_id"] = self._latest_id(account, call)
        tags = self._tags_by_id(live)
        out = {}
        for target, fld in keys:
            tag = tags.get(TAG.fullmatch(target).group(2))
            if tag is None:
                raise UnknownState("tag not in the live version")
            out[(target, fld)] = _value(tag, fld)
        return out

    def _unplanned_differences(self, before: dict, after: dict, planned: dict) -> list:
        """Every entity of the container must be unchanged except the planned tag fields."""
        diffs = []
        for name, idf in ENTITY_LISTS.items():
            def norm(version):
                out = {}
                for e in version.get(name, []) or []:
                    if not isinstance(e, dict):
                        return None
                    e = {k: v for k, v in e.items() if k not in VOLATILE}
                    if name == "tag":
                        for f in planned.get(e.get("tagId"), ()):
                            e.pop(f, None)
                        e.setdefault("paused", False)
                        e.setdefault("firingTriggerId", [])
                    out[canonical(e.get(idf))] = canonical(e)
                return out
            a, b = norm(before), norm(after)
            if a is None or b is None or a != b:
                diffs.append(name)
        return diffs

    # ---------------------------------------------------------------- the run

    def prepare(self, account: str, ops: list, ctx: dict, call: Call) -> tuple[str, str]:
        planned: dict = {}
        for op in ops:
            planned.setdefault(TAG.fullmatch(op["target"]).group(2), set()).add(op["field"])
        ctx["planned"] = planned
        if ctx.get("latest_id") != ctx.get("live_before_id"):
            return REFUSED, "base_not_live"               # an unpublished version would ride along with ours
        ans = call(HttpRequest("POST", f"{BASE}/{account}/workspaces",
                               {"name": f"zbm-clientfix {ctx.get('item_id', '')}"[:100],
                                "description": "Client-approved fix (ZBM client fix lane); deleted after the run."}))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if not (_ok(ans) and isinstance(b.get("path"), str) and WORKSPACE.fullmatch(b["path"])
                and b["path"].startswith(account + "/workspaces/")):
            return (REFUSED if _refused(ans) else UNKNOWN), "run_workspace"
        ctx["workspace"] = b["path"]
        live_tags = self._tags_by_id(ctx["live_before_version"])
        raw = ctx.setdefault("tag", {})
        for tag_id in sorted(planned):
            path = f"{ctx['workspace']}/tags/{tag_id}"
            got = call(HttpRequest("GET", f"{BASE}/{path}", None))
            t = got.body if isinstance(got, HttpAnswer) else None
            if not _ok(got) or t.get("path") != path or not isinstance(t.get("fingerprint"), str):
                return UNKNOWN, "run_workspace"
            for f in planned[tag_id]:
                try:
                    if not same(_value(t, f), _value(live_tags[tag_id], f)):
                        return REFUSED, "run_workspace_not_live"     # the workspace base is not the live version
                except (UnknownState, KeyError):
                    return UNKNOWN, "run_workspace"
            raw[tag_id] = t
        return APPLIED, "run_workspace"

    def write(self, account: str, key, value, ctx: dict, call: Call) -> tuple[str, dict]:
        target, fld = key
        tag_id = TAG.fullmatch(target).group(2)
        tag = ctx.get("tag", {}).get(tag_id)
        if tag is None or ctx.get("workspace_consumed"):
            return UNKNOWN, {}
        body = dict(tag)
        body[fld] = value
        ans = call(HttpRequest("PUT", f"{BASE}/{tag['path']}?fingerprint={tag['fingerprint']}", body))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if _ok(ans) and b.get("path") == tag["path"] and isinstance(b.get("fingerprint"), str):
            try:
                if not same(_value(b, fld), value):
                    return UNKNOWN, {}
            except UnknownState:
                return UNKNOWN, {}
            ctx["tag"][tag_id] = b
            return APPLIED, {}
        return (REFUSED if _refused(ans) else UNKNOWN), {}

    def stage_check(self, account: str, ctx: dict, call: Call) -> str:
        ws = ctx.get("workspace")
        if ws is None:
            return UNKNOWN
        ans = call(HttpRequest("GET", f"{BASE}/{ws}/status", None))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if not _ok(ans):
            return UNKNOWN
        changes, conflicts = b.get("workspaceChange", []), b.get("mergeConflict", [])
        if not isinstance(changes, list) or not isinstance(conflicts, list):
            return UNKNOWN
        if conflicts:
            return REFUSED
        planned = ctx.get("planned", {})
        for ch in changes:
            entity = {k: v for k, v in ch.items() if k != "changeStatus"} if isinstance(ch, dict) else {}
            tag = entity.get("tag")
            if set(entity) != {"tag"} or not isinstance(tag, dict) or tag.get("tagId") not in planned \
                    or ch.get("changeStatus") != "updated":
                ctx["unplanned_change"] = True
                return REFUSED                            # H3: only the plan's own changes may be versioned
        try:
            if self._latest_id(account, call) != ctx.get("live_before_id"):
                return REFUSED
        except UnknownState:
            return UNKNOWN
        ans = call(HttpRequest("POST", f"{BASE}/{ws}:quick_preview", None))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if not _ok(ans):
            return REFUSED if _refused(ans) else UNKNOWN
        err = _flag(b, "compilerError")
        return APPLIED if err is False else REFUSED if err is True else UNKNOWN

    def finalize(self, account: str, ctx: dict, call: Call) -> str:
        ws = ctx.get("workspace")
        ans = call(HttpRequest("POST", f"{BASE}/{ws}:create_version",
                               {"name": f"zbm-clientfix {ctx.get('item_id', '')}"[:100],
                                "notes": "Client-approved fix (ZBM client fix lane)."}))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        ctx["workspace_consumed"] = True                  # create_version "deletes the workspace" (or may have)
        if not _ok(ans):
            ctx["finalize_maybe"] = True
            return REFUSED if _refused(ans) else UNKNOWN
        sync = b.get("syncStatus", {})
        cv = b.get("containerVersion")
        if not isinstance(sync, dict) or _flag(sync, "syncError") is not False or sync.get("mergeConflict", []) \
                or _flag(b, "compilerError") is not False or not isinstance(cv, dict) \
                or not isinstance(cv.get("path"), str) or not VERSION_PATH.fullmatch(cv["path"]):
            ctx["finalize_maybe"] = True
            return UNKNOWN
        ctx["version_created"] = cv["path"]
        fp = f"?fingerprint={cv['fingerprint']}" if isinstance(cv.get("fingerprint"), str) else ""
        ans = call(HttpRequest("POST", f"{BASE}/{cv['path']}:publish{fp}", None))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        pv = b.get("containerVersion") if isinstance(b, dict) else None
        if _ok(ans) and _flag(b, "compilerError") is False and isinstance(pv, dict) and pv.get("path") == cv["path"]:
            ctx["published"] = True
            ctx["published_path"] = cv["path"]
            return APPLIED
        ctx["publish_maybe"] = True
        return REFUSED if _refused(ans) else UNKNOWN

    def rollback(self, account: str, written: list, ctx: dict, call: Call) -> str:
        prev = ctx.get("live_before")
        if not prev:
            return UNKNOWN
        if ctx.get("published") or ctx.get("publish_maybe"):
            try:
                live = self._live(account, call)
            except UnknownState:
                return UNKNOWN
            if live["path"] != prev:
                ans = call(HttpRequest("POST", f"{BASE}/{prev}:publish", None))
                b = ans.body if isinstance(ans, HttpAnswer) else None
                pv = b.get("containerVersion") if isinstance(b, dict) else None
                if not (_ok(ans) and isinstance(pv, dict) and pv.get("path") == prev):
                    return UNKNOWN
                ctx["published"] = False
        else:
            self._delete_workspace(ctx, call)             # nothing went live: dropping the run workspace undoes it
        try:
            live = self._live(account, call)
        except UnknownState:
            return UNKNOWN
        ctx["restored_live"] = live["path"] if live["path"] == prev else None
        return APPLIED if ctx["restored_live"] else UNKNOWN

    def rollback_keys(self, keys: list, ctx: dict) -> list:
        return [] if ctx.get("restored_live") else keys     # proof: the live version path is the snapshot's

    def _delete_workspace(self, ctx: dict, call: Call):
        ws = ctx.get("workspace")
        if ws is None or ctx.get("workspace_consumed") or ctx.get("workspace_deleted"):
            return None
        ans = call(HttpRequest("DELETE", f"{BASE}/{ws}", None))
        if isinstance(ans, HttpAnswer) and ans.status == 200 and ans.body in ({}, None):
            ctx["workspace_deleted"] = True
            return APPLIED
        return REFUSED if _refused(ans) else UNKNOWN

    def cleanup(self, account: str, ctx: dict, call: Call):
        return self._delete_workspace(ctx, call)
