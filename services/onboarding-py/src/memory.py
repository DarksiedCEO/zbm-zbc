"""
The three kinds of memory from the locked spec, kept apart:

- ClientMemoryStore — one wall per client (P11). Reads and writes always
  take a client_id; there is no API that returns more than one client's
  memory. The client can see it and request deletion; at exit it is
  exported or destroyed (P4).
- InstitutionalMemory — patterns across clients with names and
  identifiers STRIPPED before anything is stored.
- Playbook — approved rules only. Only Andre adds or changes a rule: a
  change needs an explicit approval token (HMAC-SHA256 over the exact
  proposal, keyed by ONBOARDING_ANDRE_APPROVAL_KEY). No key configured =>
  no change is possible (fail closed). Every version is kept forever.
  Memory never rewrites the playbook: learning-loop output can only
  create a *proposal*.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from redaction import scrub_obj


class MemoryWallViolation(PermissionError):
    pass


class ClientMemoryStore:
    def __init__(self) -> None:
        self._walls: dict[str, dict[str, Any]] = {}

    def put(self, client_id: str, key: str, value: Any) -> None:
        self._walls.setdefault(client_id, {})[key] = copy.deepcopy(scrub_obj(value))

    def get(self, client_id: str, key: str, default: Any = None) -> Any:
        return copy.deepcopy(self._walls.get(client_id, {}).get(key, default))

    def view(self, client_id: str) -> dict[str, Any]:
        """What the client can see (P11): only their own wall."""
        return copy.deepcopy(self._walls.get(client_id, {}))

    def delete(self, client_id: str) -> bool:
        return self._walls.pop(client_id, None) is not None

    def export(self, client_id: str) -> dict[str, Any]:
        return {"client_id": client_id, "memory": self.view(client_id)}

    def clients(self) -> int:
        return len(self._walls)


_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|net|org|io|co|shop|store)\b")
_LONG_NUM = re.compile(r"\b\d{5,}\b")
_ID_KEYS = {
    "client_id", "creator_id", "brand_id", "name", "business_name", "legal_name", "email", "phone",
    "account_id", "account_name", "website", "url", "contact", "signer", "login_holder", "address",
    "customer_id", "entity_id", "order_id", "finding_id",
}


def strip_identifiers(obj: Any, known_names: tuple[str, ...] = ()) -> Any:
    if isinstance(obj, dict):
        return {k: strip_identifiers(v, known_names) for k, v in obj.items() if k not in _ID_KEYS}
    if isinstance(obj, (list, tuple)):
        return [strip_identifiers(v, known_names) for v in obj]
    if isinstance(obj, str):
        s = _EMAIL.sub("[email]", obj)
        s = _URL.sub("[url]", s)
        s = _PHONE.sub("[phone]", s)
        s = _LONG_NUM.sub("[number]", s)
        for n in known_names:
            if n:
                s = re.sub(re.escape(n), "[client]", s, flags=re.IGNORECASE)
        return s
    return obj


class InstitutionalMemory:
    def __init__(self) -> None:
        self._patterns: list[dict] = []

    def record(self, pattern: dict, known_names: tuple[str, ...] = ()) -> dict:
        clean = strip_identifiers(scrub_obj(pattern), known_names)
        self._patterns.append(clean)
        return clean

    def patterns(self) -> list[dict]:
        return copy.deepcopy(self._patterns)


def proposal_digest(rule_id: str, version: int, text: str) -> str:
    return hashlib.sha256(json.dumps([rule_id, version, text], separators=(",", ":")).encode()).hexdigest()


def approval_token(key: str, rule_id: str, version: int, text: str) -> str:
    """What Andre's approval tool would produce. Exposed for tests and for
    whatever signing tool Andre is given; the service never mints one for
    itself at runtime."""
    return hmac.new(key.encode(), proposal_digest(rule_id, version, text).encode(), hashlib.sha256).hexdigest()


class PlaybookApprovalError(PermissionError):
    pass


class AndreApprovalError(PermissionError):
    pass


def andre_action_digest(action: str, *fields: str) -> str:
    return hashlib.sha256(json.dumps(["andre_action", action, *fields], separators=(",", ":")).encode()).hexdigest()


def andre_action_token(key: str, action: str, *fields: str) -> str:
    """Andre's approval for ONE exact action (e.g. resolving escalation X of
    client Y with this resolution text): HMAC-SHA256 keyed by
    ONBOARDING_ANDRE_APPROVAL_KEY. Same mechanism as the playbook approval
    token; a token for one action/escalation/text is useless for another."""
    return hmac.new(key.encode(), andre_action_digest(action, *fields).encode(), hashlib.sha256).hexdigest()


def verify_andre_token(key: Optional[str], expected_fn, token: Optional[str]) -> None:
    """Constant-time check of an Andre approval token. No key configured =>
    nothing attributed to Andre is possible (fail closed)."""
    if not key:
        raise AndreApprovalError("no Andre approval key configured (ONBOARDING_ANDRE_APPROVAL_KEY); refused")
    expected = expected_fn(key)
    try:
        ok = isinstance(token, str) and hmac.compare_digest(token, expected)
    except TypeError:
        ok = False
    if not ok:
        raise AndreApprovalError("missing or invalid Andre approval token for this exact action")


@dataclass
class PlaybookRule:
    rule_id: str
    version: int
    text: str
    approved_at: datetime
    approval_digest: str


@dataclass
class Playbook:
    approval_key: Optional[str] = None
    history: list[PlaybookRule] = field(default_factory=list)

    def current(self) -> dict[str, PlaybookRule]:
        cur: dict[str, PlaybookRule] = {}
        for r in self.history:
            cur[r.rule_id] = r
        return cur

    def next_version(self, rule_id: str) -> int:
        cur = self.current().get(rule_id)
        return 1 if cur is None else cur.version + 1

    def check_approval(self, rule_id: str, version: int, text: str, token: Optional[str]) -> None:
        if not self.approval_key:
            raise PlaybookApprovalError(
                "no Andre approval key configured (ONBOARDING_ANDRE_APPROVAL_KEY); playbook cannot change"
            )
        if version != self.next_version(rule_id):
            raise PlaybookApprovalError(f"version must be {self.next_version(rule_id)} for {rule_id}")
        expected = approval_token(self.approval_key, rule_id, version, text)
        try:
            ok = token is not None and hmac.compare_digest(token, expected)
        except TypeError:
            ok = False
        if not ok:
            raise PlaybookApprovalError("missing or invalid Andre approval token for this exact rule text and version")

    def append(self, rule_id: str, version: int, text: str, now: datetime) -> PlaybookRule:
        """Only call after check_approval AND the ledger write succeeded."""
        rule = PlaybookRule(rule_id, version, text, now, proposal_digest(rule_id, version, text))
        self.history.append(rule)
        return rule
