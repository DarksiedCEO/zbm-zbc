"""
Intelligence 4 — Platform Access.

Decides: which platforms to request (from a website tag scan), the exact
steps for the person who holds the logins (P22), and whether a grant is
actually usable (P21) — business not personal, current not old, the
permission level the job needs, and a live check that it works.

Rules:
- Tag scan is a deterministic pattern scan over HTML the caller supplies.
  Fetching a site over the network is NOT built (NotWiredSiteFetcher).
- Least access: each job maps to the lowest role that can do it; a grant
  below it is a blocking problem with an exact fix; a grant above it is
  noted with a recommendation to reduce (not a block).
- A grant is ``usable`` only if the metadata checks pass AND the live
  platform probe confirms it. The live probe is not wired (stand-in says
  "not verified"), so today no grant is reported to a client as
  "received" on metadata alone.
- Platform facts (roles, steps, revoke paths) are dated knowledge with a
  shelf life. A fact with no verification date, or past its shelf life, is
  NOT quotable to a client: the steps are returned as an internal draft
  and the client is not given them until a person verifies them.
  Honest status: the shipped facts are drafts written from general
  knowledge on Sep 24 2026 and have NOT been verified against platform
  documentation — ``last_verified`` is None for all of them.
- Only Google Ads, Meta and Shopify (the first platforms to certify) have
  steps at all; anything else => "ask/escalate", never a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Optional

from onboarding_schema import (
    AccessGrantIn,
    AccessProblem,
    AccessVerification,
    AccountType,
    Person,
    Platform,
)

from ._status import PHASE1_STATUS

NUMBER = 4
NAME = "Platform Access"
PHASE = 1
STATUS = PHASE1_STATUS

TAG_PATTERNS: list[tuple[Platform, re.Pattern]] = [
    (Platform.GOOGLE_ADS, re.compile(r"(?i)googleadservices\.com|gtag\(\s*['\"]config['\"]\s*,\s*['\"]AW-\d+|\bAW-\d{6,}")),
    (Platform.GOOGLE_TAG_MANAGER, re.compile(r"(?i)googletagmanager\.com/gtm\.js|\bGTM-[A-Z0-9]{4,}")),
    (Platform.GOOGLE_ANALYTICS, re.compile(r"(?i)\bG-[A-Z0-9]{6,}\b|google-analytics\.com")),
    # Meta: the ``fbq('init'`` call here; the pixel script URL is found by
    # ``_meta_tag`` (fix wave 4, R1 — see below).
    (Platform.META, re.compile(r"(?i)\bfbq\(\s*['\"]init")),
    (Platform.SHOPIFY, re.compile(r"(?i)cdn\.shopify\.com|\bShopify\.theme\b|myshopify\.com")),
    (Platform.TIKTOK, re.compile(r"(?i)analytics\.tiktok\.com|\bttq\.load\(")),
]


# Fix wave 4 (R1): the Meta pattern ``connect\.facebook\.net/[^"']*fbevents\.js``
# rescanned the rest of a quote-free stretch from EVERY host occurrence in it
# (a page of repeated "connect.facebook.net/" was quadratic). ``_meta_tag``
# gives the same answer checking each quote-free stretch once: the first
# host occurrence in a stretch decides for every later one.
_META_HOST = re.compile(r"(?i)connect\.facebook\.net/")
_META_FILE = re.compile(r"(?i)fbevents\.js")
_QUOTE = re.compile(r"[\"']")


def _meta_tag(html: str) -> Optional[tuple[str, int]]:
    pos = 0
    while True:
        h = _META_HOST.search(html, pos)
        if h is None:
            return None
        q = _QUOTE.search(html, h.end())
        stop = q.start() if q else len(html)
        last = None
        for last in _META_FILE.finditer(html, h.end(), stop):
            pass
        if last is not None:
            return html[h.start():min(last.end(), h.start() + 80)], h.start()
        if q is None:
            return None
        pos = q.end()


def scan_tags(html: str) -> list[dict]:
    html = html or ""
    found = []
    for platform, rx in TAG_PATTERNS:
        m = rx.search(html)
        if platform == Platform.META:
            # The old single pattern was "script URL | fbq('init'": the
            # leftmost of the two is the evidence.
            script = _meta_tag(html)
            if script is not None and (m is None or script[1] <= m.start()):
                found.append({"platform": platform.value, "evidence": script[0]})
                continue
        if m:
            found.append({"platform": platform.value, "evidence": m.group(0)[:80]})
    return found


@dataclass(frozen=True)
class PlatformFacts:
    roles_least_to_most: tuple[str, ...]
    job_role: dict
    grant_steps: tuple[str, ...]
    revoke_steps: tuple[str, ...]
    can_see: dict
    cannot_see: tuple[str, ...]
    last_verified: Optional[date] = None
    source_note: str = "DRAFT from general knowledge (Sep 24 2026); NOT verified against platform documentation"


KNOWLEDGE_VERSION = "2026-09-24.draft1"
PLATFORM_KNOWLEDGE: dict[Platform, PlatformFacts] = {
    Platform.GOOGLE_ADS: PlatformFacts(
        roles_least_to_most=("read_only", "standard", "admin"),
        job_role={"audit": "read_only", "reporting": "read_only", "setup": "standard", "campaign_management": "standard"},
        grant_steps=(
            "Sign in to Google Ads with the account that owns the business ad account.",
            "Open Admin, then Access and security.",
            "Add the ZBM access email with the access level named below, then send the invite.",
        ),
        revoke_steps=("Open Admin, then Access and security.", "Find the ZBM user and remove access."),
        can_see={"read_only": ["campaigns, ads and keywords", "spend and performance reports", "conversion settings"]},
        cannot_see=("your billing payment methods", "your Google account password", "other Google services you use"),
    ),
    Platform.META: PlatformFacts(
        roles_least_to_most=("analyst", "advertiser", "admin"),
        job_role={"audit": "analyst", "reporting": "analyst", "setup": "advertiser", "campaign_management": "advertiser"},
        grant_steps=(
            "Sign in to Meta Business Suite for the BUSINESS portfolio (not a personal profile).",
            "Open Business settings, then Partners, and add ZBM as a partner.",
            "Assign the ad account with the permission level named below.",
        ),
        revoke_steps=("Open Business settings, then Partners.", "Select ZBM and remove the partner or the ad account assignment."),
        can_see={"analyst": ["ad account performance and spend", "campaign structure"]},
        cannot_see=("your personal Facebook or Instagram profile", "your password", "your payment method details"),
    ),
    Platform.SHOPIFY: PlatformFacts(
        roles_least_to_most=("view_reports", "view_orders_and_reports", "manage_apps"),
        job_role={"audit": "view_orders_and_reports", "reporting": "view_reports", "setup": "manage_apps"},
        grant_steps=(
            "Sign in to your Shopify admin as the store owner.",
            "Accept ZBM's collaborator request under Settings, then Users and permissions.",
            "Grant only the permissions named below.",
        ),
        revoke_steps=("Open Settings, then Users and permissions.", "Remove the ZBM collaborator."),
        can_see={"view_orders_and_reports": ["orders, discounts and customers' order history", "sales reports"]},
        cannot_see=("your payout bank details", "your Shopify password", "staff accounts"),
    ),
}


def facts_for(platform: Platform, knowledge: dict | None = None) -> Optional[PlatformFacts]:
    return (knowledge or PLATFORM_KNOWLEDGE).get(platform)


def mark_verified(knowledge: dict, platform: Platform, on: date, by: str) -> dict:
    """Return a copy of ``knowledge`` with one platform marked verified.
    Used by the person who checks the facts against platform docs."""
    k = dict(knowledge)
    k[platform] = replace(k[platform], last_verified=on, source_note=f"verified by {by} on {on.isoformat()}")
    return k


def facts_quotable(f: PlatformFacts, today: date, shelf_life_days: int) -> tuple[bool, str]:
    if f.last_verified is None:
        return False, "platform facts have never been verified; a person must verify them before a client sees them"
    if today - f.last_verified > timedelta(days=shelf_life_days):
        return False, f"platform facts last verified {f.last_verified.isoformat()}, past the {shelf_life_days}-day shelf life; re-check before quoting"
    return True, "current"


@dataclass(frozen=True)
class AccessRequestPlan:
    platform: Platform
    job: str
    required_role: Optional[str]
    send_to: str  # email of the login holder (P22)
    send_to_is_signer: bool
    steps: list[str]
    quotable: bool
    quotable_reason: str
    ask_or_escalate: Optional[str] = None


def plan_access_request(
    platform: Platform, job: str, signer: Person, login_holder: Optional[Person], today: date,
    shelf_life_days: int, knowledge: dict | None = None,
) -> AccessRequestPlan:
    holder = login_holder or signer
    f = facts_for(platform, knowledge)
    if f is None:
        return AccessRequestPlan(platform, job, None, holder.email, login_holder is None, [], False,
                                 "no certified steps for this platform",
                                 ask_or_escalate=f"{platform.value} is not in the first certified set (Google Ads, Meta, Shopify); ask Andre")
    role = f.job_role.get(job)
    if role is None:
        return AccessRequestPlan(platform, job, None, holder.email, login_holder is None, [], False,
                                 f"no least-access role defined for job '{job}'",
                                 ask_or_escalate=f"no role mapping for job '{job}' on {platform.value}; ask Andre")
    ok, why = facts_quotable(f, today, shelf_life_days)
    steps = list(f.grant_steps) + [f"Access level to choose: {role} (the least access this job needs)."]
    if login_holder is None:
        steps.insert(0, "If someone else on your team holds these logins, forward this link to them; it is personal to your account.")
    return AccessRequestPlan(platform, job, role, holder.email, login_holder is None, steps, ok, why)


def verify_grant(
    grant: AccessGrantIn, now: datetime, stale_account_days: int, live_ok: bool, live_detail: str,
    knowledge: dict | None = None,
) -> AccessVerification:
    platform = grant.platform
    label = platform.value.replace("_", " ").title()
    problems: list[AccessProblem] = []
    notes: list[str] = []
    if grant.account_type == AccountType.PERSONAL:
        problems.append(AccessProblem(
            code="personal_account",
            fix_instruction=(
                f"The {label} access came from a personal account ({grant.account_id}). Please sign in to your "
                f"BUSINESS {label} account and grant access from there instead, then remove the personal-account grant."
            ),
        ))
    elif grant.account_type == AccountType.UNKNOWN:
        problems.append(AccessProblem(
            code="unknown_account_type",
            fix_instruction=f"Please confirm that {label} account {grant.account_id} is your business account (not a personal one).",
        ))
    if grant.account_last_activity_at is None:
        problems.append(AccessProblem(
            code="account_activity_unknown",
            fix_instruction=f"Please confirm {label} account {grant.account_id} is the one you use today, not an older account.",
        ))
    elif now - grant.account_last_activity_at > timedelta(days=stale_account_days):
        problems.append(AccessProblem(
            code="stale_account",
            fix_instruction=(
                f"{label} account {grant.account_id} was last active on {grant.account_last_activity_at.date().isoformat()}, "
                f"which looks like an old account. Please grant access to the account you run today."
            ),
        ))
    f = facts_for(platform, knowledge)
    if f is None:
        problems.append(AccessProblem(code="platform_not_certified",
                                      fix_instruction=f"{label} is not yet a certified platform; Andre will confirm next steps."))
    else:
        required = f.job_role.get(grant.job)
        roles = f.roles_least_to_most
        if required is None:
            problems.append(AccessProblem(code="unknown_job", fix_instruction=f"No access level is defined for '{grant.job}'; Andre will confirm."))
        elif grant.granted_role not in roles:
            problems.append(AccessProblem(
                code="unknown_role",
                fix_instruction=f"We couldn't recognise the access level '{grant.granted_role}'. Please set ZBM's access level to '{required}'.",
            ))
        elif roles.index(grant.granted_role) < roles.index(required):
            problems.append(AccessProblem(
                code="insufficient_role",
                fix_instruction=(
                    f"You granted '{grant.granted_role}', but this job needs '{required}'. In {label}, change ZBM's access "
                    f"level from '{grant.granted_role}' to '{required}'."
                ),
            ))
        elif roles.index(grant.granted_role) > roles.index(required):
            notes.append(f"You granted more access than we need ('{grant.granted_role}'); '{required}' is enough, and we recommend reducing it.")
    metadata_ok = not problems
    live_verified = bool(live_ok) and metadata_ok
    if metadata_ok and not live_ok:
        problems.append(AccessProblem(code="live_check_not_verified", fix_instruction=live_detail))
    usable = metadata_ok and live_verified
    if usable:
        msg = f"Access received and confirmed working for {label}."
    elif metadata_ok:
        msg = (f"Your {label} access details look right. We're confirming the connection actually works "
               f"before we call it received.")
    else:
        msg = f"We need one fix before your {label} access will work:\n" + "\n".join(
            f"- {p.fix_instruction}" for p in problems if not p.code.startswith("live_check"))
    if notes:
        msg += "\n" + "\n".join(notes)
    return AccessVerification(
        platform=platform, account_id=grant.account_id, metadata_ok=metadata_ok, live_verified=live_verified,
        usable=usable, problems=problems, client_message=msg,
    )
