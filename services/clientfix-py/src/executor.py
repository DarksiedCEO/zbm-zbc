"""
The deterministic executor for ONE item of an approved fix plan (founder decision 5, ADR 0017 decision 15). Agents
never touch a client system: they proposed the change set; this code — no model, no agent — runs it:

  1. snapshot   read every (target, field) the change set names through the connector's read API;
  2. drift      every op's ``before`` must equal the snapshot exactly, else nothing is written (``drifted``);
  3. manual     a guided-manual connector (Yelp) writes nothing: it returns exact instructions (``awaiting_manual``);
  4. dry run    the platform's own preview or validation where one exists (GBP ``validateOnly``; Shopify / GA4 have
                none: offline validation), else nothing is written (``dry_run_refused``);
  5. apply      each op in order; only the documented success shape is APPLIED; REFUSED or UNKNOWN stops the item;
  6. stage      a staged platform's preview must compile (GTM ``quick_preview``); then ``finalize`` makes it live;
  7. verify     read everything back; the read must equal every op's ``after`` exactly;
  8. rollback   on ANY failure after the first write: the connector undoes every write that may have happened, in
                reverse order, from the snapshot, and the result is READ BACK against the snapshot. A rollback that
                cannot be proven is ``rollback_failed`` (the service freezes the resource and alerts Andre).

Every request that may change the platform (``is_write``) is recorded on the ledger BEFORE it is sent and its answer
after (``step``), and every
request first asks ``live()`` whether work may continue: a revoked connection (founder decision 4), a frozen resource
or client, or a closed service raises ``Halt`` and nothing more is sent — not even a rollback, since a revoked
connection may no longer be used at all (``halted_revoked``; the service tells Andre exactly what was written). A step
that cannot be recorded raises ``Halt("LEDGER_UNAVAILABLE")``: no unrecorded request ever leaves (``interrupted``).
"""

from __future__ import annotations

import hashlib
import json
from typing import Callable, Optional

from connectors.base import APPLIED, REFUSED, UNKNOWN, Connector, HttpAnswer, HttpRequest, UnknownState, same
from ports import ConnView


class Halt(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def values_sha(values: dict) -> str:
    rows = sorted([k[0], k[1], v] for k, v in values.items())
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode()).hexdigest()


def rows(values: dict) -> list:
    return sorted([k[0], k[1], v] for k, v in values.items())


Step = Callable[[str, dict], None]


