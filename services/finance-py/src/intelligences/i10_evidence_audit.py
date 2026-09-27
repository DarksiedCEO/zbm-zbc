"""
Intelligence 10 — Evidence & Audit (Finance spec §C.10, §E). Copied from verification-py
``intelligences/i10_evidence_audit.py`` (itself compliance-py's i11: ADR 0006 decisions 4-5, amendments N14-4, N15-1,
N15-2, and ADR 0007 N16-7) with the Finance prefixes; the reconcile procedure is identical (README "Reconciling the
local log with the ledger").

Record-first: every journal entry, payable, batch decision, rail submission, hold, rule version and refused
approval is recorded on the ledger and appended to the local log BEFORE it takes effect; nothing takes effect
that could not be recorded. Before a line is appended to the local log, its (log epoch, seq, line SHA-256)
is recorded on the ledger as ``local_log_appended`` (id ``fin-log-<epoch>-<seq>-<sha40>``). ``assess``
compares the local log with ``GET /ledger/entries``:

- FATAL (start-up refuses, even with VI_RECONCILE_MODE=1): a local line the ledger does not anchor (a rewrite
  or an injected line —
  an injected journal line), an event
  the log cites that the ledger does not hold, another log's anchors, an empty log where the ledger anchors
  one, a reconcile record the ledger does not match.
- VOIDABLE (start-up refuses; only Andre's explicit reconcile lifts it): an anchor of this log that is not
  a local line (a commit that failed after its anchor, a truncated tail, another instance's line), a ruling
  of this department on the ledger the local log does not cite, a newer instance lease from another instance,
  and a ``rules_version_published`` event that matches no decision record of the local log by id and payload
  hash — forged by a ledger-token holder, or a rollback of the log (AEGIS N16-7: a version event alone no longer
  bricks start-up; Andre voids it through the recorded reconcile, or the log is restored).

The audit export shapes records in log order with their ledger event ids and the rule version; identity
values appear only as HMACs, and free text (decision notes, reasons) only as SHA-256.
"""

from __future__ import annotations

import copy
import re
from typing import Iterable, Optional

from ledger import payload_sha256
import hashlib


def sha256_text(text) -> str:
    return hashlib.sha256(str(text).encode("utf-8", "surrogatepass")).hexdigest()

NUMBER, NAME, ACTOR = 10, "Evidence & Audit", "intel_10_evidence_audit"
PAGE = 500

ANCHOR_TYPE = "local_log_appended"
VERSION_TYPE = "rules_version_published"
RECONCILE_TYPE = "reconcile"
LEASE_TYPE = "instance_lease"
# events whose effect must be in the local log (a ledger copy without a local line = a second instance or a lost
# commit: N15-2 "ghost ruling" detection)
RULING_TYPES = ("journal_entry_posted", "payable_accrued", "batch_approved_by_andre", "item_submitted",
                "rate_card_published", "callback_recorded", "invoice_issued", "sweep_approved", "funding_approved",
                "refund_approved", "top_up_approved", "clawback_written_off")
LOG_SUBJECT = "fin-log"
DEPT = "finance"
_ANCHOR_RE = re.compile(r"fin-log-([0-9a-f]{16})-([0-9]{1,12})-([0-9a-f]{40})")
_VERSION_RE = re.compile(r"fin-ver-([0-9a-f]{16})-([0-9]{1,9})-[0-9a-f]{32}")
_LEASE_RE = re.compile(r"fin-lse-([0-9a-f]{16})-([0-9a-f]{16})-([0-9]{1,12})-[0-9a-f]{16}")


def anchor_id(epoch: str, seq: int, line_sha: str) -> str:
    return f"fin-log-{epoch}-{seq}-{line_sha[:40]}"


def version_event_id(epoch: str, n: int, rows_sha: str, prev: Optional[str]) -> str:
    return f"fin-ver-{epoch}-{n}-{sha256_text(f'{n}|{rows_sha}|{prev}')[:32]}"


def lease_id(epoch: str, instance_id: str, head_seq: int, head_sha: str) -> str:
    return f"fin-lse-{epoch}-{instance_id}-{head_seq}-{head_sha[:16]}"


def reconcile_id(epoch: str, head_seq: int, payload: dict) -> str:
    return f"fin-rec-{epoch}-{head_seq}-{payload_sha256(payload)[:40]}"


def reconcile_payload(epoch: str, rules_version: Optional[int], head_seq: int, head_sha: str,
                      void_lines: list[int], void_event_ids: list[str]) -> dict:
    """What a reconcile binds (its SHA-256 is what the ledger holds)."""
    return {"epoch": epoch, "rules_version": rules_version, "head_seq": head_seq, "head_sha256": head_sha,
            "void_lines": sorted(set(void_lines)), "void_event_ids": sorted(set(void_event_ids))}


class Assessment:
    def __init__(self):
        self.fatal: list[str] = []
        self.voidable: list[str] = []
        self.void_lines: set[int] = set()
        self.void_event_ids: set[str] = set()

    @property
    def problems(self) -> list[str]:
        return self.fatal + self.voidable


