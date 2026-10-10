"""
robots.txt parsing and matching, RFC 9309 (stdlib only; ``urllib.robotparser`` is not used: it does not implement
the RFC's longest-match rule or the ``*`` / ``$`` wildcards).

- groups start at one or more ``user-agent`` lines; ``allow`` / ``disallow`` rules belong to the current group;
  rules before any user-agent line are ignored; unknown lines are ignored; ``sitemap`` lines are global;
- a crawler uses the group(s) whose user-agent value matches its product token case-insensitively (all such groups
  are merged); if none, the ``*`` group(s); if none, everything is allowed;
- the most specific (longest) matching rule wins; on equal length ``allow`` wins; an empty ``disallow`` value
  matches nothing; ``/robots.txt`` is always allowed;
- paths are compared after percent-encoding normalisation of unreserved characters only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

MAX_LINES = 20_000
MAX_RULE = 2000


@dataclass
class Group:
    agents: list = field(default_factory=list)
    rules: list = field(default_factory=list)        # (allow: bool, pattern: str)


@dataclass
class Robots:
    groups: list = field(default_factory=list)
    sitemaps: list = field(default_factory=list)
    lines_ignored: int = 0
    lines_total: int = 0

    def group_for(self, token: str) -> tuple[list, str]:
        """The rules that apply to ``token`` and which group kind matched: "named", "star" or "none"."""
        t = token.lower()
        named = [g for g in self.groups if any(a.lower() == t for a in g.agents)]
        if named:
            return [r for g in named for r in g.rules], "named"
        star = [g for g in self.groups if "*" in g.agents]
        if star:
            return [r for g in star for r in g.rules], "star"
        return [], "none"

    def allowed(self, token: str, path: str) -> bool:
        return self.decide(token, path)["allowed"]

    def decide(self, token: str, path: str) -> dict:
        rules, kind = self.group_for(token)
        p = _norm(path or "/")
        if p == "/robots.txt":
            return {"allowed": True, "group": kind, "rule": None}
        best = None
        for allow, pattern in rules:
            if pattern == "" and not allow:
                continue
            n = _match_len(pattern, p)
            if n is None:
                continue
            if best is None or n > best[0] or (n == best[0] and allow and not best[1]):
                best = (n, allow, pattern)
        if best is None:
            return {"allowed": True, "group": kind, "rule": None}
        return {"allowed": best[1], "group": kind, "rule": ("allow: " if best[1] else "disallow: ") + best[2]}


def _norm(path: str) -> str:
    try:
        return quote(unquote(path), safe="/?=&*$%:;@+,!~'()[]-._")
    except (UnicodeError, ValueError):
        return path


def _match_len(pattern: str, path: str):
    """Length of ``pattern`` (its specificity) when it matches ``path`` from the start, else None."""
    anchored = pattern.endswith("$")
    body = _norm(pattern[:-1] if anchored else pattern)
    rx = "".join(".*" if c == "*" else re.escape(c) for c in body)
    m = re.match(rx + ("$" if anchored else ""), path)
    return len(pattern) if m else None


def parse(text: str) -> Robots:
    out = Robots()
    current = None
    last_was_agent = False
    lines = text.splitlines()
    out.lines_total = min(len(lines), MAX_LINES)
    for raw in lines[:MAX_LINES]:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, value = line.partition(":")
        if not sep:
            out.lines_ignored += 1
            continue
        key = key.strip().lower().replace("_", "-")
        value = value.strip()[:MAX_RULE]
        if key in ("user-agent", "useragent"):
            if current is None or not last_was_agent:
                current = Group()
                out.groups.append(current)
            current.agents.append(value.split("/", 1)[0].strip() or "*" if value else value)
            last_was_agent = True
            continue
        last_was_agent = False
        if key in ("allow", "disallow"):
            if current is None:
                out.lines_ignored += 1
                continue
            current.rules.append((key == "allow", value))
        elif key == "sitemap":
            if value:
                out.sitemaps.append(value)
        else:
            out.lines_ignored += 1
    return out
