"""Shared multi-step setups for the certification tests."""

from __future__ import annotations

from helpers import COUNSEL_REF, rid

MSA_TEXT = "MASTER SERVICES AGREEMENT for {{client_name}}. Term ends {{end_date}}. CCPA clause MSA-CCPA-01 applies."
MSA_VARS = {"client_name": {"type": "string", "max_length": 120}, "end_date": {"type": "date"}}


def clause(cid, standard, fb1=None, fb2=None, walk=(), fb1_by="agent", obligations=()):
    return {"clause_id": cid, "title": cid.lower(), "standard_text": standard, "fallback_1_text": fb1,
            "fallback_2_text": fb2, "walk_away": [{"fact": f, "when": True} for f in walk],
            "escalation": {"fallback_1": fb1_by, "fallback_2": "counsel", "unmatched": "counsel"},
            "rationale_code": "house_position", "obligations": list(obligations)}


MSA_CLAUSES = [
    clause("MSA-RENEW-01", "Renewal requires written notice 30 days before the end date.",
           "Renewal requires written notice 45 days before the end date.",
           "Renewal is automatic unless either party objects.",
           obligations=[{"code": "renewal_notice", "party": "zbm", "owner": "andre",
                         "due_rule": "offset(end_date,-30d)", "lead_days": 14}]),
    clause("MSA-PAY-01", "Invoices are payable within 10 business days.",
           "Invoices are payable within 15 business days.", None,
           obligations=[{"code": "payment_terms", "party": "counterparty", "owner": "finance_31",
                         "due_rule": "offset(accepted_at,+10bd)", "lead_days": 3}]),
    clause("MSA-CCPA-01", "ZBM acts as a service provider under the CCPA terms attached.", None, None,
           walk=("audience_data_sale",)),
]


def playbook(x, doc_type="client_msa", clauses=MSA_CLAUSES, version="1.0"):
    ids = [c["clause_id"] for c in clauses]
    memo = x.memo(cites={"clause_ids": ids})
    p = x.ok(x.apost("/legal/v1/playbooks/proposals", {"request_id": rid("pbp"), "counsel_memo_id": memo["memo_id"],
                                                        "playbook": {"playbook_id": f"pb_{doc_type}", "doc_type": doc_type,
                                                                     "version": version, "clauses": clauses}}), 201)
    p = p["proposal"]
    x.ok(x.apost("/legal/v1/playbooks/decisions", {"request_id": rid("pbd"), "proposal_id": p["proposal_id"],
                                                   "content_sha256": p["content_sha256"], "decision": "approve",
                                                   "acknowledge_weakening": p["weakening"]}))
    return p


def executed_msa(x, client="acme", end_date="2027-09-30", with_ccpa=True, verify_cq19=True):
    """Playbook + MSA template v1.0 (counsel) + scheduler fill v1.1 (counsel sign-off on the filled hash) + an
    evidence-sufficient clickwrap acceptance by the client. Returns (filled version, acceptance)."""
    playbook(x)
    uses = [("MSA-RENEW-01", "standard"), ("MSA-PAY-01", "standard")] + ([("MSA-CCPA-01", "standard")] if with_ccpa else [])
    x.approve_doc("client_msa", MSA_TEXT, "1.0", "zbm", uses, MSA_VARS)
    fill = x.ok(x.post("/legal/v1/documents/client_msa/versions",
                       {"request_id": rid("fill"), "version": "1.1", "entity": "zbm",
                        "variables": {"client_name": f"Client {client}", "end_date": end_date}}, caller="scheduler"), 201)
    memo = x.memo(cites={"doc_versions": ["client_msa@1.1"]})
    x.ok(x.apost("/legal/v1/documents/client_msa/versions/1.1/counsel-signoff",
                 {"request_id": rid("so"), "counsel_ref": COUNSEL_REF, "signed_on": "2026-10-01",
                  "doc_sha256": fill["sha256"], "memo_id": memo["memo_id"], "memo_sha256": memo["memo_sha256"]}))
    x.ok(x.apost("/legal/v1/documents/client_msa/versions/1.1/decision",
                 {"request_id": rid("ap"), "decision": "approve", "version_sha256": fill["sha256"]}))
    if verify_cq19:
        x.verify_cq("CQ-19")
    acc = x.clickwrap("client_msa", "1.1", fill["sha256"], party=f"client:{client}", caller="onboarding")
    return fill, acc


def terms(client="acme", signed=True, ccpa=True):
    return {"client_id": client, "signed": signed, "signed_at": None, "start_date": "2026-10-01",
            "end_date": "2027-09-30", "services": ["revenue_recovery"],
            "allowed_commitment_categories": ["callback", "report", "audit"], "monthly_spend_cap_usd": "2500.00",
            "ccpa_cpra_clause_present": ccpa}

