"""
The gate algorithm shared by intelligences 2, 3 and 4 (spec C.2 steps 4-9)
and the deterministic checks of spec C.10.

Each check is a pure function of the evaluation context (``Ctx``): the
validated facts, the register version in force, stored records and the port
answers (each port call recorded on the ledger first, by the service's
``PortCalls``). A check returns unmet items; it never raises past the
engine for a missing fact (it reports ``fact_missing:<key>``).

No row on the page, no block; no pass without every applicable row
verified and its check met.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from clock import parse_iso
from facts import AGE_METHODS, EU5, get, has
from intelligences import i08_sanctions, i09_disclosure, i10_accessibility
from intelligences.i07_jurisdiction import Params, Resolution
from register import effective_status, is_effective

MESSAGE_MAX = 200
LINE_MAX = 400

# EU5 country -> the register row that holds its local ad label (HR-06: "local
# ad label per country (its register row must be verified)").
LOCAL_LABEL_ROWS = {"IT": "IT-AGCOM", "DE": "DE-UWG-5A", "NL": "NL-CVDM", "ES": "ES-LABEL", "IE": "IE-LABEL"}

# Prohibited categories per platform row (spec C.1: "mapping in code as a
# table keyed by platform row"), from the rows' verified obligation text.
PROHIBITED: dict[str, frozenset[str]] = {
    "PLT-TT-02": frozenset({"adult", "alcohol", "tobacco_vaping", "drugs_cannabis", "gambling", "financial_schemes",
                            "pharmaceuticals", "weight_loss", "political_social", "weapons", "counterfeit"}),
    "PLT-X-02": frozenset({"adult", "alcohol", "contraceptives", "dating", "drugs_cannabis", "political_social",
                           "supplements", "pharmaceuticals", "tobacco_vaping", "weapons", "weight_loss"}),
}
# PLT-X-02: "AU/EU/UK limits on financial products, crypto, gambling" -> refused
# when any target is AU, GB or an EU member we serve.
PROHIBITED_REGIONAL = {"PLT-X-02": (frozenset({"financial_products", "crypto", "gambling"}),
                                    frozenset({"AU", "GB", *EU5}))}


@dataclass
class Ctx:
    gate: str                               # activation | payout | publish
    subject_id: str
    lane: Optional[str] = None
    subject_kind: Optional[str] = None
    asset_type: Optional[str] = None
    facts: dict = field(default_factory=dict)
    platforms: Optional[set] = None
    jurisdictions: set = field(default_factory=set)
    audience: set = field(default_factory=set)
    flags: dict = field(default_factory=dict)
    resolutions: list = field(default_factory=list)
    rows: dict = field(default_factory=dict)             # version rows by id
    fallback_rows: dict = field(default_factory=dict)    # for citing rows when no version is in force
    params: Optional[Params] = None
    env: Any = None                                      # service hooks (ports, stores, config, clock)
    now: Optional[datetime] = None
    # payout / publish: stored profiles Compliance supplies itself
    clipper: Optional[dict] = None
    campaign: Optional[dict] = None
    client: Optional[dict] = None
    pre: list = field(default_factory=list)              # unmet items found while building the context
    expired_rows: list = field(default_factory=list)
    evaluated: list = field(default_factory=list)

    @property
    def today(self) -> date:
        return self.now.date()

    def row(self, oid: str) -> Optional[dict]:
        return self.rows.get(oid) or self.fallback_rows.get(oid)

    def u(self, oid: str, code: str, message: str) -> dict:
        row = self.row(oid) or {}
        status = effective_status(row, self.today) if row else "unknown"
        return {"code": code, "obligation_id": oid, "obligation_title": row.get("title", oid),
                "source_url": row.get("source_url"), "row_status": status, "message": message[:MESSAGE_MAX]}

    def missing(self, oid: str, key: str, why: str = "") -> dict:
        return self.u(oid, f"fact_missing:{key}", why or f"required fact '{key}' is missing or has the wrong type")

    def param(self, oid: str, key: str, default=None):
        return (self.row(oid) or {}).get("parameters", {}).get(key, default)


Unmet = dict
Check = Callable[[dict, Ctx], list]


# --- helpers ---------------------------------------------------------------------

def _paid(ctx: Ctx) -> Optional[bool]:
    return ctx.flags.get("paid_or_endorsement")


def _disclosure(ctx: Ctx) -> dict:
    d = ctx.facts.get("disclosure")
    return d if isinstance(d, dict) else {}


def _need(ctx: Ctx, row: dict, *keys: str) -> list:
    return [ctx.missing(row["id"], k) for k in keys if not has(ctx.facts, k)]


def _row_country(row: dict) -> Optional[str]:
    j = (row.get("applies_when") or {}).get("jurisdictions") or []
    return j[0].split("-", 1)[0] if j else None


def _eu_kit_source(ctx: Ctx) -> Optional[dict]:
    """Where the EU-kit facts live for this gate: the lane's own facts at
    activation, the campaign activation at payout, the client activation at publish."""
    if ctx.gate == "activation":
        return ctx.facts
    if ctx.gate == "payout":
        return ctx.campaign
    return ctx.client


def _category_source(ctx: Ctx) -> Optional[dict]:
    if ctx.gate == "activation":
        return ctx.facts
    return ctx.campaign if ctx.gate == "payout" else ctx.client


# --- checks (spec C.10) -------------------------------------------------------------

def c_engine_invariant(row, ctx):
    return []


def c_jurisdiction_class(row, ctx):
    out = []
    for r in ctx.resolutions:
        if r.cls == "refuse" and row["id"] in r.cites:
            out.append(ctx.u(row["id"], "jurisdiction_class", f"{r.who} jurisdiction {r.code} refused: {r.reason}"))
    return out


def c_eu_kit(row, ctx):
    out = []
    eu = sorted({c.split("-", 1)[0] for c in ctx.jurisdictions if c.split("-", 1)[0] in EU5})
    if not eu:
        return out
    src = _eu_kit_source(ctx)
    want = ctx.param("HR-06", "eu_kit_version")
    if src is None:
        out.append(ctx.missing(row["id"], "eu_kit_version_acknowledged", "no activation record carrying the EU kit acknowledgment"))
    else:
        ack = src.get("eu_kit_version_acknowledged")
        if ack is None:
            out.append(ctx.missing(row["id"], "eu_kit_version_acknowledged"))
        elif ack != want:
            out.append(ctx.u(row["id"], "eu_kit", f"EU kit version {ack} acknowledged; current is {want}"))
        client_lane = (ctx.gate == "activation" and ctx.lane in ("client", "zbc_brand")) or ctx.gate in ("payout", "publish")
        if client_lane:
            msa = src.get("msa_eu_clause")
            if msa is None:
                out.append(ctx.missing(row["id"], "msa_eu_clause"))
            elif msa is not True:
                out.append(ctx.u(row["id"], "eu_kit", "MSA EU clause (EAA, cookie and GDPR duties) not in place"))
    for c in eu:
        lid = LOCAL_LABEL_ROWS.get(c)
        lrow = ctx.rows.get(lid) if lid else None
        if lrow is None:
            out.append(ctx.u(row["id"], "eu_kit", f"no local label row for {c}; EU kit incomplete"))
        elif effective_status(lrow, ctx.today) != "verified":
            out.append(ctx.u(lid, "rule_not_in_force",
                             f"{c} local ad label rule is {effective_status(lrow, ctx.today)}; EU kit incomplete, blocked"))
    return out


def c_no_cold_outbound_ca(row, ctx):
    f = ctx.facts
    if ctx.gate == "activation":
        if ctx.lane in ("client", "zbc_brand"):
            if not has(f, "outbound_cold_contact_countries"):
                return [ctx.missing(row["id"], "outbound_cold_contact_countries")]
            if "CA" in f["outbound_cold_contact_countries"]:
                return [ctx.u(row["id"], "no_cold_outbound_ca", "cold outbound contact to Canada is not allowed")]
            return []
        country = get(f, "jurisdiction.declared_country")
        if country == "CA":
            if not has(f, "recruitment_channel"):
                return [ctx.missing(row["id"], "recruitment_channel")]
            if f["recruitment_channel"] == "outbound_cold":
                return [ctx.u(row["id"], "no_cold_outbound_ca", "Canadian creators are recruited inbound only")]
        return []
    if ctx.gate == "publish" and ctx.asset_type in ("email_campaign", "sms_campaign", "autodialed_call", "dm_campaign"):
        if not has(f, "recipient_countries"):
            return [ctx.missing(row["id"], "recipient_countries")]
        if "CA" not in f["recipient_countries"]:
            return []
        if ctx.asset_type in ("sms_campaign", "autodialed_call"):
            if get(f, "sms.consent_artifacts_complete") is not True:
                return [ctx.u(row["id"], "no_cold_outbound_ca", "Canadian recipients without evidenced consent")]
            return []
        if not has(f, "recipients_cold"):
            return [ctx.missing(row["id"], "recipients_cold")]
        if f["recipients_cold"] is not False:
            return [ctx.u(row["id"], "no_cold_outbound_ca", "cold outbound to Canadian recipients is not allowed")]
    return []


def c_age_18_plus(row, ctx):
    if ctx.gate == "activation":
        att = get(ctx.facts, "age.verification_attestation_id")
    else:
        att = (ctx.clipper or {}).get("age_attestation_id")
    if not att:
        return [ctx.missing(row["id"], "age.verification_attestation_id")]
    a = ctx.env.ports.vi_age(att)
    if not a.available:
        return [ctx.u(row["id"], "dependency_unavailable:verification_integrity",
                      "Verification and Integrity cannot attest age: not allowed yet")]
    if a.status == "minor":
        return [ctx.u(row["id"], "age_18_plus", "under 18: hard block (no guardian path)")]
    if a.status != "adult" or a.attestation_id != att:
        return [ctx.u(row["id"], "age_18_plus", "18+ not confirmed by Verification and Integrity for this attestation")]
    return []


def c_age_method(row, ctx):
    out = _need(ctx, row, "age.method", "age.dob_field_neutral")
    if out:
        return out
    if get(ctx.facts, "age.method") not in AGE_METHODS:
        out.append(ctx.u(row["id"], "age_method_highly_effective", "age check method is not highly effective"))
    if get(ctx.facts, "age.dob_field_neutral") is not True:
        out.append(ctx.u(row["id"], "age_method_highly_effective", "DOB field is not neutral"))
    return out


def _fresh(ctx, screen):
    e = ctx.env
    return i08_sanctions.freshness_problem(screen, ctx.now, e.config.sanctions_freshness_days, e.sanctions_list_version())


def c_sanctions_clear_fresh(row, ctx):
    e = ctx.env
    if ctx.gate == "activation":
        sid = ctx.facts.get("sanctions_screen_id")
        if not sid:
            return [ctx.missing(row["id"], "sanctions_screen_id")]
        screen = e.screen(sid)
        if screen is not None and (screen["subject_id"] != ctx.subject_id or screen["role"] != "payee"):
            return [ctx.u(row["id"], "sanctions_clear_fresh", "sanctions screen is not for this payee")]
    else:
        clipper_id = ctx.facts.get("clipper_id")
        screen = e.latest_screen(clipper_id, "payee", None) if clipper_id else None
    problem = _fresh(ctx, screen)
    return [ctx.u(row["id"], "sanctions_clear_fresh", problem)] if problem else []


def c_sanctions_owners(row, ctx):
    e = ctx.env
    if ctx.gate == "activation":
        ids = ctx.facts.get("owner_screen_ids")
        if not ids:
            return [ctx.missing(row["id"], "owner_screen_ids", "entity payee: owners (>= 25%) must be screened")]
        out = []
        for sid in ids:
            s = e.screen(sid)
            if s is not None and (s["role"] != "owner" or s.get("owner_of") != ctx.subject_id):
                out.append(ctx.u(row["id"], "sanctions_owners", "owner screen is not an owner screen for this payee"))
                continue
            p = _fresh(ctx, s)
            if p:
                out.append(ctx.u(row["id"], "sanctions_owners", f"owner: {p}"))
        return out
    clipper_id = ctx.facts.get("clipper_id")
    owners = (ctx.clipper or {}).get("owner_subject_ids") or []
    if not owners:
        return [ctx.u(row["id"], "sanctions_owners", "entity payee has no screened owners on record")]
    out = []
    for oid in owners:
        p = _fresh(ctx, e.latest_screen(oid, "owner", clipper_id))
        if p:
            out.append(ctx.u(row["id"], "sanctions_owners", f"owner: {p}"))
    return out


def _payee(ctx):
    return ctx.subject_id if ctx.gate == "activation" else ctx.facts.get("clipper_id")


def _tax(row, ctx, field_name: Optional[str], problem: str, code: str):
    payee = _payee(ctx)
    if not payee:
        return [ctx.missing(row["id"], "clipper_id")]
    t = ctx.env.ports.fin_tax(payee)
    if not t.available:
        return [ctx.u(row["id"], "dependency_unavailable:finance_31", "Finance (31) cannot confirm tax status: not allowed yet")]
    if not t.form_on_file:
        return [ctx.u(row["id"], code, "no tax form on file with Finance (31)")]
    if ctx.gate == "activation" and code == "tax_form_on_file" and t.form_kind != ctx.facts.get("tax_form_kind"):
        return [ctx.u(row["id"], code, "Finance (31) holds a different tax form kind than declared")]
    if field_name and getattr(t, field_name) is not True:
        return [ctx.u(row["id"], code, problem)]
    return []


def c_tax_form_on_file(row, ctx):
    return _tax(row, ctx, None, "", "tax_form_on_file")


def c_tin_match(row, ctx):
    return _tax(row, ctx, "tin_match", "TIN match not confirmed by Finance (31)", "tin_match")


def c_w8_current(row, ctx):
    return _tax(row, ctx, "w8_current", "W-8 not current per Finance (31)", "w8_current")


def c_foreign_services(row, ctx):
    return _tax(row, ctx, "services_outside_us_attested", "services-outside-US attestation not confirmed",
                "foreign_services_attestation")


def c_rail_kyc(row, ctx):
    if ctx.gate == "activation":
        if not has(ctx.facts, "rail_kyc.status"):
            return [ctx.missing(row["id"], "rail_kyc.status")]
        if get(ctx.facts, "rail_kyc.status") != "verified":
            return [ctx.u(row["id"], "rail_kyc_verified", "payout rail KYC not verified")]
        return []
    payee = _payee(ctx)
    r = ctx.env.ports.fin_rail(payee)
    if not r.available:
        return [ctx.u(row["id"], "dependency_unavailable:finance_31", "Finance (31) cannot confirm rail KYC: not allowed yet")]
    if r.status != "verified" or not r.payouts_enabled:
        return [ctx.u(row["id"], "rail_kyc_verified", "payout rail KYC not verified or payouts not enabled")]
    return []


def c_training(row, ctx):
    if not has(ctx.facts, "disclosure_training_attested"):
        return [ctx.missing(row["id"], "disclosure_training_attested")]
    if ctx.facts["disclosure_training_attested"] is not True:
        return [ctx.u(row["id"], "disclosure_training_attested", "disclosure training not attested")]
    return []


def c_accounts(row, ctx):
    if ctx.gate == "activation" and ctx.lane != "zbc_creator":
        return []  # posting accounts are the clipper's (A.1: zbc_creator lane); clients post nothing themselves
    if ctx.gate == "activation":
        src = ctx.facts
    else:
        src = ctx.clipper or {}
    accounts = src.get("accounts")
    if accounts is None or "accounts_complete_attested" not in src:
        return [ctx.missing(row["id"], "accounts")]
    if not accounts or src.get("accounts_complete_attested") is not True:
        return [ctx.u(row["id"], "creator_accounts_disclosed", "posting accounts not all disclosed and attested")]
    if ctx.gate == "payout" and ctx.facts.get("platform") not in {a.get("platform") for a in accounts}:
        return [ctx.u(row["id"], "creator_accounts_disclosed", "clip posted from a platform with no disclosed account")]
    return []


def c_no_sentiment_pay(row, ctx):
    src = ctx.facts if ctx.gate == "activation" else (ctx.campaign or {})
    if "pay_basis" not in src or "sentiment_conditions" not in src:
        return [ctx.missing(row["id"], "pay_basis")]
    if src["pay_basis"] != "verified_views" or src["sentiment_conditions"] is not False:
        return [ctx.u(row["id"], "no_sentiment_pay", "pay must be for verified views only, never conditioned on sentiment")]
    return []


def c_no_review_suppression(row, ctx):
    if not has(ctx.facts, "services_include_review_suppression"):
        return [ctx.missing(row["id"], "services_include_review_suppression")]
    if ctx.facts["services_include_review_suppression"] is not False:
        return [ctx.u(row["id"], "no_review_suppression", "review-suppression services are not offered")]
    return []


def c_pooled_funds_false(row, ctx):
    v = ctx.flags.get("pooled_client_funds")
    if v is None:
        return [ctx.missing(row["id"], "flags.pooled_client_funds")]
    return [ctx.u(row["id"], "pooled_funds_false", "pooled client funds (Model B) are not allowed")] if v else []


def c_claims_substantiated(row, ctx):
    if ctx.gate == "activation":
        fid, ok = get(ctx.facts, "claims.claim_file_id"), get(ctx.facts, "claims.claim_file_approved")
        keys = ("claims.claim_file_id", "claims.claim_file_approved")
    else:
        fid, ok = ctx.facts.get("claim_file_id"), ctx.facts.get("claim_file_approved")
        keys = ("claim_file_id", "claim_file_approved")
    miss = _need(ctx, row, *keys)
    if miss:
        return miss
    if not fid or ok is not True:
        return [ctx.u(row["id"], "claims_substantiated", "claims present without an approved claim file")]
    return []


def c_hbnr(row, ctx):
    if not has(ctx.facts, "hbnr_clause_signed"):
        return [ctx.missing(row["id"], "hbnr_clause_signed")]
    return [] if ctx.facts["hbnr_clause_signed"] is True else [ctx.u(row["id"], "hbnr_clause", "HBNR clause not signed")]


def c_prohibited_category(row, ctx):
    if ctx.gate == "activation" and ctx.lane == "zbc_creator":
        return []  # creators have no client category; the campaign's is checked at brand activation and payout
    src = _category_source(ctx)
    cat = (src or {}).get("client_category")
    if cat is None:
        return [ctx.missing(row["id"], "client_category")]
    banned = PROHIBITED.get(row["id"])
    if banned is None:
        return [ctx.u(row["id"], "prohibited_category", "no category table for this platform row: blocked")]
    if cat in banned:
        return [ctx.u(row["id"], "prohibited_category", f"category '{cat}' is prohibited on this platform")]
    regional = PROHIBITED_REGIONAL.get(row["id"])
    if regional and cat in regional[0]:
        targets = {c.split("-", 1)[0] for c in ((src or {}).get("target_jurisdictions") or [])} | \
                  {c.split("-", 1)[0] for c in ctx.jurisdictions}
        if targets & regional[1]:
            return [ctx.u(row["id"], "prohibited_category", f"category '{cat}' is limited for AU/EU/UK targets")]
    return []


def c_verified_views(row, ctx):
    f = ctx.facts
    miss = _need(ctx, row, "post_ref", "platform", "posted_at")
    if miss:
        return miss
    lag = ctx.param("HR-13", "settlement_lag_days", 14)
    if not isinstance(lag, int) or isinstance(lag, bool) or lag < 1:
        return [ctx.u(row["id"], "verified_views_attested", "settlement lag parameter invalid: blocked")]
    if ctx.now < parse_iso(f["posted_at"]) + timedelta(days=lag):
        return [ctx.u(row["id"], "verified_views_attested", f"settlement lag of {lag} days after posting has not elapsed")]
    a = ctx.env.ports.vi_clip(ctx.subject_id, f["post_ref"], f["platform"], f["posted_at"], lag)
    if not a.available:
        return [ctx.u(row["id"], "dependency_unavailable:verification_integrity",
                      "Verification and Integrity cannot attest verified views: not allowed yet")]
    if not (a.verified_views and a.anomaly_screen_passed and not a.purchased_engagement and a.still_live_at_minimum_period):
        return [ctx.u(row["id"], "verified_views_attested", "views not attested as verified under HR-13")]
    return []


def c_disclosure_present(row, ctx):
    paid = _paid(ctx)
    if paid is None:
        return [ctx.missing(row["id"], "flags.paid_or_endorsement")]
    if not paid:
        return []
    if "disclosure" not in ctx.facts:
        return [ctx.missing(row["id"], "disclosure")]
    label = _disclosure(ctx).get("in_video_label_text")
    if not label or not label.strip():
        return [ctx.u(row["id"], "disclosure_present", "paid content carries no disclosure")]
    return []


def c_platform_toggle(row, ctx):
    paid = _paid(ctx)
    if paid is None:
        return [ctx.missing(row["id"], "flags.paid_or_endorsement")]
    if not paid:
        return []
    if not _disclosure(ctx).get("platform_toggle_evidence_ref"):
        return [ctx.u(row["id"], "platform_toggle", "platform paid-partnership / commercial-content toggle not evidenced")]
    return []


def c_in_video_label(row, ctx):
    if not _paid(ctx):
        return [] if _paid(ctx) is False else [ctx.missing(row["id"], "flags.paid_or_endorsement")]
    d = _disclosure(ctx)
    miss = [ctx.missing(row["id"], f"disclosure.{k}") for k in ("in_video_label_start_s", "voice_present")
            if k not in d]
    if miss:
        return miss
    out = []
    t = i09_disclosure.timing_problem(d, ctx.env.config.disclosure_max_offset_s)
    if t:
        out.append(ctx.u(row["id"], "in_video_label", t))
    a = i09_disclosure.audio_problem(d)
    if a:
        out.append(ctx.u(row["id"], "in_video_label", a))
    return out


def c_label_vocabulary(row, ctx):
    if not _paid(ctx):
        return [] if _paid(ctx) is False else [ctx.missing(row["id"], "flags.paid_or_endorsement")]
    p = i09_disclosure.label_problem(_disclosure(ctx).get("in_video_label_text"),
                                    row.get("parameters", {}).get("accepted", []),
                                    row.get("parameters", {}).get("rejected", []))
    return [ctx.u(row["id"], "label_vocabulary", p)] if p else []


def c_local_label(row, ctx):
    country = _row_country(row)
    labels = (ctx.param("HR-06", "local_labels", {}) or {}).get(country or "", [])
    if ctx.gate == "activation":
        src = _eu_kit_source(ctx) or {}
        if src.get("eu_kit_version_acknowledged") != ctx.param("HR-06", "eu_kit_version"):
            return [ctx.u(row["id"], "local_label", f"{country} local label duty needs the current EU kit acknowledged")]
        return []
    if not _paid(ctx):
        return [] if _paid(ctx) is False else [ctx.missing(row["id"], "flags.paid_or_endorsement")]
    if not labels:
        return [ctx.u(row["id"], "local_label", f"no local label configured for {country}: blocked")]
    if not i09_disclosure.local_label_present(_disclosure(ctx).get("in_video_label_text"), labels):
        return [ctx.u(row["id"], "local_label", f"{country} audience: label must include one of {', '.join(labels)}")]
    return []


def c_ai_disclosure_synthetic(row, ctx):
    # The C.1 fact schema has no field that evidences an AI/synthetic
    # disclosure, so nothing can satisfy this check yet (ADR 0006, gap list).
    return [ctx.u(row["id"], "ai_disclosure_synthetic",
                  "synthetic or manipulated media: no AI disclosure evidence field exists yet; blocked")]


def c_likeness_consent(row, ctx):
    if "consent_document_id" not in ctx.facts:
        return [ctx.missing(row["id"], "consent_document_id")]
    if not ctx.facts["consent_document_id"]:
        return [ctx.u(row["id"], "likeness_consent", "real-person likeness or implied affiliation without a consent document")]
    return []


def c_personal_use(row, ctx):
    if "personal_use_attested" not in ctx.facts:
        return [ctx.missing(row["id"], "personal_use_attested")]
    return [] if ctx.facts["personal_use_attested"] is True else \
        [ctx.u(row["id"], "personal_use_attested", "personal-use claim not attested")]


def c_music_source(row, ctx):
    m = ctx.facts.get("music")
    if not isinstance(m, dict) or "source" not in m:
        return [ctx.missing(row["id"], "music.source")]
    if m["source"] not in ("commercial_library", "licensed") or not m.get("track_or_license_id"):
        return [ctx.u(row["id"], "music_source_declared", "music present without a commercial-library or licensed source id")]
    return []


def c_rights(row, ctx):
    if "rights_clearance_id" not in ctx.facts:
        return [ctx.missing(row["id"], "rights_clearance_id")]
    if not ctx.facts["rights_clearance_id"]:
        return [ctx.u(row["id"], "rights_cleared_no_strike", "no rights clearance id")]
    f = ctx.facts
    if not all(k in f for k in ("post_ref", "platform", "posted_at")):
        return [ctx.missing(row["id"], "post_ref")]
    lag = ctx.param("HR-13", "settlement_lag_days", 14)
    a = ctx.env.ports.vi_clip(ctx.subject_id, f["post_ref"], f["platform"], f["posted_at"], lag)
    if not a.available:
        return [ctx.u(row["id"], "dependency_unavailable:verification_integrity",
                      "Verification and Integrity cannot report copyright strikes: not allowed yet")]
    if a.copyright_strike:
        return [ctx.u(row["id"], "rights_cleared_no_strike", "copyright strike reported on the clip")]
    return []


def c_accessibility(row, ctx):
    p = i10_accessibility.a11y_problem(ctx.env.a11y_results(ctx.facts.get("asset_content_sha256")),
                                       ctx.facts.get("asset_content_sha256"), ctx.asset_type, ctx.now,
                                       ctx.env.config.a11y_max_age_days)
    return [ctx.u(row["id"], "accessibility_pass", p)] if p else []


def c_consent_banner(row, ctx):
    keys = ("tracking_disclosed", "consent_banner_present", "consent_before_nonessential", "opt_out_present")
    miss = _need(ctx, row, *(f"tracking.{k}" for k in keys))
    if miss:
        return miss
    bad = [k for k in keys if get(ctx.facts, f"tracking.{k}") is not True]
    return [ctx.u(row["id"], "consent_banner", f"tracking without: {', '.join(bad)}")] if bad else []


def c_required_docs(row, ctx):
    docs = ctx.facts.get("docs")
    miss = _need(ctx, row, "docs.privacy_policy", "docs.terms", "docs.forms")
    if miss:
        return miss
    out = []
    for d in [docs["privacy_policy"], docs["terms"], *docs["forms"]]:
        if "doc_id" not in d or "version" not in d:  # (the schema already reports incomplete docs)
            return [ctx.missing(row["id"], "docs")]
        v = ctx.env.ports.legal_doc(d["doc_id"])
        if not v.available:
            return [ctx.u(row["id"], "dependency_unavailable:legal_37",
                          "Legal (37) cannot confirm the current document versions: not allowed yet")]
        if v.current_version != d["version"]:
            out.append(ctx.u(row["id"], "required_docs_current", f"document {d['doc_id']} is not at the current version"))
    return out


def _all_true(row, ctx, prefix, keys, code, extra=None):
    miss = _need(ctx, row, *(f"{prefix}.{k}" if prefix else k for k in keys))
    if miss:
        return miss
    bad = [k for k in keys if get(ctx.facts, f"{prefix}.{k}" if prefix else k) is not True]
    out = [ctx.u(row["id"], code, f"not met: {', '.join(bad)}")] if bad else []
    if extra:
        out += extra()
    return out


def c_canspam(row, ctx):
    def limits():
        n = get(ctx.facts, "canspam.opt_out_honor_business_days")
        if n is None:
            return [ctx.missing(row["id"], "canspam.opt_out_honor_business_days")]
        return [ctx.u(row["id"], "canspam_elements", "opt-out must be honored within 10 business days")] if n > 10 else []
    return _all_true(row, ctx, "canspam", ("ad_identified", "postal_address_present", "opt_out_mechanism_present",
                                             "sender_vendor_monitored"), "canspam_elements", limits)


def c_sms(row, ctx):
    def limits():
        out = []
        qh, mx = get(ctx.facts, "sms.quiet_hours_local"), get(ctx.facts, "sms.max_per_24h")
        if qh is None or mx is None:
            return [ctx.missing(row["id"], "sms.quiet_hours_local" if qh is None else "sms.max_per_24h")]
        if qh != "08:00-20:00":
            out.append(ctx.u(row["id"], "sms_consent_artifact", "messages only 08:00-20:00 recipient local time"))
        if mx > 3:
            out.append(ctx.u(row["id"], "sms_consent_artifact", "at most 3 messages per 24 hours"))
        return out
    return _all_true(row, ctx, "sms", ("consent_artifacts_complete", "opt_out_immediate"), "sms_consent_artifact", limits)


def c_subscription(row, ctx):
    return _all_true(row, ctx, "subscription", ("terms_before_consent", "affirmative_unchecked_consent",
                                                  "online_cancel_as_easy"), "subscription_terms")


def c_chatbot(row, ctx):
    return _all_true(row, ctx, "", ("genai_disclosed_at_outset",), "chatbot_disclosure")


def c_flag_blocks(row, ctx):
    return [ctx.u(row["id"], "flag_blocks", "flagged content is held pending counsel")]


def c_counsel_memo(row, ctx):
    return [ctx.u(row["id"], "counsel_memo", "counsel-only question: stays red until an approved counsel memo replaces it")]


def c_control_only(row, ctx):
    return [ctx.u(row["id"], row["check"], "control-only check reached a gate: blocked")]


CHECKS: dict[str, Check] = {
    "engine_invariant": c_engine_invariant, "jurisdiction_class": c_jurisdiction_class, "eu_kit": c_eu_kit,
    "no_cold_outbound_ca": c_no_cold_outbound_ca, "age_18_plus": c_age_18_plus,
    "age_method_highly_effective": c_age_method, "sanctions_clear_fresh": c_sanctions_clear_fresh,
    "sanctions_owners": c_sanctions_owners, "tax_form_on_file": c_tax_form_on_file, "tin_match": c_tin_match,
    "w8_current": c_w8_current, "foreign_services_attestation": c_foreign_services, "rail_kyc_verified": c_rail_kyc,
    "disclosure_training_attested": c_training, "creator_accounts_disclosed": c_accounts,
    "no_sentiment_pay": c_no_sentiment_pay, "no_review_suppression": c_no_review_suppression,
    "pooled_funds_false": c_pooled_funds_false, "claims_substantiated": c_claims_substantiated,
    "hbnr_clause": c_hbnr, "prohibited_category": c_prohibited_category,
    "verified_views_attested": c_verified_views, "disclosure_present": c_disclosure_present,
    "platform_toggle": c_platform_toggle, "in_video_label": c_in_video_label, "label_vocabulary": c_label_vocabulary,
    "local_label": c_local_label, "ai_disclosure_synthetic": c_ai_disclosure_synthetic,
    "likeness_consent": c_likeness_consent, "personal_use_attested": c_personal_use,
    "music_source_declared": c_music_source, "rights_cleared_no_strike": c_rights,
    "accessibility_pass": c_accessibility, "consent_banner": c_consent_banner,
    "required_docs_current": c_required_docs, "canspam_elements": c_canspam, "sms_consent_artifact": c_sms,
    "subscription_terms": c_subscription, "chatbot_disclosure": c_chatbot, "flag_blocks": c_flag_blocks,
    "counsel_memo": c_counsel_memo, "control_sla": c_control_only, "threshold_counter": c_control_only,
    "effective_date_reminder": c_control_only,
}


# --- applicability (spec B.2) ---------------------------------------------------------

def _juris_match(entries: list[str], codes: set) -> bool:
    return any(c == e or c.startswith(e + "-") for e in entries for c in codes)


def base_match(aw: dict, ctx: Ctx) -> bool:
    """All non-flag keys, ANDed; a dimension the context does not carry does not restrict."""
    if aw.get("lanes") is not None and ctx.lane is not None and ctx.lane not in aw["lanes"]:
        return False
    if aw.get("subject_kinds") is not None and ctx.subject_kind is not None and ctx.subject_kind not in aw["subject_kinds"]:
        return False
    if aw.get("asset_types") is not None and ctx.asset_type is not None and ctx.asset_type not in aw["asset_types"]:
        return False
    if aw.get("platforms") is not None and ctx.platforms is not None and not (set(aw["platforms"]) & ctx.platforms):
        return False
    if aw.get("jurisdictions") is not None and not _juris_match(aw["jurisdictions"], ctx.jurisdictions):
        return False
    return True


def evaluate(ctx: Ctx) -> list[Unmet]:
    """Spec C.2 steps 4-6 (required facts are in ``ctx.pre``). Controls and holds are added by the caller."""
    out: list[Unmet] = list(ctx.pre)
    for oid in sorted(ctx.rows):
        row = ctx.rows[oid]
        if ctx.gate not in row["gates"] or row["status"] == "superseded" or not is_effective(row, ctx.today):
            continue
        aw = row.get("applies_when") or {}
        if not base_match(aw, ctx):
            continue
        named = list(aw.get("flags_any") or []) + list(aw.get("flags_none") or [])
        missing = [f for f in named if not isinstance(ctx.flags.get(f), bool)]
        if missing:
            for f in missing:
                out.append(ctx.missing("HR-03", f"flags.{f}"))
                out.append(ctx.missing(oid, f"flags.{f}"))
            continue
        if aw.get("flags_any") and not any(ctx.flags[f] for f in aw["flags_any"]):
            continue
        if aw.get("flags_none") and any(ctx.flags[f] for f in aw["flags_none"]):
            continue
        ctx.evaluated.append(oid)
        st = effective_status(row, ctx.today)
        if st != "verified":
            if st == "expired":
                ctx.expired_rows.append(oid)
                msg = f"rule expired on {row['expires_at']}; blocks {ctx.gate} until re-verified and approved"
            else:
                msg = f"rule is {st}; blocks {ctx.gate} until verified and approved"
            out.append(ctx.u(oid, "rule_not_in_force", msg))
            continue
        out.extend(CHECKS[row["check"]](row, ctx))
    return out


def finalize(items: list[Unmet]) -> list[Unmet]:
    """Deduplicate by (obligation_id, code), keep the first message; sort by id then code."""
    seen: dict[tuple, Unmet] = {}
    for it in items:
        seen.setdefault((it["obligation_id"], it["code"]), it)
    return [seen[k] for k in sorted(seen)]


def unmet_line(it: Unmet) -> str:
    line = (f"compliance_38/{it['obligation_id']}/{it['code']}: {it['message']} "
            f"[{it['source_url'] or 'no source url'}]")
    return line[:LINE_MAX]


def resolution_unmet(ctx: Ctx, r: Resolution) -> list[Unmet]:
    if r.cls != "missing":
        return []
    out = []
    for key in r.missing:
        out.append(ctx.missing("HR-03", key))
        for oid in r.cites:
            out.append(ctx.missing(oid, key))
    return out


__all__ = ["CHECKS", "Ctx", "evaluate", "finalize", "unmet_line", "base_match", "resolution_unmet",
           "LOCAL_LABEL_ROWS", "PROHIBITED"]
