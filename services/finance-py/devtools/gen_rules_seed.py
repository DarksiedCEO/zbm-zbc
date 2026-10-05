"""
Generate ``seed/fin_rules_seed.json`` from the Finance spec §B.12 rule table and the §H counsel/CPA rows.

The seed's SHA-256 is pinned in ``src/config.py`` and ADR 0009; the service refuses to start on any other file
unless ``FIN_ALLOW_UNPINNED_SEED=1`` with an explicit ``FIN_SEED_SHA256`` (non-production). Run:

    python3 devtools/gen_rules_seed.py            # writes the file and prints its SHA-256

Row shape (verification-py's): rule_id, title, statement, source_urls, basis_obligation_ids, kind, parameters,
status. Finance rule rows are the lead's/founder's decisions and are seeded ``verified`` (their Compliance basis
rows are still read before any action relies on them, spec §D.3). Counsel/CPA rows FIN-CQ-01..15 are seeded
``unverified`` with ``check: counsel_memo``: each blocks what it names until Andre approves an amend that carries a
``memo_ref`` (spec §H).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "seed" / "fin_rules_seed.json"

SQUARE = "https://developer.squareup.com/blog/books-an-immutable-double-entry-accounting-database-service/"
MT = "https://www.moderntreasury.com/journal/enforcing-immutability-in-your-double-entry-ledger"
OCC = ("https://www.occ.gov/publications-and-resources/publications/comptrollers-handbook/files/"
       "payment-sys-funds-transfer-activities/pub-ch-payment-systems.pdf")
FDIC = "https://www.fdic.gov/risk-management-manual-examination-policies/sc-ach"
IRS_BWH = "https://www.irs.gov/businesses/small-businesses-self-employed/backup-withholding"
P1281 = "https://www.irs.gov/pub/irs-pdf/p1281.pdf"
OFAC_FW = "https://ofac.treasury.gov/media/16331/download?inline"
IC3 = "https://www.ic3.gov/PSA/2024/PSA240911"
NACHA_TIPS = "https://www.nacha.org/news/tips-originators-comply-2026-risk-management-rules"
GFOA = "https://www.gfoa.org/materials/bank-account-fraud-prevention"
STRIPE_DISPUTES = "https://docs.stripe.com/disputes/how-disputes-work"
I1099 = "https://www.irs.gov/instructions/i1099mec"
STRIPE_XB = "https://docs.stripe.com/connect/cross-border-payouts"
STRIPE_IDEM = "https://docs.stripe.com/api/idempotent_requests"
CCR = "https://www.law.cornell.edu/regulations/california/10-CCR-80.126.10"
VYRO = "https://www.vyro.com/clipper-terms"
FINDLAW = "https://codes.findlaw.com/ca/civil-code/civ-sect-1748-1/"
COOLEY = "https://www.cooley.com/news/insight/2025/2025-06-04-california-automatic-renewal-law-amendments-take-effect-on-july-1-2025"
KEITER = "https://keitercpa.com/blog/financial-close-checklist/"
QBO = "https://quickbooks.intuit.com/pricing/"

# (rule_id, title, statement, sources, basis, kind)
RULES = [
    ("FIN-00", "No action without an Andre-approved rule version",
     "Until Andre approves a Finance rule version every action is refused RULES_NOT_IN_FORCE.", [], [], "founder"),
    ("FIN-01", "Entities never mix; restricted cash never pays opex or ZBM",
     "No journal entry carries lines of both ZBC and ZBM; restricted cash (1020/1040/1041) never funds operating "
     "expenses, ZBM or intercompany payables.", [], [], "founder"),
    ("FIN-02", "Double entry, balance to zero, append-only, reverse never edit",
     "Every entry balances (debits = credits), has two or more lines in one entity's chart, is never updated or "
     "deleted; a mistake gets a reversing entry. Trial balance per entity is 0.00 after every append.",
     [SQUARE, MT], [], "founder"),
    ("FIN-03", "Money: Decimal, two-decimal strings, one HALF_UP quantize per payable",
     "Money is decimal.Decimal, on the wire a canonical two-decimal string; views x rate / 1000 is exact and "
     "quantized half-up once per payable.", [], [], "founder"),
    ("FIN-04", "Payable only from a V&I certified count and an allowed Compliance payout ruling",
     "A payable is accrued only from a V&I certification (certified/revised) read by Finance and an allowed "
     "Compliance payout ruling for that submission; no caller ever supplies a count or an amount.", [],
     ["HR-12", "HR-13"], "founder"),
    ("FIN-05", "Rate = Andre-published rate card effective at posting time",
     "The creator rate is the latest Andre-published rate card version for the campaign effective at the clip's "
     "posting time (CN-17).", [], [], "founder"),
    ("FIN-06", "Accrual beyond remaining deposit goes to over_budget_hold for Andre",
     "A certification whose revenue amount exceeds the campaign's remaining unearned deposit posts nothing and waits "
     "for Andre (top-up invoice, operating top-up, or refusal).", [], [], "spec_choice"),
    ("FIN-07", "Maker-checker: Andre's token on the batch content_sha256",
     "The system proposes a payout batch; only Andre approves it, by quoting its content_sha256; the approver cannot "
     "release and no caller can approve.", [OCC], [], "founder"),
    ("FIN-08", "Release gates, all re-evaluated at release",
     "Every release gate (certification, Compliance, tax, OFAC, rail, holds, minimum, limits, reconciliation, "
     "treasury, controls, rules) re-runs at release; an absent, stale or stand-in input fails its gate.", [], [],
     "founder"),
    ("FIN-09", "Idempotency key batch+payee+period; a payable in at most one live item",
     "The rail idempotency key is SHA-256(zbc|payout|batch|payee|period); a payable appears in at most one payout item "
     "that is not failed, cancelled or excluded at release; after 23 h a rail retry becomes a lookup.",
     [STRIPE_IDEM], [], "lead_default"),
    ("FIN-10", "Net below the minimum payout carries forward",
     "A payee whose net is below FIN_MIN_PAYOUT is carried forward to the next run (not an error).", [VYRO], [],
     "lead_default"),
    ("FIN-11", "Tax gate before the first dollar",
     "W-9 on file with TIN matched, or W-8 current with the outside-US attestation; anything else blocks (policy "
     "block). No TIN is ever stored by Finance.", [IRS_BWH], ["US-IRS-BWH", "US-IRS-TIN", "US-IRS-FOREIGN",
                                                              "US-IRS-W8-VALID"], "lead_default"),
    ("FIN-12", "Backup withholding 24% when flagged; B-notice timers",
     "A flagged payee is paid gross less 24% withheld (credited to 2040); CP2100 intake starts the 15 / 30 business "
     "day B-notice timers; an overdue timer blocks runs.", [IRS_BWH, P1281], ["US-IRS-BWH"], "research"),
    ("FIN-13", "OFAC: fresh clear screen at release",
     "A payee (and each 25%+ owner of an entity payee) needs a clear Compliance screen at most FIN_OFAC_MAX_AGE_DAYS "
     "old on the current list version at release.", [OFAC_FW], ["US-OFAC-01", "US-OFAC-02", "US-OFAC-03"],
     "lead_default"),
    ("FIN-14", "Destination change: hold, callback to the number on file, cooling-off",
     "A payout destination change puts the payee on hold; only an Andre callback to the contact on file (never one "
     "supplied in the request) lifts it, after FIN_PAYEE_CHANGE_COOLING_OFF_H.", [IC3, NACHA_TIPS], [], "research"),
    ("FIN-15", "Velocity and exposure limits go to the exception queue, never silently",
     "Per-payee per-run, rolling 30-day, first-payout, per-batch and item-count limits; over a limit the payee goes to "
     "the exception queue for Andre.", [FDIC], [], "research"),
    ("FIN-16", "Clawback only by netting; write-off only by Andre; never a pull",
     "Clawbacks are netted against future earnings; a write-off needs Andre and FIN_CLAWBACK_WRITEOFF_MIN_DAYS; no "
     "route debits a creator's external account.", [], [], "lead_default"),
    ("FIN-17", "Daily reconciliation, zero tolerance; an open break blocks runs and sweeps",
     "Every leg must match to the cent; a difference opens a break; any open break or a stale run blocks payout "
     "runs, releases, sweeps and refunds.", [OCC, FDIC], [], "founder"),
    ("FIN-18", "Restricted pool covers creator-and-client liabilities, always",
     "1020 + 1040 + 1041 >= 2010 + 2020 + 2030 + 2040 + 2050 + 2070, on the journal and on independent balances.",
     [], [], "founder"),
    ("FIN-19", "Separation of duties by caller identity",
     "V&I certifies and cannot release; the scheduler proposes and releases and cannot approve; Andre approves and "
     "cannot release; rate cards and rules are Andre-only.", [OCC, FDIC], [], "founder"),
    ("FIN-20", "Access (token roster) review every FIN_ACCESS_REVIEW_DAYS",
     "Andre attests who holds which token; overdue blocks runs and releases.", [NACHA_TIPS, GFOA], [], "research"),
    ("FIN-21", "No surcharge; no card-fee line",
     "No invoice line is a surcharge, card fee, convenience fee or processing fee; card costs are priced into rate "
     "cards.", [FINDLAW], [], "lead_default"),
    ("FIN-22", "Recurring billing only with ARL/ROSCA mechanics",
     "A subscription or retainer invoice needs every recurring field (consent artifact, cancellation medium, annual "
     "reminder, price-change notice, trial reminder) and US-ROSCA in force.", [COOLEY], ["US-ROSCA"], "research"),
    ("FIN-23", "Disputes freeze refunds; evidence pack at settlement",
     "An open dispute pauses the campaign's accruals and freezes refunds on it.", [STRIPE_DISPUTES], [], "research"),
    ("FIN-24", "1099-NEC tracker, IRIS channel, FTB filing",
     "Reportable payments per payee per tax year (America/Los_Angeles release date) are tracked against the year's "
     "threshold (2026: 2000.00) only while US-IRS-1099NEC is in force.", [I1099], ["US-IRS-1099NEC", "US-IRS-1099K"],
     "lead_default"),
    ("FIN-25", "Five-workday close; closed periods locked",
     "A locked period accepts no posting; corrections post in the open period with reverses_entry_id.", [KEITER], [],
     "lead_default"),
    ("FIN-26", "GL is downstream; the journal is the source of truth", "QBO receives a copy; it never drives Finance.",
     [QBO], [], "lead_default"),
    ("FIN-27", "Counsel/CPA rows block what they name",
     "Each FIN-CQ row blocks what it names until Andre approves a memo row.", [], [], "founder"),
    ("FIN-28", "No bank, card, TIN, SSN or DOB data stored",
     "Bank details live at the rail only; Finance keeps opaque references and hashes.", [], [], "founder"),
    ("FIN-29", "Principal model; account titled per R5; banned custody words",
     "ZBC holds its own customer deposits in 'ZBC Client Campaign Deposits'; the words escrow, trust, FBO and 'for "
     "the benefit of' are refused in account titles, invoice lines and template variables.", [CCR],
     ["US-FINCEN-01", "US-CA-FIN-2010L"], "lead_default"),
    ("FIN-30", "Rail reach and Compliance jurisdiction",
     "A payee whose rail cannot reach the country, whose rail account is not verified with payouts enabled, or whose "
     "country is not operate/conditional at Compliance is not payable.", [STRIPE_XB], ["US-STRIPE-KYC"],
     "lead_default"),
    # ADR 0009 amendment, Oct 5 2026 (founder decisions M1-M8, docs/golive/MEDIA_BILLING_SPEC.md)
    ("FIN-31", "Media billing: principal, prepaid, collect before pay, ACH or wire",
     "ZBM buys media as principal: every media buy is prepaid by ACH or wire (never card) on an invoice Finance "
     "builds from the buy, with the vendor cost and ZBM's markup (15% default, set per buy) stored separately; a "
     "vendor payment is recorded only after the client's prepayment has cleared and the hold has passed, never above "
     "the vendor cost; revenue and cost post together when the media has run. A card is accepted only on a Revenue "
     "Recovery invoice of at most $5,000 (above it, ACH), never with a surcharge.", [], [], "founder"),
]

# (id, question, blocks, alias_of)
COUNSEL = [
    ("FIN-CQ-01", "Model A deposit: customer deposit vs trust/fiduciary under California law", "campaign fundable", None),
    ("FIN-CQ-02", "Third-party custody (FBO, DFPI escrow agent, Stripe funds segregation)", "custody model other than own_deposit", None),
    ("FIN-CQ-03", "ASC 606 principal-versus-agent memo; gross vs net", "close finalization", None),
    ("FIN-CQ-04", "Accounting for clawbacks and any administrative hold on unspent budget", "close finalization; admin fee > 0", None),
    ("FIN-CQ-05", "Contractual restriction as restricted cash; current or noncurrent", "close finalization", None),
    ("FIN-CQ-06", "Year-one TIN matching route; unmatched W-9 withholding posture", "withhold_24 policy", None),
    ("FIN-CQ-07", "AB 5 classification of clippers", "nothing extra (Compliance blocks payouts)", "CQ-01"),
    ("FIN-CQ-08", "Days-in-the-US allocation for foreign clippers", "nothing extra", "CQ-04"),
    # Founder M10 (Oct 5 2026) decided card acceptance (Revenue Recovery, <= $5,000, no surcharge); this row now
    # blocks what is still a legal question: late fees and any card surcharge
    ("FIN-CQ-09", "Civ. Code 1748.1 reach to B2B; surviving late fee", "late fees; any card surcharge", None),
    ("FIN-CQ-10", "ARL scope for small-business subscribers; consent/cancel artifacts", "subscription/retainer invoices", None),
    ("FIN-CQ-11", "California sales tax on ZBC/ZBM deliverables; nexus", "invoice issuance (both entities)", None),
    ("FIN-CQ-12", "UCC 4A loss allocation under the bank security procedure", "FC-12 stays red", None),
    ("FIN-CQ-13", "Direct ACH origination obligations", "direct ACH rail", None),
    ("FIN-CQ-14", "FDIC custodial-account recordkeeping; pass-through insurance for FBO", "FBO custody model", None),
    ("FIN-CQ-15", "DAC7/UK/Canada platform-operator status", "nothing extra", "CQ-07"),
    # AEGIS round 17 (N17-12), ADR 0009 amendment: spec C.6 says 24% of GROSS; whether clawback netting at release
    # reduces the backup-withholding basis is a CPA question
    ("FIN-CQ-16", "Backup withholding basis: 24% of the gross payment, or of the gross less clawback netting",
     "FIN_WITHHOLDING_BASIS=gross_minus_netting (gross applies until verified)", None),
]


def rows() -> list[dict]:
    out = []
    for rid, title, stmt, srcs, basis, kind in RULES:
        out.append({"rule_id": rid, "title": title, "statement": stmt, "source_urls": srcs,
                    "basis_obligation_ids": basis, "kind": kind, "parameters": {}, "status": "verified"})
    for rid, question, blocks, alias in COUNSEL:
        params = {"check": "counsel_memo", "blocks": blocks, "memo_ref": None}
        if alias:
            params["alias_of"] = alias
        out.append({"rule_id": rid, "title": question[:240], "statement": f"Counsel/CPA question (spec §H): {question}. "
                    f"Blocks: {blocks}.", "source_urls": [], "basis_obligation_ids": [], "kind": "counsel",
                    "parameters": params, "status": "unverified"})
    return out


def main() -> None:
    doc = {"_doc": "Finance (31) rule register seed — generated by devtools/gen_rules_seed.py from FINANCE_SPEC "
                   "§B.12 and §H (rev 1, Sep 26 2026) plus FIN-CQ-16 (ADR 0009 amendment, AEGIS round 17) and FIN-31 (ADR 0009 amendment, media billing, Oct 5 2026). Do not edit by hand: the SHA-256 is pinned.",
           "rows": rows()}
    data = (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode("utf-8")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(data)
    print(hashlib.sha256(data).hexdigest())


if __name__ == "__main__":
    main()
