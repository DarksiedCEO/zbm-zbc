"""Fix wave 25 (scout B M6 and its class): every environment variable the service reads is documented.

Scout B found switches an operator could not learn about — CN_VI_ACCEPT_UNPINNED / CN_COMPLIANCE_ACCEPT_UNPINNED
(answers from an unpinned peer accepted), the DLV_ALLOW_* refusal switches, the serve.py tuning knobs — read by src/
and named in no README or ADR. This test reads every SALES_* name spelled as a string literal under src/ and fails on
any that neither README.md nor docs/adr/0013-*.md names (exactly, or by a documented glob such as `LEGAL_TIER_*`)."""

from __future__ import annotations

import re
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
PREFIX = "SALES_"


# names that weaken a check or unlock something: never covered by a documented glob (`CN_COMPLIANCE_*` used to
# "document" CN_COMPLIANCE_ACCEPT_UNPINNED without ever naming it)
SECURITY = re.compile(r"ALLOW|ACCEPT|UNPINNED|UNSAFE|INSECURE|SKIP|DISABLE|BYPASS|NON_PRODUCTION")


def _documented_names() -> str:
    adr = next((SERVICE.parents[1] / "docs" / "adr").glob("0013-*.md"))
    return (SERVICE / "README.md").read_text(encoding="utf-8") + adr.read_text(encoding="utf-8")


def test_every_environment_variable_the_service_reads_is_documented():
    read, prefixes = set(), set()
    for p in sorted((SERVICE / "src").rglob("*.py")):
        text = p.read_text(encoding="utf-8")
        read |= set(re.findall(r"""["'](%s[A-Z0-9_]+)["']""" % PREFIX, text))
        # a thin client's prefix (`_client(env, "CN_VI", ...)` reads CN_VI_URL, CN_VI_SERVICE_TOKEN, ...): documented
        # when the names it builds are
        prefixes |= set(re.findall(r"""_(?:client|thin)\(env,\s*["'](%s[A-Z0-9_]+)["']""" % PREFIX, text))
    read -= prefixes
    docs = _documented_names()
    undocumented_prefixes = sorted(x for x in prefixes if not re.search(re.escape(x) + r"_[A-Z*]", docs))
    assert not undocumented_prefixes, f"thin-client variables documented nowhere: {undocumented_prefixes}_*"
    globs = [g for g in re.findall(r"(%s[A-Z0-9_]*)\*([A-Z0-9_]*)" % PREFIX, docs) if len(g[0]) > len(PREFIX) + 1 or g[1]]

    def documented(name: str) -> bool:
        if re.search(r"(?<![A-Z0-9_])%s(?![A-Z0-9_])" % re.escape(name), docs):
            return True
        if SECURITY.search(name):
            return False          # a security switch is spelled out where an operator reads it, never left to a glob
        return any(name.startswith(head) and name.endswith(tail) and len(name) > len(head) + len(tail)
                   for head, tail in globs)
    assert read, "no SALES_* variable found under src/ (the scan itself is broken)"
    missing = sorted(n for n in read if not documented(n))
    assert not missing, f"read under src/ but documented in neither README.md nor ADR 0013: {missing}"
