"""Builds services/compliance-py/seed/compliance_obligations_seed.json v2 from v1.

Founder decisions of Oct 30, 2026 (service area: every inhabited US jurisdiction; client OFAC screening recorded at
activation; client legal documents current at activation; economic-nexus thresholds for Finance's readiness item).
Kept so the seed can be reproduced and reviewed (ADR 0024). From the repository root:
    git show 8d20e83:services/compliance-py/seed/compliance_obligations_seed.json > /tmp/seed_v1.json
    python3 services/compliance-py/devtools/make_seed_v2.py /tmp/seed_v1.json services/compliance-py/seed/compliance_obligations_seed.json
The output is byte-identical to the pinned seed (check: sha256sum against config.PINNED_SEED_SHA256).
"""
import json
import sys

V1, OUT = sys.argv[1], sys.argv[2]
d = json.load(open(V1, encoding="utf-8"))
assert d["seed_version"] == 1
rows = d["rows"]
by = {r["id"]: r for r in rows}

DECIDED = "2026-10-30"
STATES = ["AK", "AL", "AR", "AZ", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "IA", "ID", "IL", "IN", "KS", "KY", "LA",
          "MA", "MD", "ME", "MI", "MN", "MO", "MS", "MT", "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH", "OK",
          "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY"]
assert len(STATES) == 50
# Founder update 12:51, Oct 30, 2026: every inhabited US jurisdiction. US-UM (Minor Outlying Islands, uninhabited) is
# left out: the model does not need it.
SERVICE_AREA = [f"US-{s}" for s in STATES] + ["US-DC", "US-PR", "US-GU", "US-VI", "US-AS", "US-MP",
                                              "PR", "GU", "VI", "AS", "MP"]

# --- HR-05: the service area (the allowed set, one reviewed parameter) ------------------------------------------
hr05 = by["HR-05"]
hr05.update(
    title="Service area: clients in the US (50 states, DC and the five inhabited territories)",
    obligation=("ZBM and ZBC do business only with clients located in a US jurisdiction: the 50 states, the District "
                "of Columbia, Puerto Rico, Guam, the US Virgin Islands, American Samoa and the Northern Mariana "
                "Islands: the codes in parameters.operate, the one allowed set. A person is placed by a declared and "
                "attested ISO 3166-2 code (US-CA, US-DC; a territory as US-PR or PR, US-GU or GU, US-VI or VI, US-AS "
                "or AS, US-MP or MP); a bare US needs its state. A campaign target may also be the bare country US "
                "(parameters.target_countries). Every other code is refused (HR-07), including US-UM "
                "(uninhabited)."),
    effective_date=None,
    effective_note=("Founder service-area decision, Oct 30, 2026 (all inhabited US jurisdictions, 12:51 update); replaces the Sep 26, 2026 ruling "
                    "(US, GB, CA outside Quebec). In force from Andre's approval of this seed."),
    verified_at=DECIDED,
    parameters={"operate": SERVICE_AREA, "operate_excludes": [], "region_required": ["US"],
                "target_countries": ["US"]},
)

# --- HR-07: refuse everything else; OFAC jurisdictions named ---------------------------------------------------------
hr07 = by["HR-07"]
hr07.update(
    title="Jurisdictions: refuse everything outside the service area",
    obligation=("Refuse every jurisdiction that is not in HR-05's service area (default deny, parameters.unlisted), "
                "and in every case the OFAC comprehensively sanctioned jurisdictions (Cuba, Iran, North Korea, "
                "Crimea incl. Sevastopol, and the so-called DNR/LNR regions), listed in parameters.refuse. Canada, "
                "the United Kingdom, France, every EU member and every other country are refused."),
    effective_date=None,
    effective_note="Founder service-area decision, Oct 30, 2026; replaces the Sep 26, 2026 jurisdiction ruling.",
    verified_at=DECIDED,
    parameters={"refuse": ["CU", "IR", "KP", "UA-43", "UA-40", "UA-14", "UA-09", "UA-23", "UA-65"],
                "unlisted": "refuse"},
)

