"""
Gate facts (spec C.1): schema and validation.

The rules the spec sets, exactly:
- unknown keys at any level -> 422 (strict; e.g. ``age_verified``,
  ``sanctions_clear``, a guardian field — none of them exist);
- a fact that is missing, or present with the wrong type/format, is an
  unmet ``fact_missing:<dotted key>`` — the gate blocks and names it
  (``validate`` drops such a value and reports it);
- all free text is bounded; control characters and over-long values are a
  422 (malformed input, never evaluated). Nothing here interprets text.

``validate(schema, facts)`` returns ``(clean, wrong)``: the values that
passed their type check, and the dotted keys present with the wrong type.
Which keys are REQUIRED is decided per gate/lane by the gate modules
(conditional requirements depend on other facts).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Optional

from clock import parse_iso
from errors import Invalid
from textguard import has_control_chars

ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
ISO2_RE = re.compile(r"[A-Z]{2}")
REGION_RE = re.compile(r"[A-Z]{2}-[A-Z0-9]{1,3}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
SLUG_RE = re.compile(r"[a-z0-9_]{1,40}")
HOURS_RE = re.compile(r"[0-2][0-9]:[0-5][0-9]-[0-2][0-9]:[0-5][0-9]")

PLATFORMS = ("youtube", "tiktok", "x", "snapchat", "instagram", "facebook", "twitch", "web", "email", "sms")
CATEGORIES = ("general", "adult", "alcohol", "tobacco_vaping", "drugs_cannabis", "gambling", "financial_schemes",
              "crypto", "financial_products", "pharmaceuticals", "supplements", "weight_loss", "contraceptives",
              "dating", "political_social", "weapons", "counterfeit", "cosmetic_medical")
AGE_METHODS = ("open_banking", "photo_id_match", "facial_age_estimation", "mobile_operator", "credit_card",
               "digital_identity", "email_age_estimation")
ASSET_TYPES = ("ad_video", "ad_static", "site", "landing_page", "form", "portal", "email_campaign", "sms_campaign",
               "autodialed_call", "dm_campaign", "subscription_checkout", "chatbot")
SITE_TYPES = ("site", "landing_page", "form", "portal")
EU5 = ("DE", "NL", "IE", "ES", "IT")

MAX_TEXT = 512
MAX_LIST = 60


@dataclass(frozen=True)
class F:
    kind: str                       # bool id iso2 region code enum int number sha256 datetime text slug hours list obj
    values: tuple = ()
    lo: float = 0
    hi: float = 0
    item: Optional["F"] = None
    fields: Optional[dict] = None
    nullable: bool = False
    max_len: int = MAX_TEXT
    max_items: int = MAX_LIST
    complete: bool = False          # obj: every field must be present (list items, documents)
    min_items: int = 0              # list: fewer items is a 422 (AEGIS N15-5: no vacuous "all of nothing")


def B() -> F:
    return F("bool")


def ID(nullable=False) -> F:
    return F("id", nullable=nullable)


def E(*values, nullable=False) -> F:
    return F("enum", values=values, nullable=nullable)


def L(item: F, max_items: int = MAX_LIST, min_items: int = 0) -> F:
    return F("list", item=item, max_items=max_items, min_items=min_items)


def O(**fields) -> F:
    return F("obj", fields=fields)


def OC(**fields) -> F:
    """An object whose every field is required (an incomplete one is the wrong type)."""
    return F("obj", fields=fields, complete=True)


def _type_ok(spec: F, v: Any) -> bool:
    k = spec.kind
    if k == "bool":
        return isinstance(v, bool)
    if k in ("int",):
        return isinstance(v, int) and not isinstance(v, bool) and spec.lo <= v <= spec.hi
    if k == "number":
        return (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                and spec.lo <= v <= spec.hi)
    if not isinstance(v, str):
        return False
    if k == "id":
        return bool(ID_RE.fullmatch(v))
    if k == "iso2":
        return bool(ISO2_RE.fullmatch(v))
    if k == "region":
        return bool(REGION_RE.fullmatch(v))
    if k == "code":
        return bool(ISO2_RE.fullmatch(v) or REGION_RE.fullmatch(v))
    if k == "enum":
        return v in spec.values
    if k == "sha256":
        return bool(SHA_RE.fullmatch(v))
    if k == "slug":
        return bool(SLUG_RE.fullmatch(v))
    if k == "hours":
        return bool(HOURS_RE.fullmatch(v))
    if k == "datetime":
        try:
            parse_iso(v)
            return True
        except ValueError:
            return False
    if k == "text":
        return True
    raise AssertionError(k)


def _bounded(v: Any, spec: F, path: str) -> None:
    """Malformed-input rules that are a 422 whatever the type: strings too
    long or with control characters, lists too long."""
    if isinstance(v, str):
        if len(v) > spec.max_len:
            raise Invalid(f"facts.{path}: longer than {spec.max_len} characters")
        if has_control_chars(v):
            raise Invalid(f"facts.{path}: control characters are not accepted")
    if isinstance(v, list) and spec.kind == "list" and len(v) > spec.max_items:
        raise Invalid(f"facts.{path}: more than {spec.max_items} items")
    if isinstance(v, list) and spec.kind == "list" and len(v) < spec.min_items:
        # AEGIS N15-5: an empty target/platform/account list would make every rule scoped to it not apply
        raise Invalid(f"facts.{path}: must list at least {spec.min_items} item(s); an empty list is refused "
                      "(every rule scoped to it would silently not apply)")


def _walk(spec: F, v: Any, path: str, wrong: list[str]) -> tuple[bool, Any]:
    if v is None:
        if spec.nullable:
            return True, None
        wrong.append(path)
        return False, None
    if spec.kind == "obj":
        if not isinstance(v, dict):
            _bounded(v, F("text"), path)
            wrong.append(path)
            return False, None
        out = {}
        for key in v:
            if not isinstance(key, str) or key not in spec.fields:
                shown = key if isinstance(key, str) and ID_RE.fullmatch(key) and len(key) <= 64 else "<key>"
                raise Invalid(f"facts.{path + '.' if path else ''}{shown}: unknown fact (strict schema)")
        for key, sub in spec.fields.items():
            if key in v:
                ok, val = _walk(sub, v[key], f"{path}.{key}" if path else key, wrong)
                if ok:
                    out[key] = val
        if spec.complete and set(out) != set(spec.fields):
            wrong.append(path)
            return False, None
        return True, out
    if spec.kind == "list":
        if not isinstance(v, list):
            _bounded(v, F("text"), path)
            wrong.append(path)
            return False, None
        _bounded(v, spec, path)
        items = []
        bad = False
        for i, item in enumerate(v):
            ok, val = _walk(spec.item, item, f"{path}[{i}]", wrong)
            bad = bad or not ok
            items.append(val)
        if bad:
            wrong.append(path)
            return False, None
        return True, items
    _bounded(v, spec, path)
    if _type_ok(spec, v):
        return True, v
    wrong.append(path)
    return False, None


def validate(schema: F, facts: Any) -> tuple[dict, list[str]]:
    if not isinstance(facts, dict):
        raise Invalid("facts must be a JSON object")
    wrong: list[str] = []
    _, clean = _walk(schema, facts, "", wrong)
    return clean, sorted(set(w for w in wrong if not re.search(r"\[\d+\]", w)))


def get(d: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def has(d: dict, dotted: str) -> bool:
    sentinel = object()
    return get(d, dotted, sentinel) is not sentinel


# --- schemas -------------------------------------------------------------------------

JURISDICTION = O(declared_country=F("iso2"), declared_region=F("region", nullable=True), attested=B(),
                 attestation_ref=ID())
ACTIVATION_FLAGS = ("political_content", "child_directed", "health_data_shared", "biz_opp_client",
                    "audience_data_sale", "marketplace", "pooled_client_funds")
CLAIMS = O(claims_present=B(), health_or_earnings_claim=B(), claim_file_id=ID(nullable=True), claim_file_approved=B())

_COMMON = dict(jurisdiction=JURISDICTION, network_country_signal=F("iso2", nullable=True),
               flags=O(**{f: B() for f in ACTIVATION_FLAGS}))
_CLIENT = dict(_COMMON, target_jurisdictions=L(F("code"), min_items=1), platforms=L(E(*PLATFORMS), 10, min_items=1),
               client_category=E(*CATEGORIES), claims=CLAIMS, services_include_review_suppression=B(),
               outbound_cold_contact_countries=L(F("iso2")), hbnr_clause_signed=B(),
               eu_kit_version_acknowledged=F("int", lo=0, hi=10_000), msa_eu_clause=B())
_BRAND = dict(_CLIENT, pay_basis=E("verified_views", "raw_views", "engagement", "sentiment", "flat_fee", "other"),
              sentiment_conditions=B())
_CREATOR = dict(_COMMON,
                age=O(verification_attestation_id=ID(), method=E(*AGE_METHODS), dob_field_neutral=B()),
                recruitment_channel=E("inbound", "referral", "outbound_cold"),
                payee_type=E("individual", "entity"), sanctions_screen_id=ID(), owner_screen_ids=L(ID(), 20),
                tax_form_kind=E("w9", "w8ben", "w8bene"),
                rail_kyc=O(status=E("verified", "pending", "restricted", "unverified"), rail=F("slug")),
                creator_agreement_version=ID(), disclosure_training_attested=B(),
                accounts=L(OC(platform=E(*PLATFORMS), handle_sha256=F("sha256")), 20, min_items=1),
                accounts_complete_attested=B(), es_special_relevance=B(),
                eu_kit_version_acknowledged=F("int", lo=0, hi=10_000))

ACTIVATION_SCHEMAS = {"client": O(**_CLIENT), "zbc_brand": O(**_BRAND), "zbc_creator": O(**_CREATOR)}

DISCLOSURE = O(platform_toggle_evidence_ref=ID(nullable=True), in_video_label_text=F("text", max_len=200),
               in_video_label_start_s=F("number", lo=0, hi=86_400), voice_present=B(), audio_disclosure_present=B())
MUSIC = O(present=B(), source=E("commercial_library", "licensed", "none"), track_or_license_id=ID(nullable=True))
CLIP_FLAGS = ("synthetic_performer", "ai_manipulated_media", "real_person_likeness", "implied_affiliation",
              "personal_use_claim")
PAYOUT_SCHEMA = O(campaign_id=ID(), rulebook_version=F("int", lo=1, hi=1_000_000), post_ref=F("text", max_len=512),
                  clipper_id=ID(), posted_at=F("datetime"), clip_review=F("slug"), platform=E(*PLATFORMS),
                  disclosure=DISCLOSURE, music=MUSIC, rights_clearance_id=ID(nullable=True),
                  flags=O(**{f: B() for f in CLIP_FLAGS}), consent_document_id=ID(nullable=True),
                  personal_use_attested=B())

PUBLISH_FLAGS = ("paid_or_endorsement", "synthetic_performer", "ai_manipulated_media", "real_person_likeness",
                 "implied_affiliation", "personal_use_claim", "claims_present", "health_or_earnings_claim",
                 "child_directed", "child_access_likely", "political_content", "audience_data_sale", "uses_tracking",
                 "collects_pii", "consumer_ecommerce")
DOC = OC(doc_id=ID(), version=ID())
PUBLISH_SCHEMA = O(
    brief_id=ID(), asset_type=E(*ASSET_TYPES), asset_content_sha256=F("sha256"), client_id=ID(),
    target_jurisdictions=L(F("code"), min_items=1), platforms=L(E(*PLATFORMS), 10, min_items=1),
    flags=O(**{f: B() for f in PUBLISH_FLAGS}),
    claim_file_id=ID(nullable=True), claim_file_approved=B(), disclosure=DISCLOSURE, music=MUSIC,
    consent_document_id=ID(nullable=True), personal_use_attested=B(),
    docs=O(privacy_policy=DOC, terms=DOC, forms=L(DOC, 20)),
    tracking=O(tracking_disclosed=B(), consent_banner_present=B(), consent_before_nonessential=B(),
               opt_out_present=B()),
    canspam=O(ad_identified=B(), postal_address_present=B(), opt_out_mechanism_present=B(),
              opt_out_honor_business_days=F("int", lo=0, hi=365), sender_vendor_monitored=B()),
    recipient_countries=L(F("iso2")), recipients_cold=B(),
    sms=O(consent_artifacts_complete=B(), quiet_hours_local=F("hours"), max_per_24h=F("int", lo=0, hi=10_000),
          opt_out_immediate=B()),
    subscription=O(terms_before_consent=B(), affirmative_unchecked_consent=B(), online_cancel_as_easy=B()),
    genai_disclosed_at_outset=B(),
)
