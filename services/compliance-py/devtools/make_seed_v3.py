"""Builds services/compliance-py/seed/compliance_obligations_seed.json v3 from v2.

Founder ask of Oct 31, 2026 (ADR 0027 decision 4; MSA 1.5(b), (c), (e)): the client activation gate screens the
client's disclosed owners alongside the client, holds activation on any owner match or on a missing ownership
attestation for an entity, applies OFAC's 50 Percent Rule (aggregate, direct or indirect), and re-screens owners with
the client every 90 days. HR-14 says so, with two new reviewed parameters. Kept so the seed can be reproduced and
reviewed. From the repository root:
    git show 6da61211:services/compliance-py/seed/compliance_obligations_seed.json > /tmp/seed_v2.json
    python3 services/compliance-py/devtools/make_seed_v3.py /tmp/seed_v2.json services/compliance-py/seed/compliance_obligations_seed.json
The output is byte-identical to the pinned seed (check: sha256sum against config.PINNED_SEED_SHA256).
"""
import hashlib
import json
import sys

V2, OUT = sys.argv[1], sys.argv[2]
raw = open(V2, "rb").read()
d = json.loads(raw)
assert d["seed_version"] == 2
by = {r["id"]: r for r in d["rows"]}

hr14 = by["HR-14"]
hr14.update(
    title="Client and owner sanctions screening recorded before activation",
    obligation=("Before a client (or ZBC brand) is activated, Andre searches the client's legal name on OFAC's Sanctions "
                "List Search (SDN and Consolidated lists) and records the search: date, list version, lists searched, "
                "result and a hash of the saved evidence. He records who owns the client (every owner it discloses, at "
                "least those holding parameters.owner_disclosure_bp basis points or more, directly or indirectly, and "
                "the owners of such an entity owner), the client's attestation of that disclosure (required for an "
                "entity), and the same search for each owner. Activation needs a no-match record for the client and "
                "every owner, no older than parameters.max_age_days; it stays current for "
                "parameters.rescreen_after_days after the oldest search (owners are re-screened with the client). Any "
                "potential match or match, of the client or an owner, refuses and holds the client permanently. An "
                "entity owned parameters.aggregate_block_bp basis points or more in the aggregate, directly or "
                "indirectly, by blocked persons is itself blocked (OFAC 50 Percent Rule). Basis: US-OFAC-01 and OFAC's "
                "Revised Guidance on Entities Owned by Blocked Persons (Aug 13, 2014)."),
    effective_note="Founder decision, Oct 30, 2026 (business register G1); owners and the 50 Percent Rule added Oct 31, "
                   "2026 (ADR 0027).",
)
hr14["additional_sources"] = list(hr14["additional_sources"]) + ["https://ofac.treasury.gov/media/6186/download",
                                                                  "https://ofac.treasury.gov/faqs/401"]
hr14["parameters"] = {**hr14["parameters"], "owner_disclosure_bp": 2500, "aggregate_block_bp": 5000}

d["seed_version"] = 3
d["generated_on"] = "2026-10-31"
d["source_report"] = (d["source_report"] + "; HR-14 extended to the client's disclosed owners and OFAC's 50 Percent Rule "
                      "(ADR 0027, Oct 31, 2026); seed v2 SHA-256 " + hashlib.sha256(raw).hexdigest())
with open(OUT, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(hashlib.sha256(open(OUT, "rb").read()).hexdigest())
