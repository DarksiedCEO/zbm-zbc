"""
Intelligence 4 — Publish Gate (spec A.3, C.1).

allowed/blocked + unmet for one asset (``subject_kind: zbm_work``): ad,
site, page, form, portal, message, checkout, chatbot. Per-type facts are
required per the A.3 table; the client's latest ALLOWED ``client``
activation supplies the category and EU-kit acknowledgment. Creative's
opaque ``export`` / ``rights`` objects travel in ``caller_context`` and are
never interpreted. Never drafts or fixes copy, policies or pages.
"""

from __future__ import annotations

from facts import EU5, PUBLISH_FLAGS, PUBLISH_SCHEMA, SITE_TYPES, get, has, validate
from intelligences.engine import Ctx, resolution_unmet
from intelligences.i07_jurisdiction import Params, resolve_target

NUMBER, NAME, ACTOR = 4, "Publish Gate", "intel_04_publish_gate"

_REQUIRED = ("brief_id", "asset_type", "asset_content_sha256", "client_id", "target_jurisdictions", "platforms",
             *(f"flags.{f}" for f in PUBLISH_FLAGS))


def required(f: dict) -> list[str]:
    req = list(_REQUIRED)
    fl = f.get("flags") or {}
    t = f.get("asset_type")
    if fl.get("claims_present") is True or fl.get("health_or_earnings_claim") is True:
        req += ["claim_file_id", "claim_file_approved"]
    if fl.get("paid_or_endorsement") is True:
        req += ["disclosure.in_video_label_text", "disclosure.in_video_label_start_s", "disclosure.voice_present"]
        if get(f, "disclosure.voice_present") is True:
            req.append("disclosure.audio_disclosure_present")
    if fl.get("real_person_likeness") is True or fl.get("implied_affiliation") is True:
        req.append("consent_document_id")
    if fl.get("personal_use_claim") is True:
        req.append("personal_use_attested")
    if t == "ad_video":
        req.append("music.present")
    if get(f, "music.present") is True:
        req += ["music.source", "music.track_or_license_id"]
    if t in SITE_TYPES:
        req += ["docs", "tracking"]
    if t == "email_campaign":
        req += ["canspam", "recipient_countries", "recipients_cold"]
    if t in ("sms_campaign", "autodialed_call"):
        req += ["sms", "recipient_countries"]
    if t == "dm_campaign":
        req += ["recipient_countries", "recipients_cold"]
    if t == "subscription_checkout":
        req.append("subscription")
    if t == "chatbot":
        req.append("genai_disclosed_at_outset")
    return req


def build(work_id: str, raw_facts: dict, rows: dict, fallback: dict, env, now) -> Ctx:
    f, wrong = validate(PUBLISH_SCHEMA, raw_facts)
    params = Params.from_rows(rows or fallback)
    ctx = Ctx(gate="publish", subject_id=work_id, subject_kind="zbm_work", asset_type=f.get("asset_type"), facts=f,
              rows=rows, fallback_rows=fallback, params=params, env=env, now=now)
    for w in wrong:
        ctx.pre.append(ctx.missing("HR-03", w))
    for key in required(f):
        if not has(f, key):
            ctx.pre.append(ctx.missing("HR-03", key))

    client = env.latest_allowed_activation("client", f["client_id"]) if "client_id" in f else None
    ctx.client = client
    targets = f.get("target_jurisdictions") or []
    resolutions = [resolve_target(t, params) for t in targets]
    ctx.resolutions = resolutions
    for r in resolutions:
        ctx.pre.extend(resolution_unmet(ctx, r))
    ctx.jurisdictions = {r.code for r in resolutions if r.code}
    ctx.audience = set(ctx.jurisdictions)
    ctx.platforms = set(f.get("platforms") or [])
    needs_client = any(t.split("-", 1)[0] in EU5 for t in targets)
    if client is None and (needs_client or ctx.platforms & {"tiktok", "x"}):
        ctx.pre.append(ctx.missing("HR-03", "client_activation", "no allowed client activation ruling for this client"))

    flags = {k: v for k, v in (f.get("flags") or {}).items()}
    if f.get("asset_type") == "ad_video" or has(f, "music.present"):
        if has(f, "music.present"):
            flags["music_present"] = f["music"]["present"]
    else:
        flags["music_present"] = False  # no music facts on a non-video asset: nothing to declare
    ctx.flags = flags
    return ctx
