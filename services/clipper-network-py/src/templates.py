"""
Message templates (spec §C.6, CN-15, CN-16, CN-26, §0.1.7-8).

A template is data in the Andre-approved register: ``template_id``, ``version``,
``purpose`` (transactional | relationship | commercial), ``channels``, ``body``
with ``{name}`` placeholders, and ``variables`` {name: type}. There is no
free text: every variable has one of the closed TYPES below, each with a
strict value format, so no caller or record can put prose, a money value or
an earnings promise into a message. ``validate_template`` refuses (Invalid,
422) a template whose body or types could carry one; ``render`` refuses a
value that does not match its type.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from errors import Invalid
from textguard import display_name_problem, escape_for_channel, has_control_chars, money_or_earnings, scan_injection

TEMPLATE_IDS = ("application_received", "admission_decision", "agreement_new_version", "rate_card_published",
                "rate_card_changed", "rulebook_announced", "kit_delivered", "clip_flagged", "certification_result",
                "strike_notice", "suspension_notice", "ban_notice", "appeal_received", "appeal_outcome",
                "offboarding_confirmation", "data_export_ready", "recruiting_invite")
PURPOSES = ("transactional", "relationship", "commercial")
CHANNELS = ("email", "in_app", "discord_server_post")
BODY_MAX = 1200
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]{0,39})\}")
_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_WORD = re.compile(r"[a-z][a-z0-9_]{0,39}")
_RULE = re.compile(r"CN-(?:[0-9]{2}|CQ-[0-9]{2})")
_SHA = re.compile(r"[0-9a-f]{64}")

# Variable types. There is deliberately no "text", "money", "amount", "rate" or "number with decimals" type.
TYPES = ("id", "ids", "rule_ids", "date", "int", "word", "sha256", "until", "display_name", "automation_disclosure",
         "postal_address", "opt_out_link")
# Filled by the service itself, never by a record's variables.
SERVICE_FILLED = ("display_name", "automation_disclosure", "postal_address", "opt_out_link")

# What each template must carry (enforced on every proposal; a template that drops one is refused).
REQUIRED = {
    "application_received": ["{automation_disclosure}", "18+"],
    "admission_decision": ["{automation_disclosure}", "{rule_ids}"],
    "recruiting_invite": ["Advertisement", "18+ only", "{opt_out_link}", "{postal_address}"],
    "rate_card_changed": ["{effective_date}", "{rule_ids}"],
    "clip_flagged": ["{rule_ids}", "{appeal_deadline}"],
    "strike_notice": ["{rule_ids}", "{appeal_deadline}"],
    "suspension_notice": ["{rule_ids}", "{appeal_deadline}"],
    "ban_notice": ["{rule_ids}", "{appeal_deadline}"],
    "appeal_outcome": ["{rule_ids}"],
    "offboarding_confirmation": ["{connections}", "{rule_ids}"],
}
COMMERCIAL = ("recruiting_invite",)


def placeholders(body: str) -> set[str]:
    return set(_PLACEHOLDER.findall(body))


def validate_template(t: Any) -> dict:
    """Strict shape + content rules. Returns the normalized template dict or raises Invalid (422)."""
    if not isinstance(t, dict):
        raise Invalid("template must be an object")
    keys = {"template_id", "version", "purpose", "channels", "body", "variables"}
    if set(t) != keys:
        raise Invalid(f"template fields must be exactly {sorted(keys)}")
    tid, ver, purpose, channels, body, variables = (t[k] for k in ("template_id", "version", "purpose", "channels",
                                                                   "body", "variables"))
    if tid not in TEMPLATE_IDS:
        raise Invalid(f"template_id must be one of the spec §C.6 templates ({', '.join(TEMPLATE_IDS)})")
    if not isinstance(ver, int) or isinstance(ver, bool) or not 1 <= ver <= 100_000:
        raise Invalid("version must be an integer 1..100000")
    if purpose not in PURPOSES:
        raise Invalid("purpose must be transactional, relationship or commercial")
    if (purpose == "commercial") != (tid in COMMERCIAL):
        raise Invalid("only recruiting_invite is commercial, and it is always commercial (CAN-SPAM)")
    if not isinstance(channels, list) or not channels or len(channels) > 3 or len(set(channels)) != len(channels) \
            or any(c not in CHANNELS for c in channels):
        raise Invalid(f"channels must be a non-empty subset of {', '.join(CHANNELS)} (SMS is off, CN-09)")
    if "discord_server_post" in channels and tid not in COMMERCIAL:
        raise Invalid("member messages never go to a Discord server post (they are addressed to one clipper)")
    if not isinstance(body, str) or not 1 <= len(body) <= BODY_MAX or has_control_chars(body):
        raise Invalid(f"body must be 1..{BODY_MAX} characters with no control characters")
    if not isinstance(variables, dict) or len(variables) > 20:
        raise Invalid("variables must be an object of at most 20 names")
    for name, typ in variables.items():
        if not isinstance(name, str) or not _PLACEHOLDER.fullmatch("{" + name + "}"):
            raise Invalid("variable names are [a-z_][a-z0-9_]{0,39}")
        if typ not in TYPES:
            raise Invalid(f"variable {name}: type {typ!r} is not allowed (types: {', '.join(TYPES)}; no money, amount, "
                          "rate or free-text type exists, CN-26 / §0.1.8)")
    if placeholders(body) != set(variables):
        raise Invalid("every {placeholder} in the body must be a declared variable and every variable must be used")
    literal = _PLACEHOLDER.sub(" ", body)
    hits = money_or_earnings(literal)
    if hits:
        raise Invalid(f"template text carries money or an earnings/guarantee claim ({', '.join(hits)}): refused (CN-26)")
    if scan_injection(literal):
        raise Invalid("template text carries instruction-like text: refused")
    for need in REQUIRED.get(tid, []):
        if need not in body:
            raise Invalid(f"template {tid} must contain {need!r}")
    for name, typ in variables.items():
        if typ in ("automation_disclosure",) and tid not in ("application_received", "admission_decision"):
            continue
        if typ in ("postal_address", "opt_out_link") and tid not in COMMERCIAL:
            raise Invalid(f"{typ} belongs to recruiting_invite only")
    return {"template_id": tid, "version": ver, "purpose": purpose, "channels": sorted(channels), "body": body,
            "variables": dict(sorted(variables.items()))}


def check_value(typ: str, value: Any) -> str:
    """The value's rendering, or Invalid. Records carry only these typed values (never prose)."""
    ok = False
    out = ""
    if typ == "id":
        ok = isinstance(value, str) and bool(_ID.fullmatch(value))
        out = value if ok else ""
    elif typ == "ids":
        ok = isinstance(value, list) and 0 < len(value) <= 20 and all(isinstance(v, str) and _ID.fullmatch(v) for v in value)
        out = ", ".join(value) if ok else ""
    elif typ == "rule_ids":
        ok = isinstance(value, list) and 0 < len(value) <= 30 and all(isinstance(v, str) and _RULE.fullmatch(v) for v in value)
        out = ", ".join(value) if ok else ""
    elif typ == "date":
        try:
            ok = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
        except ValueError:
            ok = False
        out = value if ok else ""
    elif typ == "until":
        try:
            ok = isinstance(value, str) and (value == "further_review" or date.fromisoformat(value).isoformat() == value)
        except ValueError:
            ok = False
        out = value if ok else ""
    elif typ == "int":
        ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**12
        out = str(value) if ok else ""
    elif typ == "word":
        ok = isinstance(value, str) and bool(_WORD.fullmatch(value))
        out = value if ok else ""
    elif typ == "sha256":
        ok = isinstance(value, str) and bool(_SHA.fullmatch(value))
        out = value if ok else ""
    if not ok:
        raise Invalid(f"template variable of type {typ} has a value that does not match the type")
    if money_or_earnings(out):
        raise Invalid("a template variable value looks like money or an earnings claim: refused (CN-26)")
    return out


