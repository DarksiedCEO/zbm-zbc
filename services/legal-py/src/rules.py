"""
The Legal rule register (spec §B.12) — rules are data, only Andre changes them. Copied from verification-py
(the Compliance B.5/B.6 pattern):
- rows are validated strictly (unknown keys, ``approved_by``/``in_force``/``rules_version`` → 422);
- a version is immutable and chained by ``prev_version_sha256``; ``rows_sha256`` is the canonical hash;
- proposals (seed | add | amend | retire) carry a ``content_sha256``; a decision must quote it (409 on a
  changed proposal) and an amend/retire must still match the row it was drafted against (409 stale);
- every proposal is flagged ``weakening`` with reasons; approving one needs ``acknowledge_weakening``;
- founder rules (§0.1) can never be weakened or retired, and no rule the reason catalog cites can be retired
  (G3: every negative answer cites a rule in the version in force) — today that is every LG rule.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from errors import Conflict, Invalid
from reasons import CATALOG

RULE_ID_RE = re.compile(r"LG-[0-9]{2}[a-z]?")
URL_RE = re.compile(r"https://[^\s\x00-\x1f\x7f]{3,490}")
KINDS = ("founder", "lead_default", "research", "spec_choice")
STATUSES = ("verified", "unverified")
ROW_KEYS = ("rule_id", "title", "statement", "source_urls", "kind", "parameters", "status")
SERVER_ONLY = ("approved_by", "approved_at", "in_force", "rules_version")
PROPOSAL_KINDS = ("add", "amend", "retire")
CODE_CITED = frozenset(CATALOG.values())
MAX_ROWS = 500


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8", "surrogatepass")).hexdigest()


def rows_sha256(rows: list[dict]) -> str:
    return sha(sorted(rows, key=lambda r: r["rule_id"]))


def _scalar_ok(v: Any, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if isinstance(v, bool) or v is None or isinstance(v, int):
        return True
    if isinstance(v, str):
        return len(v) <= 200
    if isinstance(v, list):
        return len(v) <= 50 and all(_scalar_ok(x, depth + 1) for x in v)
    if isinstance(v, dict):
        return len(v) <= 50 and all(isinstance(k, str) and len(k) <= 64 and _scalar_ok(x, depth + 1)
                                    for k, x in v.items())
    return False   # floats refused: thresholds live in config, never as floats in rule data


def validate_row(row: Any) -> dict:
    if not isinstance(row, dict):
        raise Invalid("a rule row must be an object")
    bad = [k for k in row if k in SERVER_ONLY]
    if bad:
        raise Invalid(f"server-set field(s) may not be sent: {', '.join(sorted(bad))}")
    extra = sorted(set(row) - set(ROW_KEYS))
    missing = sorted(set(ROW_KEYS) - set(row))
    if extra or missing:
        raise Invalid(f"rule row keys: unknown {extra[:5]}, missing {missing[:5]}")
    if not isinstance(row["rule_id"], str) or not RULE_ID_RE.fullmatch(row["rule_id"]):
        raise Invalid("rule_id format (LG-NN or LG-NNa)")
    for k, n in (("title", 240), ("statement", 2000)):
        v = row[k]
        if not isinstance(v, str) or not v.strip() or len(v) > n or re.search(r"[\x00-\x1f\x7f-\x9f]", v):
            raise Invalid(f"{k} must be 1..{n} printable characters")
    urls = row["source_urls"]
    if not isinstance(urls, list) or len(urls) > 10 or not all(isinstance(u, str) and URL_RE.fullmatch(u) for u in urls):
        raise Invalid("source_urls: at most 10 https URLs")
    if row["kind"] not in KINDS:
        raise Invalid(f"kind must be one of {', '.join(KINDS)}")
    if row["status"] not in STATUSES:
        raise Invalid("status must be verified or unverified")
    if not isinstance(row["parameters"], dict) or not _scalar_ok(row["parameters"]):
        raise Invalid("parameters: a small object of strings, integers, booleans and lists (no floats)")
    return {k: row[k] for k in ROW_KEYS}


@dataclass(frozen=True)
class Version:
    version: int
    created_at: str
    approved_by: str
    proposal_ids: tuple
    rows_sha256: str
    prev_version_sha256: Optional[str]
    rows: tuple

    @property
    def meta(self) -> dict:
        return {"version": self.version, "created_at": self.created_at, "approved_by": self.approved_by,
                "proposal_ids": list(self.proposal_ids), "rows_sha256": self.rows_sha256,
                "prev_version_sha256": self.prev_version_sha256}

    @property
    def version_sha256(self) -> str:
        return sha(self.meta)

    def by_id(self) -> dict[str, dict]:
        return {r["rule_id"]: r for r in self.rows}


def load_seed(seed_bytes: bytes) -> list[dict]:
    doc = json.loads(seed_bytes)
    rows = [validate_row(r) for r in doc["rows"]]
    ids = [r["rule_id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise RuntimeError("seed has duplicate rule ids")
    missing = sorted(CODE_CITED - set(ids))
    if missing:
        raise RuntimeError(f"seed lacks rules the code cites: {missing}")
    return rows


def weakening_reasons(kind: str, old: Optional[dict], new: Optional[dict]) -> list[str]:
    if kind == "seed":
        return []
    if kind == "retire":
        return ["retire"]
    if kind == "add":
        return []
    out = []
    assert old is not None and new is not None
    if old["status"] == "unverified" and new["status"] == "verified":
        out.append("unverified_to_verified")
    if set(old["source_urls"]) - set(new["source_urls"]):
        out.append("sources_removed")
    if canonical(old["parameters"]) != canonical(new["parameters"]):
        out.append("parameters_changed")
    if old["statement"] != new["statement"] or old["title"] != new["title"]:
        out.append("text_changed")
    if old["kind"] != new["kind"]:
        out.append("kind_changed")
    return out


def build_proposal(kind: str, target_id: Optional[str], proposed_row: Optional[dict], current: Optional[dict[str, dict]],
                   proposed_by: str, created_at: str, proposal_id: str) -> dict:
    if kind not in PROPOSAL_KINDS:
        raise Invalid(f"kind must be one of {', '.join(PROPOSAL_KINDS)}")
    if current is None:
        raise Conflict("no rule version is in force yet: approve the seed first")
    old = None
    new = None
    if kind == "add":
        if target_id is not None:
            raise Invalid("an add proposal has no target_id (the row names its rule_id)")
        new = validate_row(proposed_row)
        if new["rule_id"] in current:
            raise Conflict(f"{new['rule_id']} already exists; propose an amend")
        target_id = new["rule_id"]
    else:
        if not isinstance(target_id, str) or target_id not in current:
            raise Invalid("target_id must name a rule in the version in force")
        old = current[target_id]
        if kind == "amend":
            new = validate_row(proposed_row)
            if new["rule_id"] != target_id:
                raise Invalid("an amend keeps the rule_id")
            if canonical(new) == canonical(old):
                raise Invalid("the proposed row is identical to the rule in force")
        else:
            if proposed_row is not None:
                raise Invalid("a retire proposal carries no proposed_row")
            if target_id in CODE_CITED:
                raise Invalid(f"{target_id} is cited by the service's reason catalog and cannot be retired")
    reasons = weakening_reasons(kind, old, new)
    if old is not None and old["kind"] == "founder" and reasons:
        raise Invalid(f"{target_id} is founder-locked (spec §0.1): no change may weaken it ({', '.join(reasons)})")
    body = {"proposal_id": proposal_id, "kind": kind, "target_id": target_id, "proposed_row": new,
            "base_row_sha256": sha(old) if old is not None else None, "proposed_by": proposed_by,
            "created_at": created_at, "weakening": bool(reasons), "weakening_reasons": reasons}
    body["content_sha256"] = sha(body)
    return body


def seed_proposal(rows: list[dict], seed_sha: str, created_at: str, proposal_id: str) -> dict:
    body = {"proposal_id": proposal_id, "kind": "seed", "target_id": None, "proposed_rows": rows,
            "seed_sha256": seed_sha, "base_row_sha256": None, "proposed_by": "seed", "created_at": created_at,
            "weakening": False, "weakening_reasons": []}
    body["content_sha256"] = sha(body)
    return body


def apply(base: Optional[list[dict]], approvals: list[dict]) -> list[dict]:
    """New rows from approved proposals (atomic; raises Conflict on a stale proposal)."""
    if any(p["kind"] == "seed" for p in approvals):
        if base is not None:
            raise Conflict("the seed can be approved only while no rule version is in force")
        if len(approvals) != 1:
            raise Conflict("approve the seed alone")
        return [dict(r) for r in approvals[0]["proposed_rows"]]
    if base is None:
        raise Conflict("no rule version is in force: approve the seed first")
    rows = {r["rule_id"]: dict(r) for r in base}
    for p in approvals:
        tid = p["target_id"]
        if p["kind"] == "add":
            if tid in rows:
                raise Conflict(f"proposal {p['proposal_id']} is stale: {tid} exists now")
            rows[tid] = dict(p["proposed_row"])
            continue
        if tid not in rows or sha(rows[tid]) != p["base_row_sha256"]:
            raise Conflict(f"proposal {p['proposal_id']} is stale: {tid} changed since it was drafted")
        if p["kind"] == "amend":
            rows[tid] = dict(p["proposed_row"])
        else:
            del rows[tid]
    if len(rows) > MAX_ROWS:
        raise Invalid(f"at most {MAX_ROWS} rules")
    return sorted(rows.values(), key=lambda r: r["rule_id"])