# --- rows about serving markets outside the service area: superseded, kept unchanged for history ----------------------
OUTSIDE = ["HR-06", "EU-GDPR-27", "EU-AIACT-50", "EU-EAA", "FR-LOI-2023-451", "IT-AGCOM", "ES-RD444", "ES-LABEL",
           "DE-UWG-5A", "NL-CVDM", "IE-LABEL", "UK-HIDDEN-ADS", "CA-COMPBUREAU", "CA-QC-LAW25", "CQ-05", "CQ-06",
           "CQ-14"]
NOTE = "Superseded Oct 30, 2026: outside the US service area (HR-05, founder decision). Kept for history."
for oid in OUTSIDE:
    r = by[oid]
    old = r.get("effective_note")
    r["status"] = "superseded"
    r["effective_note"] = (NOTE + (f" Was: {old}" if old else ""))[:300]

# --- new rows ------------------------------------------------------------------------------------------------------
new_rows = [
    {
        "id": "HR-14",
        "jurisdiction": "US",
        "domain": "sanctions",
        "title": "Client sanctions screening recorded before activation",
        "obligation": ("Before a client (or ZBC brand) is activated, Andre searches the client's legal name on OFAC's "
                       "Sanctions List Search (SDN and Consolidated lists) and records the search: date, list "
                       "version, lists searched, result and a hash of the saved evidence. Activation needs a "
                       "no-match record for that subject no older than parameters.max_age_days; an activation "
                       "stays current for parameters.rescreen_after_days after the search. Any potential match or "
                       "match refuses (never investigated around). Basis: US-OFAC-01 (strict liability, screen "
                       "before onboarding) and OFAC's risk-based Framework."),
        "source_url": "https://sanctionssearch.ofac.treas.gov/",
        "additional_sources": ["https://ofac.treasury.gov/faqs/topic/1501",
                               "https://ofac.treasury.gov/media/16331/download?inline"],
        "source_kind": "house-rule",
        "source_quality": "founder",
        "effective_date": DECIDED,
        "effective_note": "Founder decision, Oct 30, 2026 (business register G1).",
        "verified_at": DECIDED,
        "expires_at": None,
        "status": "verified",
        "owner_intelligence": "i08_sanctions",
        "gates": ["activation"],
        "applies_when": {"lanes": ["client", "zbc_brand"]},
        "check": "client_sanctions_attested",
        "penalty_note": "House rule (founder-locked). The underlying OFAC liability is strict (US-OFAC-01).",
        "counsel_flag": False,
        "report_ref": None,
        "parameters": {"max_age_days": 30, "rescreen_after_days": 90, "lists": ["SDN", "Consolidated"],
                       "tool": "ofac_sanctions_list_search"},
    },
    {
        "id": "HR-15",
        "jurisdiction": "ALL",
        "domain": "consumer_contracts",
        "title": "Client legal documents current at activation",
        "obligation": ("A client is activated only when Legal (37) confirms, for each document in parameters."
                       "required_doc_ids: the version the client accepted is Legal's current version (same hash), "
                       "and the acceptance is this client's, for that version and hash, with sufficient evidence. "
                       "Legal unreachable, a newer current version, or any mismatch refuses."),
        "source_url": None,
        "additional_sources": [],
        "source_kind": "house-rule",
        "source_quality": "founder",
        "effective_date": DECIDED,
        "effective_note": "Founder decision, Oct 30, 2026.",
        "verified_at": DECIDED,
        "expires_at": None,
        "status": "verified",
        "owner_intelligence": "i02_activation_gate",
        "gates": ["activation"],
        "applies_when": {"lanes": ["client"]},
        "check": "client_docs_current",
        "penalty_note": "House rule (founder-locked); no external penalty.",
        "counsel_flag": False,
        "report_ref": None,
        "parameters": {"required_doc_ids": ["client_msa"]},
    },
    {
        "id": "US-SALESTAX-NEXUS",
        "jurisdiction": "US",
        "domain": "tax",
        "title": "Economic-nexus thresholds for sales tax (monitoring, 75% alert)",
        "obligation": ("With no office, staff or property outside California, ZBM must register for another state's "
                       "or territory's sales tax only once its economic-nexus threshold is passed. Finance reports "
                       "invoiced totals per client state/territory for the trailing 12 months and the calendar year "
                       "and flags any at or above parameters.alert_ratio of its threshold (memo "
                       "MEMO-FIN-CQ-11-2026-10-30). Thresholds: parameters.default, with parameters.overrides per "
                       "code. Other states' own thresholds and measurement periods are not individually verified; Guam, the US "
                       "Virgin Islands, American Samoa and the Northern Mariana Islands have no number here "
                       "(parameters.check_before_first_client)."),
        "source_url": "https://trykintsugi.com/sales-tax-guides/usa/economic-nexus",
        "additional_sources": ["https://www.cdtfa.ca.gov/industry/wayfair.htm"],
        "source_kind": "guidance",
        "source_quality": "vendor",
        "effective_date": None,
        "effective_note": "Adopted by founder memo MEMO-FIN-CQ-11-2026-10-30; vendor table, so unverified.",
        "verified_at": None,
        "expires_at": None,
        "status": "unverified",
        "owner_intelligence": "i05_control_monitor",
        "gates": ["control"],
        "applies_when": {},
        "check": "threshold_counter",
        "penalty_note": "Unregistered sales tax owed with interest and penalties once a threshold is passed.",
        "counsel_flag": False,
        "report_ref": "MEMO-FIN-CQ-11-2026-10-30",
        "parameters": {
            "alert_ratio": "0.75",
            "periods": ["trailing_12_months", "calendar_year"],
            "default": {"sales_usd": "100000.00", "transactions": None},
            "overrides": {
                "US-CA": {"sales_usd": "500000.00", "transactions": None,
                          "source": "https://www.cdtfa.ca.gov/industry/wayfair.htm"},
                "US-HI": {"sales_usd": "100000.00", "transactions": 200,
                          "source": "https://trykintsugi.com/sales-tax-guides/usa/economic-nexus"},
                "US-NV": {"sales_usd": "100000.00", "transactions": 200,
                          "source": "https://trykintsugi.com/sales-tax-guides/usa/economic-nexus"},
                "PR": {"sales_usd": "100000.00", "transactions": 200,
                       "source": "https://trykintsugi.com/sales-tax-guides/usa/economic-nexus"},
            },
            "default_source": "https://trykintsugi.com/sales-tax-guides/usa/economic-nexus",
            # no verified threshold source for these territories: never a number; Finance reports them as a check
            "check_before_first_client": {
                "GU": "local gross receipts/business tax (Guam business privilege tax) - check before first client",
                "VI": "local gross receipts/business tax - check before first client",
                "AS": "local gross receipts/business tax - check before first client",
                "MP": "local gross receipts/business tax - check before first client",
            },
        },
    },
]
for r in new_rows:
    assert r["id"] not in by
rows.extend(new_rows)

d["seed_version"] = 2
d["generated_on"] = DECIDED
d["source_report"] = (d["source_report"] + "; amended by the founder decisions of Oct 30, 2026 (service area: the 50 states, DC "
                      "and the five inhabited US territories, client sanctions screening and legal documents at activation, economic-nexus "
                      "thresholds); seed v1 SHA-256 4e3821d018a42be76ede1bf501004f0a8c5590327fa98ddd9f8c3f16845f584d")
c = d["counts"]
c["rows"] = len(rows)
c["verified"] = sum(1 for r in rows if r["status"] == "verified")
c["unverified"] = sum(1 for r in rows if r["status"] == "unverified")
c["superseded"] = sum(1 for r in rows if r["status"] == "superseded")
c["house_rules"] = sum(1 for r in rows if r["source_kind"] == "house-rule")
c["counsel"] = sum(1 for r in rows if r["source_kind"] == "counsel-question")
with open(OUT, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(c)
