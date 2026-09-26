"""
Intelligence 4 — Campaign Enrolment (spec §C.4, CN-04, CN-06, CN-12..CN-14,
CN-17, CN-19, CN-23).

Eligible only when ALL hold; every item cites CN-12 unless noted. Never
enrols past a cap or into an unsigned kit. Runs every check (no
short-circuit); each input is a recorded port answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from clock import parse_iso
from intelligences.common import item, unavailable
from intelligences.i03_tiering import rank
from ports import ComplianceRuling, ConnectionsAnswer, DocVersionAnswer, JurisdictionAnswer, KitAnswer, RateCardAnswer, \
    RulebookAnswer

NUMBER, NAME, ACTOR = 4, "Campaign Enrolment", "intel_04_enrolment"


def covered(code: str, allowed: list[str]) -> bool:
    """Prefix rule (spec §B.4): ``US`` covers ``US-CA``; ``US-CA`` covers only itself."""
    return any(code == a or code.startswith(a + "-") for a in allowed)


@dataclass
class Inputs:
    clipper: dict
    config: Optional[dict]
    now: datetime
    suspended: Optional[dict]
    jurisdiction: Optional[JurisdictionAnswer]
    connections: ConnectionsAnswer
    legal: DocVersionAnswer
    acceptance: Optional[dict]
    rulebook: RulebookAnswer
    kit: KitAnswer
    brand_activation: ComplianceRuling
    rate_card: RateCardAnswer
    active_enrolments: int
    tier_cap: int
    campaign_clippers: int
    counsel_blocks: list[str]          # open counsel rows whose block applies (rule ids)
    already_enrolled: bool


def evaluate(x: Inputs) -> list[dict]:
    out: list[dict] = []
    c, cfg = x.clipper, x.config
    if c["status"] != "active":
        out.append(item("CLIPPER_NOT_ACTIVE", "CN-12", f"clipper status is {c['status']}, not active"))
    if x.suspended:
        out.append(item("SUSPENDED", "CN-19", f"suspended ({x.suspended['kind']}) until {x.suspended['until'] or 'review'}",
                        "clipper_network", x.suspended.get("strike_id")))
    if x.already_enrolled:
        out.append(item("ALREADY_ENROLLED", "CN-12", "the clipper already has an active enrolment in this campaign"))
    if cfg is None:
        out.append(item("NO_NETWORK_CONFIG", "CN-12", "the campaign has no network config from Andre"))
        return out
    if not (parse_iso(cfg["opens_at"]) <= x.now < parse_iso(cfg["closes_at"])):
        out.append(item("CAMPAIGN_NOT_OPEN", "CN-12", "the campaign is not open for enrolment now"))
    if rank(c.get("tier") or "T0") < rank(cfg["min_tier"]):
        out.append(item("TIER_TOO_LOW", "CN-12", f"tier {c.get('tier')} is below the campaign's min_tier {cfg['min_tier']}"))
    code = c.get("declared_region") or c["declared_country"]
    if not covered(code, cfg["clipper_jurisdictions"]) and not covered(c["declared_country"], cfg["clipper_jurisdictions"]):
        out.append(item("JURISDICTION_NOT_IN_CAMPAIGN", "CN-12", f"declared jurisdiction {code} is not in the campaign's "
                        "clipper_jurisdictions"))
    if x.jurisdiction is None or not x.jurisdiction.available:
        out.append(unavailable("compliance_38", "CN-12", "fresh jurisdiction resolve"))
    elif x.jurisdiction.jurisdiction_class not in ("operate", "conditional"):
        out.append(item("JURISDICTION_NOT_IN_CAMPAIGN", "CN-12", f"Compliance resolves the clipper's jurisdiction as "
                        f"{x.jurisdiction.jurisdiction_class}", "compliance_38", x.jurisdiction.resolution_id))
    if not x.connections.available:
        out.append(unavailable("verification_integrity", "CN-06", "platform connections"))
    elif not any(k.status == "active" and k.platform in cfg["platforms"] for k in x.connections.connections):
        out.append(item("NO_ACTIVE_CONNECTION", "CN-06", "no active V&I connection on one of the campaign's platforms "
                        f"({', '.join(cfg['platforms'])})", "verification_integrity"))
    if not x.legal.available:
        out.append(unavailable("legal_37", "CN-04", "current Clipper Agreement version"))
    elif x.acceptance is None or x.acceptance.get("version") != x.legal.version \
            or x.acceptance.get("doc_sha256") != x.legal.doc_sha256:
        out.append(item("AGREEMENT_NOT_CURRENT", "CN-04", f"the current Clipper Agreement version ({x.legal.version}) "
                        "must be accepted before the next enrolment", "legal_37"))
    if not x.rulebook.available:
        out.append(unavailable("creative_production", "CN-14", "live rulebook"))
    elif x.rulebook.live_version is None:
        out.append(item("RULEBOOK_NOT_LIVE", "CN-14", "Creative has no live rulebook for this campaign",
                        "creative_production"))
    if not x.kit.available:
        out.append(item("KIT_UNAVAILABLE", "CN-14", "Creative's campaign kit could not be read", "creative_production"))
    elif x.kit.status != "signed" or (x.rulebook.available and x.kit.rulebook_version != x.rulebook.live_version):
        out.append(item("KIT_NOT_SIGNED", "CN-14", "the kit is not Andre-signed for the live rulebook version",
                        "creative_production", x.kit.kit_id))
    if not x.brand_activation.available:
        out.append(unavailable("compliance_38", "CN-12", "the campaign's latest zbc_brand activation"))
    elif not x.brand_activation.allowed:
        out.append(item("CAMPAIGN_NOT_ACTIVATED", "CN-12", "Compliance's latest zbc_brand activation of this campaign "
                        "is not allowed", "compliance_38", x.brand_activation.ruling_id))
    if not x.rate_card.available:
        out.append(unavailable("finance_31", "CN-17", "rate card"))
    elif not x.rate_card.published or (x.rate_card.doc_id, x.rate_card.version, x.rate_card.sha256) != \
            (cfg["rate_card_ref"]["finance_doc_id"], cfg["rate_card_ref"]["version"], cfg["rate_card_ref"]["sha256"]):
        out.append(item("RATE_CARD_NOT_PUBLISHED", "CN-17", "Finance has not published the rate card this config "
                        "version names", "finance_31"))
    if x.active_enrolments >= x.tier_cap:
        out.append(item("CAP_CLIPPER_ENROLMENTS", "CN-13", f"{x.active_enrolments} active enrolments: the tier cap is "
                        f"{x.tier_cap}"))
    if x.campaign_clippers >= cfg["max_clippers"]:
        out.append(item("CAP_CAMPAIGN_FULL", "CN-13", f"the campaign has reached max_clippers ({cfg['max_clippers']})"))
    for cq in x.counsel_blocks:
        out.append(item("COUNSEL_HOLD", cq, f"open counsel question {cq} blocks this enrolment until Andre approves a memo "
                        "(CN-23)", "clipper_network", cq))
    return out
