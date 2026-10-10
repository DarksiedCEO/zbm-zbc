"""
The outcome envelope every agent returns, and the closed vocabularies it uses (ADR 0017 decisions 4-6).

An agent's OUTCOME (did the agent run, and how far) is kept separate from its FINDINGS (what it saw). A run that
could not observe anything is never "no problems found": it is ``NOT_CONNECTED``, ``BLOCKED``, ``FAILED``,
``KILLED`` or ``QUARANTINED`` with an empty finding list, and the report says so.

Evidence classes (spec, Oct 6):
  data    measured | estimated | modeled | inferred | unknown
  effect  observed | attributed | incremental | causally_supported | financially_verified
"Credit is not causation": Wave 1 makes no effect claim at all, so every finding's ``effect_class`` is None.

Decision states: ACT, TEST, WATCH, DEFER, STOP, DO_NOT_BUILD, DO_NOT_PUBLISH, DO_NOT_SPEND, INSUFFICIENT_EVIDENCE.

Everything here is plain data (dicts of str / int / bool / None / lists): it goes into the hash-chained log as is.
Text taken from a crawled page or a provider answer is carried only in a field named ``observed`` (bounded, marked
``untrusted``): it is data to report, never an instruction to anything in this service.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Optional

OUTCOMES = ("OK", "PARTIAL", "NOT_CONNECTED", "BLOCKED", "FAILED", "KILLED", "QUARANTINED", "INSUFFICIENT_EVIDENCE")
DATA_CLASSES = ("measured", "estimated", "modeled", "inferred", "unknown")
EFFECT_CLASSES = ("observed", "attributed", "incremental", "causally_supported", "financially_verified")
DECISIONS = ("ACT", "TEST", "WATCH", "DEFER", "STOP", "DO_NOT_BUILD", "DO_NOT_PUBLISH", "DO_NOT_SPEND",
             "INSUFFICIENT_EVIDENCE")
SEVERITIES = ("critical", "high", "medium", "low", "info")
# The ten capability areas (P1-P10). Wave 1 builds P1-P5 read-side only (P6 local presence: the canonical entity
# record and the on-site consistency check only).
CAPABILITIES = {
    "P1": "technical", "P2": "crawler_access", "P3": "machine_readability", "P4": "ai_visibility",
    "P5": "execution_and_proof", "P6": "local_presence", "P7": "authority", "P8": "content", "P9": "competitor",
    "P10": "reputation",
}
OBSERVED_MAX = 300          # characters of crawled / provider text kept in one finding
FINDINGS_MAX = 400          # per envelope; past it the envelope says how many were dropped


def sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8", "surrogatepass")).hexdigest()


def observed(text: Optional[str], limit: int = OBSERVED_MAX) -> Optional[dict]:
    """Crawled or provider text as reportable data: control characters removed, bounded, marked untrusted."""
    if text is None:
        return None
    s = "".join(ch if ch.isprintable() else " " for ch in str(text))
    s = " ".join(s.split())
    return {"untrusted": True, "text": s[:limit], "truncated": len(s) > limit, "sha256": hashlib.sha256(
        s.encode("utf-8", "surrogatepass")).hexdigest()}


def finding(code: str, severity: str, data_class: str, decision: str, *, url: Optional[str] = None,
            detail: Optional[dict] = None, capability: str = "P1", effect_class: Optional[str] = None) -> dict:
    if severity not in SEVERITIES or data_class not in DATA_CLASSES or decision not in DECISIONS:
        raise ValueError("finding outside the closed vocabularies")
    if effect_class is not None and effect_class not in EFFECT_CLASSES:
        raise ValueError("effect class outside the closed vocabulary")
    if capability not in CAPABILITIES:
        raise ValueError("unknown capability area")
    return {"code": code, "severity": severity, "data_class": data_class, "effect_class": effect_class,
            "decision": decision, "capability": capability, "url": url, "detail": detail or {}}


def envelope(agent: str, task: str, outcome: str, findings: Iterable[dict] = (), *, methodology: str,
             limitations: Iterable[str] = (), not_connected: Iterable[str] = (), facts: Optional[dict] = None,
             reason: Optional[str] = None) -> dict:
    """One agent's answer. ``facts``: what was measured (counts, states), kept apart from the findings."""
    if outcome not in OUTCOMES:
        raise ValueError("outcome outside the closed vocabulary")
    fl = list(findings)
    if outcome in ("NOT_CONNECTED", "KILLED", "QUARANTINED") and fl:
        raise ValueError("an agent that observed nothing reports no findings")
    return {"agent": agent, "task": task, "outcome": outcome, "reason": reason,
            "findings": fl[:FINDINGS_MAX], "findings_dropped": max(0, len(fl) - FINDINGS_MAX),
            "facts": facts or {}, "methodology": methodology, "limitations": list(limitations),
            "not_connected": sorted(set(not_connected))}


def worst_outcome(outcomes: Iterable[str]) -> str:
    """The audit's overall outcome: OK only when every agent is OK."""
    s = set(outcomes)
    if not s:
        return "INSUFFICIENT_EVIDENCE"
    if s == {"OK"}:
        return "OK"
    if s <= {"OK", "PARTIAL", "NOT_CONNECTED"}:
        return "PARTIAL"
    if "OK" in s or "PARTIAL" in s:
        return "PARTIAL"
    return sorted(s, key=OUTCOMES.index)[-1]
