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
  create_version          POST {base}{workspace path}:create_version, body {name, notes} -> {containerVersion ("The
                          container version created."), syncStatus ("Whether version creation failed when syncing the
                          workspace to the LATEST container version"), compilerError, newWorkspacePath}; it "deletes the
                          workspace, and sets the base container version to the newly created version"
                          (edit.containerversions) — accounts.containers.workspaces/create_version
  versions.get            GET  {base}{accounts/*/containers/*/versions/*} -> ContainerVersion (incl. ``name``)
                          — accounts.containers.versions/get
  workspaces.list         GET  {base}{accounts/*/containers/*}/workspaces[?pageToken=] -> {workspace[], nextPageToken}
                          — accounts.containers.workspaces/list
  versions.publish        POST {base}{accounts/*/containers/*/versions/*}:publish[?fingerprint=] -> {containerVersion,
                          compilerError} (tagmanager.publish) — accounts.containers.versions/publish. The fingerprint
                          is the PUBLISHED version's own ("must match the fingerprint of the container version in
                          storage"); there is NO precondition on the version currently live (AEGIS round 3 L4: no
                          compare-and-swap of the live version exists — a race is detected afterwards and frozen).
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
  5. finalize  ``create_version`` (no sync error, no merge conflict, compiles); because it syncs the workspace to the
               LATEST version, the returned containerVersion must be EXACTLY the snapshot plus the planned values (a
               revert: exactly the snapshot) and carry our exact name, else it is NOT published (AEGIS round 3 M1:
               ``poisoned_version`` names it); then ``publish`` with its fingerprint;
  6. verify    the live version is ours, every planned field reads back, and EVERY other entity of the container
               equals the snapshot's (nothing unplanned went live);
  7. rollback  after publishing: re-publish the snapshot version; before it: delete the run workspace (nothing live
               changed) and prove the live version is still the snapshot's; cleanup deletes a run workspace left behind.
               A version is taken for ours only by its exact name AND exact content (round 3 M2); a revert is never
               built on a version that carries anything unplanned (round 3 M1).
  8. reap      the recover tick deletes only run workspaces whose generated name the ledger recorded BEFORE the create
               request, once that run settled, and only when ``getStatus`` shows no change at all (round 3 M3).
The lease is the whole CONTAINER (one run per container at a time). Allowlist: a tag's ``paused`` flag and its
``firingTriggerId`` list; never a tag's code, type or parameters. Target shape: ``accounts/A/containers/C/tags/T``.
"""

from __future__ import annotations

import re

import secrets
from typing import Optional
from urllib.parse import quote

from connectors.base import (APPLIED, CONFLICT, REFUSED, UNKNOWN, Call, Connector, HttpAnswer, HttpRequest, OpSpec,
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
RUN_PREFIX = "zbm-clientfix-run-"        # every workspace a run creates; the ONLY workspaces this code deletes
MAX_PAGES = 20


class NotOurs(Exception):
    """The container's latest version is not exactly the one this run created (AEGIS round 3 M2)."""


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
                        e["firingTriggerId"] = sorted(set(e.get("firingTriggerId", [])))
                    out[canonical(e.get(idf))] = canonical(e)
                return out
            a, b = norm(before), norm(after)
            if a is None or b is None or a != b:
                diffs.append(name)
        return diffs

    # ---------------------------------------------------------------- the run

    # ---------------------------------------------------------------- run workspaces (named, found, deleted)

    def _new_workspace(self, account: str, ctx: dict, call: Call, role: str) -> tuple[str, Optional[str]]:
        """Create a run workspace with a UNIQUE name (AEGIS round 2 R2-5). When the answer is lost the workspace may
        exist: the container's workspaces are listed and ours is found by its exact name, so cleanup deletes it."""
        name = f"{RUN_PREFIX}{role}-{str(ctx.get('item_id', ''))[-12:]}-{secrets.token_hex(6)}"
        ctx.setdefault("ws_names", {})[role] = name
        # M3: the generated name goes on the ledger WITH the create request, before it is sent (``note``)
        ans = call(HttpRequest("POST", f"{BASE}/{account}/workspaces",
                               {"name": name, "description": "Client-approved fix (ZBM client fix lane); deleted after "
                                                             "the run."},
                               note={"gtm_run_workspace": name, "account": account}))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if _ok(ans) and isinstance(b.get("path"), str) and WORKSPACE.fullmatch(b["path"]) \
                and b["path"].startswith(account + "/workspaces/"):
            ctx.setdefault("workspaces", {})[role] = b["path"]
            return APPLIED, b["path"]
        if _refused(ans):
            return REFUSED, None
        try:
            found = [w for w in self.list_workspaces(account, call) if w.get("name") == name]
        except UnknownState:
            found = []
        if len(found) == 1:
            ctx.setdefault("workspaces", {})[role] = found[0]["path"]   # cleanup deletes it
        return UNKNOWN, None

    def list_workspaces(self, account: str, call: Call) -> list:
        out, token = [], None
        for _ in range(MAX_PAGES):
            url = f"{BASE}/{account}/workspaces" + (f"?pageToken={quote(token, safe='')}" if token else "")
            ans = call(HttpRequest("GET", url, None))
            b = ans.body if isinstance(ans, HttpAnswer) else None
            if not _ok(ans) or not isinstance(b.get("workspace", []), list):
                raise UnknownState("workspaces.list answer")
            for w in b.get("workspace", []):
                if not isinstance(w, dict) or not isinstance(w.get("path"), str) \
                        or not WORKSPACE.fullmatch(w["path"]) or not w["path"].startswith(account + "/workspaces/"):
                    raise UnknownState("workspace shape")
                out.append(w)
            token = b.get("nextPageToken")
            if not token:
                return out
        raise UnknownState("too many workspaces")

    def _delete(self, account: str, path: str, call: Call) -> str:
        """The ONLY delete this connector sends: a workspace of this container that a run created (RUN_PREFIX)."""
        if not (WORKSPACE.fullmatch(path) and path.startswith(account + "/workspaces/")):
            raise UnknownState("refusing to delete outside this container's workspaces")
        ans = call(HttpRequest("DELETE", f"{BASE}/{path}", None))
        if isinstance(ans, HttpAnswer) and ans.status == 200 and ans.body in ({}, None):
            return APPLIED
        return REFUSED if _refused(ans) else UNKNOWN

    def workspace_changes(self, path: str, call: Call) -> Optional[bool]:
        """workspaces.getStatus: True when the workspace holds ANY change or merge conflict, False when it holds none,
        None when the answer is not the documented shape."""
        ans = call(HttpRequest("GET", f"{BASE}/{path}/status", None))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if not _ok(ans):
            return None
        changes, conflicts = b.get("workspaceChange", []), b.get("mergeConflict", [])
        if not isinstance(changes, list) or not isinstance(conflicts, list):
            return None
        return bool(changes or conflicts)

    def reap(self, account: str, owned: dict, call: Call, may_delete) -> dict:
        """The recover tick's reaper (AEGIS round 2 R2-5, round 3 M3). ``owned`` maps the exact generated names the
        ledger recorded BEFORE their create request, of runs that have SETTLED, to their record. A workspace is
        deleted only when its name is one of them, ``getStatus`` shows no change at all (someone may have picked an
        orphaned run workspace up and worked in it), and ``may_delete()`` — asked under the service lock right before
        the DELETE (round 3 L1) — still allows it. A workspace with any change is HELD for Andre, never deleted.
        Returns {"deleted": [names], "held": [(name, path)]}."""
        out = {"deleted": [], "held": []}
        for w in self.list_workspaces(account, call):
            name = w.get("name")
            if not isinstance(name, str) or not name.startswith(RUN_PREFIX) or name not in owned:
                continue                                  # not a name this service recorded: never touched
            changed = self.workspace_changes(w["path"], call)
            if changed is None:
                continue
            if changed:
                out["held"].append((name, w["path"]))
                continue
            if not may_delete():
                break
            if self._delete(account, w["path"], call) == APPLIED:
                out["deleted"].append(name)
        return out

    def _load_tags(self, ws: str, planned: dict, call: Call) -> Optional[dict]:
        raw = {}
        for tag_id in sorted(planned):
            path = f"{ws}/tags/{tag_id}"
            got = call(HttpRequest("GET", f"{BASE}/{path}", None))
            t = got.body if isinstance(got, HttpAnswer) else None
            if not _ok(got) or t.get("path") != path or not isinstance(t.get("fingerprint"), str):
                return None
            raw[tag_id] = t
        return raw

    def _put(self, tag: dict, fld: str, value, call: Call) -> tuple[str, Optional[dict]]:
        body = dict(tag)
        body[fld] = value
        ans = call(HttpRequest("PUT", f"{BASE}/{tag['path']}?fingerprint={tag['fingerprint']}", body))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if _ok(ans) and b.get("path") == tag["path"] and isinstance(b.get("fingerprint"), str):
            try:
                return (APPLIED, b) if same(_value(b, fld), value) else (UNKNOWN, None)
            except UnknownState:
                return UNKNOWN, None
        return (REFUSED if _refused(ans) else UNKNOWN), None

    def _only_planned_changes(self, ws: str, planned: dict, call: Call, ctx: dict) -> str:
        ans = call(HttpRequest("GET", f"{BASE}/{ws}/status", None))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if not _ok(ans):
            return UNKNOWN
        changes, conflicts = b.get("workspaceChange", []), b.get("mergeConflict", [])
        if not isinstance(changes, list) or not isinstance(conflicts, list):
            return UNKNOWN
        if conflicts:
            return REFUSED
        for ch in changes:
            entity = {k: v for k, v in ch.items() if k != "changeStatus"} if isinstance(ch, dict) else {}
            tag = entity.get("tag")
            if set(entity) != {"tag"} or not isinstance(tag, dict) or tag.get("tagId") not in planned \
                    or ch.get("changeStatus") != "updated":
                ctx["unplanned_change"] = True
                return REFUSED                            # H3: only the plan's own changes may be versioned
        return APPLIED

    def _exact(self, version: dict, ctx: dict, want: dict) -> bool:
        """``version`` holds EXACTLY the snapshot with every planned field at ``want[(tag_id, field)]`` — nothing more,
        nothing less (AEGIS round 3 M1 / M2)."""
        planned = ctx.get("planned", {})
        try:
            if not isinstance(version, dict) or self._unplanned_differences(ctx["live_before_version"], version,
                                                                             planned):
                return False
            tags = self._tags_by_id(version)
            return all(tid in tags and same(_value(tags[tid], f), want[(tid, f)])
                       for tid, fields in planned.items() for f in fields)
        except (UnknownState, KeyError, TypeError):
            return False

    def _snapshot_values(self, ctx: dict) -> dict:
        snap = self._tags_by_id(ctx["live_before_version"])
        return {(tid, f): _value(snap[tid], f) for tid, fields in ctx.get("planned", {}).items() for f in fields}

    def _version_and_publish(self, ws: str, name: str, want: dict, ctx: dict,
                             call: Call) -> tuple[str, Optional[str], Optional[str]]:
        """create_version then publish. Returns (outcome, created version path or None, published path or None).

        AEGIS round 3 M1: create_version syncs the workspace "to the latest container version" (workspaces/
        create_version, syncStatus) — a version the client created after our last check is merged into ours. The
        returned containerVersion ("The container version created.") is therefore compared with the EXACT expected
        content — the snapshot plus ``want`` (the planned values going forward; the snapshot values for a revert) —
        and our exact name, BEFORE publishing. Any difference: nothing is published, the outcome is UNKNOWN and
        ``poisoned_version`` names the version (it is now the container's latest; Andre is tasked)."""
        name = name[:100]
        ans = call(HttpRequest("POST", f"{BASE}/{ws}:create_version", {"name": name, "notes": "ZBM client fix lane."}))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        if not _ok(ans):
            return (REFUSED if _refused(ans) else UNKNOWN), None, None
        sync = b.get("syncStatus", {})
        cv = b.get("containerVersion")
        if not isinstance(sync, dict) or _flag(sync, "syncError") is not False or sync.get("mergeConflict", []) \
                or _flag(b, "compilerError") is not False or not isinstance(cv, dict) \
                or not isinstance(cv.get("path"), str) or not VERSION_PATH.fullmatch(cv["path"]):
            created = cv.get("path") if isinstance(cv, dict) and isinstance(cv.get("path"), str) else None
            if created:
                ctx["poisoned_version"] = created       # its content is unknown: never published, never built on
            return UNKNOWN, created, None
        if cv.get("name") != name or not self._exact(cv, ctx, want):
            ctx["poisoned_version"] = cv["path"]
            ctx["version_mismatch"] = cv["path"]
            return UNKNOWN, cv["path"], None            # M1: never publish a version holding anything unplanned
        ctx.setdefault("clean_versions", []).append(cv["path"])
        fp = f"?fingerprint={cv['fingerprint']}" if isinstance(cv.get("fingerprint"), str) else ""
        ans = call(HttpRequest("POST", f"{BASE}/{cv['path']}:publish{fp}", None))
        b = ans.body if isinstance(ans, HttpAnswer) else None
        pv = b.get("containerVersion") if isinstance(b, dict) else None
        if _ok(ans) and _flag(b, "compilerError") is False and isinstance(pv, dict) and pv.get("path") == cv["path"]:
            return APPLIED, cv["path"], cv["path"]
        return (REFUSED if _refused(ans) else UNKNOWN), cv["path"], None

    # ---------------------------------------------------------------- the run

    def prepare(self, account: str, ops: list, ctx: dict, call: Call) -> tuple[str, str]:
        planned: dict = {}
        for op in ops:
            planned.setdefault(TAG.fullmatch(op["target"]).group(2), set()).add(op["field"])
        ctx["planned"] = planned
        ctx["planned_after"] = {(TAG.fullmatch(op["target"]).group(2), op["field"]): op["after"] for op in ops}
        if ctx.get("latest_id") != ctx.get("live_before_id"):
            return REFUSED, "base_not_live"               # an unpublished version would ride along with ours
        outcome, ws = self._new_workspace(account, ctx, call, "fix")
        if outcome != APPLIED:
            return outcome, "run_workspace"
        ctx["workspace"] = ws
        raw = self._load_tags(ws, planned, call)
        if raw is None:
            return UNKNOWN, "run_workspace"
        live_tags = self._tags_by_id(ctx["live_before_version"])
        for tag_id, fields in planned.items():
            for f in fields:
                try:
                    if not same(_value(raw[tag_id], f), _value(live_tags[tag_id], f)):
                        return REFUSED, "run_workspace_not_live"     # the workspace base is not the live version
                except (UnknownState, KeyError):
                    return UNKNOWN, "run_workspace"
        ctx["tag"] = raw
        return APPLIED, "run_workspace"

    def write(self, account: str, key, value, ctx: dict, call: Call) -> tuple[str, dict]:
        target, fld = key
        tag_id = TAG.fullmatch(target).group(2)
        tag = ctx.get("tag", {}).get(tag_id)
        if tag is None or ctx.get("workspace_consumed"):
            return UNKNOWN, {}
        outcome, b = self._put(tag, fld, value, call)
        if outcome == APPLIED:
            ctx["tag"][tag_id] = b
        return outcome, {}

    def stage_check(self, account: str, ctx: dict, call: Call) -> str:
        ws = ctx.get("workspace")
        if ws is None:
            return UNKNOWN
        st = self._only_planned_changes(ws, ctx.get("planned", {}), call, ctx)
        if st != APPLIED:
            return st
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
        # M2: a unique, exact name — a version is taken for ours only by this name AND its exact content
        ctx["version_name"] = f"zbm-clientfix {str(ctx.get('item_id', ''))[-40:]} {secrets.token_hex(6)}"[:100]
        ctx["workspace_consumed"] = True                  # create_version "deletes the workspace" (or may have)
        ctx["finalize_maybe"] = True
        outcome, created, published = self._version_and_publish(ctx["workspace"], ctx["version_name"],
                                                                ctx.get("planned_after", {}), ctx, call)
        if created:
            ctx["version_created"] = created
        if published:
            ctx["published"] = True
            ctx["published_path"] = published
        return outcome

    # ---------------------------------------------------------------- rollback (AEGIS round 2 R2-1 / R2-2)

    def rollback(self, account: str, written: list, ctx: dict, call: Call) -> str:
        """Before any version was attempted: delete the run workspace — nothing went anywhere — and prove the live
        version is still the snapshot's. Once a version may exist, ``create_version`` "sets the base container
        version to the newly created version" (workspaces/create_version) and the latest version header
        (version_headers/latest) would stay OURS even after the old version is re-published, so the next workspace
        (workspaces/create; workspaces/sync "syncs a workspace to the latest container version") would carry the
        rolled-back change. So: (1) re-publish the snapshot version ONLY when the live version is our own (R2-2:
        never over someone else's newer release), then (2) when the latest version is ours, build a REVERT version
        through a fresh run workspace — the planned fields back to their snapshot values, getStatus showing only
        those, create_version, publish — and (3) prove latest == live and the live content == the snapshot's.
        Anything else is CONFLICT / UNKNOWN with ``poisoned_version`` naming our version (frozen, Andre tasked)."""
        prev, prev_id = ctx.get("live_before"), ctx.get("live_before_id")
        if not prev:
            return UNKNOWN
        if not ctx.get("finalize_maybe"):
            self._delete_workspace(ctx, call)
            try:
                live = self._live(account, call)
            except UnknownState:
                return UNKNOWN
            ctx["restored_live"] = live["path"] if live["path"] == prev else None
            return APPLIED if ctx["restored_live"] else CONFLICT
        try:
            created = ctx.get("version_created") or self._identify_ours(account, ctx, call)
        except NotOurs as exc:
            ctx["foreign_version"] = exc.args[0]          # round 4 Info 2: the task names it
            return CONFLICT                               # M2: the latest version is someone else's: left alone
        except UnknownState:
            return UNKNOWN
        try:
            live = self._live(account, call)
            if created is None:                           # nothing of ours exists: the live state must be untouched
                ctx["restored_live"] = live["path"] if live["path"] == prev else None
                return APPLIED if ctx["restored_live"] else CONFLICT
            ctx["poisoned_version"] = created
            clean = created in ctx.get("clean_versions", ())
            created_id = created.rsplit("/", 1)[1]
            republished = False
            if live["path"] == created:
                # L4 (accepted risk): versions.publish has no precondition on the live version, so a release the
                # client publishes between this read and our publish is un-published; it is DETECTED just below
                ans = call(HttpRequest("POST", f"{BASE}/{prev}:publish", None))
                b = ans.body if isinstance(ans, HttpAnswer) else None
                pv = b.get("containerVersion") if isinstance(b, dict) else None
                if not (_ok(ans) and isinstance(pv, dict) and pv.get("path") == prev):
                    return UNKNOWN
                ctx["published"] = False
                republished = True
            elif live["path"] != prev:
                ctx["foreign_version"] = live["path"]
                return CONFLICT                           # someone else published after us: never un-publish them
            latest = self._latest_id(account, call)
            if latest not in (prev_id, created_id):
                if republished:                           # a newer version than ours may have been live a moment ago
                    ctx["release_may_be_unpublished"] = f"{account}/versions/{latest}"
                ctx["foreign_version"] = f"{account}/versions/{latest}"
                return CONFLICT                           # someone else's newer version sits on top of ours
            if latest == prev_id:
                ctx["poisoned_version"] = None
            elif not clean:
                return UNKNOWN                            # M1: never build a revert on a version with unplanned content
            elif self._build_revert(account, ctx, call) != APPLIED:
                return UNKNOWN
            live = self._live(account, call)
            if self._latest_id(account, call) != live["containerVersionId"] \
                    or self._unplanned_differences(ctx["live_before_version"], live, {}):
                return UNKNOWN
            ctx["restored_live"] = live["path"]
            ctx["poisoned_version"] = None
            return APPLIED
        except UnknownState:
            return UNKNOWN

    def _identify_ours(self, account: str, ctx: dict, call: Call) -> Optional[str]:
        """The create_version answer was lost: the latest version is ours only when its ``name`` is EXACTLY the
        unique name we sent AND its content is exactly the snapshot with every planned field at our ``after``
        (versions/get; AEGIS round 3 M2). Anything else is someone else's version: ``NotOurs`` (CONFLICT)."""
        latest = self._latest_id(account, call)
        if latest == ctx.get("live_before_id"):
            return None
        path = f"{account}/versions/{latest}"
        ans = call(HttpRequest("GET", f"{BASE}/{path}", None))
        v = ans.body if isinstance(ans, HttpAnswer) else None
        if not _ok(ans) or v.get("path") != path:
            raise UnknownState("versions.get answer")
        if not ctx.get("version_name") or v.get("name") != ctx["version_name"] \
                or not self._exact(v, ctx, ctx.get("planned_after", {})):
            raise NotOurs(path)
        ctx.setdefault("clean_versions", []).append(path)
        return path

    def _build_revert(self, account: str, ctx: dict, call: Call) -> str:
        planned = ctx.get("planned", {})
        outcome, ws = self._new_workspace(account, ctx, call, "revert")
        if outcome != APPLIED:
            return outcome
        ctx["revert_workspace"] = ws
        raw = self._load_tags(ws, planned, call)
        if raw is None:
            return UNKNOWN
        snap_tags = self._tags_by_id(ctx["live_before_version"])
        for tag_id, fields in sorted(planned.items()):
            for f in sorted(fields):
                want = _value(snap_tags[tag_id], f)
                if same(_value(raw[tag_id], f), want):
                    continue
                o, b = self._put(raw[tag_id], f, want, call)
                if o != APPLIED:
                    return o
                raw[tag_id] = b
        if self._only_planned_changes(ws, planned, call, ctx) != APPLIED:
            return REFUSED
        ctx["revert_consumed"] = True
        name = f"zbm-clientfix revert {str(ctx.get('item_id', ''))[-40:]} {secrets.token_hex(6)}"
        outcome, _, published = self._version_and_publish(ws, name, self._snapshot_values(ctx), ctx, call)
        return APPLIED if outcome == APPLIED and published else outcome

    def rollback_keys(self, keys: list, ctx: dict) -> list:
        return [] if ctx.get("restored_live") else keys     # proof: the live version is the snapshot's content

    def _delete_workspace(self, ctx: dict, call: Call):
        """Delete what this run created and has not consumed: the fix workspace and a revert workspace."""
        worst = None
        for role, consumed in (("fix", "workspace_consumed"), ("revert", "revert_consumed")):
            path = ctx.get("workspaces", {}).get(role)
            if path is None or ctx.get(consumed) or ctx.get(f"{role}_deleted"):
                continue
            account = path.rsplit("/workspaces/", 1)[0]
            try:
                out = self._delete(account, path, call)
            except UnknownState:
                out = UNKNOWN
            if out == APPLIED:
                ctx[f"{role}_deleted"] = True
                if role == "fix":
                    ctx["workspace_deleted"] = True
            worst = out if worst in (None, APPLIED) else worst
        return worst

    def cleanup(self, account: str, ctx: dict, call: Call):
        return self._delete_workspace(ctx, call)

    def leftovers(self, ctx: dict) -> list:
        """Run workspaces that may still exist after cleanup (round 4 Info 2): one not deleted and not consumed, or one
        handed to a create_version whose outcome is unknown (it "deletes the workspace" only if it ran)."""
        out = []
        for role, consumed, made in (("fix", "workspace_consumed", "version_created"),
                                     ("revert", "revert_consumed", None)):
            path = ctx.get("workspaces", {}).get(role)
            if path is None or ctx.get(f"{role}_deleted"):
                continue
            if ctx.get(consumed) and (made is None or ctx.get(made)):
                continue
            out.append(path)
        return out
