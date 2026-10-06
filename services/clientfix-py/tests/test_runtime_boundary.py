"""The fire-team runtime boundary (founder decisions 1-2, ADR 0017 decision 4).

This service never embeds an agent runtime: the engineers run inside delivery-py's deer-flow harness behind its
sandbox, guardrail and egress adapters, and only their change sets reach this service, as untrusted data. So nothing
under src/ may import deer-flow, LangGraph or LangChain, any model SDK, or the Elastic-2.0 LangGraph Studio packages
(``langgraph-api`` / ``langgraph-runtime-inmem``), and the Studio cut in delivery-py — the runtime the fire teams use —
must stay in force: both packages overridden out of its lock and refused by its start-up gate and licence gate."""

from __future__ import annotations

import ast
import re
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
DELIVERY = SERVICE.parent / "delivery-py"
FORBIDDEN_IMPORTS = ("deerflow", "deerflow_extension_api", "langgraph", "langgraph_api", "langgraph_runtime_inmem",
                     "langchain", "langchain_core", "langchain_anthropic", "anthropic", "openai", "zbm_delivery")


def _imports(path: Path) -> set:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def test_no_agent_runtime_or_model_sdk_is_imported_by_this_service():
    found = {}
    for p in sorted((SERVICE / "src").rglob("*.py")):
        bad = _imports(p) & set(FORBIDDEN_IMPORTS)
        if bad:
            found[str(p.relative_to(SERVICE))] = sorted(bad)
    assert not found, found


def test_requirements_pin_only_the_api_layer():
    names = {re.split(r"[=<>\[ ]", ln.strip())[0].lower() for ln in (SERVICE / "requirements.txt").read_text()
             .splitlines() if ln.strip() and not ln.startswith("#")}
    assert names == {"pydantic", "pytest", "fastapi", "uvicorn", "httpx"}


def test_delivery_runtime_keeps_the_elastic_studio_path_cut():
    pyproject = (DELIVERY / "pyproject.toml").read_text()
    for pkg in ("langgraph-api", "langgraph-runtime-inmem", "langgraph-cli"):
        assert f"\"{pkg} ; sys_platform == 'never'\"" in pyproject, pkg
    gate = (DELIVERY / "src" / "zbm_delivery" / "gate.py").read_text()
    assert re.search(r"FORBIDDEN_MODULES = \([^)]*\"langgraph_api\"", gate)
    allow = (DELIVERY / "seed" / "licence_allowlist.json").read_text()
    for pkg in ("langgraph-api", "langgraph-runtime-inmem"):
        assert f'"{pkg}"' in allow.split('"forbidden_distributions"', 1)[1].split("]", 1)[0]


def test_the_engineer_brief_never_carries_a_credential(h):
    conn, j = h.seo_job(approve=False)
    with h.svc.lock:
        brief = h.svc.brief(j["job_id"])
    blob = repr(brief)
    assert "vault:" not in blob and "token" not in blob.lower()
    assert brief["items"][0]["allowed_ops"] == sorted(h.svc.connectors["shopify"].ops)
