"""
The connector contract (ADR 0017 decisions 11-13). A connector is DETERMINISTIC code that knows one platform's
officially documented API: it builds exact HTTP requests and parses documented answers. It performs no I/O of its own:
every request goes through the ``call`` the executor hands it, which records every request that may change the
platform (``HttpRequest.is_write``) on the ledger BEFORE it leaves (record first) and refuses everything once the
connection is revoked.

The change model is uniform. A change set is a list of typed operations; each names an operation type from the
connector's allowlist, a ``target`` (a platform resource) and a ``field`` of it, and carries the exact ``before`` and
``after`` values. ``None`` means "absent" (a ``before`` of None is a create, an ``after`` of None a removal, each only
where the operation type allows it). The state of a client resource is therefore a map ``(target, field) -> value``,
read through the platform's read API before (snapshot) and after (verify) — the executor compares, never the agent.

Outcomes of a write are three and only three:
  APPLIED  — the documented success shape, exactly;
  REFUSED  — the platform said no in a documented way (GraphQL ``userErrors``, an HTTP 4xx): certainly not applied;
  UNKNOWN  — anything else (a timeout, an exception, a 5xx, an unparseable or undocumented body). Never success: an
             unknown write is treated as possibly applied and rolled back.
A read that is not the documented shape raises ``UnknownState``: no decision is ever taken on a guess.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

APPLIED, REFUSED, UNKNOWN = "applied", "refused", "unknown"

Key = tuple            # (target, field)


class UnknownState(Exception):
    """A read answered with something other than the documented shape: the state is unknown."""


class OpRefused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class HttpRequest:
    """One HTTP request to a platform. ``auth`` names the credential the TRANSPORT attaches from the vault (the
    connection's token reference, or the app's own read key); a connector never sees a token."""
    method: str
    url: str
    body: Optional[dict] = None
    auth: str = "connection"
    mutates: Optional[bool] = None    # None: every method but GET mutates (a GraphQL query is a POST that does not)

    @property
    def is_write(self) -> bool:
        return self.method != "GET" if self.mutates is None else self.mutates

    def describe(self) -> dict:
        """What the ledger evidence carries: method, URL and the SHA-256 of the canonical body (never the body)."""
        b = None if self.body is None else hashlib.sha256(
            json.dumps(self.body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        return {"method": self.method, "url_sha256": hashlib.sha256(self.url.encode()).hexdigest(), "body_sha256": b}


@dataclass(frozen=True)
class HttpAnswer:
    status: int
    body: Any = None


Call = Callable[[HttpRequest], HttpAnswer]


@dataclass(frozen=True)
class OpSpec:
    """One allowlisted operation type."""
    name: str
    target: re.Pattern
    fields: dict                                   # exact field name -> value validator
    field_patterns: tuple = ()                     # ((regex, validator), ...) for keyed fields (metafields, key events)
    create: bool = False                           # before may be None
    remove: bool = False                           # after may be None
    target_check: Optional[Callable[[str, str], bool]] = None   # (account_ref, target) -> belongs to that account

    def validator(self, fld: str):
        if fld in self.fields:
            return self.fields[fld]
        for rx, v in self.field_patterns:
            if rx.fullmatch(fld):
                return v
        return None


def canonical(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def same(a: Any, b: Any) -> bool:
    """Exact equality of two JSON values (canonical form; no None == None shortcut on ids — values only)."""
    return canonical(a) == canonical(b)


# ------------------------------------------------------------------------------------------- value validators

_UNSAFE_HTML = re.compile(r"<\s*(script|iframe|object|embed|form|base|meta|link|style)\b|javascript\s*:|vbscript\s*:"
                          r"|data\s*:\s*text/html|\son[a-z]{2,20}\s*=|srcdoc\s*=", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")


def text(max_len: int, min_len: int = 1) -> Callable[[Any], bool]:
    def ok(v: Any) -> bool:
        return isinstance(v, str) and min_len <= len(v) <= max_len and not _CONTROL.search(v) and "\n" not in v
    return ok


def html(max_len: int) -> Callable[[Any], bool]:
    """Rich text an agent may write into a client's store: no script, frame, form, style, event handler or script
    URL (ADR 0017 decision 12; the client approves the exact text as well)."""
    def ok(v: Any) -> bool:
        return isinstance(v, str) and len(v) <= max_len and not _CONTROL.search(v) and not _UNSAFE_HTML.search(v)
    return ok


def boolean(v: Any) -> bool:
    return isinstance(v, bool)


def one_of(*values: str) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, str) and v in values


@dataclass
class Connector:
    """A platform connector. Subclasses override the I/O-shaped methods; this base refuses everything."""
    name: str = "base"
    status: str = "not_built"         # verified | verified_gated | guided_manual | not_built
    docs: tuple = ()
    account_ref: re.Pattern = re.compile(r"(?!)")
    scopes: tuple = ()
    ops: dict = field(default_factory=dict)
    manual: bool = False
    max_dry_run: str = "offline"      # what "dry run" means on this platform (README / ADR table)

    # ---------------------------------------------------------------- validation (pure)

    def validate(self, account: str, op: dict) -> OpSpec:
        """Refuse anything outside the allowlist, with a reason code. Pure: no I/O."""
        spec = self.ops.get(op.get("op"))
        if spec is None:
            raise OpRefused("OP_NOT_ALLOWED" if self.status != "not_built" else "CONNECTOR_NOT_BUILT")
        target, fld = op.get("target"), op.get("field")
        if not isinstance(target, str) or not spec.target.fullmatch(target):
            raise OpRefused("OP_TARGET_INVALID")
        if spec.target_check is not None and not spec.target_check(account, target):
            raise OpRefused("TENANT_MISMATCH")
        check = spec.validator(fld) if isinstance(fld, str) else None
        if check is None:
            raise OpRefused("OP_FIELD_NOT_ALLOWED")
        before, after = op.get("before"), op.get("after")
        if before is None and not spec.create:
            raise OpRefused("OP_VALUE_INVALID")
        if after is None and not spec.remove:
            raise OpRefused("OP_VALUE_INVALID")
        for v in (before, after):
            if v is not None and not check(v):
                raise OpRefused("OP_VALUE_INVALID")
        if same(before, after):
            raise OpRefused("OP_NO_CHANGE")
        self.extra_checks(op)
        return spec

    def extra_checks(self, op: dict) -> None:
        return None

    # ---------------------------------------------------------------- I/O through ``call``

    def read(self, account: str, keys: list, ctx: dict, call: Call) -> dict:
        raise UnknownState("connector not built")

    def dry_run(self, account: str, ops: list, ctx: dict, call: Call) -> tuple[str, str]:
        """``(APPLIED, mode)`` = the change would be accepted; REFUSED / UNKNOWN otherwise. Shopify and GA4 offer no
        server-side dry run: the offline validation above is all there is ("offline")."""
        return APPLIED, "offline"

    def write(self, account: str, key: Key, value: Any, ctx: dict, call: Call) -> tuple[str, dict]:
        return UNKNOWN, {}

    def stage_check(self, account: str, ctx: dict, call: Call) -> str:
        """After every write, before anything goes live (GTM quick_preview). APPLIED = fine."""
        return APPLIED

    def finalize(self, account: str, ctx: dict, call: Call) -> str:
        """Make staged writes live (GTM: create a version and publish it). Most platforms write live directly."""
        return APPLIED

    def rollback(self, account: str, written: list, ctx: dict, call: Call) -> str:
        """Undo, in reverse order, every write that may have happened (``written`` = [(key, before)], an UNKNOWN write
        included). The current state is RE-READ first (it refreshes ids and compare digests a lost answer never
        delivered), and only a key that differs from its snapshot is written back; every write is attempted even
        after one fails, and the worst outcome is returned. A connector whose finalize is reversible as a whole
        (GTM: re-publish the previous live version) overrides this."""
        keys = [k for k, _ in written]
        try:
            current = self.read(account, keys, ctx, call)
        except UnknownState:
            return UNKNOWN
        ctx.setdefault("state", {}).update(current)
        worst = APPLIED
        for key, before in reversed(written):
            if same(current.get(key), before):
                continue
            outcome, _ = self.write(account, key, before, ctx, call)
            if outcome == APPLIED:
                ctx["state"][key] = before
            elif outcome == UNKNOWN or worst == UNKNOWN:
                worst = UNKNOWN
            else:
                worst = REFUSED
        return worst

    def rollback_keys(self, keys: list, ctx: dict) -> list:
        """The keys whose read-back proves the rollback (default: the written keys themselves)."""
        return keys

    def instructions(self, account: str, ops: list) -> list:
        """Guided manual connectors only: exact steps for the client or Andre."""
        return []
