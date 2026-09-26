"""
Intelligence 10 — Evidence and Audit (spec §C.10; "as V&I §C.10", i.e. the compliance-py
i11_evidence_audit module, copied and re-keyed for department ``clipper_network``).

Every decision (admission, enrolment, tier, message, dispute, discipline,
ban, offboarding step, rule version) is recorded on the ledger and appended
to the local log BEFORE it takes effect (the service's record-first plumbing
goes through ``ledger.Recorder`` and ``store.RecordLog``). This module shapes
the audit export: records in log order with their ledger event ids and the
rule version in force. The log never holds contact data (it is in the
contact store), so the export carries only HMACs and hashes of it. Never
acts on what it could not record.

Local log anchoring (AEGIS N14-4, hardened in round 15). Before a line is
appended to the local log, its (log epoch, seq, line SHA-256) is recorded on
the ledger as a ``local_log_appended`` event (id
``cn-log-<epoch>-<seq>-<sha40>``). ``assess`` compares the local log with the
ledger and sorts every mismatch into one of two classes:

- FATAL (start-up refuses, even with CN_RECONCILE_MODE=1): a rule
  version the ledger published above the local version (a rollback), a local
  line the ledger does not anchor (a rewrite), an event the log cites that the
  ledger does not hold, another log's anchors on this ledger, an empty local
  log where the ledger anchors one, a reconcile record the ledger does not
  match.
- VOIDABLE (start-up refuses; only Andre's explicit reconcile lifts it,
  N15-1): an anchor of this log that is not a local line (a commit that failed
  after its anchor, a truncated tail, another instance's line), a ruling of
  this department (admission and enrolment rulings) on the ledger that the
  local log does not hold (N15-2), and
  an instance lease on the ledger newer than the local log's own latest lease
  from a different instance (a copied data directory, N15-2).

Nothing on the ledger alone withdraws an anchor (round 15 N15-1: the old
``local_commit_failed`` marker is no longer written or honoured). A void is
honoured only through a ``reconcile`` line in the LOCAL log whose payload
(epoch, rule version, head seq and head line SHA-256, the voided line
numbers and event ids) hashes to the ``payload_sha256`` of the matching
``reconcile`` event on the ledger, and whose head matches the local line
before it. ``GET /cn/v1/integrity`` reports the same problems at run time.
"""

from __future__ import annotations

import copy
import re
from typing import Iterable, Optional

from ledger import payload_sha256
from rules import sha_text as sha256_text

NUMBER, NAME, ACTOR = 10, "Evidence & Audit", "intel_10_evidence_audit"
PAGE = 500

ANCHOR_TYPE = "local_log_appended"
VERSION_TYPE = "rules_version_published"
RECONCILE_TYPE = "reconcile"
LEASE_TYPE = "instance_lease"
RULING_TYPES = ("admission_ruling", "enrolment_ruling")
LOG_SUBJECT = "cn-log"
DEPARTMENT = "clipper_network"
_ANCHOR_RE = re.compile(r"cn-log-([0-9a-f]{16})-([0-9]{1,12})-([0-9a-f]{40})")
_VERSION_RE = re.compile(r"cn-ver-([0-9a-f]{16})-([0-9]{1,9})-[0-9a-f]{32}")
_LEASE_RE = re.compile(r"cn-lse-([0-9a-f]{16})-([0-9a-f]{16})-([0-9]{1,12})-[0-9a-f]{16}")


def anchor_id(epoch: str, seq: int, line_sha: str) -> str:
    return f"cn-log-{epoch}-{seq}-{line_sha[:40]}"


def version_event_id(epoch: str, n: int, rows_sha: str, prev: Optional[str]) -> str:
    return f"cn-ver-{epoch}-{n}-{sha256_text(f'{n}|{rows_sha}|{prev}')[:32]}"


def lease_id(epoch: str, instance_id: str, head_seq: int, head_sha: str) -> str:
    return f"cn-lse-{epoch}-{instance_id}-{head_seq}-{head_sha[:16]}"


def reconcile_id(epoch: str, head_seq: int, payload: dict) -> str:
    return f"cn-rec-{epoch}-{head_seq}-{payload_sha256(payload)[:40]}"