def run_item(connector: Connector, conn: ConnView, item_id: str, ops: list, transport, step: Step,
             live: Callable[[], Optional[str]], live_rollback: Optional[Callable[[], Optional[str]]] = None) -> dict:
    """``live`` is asked before every request; ``live_rollback`` (revocation and shutdown only, never a freeze) is
    used for the guarded rollback after Andre froze a resource in the middle of a write (AEGIS round 1 Low)."""
    keys = [(op["target"], op["field"]) for op in ops]
    extra = [k for k in connector.companions(keys) if k not in keys]
    read_keys = keys + extra
    ctx: dict = {"item_id": item_id, "state": {}}
    out: dict = {"status": None, "failure": None, "written": [], "snapshot": None, "readback": None,
                 "rollback": None, "dry_run": None, "instructions": None, "sent_writes": 0}
    sent = {"n": 0}

    def make_call(gate):
        def call(req: HttpRequest) -> HttpAnswer:
            code = gate()
            if code:
                raise Halt(code)
            write = req.is_write
            if write:
                sent["n"] += 1
                facts = {"n": sent["n"], "request": req.describe()}
                if req.note:
                    facts["note"] = dict(req.note)      # e.g. a GTM run workspace's generated name (round 3 M3)
                step("request_sending", facts)
                out["sent_writes"] = sent["n"]
            try:
                ans = transport.call(conn, req)
            except Halt:
                raise
            except Exception:          # noqa: BLE001 — any transport failure is an UNKNOWN answer, never success
                ans = None
            if not isinstance(ans, HttpAnswer) or isinstance(ans.status, bool) or not isinstance(ans.status, int):
                ans = HttpAnswer(0, None)
            if write:
                body_sha = hashlib.sha256(json.dumps(ans.body, sort_keys=True, default=str).encode()).hexdigest()
                step("request_answered", {"n": sent["n"], "status": ans.status, "body_sha256": body_sha})
            return ans
        return call

    call = make_call(live)
    snap: dict = {}
    try:
        try:
            # 1-2 snapshot (the planned keys AND their companions) and drift
            try:
                snap = connector.read(conn.account_ref, read_keys, ctx, call)
            except UnknownState:
                step("snapshot_unknown", {})
                out["status"] = "snapshot_unknown"
                return out
            ctx["state"] = dict(snap)
            ctx["snapshot"] = dict(snap)
            out["snapshot"] = rows(snap)
            step("snapshot_taken", {"snapshot_sha256": values_sha(snap), "snapshot": rows(snap)})
            drift = [list(k) for op, k in zip(ops, keys) if not same(snap.get(k), op["before"])]
            if drift:
                step("drift_found", {"keys": drift})
                out["status"] = "drifted"
                return out
            # 3 manual
            if connector.manual:
                out["instructions"] = connector.instructions(conn.account_ref, ops)
                step("manual_instructions", {"instructions": out["instructions"]})
                out["status"] = "awaiting_manual"
                return out
            # 4 prepare (GTM: base check, run workspace) and the dry run
            pr, pmode = connector.prepare(conn.account_ref, ops, ctx, call)
            dr, mode = (connector.dry_run(conn.account_ref, ops, ctx, call) if pr == APPLIED else (pr, pmode))
            out["dry_run"] = {"outcome": dr, "mode": mode}
            step("dry_run", {"outcome": dr, "mode": mode})
            if dr != APPLIED:
                out["status"] = "dry_run_refused" if dr == REFUSED else "dry_run_unknown"
                return out
            # 5-7 apply, stage, finalize, verify (companions must read back as their snapshot)
            failure = _apply_and_verify(connector, conn, ops, keys, extra, snap, ctx, call, step, out)
            if failure is None:
                out["status"] = "applied_verified"
                return out
            out["failure"] = failure
            # 8 rollback (guarded)
            _rollback(connector, conn, snap, ctx, call, step, out)
            return out
        except Halt as h:
            out["failure"] = out["failure"] or h.code
            out["status"] = "halted_revoked" if h.code == "CONNECTION_REVOKED" else \
                "halted_frozen" if h.code == "RESOURCE_FROZEN" else "interrupted"
            out["halt"] = h.code
            _gtm_refs(ctx, out)                       # AEGIS round 3 L2: a halt mid-revert still names our version
            if h.code == "RESOURCE_FROZEN" and sent["n"] and live_rollback is not None and snap:
                try:                                  # Andre froze it mid-write: a guarded rollback, then stop
                    _rollback(connector, conn, snap, ctx, make_call(live_rollback), step, out)
                except Halt:
                    out["rollback"] = {"outcome": UNKNOWN, "proven": False}
                out["status"] = "halted_frozen"
            return out
    finally:
        _cleanup(connector, conn, ctx, make_call(live_rollback or live), step, out)


def _cleanup(connector, conn, ctx, call, step, out) -> None:
    try:
        outcome = connector.cleanup(conn.account_ref, ctx, call)
    except Halt:
        outcome = "halted"
    except Exception:                  # noqa: BLE001
        outcome = UNKNOWN
    if outcome is not None:
        try:
            step("cleanup", {"outcome": outcome})
        except Halt:
            pass
    try:
        left = connector.leftovers(ctx)
    except Exception:                  # noqa: BLE001
        left = []
    if left:
        out["run_workspaces_left"] = sorted(left)


