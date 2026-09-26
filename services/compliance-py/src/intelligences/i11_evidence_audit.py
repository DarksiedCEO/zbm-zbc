"""
Intelligence 11 — Evidence and Audit (spec C.9).

Every ruling, decision, screen, check result, control result, hold change
and refused approval is recorded on the ledger and appended to the local
log BEFORE it takes effect (the service's record-first plumbing goes
through ``ledger.Recorder`` and ``store.RecordLog``). This module shapes the
audit export: records in log order with their ledger event ids and the
register version in force, with personal data removed (screen names and
aliases, hold-release reasons, page text are exported only as hashes).
Never issues a ruling it could not record.

Local log anchoring (AEGIS N14-4). Before a line is appended to the local
log, its (log epoch, seq, line SHA-256) is recorded on the ledger as a
``local_log_appended`` event (id ``cmp-log-<epoch>-<seq>-<sha40>``); a
commit that then fails records a best-effort ``local_commit_failed`` marker
with the same coordinates. ``anchor_problems`` compares the local log with
the ledger: a line the ledger anchors beyond the local head (truncation), a
local line the ledger does not anchor (rewrite, or another ledger), an event
id the log cites that the ledger does not hold, or a register version the
ledger published (and no failure marker withdrew) above the local version.
Start-up refuses on any problem (disk logs); control C-11 goes red.
"""

from __future__ import annotations

import copy
import re
from typing import Iterable, Optional

from register import sha256_text

NUMBER, NAME, ACTOR = 11, "Evidence and Audit", "intel_11_evidence_audit"
PAGE = 500

ANCHOR_TYPE = "local_log_appended"
FAILED_TYPE = "local_commit_failed"
VERSION_TYPE = "register_version_published"
LOG_SUBJECT = "compliance-log"
_ANCHOR_RE = re.compile(r"cmp-log-([0-9a-f]{16})-([0-9]{1,12})-([0-9a-f]{40})")
_FAILED_RE = re.compile(r"cmp-lcf-([0-9a-f]{16})-([0-9]{1,12})-([0-9a-f]{40})")
_VERSION_RE = re.compile(r"cmp-ver-([0-9a-f]{16})-([0-9]{1,9})-[0-9a-f]{32}")
_VERSION_SUBJECT_RE = re.compile(r"register:v([0-9]{1,9})")


def anchor_id(epoch: str, seq: int, line_sha: str) -> str:
    return f"cmp-log-{epoch}-{seq}-{line_sha[:40]}"


def failed_id(epoch: str, seq: int, line_sha: str) -> str:
    return f"cmp-lcf-{epoch}-{seq}-{line_sha[:40]}"


def version_event_id(epoch: str, n: int, rows_sha: str, prev: Optional[str]) -> str:
    return f"cmp-ver-{epoch}-{n}-{sha256_text(f'{n}|{rows_sha}|{prev}')[:32]}"


def anchor_problems(entries: Iterable[dict], epoch: Optional[str], lines: list[tuple[int, str, bool]],
                    referenced_ids: set[str], local_version: int, strict: bool) -> list[str]:
    """``lines``: (seq, line_sha256, anchored) per local line (``anchored`` False only for lines written
    before anchoring existed). ``strict`` (a disk log): another log's live anchors on this ledger, or an
    empty local log while the ledger anchors one, are problems too."""
    anchors: dict[str, set[tuple[int, str]]] = {}
    failed: set[tuple[str, int, str]] = set()
    versions: dict[str, set[int]] = {}
    withdrawn: set[tuple[str, int]] = set()
    held: set[str] = set()
    for e in entries:
        if not isinstance(e, dict) or e.get("department") != "compliance":
            continue
        eid, et = e.get("event_id"), e.get("event_type")
        if not isinstance(eid, str):
            continue
        held.add(eid)
        if et == ANCHOR_TYPE and (m := _ANCHOR_RE.fullmatch(eid)):
            anchors.setdefault(m.group(1), set()).add((int(m.group(2)), m.group(3)))
        elif et == FAILED_TYPE and (m := _FAILED_RE.fullmatch(eid)):
            failed.add((m.group(1), int(m.group(2)), m.group(3)))
            if (vm := _VERSION_SUBJECT_RE.fullmatch(str(e.get("subject_id")))):
                withdrawn.add((m.group(1), int(vm.group(1))))
        elif et == VERSION_TYPE and (m := _VERSION_RE.fullmatch(eid)):
            versions.setdefault(m.group(1), set()).add(int(m.group(2)))
    live = {ep: {a for a in s if (ep, a[0], a[1]) not in failed} for ep, s in anchors.items()}
    live = {ep: s for ep, s in live.items() if s}
    problems: list[str] = []
    if epoch is None:
        if strict and (live or versions):
            problems.append("the local log is empty but the ledger anchors a Compliance log (log deleted or "
                            "truncated to nothing, or a second Compliance instance on this ledger)")
        return problems
    if strict:
        foreign = sorted(ep for ep in live if ep != epoch)
        if foreign:
            problems.append(f"the ledger anchors {len(foreign)} other Compliance log(s) (a replaced or recreated "
                            "log, or a second Compliance instance on this ledger)")
    mine = live.get(epoch, set())
    n = len(lines)
    beyond = [seq for seq, _ in mine if seq > n]
    if beyond:
        problems.append(f"local log truncated: the ledger anchors line {max(beyond)} but the local log has {n} lines")
    local_sha = {seq: sha[:40] for seq, sha, _ in lines}
    anchored_by_seq: dict[int, set[str]] = {}
    for seq, sha in mine:
        anchored_by_seq.setdefault(seq, set()).add(sha)
    differs = sorted(seq for seq, shas in anchored_by_seq.items() if seq <= n and local_sha.get(seq) not in shas)
    if differs:
        problems.append(f"{len(differs)} local log line(s) differ from what the ledger anchored (first: line "
                        f"{differs[0]}): the log was edited or rewritten")
    unanchored = [seq for seq, sha, anchored in lines if anchored and (seq, sha[:40]) not in mine]
    if unanchored:
        problems.append(f"{len(unanchored)} local log line(s) have no ledger anchor (first: line {unanchored[0]}): "
                        "the log was rewritten or this is not its ledger")
    missing = referenced_ids - held
    if missing:
        problems.append(f"the local log cites {len(missing)} ledger event(s) the ledger does not hold")
    committed = {v for v in versions.get(epoch, set()) if (epoch, v) not in withdrawn}
    if committed and max(committed) > local_version:
        problems.append(f"register version {local_version} is behind the ledger's latest approved version "
                        f"{max(committed)}")
    return problems


def redact(kind: str, record: dict) -> dict:
    r = copy.deepcopy(record)
    if kind == "screen":
        r.pop("legal_name", None)
        r.pop("aliases", None)
    if kind == "hold_release" and "reason" in r:
        r["reason_sha256"] = sha256_text(r.pop("reason"))
    if kind == "snapshot":
        r.pop("text", None)
    if kind == "proposal" and r.get("kind") == "seed":
        r["proposed_rows"] = f"<{len(r.get('proposed_rows') or [])} rows; see GET /compliance/v1/register>"
    if kind == "decision" and r.get("version"):
        r["version"] = {k: v for k, v in r["version"].items() if k != "rows"}
    return r


def export_entry(rec: dict) -> dict:
    data = rec["data"]
    return {"seq": rec["seq"], "kind": rec["kind"], "at": rec["at"], "register_version": data.get("register_version"),
            "ledger_event_ids": data.get("ledger_event_ids", []), "record": redact(rec["kind"], data.get("record", {}))}