def reconcile_payload(epoch: str, register_version: Optional[int], head_seq: int, head_sha: str,
                      void_lines: list[int], void_event_ids: list[str]) -> dict:
    """What a reconcile binds (its SHA-256 is what the ledger holds)."""
    return {"epoch": epoch, "register_version": register_version, "head_seq": head_seq, "head_sha256": head_sha,
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
           local_leases: list[tuple[int, str, str]] = (), reconciles: list[tuple[int, dict, str, Optional[int]]] = ()
           ) -> Assessment:
    """``lines``: (seq, line_sha256, anchored) per local line (``anchored`` False only for lines written before
    anchoring existed). ``local_leases``: (seq, instance_id, event_id) of the local lease lines.
    ``reconciles``: (seq, payload, event_id, register_version of that line) of the local reconcile lines.
    ``strict`` (a disk log): another log's anchors on this ledger, or an empty local log while the ledger
    anchors one, are fatal too."""
    out = Assessment()
    anchors: dict[str, dict[tuple[int, str], str]] = {}
    versions: dict[str, set[int]] = {}
    leases: list[tuple[int, str, str]] = []          # (ledger index, event id, instance) for this epoch
    rulings: list[tuple[int, str]] = []
    recs: dict[str, str] = {}                        # reconcile event id -> payload_sha256
    held: set[str] = set()
    first_anchor: Optional[int] = None
    index: dict[str, int] = {}
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or e.get("department") != DEPARTMENT:
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
            versions.setdefault(m.group(1), set()).add(int(m.group(2)))
        elif et == LEASE_TYPE and (m := _LEASE_RE.fullmatch(eid)):
            if m.group(1) == epoch:
                leases.append((i, eid, m.group(2)))
        elif et in RULING_TYPES:
            rulings.append((i, eid))
        elif et == RECONCILE_TYPE:
            recs[eid] = str(e.get("payload_sha256"))
    if epoch is None:
        if strict and (anchors or versions):
            out.fatal.append("the local log is empty but the ledger anchors a Clipper Network log (log deleted or "
                             "truncated to nothing, or a second instance on this ledger)")
        return out
    local_sha = {seq: sha for seq, sha, _ in lines}
    # reconciles: honoured only when the local record, the local line before it and the ledger event all agree
    voided: set[str] = set()
    for seq, payload, eid, line_version in reconciles:
        ok = (isinstance(payload, dict) and payload.get("epoch") == epoch and payload.get("head_seq") == seq - 1
              and seq >= 2 and payload.get("head_sha256") == local_sha.get(seq - 1)
              and payload.get("register_version") == line_version
              and eid == reconcile_id(epoch, seq - 1, payload) and recs.get(eid) == payload_sha256(payload))
        if not ok:
            out.fatal.append(f"the reconcile recorded at local log line {seq} does not match the ledger's reconcile "
                             "event (missing, or its payload hash differs): its voids are not honoured")
            continue
        voided.update(payload.get("void_event_ids") or [])
    if strict:
        foreign = sorted(ep for ep in anchors if ep != epoch)
        if foreign:
            out.fatal.append(f"the ledger anchors {len(foreign)} other Clipper Network log(s) (a replaced or "
                             "recreated log, or a second instance on this ledger)")
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
    top = max(versions.get(epoch, set()), default=None)
    if top is not None and top > local_version:
        # never voidable: a rollback refuses to start, reconcile or not (N15-1)
        out.fatal.append(f"rule version {local_version} is behind the ledger's latest approved version {top} "
                         "(a rollback; no reconcile can void a published version)")
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
             and eid not in local_rulings and eid not in voided]
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


def anchor_problems(entries: Iterable[dict], epoch: Optional[str], lines: list[tuple[int, str, bool]],
                    referenced_ids: set[str], local_version: int, strict: bool, **kw) -> list[str]:
    return assess(entries, epoch, lines, referenced_ids, local_version, strict, **kw).problems


def redact(kind: str, record: dict) -> dict:
    """The log already holds no contact data; the seed proposal is summarized."""
    r = copy.deepcopy(record)
    if kind == "proposal" and r.get("kind") == "seed":
        r["proposed"] = (f"<{len((r.get('proposed') or {}).get('rules') or [])} rules, "
                         f"{len((r.get('proposed') or {}).get('templates') or [])} templates; see GET /cn/v1/rules>")
    if kind == "decision" and r.get("version"):
        r["version"] = {k: v for k, v in r["version"].items() if k not in ("rules", "templates")}
    return r


def export_entry(rec: dict) -> dict:
    data = rec["data"]
    return {"seq": rec["seq"], "kind": rec["kind"], "at": rec["at"], "rules_version": data.get("register_version"),
            "ledger_event_ids": data.get("ledger_event_ids", []), "record": redact(rec["kind"], data.get("record", {}))}