def assess(entries: Iterable[dict], epoch: Optional[str], lines: list[tuple[int, str, bool]],
           referenced_ids: set[str], local_version: int, strict: bool, *, local_rulings: set[str] = frozenset(),
           local_leases: list[tuple[int, str, str]] = (), reconciles: list[tuple[int, dict, str, Optional[int]]] = (),
           local_versions: Optional[dict[str, str]] = None) -> Assessment:
    """``lines``: (seq, line_sha256, anchored) per local line (``anchored`` False only for lines written before
    anchoring existed). ``local_leases``: (seq, instance_id, event_id) of the local lease lines.
    ``reconciles``: (seq, payload, event_id, rules_version of that line) of the local reconcile lines.
    ``strict`` (a disk log): another log's anchors on this ledger, or an empty local log while the ledger
    anchors one, are fatal too. ``local_versions``: version event id -> payload SHA-256 of every version the
    LOCAL log's decision records published (``local_version_events``); a version event on the ledger that matches
    none of them is voidable, never honoured on its own (AEGIS N16-7)."""
    out = Assessment()
    anchors: dict[str, dict[tuple[int, str], str]] = {}
    versions: dict[str, list[tuple[str, int, str]]] = {}   # epoch -> [(event id, version, payload_sha256)]
    leases: list[tuple[int, str, str]] = []          # (ledger index, event id, instance) for this epoch
    rulings: list[tuple[int, str]] = []
    recs: dict[str, str] = {}                        # reconcile event id -> payload_sha256
    held: set[str] = set()
    first_anchor: Optional[int] = None
    index: dict[str, int] = {}
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or e.get("department") != DEPT:
            continue
        eid, et = e.get("event_id"), e.get("event_type")
        if not isinstance(eid, str):
            continue
        held.add(eid)
        index.setdefault(eid, i)
        if et == ANCHOR_TYPE and (m := _ANCHOR_RE.fullmatch(eid)):
            anchors.setdefault(m.group(1), {})[(int(m.group(2)), m.group(3))] = eid
            if m.group(1) == epoch and first_anchor is None:
                first_anchor = i
        elif et == VERSION_TYPE and (m := _VERSION_RE.fullmatch(eid)):
            versions.setdefault(m.group(1), []).append((eid, int(m.group(2)), str(e.get("payload_sha256"))))
        elif et == LEASE_TYPE and (m := _LEASE_RE.fullmatch(eid)):
            if m.group(1) == epoch:
                leases.append((i, eid, m.group(2)))
        elif et in RULING_TYPES:
            rulings.append((i, eid))
        elif et == RECONCILE_TYPE:
            recs[eid] = str(e.get("payload_sha256"))
    if epoch is None:
        if strict and anchors:
            out.fatal.append("the local log is empty but the ledger anchors a Finance log (log deleted or "
                             "truncated to nothing, or a second Finance instance on this ledger)")
        return out
    local_sha = {seq: sha for seq, sha, _ in lines}
    # reconciles: honoured only when the local record, the local line before it and the ledger event all agree
    voided: set[str] = set()
    for seq, payload, eid, line_version in reconciles:
        ok = (isinstance(payload, dict) and payload.get("epoch") == epoch and payload.get("head_seq") == seq - 1
              and seq >= 2 and payload.get("head_sha256") == local_sha.get(seq - 1)
              and payload.get("rules_version") == line_version
              and eid == reconcile_id(epoch, seq - 1, payload) and recs.get(eid) == payload_sha256(payload))
        if not ok:
            out.fatal.append(f"the reconcile recorded at local log line {seq} does not match the ledger's reconcile "
                             "event (missing, or its payload hash differs): its voids are not honoured")
            continue
        voided.update(payload.get("void_event_ids") or [])
    if strict:
        foreign = sorted(ep for ep in anchors if ep != epoch)
        if foreign:
            out.fatal.append(f"the ledger anchors {len(foreign)} other Finance log(s) (a replaced or recreated "
                             "log, or a second Finance instance on this ledger)")
    mine = anchors.get(epoch, {})
    local_pairs = {(seq, sha[:40]) for seq, sha, _ in lines}
    n = len(lines)
    unanchored = [seq for seq, sha, anchored in lines if anchored and (seq, sha[:40]) not in mine]
    if unanchored:
        out.fatal.append(f"{len(unanchored)} local log line(s) have no ledger anchor (first: line {unanchored[0]}): "
                         "the log was rewritten or this is not its ledger")
    missing = referenced_ids - held
    if missing:
        out.fatal.append(f"the local log cites {len(missing)} ledger event(s) the ledger does not hold")
    # AEGIS N16-7: a version event is honoured only when its id and payload hash match a decision record of the
    # LOCAL log. Any other one in this log's epoch (forged by a ledger-token holder, or published by a decision the
    # local log no longer holds: a rollback) is VOIDABLE: start-up refuses until Andre voids it through the
    # recorded reconcile (voiding a genuine version is a deliberate rollback of the rules, recorded as such).
    known = local_versions or {}
    foreign = sorted((n, eid) for eid, n, psha in versions.get(epoch, [])
                     if eid not in voided and known.get(eid) != psha)
    if foreign:
        out.voidable.append(f"{len(foreign)} rules version event(s) on the ledger match no decision in the local log "
                            f"(version {', '.join(str(n) for n, _ in foreign[:5])}; local version {local_version}): a "
                            "forged event, or a local log rolled back past a published version")
        out.void_event_ids.update(eid for _, eid in foreign)
    stray = sorted((seq, sha, eid) for (seq, sha), eid in mine.items()
                   if (seq, sha) not in local_pairs and eid not in voided)
    beyond = [s for s in stray if s[0] > n]
    within = [s for s in stray if s[0] <= n]
    if beyond:
        out.voidable.append(f"local log truncated: the ledger anchors line {max(s[0] for s in beyond)} but the local "
                            f"log has {n} lines")
    if within:
        out.voidable.append(f"{len(within)} ledger anchor(s) of this log are not local lines (first: line "
                            f"{within[0][0]}): a failed commit, an edited line or another instance's writes")
    for seq, _, eid in stray:
        out.void_lines.add(seq)
        out.void_event_ids.add(eid)
    # N15-2: rulings of this department on the ledger (since this log's first anchor) that the local log lacks
    ghost = [eid for i, eid in rulings if first_anchor is not None and i > first_anchor
             and eid not in local_rulings and eid not in referenced_ids and eid not in voided]
    if ghost:
        out.voidable.append(f"{len(ghost)} ruling(s) on the ledger since this log's first anchor are not in the "
                            f"local log (first: {ghost[0]}): a second instance, or a ruling whose commit failed")
        out.void_event_ids.update(ghost)
    # N15-2: an instance lease newer than this log's own latest lease, from another instance
    mine_leases = {eid for _, _, eid in local_leases}
    latest = max((index[eid] for eid in mine_leases if eid in index), default=-1)
    newer = [(i, eid, inst) for i, eid, inst in leases if i > latest and eid not in mine_leases and eid not in voided]
    if newer:
        out.voidable.append(f"the ledger shows a newer instance lease for this log from another instance "
                            f"({newer[-1][2]}): this data "
                            "directory was copied and another instance runs (or ran) on it")
        out.void_event_ids.update(eid for _, eid, _ in newer)
    return out