def _apply_and_verify(connector, conn, ops, keys, extra, snap, ctx, call, step, out) -> Optional[str]:
    try:
        for op, key in zip(ops, keys):
            out["written"].append([key[0], key[1], op["before"], op["after"]])
            outcome, _ = connector.write(conn.account_ref, key, op["after"], ctx, call)
            step("op_result", {"target": key[0], "field": key[1], "outcome": outcome})
            if outcome != APPLIED:
                return f"write_{outcome}"
            ctx["state"][key] = op["after"]
        sc = connector.stage_check(conn.account_ref, ctx, call)
        if sc != APPLIED:
            step("stage_check", {"outcome": sc})
            return f"stage_{sc}"
        fin = connector.finalize(conn.account_ref, ctx, call)
        step("finalized", {"outcome": fin})
        if fin != APPLIED:
            return f"finalize_{fin}"
        try:
            back = connector.read(conn.account_ref, keys + extra, ctx, call)
        except UnknownState:
            step("verified", {"outcome": UNKNOWN})
            return "verify_unknown"
        out["readback"] = rows(back)
        mismatched = [list(k) for op, k in zip(ops, keys) if not same(back.get(k), op["after"])]
        mismatched += [list(k) for k in extra if not same(back.get(k), snap.get(k))]
        step("verified", {"outcome": "match" if not mismatched else "mismatch", "readback_sha256": values_sha(back),
                          "readback": rows(back), "mismatched": mismatched})
        return "verify_mismatch" if mismatched else None
    except Halt:
        raise
    except Exception:                   # noqa: BLE001 — a connector bug is a failure, and failures roll back
        step("connector_error", {})
        return "connector_error"


def _rollback(connector, conn, snap, ctx, call, step, out) -> None:
    written = [((t, f), before, after) for t, f, before, after in out["written"]]
    step("rollback_started", {"failure": out["failure"], "writes": len(written)})
    try:
        rb = connector.rollback(conn.account_ref, written, ctx, call)
        keys = [k for k, _, _ in written]
        keys += [k for k in connector.companions(keys) if k not in keys]
        proof_keys = connector.rollback_keys(keys, ctx)
        proven = None
        if rb == APPLIED:
            try:
                back = connector.read(conn.account_ref, proof_keys, ctx, call) if proof_keys else {}
                proven = all(same(back.get(k), snap.get(k)) for k in proof_keys)
            except UnknownState:
                proven = False
    except Halt:
        raise
    except Exception:                   # noqa: BLE001
        rb, proven = UNKNOWN, False
    ok = rb == APPLIED and proven is True
    out["rollback"] = {"outcome": rb, "proven": bool(proven)}
    _gtm_refs(ctx, out)
    step("rollback_result", {"outcome": rb, "proven": bool(proven)})
    out["status"] = "rolled_back" if ok else "rollback_failed"


def _gtm_refs(ctx: dict, out: dict) -> None:
    """GTM: our version is still the container's latest (R2-1) / a newer release may have been un-published by our
    re-publish of the snapshot (round 3 L4, accepted risk): each is named so Andre's task points at it."""
    for k in ("poisoned_version", "release_may_be_unpublished", "foreign_version"):
        if ctx.get(k):
            out[k] = ctx[k]
        else:
            out.pop(k, None)


def verify_manual(connector: Connector, conn: ConnView, ops: list, transport, live) -> tuple[str, Optional[list]]:
    """A guided manual fix: read the platform (never write). ``match`` only when every field equals its ``after``."""
    keys = [(op["target"], op["field"]) for op in ops]

    def call(req: HttpRequest) -> HttpAnswer:
        code = live()
        if code:
            raise Halt(code)
        if req.is_write:
            raise Halt("CONNECTOR_NOT_WIRED")      # a manual connector never sends anything but a read
        try:
            ans = transport.call(conn, req)
        except Halt:
            raise
        except Exception:              # noqa: BLE001
            ans = None
        return ans if isinstance(ans, HttpAnswer) else HttpAnswer(0, None)

    try:
        back = connector.read(conn.account_ref, keys, {}, call)
    except (UnknownState, Halt):
        return UNKNOWN, None
    ok = all(same(back.get(k), op["after"]) for op, k in zip(ops, keys))
    return ("match" if ok else "mismatch"), rows(back)

