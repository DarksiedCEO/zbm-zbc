"""Every environment variable the service reads is documented (sales-py's fix wave 25 test): every INF_* name spelled as
a string literal under src/ must be named in README.md or docs/adr/0015-*.md."""

from __future__ import annotations

import re
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
PREFIX = "INF_"
SECURITY = re.compile(r"ALLOW|ACCEPT|UNPINNED|UNSAFE|INSECURE|SKIP|DISABLE|BYPASS|NON_PRODUCTION")


def _documented_names() -> str:
    adr = next((SERVICE.parents[1] / "docs" / "adr").glob("0015-*.md"))
    return (SERVICE / "README.md").read_text(encoding="utf-8") + adr.read_text(encoding="utf-8")


def test_every_environment_variable_the_service_reads_is_documented():
    read = set()
    for p in sorted((SERVICE / "src").rglob("*.py")):
        read |= set(re.findall(r"""["'](%s[A-Z0-9_]+)["']""" % PREFIX, p.read_text(encoding="utf-8")))
    docs = _documented_names()
    globs = [g for g in re.findall(r"(%s[A-Z0-9_]*)\*([A-Z0-9_]*)" % PREFIX, docs) if len(g[0]) > len(PREFIX) + 1 or g[1]]

    def documented(name: str) -> bool:
        if re.search(r"(?<![A-Z0-9_])%s(?![A-Z0-9_])" % re.escape(name), docs):
            return True
        if SECURITY.search(name):
            return False
        return any(name.startswith(head) and name.endswith(tail) and len(name) > len(head) + len(tail)
                   for head, tail in globs)
    assert read, "no INF_* variable found under src/ (the scan itself is broken)"
    missing = sorted(n for n in read if not documented(n))
    assert not missing, f"read under src/ but documented in neither README.md nor ADR 0015: {missing}"
