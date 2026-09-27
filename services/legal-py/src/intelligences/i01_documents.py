"""
Intelligence 1 — Document Register (Legal spec §B.1, §C.1). Decides versions, the current version and the
approval preconditions; never approves (Andre does) and never answers "current" for an unapproved or expired
version.

- Current version = the highest ``approved`` version with ``effective_at`` <= now and ``review_by`` > now; none
  -> no current version (consumers block).
- Approval (LG-02): Andre's token on the version's content hash AND (``counsel_required`` false OR a counsel
  sign-off whose ``doc_sha256`` equals the version SHA-256). Documents a counsel question blocks (§I: CQ-17 ->
  ic_agreement, CQ-15 -> not_legal_advice_v1) also need that question verified (LG-18); the engagement letter
  needs its AI-use clause ``ENG-AI-01`` (CQ-24, LG-17).
- Template fill: the template is the document's own CURRENT (approved) version; placeholders ``{{name}}`` are
  replaced by typed, bounded variables only. Every string variable passes the advice-text guard (the counsel-
  approved body does not need to).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

import reasons as R

NUMBER, NAME, ACTOR = 1, "Document Register", "intel_01_documents"
VERSION_RE = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}$")
PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]{0,39})\}\}")
VAR_TYPES = ("string", "date", "int", "enum")
VAR_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_VARS = 50
STRING_MAX = 500
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")


def vkey(v: str) -> tuple[int, int]:
    a, b = v.split(".")
    return int(a), int(b)


def current(versions: list[dict], now: datetime) -> Optional[dict]:
    from clock import parse_iso
    live = [v for v in versions if v["status"] == "approved" and v.get("effective_at") and v.get("review_by")
            and parse_iso(v["effective_at"]) <= now < parse_iso(v["review_by"])]
    return max(live, key=lambda v: vkey(v["version"]), default=None)


def validate_schema(schema) -> dict:
    """Typed template-variable schema: {name: {type, max_length | values | min,max}}. Raises ValueError."""
    if not isinstance(schema, dict) or len(schema) > MAX_VARS:
        raise ValueError(f"template_variables: an object of at most {MAX_VARS} variables")
    out = {}
    for name, spec in schema.items():
        if not isinstance(name, str) or not VAR_NAME.fullmatch(name) or not isinstance(spec, dict):
            raise ValueError("template variable names are [a-z][a-z0-9_]{0,39} with an object spec")
        t = spec.get("type")
        if t not in VAR_TYPES:
            raise ValueError(f"variable {name}: type must be one of {', '.join(VAR_TYPES)}")
        allowed = {"type"} | {"string": {"max_length"}, "enum": {"values"}, "int": {"min", "max"}, "date": set()}[t]
        if set(spec) - allowed:
            raise ValueError(f"variable {name}: unknown keys")
        if t == "string":
            ml = spec.get("max_length", 200)
            if isinstance(ml, bool) or not isinstance(ml, int) or not 1 <= ml <= STRING_MAX:
                raise ValueError(f"variable {name}: max_length 1..{STRING_MAX}")
        if t == "enum":
            vals = spec.get("values")
            if not isinstance(vals, list) or not 1 <= len(vals) <= 50 or not all(
                    isinstance(x, str) and 1 <= len(x) <= 100 and not _CONTROL.search(x) for x in vals):
                raise ValueError(f"variable {name}: values is a list of 1..50 short strings")
        if t == "int":
            for k in ("min", "max"):
                if k in spec and (isinstance(spec[k], bool) or not isinstance(spec[k], int)):
                    raise ValueError(f"variable {name}: {k} must be an integer")
        out[name] = dict(spec)
    return out


def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text))


def check_variables(schema: dict, variables) -> dict:
    """Type/bounds check of supplied variables (all declared variables required, no others). Returns the values
    as strings for rendering. Raises ValueError."""
    if not isinstance(variables, dict):
        raise ValueError("variables must be an object")
    extra, missing = set(variables) - set(schema), set(schema) - set(variables)
    if extra or missing:
        raise ValueError(f"variables: unknown {sorted(extra)[:5]}, missing {sorted(missing)[:5]}")
    out = {}
    for name, spec in schema.items():
        v = variables[name]
        t = spec["type"]
        if t == "string":
            if not isinstance(v, str) or not v.strip() or len(v) > spec.get("max_length", 200) or _CONTROL.search(v):
                raise ValueError(f"variable {name}: a printable single-line string of at most "
                                 f"{spec.get('max_length', 200)} characters")
            out[name] = v
        elif t == "date":
            if not isinstance(v, str) or len(v) != 10:
                raise ValueError(f"variable {name}: a YYYY-MM-DD date")
            date.fromisoformat(v)
            out[name] = v
        elif t == "int":
            if isinstance(v, bool) or not isinstance(v, int) or not spec.get("min", -10**9) <= v <= spec.get("max", 10**9):
                raise ValueError(f"variable {name}: an integer within bounds")
            out[name] = str(v)
        else:
            if v not in spec["values"]:
                raise ValueError(f"variable {name}: one of the declared values")
            out[name] = v
    return out


def render(template_text: str, values: dict) -> str:
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], template_text)


def approval_reasons(doc: dict, version: dict, cq_verified, now: datetime) -> list[dict]:
    """Every precondition that is unmet (complete, no short-circuit). ``cq_verified(cq_id) -> bool``."""
    out = []
    if version["status"] not in ("draft", "counsel_review"):
        out.append(R.item("VERSION_NOT_APPROVABLE", f"version {version['version']} is {version['status']}"))
    if doc["counsel_required"]:
        so = version.get("counsel_signoff")
        if so is None:
            out.append(R.item("COUNSEL_RECORD_MISSING", f"{doc['doc_id']} {version['version']} has no counsel sign-off"
                              " record"))
        elif so.get("doc_sha256") != version["sha256"]:
            out.append(R.item("COUNSEL_RECORD_MISSING", "the counsel sign-off names a different document hash"))
    for cq in doc.get("approval_blocked_by") or []:
        if not cq_verified(cq):
            out.append(R.item("CQ_UNVERIFIED", f"approval of {doc['doc_id']} waits on counsel question {cq}", cq_id=cq))
    have = {c["clause_id"] for c in version.get("clause_ids") or []}
    for c in doc.get("required_clause_ids") or []:
        if c not in have:
            out.append(R.item("ENGAGEMENT_AI_CLAUSE_MISSING", f"{doc['doc_id']} {version['version']} lacks clause {c}",
                              cq_id="CQ-24"))
    return out
