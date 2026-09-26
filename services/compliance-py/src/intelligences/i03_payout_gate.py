"""
Intelligence 3 — Payout Gate (spec A.2, C.1).

allowed/blocked + unmet for ONE clip (``subject_kind: zbc_clip``). Consulted
by Creative's payout eligibility after Clip Review passed, and re-checks
``clip_review == "pass"`` itself. The context inherits the campaign's
latest ALLOWED ``zbc_brand`` activation (targets = audience, category,
pay basis, flags) and the clipper's latest ALLOWED ``zbc_creator``
activation (jurisdiction, payee type, accounts, age attestation id), both
supplied by Compliance from its own records; the clipper and campaign
jurisdictions are re-resolved under the register version in force (A.2
item 9). A pass means "may be handed to Finance for payment
consideration" and carries no amount. Never sees an amount, rate or balance.
"""

from __future__ import annotations

from facts import ACTIVATION_FLAGS, CLIP_FLAGS, PAYOUT_SCHEMA, get, has, validate
from intelligences.engine import Ctx, resolution_unmet
from intelligences.i07_jurisdiction import Params, resolve_person, resolve_target

NUMBER, NAME, ACTOR = 3, "Payout Gate", "intel_03_payout_gate"

_REQUIRED = ("campaign_id", "rulebook_version", "post_ref", "clipper_id", "posted_at", "clip_review", "platform",
             "disclosure.in_video_label_text", "disclosure.in_video_label_start_s", "disclosure.voice_present",
             "music.present", "rights_clearance_id", *(f"flags.{f}" for f in CLIP_FLAGS))


def required(f: dict) -> list[str]:
    req = list(_REQUIRED)
    if get(f, "disclosure.voice_present") is True:
        req.append("disclosure.audio_disclosure_present")
    if get(f, "music.present") is True:
        req += ["music.source", "music.track_or_license_id"]
    if get(f, "flags.real_person_likeness") is True or get(f, "flags.implied_affiliation") is True:
        req.append("consent_document_id")
    if get(f, "flags.personal_use_claim") is True:
        req.append("personal_use_attested")
    return req


def build(submission_id: str, raw_facts: dict, rows: dict, fallback: dict, env, now) -> Ctx:
    f, wrong = validate(PAYOUT_SCHEMA, raw_facts)
    params = Params.from_rows(rows or fallback)
    ctx = Ctx(gate="payout", subject_id=submission_id, subject_kind="zbc_clip", facts=f, rows=rows,
              fallback_rows=fallback, params=params, env=env, now=now)
    for w in wrong:
        ctx.pre.append(ctx.missing("HR-03", w))
    for key in required(f):
        if not has(f, key):
            ctx.pre.append(ctx.missing("HR-03", key))
    if "clip_review" in f and f["clip_review"] != "pass":
        ctx.pre.append(ctx.u("HR-03", "clip_review_pass", f"Clip Review outcome is '{f['clip_review']}', not 'pass'"))

    clipper = env.latest_allowed_activation("zbc_creator", f["clipper_id"]) if "clipper_id" in f else None
    campaign = env.latest_allowed_activation("zbc_brand", f["campaign_id"]) if "campaign_id" in f else None
    ctx.clipper, ctx.campaign = clipper, campaign
    if clipper is None:
        for oid in ("HR-03", "HR-02"):
            ctx.pre.append(ctx.missing(oid, "clipper_activation", "no allowed activation ruling for this clipper"))
    if campaign is None:
        ctx.pre.append(ctx.missing("HR-03", "campaign_activation", "no allowed activation ruling for this campaign"))

    resolutions = []
    if clipper is not None:
        resolutions.append(resolve_person(clipper.get("jurisdiction"), params, "clipper"))
    if campaign is not None:
        resolutions.append(resolve_person(campaign.get("jurisdiction"), params, "campaign client"))
        for t in campaign.get("target_jurisdictions") or []:
            resolutions.append(resolve_target(t, params, "campaign target"))
    ctx.resolutions = resolutions
    for r in resolutions:
        ctx.pre.extend(resolution_unmet(ctx, r))
    ctx.jurisdictions = {r.code for r in resolutions if r.code}
    ctx.audience = {r.code for r in resolutions if r.code and r.who == "campaign target"}
    ctx.platforms = {f["platform"]} if "platform" in f else set()

    flags: dict = {k: v for k, v in (f.get("flags") or {}).items()}
    if campaign is not None:
        cflags = campaign.get("flags") or {}
        for k in ACTIVATION_FLAGS:
            if k in cflags:
                flags[k] = cflags[k]
        claims = campaign.get("claims") or {}
        for k in ("claims_present", "health_or_earnings_claim"):
            if k in claims:
                flags[k] = claims[k]
    if clipper is not None:
        if clipper.get("payee_type") in ("individual", "entity"):
            flags["entity_payee"] = clipper["payee_type"] == "entity"
        country = get(clipper, "jurisdiction.declared_country")
        if country:
            flags["foreign_payee"] = country != "US"
    flags["paid_or_endorsement"] = True          # derived: every zbc_clip is a paid endorsement
    if "music" in f and "present" in f["music"]:
        flags["music_present"] = f["music"]["present"]
    ctx.flags = flags
    return ctx
