"""
The 15 single-task intelligences (locked spec, "Intelligence layer").

One job each, one file each. Each module declares NUMBER, NAME, PHASE and
STATUS. Nothing here calls a model or the network: every decision is an
explicit rule over provided inputs. No intelligence calls another
intelligence or a department directly; the service layer (service.py)
passes results between them and writes every crossing to the ledger.

Certification status is stated honestly: certification needs all four
test types (scenario, attack, guardrail, independent review). This build
supplies the first three; independent review (type 4) happens outside this
workstream, so NO intelligence is certified for real clients yet.
"""

from importlib import import_module

MODULES = [
    "i01_client_understanding",
    "i02_conversation",
    "i03_priority_fusion",
    "i04_platform_access",
    "i05_setup",
    "i06_audit_baseline",
    "i07_momentum_moment",
    "i08_risk_anomaly",
    "i09_promise_keeper",
    "i10_escalation_briefing",
    "i11_creator_vetting",
    "i12_brand_campaign",
    "i13_learning_loop",
    "i14_contract_obligation",
    "i15_compliance",
]


def registry() -> list[dict]:
    out = []
    for m in MODULES:
        mod = import_module(f"intelligences.{m}")
        out.append({"number": mod.NUMBER, "name": mod.NAME, "phase": mod.PHASE, "status": mod.STATUS})
    return out
