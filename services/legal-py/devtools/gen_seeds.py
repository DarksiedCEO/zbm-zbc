"""
Generates every file in ``seed/`` deterministically (run from anywhere: ``python3 devtools/gen_seeds.py``).

The output bytes are pinned in ``src/config.py`` (PINNED_SEEDS) and in ADR 0010; the service refuses to start
when a seed file's SHA-256 differs (the rules seed alone has the N14-13 non-production override). Re-running
this script must reproduce the files byte for byte; a changed seed is a code change with a new pin.

Sources: LEGAL_SPEC.md rev 1 (§B.12 rules, §D documents, §I counsel questions, §B.10 retention, §B.11
sign-off topics, §A.3 advice patterns) and FINANCE_SPEC §H (FIN-CQ rows, mirrored by id). Nothing here is
legal advice; every row that rests on an unverified or secondary source says so.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "seed"

LR = "reports/Legal department world class model.md"
JUSTIA_UPL = "https://law.justia.com/codes/california/2005/bpc/6125-6140.05.html"
CALBAR = "https://www.calbar.ca.gov/Portals/0/documents/ethics/Generative-AI-Practical-Guidance.pdf"
ABA512 = "https://www.americanbar.org/news/abanews/aba-news-archives/2024/07/aba-issues-first-ethics-guidance-ai-tools/"
CIV1633_9 = "https://law.justia.com/codes/california/code-civ/division-3/part-2/title-2-5/section-1633-9/"
ESIGN = "https://uscode.house.gov/view.xhtml?path=%2Fprelim%40title15%2Fchapter96&edition=prelim"
IRONCLAD = "https://ironcladapp.com/resources/articles/modern-contract-playbook"
SALESFORCE = "https://www.salesforce.com/news/stories/salesforce-legal-building-agentic-enterprise-playbook/"
CLA = ("https://calawyers.org/law-practice-management-technology/preservation-obligations-preserving-potentially-"
       "relevant-evidence-in-california-litigation/")
USC512 = "https://www.law.cornell.edu/uscode/text/17/512"
COPYRIGHT_FAQ = "https://www.copyright.gov/dmca-directory/faq.html"
USPTO = "https://www.uspto.gov/trademarks/fees-payment-information/summary-2025-trademark-fee-changes"
SOS = "https://www.sos.ca.gov/business-programs/business-entities/statements"
TIKTOK_CML = "https://ads.tiktok.com/help/article/commercial-music-library"
ZENGRC = "https://www.zengrc.com/blog/how-long-do-i-have-to-respond-to-ccpa-verifiable-consumer-requests/"
ACC = "https://www.acc.com/maturity/external-resources-management"


def rule(rid, title, statement, kind, urls, status="verified", **params):
    return {"rule_id": rid, "title": title, "statement": statement, "source_urls": urls, "kind": kind,
            "parameters": params, "status": status}


RULES = [
    rule("LG-00", "No action without an Andre-approved rule version",
         "Until Andre approves a Legal rule version every action is refused RULES_NOT_IN_FORCE.", "founder", []),
    rule("LG-01", "UPL boundary: Legal never gives legal advice",
         "To a third party Legal emits only counsel-approved documents or templates (pinned by SHA-256) with typed, "
         "bounded variables, the not_legal_advice_v1 routing notice, or dates and statuses of the party's own "
         "records. To Andre and departments it emits codes labelled unreviewed until a counsel memo or template id "
         "is attached. Every rendered outbound text and every template variable passes the deterministic "
         "advice-text guard.", "founder", [JUSTIA_UPL, CALBAR, ABA512]),
    rule("LG-02", "Approved = counsel sign-off matching the hash (when required) + Andre",
         "A document version is approved only by Andre's token on its content hash and, when counsel_required, only "
         "after a counsel sign-off whose doc_sha256 equals the version's SHA-256.", "founder", []),
    rule("LG-03", "Acceptance evidence is IP-free and hash-bound",
         "Acceptance records carry doc id, version, doc SHA-256, presented SHA-256, Legal's timestamp, method, "
         "presentation, affirmative act and a signer identity ref; never an IP address, user agent or device data. "
         "A hash that differs from the register is refused.", "lead_default", [CIV1633_9, ESIGN]),
    rule("LG-04", "Deviation beyond the agent's fallback authority goes to counsel",
         "An agent may accept a playbook fallback_1 where the clause allows it and LEGAL_AGENT_MAX_FALLBACK permits; "
         "every fallback_2, unmatched clause and walk-away trigger escalates to counsel.", "lead_default", [IRONCLAD],
         max_fallback_ceiling=1),
    rule("LG-05", "Playbook and template changes only by Andre with a counsel memo",
         "A playbook or template change takes effect only on Andre's approval and only with a filed counsel memo "
         "that cites every clause it sets.", "founder", []),
    rule("LG-06", "Obligations only from evidence-sufficient executed documents",
         "Obligations are created only when an acceptance with evidence_sufficient true exists, by binding the "
         "clause's obligation descriptors to the executed variables; counterparty paper obligations are entered by "
         "Andre from a counsel memo only.", "spec_choice", []),
    rule("LG-07", "Counsel memo intake is the only path to verified",
         "Only a filed counsel memo turns an unverified item verified, and only for what the memo's cites name. "
         "Compliance may only tighten (invalidate).", "founder", []),
    rule("LG-08", "Triage matrix and same-day counsel",
         "Agency letters, subpoenas, demand letters, litigation threats, threatened class actions and data "
         "incidents route to counsel the same day and a hold is issued at once; an ambiguous trigger escalates "
         "and a hold is issued.", "research", [SALESFORCE], dispute_threshold_config="LEGAL_DISPUTE_THRESHOLD"),
    rule("LG-09", "An active hold bars deletion",
         "An active hold bars deletion of every subject ref it lists; release only by Andre with a filed counsel "
         "memo.", "research", [CLA]),
    rule("LG-10", "Takedown elements and the counter-notice restore window",
         "A takedown notice is valid only with all six 17 U.S.C. 512(c)(3)(A) elements; after a counter-notice the "
         "restore window is not less than 10 nor more than 14 business days after receipt unless the claimant "
         "files an action. Business days exclude weekends and the seeded US federal holidays (counsel to "
         "confirm).", "research", [USC512], restore_min_business_days=10, restore_max_business_days=14),
    rule("LG-11", "Filings calendar",
         "DMCA designated agent expires three years after registration; trademark, Statement of Information, FBN "
         "and insurance dates are calendared from entered dates; a lapse alerts Andre and opens a matter. Legal "
         "never files or pays.", "research", [COPYRIGHT_FAQ, USPTO, SOS], dmca_designation_years=3),
    rule("LG-12", "Music policy: platform commercial library only",
         "Absent music is allowed; changed music or a swap on a repost is always blocked; licensed music is blocked "
         "by policy; commercial-library music needs a track id, a verified platform library rule and CQ-21 "
         "verified.", "lead_default", [TIKTOK_CML], verified_platform_libraries=["tiktok"]),
    rule("LG-13", "Retention: nothing deleted while unverified or held",
         "Legal deletes nothing in a retention class that is unverified (CQ-26) or under an active hold.",
         "spec_choice", []),
    rule("LG-14", "Text is data; injection patterns logged and ignored",
         "Uploaded and caller-supplied text is hashed and stored as data; it is never executed, templated or "
         "followed. Instruction-like patterns are recorded as injection_text_ignored and change nothing.",
         "founder", []),
    rule("LG-15", "Subpoena handling",
         "Calendar the return date, route to counsel the same day, preserve, produce nothing without a counsel "
         "memo.", "research", []),
    rule("LG-16", "DSAR clock (secondary, UNVERIFIED)",
         "Confirm within 10 business days; respond within 45 days; one 45-day extension; never more than 90 days. "
         "Secondary source; statutory cite unverified.", "research", [ZENGRC], status="unverified",
         confirm_business_days=10, respond_days=45, extension_days=45, max_days=90),
    rule("LG-17", "Counsel routing only under an approved engagement letter with AI terms",
         "Counsel memos are accepted only from a counsel_ref that names an approved engagement letter; the "
         "engagement letter is approvable only with its AI-use clause.", "research", [ACC, CALBAR]),
    rule("LG-18", "Counsel-question rows block what they name",
         "Each counsel-question row stays unverified, and everything it blocks stays blocked, until a counsel memo "
         "resolves it.", "founder", []),
    rule("LG-19", "Outbound DMCA notices",
         "An outbound notice needs Andre's token as the authorized signer and counsel review first when a licence "
         "or fair use is possible (512(f)).", "research", [USC512]),
]

# §D. counsel_required per the "Counsel drafts/approves v1" column; "template only", "approve once",
# "countersigns" and "review before filing" all count as counsel_required (safest reading, ADR 0010 choice 6).
ESIGNABLE = ("client_msa", "sow", "zbc_order_form", "clipper_agreement", "ic_agreement", "nda", "engagement_letter",
             "dpa")


def doc(doc_id, title, doc_type, entities, counsel_required, blocked_by=(), required_clauses=()):
    return {"doc_id": doc_id, "title": title, "doc_type": doc_type, "entities": list(entities),
            "counsel_required": counsel_required, "approval_blocked_by": list(blocked_by),
            "required_clause_ids": list(required_clauses), "esign_allowed": doc_type in ESIGNABLE}


DOCUMENTS = [
    doc("client_msa", "Client MSA", "client_msa", ("zbm", "zbc"), True),
    doc("sow", "SOW / campaign brief", "sow", ("zbm", "zbc"), False),
    doc("zbc_order_form", "ZBC campaign order form", "zbc_order_form", ("zbc",), True),
    doc("clipper_agreement", "Clipper / creator agreement", "clipper_agreement", ("zbc",), True),
    doc("ic_agreement", "Independent-contractor agreement", "ic_agreement", ("zbm", "zbc"), True, ("CQ-17",)),
    doc("nda_mutual", "Mutual NDA", "nda", ("zbm", "zbc"), True),
    doc("nda_oneway", "One-way NDA", "nda", ("zbm", "zbc"), True),
    doc("privacy_policy", "Privacy policy", "privacy_policy", ("zbm", "zbc"), True),
    doc("terms", "Website / portal terms of service", "terms_of_service", ("zbm", "zbc"), True),
    doc("cookie_notice", "Cookie / consent notice", "cookie_notice", ("zbm", "zbc"), True),
    doc("dmca_procedure", "DMCA agent designation and takedown procedure", "dmca_procedure", ("zbc",), True),
    doc("tm_zbm", "Trademark application: Z Best Media", "data_sheet", ("zbm",), True),
    doc("tm_zbc", "Trademark application: Z Best Clips", "data_sheet", ("zbc",), True),
    doc("engagement_letter", "Outside-counsel engagement letter and billing guidelines", "engagement_letter",
        ("zbm",), True, (), ("ENG-AI-01",)),
    doc("dpa", "Data processing addendum (EU/UK)", "dpa", ("zbm", "zbc"), True),
    doc("lit_hold_notice", "Litigation hold notice template", "lit_hold_notice", ("zbm", "zbc"), True),
    doc("subpoena_response", "Subpoena response template", "subpoena_response", ("zbm", "zbc"), True),
    doc("dsar_response", "DSAR response template", "dsar_response", ("zbm", "zbc"), True),
    doc("not_legal_advice_v1", "Routing notice (not legal advice)", "routing_notice", ("zbm", "zbc", "silverback"),
        True, ("CQ-15",)),
]
# soi_<entity> and fbn_<dba> are data sheets created on Andre's first upload (counsel_required false, §D).
DATA_SHEET_PREFIXES = ("soi_", "fbn_")


def cq(cq_id, origin, question, why, url, blocks, alias_of=None, related=()):
    return {"cq_id": cq_id, "origin": origin, "question": question, "why_counsel_only": why, "source_url": url,
            "blocks": [{"service": s, "what": w} for s, w in blocks], "alias_of": alias_of, "related": list(related)}


OGLETREE = "https://ogletree.com/insights-resources/blog-posts/ab-2257-enacts-significant-changes-to-ab-5-on-classification-of-workers-as-independent-contractors/"
QUESTIONS = [
    cq("CQ-01", "compliance", "Are clippers independent contractors under AB 5's ABC test; does an AB 2257 exemption fit?",
       "prong B fits poorly; retroactive state liability", OGLETREE, [("compliance", "every payout")],
       related=("CQ-17",)),
    cq("CQ-02", "compliance", "If ZBC pools client funds (Model B), do FinCEN processor conditions and section 2010(l) cover it; what contract language?",
       "money-transmitter licensing", "https://www.fincen.gov/sites/default/files/administrative_ruling/FIN-2014-R009.pdf",
       [("compliance", "payout when pooled")], related=("FIN-CQ-01",)),
    cq("CQ-03", "compliance", "Can a disclosed pay-per-view clip be a Part 465 testimonial; does paying only for verified views keep ZBC outside 465.4?",
       "no FTC guidance on point", None, [("compliance", "every payout")]),
    cq("CQ-04", "compliance", "Income sourcing for foreign clippers; days-in-US allocation",
       "pay-per-view pattern untested", "https://www.irs.gov/publications/p515", [("compliance", "foreign payouts")]),
    cq("CQ-05", "compliance", "GDPR/UK Art. 27(2) occasional-processing exemption or appoint representatives?",
       "no regulator guidance", None, [("compliance", "control only")]),
    cq("CQ-06", "compliance", "Does Loi 2023-451 Art. 9 bind a non-EU agency targeting France?",
       "secondary sources only", None, [("compliance", "all gates for FR")]),
    cq("CQ-07", "compliance", "Does a self-serve marketplace make ZBC a DAC7/UK/Canada platform operator?",
       "directive text not fetched", None, [("compliance", "activation for a marketplace")]),
    cq("CQ-08", "compliance", "Which NY-visible clips need a GBL 396-b synthetic-performer disclosure; what is conspicuous?",
       "new law, no rulings",
       "https://www.cooley.com/news/insight/2026/2026-01-29-new-york-enacts-synthetic-performer-disclosure-law-for-advertisements-including-those-using-generative-ai",
       [("compliance", "payout/publish, synthetic + US")]),
    cq("CQ-09", "compliance", "Texas TDPSA sensitive-data consent vs SBA small business; IN/KY cure periods",
       "secondary sources conflict", None, [("compliance", "control only")]),
    cq("CQ-10", "compliance", "Is ZBM a Delete Act data broker; registration window and fee",
       "fact-specific; daily penalties", None, [("compliance", "activation/publish with audience-data sale")],
       related=("CQ-20",)),
    cq("CQ-11", "compliance", "Creator-agreement venue/arbitration/governing law across states; does minimum-live forfeiture survive wage challenges?",
       "jurisdictional", None, [("compliance", "every payout")], related=("CQ-18",)),
    cq("CQ-12", "compliance", "Foreign-qualification or income-tax nexus from paying out-of-state creators; is portal SaaS taxable?",
       "CPA/SALT item", None, [("compliance", "control only")], related=("FIN-CQ-11",)),
    cq("CQ-13", "compliance", "May ZBM crawl X's and Meta's policy pages?", "X terms bar crawling",
       "https://techcrunch.com/2023/09/08/x-updates-its-terms-to-ban-crawling-and-scraping",
       [("compliance", "X/Meta change detection")]),
    cq("CQ-14", "compliance", "Do clients selling into Quebec trigger Charter of the French Language duties for ZBM's creative?",
       "not researched", None, [("compliance", "all gates for CA-QC")]),
    cq("CQ-15", "legal", "Which agent activities fall inside B&P 6125, and what portal language keeps them outside?",
       "6125 undefined; 6126 criminal", JUSTIA_UPL,
       [("legal", "portal FAQ (forced off)"), ("legal", "not_legal_advice_v1 approval")], related=("CN-CQ-08",)),
    cq("CQ-16", "legal", "Does the Talent Agencies Act reach a clipping agency; does buyer-of-deliverables with no agency avoid 1700.5 and 1700.25?",
       "no Labor Commissioner ruling found", "https://www.dir.ca.gov/dlse/talent/talent_laws_relating_to_talent_agencies.pdf",
       [("clipper_network", "campaign enrolment")]),
    cq("CQ-17", "legal", "Which 2775 exemption (2776 B2B, 2778) fits clippers and editors; which practices break it?",
       "prong B; Tomasello split", "https://www.labor.ca.gov/employmentstatus/faq/",
       [("legal", "approval of ic_agreement")], related=("CQ-01",)),
    cq("CQ-18", "legal", "Are arbitration, class waiver, minimum-live forfeiture and clawback enforceable if a clipper is later an employee or consumer?",
       "status-dependent", None, [], related=("CQ-11",)),
    cq("CQ-19", "legal", "What clickwrap presentation and versioned-acceptance evidence satisfies Civ. Code 1633.9; which documents need 7001(c) consent?",
       "attribution burden on ZBM; 1633.3 UNVERIFIED", CIV1633_9, [("legal", "clickwrap evidence_sufficient")]),
    cq("CQ-20", "legal", "Mandatory CCPA service-provider terms; which client asks make ZBM a third party or a Delete Act data broker?",
       "1798.100(d) not fetched", "https://oag.ca.gov/privacy/ccpa", [("legal", "clause MSA-CCPA-01 / Onboarding P23")],
       related=("CQ-10",)),
    cq("CQ-21", "legal", "Music warranty, repost prohibition and indemnity; does a platform commercial library license a paid clip?",
       "labels plead creative control",
       "https://www.musicbusinessworldwide.com/fashion-brand-quince-recently-valued-at-10b-sued-by-umg-over-unlicensed-use-of-music-from-sabrina-carpenter-justin-bieber-billie-eilish-and-more-in-tiktok-posts/",
       [("legal", "every music-bearing clip (LG-12)")]),
    cq("CQ-22", "legal", "DMCA agent and 512(i) policy for a portal hosting submissions; exposure as an uploader without safe harbor",
       "safe harbor covers hosted material only", USC512, [("verification_integrity", "portal media hosting")]),
    cq("CQ-23", "legal", "Which coverages and limits fit; which exclusions gut them?", "broker pages omit exclusions",
       "https://foundershield.com/business-insurance/media/advertising-insurance/", []),
    cq("CQ-24", "legal", "Engagement-letter terms on counsel's AI use, agent drafts, AI billing, conflicts",
       "Rule 1.5/1.6 duties are counsel's", CALBAR, [("legal", "approval of engagement_letter without the AI clauses")]),
    cq("CQ-25", "legal", "Which events beyond demand letters, subpoenas and complaints trigger preservation; hold scope for clipper-owned accounts?",
       "fact-specific", CLA, []),
    cq("CQ-26", "legal", "Retention periods for payroll, contractor, tax and contract records; what survives a hold override?",
       "1174 by paraphrase; others UNVERIFIED", "https://877suemyboss.com/labor-code-1174/",
       [("legal", "every deletion by Legal")]),
    cq("CQ-27", "legal", "One class or several for Z Best Media / Z Best Clips; do the marks clear a search?",
       "clearance is a legal judgment", USPTO, [("legal", "trademark filings ready")]),
    cq("VI-CQ-01", "vi", "Is fraud screening on YouTube API Data or a cross-clipper winners library derived data or prohibited aggregation?",
       "YouTube audits use cases", "https://developers.google.com/youtube/terms/developer-policies",
       [("verification_integrity", "YouTube anomaly signals; YouTube results to Creative Memory")]),
    cq("VI-CQ-02", "vi", "Retention/use limits on TikTok, Instagram and X API data", "not found (UNVERIFIED)", None, []),
    cq("VI-CQ-03", "vi", "May V&I keep a minor's refusal record (result + identity HMACs)?", "FTC: delete promptly",
       "https://www.ftc.gov/system/files/ftc_gov/pdf/coppa-age-verification-policy-statement.pdf",
       [("verification_integrity", "the retained HMACs")]),
    cq("VI-CQ-04", "vi", "Device-fingerprint / IP collection for duplicate-identity checks", "privacy law not researched",
       None, [("verification_integrity", "device/IP signals")]),
    cq("VI-CQ-05", "vi", "May platform-returned thumbnails/media be hashed and compared?", "ToS on media use not retrieved",
       None, [("verification_integrity", "TikTok cover PDQ")]),
    cq("VI-CQ-06", "vi", "Is X impression_count a payable view?", "definition UNVERIFIED",
       "https://docs.x.com/x-api/fundamentals/data-dictionary", [("verification_integrity", "X certification")]),
    cq("CN-CQ-01", "cn", "Talent Agencies Act", "alias", "https://www.dir.ca.gov/dlse/talent/talent_laws_relating_to_talent_agencies.pdf",
       [("clipper_network", "enrolment")], alias_of="CQ-16"),
    cq("CN-CQ-02", "cn", "AB 5", "alias", "https://www.labor.ca.gov/employmentstatus/faq/", [], alias_of="CQ-01"),
    cq("CN-CQ-03", "cn", "May SAG-AFTRA members be admitted; does the 2025 Influencer Waiver apply?", "union obligations",
       "https://www.sagaftra.org/sites/default/files/2025%20Influencer-Produced%20Sponsored%20Content%20Waiver.pdf",
       [("clipper_network", "enrolment of members")]),
    cq("CN-CQ-04", "cn", "Minimum-live forfeiture and clawback vs wage challenges", "alias", None, [], alias_of="CQ-11"),
    cq("CN-CQ-05", "cn", "Reddit and X rules on paid-opportunity/recruiting posts", "not fetched", None,
       [("clipper_network", "Reddit, X channels")]),
    cq("CN-CQ-06", "cn", "CASL for member messages to Canadian clippers", "CASL page not fetched",
       "https://www.fightspam.gc.ca/eic/site/030.nsf/eng/home", [("clipper_network", "email to CA clippers")]),
    cq("CN-CQ-07", "cn", "TCPA consent for recruiting or member texts", "FCC page not fetched",
       "https://www.fcc.gov/consumers/guides/stop-unwanted-robocalls-and-texts", [("clipper_network", "SMS")]),
    cq("CN-CQ-08", "cn", "Automated-system disclosure wording (Cal. B&P 17941)", "wording by counsel", None, [],
       related=("CQ-15",)),
    cq("FIN-CQ-01", "finance", "Does the client contract make prepayments ZBC's own customer deposit; does a segregated-account policy create a trust?",
       "money-transmitter exposure", "https://www.law.cornell.edu/regulations/california/10-CCR-80.126.10",
       [("finance_31", "campaign fundable")], related=("CQ-02",)),
    cq("FIN-CQ-02", "finance", "Third-party custody options (bank FBO, licensed escrow, processor segregation)",
       "escrow-law burden on the claimant", "https://dfpi.ca.gov/regulated-industries/escrow-law/about-the-escrow-law/",
       [("finance_31", "any custody model other than own_deposit")]),
    cq("FIN-CQ-03", "finance", "ASC 606 principal-versus-agent memo per service", "judgment on control indicators",
       "https://storage.fasb.org/ASU%202016-08.pdf", [("finance_31", "close finalization")]),
    cq("FIN-CQ-04", "finance", "Accounting for clawbacks and administrative holds", "no primary source found", None,
       [("finance_31", "close finalization; admin fee")]),
    cq("FIN-CQ-05", "finance", "Does a contractual restriction make the deposit account restricted cash?",
       "ASC 210 unverified", "https://storage.fasb.org/ASU%202016-18.pdf", [("finance_31", "close finalization")]),
    cq("FIN-CQ-06", "finance", "First-year TIN Matching and withholding posture", "payer account file requirement",
       "https://www.irs.gov/tax-professionals/taxpayer-identification-number-tin-matching",
       [("finance_31", "withhold_24 policy")]),
    cq("FIN-CQ-07", "finance", "AB 5 classification of clippers", "alias", "https://www.dir.ca.gov/dlse/faq_independentcontractor.htm",
       [], alias_of="CQ-01"),
    cq("FIN-CQ-08", "finance", "Days-in-US allocation for foreign clippers", "alias", "https://www.irs.gov/publications/p515", [],
       alias_of="CQ-04"),
    cq("FIN-CQ-09", "finance", "Does Civ. Code 1748.1 reach B2B invoices; what late fee survives 1671?",
       "primary text robots-blocked", "https://codes.findlaw.com/ca/civil-code/civ-sect-1748-1/",
       [("finance_31", "late fees; card acceptance")]),
    cq("FIN-CQ-10", "finance", "Does the ARL apply to small-business subscribers; which artifacts must be kept?",
       "consumer scope fact-specific",
       "https://www.cooley.com/news/insight/2025/2025-06-04-california-automatic-renewal-law-amendments-take-effect-on-july-1-2025",
       [("finance_31", "subscription/retainer invoices")]),
    cq("FIN-CQ-11", "finance", "California sales tax on deliverables and portal SaaS; nexus", "Pub 37 text unverified",
       "https://www.cdtfa.ca.gov/formspubs/pub37.pdf", [("finance_31", "invoice issuance")], related=("CQ-12",)),
    cq("FIN-CQ-12", "finance", "UCC 4A loss allocation under the bank's security-procedure agreement",
       "4A rests on a secondary source", "https://www.consumerfinance.gov/rules-policy/regulations/1005/2/",
       [("finance_31", "FC-12 stays red")]),
    cq("FIN-CQ-13", "finance", "If ZBC originates ACH: Nacha reversal rules, data security, fraud-monitoring procedure",
       "rules paywalled", "https://www.nacha.org/rules/operating-rules", [("finance_31", "direct ACH rail")]),
    cq("FIN-CQ-14", "finance", "Status of the FDIC custodial-account recordkeeping rule; FBO pass-through insurance",
       "finalization UNVERIFIED",
       "https://www.federalregister.gov/documents/2024/10/02/2024-22565/recordkeeping-for-custodial-accounts",
       [("finance_31", "FBO custody model")]),
    cq("FIN-CQ-15", "finance", "Marketplace platform-operator status (DAC7/UK/Canada)", "alias", None, [], alias_of="CQ-07"),
]

RETENTION = [
    {"class": "contracts_and_esign_evidence", "period": "UNVERIFIED",
     "basis": "UNVERIFIED (LR: life of the contract plus the limitations period)"},
    {"class": "payroll", "period": "P3Y", "basis": "https://877suemyboss.com/labor-code-1174/ (secondary paraphrase)"},
    {"class": "contractor_payments", "period": "UNVERIFIED", "basis": "UNVERIFIED"},
    {"class": "tax", "period": "UNVERIFIED", "basis": "UNVERIFIED (IRS four-year period not fetched)"},
    {"class": "takedowns", "period": "UNVERIFIED", "basis": "UNVERIFIED"},
    {"class": "matters", "period": "UNVERIFIED", "basis": "UNVERIFIED"},
    {"class": "holds", "period": "UNVERIFIED", "basis": "UNVERIFIED"},
    {"class": "legal_memos", "period": "UNVERIFIED", "basis": "UNVERIFIED"},
    {"class": "acceptance_records", "period": "UNVERIFIED", "basis": "UNVERIFIED"},
]

SIGNOFF_TOPICS = [
    {"topic": "ai_generative_fill", "scope_key": "asset_ids"},
    {"topic": "agpl_code", "scope_key": "component_ids"},
    {"topic": "reuse_bridge", "scope_key": "subject_ids"},
]

# §A.3. The four spec patterns first, then the extended list. Patterns run on the guard's normalized text
# (NFKC, case-fold, confusables folded, contractions expanded, punctuation -> spaces); each carries phrases it
# must block (G7: zero false negatives on these).
ADVICE_PATTERNS = [
    {"id": "AP-01", "regex": r"\byou (should|must|need to|are required to|have to|ought to|had better)\b",
     "examples": ["You must file a counter-notice", "you should sign this", "You have to respond by Friday",
                  "you need to keep records", "you are required to disclose", "You ought to consult nobody",
                  "you had better accept"]},
    {"id": "AP-02", "regex": r"\b(legally|under (the )?law|in my opinion|my advice|we advise|i advise|our advice)\b",
     "examples": ["Legally you are required to", "under the law this works", "In my opinion the term is fine",
                  "my advice is to wait", "we advise against it"]},
    {"id": "AP-03", "regex": r"\b(is|are|was|were|be|seems|looks) (not )?(legal|illegal|lawful|unlawful|enforceable|unenforceable|void|voidable|binding|valid|invalid)\b(?! (advice|counsel|department|review|team|hold|notice|matter))",
     "examples": ["this clause is unenforceable", "That is illegal", "the waiver is not enforceable",
                  "the contract is binding on you", "this term is void"]},
    {"id": "AP-04", "regex": r"\bthis (means|requires) (that )?you\b",
     "examples": ["This means you lose the deposit", "this requires that you notify us"]},
    {"id": "AP-05", "regex": r"\b(u|ya|yall|y all) (should|must|need to|have to)\b",
     "examples": ["u must sign", "ya need to file"]},
    {"id": "AP-06", "regex": r"\byou (are|were) (not )?(liable|obligated|entitled|protected|in breach|in violation)\b",
     "examples": ["you are liable for the damages", "you are not entitled to a refund", "you are in breach"]},
    {"id": "AP-07", "regex": r"\b(we|i) (recommend|suggest|advise) (that )?(you|u)\b",
     "examples": ["We recommend that you sign", "I suggest you file a response"]},
    {"id": "AP-08", "regex": r"\byour (legal )?(rights|obligations|liability|claim|defen[cs]e) (is|are|include|includes|would be)\b",
     "examples": ["your rights are limited here", "Your obligations include indemnity", "your claim is weak"]},
    {"id": "AP-09", "regex": r"\b(you can|you may|you cannot|you can not) (legally|lawfully|sue|be sued|win|lose)\b",
     "examples": ["you can sue them", "you cannot win this", "you may lawfully refuse"]},
    {"id": "AP-10", "regex": r"\b(fair use|infring(e|es|ing|ement)|licensed|public domain) (applies|covers|protects|does not apply)\b",
     "examples": ["fair use applies to this clip", "public domain covers the track"]},
    {"id": "AP-11", "regex": r"\b(does|do|would) (not )?(constitute|amount to) (fair use|infringement|a breach|a violation)\b",
     "examples": ["this does not constitute infringement", "posting it would amount to a breach"]},
    {"id": "AP-12", "regex": r"\b(the law|the statute|the court|a court|courts) (requires|require|says|say|allows|allow|would|will)\b",
     "examples": ["the law requires notice", "a court would enforce it", "the statute allows this"]},
    {"id": "AP-13", "regex": r"\b(don t|do not|never) (sign|accept|file|respond|reply|agree)\b",
     "examples": ["Don't sign that", "do not file a counter-notice", "never accept those terms"]},
    {"id": "AP-14", "regex": r"\b(i|we) (think|believe) (you|the (clause|contract|term|claim|notice))\b",
     "examples": ["I think you will win", "we believe the clause is weak"]},
]


def observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def federal_holidays(first: int, last: int) -> list[dict]:
    """5 U.S.C. 6103 holidays with the Saturday->Friday / Sunday->Monday observance shift (observed dates
    are the ones listed; a New Year's Day on a Saturday is observed on Dec 31 of the year before)."""
    out = []
    for y in range(first, last + 2):
        for name, d in (("new_years_day", observed(date(y, 1, 1))), ("mlk_day", nth_weekday(y, 1, 0, 3)),
                        ("washingtons_birthday", nth_weekday(y, 2, 0, 3)), ("memorial_day", last_weekday(y, 5, 0)),
                        ("juneteenth", observed(date(y, 6, 19))), ("independence_day", observed(date(y, 7, 4))),
                        ("labor_day", nth_weekday(y, 9, 0, 1)), ("columbus_day", nth_weekday(y, 10, 0, 2)),
                        ("veterans_day", observed(date(y, 11, 11))), ("thanksgiving", nth_weekday(y, 11, 3, 4)),
                        ("christmas", observed(date(y, 12, 25)))):
            if date(first, 1, 1) <= d <= date(last, 12, 31):
                out.append({"date": d.isoformat(), "name": name})
    return sorted(out, key=lambda h: h["date"])


def dump(name: str, obj) -> None:
    (OUT / name).write_bytes(json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=True).encode("ascii") + b"\n")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    dump("legal_rules_seed.json", {"version": 1, "source": "LEGAL_SPEC.md rev 1 §B.12", "rows": RULES})
    dump("documents.json", {"version": 1, "source": "LEGAL_SPEC.md rev 1 §D", "documents": DOCUMENTS,
                            "data_sheet_prefixes": list(DATA_SHEET_PREFIXES)})
    dump("counsel_questions.json", {"version": 1, "source": "LEGAL_SPEC.md rev 1 §I; FINANCE_SPEC §H",
                                    "rows": QUESTIONS})
    dump("retention.json", {"version": 1, "source": "LEGAL_SPEC.md rev 1 §B.10", "classes": RETENTION})
    dump("signoff_topics.json", {"version": 1, "source": "LEGAL_SPEC.md rev 1 §B.11", "topics": SIGNOFF_TOPICS})
    dump("advice_patterns.json", {"version": 1, "source": "LEGAL_SPEC.md rev 1 §A.3", "patterns": ADVICE_PATTERNS})
    dump("us_federal_holidays.json", {"version": 1, "source": "5 U.S.C. 6103 with observance shifts (spec choice; "
                                      "counsel to confirm the business-day definition)", "first_year": 2026,
                                      "last_year": 2030, "holidays": federal_holidays(2026, 2030)})
    import hashlib
    import re
    import sys
    pins = {}
    for p in sorted(OUT.glob("*.json")):
        pins[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
        print(p.name, pins[p.name])
    if "--pin" in sys.argv:          # rewrite the pins in src/config.py (a changed seed is a code change)
        cfg = OUT.parent / "src" / "config.py"
        text = cfg.read_text()
        for name, h in pins.items():
            text = re.sub(rf'("{re.escape(name)}": )"[0-9a-fPIN]+"', rf'\1"{h}"', text)
        cfg.write_text(text)
        print("pinned in src/config.py")


if __name__ == "__main__":
    main()
