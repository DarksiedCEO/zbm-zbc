"""
The CN rule register (spec §B.9): rules CN-00..CN-26, counsel holds CN-CQ-01..08
and the §C.6 message templates, as ONE versioned, hash-chained register that
only Andre changes (§0.1.5; the Compliance B.5/B.6 pattern, ADR 0006
decisions 3, 7, 8 and N14-9 / N15-3).

- The seed (``seed/cn_rules_seed.json``) is pinned by SHA-256 (config.py);
  it becomes ONE ``seed`` proposal; nothing is in force until Andre approves
  it (CN-00: every decision is ``RULES_NOT_IN_FORCE``).
- Proposals: rule new / amend / retire, counsel memo (resolves a CN-CQ row),
  template new / amend / retire. Every proposal carries a server-computed
  ``diff``, ``weakening`` and ``weakening_reasons`` (hashed into
  ``content_sha256``); approving a weakening proposal needs
  ``acknowledge_weakening: true``; weakening is recomputed at approval
  against the register as it stands then (N15-3).
- Parameters of a seeded rule keep the seed's SHAPE (same keys, same leaf
  types, bounded values) so the engine can never read a malformed table.
- Some changes are refused outright (422), not just flagged: the 18+ rule's
  guardian path (§0.1.1), a counsel row resolved other than by a memo, a
  template that could carry money or drop a required element.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from clock import iso
from errors import Conflict, Invalid
from templates import validate_template
from textguard import has_control_chars

RULE_ID = re.compile(r"CN-(?:[0-9]{2}|CQ-[0-9]{2}(?:-M[0-9]{1,3})?)")
KINDS = ("founder", "lead_default", "research", "spec_choice", "counsel", "counsel_memo")
STATUSES = ("in_force", "open", "resolved", "retired")
PROPOSAL_KINDS = ("seed", "rule_new", "rule_amend", "rule_retire", "counsel_memo", "template_new", "template_amend",
                  "template_retire")
_URL = re.compile(r"https://[^\s\x00-\x1f\x7f]{1,500}")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("ascii")).hexdigest()


def sha_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()


# ------------------------------------------------------------------ rows


def _text(v, n, what) -> str:
    if not isinstance(v, str) or not 1 <= len(v) <= n or has_control_chars(v):
        raise Invalid(f"{what} must be 1..{n} characters, no control characters")
    return v


def _param_shape_ok(new: Any, ref: Any, path: str = "") -> Optional[str]:
    """``new`` must have exactly ``ref``'s shape: same keys, same leaf types; ints 0..100000, strings <= 300,
    lists of strings <= 30 items. Returns a problem or None."""
    if isinstance(ref, dict):
        if not isinstance(new, dict) or set(new) != set(ref):
            return f"parameters{path} must keep the keys {sorted(ref)}"
        for k in ref:
            p = _param_shape_ok(new[k], ref[k], f"{path}.{k}")
            if p:
                return p
        return None
    if isinstance(ref, bool):
        return None if isinstance(new, bool) else f"parameters{path} must be a boolean"
    if isinstance(ref, int):
        ok = isinstance(new, int) and not isinstance(new, bool) and 0 <= new <= 100_000
        return None if ok else f"parameters{path} must be an integer 0..100000"
    if isinstance(ref, str):
        ok = isinstance(new, str) and 1 <= len(new) <= 300 and not has_control_chars(new)
        return None if ok else f"parameters{path} must be a string of 1..300 characters"
    if isinstance(ref, list):
        ok = isinstance(new, list) and len(new) <= 30 and all(isinstance(x, str) and 1 <= len(x) <= 64 for x in new)
        return None if ok else f"parameters{path} must be a list of at most 30 short strings"
    return f"parameters{path} has an unsupported shape"


def validate_rule(r: Any, seed_row: Optional[dict]) -> dict:
    if not isinstance(r, dict):
        raise Invalid("rule must be an object")
    keys = {"rule_id", "title", "statement", "kind", "sources", "parameters", "status"}
    if set(r) != keys:
        raise Invalid(f"rule fields must be exactly {sorted(keys)} (approved_by, version and similar are server-set)")
    if not isinstance(r["rule_id"], str) or not RULE_ID.fullmatch(r["rule_id"]):
        raise Invalid("rule_id must be CN-nn or CN-CQ-nn")
    _text(r["title"], 200, "title")
    _text(r["statement"], 1000, "statement")
    if r["kind"] not in KINDS:
        raise Invalid(f"kind must be one of {', '.join(KINDS)}")
    if r["status"] not in STATUSES:
        raise Invalid(f"status must be one of {', '.join(STATUSES)}")
    src = r["sources"]
    if not isinstance(src, list) or len(src) > 10:
        raise Invalid("sources must be a list of at most 10")
    for s in src:
        if not isinstance(s, dict) or set(s) != {"text", "url"}:
            raise Invalid("each source is {text, url}")
        _text(s["text"], 300, "source text")
        if s["url"] is not None and (not isinstance(s["url"], str) or not _URL.fullmatch(s["url"])):
            raise Invalid("source url must be https or null")
    params = r["parameters"]
    if not isinstance(params, dict) or len(canonical(params)) > 4096:
        raise Invalid("parameters must be an object of at most 4 KB")
    if seed_row is not None:
        problem = _param_shape_ok(params, seed_row["parameters"])
        if problem:
            raise Invalid(problem + f" (the shape of {r['rule_id']} in the pinned seed)")
    elif params:
        raise Invalid("a new rule carries no parameters: only the seeded rules are read by code")
    if r["rule_id"] == "CN-01" and params.get("guardian_path") is not False:
        raise Invalid("CN-01: clippers are 18+ with no guardian path (founder-locked §0.1.1); refused")
    return copy.deepcopy(r)


# ------------------------------------------------------------------ weakening

# dotted parameter path -> direction in which a change WEAKENS the rule
#   "lower": a smaller number weakens; "higher": a larger number weakens; "false": true->false weakens;
#   "true": false->true weakens; "add": adding list items weakens; "remove": removing list items weakens;
#   "any": any change is flagged.
DIRECTIONS: dict[str, dict[str, str]] = {
    "CN-06": {"required": "false", "enabled_platforms": "add"},
    "CN-08": {"refuse_recipient_countries": "remove"},
    "CN-09": {"channels": "add"},
    "CN-10": {"t1.min_certified_clips": "lower", "t1.min_days_since_admission": "lower", "t1.max_active_s1": "higher",
              "t2.min_certified_clips": "lower", "t2.min_campaigns": "lower", "t2.min_median_certified_views": "lower",
              "t2.min_days_since_admission": "lower", "t2.no_upheld_strike_days": "lower",
              "t2.median_exclude_platforms": "remove", "t3.requires_andre_nomination": "false",
              "max_active_enrolments.T0": "higher", "max_active_enrolments.T1": "higher",
              "max_active_enrolments.T2": "higher", "max_active_enrolments.T3": "higher", "platform_anchors": "true"},
    "CN-12": {"jurisdiction_freshness_hours": "higher"},
    "CN-15": {"quiet_window": "any", "no_time_zone_channel": "any"},
    "CN-16": {"disclosure_text": "any", "required_in": "remove"},
    "CN-17": {"rate_notice_days": "lower"},
    "CN-18": {"appeal_window_days": "lower", "sla_business_days": "higher", "sla_push_business_day": "higher"},
    "CN-19": {"table.S2.days": "lower", "table.S2.tier_cap": "any", "table.S2.action": "any", "table.S1.action": "any",
              "table.S3.action": "any", "table.S3.ban_proposal": "false"},
    "CN-21": {"post_exit_retention_days": "higher", "agreement_retention_days": "lower"},
}


def _leaves(d: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(d, dict):
        out: dict[str, Any] = {}
        for k, v in d.items():
            out.update(_leaves(v, f"{prefix}.{k}" if prefix else k))
        return out
    return {prefix: d}


def rule_weakening(old: Optional[dict], new: Optional[dict]) -> list[str]:
    """Reasons a rule change may weaken the register (empty = not weakening)."""
    if old is None:
        return []                                             # a new rule adds; it cannot weaken
    if new is None or new["status"] == "retired":
        return ["rule_retired"]
    reasons = []
    if new["statement"] != old["statement"]:
        reasons.append("statement_changed")
    if new["kind"] != old["kind"]:
        reasons.append("kind_changed")
    if old["status"] == "in_force" and new["status"] != "in_force":
        reasons.append("status_left_in_force")
    dirs = DIRECTIONS.get(old["rule_id"], {})
    lo, ln = _leaves(old["parameters"]), _leaves(new["parameters"])
    for path in sorted(set(lo) | set(ln)):
        a, b = lo.get(path), ln.get(path)
        if a == b:
            continue
        d = dirs.get(path, "any")
        weak = (d == "any" or (d == "lower" and b < a) or (d == "higher" and b > a) or (d == "false" and a and not b)
                or (d == "true" and b and not a) or (d == "add" and bool(set(b) - set(a)))
                or (d == "remove" and bool(set(a) - set(b))))
        if weak:
            reasons.append(f"parameter_weakened:{path}")
    return reasons


def template_weakening(old: Optional[dict], new: Optional[dict]) -> list[str]:
    if old is None:
        return []
    if new is None:
        return ["template_retired"]
    reasons = []
    if set(new["channels"]) - set(old["channels"]):
        reasons.append("channel_added")
    if new["purpose"] != old["purpose"]:
        reasons.append("purpose_changed")
    if any(old["variables"].get(k) != v for k, v in new["variables"].items() if k in old["variables"]):
        reasons.append("variable_type_changed")
    if new["body"] != old["body"]:
        reasons.append("body_changed")
    return reasons


# ------------------------------------------------------------------ versions


@dataclass(frozen=True)
class Version:
    version: int
    created_at: str
    approved_by: str
    proposal_ids: tuple[str, ...]
    content_sha256: str
    prev_version_sha256: Optional[str]
    rules: tuple[dict, ...]
    templates: tuple[dict, ...]

    @property
    def meta(self) -> dict:
        return {"version": self.version, "created_at": self.created_at, "approved_by": self.approved_by,
                "proposal_ids": list(self.proposal_ids), "content_sha256": self.content_sha256,
                "prev_version_sha256": self.prev_version_sha256}

    @property
    def version_sha256(self) -> str:
        return sha(self.meta)

    def rule(self, rid: str) -> Optional[dict]:
        for r in self.rules:
            if r["rule_id"] == rid:
                return r
        return None

    def template(self, tid: str) -> Optional[dict]:
        for t in self.templates:
            if t["template_id"] == tid:
                return t
        return None


def content_sha256(rules: list[dict], templates: list[dict]) -> str:
    return sha({"rules": sorted(rules, key=lambda r: r["rule_id"]),
                "templates": sorted(templates, key=lambda t: t["template_id"])})


# ------------------------------------------------------------------ proposals


def seed_proposal(seed: dict, seed_sha: str, now: datetime) -> dict:
    rules = [validate_rule(r, r) for r in seed["rules"]]
    templates = [validate_template(t) for t in seed["templates"]]
    ids = [r["rule_id"] for r in rules]
    if len(set(ids)) != len(ids) or len({t["template_id"] for t in templates}) != len(templates):
        raise RuntimeError("seed has duplicate rule or template ids")
    for need in [f"CN-{n:02d}" for n in range(27)]:
        if need not in ids:
            raise RuntimeError(f"seed lacks {need}")
    p = {"proposal_id": "cn-prop-seed-" + seed_sha[:24], "kind": "seed", "target_id": None,
         "proposed": {"rules": rules, "templates": templates}, "diff": {"seed_sha256": seed_sha},
         "weakening": False, "weakening_reasons": [], "proposed_by": "seed", "created_at": iso(now), "evidence": None}
    p["content_sha256"] = proposal_sha(p)
    return p


def proposal_sha(p: dict) -> str:
    return sha({k: p[k] for k in ("proposal_id", "kind", "target_id", "proposed", "diff", "weakening",
                                  "weakening_reasons", "evidence")})


def build_rule_proposal(pid: str, kind: str, target_id: Optional[str], rule: Optional[dict], memo: Optional[dict],
                        current: Optional[Version], seed_rules: dict[str, dict], now: datetime) -> dict:
    if current is None:
        raise Conflict("no rule version is in force yet: Andre approves the seed first (GET /cn/v1/inbox)")
    old = current.rule(target_id) if target_id else None
    evidence = None
    if kind == "rule_new":
        if rule is None or target_id is not None:
            raise Invalid("rule_new carries a rule and no target_id")
        new = validate_rule(rule, None)
        if current.rule(new["rule_id"]) is not None or new["rule_id"] in seed_rules:
            raise Conflict(f"{new['rule_id']} already exists")
        if new["status"] != "in_force" or new["kind"] in ("counsel", "counsel_memo"):
            raise Invalid("a new rule starts in_force and is not a counsel row")
        target = new["rule_id"]
    elif kind == "rule_amend":
        if old is None or rule is None or rule.get("rule_id") != target_id:
            raise Invalid("rule_amend names an existing target_id and carries that rule")
        new = validate_rule(rule, seed_rules.get(target_id))
        if old["kind"] == "counsel" or new["kind"] != old["kind"] and "counsel" in (new["kind"], old["kind"]):
            raise Invalid("a counsel row changes only through a counsel_memo proposal (CN-23)")
        if new["status"] != old["status"]:
            raise Invalid("status changes go through rule_retire or counsel_memo")
        target = target_id
    elif kind == "rule_retire":
        if old is None or rule is not None:
            raise Invalid("rule_retire names an existing target_id and carries no rule")
        if target_id == "CN-00":
            raise Invalid("CN-00 cannot be retired (no decision without an Andre-approved rule version)")
        if old["kind"] == "counsel":
            raise Invalid("a counsel row is resolved only by a counsel_memo proposal (CN-23)")
        new = {**old, "status": "retired"}
        target = target_id
    else:  # counsel_memo
        if old is None or old["kind"] != "counsel" or old["status"] != "open":
            raise Invalid("counsel_memo resolves an OPEN counsel row (CN-CQ-nn)")
        if not isinstance(memo, dict) or set(memo) != {"memo_sha256", "memo_ref", "summary"}:
            raise Invalid("counsel_memo carries memo {memo_sha256, memo_ref, summary}")
        if not isinstance(memo["memo_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", memo["memo_sha256"]):
            raise Invalid("memo_sha256 must be 64 lowercase hex")
        _text(memo["memo_ref"], 200, "memo_ref")
        _text(memo["summary"], 500, "summary")
        if rule is not None:
            raise Invalid("counsel_memo carries no rule")
        new = {**old, "status": "resolved"}
        evidence = dict(memo)
        target = target_id
    reasons = [] if kind == "counsel_memo" else rule_weakening(old, new if kind != "rule_retire" else None)
    diff = {"old": old, "new": new}
    p = {"proposal_id": pid, "kind": kind, "target_id": target, "proposed": new, "diff": diff,
         "weakening": bool(reasons), "weakening_reasons": reasons, "proposed_by": "andre", "created_at": iso(now),
         "evidence": evidence}
    p["content_sha256"] = proposal_sha(p)
    return p


def build_template_proposal(pid: str, kind: str, target_id: Optional[str], template: Optional[dict],
                            current: Optional[Version], now: datetime) -> dict:
    if current is None:
        raise Conflict("no rule version is in force yet: Andre approves the seed first (GET /cn/v1/inbox)")
    old = current.template(target_id) if target_id else None
    if kind == "template_new":
        if template is None or target_id is not None:
            raise Invalid("template_new carries a template and no target_id")
        new = validate_template(template)
        if current.template(new["template_id"]) is not None:
            raise Conflict(f"template {new['template_id']} already exists; propose template_amend")
        if new["version"] != 1:
            raise Invalid("a new template starts at version 1")
        target = new["template_id"]
    elif kind == "template_amend":
        if old is None or template is None or template.get("template_id") != target_id:
            raise Invalid("template_amend names an existing target_id and carries that template")
        new = validate_template(template)
        if new["version"] != old["version"] + 1:
            raise Invalid(f"template_amend must be version {old['version'] + 1}")
        target = target_id
    else:  # template_retire
        if old is None or template is not None:
            raise Invalid("template_retire names an existing target_id and carries no template")
        new = None
        target = target_id
    reasons = template_weakening(old, new)
    p = {"proposal_id": pid, "kind": kind, "target_id": target, "proposed": new, "diff": {"old": old, "new": new},
         "weakening": bool(reasons), "weakening_reasons": reasons, "proposed_by": "andre", "created_at": iso(now),
         "evidence": None}
    p["content_sha256"] = proposal_sha(p)
    return p


def apply(current: Optional[Version], approvals: list[dict], recheck: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """New (rules, templates) after the approvals, applied in order. A proposal whose ``diff.old`` no longer
    equals the register is stale (409). Weakening is recomputed against the register NOW (N15-3) into
    ``recheck`` [proposal_id -> {proposal_id, weakening_reasons}]."""
    if any(p["kind"] == "seed" for p in approvals):
        if current is not None:
            raise Conflict("the seed was already approved")
        seed = next(p for p in approvals if p["kind"] == "seed")
        rules = copy.deepcopy(seed["proposed"]["rules"])
        templates = copy.deepcopy(seed["proposed"]["templates"])
        others = [p for p in approvals if p["kind"] != "seed"]
        if others:
            raise Conflict("approve the seed on its own first")
        return rules, templates
    if current is None:
        raise Conflict("no rule version is in force: approve the seed first")
    rules = {r["rule_id"]: copy.deepcopy(r) for r in current.rules}
    templates = {t["template_id"]: copy.deepcopy(t) for t in current.templates}
    for p in approvals:
        tgt, old = p["target_id"], p["diff"]["old"]
        if p["kind"].startswith("rule_") or p["kind"] == "counsel_memo":
            now_row = rules.get(tgt)
            if (old is None and now_row is not None) or (old is not None and now_row != old):
                raise Conflict(f"proposal {p['proposal_id']} is stale: {tgt} changed since it was drafted")
            if p["kind"] == "counsel_memo":
                reasons = []
            else:
                reasons = rule_weakening(now_row, p["proposed"] if p["kind"] != "rule_retire" else None)
            rules[tgt] = copy.deepcopy(p["proposed"])
        else:
            now_t = templates.get(tgt)
            if (old is None and now_t is not None) or (old is not None and now_t != old):
                raise Conflict(f"proposal {p['proposal_id']} is stale: template {tgt} changed since it was drafted")
            reasons = template_weakening(now_t, p["proposed"])
            if p["proposed"] is None:
                templates.pop(tgt, None)
            else:
                templates[tgt] = copy.deepcopy(p["proposed"])
        recheck[p["proposal_id"]] = {"proposal_id": p["proposal_id"], "weakening_reasons": reasons}
    return sorted(rules.values(), key=lambda r: r["rule_id"]), sorted(templates.values(), key=lambda t: t["template_id"])


def needs_acknowledgment(p: dict, re_: Optional[dict]) -> bool:
    now_reasons = (re_ or {}).get("weakening_reasons") or []
    return bool(p.get("weakening")) or bool(now_reasons) or sorted(now_reasons) != sorted(p.get("weakening_reasons") or [])


def param(version: Version, rule_id: str, path: str) -> Any:
    cur: Any = (version.rule(rule_id) or {}).get("parameters", {})
    for part in path.split("."):
        cur = cur[part]
    return cur
