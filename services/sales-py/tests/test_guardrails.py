"""Static guardrails (finance-py's G1 rule; the closed reason catalogue)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
# the launcher and the shared byte-identical modules carry timing floats, never money
NOT_MONEY = {"serve.py", "graceful_close.py", "launch_guard.py"}


def test_g1_no_float_anywhere_in_src():
    for f in sorted(SRC.rglob("*.py")):
        if f.name in NOT_MONEY:
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            assert not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "float"), f"float() in {f}"
            assert not (isinstance(node, ast.Constant) and isinstance(node.value, float)), \
                f"float literal {getattr(node, 'value', '')} in {f}:{node.lineno}"


def test_every_reason_code_used_is_catalogued():
    import reasons
    used = set()
    for f in sorted(SRC.rglob("*.py")):
        used |= set(re.findall(r'R\("([A-Z_]+)"\)', f.read_text()))
    assert used <= reasons.CODES


def test_quote_problem_and_routing_codes_are_catalogued():
    import reasons
    from intelligences import i11_pricing
    text = (SRC / "intelligences" / "i11_pricing.py").read_text() + (SRC / "intelligences" / "i04_routing.py").read_text()
    codes = set(re.findall(r'(?:QuoteProblem|return None,)\s*\(?"([A-Z_]+)"', text))
    assert codes and codes <= reasons.CODES
    assert i11_pricing.QuoteProblem("X").code == "X"


def test_send_problem_codes_are_catalogued():
    import reasons
    text = (SRC / "svc_outreach.py").read_text()
    for code in re.findall(r'return "([A-Z_]+)", None', text):
        assert code in reasons.CODES or code in ("OUTREACH_DOMAIN_CHANGED", "RENDERED_CONTENT_CHANGED")
