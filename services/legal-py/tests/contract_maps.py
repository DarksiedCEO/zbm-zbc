"""
The §E.1 thin-client mappings, applied to Legal's real answers using the callers' OWN answer dataclasses (loaded
from their source files under private module names, so no service imports another at runtime). These are the
mappings each caller's future ``HttpLegal37`` must implement (spec "Changes other services must make"); the
tests prove Legal's wire shapes support them. Any failure (non-200, unreadable body) maps to the negative answer.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ports(service: str, alias: str, rel: str = "src/ports.py"):
    return sys.modules.get(alias) or _load(alias, SERVICES / service / rel)


def compliance_doc_version(resp):
    P = _ports("compliance-py", "_cmp_ports")
    if resp.status_code != 200:
        return P.DocVersion(False, reason="legal_37 unavailable")
    b = resp.json()
    return P.DocVersion(bool(b["available"] and b["current_version"] is not None), b["current_version"], b["reason"])


def cn_doc_version(resp):
    P = _ports("clipper-network-py", "_cn_ports")
    if resp.status_code != 200:
        return P.DocVersionAnswer(False, reason="legal_37 unavailable")
    b = resp.json()
    ok = bool(b["available"] and b["current_version"])
    return P.DocVersionAnswer(ok, b["current_version"], b["doc_sha256"], b["reason"])


def vi_takedowns(resp):
    P = _ports("verification-py", "_vi_ports")
    if resp.status_code != 200:
        return P.TakedownAnswer(False)
    b = resp.json()
    if not isinstance(b.get("notices"), int) or b.get("available") is not True:
        return P.TakedownAnswer(False)
    return P.TakedownAnswer(True, b["notices"])


def creative_signoff(resp):
    # departments.py imports only the standard library; load it standalone
    D = sys.modules.get("_cr_departments") or _load("_cr_departments",
                                                    SERVICES / "creative-py" / "src" / "shared" / "departments.py")
    if resp.status_code != 200:
        return D.GateResult("legal_37", False, "legal_37 unavailable")
    b = resp.json()
    return D.GateResult("legal_37", b["allowed"] is True, b["reason"], b["reference"])