def local_version_events(epoch: Optional[str], metas: Iterable[dict]) -> dict[str, str]:
    """version event id -> payload SHA-256 for every version a local decision record published (its meta holds
    version, rows_sha256, prev_version_sha256, proposal_ids) — exactly what the service recorded."""
    out: dict[str, str] = {}
    for meta in metas:
        try:
            eid = version_event_id(epoch or "0" * 16, meta["version"], meta["rows_sha256"], meta["prev_version_sha256"])
            out[eid] = payload_sha256({k: meta[k] for k in ("version", "rows_sha256", "prev_version_sha256",
                                                            "proposal_ids")})
        except (KeyError, TypeError):
            continue
    return out


def anchor_problems(entries: Iterable[dict], epoch: Optional[str], lines: list[tuple[int, str, bool]],
                    referenced_ids: set[str], local_version: int, strict: bool, **kw) -> list[str]:
    return assess(entries, epoch, lines, referenced_ids, local_version, strict, **kw).problems


REDACT_TEXT_KEYS = ("note", "reason", "decision_reason", "notes", "description", "message_text")
# never exported (spec §D audit export: "no contact refs"; FIN-28): dropped outright, wherever they sit
DROP_KEYS = ("callback_contact_ref", "contact_ref", "rail_account_ref", "account_ref")


def _scrub(obj, depth: int = 0):
    if depth > 30:
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in DROP_KEYS:
                continue
            if k in REDACT_TEXT_KEYS and isinstance(v, str):
                out[k + "_sha256"] = sha256_text(v)
                continue
            out[k] = _scrub(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [_scrub(x, depth + 1) for x in obj]
    return obj


def redact(kind: str, record: dict) -> dict:
    """Free text only as SHA-256; contact and rail account references dropped; seed rows summarized."""
    r = _scrub(copy.deepcopy(record))
    if kind == "proposal" and r.get("kind") == "seed":
        r["proposed_rows"] = f"<{len(r.get('proposed_rows') or [])} rows; see GET /fin/v1/rules>"
    if kind == "decision" and r.get("version"):
        r["version"] = {k: v for k, v in r["version"].items() if k != "rows"}
    return r


def export_entry(rec: dict) -> dict:
    data = rec["data"]
    return {"seq": rec["seq"], "kind": rec["kind"], "at": rec["at"], "rules_version": data.get("rules_version"),
            "ledger_event_ids": data.get("ledger_event_ids", []),
            "records": [{"kind": k, "record": redact(k, r)} for k, r in data.get("ops", [])]}
