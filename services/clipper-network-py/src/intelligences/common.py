"""
Decision items (spec §A wire shape) shared by every intelligence.

``{"code", "rule_id", "message", "source", "evidence_ref"}``; ``unmet_lines``
render as ``cn/{rule_id}/{code}: {message}`` (≤ 400 chars). Every adverse item
cites a rule id; ``Citer`` makes sure that id is IN FORCE in the version the
decision is made under (guardrail G4): an item citing a rule that is absent
or retired is replaced by ``CN-00 / RULE_NOT_IN_FORCE:<id>`` (the decision
stays negative — fail closed).
"""

from __future__ import annotations

from typing import Optional

LINE_MAX = 400
MESSAGE_MAX = 300


def item(code: str, rule_id: str, message: str, source: str = "clipper_network",
         evidence_ref: Optional[str] = None) -> dict:
    return {"code": code[:80], "rule_id": rule_id, "message": " ".join(str(message).split())[:MESSAGE_MAX],
            "source": source, "evidence_ref": evidence_ref}


def unavailable(port: str, rule_id: str, what: str) -> dict:
    return item(f"DEPENDENCY_UNAVAILABLE:{port}", rule_id, f"{what}: {port} did not give a usable answer (fail closed)",
                port)


def unmet_line(u: dict) -> str:
    return f"cn/{u['rule_id']}/{u['code']}: {u['message']}"[:LINE_MAX]


def finalize(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for u in items:
        key = (u["rule_id"], u["code"], u["message"], u["evidence_ref"])
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


class Citer:
    """Maps an item's rule id to the version in force (G4)."""

    def __init__(self, version):
        self.version = version

    def in_force(self, rule_id: str) -> bool:
        if self.version is None:
            return False
        r = self.version.rule(rule_id)
        if r is None:
            return False
        if r["kind"] == "counsel":
            return r["status"] == "open"
        return r["status"] == "in_force"

    def check(self, items: list[dict]) -> list[dict]:
        out = []
        for u in items:
            if self.in_force(u["rule_id"]):
                out.append(u)
            else:
                out.append(item(f"RULE_NOT_IN_FORCE:{u['rule_id']}", "CN-00",
                                f"the rule this check cites ({u['rule_id']}) is not in force in the rule version "
                                f"in force; decision refused ({u['code']})", u["source"], u["evidence_ref"]))
        return out


def rules_not_in_force() -> dict:
    return item("RULES_NOT_IN_FORCE", "CN-00", "no Andre-approved rule version is in force; every decision is negative")
