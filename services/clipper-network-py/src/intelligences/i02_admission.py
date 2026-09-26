"""
Intelligence 2 — Admission (spec §C.2, CN-01..CN-07, CN-12, CN-19).

Decides admitted / not admitted + every unmet item. ALL checks run every
time (no short-circuit); every input is a recorded port answer, never a
caller's boolean (a tick box "I am 18+" never counts, CN-01). An unavailable
dependency is ``DEPENDENCY_UNAVAILABLE:<port>`` citing the rule it feeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from intelligences.common import item, unavailable
from ports import (AgeAnswer, ComplianceRuling, ConnectionsAnswer, DocVersionAnswer, IdentityAnswer, IntegrityAnswer,
                   JurisdictionAnswer, TaxAnswer)

NUMBER, NAME, ACTOR = 2, "Admission", "intel_02_admission"
REGION_REQUIRED = ("US", "CA", "UA")   # Compliance C.1 / HR-05 region_required
VI, CMP, FIN, LEG = "verification_integrity", "compliance_38", "finance_31", "legal_37"


@dataclass
class Inputs:
    clipper: dict
    application: Optional[dict]
    jurisdiction: JurisdictionAnswer
    age: AgeAnswer
    local_duplicate_of: Optional[str]
    identity: IdentityAnswer
    connections: ConnectionsAnswer
    requires_connection: bool
    enabled_platforms: tuple[str, ...]
    legal: DocVersionAnswer
    acceptance: Optional[dict]
    training: Optional[dict]
    tax: TaxAnswer
    activation: ComplianceRuling
    latest_activation: ComplianceRuling
    integrity: IntegrityAnswer


def evaluate(x: Inputs) -> list[dict]:
    out: list[dict] = []
    c = x.clipper
    # 1. application complete (CN-12)
    if x.application is None:
        out.append(item("FACT_MISSING", "CN-12", "no open application for this clipper"))
    else:
        if not c.get("jurisdiction_attested"):
            out.append(item("FACT_MISSING", "CN-12", "jurisdiction not attested on the application"))
        if c.get("declared_country") in REGION_REQUIRED and not c.get("declared_region"):
            out.append(item("FACT_MISSING", "CN-12", f"declared_region is required for {c.get('declared_country')}"))
        if not isinstance(x.application.get("sag_aftra_member"), bool):
            out.append(item("FACT_MISSING", "CN-12", "sag_aftra_member not answered"))
    # 2. jurisdiction (Compliance resolve; never inferred from IP)
    if not x.jurisdiction.available:
        out.append(unavailable(CMP, "CN-02", "jurisdiction resolve"))
    elif x.jurisdiction.jurisdiction_class not in ("operate", "conditional"):
        out.append(item("JURISDICTION_REFUSED", "CN-02", f"Compliance resolves the declared jurisdiction as "
                        f"{x.jurisdiction.jurisdiction_class}", CMP, x.jurisdiction.resolution_id))
    # 3. age (V&I latest attestation; the tick box never counts)
    if not x.age.available:
        out.append(unavailable(VI, "CN-01", "age attestation"))
    elif x.age.status != "adult":
        out.append(item("AGE_NOT_ADULT", "CN-01", "V&I has no adult attestation for this clipper"
                        + (" (V&I attests a minor: refused, no re-application)" if x.age.status == "minor" else ""),
                        VI, x.age.attestation_id))
    # 4. duplicate identity (CN's own email check + V&I)
    if x.local_duplicate_of:
        out.append(item("DUPLICATE_IDENTITY", "CN-03", "another clipper identity already holds this email", "clipper_network",
                        x.local_duplicate_of))
    if not x.identity.available:
        out.append(unavailable(VI, "CN-03", "duplicate-identity check"))
    elif x.identity.status == "duplicate":
        out.append(item("DUPLICATE_IDENTITY", "CN-03", "V&I found a duplicate identity (a human decides)", VI,
                        (x.identity.finding_ids or (None,))[0]))
    elif x.identity.status != "clear":
        out.append(item("IDENTITY_CHECK_INCOMPLETE", "CN-03", "V&I duplicate-identity check is incomplete: not clear", VI))
    # 5. >= 1 active connection on an enabled platform
    if x.requires_connection:
        if not x.connections.available:
            out.append(unavailable(VI, "CN-06", "platform connections"))
        elif not any(k.status == "active" and k.platform in x.enabled_platforms for k in x.connections.connections):
            out.append(item("NO_ACTIVE_CONNECTION", "CN-06", "no active V&I connection on an enabled platform "
                            f"({', '.join(x.enabled_platforms)})", VI))
    # 6. current agreement accepted
    if not x.legal.available:
        out.append(unavailable(LEG, "CN-04", "current Clipper Agreement version"))
    elif x.acceptance is None or x.acceptance.get("version") != x.legal.version \
            or x.acceptance.get("doc_sha256") != x.legal.doc_sha256:
        out.append(item("AGREEMENT_NOT_CURRENT", "CN-04", f"the current Clipper Agreement version ({x.legal.version}) "
                        "is not accepted", LEG, (x.acceptance or {}).get("acceptance_id")))
    # 7. disclosure training
    if x.training is None:
        out.append(item("TRAINING_MISSING", "CN-07", "disclosure training not attested"))
    # 8. tax form on file (Finance; status only)
    if not x.tax.available:
        out.append(unavailable(FIN, "CN-05", "tax form status"))
    elif not x.tax.form_on_file:
        out.append(item("TAX_FORM_MISSING", "CN-05", "Finance has no W-9 or W-8 on file", FIN))
    # 9. Compliance zbc_creator activation: this ruling and the latest read must agree and be allowed
    if not x.activation.available:
        out.append(unavailable(CMP, "CN-02", "zbc_creator activation ruling"))
    elif not x.activation.allowed:
        for line in x.activation.unmet_lines or ("activation blocked (no reason given)",):
            out.append(item("COMPLIANCE_BLOCKED", "CN-02", line, CMP, x.activation.ruling_id))
    if x.activation.available and x.activation.allowed:
        if not x.latest_activation.available:
            out.append(unavailable(CMP, "CN-02", "latest zbc_creator activation read"))
        elif not x.latest_activation.allowed or x.latest_activation.ruling_id != x.activation.ruling_id:
            out.append(item("COMPLIANCE_BLOCKED", "CN-02", "Compliance's LATEST zbc_creator activation ruling is not the "
                            "allowed one just issued", CMP, x.latest_activation.ruling_id))
    # 10. no open V&I S3 / hold on the identity
    if not x.integrity.available:
        out.append(unavailable(VI, "CN-19", "clipper integrity"))
    elif not x.integrity.clear:
        out.append(item("INTEGRITY_HOLD", "CN-19", "V&I reports an open S3 or a hold on this identity", VI))
    return out


def compliance_facts(clipper: dict, application: dict, acceptance: Optional[dict], training: Optional[dict],
                     accounts: list[dict]) -> dict:
    """The zbc_creator C.1 facts Clipper Network HOLDS; every other fact is left out so Compliance names it
    missing (spec §C.2). No DOB, tax, name or IP ever leaves CN here."""
    channel = {"inbound_form": "inbound", "referral": "referral", "discord_server_post": "inbound",
               "email_opt_in": "inbound"}[application["channel"]]
    facts = {"jurisdiction": {"declared_country": clipper["declared_country"],
                              "declared_region": clipper.get("declared_region"),
                              "attested": bool(clipper.get("jurisdiction_attested")),
                              "attestation_ref": application["application_id"]},
             "network_country_signal": None, "recruitment_channel": channel,
             "disclosure_training_attested": training is not None}
    if acceptance is not None:
        facts["creator_agreement_version"] = acceptance["version"]
    if accounts:
        facts["accounts"] = accounts
    return facts
