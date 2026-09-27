"""
Intelligence 4 — Obligation Tracker (Legal spec §B.4, §C.4). Decides dated obligations and their due/missed
status; never invents an obligation from unexecuted text.

Obligations are created only from an acceptance with ``evidence_sufficient: true`` (LG-06), by binding each
clause's obligation descriptors ``{code, party, owner, due_rule, lead_days}`` to the executed version's
variables: ``offset(<field>, ±Nd|±Nbd)`` (calendar or business days from a date variable or ``accepted_at``),
``fixed(<field>)``, ``per_clip`` (no date: tracked per clip by the owner). A field that is not a date variable of
the executed version leaves the obligation unbound (``due: null``, ``unbound: true``) — it is still created and
shown to Andre, never dropped. The daily job marks ``due_soon`` (today >= due - lead_days) and ``missed``
(today > due); ``done`` only with evidence from the owner department; ``waived`` only by Andre.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from bizdays import BusinessCalendar
from intelligences.i02_playbooks import DUE_RULE

NUMBER, NAME, ACTOR = 4, "Obligation Tracker", "intel_04_obligations"
OPEN_STATES = ("open", "due_soon")


def bind_due(due_rule: str, fields: dict[str, str], cal: BusinessCalendar) -> tuple[Optional[str], bool]:
    """(due date ISO or None, unbound). ``fields``: name -> YYYY-MM-DD (date variables + accepted_at)."""
    m = DUE_RULE.fullmatch(due_rule)
    if m is None or due_rule == "per_clip":
        return None, False
    name = m.group("of") or m.group("ff")
    raw = fields.get(name)
    if raw is None:
        return None, True
    try:
        base = date.fromisoformat(raw)
    except ValueError:
        return None, True
    if m.group("ff"):
        return base.isoformat(), False
    n = int(m.group("n")) * (1 if m.group("sign") == "+" else -1)
    if m.group("unit") == "d":
        return (base + timedelta(days=n)).isoformat(), False
    return cal.add(base, n).isoformat(), False


def next_status(ob: dict, today: date) -> Optional[str]:
    """The status the daily job moves an open obligation to (None: no change)."""
    if ob["status"] not in OPEN_STATES or not ob.get("due"):
        return None
    due = date.fromisoformat(ob["due"])
    if today > due:
        return "missed"
    if ob["status"] == "open" and today >= due - timedelta(days=ob["alert_lead_days"]):
        return "due_soon"
    return None
