"""
Intelligence 1 — Obligation Register Keeper (spec B.4-B.6, C table).

Loads the seed as a proposal; computes expiry and effective status; drafts
``reverify`` proposals 7 days before expiry (only when the Change Watcher
holds a fresh snapshot of the row's own source page to use as evidence);
builds the next version from Andre's approved decisions. Never approves
anything; never extends an expiry without Andre; never edits a row in place.

Everything here is pure: ``validate_proposal`` checks one proposal against
the rules of B.5; ``apply`` builds version n+1's rows from version n's rows
plus the approvals of one decision call (all or nothing).
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clock import iso, parse_iso
from controls import ControlDef
from errors import Conflict, Invalid
from ledger import canonical
from register import SHELF_LIFE_DAYS, effective_status, expected_expiry, seed_checks, sha256_text, validate_row

NUMBER, NAME, ACTOR = 1, "Obligation Register Keeper", "intel_01_register"

KINDS = ("seed", "new", "amend", "reverify", "supersede", "retire", "control", "watch_notice")
# watch_notice: an informational inbox item drafted by the Change Watcher when a
# source floods (AEGIS N14-11). Deciding it never changes the register.
INFO_KINDS = ("watch_notice",)
REVERIFY_WINDOW_DAYS = 7
REVERIFY_SNAPSHOT_MAX_AGE = timedelta(days=2)
# AEGIS N14-5: evidence dates (verified_at, evidence.fetched_at) may be at most
# this far in the future (clock skew between a source and us); beyond -> 422.
FUTURE_TOLERANCE = timedelta(days=1)
PROPOSAL_FIELDS = ("proposal_id", "kind", "target_id", "proposed_row", "proposed_rows", "diff", "evidence",
                   "proposed_by", "created_at", "watch", "weakening", "weakening_reasons")


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_url: Optional[str] = Field(max_length=2048)
    fetched_at: str = Field(max_length=40)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quoted_excerpt: str = Field(max_length=2000)
    doc_number: Optional[str] = Field(max_length=64, pattern=r"^[A-Za-z0-9._:/-]{1,64}$")


def content_sha256(p: dict) -> str:
    return sha256_text(canonical({k: p.get(k) for k in PROPOSAL_FIELDS}))


def compute_diff(old: Optional[dict], new: Optional[dict]) -> dict:
    if old is None and new is None:
        return {}
    keys = sorted(set(old or {}) | set(new or {}))
    return {k: {"old": (old or {}).get(k), "new": (new or {}).get(k)} for k in keys
            if (old or {}).get(k) != (new or {}).get(k)}


def finalize_row(row: dict) -> dict:
    """The service recomputes expires_at (never taken from a proposal)."""
    row = copy.deepcopy(row)
    row["expires_at"] = expected_expiry(row) if row["status"] == "verified" else None
    return row


def validate_proposed_row(row: Any, evidence: Optional[dict], proposer: str, now: Optional[datetime] = None) -> dict:
    """B.5 validation. Raises Invalid (422)."""
    if not isinstance(row, dict):
        raise Invalid("proposed_row must be an object")
    try:
        validate_row(row)
    except ValidationError as exc:
        errs = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}"[:160] for e in exc.errors()[:5])
        raise Invalid(f"proposed_row is not a valid register row: {errs}") from None
    if row["status"] not in ("verified", "unverified"):
        raise Invalid("a proposal may set status only to verified or unverified")
    if now is not None and row["verified_at"] is not None and \
            date.fromisoformat(row["verified_at"]) > (now + FUTURE_TOLERANCE).date():
        raise Invalid("verified_at is in the future (more than 1 day ahead): refused")
    if row["status"] == "unverified":
        if row["verified_at"] is not None:
            raise Invalid("an unverified row carries no verified_at")
        return finalize_row(row)
    if row["source_kind"] == "house-rule":
        # ADR 0006 choice 4: house rules (HR-05/06/07 jurisdiction lists and the
        # like) are Andre's own rules — only Andre proposes them, with no URL.
        if proposer != "andre":
            raise Invalid("only Andre may propose a house rule")
        if row["source_quality"] != "founder" or row["source_url"] is not None:
            raise Invalid("a house rule has source_quality founder and no source_url")
        return finalize_row(row)
    if row["source_kind"] == "counsel-question":
        raise Invalid("a counsel question is never verified; supersede it with an approved counsel memo row")
    if evidence is None:
        raise Invalid("status verified requires evidence")
    if evidence.get("source_url") != row["source_url"] or row["source_url"] is None:
        raise Invalid("evidence.source_url must equal proposed_row.source_url")
    if row["source_quality"] not in ("primary", "vendor"):
        raise Invalid("status verified requires source_quality primary or vendor")
    try:
        fetched = parse_iso(evidence["fetched_at"]).date().isoformat()
    except ValueError:
        raise Invalid("evidence.fetched_at must be an RFC 3339 timestamp") from None
    if row["verified_at"] != fetched:
        raise Invalid("verified_at must equal the date of evidence.fetched_at")
    if SHELF_LIFE_DAYS.get(row["source_kind"]) is None:
        raise Invalid("this source_kind has no shelf life and cannot be verified by evidence")
    return finalize_row(row)


def validate_evidence(ev: Any, now: Optional[datetime] = None) -> Optional[dict]:
    if ev is None:
        return None
    try:
        Evidence.model_validate(ev)
    except ValidationError as exc:
        errs = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}"[:160] for e in exc.errors()[:5])
        raise Invalid(f"evidence invalid: {errs}") from None
    if now is not None:
        try:
            fetched = parse_iso(ev["fetched_at"])
        except ValueError:
            raise Invalid("evidence.fetched_at must be an RFC 3339 timestamp") from None
        if fetched > now + FUTURE_TOLERANCE:
            raise Invalid("evidence.fetched_at is in the future (more than 1 day ahead): refused")
    return dict(ev)


# --- weakening (AEGIS N14-9) -----------------------------------------------------------

def _shelf(kind: str) -> float:
    life = SHELF_LIFE_DAYS.get(kind)
    return float("inf") if life is None else float(life)


def _covered(entry: str, entries: list[str]) -> bool:
    return any(entry == e or entry.startswith(e + "-") for e in entries)


def applies_when_narrowed(old: dict, new: dict) -> bool:
    """True when ``new`` applies in fewer situations than ``old`` on any key (spec B.2)."""
    old, new = old or {}, new or {}
    for key in ("lanes", "subject_kinds", "asset_types", "platforms"):
        o, n = old.get(key), new.get(key)
        if n is not None and (o is None or not set(o) <= set(n)):
            return True
    o, n = old.get("jurisdictions"), new.get("jurisdictions")
    if n is not None and (o is None or not all(_covered(e, n) for e in o)):
        return True
    o, n = old.get("flags_any"), new.get("flags_any")
    if n is not None and (o is None or not set(o) <= set(n)):
        return True
    o, n = set(old.get("flags_none") or []), set(new.get("flags_none") or [])
    return bool(n - o)


def row_weakening(kind: str, old: Optional[dict], new: Optional[dict]) -> list[str]:
    """Why a row change may WEAKEN the register (flag, never block: Andre decides with an explicit
    acknowledgment). A new row adds a rule; making a row unverified is stricter; making an unverified
    (blocking) row verified is a weakening (AEGIS N15-7) except on the counsel-memo path (spec E)."""
    if kind == "retire":
        return ["rule_retired"]
    if old is None or new is None:
        return []
    memo = kind == "supersede" and old["source_kind"] == "counsel-question"
    out = []
    if old["status"] == "unverified" and new["status"] == "verified" and not memo:
        out.append("unverified_to_verified")
    if set(old["gates"]) - set(new["gates"]):
        out.append("gates_removed")
    if applies_when_narrowed(old.get("applies_when"), new.get("applies_when")):
        out.append("applies_when_narrowed")
    if new["check"] != old["check"] and not (memo and old["check"] == "counsel_memo"):
        out.append("check_changed")
    if _shelf(new["source_kind"]) > _shelf(old["source_kind"]):
        out.append("source_kind_longer_shelf_life")
    if new.get("parameters") != old.get("parameters"):
        out.append("parameters_changed")
    oe, ne = old.get("effective_date"), new.get("effective_date")
    if ne is not None and (oe is None or ne > oe):
        out.append("effective_date_later")
    if old.get("counsel_flag") and not new.get("counsel_flag") and not memo:
        out.append("counsel_flag_cleared")
    return out


def control_weakening(old: Optional[dict], new: dict) -> list[str]:
    if old is None:
        return []
    out = []
    if set(old.get("blocks_gates") or []) - set(new.get("blocks_gates") or []):
        out.append("control_blocks_fewer_gates")
    if new.get("sla_hours", 0) > old.get("sla_hours", 0):
        out.append("control_sla_longer")
    if set(old.get("obligation_ids") or []) - set(new.get("obligation_ids") or []):
        out.append("control_obligations_removed")
    if new.get("owner_department") != old.get("owner_department"):
        out.append("control_owner_changed")
    if new.get("test") != old.get("test"):
        out.append("control_test_changed")
    if new.get("owner_intelligence") != old.get("owner_intelligence"):   # AEGIS N15-7
        out.append("control_owner_intelligence_changed")
    if new.get("evidence") != old.get("evidence"):                        # AEGIS N15-7: evidence requirements
        out.append("control_evidence_changed")
    return out


def check_verification_path(kind: str, old: dict, new: dict) -> None:
    """AEGIS N15-7: an unverified (blocking) row becomes verified only through a reverify whose evidence is
    the row's OWN source_url (or, for counsel questions, the counsel-memo supersede of spec E). A reverify
    never moves a row to another source (amend the source first, then reverify it)."""
    if _counsel_row(old):
        return  # check_counsel_path governs counsel questions
    if kind == "reverify" and new["source_url"] != old["source_url"]:
        raise Invalid("a reverify keeps the row's own source_url (its evidence must come from that source); "
                      "amend the source first, then reverify")
    if old["status"] == "unverified" and new["status"] == "verified" and kind in ("amend", "supersede"):
        raise Invalid(f"an unverified row becomes verified only by a reverify with evidence from its own "
                      f"source_url, not by {kind}")


def _counsel_row(row: dict) -> bool:
    return row.get("source_kind") == "counsel-question" or bool(row.get("counsel_flag"))


def check_counsel_path(kind: str, old: dict, new: dict) -> None:
    """Spec §E: a counsel question becomes answered ONLY by a supersede whose
    replacement is a counsel-memo row (guidance, primary, verified with the memo
    as evidence). Amend/reverify may never verify it, re-label it or change its check."""
    if not _counsel_row(old):
        return
    if kind == "reverify":
        raise Invalid("a counsel question is never re-verified; supersede it with an approved counsel memo row (spec E)")
    if kind == "amend" and (new["status"] == "verified" or new["source_kind"] != old["source_kind"]
                            or new["counsel_flag"] != old["counsel_flag"] or new["check"] != old["check"]):
        raise Invalid("a counsel question cannot be verified, re-labelled or given another check by amend; "
                      "supersede it with an approved counsel memo row (spec E)")
    if kind == "supersede" and not (new["source_kind"] == "guidance" and new["source_quality"] == "primary"
                                    and new["status"] == "verified"):
        raise Invalid("a counsel question is superseded only by a counsel memo row: source_kind guidance, "
                      "source_quality primary, status verified, the memo as evidence (spec E)")


def build_proposal(kind: str, target_id: Optional[str], proposed_row: Optional[dict], evidence: Optional[dict],
                   proposer: str, now: datetime, current: Optional[dict[str, dict]], ever_ids: set[str],
                   control_defs: dict[str, dict], proposal_id: str, watch: Optional[dict] = None) -> dict:
    """Validate a new/amend/reverify/supersede/retire/control proposal against the
    register version in force. Raises Invalid (422) or Conflict (409)."""
    if kind not in KINDS or kind == "seed" or kind in INFO_KINDS:
        raise Invalid("kind must be one of new, amend, reverify, supersede, retire, control")
    ev = validate_evidence(evidence, now)
    weakening: list[str] = []
    rows = current or {}
    diff: dict
    if kind == "control":
        if not isinstance(proposed_row, dict):
            raise Invalid("a control proposal carries the full control definition in proposed_row")
        try:
            ControlDef.model_validate(proposed_row)
        except ValidationError:
            raise Invalid("proposed_row is not a valid control definition") from None
        if target_id is not None and target_id != proposed_row["control_id"]:
            raise Invalid("target_id must equal proposed_row.control_id")
        old = control_defs.get(proposed_row["control_id"])
        if target_id is None and old is not None:
            raise Conflict("control id already exists; use target_id to change it")
        if target_id is not None and old is None:
            raise Invalid("target control does not exist")
        new = dict(proposed_row)
        diff = compute_diff(old, new)
        weakening = control_weakening(old, new)
    else:
        if current is None:
            raise Conflict("no register version is in force yet: only the seed can be decided")
        if kind == "retire":
            if proposed_row is not None:
                raise Invalid("a retire proposal carries no proposed_row")
            if target_id not in rows:
                raise Invalid("target_id is not a row of the version in force")
            new = None
            diff = {"status": {"old": rows[target_id]["status"], "new": "superseded"}}
            weakening = row_weakening("retire", rows[target_id], None)
        else:
            new = validate_proposed_row(proposed_row, ev, proposer, now)
            if kind == "new":
                if target_id is not None:
                    raise Invalid("a new proposal has no target_id")
                if new["id"] in ever_ids:
                    raise Invalid("row ids are never reused")
                diff = compute_diff(None, new)
            elif kind in ("amend", "reverify"):
                if target_id not in rows or new["id"] != target_id:
                    raise Invalid("amend/reverify: target_id must be a row of the version in force and equal proposed_row.id")
                if kind == "reverify" and new["status"] != "verified":
                    raise Invalid("a reverify proposal proposes status verified")
                check_counsel_path(kind, rows[target_id], new)
                check_verification_path(kind, rows[target_id], new)
                diff = compute_diff(rows[target_id], new)
                weakening = row_weakening(kind, rows[target_id], new)
            else:  # supersede
                if target_id not in rows:
                    raise Invalid("target_id is not a row of the version in force")
                if new["id"] in ever_ids:
                    raise Invalid("the replacement row needs a new id (ids are never reused)")
                check_counsel_path(kind, rows[target_id], new)
                check_verification_path(kind, rows[target_id], new)
                weakening = row_weakening(kind, rows[target_id], new)
                diff = {"target_status": {"old": rows[target_id]["status"], "new": "superseded"},
                        **{f"new.{k}": v for k, v in compute_diff(None, new).items() if k == "id"}}
    p = {"proposal_id": proposal_id, "kind": kind, "target_id": target_id,
         "proposed_row": new if kind != "retire" else None, "proposed_rows": None, "diff": diff, "evidence": ev,
         "proposed_by": proposer, "created_at": iso(now), "watch": watch,
         "weakening": bool(weakening), "weakening_reasons": sorted(set(weakening))}
    p["content_sha256"] = content_sha256(p)
    return p


def watch_notice_proposal(proposal_id: str, now: datetime, watch: dict) -> dict:
    """AEGIS N14-11: one inbox item telling Andre a source flooded (its excess items were not drafted)."""
    p = {"proposal_id": proposal_id, "kind": "watch_notice", "target_id": None, "proposed_row": None,
         "proposed_rows": None, "diff": {}, "evidence": None, "proposed_by": "i06_change_watcher",
         "created_at": iso(now), "watch": watch, "weakening": False, "weakening_reasons": []}
    p["content_sha256"] = content_sha256(p)
    return p


def seed_proposal(rows: list[dict], seed_sha: str, now: datetime) -> dict:
    problems = seed_checks(rows)
    if problems:
        raise RuntimeError(f"seed rows fail the register checks: {problems[:3]}")
    p = {"proposal_id": f"prop-seed-{seed_sha[:16]}", "kind": "seed", "target_id": None, "proposed_row": None,
         "proposed_rows": rows, "diff": {"rows_added": len(rows)},
         "evidence": {"source_url": None, "fetched_at": iso(now), "snapshot_sha256": seed_sha,
                      "normalized_text_sha256": seed_sha, "quoted_excerpt": "", "doc_number": None},
         "proposed_by": "i01_register", "created_at": iso(now), "watch": None, "weakening": False,
         "weakening_reasons": []}
    p["content_sha256"] = content_sha256(p)
    return p


def _recheck(p: dict, reasons: list[str], old: Optional[dict], new: Optional[dict]) -> dict:
    """AEGIS N15-3: the weakening of ``p`` recomputed against the state it is applied to NOW (not the
    proposal-time base). A field changed since the proposal was drafted that the proposal does not touch
    would be silently reverted by it: that is flagged too, and the whole current diff is shown."""
    now_diff = compute_diff(old, new) if (old is not None and new is not None) else dict(p.get("diff") or {})
    reverts = sorted(k for k in now_diff if k not in (p.get("diff") or {}))
    if reverts:
        reasons = [*reasons, "reverts_changes_made_since_drafted"]
    return {"proposal_id": p["proposal_id"], "weakening_reasons": sorted(set(reasons)),
            "drafted_weakening_reasons": sorted(set(p.get("weakening_reasons") or [])), "reverts": reverts,
            "diff": now_diff}


def needs_acknowledgment(p: dict, recheck: Optional[dict]) -> bool:
    """Approval needs acknowledge_weakening when the proposal was flagged, is weakening NOW, or its
    weakening changed since it was drafted (AEGIS N14-9, N15-3)."""
    if recheck is None:
        return bool(p.get("weakening"))
    return (bool(p.get("weakening")) or bool(recheck["weakening_reasons"])
            or recheck["weakening_reasons"] != recheck["drafted_weakening_reasons"])


def apply(base: Optional[list[dict]], controls: dict[str, dict], approvals: list[dict], today: date,
          recheck: Optional[dict[str, dict]] = None) -> tuple[Optional[list[dict]], dict[str, dict]]:
    """All-or-nothing: the rows and control catalog after applying ``approvals`` in order.
    Raises Conflict (409) when a proposal no longer fits (stale: a field it touches changed since it was
    drafted; duplicate id; seed twice). ``recheck`` (filled when given): per proposal id, its weakening
    recomputed against the row or control it replaces at this point of the application (AEGIS N15-3)."""
    rows = None if base is None else {r["id"]: copy.deepcopy(r) for r in base}
    ctl = {k: dict(v) for k, v in controls.items()}
    ever = set(rows or {})
    recheck = {} if recheck is None else recheck
    for p in approvals:
        kind = p["kind"]
        if kind == "seed":
            if rows is not None:
                raise Conflict(f"{p['proposal_id']}: a register version is already in force; the seed cannot be approved again")
            rows = {r["id"]: copy.deepcopy(r) for r in p["proposed_rows"]}
            ever |= set(rows)
            continue
        if kind in INFO_KINDS:
            continue  # informational: deciding it changes nothing
        if kind == "control":
            cid = p["proposed_row"]["control_id"]
            old = ctl.get(cid)
            for k, d in p["diff"].items():
                if (old or {}).get(k) != d["old"]:
                    raise Conflict(f"{p['proposal_id']}: control {cid} changed since the proposal was drafted")
            recheck[p["proposal_id"]] = _recheck(p, control_weakening(old, p["proposed_row"]), old, p["proposed_row"])
            ctl[cid] = dict(p["proposed_row"])
            continue
        if rows is None:
            raise Conflict(f"{p['proposal_id']}: no register version is in force; approve the seed first")
        tid = p["target_id"]
        if kind == "new":
            nid = p["proposed_row"]["id"]
            if nid in ever:
                raise Conflict(f"{p['proposal_id']}: row id {nid} already exists")
            rows[nid] = copy.deepcopy(p["proposed_row"])
            ever.add(nid)
        elif kind in ("amend", "reverify"):
            cur = rows.get(tid)
            if cur is None:
                raise Conflict(f"{p['proposal_id']}: target row {tid} no longer exists")
            for k, d in p["diff"].items():
                if cur.get(k) != d["old"]:
                    raise Conflict(f"{p['proposal_id']}: row {tid} changed since the proposal was drafted (stale)")
            recheck[p["proposal_id"]] = _recheck(p, row_weakening(kind, cur, p["proposed_row"]), cur, p["proposed_row"])
            rows[tid] = copy.deepcopy(p["proposed_row"])
        elif kind in ("supersede", "retire"):
            cur = rows.get(tid)
            if cur is None or cur["status"] == "superseded":
                raise Conflict(f"{p['proposal_id']}: target row {tid} is gone or already superseded")
            if cur["status"] != p["diff"].get("target_status", p["diff"].get("status", {})).get("old"):
                raise Conflict(f"{p['proposal_id']}: row {tid} changed since the proposal was drafted (stale)")
            recheck[p["proposal_id"]] = _recheck(p, row_weakening(kind, cur, p.get("proposed_row")), None, None)
            rows[tid] = {**cur, "status": "superseded"}
            if kind == "supersede":
                nid = p["proposed_row"]["id"]
                if nid in ever:
                    raise Conflict(f"{p['proposal_id']}: row id {nid} already exists")
                rows[nid] = copy.deepcopy(p["proposed_row"])
                ever.add(nid)
    if rows is None:
        return None, ctl  # control-only decision before any version: the catalog changes, rows stay absent
    out = sorted(rows.values(), key=lambda r: r["id"])
    problems = [x for x in seed_checks(out)]
    if problems:
        raise Conflict(f"the resulting register would fail its checks: {problems[:2]}")
    return out, ctl


def reverify_candidates(rows: list[dict], today: date) -> list[dict]:
    """Verified rows (not house rules) whose expiry falls within the next 7 days (or has passed)."""
    out = []
    for r in rows:
        if r["status"] != "verified" or not r.get("expires_at"):
            continue
        if date.fromisoformat(r["expires_at"]) - timedelta(days=REVERIFY_WINDOW_DAYS) <= today:
            out.append(r)
    return out


def reverify_row_from_snapshot(row: dict, snap: dict) -> Optional[tuple[dict, dict]]:
    """A reverify draft from a fresh watcher snapshot of the row's own source_url."""
    if row["source_quality"] not in ("primary", "vendor") or not row.get("source_url"):
        return None
    fetched = parse_iso(snap["fetched_at"]).date().isoformat()
    new = finalize_row({**row, "verified_at": fetched, "status": "verified"})
    ev = {"source_url": row["source_url"], "fetched_at": snap["fetched_at"], "snapshot_sha256": snap["raw_sha256"],
          "normalized_text_sha256": snap["normalized_sha256"], "quoted_excerpt": snap["excerpt"][:2000],
          "doc_number": None}
    return new, ev


def status_now(row: dict, today: date) -> str:
    return effective_status(row, today)
