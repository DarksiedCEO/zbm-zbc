"""
llms.txt shape oracle, from the llmstxt.org proposal: a Markdown file with one H1 title (``# ``), an optional
blockquote summary, then H2 sections (``## ``) holding Markdown link lists (``- [name](url)`` with an optional
``: notes``). Issues from the closed set of SPEC B-DE-14. A link is fine only when it is an absolute http(s) URL.
"""

from __future__ import annotations

ISSUES = ("NO_H1_TITLE", "MORE_THAN_ONE_H1", "NO_H2_SECTIONS", "NO_LINK_LISTS", "RELATIVE_OR_NON_HTTP_LINKS")


def _link(line: str):
    """``- [name](url)...`` -> url, else None (a hand-written reader)."""
    s = line.strip()
    if not (s.startswith("- [") or s.startswith("* [")):
        return None
    close = s.find("](", 3)
    if close < 0:
        return None
    end = s.find(")", close + 2)
    if end < 0:
        return None
    return s[close + 2:end].strip()


def issues(text: str) -> list:
    """The proposal: the file BEGINS with the H1 (first non-blank line); any later H1 is a second one."""
    body = [ln for ln in text.splitlines() if ln.strip()]
    first_h1 = bool(body) and body[0].startswith("# ")
    h1_later = h2 = links = 0
    bad = False
    for k, line in enumerate(body):
        if line.startswith("# ") and not (k == 0 and first_h1):
            h1_later += 1
        elif line.startswith("## "):
            h2 += 1
        u = _link(line)
        if u is not None:
            links += 1
            low = u.lower()
            if not (low.startswith("http://") or low.startswith("https://")):
                bad = True
    out = set()
    if not first_h1:
        out.add("NO_H1_TITLE")
    if h1_later:
        out.add("MORE_THAN_ONE_H1")
    if h2 == 0:
        out.add("NO_H2_SECTIONS")
    if links == 0:
        out.add("NO_LINK_LISTS")
    if bad:
        out.add("RELATIVE_OR_NON_HTTP_LINKS")
    return sorted(out)


def self_test() -> None:
    ok = "# T\n\n> s\n\n## Docs\n- [A](https://x.test/a): n\n"
    assert issues(ok) == []
    assert issues("## Docs\n- [A](https://x.test/a)\n") == ["NO_H1_TITLE"]
    assert "RELATIVE_OR_NON_HTTP_LINKS" in issues("# T\n## D\n- [A](/a)\n")
