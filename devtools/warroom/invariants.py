"""War room invariants (ADR 0018): pure predicates over one case's observation. Standard library only.

An observation is what a driver saw inside the sandbox: ``steps`` (one entry per HTTP call: action, status, body) and
``state`` (the service's resulting state and evidence, read back through its own API or the harness's recording
fakes). Every invariant returns ``(held, detail)``. Severity is not decided here: the scenario library gives each
invariant MUST (gate-blocking) or SHOULD (scored, reported)."""

from __future__ import annotations

import json
from typing import Any, Callable

# service-py (ADR 0014 + sweep A / AEGIS re-reviews): the alert codes that put a possible opt-out in front of Andre
OPT_OUT_ALERTS = frozenset({"OPT_OUT_POSSIBLE", "EMAIL_OPT_OUT_UNCLEAR", "OPT_OUT_IN_QUOTED_TEXT",
                            "SMS_OPT_OUT_SUSPECTED", "EMAIL_OPTED_OUT_BY_REQUEST"})


def _step(obs: dict, name: str) -> dict:
    for s in obs.get("steps", []):
        if s.get("action") == name:
            return s
    raise KeyError(f"no step named {name!r} in the observation")


def _field(body: Any, path: str) -> Any:
    cur = body
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


# --------------------------------------------------------------------------------------------- generic

def status_in(obs: dict, step: str, codes: list[int]) -> tuple[bool, str]:
    st = _step(obs, step)["status"]
    return st in codes, f"{step}: HTTP {st}, wanted one of {codes}"


def field_equals(obs: dict, step: str, field: str, value: Any) -> tuple[bool, str]:
    got = _field(_step(obs, step).get("body"), field)
    return got == value, f"{step}.{field} = {got!r}, wanted {value!r}"


def any_field_truthy(obs: dict, step: str, fields: list[str]) -> tuple[bool, str]:
    body = _step(obs, step).get("body")
    got = {f: _field(body, f) for f in fields}
    return any(bool(v) for v in got.values()), f"{step}: {got}"


def refused_or_field_equals(obs: dict, step: str, refused: list[int], then_step: str, field: str,
                            value: Any) -> tuple[bool, str]:
    """``step`` was refused with one of ``refused`` (nothing to aggregate), or it was accepted and ``then_step``'s
    ``field`` is ``value``."""
    st = _step(obs, step)["status"]
    if st in refused:
        return True, f"{step}: refused with HTTP {st}"
    got = _field(_step(obs, then_step).get("body"), field)
    return got == value, f"{step}: HTTP {st}; {then_step}.{field} = {got!r}, wanted {value!r}"


def state_equals(obs: dict, key: str, value: Any) -> tuple[bool, str]:
    got = _field(obs.get("state", {}), key)
    return got == value, f"state.{key} = {got!r}, wanted {value!r}"


def state_in(obs: dict, key: str, values: list) -> tuple[bool, str]:
    got = _field(obs.get("state", {}), key)
    return got in values, f"state.{key} = {got!r}, wanted one of {values!r}"


def state_same(obs: dict, a: str, b: str) -> tuple[bool, str]:
    va, vb = _field(obs.get("state", {}), a), _field(obs.get("state", {}), b)
    return va == vb, f"state.{a} = {va!r}, state.{b} = {vb!r}"


def no_echo(obs: dict, key: str = "foreign_identifiers") -> tuple[bool, str]:
    """No response body carries any of another person's identifiers (``state.<key>``, a list of strings)."""
    needles = [n for n in (_field(obs.get("state", {}), key) or []) if n]
    if not needles:
        return False, f"state.{key} is empty: nothing to check against (the driver must list them)"
    hay = json.dumps([s.get("body") for s in obs.get("steps", [])], ensure_ascii=False).casefold()
    hits = [n for n in needles if n.casefold() in hay]
    return not hits, f"echoed: {hits}" if hits else "no foreign identifier in any response"


# --------------------------------------------------------------------------------------------- consent (service-py)

def _consents(obs: dict, which: str) -> dict:
    return _field(obs.get("state", {}), which) or {}


def opt_out_honoured_or_surfaced(obs: dict) -> tuple[bool, str]:
    before, after = _consents(obs, "consents_before"), _consents(obs, "consents_after")
    revoked = sorted(ch for ch, st in after.items() if st == "revoked" and before.get(ch) != "revoked")
    alerts = sorted(set(obs.get("state", {}).get("alert_codes", [])) & OPT_OUT_ALERTS)
    return bool(revoked or alerts), f"revoked={revoked} alerts={alerts}"


def consent_is(obs: dict, channel: str, status: str) -> tuple[bool, str]:
    got = _consents(obs, "consents_after").get(channel)
    return got == status, f"{channel} consent {got!r}, wanted {status!r}"


def consent_unchanged(obs: dict) -> tuple[bool, str]:
    before, after = _consents(obs, "consents_before"), _consents(obs, "consents_after")
    return before == after, f"before={before} after={after}"


def no_opt_out_alert(obs: dict) -> tuple[bool, str]:
    alerts = sorted(set(obs.get("state", {}).get("alert_codes", [])) & OPT_OUT_ALERTS)
    return not alerts, f"alerts={alerts}"


INVARIANTS: dict[str, Callable[..., tuple[bool, str]]] = {
    "status_in": status_in, "field_equals": field_equals, "any_field_truthy": any_field_truthy,
    "refused_or_field_equals": refused_or_field_equals,
    "state_equals": state_equals, "state_in": state_in, "state_same": state_same, "no_echo": no_echo,
    "opt_out_honoured_or_surfaced": opt_out_honoured_or_surfaced, "consent_is": consent_is,
    "consent_unchanged": consent_unchanged, "no_opt_out_alert": no_opt_out_alert,
}


def evaluate(spec: dict, obs: dict) -> tuple[bool, str]:
    """One invariant spec ``{"check": name, "args": {...}}`` against an observation. A predicate that cannot be
    evaluated (missing step, bad args) does NOT hold: the detail says why."""
    fn = INVARIANTS.get(spec["check"])
    if fn is None:
        return False, f"unknown invariant check {spec['check']!r}"
    try:
        return fn(obs, **(spec.get("args") or {}))
    except Exception as e:  # noqa: BLE001 - a broken predicate must fail, never pass
        return False, f"invariant could not be evaluated: {type(e).__name__}: {e}"