def check_variables(template: dict, variables: dict) -> dict:
    """The record-side variables (everything the service does not fill itself), type-checked."""
    want = {n: t for n, t in template["variables"].items() if t not in SERVICE_FILLED}
    if set(variables) != set(want):
        raise Invalid(f"template {template['template_id']} needs exactly the variables {sorted(want)}")
    for n, typ in want.items():
        check_value(typ, variables[n])
    return {n: variables[n] for n in sorted(want)}


def render(template: dict, variables: dict, filled: dict[str, str], channel: str) -> str:
    """Render the body: record variables are re-checked against their types; service-filled values (display
    name from the contact store, the CN-16 disclosure, the postal address, the opt-out link) are passed in.
    The display name is the only clipper-supplied text: it is re-checked (a name stored before AEGIS N16-11 is
    refused here) and escaped for ``channel``."""
    values: dict[str, str] = {}
    for name, typ in template["variables"].items():
        if typ in SERVICE_FILLED:
            v = filled.get(typ)
            if not isinstance(v, str) or not v:
                raise Invalid(f"{typ} is not available for this message")
            if typ == "display_name":
                why = display_name_problem(v)
                if why:
                    raise Invalid(f"stored display name is not renderable ({why})")
                v = escape_for_channel(v, channel)
            elif money_or_earnings(v):
                raise Invalid(f"{typ} looks like money or an earnings claim: refused (CN-26)")
            values[name] = v
        else:
            values[name] = check_value(typ, variables.get(name))
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template["body"])
