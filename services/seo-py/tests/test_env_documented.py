"""service-py's fix-wave-25 check: every environment variable the service reads is documented. Reads every SEO_*
name spelled as a string literal under src/ and fails on any that neither README.md nor docs/adr/0017-*.md names
(exactly, or by a documented glob). A security-sensitive switch must be spelled out, never left to a glob."""

from __future__ import annotations

import re
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
PREFIX = "SEO_"
SECURITY = re.compile(r"ALLOW|ACCEPT|UNPINNED|UNSAFE|INSECURE|SKIP|DISABLE|BYPASS|NON_PRODUCTION")


def _documented_names() -> str:
    adr = next((SERVICE.parents[1] / "docs" / "adr").glob("0017-*.md"))
    return (SERVICE / "README.md").read_text(encoding="utf-8") + adr.read_text(encoding="utf-8")


def test_every_environment_variable_the_service_reads_is_documented():
    read = set()
    for p in sorted((SERVICE / "src").rglob("*.py")):
        read |= set(re.findall(r"""["'](%s[A-Z0-9_]+)["']""" % PREFIX, p.read_text(encoding="utf-8")))
    docs = _documented_names()
    globs = [g for g in re.findall(r"(%s[A-Z0-9_]*)\*([A-Z0-9_]*)" % PREFIX, docs) if len(g[0]) > len(PREFIX) + 1
             or g[1]]

    def documented(name: str) -> bool:
        if re.search(r"(?<![A-Z0-9_])%s(?![A-Z0-9_])" % re.escape(name), docs):
            return True
        if SECURITY.search(name):
            return False
        return any(name.startswith(head) and name.endswith(tail) and len(name) > len(head) + len(tail)
                   for head, tail in globs)
    assert read, "no SEO_* variable found under src/ (the scan itself is broken)"
    missing = sorted(n for n in read if not documented(n))
    assert not missing, f"read under src/ but documented in neither README.md nor ADR 0017: {missing}"


def test_every_not_built_switch_is_named_in_the_readme():
    import sys
    sys.path.insert(0, str(SERVICE / "src"))
    import config
    readme = (SERVICE / "README.md").read_text(encoding="utf-8")
    assert all(name in readme for name in config.NOT_BUILT)
