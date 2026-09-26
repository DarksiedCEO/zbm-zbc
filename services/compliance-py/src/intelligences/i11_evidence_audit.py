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
"""

from __future__ import annotations

import copy

from register import sha256_text

NUMBER, NAME, ACTOR = 11, "Evidence and Audit", "intel_11_evidence_audit"
PAGE = 500


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
