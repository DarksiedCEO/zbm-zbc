"""
Intelligence 2 — Activation Gate (spec A.1, C.1, C.2).

allowed/blocked + unmet for a client (``client``), a ZBC campaign
(``zbc_brand``, subject_id = campaign id) or a clipper (``zbc_creator``).
Builds the evaluation context from the lane's facts; the shared engine runs
the rows. Never activates anything itself; never accepts a caller's
``age_verified: true`` (no such fact exists: 422) in place of the V&I
attestation.

Flags per lane (spec B.2 "flags are never defaulted"): the seven activation
flags are REQUIRED facts in every lane; the ``claims`` booleans are required
in the client lanes. Flags that do not exist in a lane are fixed, not
defaulted, and the table below is the whole list (tests assert every flag a
row can name is covered): creators make no claims (claims are the
campaign's), and client lanes have no payee (Compliance screens payees
only; screening clients is an open item, spec §I).
"""

from __future__ import annotations

from facts import ACTIVATION_FLAGS, ACTIVATION_SCHEMAS, EU5, get, has, validate
from intelligences.engine import Ctx, resolution_unmet
from intelligences.i07_jurisdiction import Params, resolve_person, resolve_target

NUMBER, NAME, ACTOR = 2, "Activation Gate", "intel_02_activation_gate"

LANE_FIXED_FLAGS = {
    "client": {"entity_payee": False, "foreign_payee": False},
    "zbc_brand": {"entity_payee": False, "foreign_payee": False},
    "zbc_creator": {"claims_present": False, "health_or_earnings_claim": False},
}

_COMMON_REQUIRED = ("jurisdiction", *(f"flags.{f}" for f in ACTIVATION_FLAGS))
_CLIENT_REQUIRED = ("target_jurisdictions", "platforms", "client_category", "claims.claims_present",
                    "claims.health_or_earnings_claim", "services_include_review_suppression",
                    "outbound_cold_contact_countries")
_BRAND_REQUIRED = ("pay_basis", "sentiment_conditions")
_CREATOR_REQUIRED = ("age.verification_attestation_id", "age.method", "age.dob_field_neutral", "recruitment_channel",
                     "payee_type", "sanctions_screen_id", "tax_form_kind", "rail_kyc.status", "rail_kyc.rail",
                     "creator_agreement_version", "disclosure_training_attested", "accounts",
                     "accounts_complete_attested")


def required(lane: str, f: dict) -> list[str]:
    req = list(_COMMON_REQUIRED)
    if lane in ("client", "zbc_brand"):
        req += _CLIENT_REQUIRED
        if lane == "zbc_brand":
            req += _BRAND_REQUIRED
        if get(f, "flags.health_data_shared") is True:
            req.append("hbnr_clause_signed")
        if any(t.split("-", 1)[0] in EU5 for t in f.get("target_jurisdictions") or []):
            req += ["eu_kit_version_acknowledged", "msa_eu_clause"]
    else:
        req += _CREATOR_REQUIRED
        if f.get("payee_type") == "entity":
            req.append("owner_screen_ids")
        if get(f, "jurisdiction.declared_country") == "ES":
            req.append("es_special_relevance")
        if get(f, "jurisdiction.declared_country") in EU5:
            req.append("eu_kit_version_acknowledged")
    return req


def build(lane: str, subject_id: str, raw_facts: dict, rows: dict, fallback: dict, env, now) -> tuple[Ctx, dict]:
    """Returns (context, side-effect hints). Unknown fact keys raise Invalid (422)."""
    f, wrong = validate(ACTIVATION_SCHEMAS[lane], raw_facts)
    params = Params.from_rows(rows or fallback)
    ctx = Ctx(gate="activation", subject_id=subject_id, lane=lane, facts=f, rows=rows, fallback_rows=fallback,
              params=params, env=env, now=now)
    for w in wrong:
        ctx.pre.append(ctx.missing("HR-03", w))
    for key in required(lane, f):
        if not has(f, key):
            ctx.pre.append(ctx.missing("HR-03", key))

    subj = resolve_person(f.get("jurisdiction"), params, "subject")
    resolutions = [subj]
    if lane in ("client", "zbc_brand"):
        for t in f.get("target_jurisdictions") or []:
            resolutions.append(resolve_target(t, params))
        ctx.platforms = set(f.get("platforms") or [])
    else:
        ctx.platforms = {a.get("platform") for a in (f.get("accounts") or []) if isinstance(a, dict)}
    ctx.resolutions = resolutions
    for r in resolutions:
        ctx.pre.extend(resolution_unmet(ctx, r))
    ctx.jurisdictions = {r.code for r in resolutions if r.code}
    ctx.audience = {r.code for r in resolutions[1:] if r.code}

    flags = {k: v for k, v in (f.get("flags") or {}).items()}
    claims = f.get("claims") or {}
    if lane in ("client", "zbc_brand"):
        for k in ("claims_present", "health_or_earnings_claim"):
            if k in claims:
                flags[k] = claims[k]
    else:
        if "payee_type" in f:
            flags["entity_payee"] = f["payee_type"] == "entity"
        country = get(f, "jurisdiction.declared_country")
        if country:
            flags["foreign_payee"] = country != "US"
        if "es_special_relevance" in f:
            flags["es_special_relevance"] = f["es_special_relevance"]
    flags.update(LANE_FIXED_FLAGS[lane])
    ctx.flags = flags

    hints = {}
    signal = f.get("network_country_signal")
    declared = get(f, "jurisdiction.declared_country")
    if signal and declared and signal != declared:
        hints["signal_mismatch"] = {"declared": declared, "signal": signal}
    return ctx, hints


def profile(lane: str, f: dict, owner_subject_ids: list[str]) -> dict:
    """What a later payout/publish needs from an ALLOWED activation (stored locally, never on the ledger)."""
    if lane == "zbc_creator":
        return {"jurisdiction": f.get("jurisdiction"), "flags": f.get("flags"), "payee_type": f.get("payee_type"),
                "age_attestation_id": get(f, "age.verification_attestation_id"), "accounts": f.get("accounts"),
                "accounts_complete_attested": f.get("accounts_complete_attested"),
                "owner_subject_ids": owner_subject_ids, "eu_kit_version_acknowledged": f.get("eu_kit_version_acknowledged"),
                "es_special_relevance": f.get("es_special_relevance")}
    return {"jurisdiction": f.get("jurisdiction"), "target_jurisdictions": f.get("target_jurisdictions"),
            "platforms": f.get("platforms"), "client_category": f.get("client_category"), "claims": f.get("claims"),
            "flags": f.get("flags"), "pay_basis": f.get("pay_basis"), "sentiment_conditions": f.get("sentiment_conditions"),
            "eu_kit_version_acknowledged": f.get("eu_kit_version_acknowledged"), "msa_eu_clause": f.get("msa_eu_clause")}
