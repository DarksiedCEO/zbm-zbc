"""
Intelligence 5 — Counsel Memo Intake (Legal spec §B.6, §C.5): the ONLY path to verified (LG-07). Files memos
and produces proposals for exactly what a memo cites; never interprets a memo and never flips anything the
memo does not cite.

Andre (token) uploads the memo blob plus the ``answers`` / ``cites`` structure he typed from it. Every effect
must be named in ``cites`` (else 409 ``MEMO_DOES_NOT_CITE``, nothing filed, no Compliance call):
- ``answers[].cq_id`` in ``cites.cq_ids`` (aliases are refused: a memo cites the canonical row);
- ``compliance_rows[].obligation_id`` in ``cites.obligation_ids``;
- ``retention_periods`` keys in ``cites.retention_classes``; ``signoff_scopes`` keys in ``cites.signoff_topics``.
A document version's counsel sign-off and a playbook's approval later consume the memo only for the
``doc_versions`` / ``clause_ids`` it cites. The Compliance proposal rows are Andre-typed; Legal only wraps them
with the memo as evidence (``legal37://memos/<memo_id>``).
"""

from __future__ import annotations

import re

import reasons as R

NUMBER, NAME, ACTOR = 5, "Counsel Memo Intake", "intel_05_memo_intake"
RESOLUTIONS = ("verified_rule", "blocks_stay", "needs_more_facts")
DOC_VERSION = re.compile(r"^([a-z][a-z0-9_]{1,60})@([0-9]{1,4}\.[0-9]{1,4})$")
OBLIGATION_ID = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,39}$")
PERIOD = re.compile(r"^P([0-9]{1,3})(Y|M|D)$")


def uncited(memo: dict) -> list[dict]:
    """Every effect the memo asks for that its ``cites`` does not name (complete, no short-circuit)."""
    c = memo["cites"]
    out = []
    for a in memo["answers"]:
        if a["cq_id"] not in c["cq_ids"]:
            out.append(R.item("MEMO_DOES_NOT_CITE", f"answer for {a['cq_id']} but the memo cites "
                              f"{', '.join(c['cq_ids']) or 'no counsel question'}", cq_id=a["cq_id"]))
    for r in memo["compliance_rows"]:
        if r["obligation_id"] not in c["obligation_ids"]:
            out.append(R.item("MEMO_DOES_NOT_CITE", f"Compliance row {r['obligation_id']} is not cited by the memo"))
    for k in memo["retention_periods"]:
        if k not in c["retention_classes"]:
            out.append(R.item("MEMO_DOES_NOT_CITE", f"retention class {k} is not cited by the memo"))
    for k in memo["signoff_scopes"]:
        if k not in c["signoff_topics"]:
            out.append(R.item("MEMO_DOES_NOT_CITE", f"sign-off topic {k} is not cited by the memo"))
    return out


def evidence(memo_id: str, memo_sha256: str, received_at: str, excerpt: str) -> dict:
    """Compliance B.1 evidence for a memo-backed proposal (spec §C.5)."""
    return {"source_url": f"legal37://memos/{memo_id}", "fetched_at": received_at, "snapshot_sha256": memo_sha256,
            "normalized_text_sha256": memo_sha256, "quoted_excerpt": excerpt, "doc_number": memo_id}
