"""
robots.txt parsing and matching, RFC 9309 (stdlib only; ``urllib.robotparser`` is not used: it does not implement
the RFC's longest-match rule or the ``*`` / ``$`` wildcards).

- groups start at one or more ``user-agent`` lines; ``allow`` / ``disallow`` rules belong to the current group;
  rules before any user-agent line are ignored; unknown lines are ignored; ``sitemap`` lines are global; a leading
  UTF-8 byte order mark is not part of the first line;
- a crawler uses the group(s) whose user-agent value matches its product token case-insensitively (all such groups
  are merged); if none, the ``*`` group(s); if none, everything is allowed;
- the most specific (longest) matching rule wins; on equal length ``allow`` wins; an empty ``disallow`` value
  matches nothing; ``/robots.txt`` is always allowed;
- paths are compared after percent-encoding normalisation of unreserved characters only.

Cost (AEGIS Wave-1 certification M-S1). Matching never uses a regular expression: a pattern is compiled ONCE, when
the file is parsed, into its literal pieces between ``*`` wildcards, and is matched by greedy leftmost search of
each piece in turn (``str.find``), which is correct for ``*``-only patterns and linear in the path length per piece:
no backtracking, whatever the pattern. The rules that apply to a product token are merged once per token and kept.
Bounds, each counted and reported (``limits()``; Selene reports ROBOTS_LIMITS_EXCEEDED), never silently dropped:

  MAX_LINES            lines read (above what a 500 KiB file can hold; RFC 9309 asks for at least 500 KiB and
                       the fetcher reads at most that)
  MAX_PATTERN          characters in one rule's path pattern
  MAX_WILDCARDS        ``*`` wildcards in one pattern (consecutive ``*`` count once)
  MAX_RULES_PER_GROUP  distinct rules in one group, and in the rules merged for one product token (a repeated
                       rule in a group is read once)
  MAX_PATH             characters in a path to decide (longer: undecided)

Over a bound the parser FAILS CLOSED for whoever honours the file: a disallow rule over MAX_PATTERN or
MAX_WILDCARDS is shortened to a prefix pattern that matches at least every path the rule matched (it disallows
more, never less); an allow rule over a bound, and any rule past MAX_RULES_PER_GROUP, is not used, and the decision
for that token is then marked ``complete: False`` (``allowed`` decisions from an incomplete rule set are not
trusted by this service's own crawler: see fetch.py). Each decision can also be given a ``Budget`` (the caller's
time budget for a step): once it is spent, ``decide`` answers ``allowed: None`` (undecided) without matching, and
the caller reports PARTIAL / INSUFFICIENT_EVIDENCE instead of running on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote, unquote

MAX_LINES = 130_000            # more than any 500 KiB file can hold (a rule line is at least 4 bytes)
MAX_PATTERN = 2000
MAX_WILDCARDS = 10
MAX_RULES_PER_GROUP = 4000
MAX_PATH = 4096
# Kept for callers that read the old name (the value cap of one line).
MAX_RULE = MAX_PATTERN
BOM = "\ufeff"


class Budget:
    """A step's time budget for robots.txt decisions (monotonic seconds). ``spent`` once the deadline has passed."""

    def __init__(self, seconds: float):
        self.seconds = float(seconds)
        self.deadline = time.monotonic() + self.seconds
        self.undecided = 0

    def spent(self) -> bool:
        return time.monotonic() >= self.deadline


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str            # as written (after the length / wildcard bounds)
    length: int             # specificity: the length of the pattern as written in the file
    pieces: tuple           # the normalised literal pieces between wildcards
    anchored: bool          # ends with ``$``


def _compile(allow: bool, pattern: str, length: Optional[int] = None) -> Rule:
    anchored = pattern.endswith("$")
    body = _norm(pattern[:-1] if anchored else pattern)
    pieces = tuple(body.split("*"))
    return Rule(allow, pattern, len(pattern) if length is None else length, pieces, anchored)


def _match(rule: Rule, path: str) -> bool:
    """Greedy leftmost matching of ``*``-only patterns: the first piece is a prefix, each next piece is found at
    its leftmost position after the previous one; with ``$`` the last piece must end the path. Linear per piece."""
    pieces = rule.pieces
    first = pieces[0]
    if not path.startswith(first):
        return False
    if len(pieces) == 1:
        return (not rule.anchored) or len(path) == len(first)
    pos = len(first)
    last = len(pieces) - 1
    for i in range(1, last):
        p = pieces[i]
        if not p:
            continue
        j = path.find(p, pos)
        if j < 0:
            return False
        pos = j + len(p)
    tail = pieces[last]
    if rule.anchored:
        # the last piece must end the path and start at or after where the previous piece ended
        return len(path) - len(tail) >= pos and path.endswith(tail)
    return not tail or path.find(tail, pos) >= 0


@dataclass
class Group:
    agents: list = field(default_factory=list)
    rules: list = field(default_factory=list)        # [Rule]
    rules_dropped: int = 0


@dataclass
class Robots:
    groups: list = field(default_factory=list)
    sitemaps: list = field(default_factory=list)
    lines_ignored: int = 0
    lines_total: int = 0
    lines_over_cap: int = 0
    patterns_shortened: int = 0       # disallow rules shortened to a broader prefix (fail closed)
    rules_not_used: int = 0           # allow rules over a bound, and rules past MAX_RULES_PER_GROUP
    bom_stripped: bool = False
    _by_token: dict = field(default_factory=dict, repr=False)
    _by_groups: dict = field(default_factory=dict, repr=False)
    _memo: dict = field(default_factory=dict, repr=False)

    def limits(self) -> dict:
        """What the bounds changed in this file (all zero / False for an ordinary robots.txt)."""
        return {"lines_over_cap": self.lines_over_cap, "patterns_shortened": self.patterns_shortened,
                "rules_not_used": self.rules_not_used,
                "caps": {"lines": MAX_LINES, "pattern_chars": MAX_PATTERN, "wildcards": MAX_WILDCARDS,
                         "rules_per_group": MAX_RULES_PER_GROUP, "path_chars": MAX_PATH}}

    def limits_exceeded(self) -> bool:
        return bool(self.lines_over_cap or self.patterns_shortened or self.rules_not_used)

    def _rules_for(self, token: str) -> tuple:
        """(rules, group kind, complete) for ``token``, merged once and kept."""
        t = token.lower()
        hit = self._by_token.get(t)
        if hit is not None:
            return hit
        named = [g for g in self.groups if any(a.lower() == t for a in g.agents)]
        kind = "named"
        if not named:
            named = [g for g in self.groups if "*" in g.agents]
            kind = "star" if named else "none"
        gkey = tuple(id(g) for g in named)
        same = self._by_groups.get(gkey)            # tokens that fall into the same groups share one merged list
        if same is None:
            merged: list = []
            complete = not any(g.rules_dropped for g in named)
            for g in named:
                for r in g.rules:
                    if len(merged) >= MAX_RULES_PER_GROUP:
                        complete = False
                        break
                    merged.append(r)
            # Longest first, allow before disallow at equal length: the first match is the RFC 9309 answer.
            merged.sort(key=lambda r: (-r.length, not r.allow))
            same = self._by_groups[gkey] = (tuple(merged), complete)
        hit = (same[0], kind, same[1])
        if len(self._by_token) < 256:
            self._by_token[t] = hit
        return hit

    def group_for(self, token: str) -> tuple[list, str]:
        """The rules that apply to ``token`` and which group kind matched: "named", "star" or "none"."""
        rules, kind, _ = self._rules_for(token)
        return [(r.allow, r.pattern) for r in rules], kind

    def allowed(self, token: str, path: str, budget: Optional[Budget] = None) -> Optional[bool]:
        """True / False, or None when undecided (budget spent or path over MAX_PATH)."""
        return self.decide(token, path, budget)["allowed"]

    def decide(self, token: str, path: str, budget: Optional[Budget] = None) -> dict:
        rules, kind, complete = self._rules_for(token)
        p = _norm(path or "/")
        if p == "/robots.txt":
            return {"allowed": True, "group": kind, "rule": None, "complete": True}
        if len(p) > MAX_PATH:
            if budget is not None:
                budget.undecided += 1
            return {"allowed": None, "group": kind, "rule": None, "complete": complete, "undecided": "path_too_long"}
        mk = (id(rules), p)
        hit = self._memo.get(mk)
        if hit is None:
            if budget is not None and budget.spent():
                budget.undecided += 1
                return {"allowed": None, "group": kind, "rule": None, "complete": complete, "undecided": "budget"}
            hit = (True, None)
            for r in rules:
                if not r.allow and r.pattern == "":
                    continue
                if _match(r, p):
                    hit = (r.allow, ("allow: " if r.allow else "disallow: ") + r.pattern)
                    break
            if len(self._memo) < 4096:
                self._memo[mk] = hit
        return {"allowed": hit[0], "group": kind, "rule": hit[1], "complete": complete}


def _norm(path: str) -> str:
    try:
        return quote(unquote(path), safe="/?=&*$%:;@+,!~'()[]-._")
    except (UnicodeError, ValueError):
        return path


def _collapse(pattern: str) -> str:
    while "**" in pattern:
        pattern = pattern.replace("**", "*")
    return pattern


def _bounded_rule(allow: bool, value: str, out: Robots) -> Optional[Rule]:
    """The rule for one ``allow`` / ``disallow`` value within MAX_PATTERN and MAX_WILDCARDS, or None (not used)."""
    written = len(value)
    v = _collapse(value)
    over_len = len(v) > MAX_PATTERN
    over_wild = v.count("*") > MAX_WILDCARDS
    if not over_len and not over_wild:
        return _compile(allow, v, written)
    if allow:
        out.rules_not_used += 1          # fail closed: an allow over a bound allows nothing
        return None
    # fail closed: a disallow over a bound becomes a prefix pattern that matches at least what it matched
    v = v.removesuffix("$")
    if over_wild:
        cut, seen = len(v), 0
        for i, c in enumerate(v):
            if c == "*":
                seen += 1
                if seen > MAX_WILDCARDS:
                    cut = i
                    break
        v = v[:cut]
    v = v[:MAX_PATTERN]
    out.patterns_shortened += 1
    return _compile(False, v, written)


def parse(text: str) -> Robots:
    out = Robots()
    current = None
    last_was_agent = False
    if text.startswith(BOM):                     # AEGIS Wave-1 S-S1: a BOM is not part of the first line
        text = text[len(BOM):]
        out.bom_stripped = True
    lines = text.splitlines()
    out.lines_total = min(len(lines), MAX_LINES)
    out.lines_over_cap = max(0, len(lines) - MAX_LINES)
    seen_rules: set = set()
    for raw in lines[:MAX_LINES]:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, value = line.partition(":")
        if not sep:
            out.lines_ignored += 1
            continue
        key = key.strip().lower().replace("_", "-")
        value = value.strip()
        if key in ("user-agent", "useragent"):
            if current is None or not last_was_agent:
                current = Group()
                out.groups.append(current)
                seen_rules = set()
            value = value[:MAX_PATTERN]
            current.agents.append(value.split("/", 1)[0].strip() or "*" if value else value)
            last_was_agent = True
            continue
        last_was_agent = False
        if key in ("allow", "disallow"):
            if current is None:
                out.lines_ignored += 1
                continue
            allow = key == "allow"
            if (allow, value) in seen_rules:     # a repeated rule decides nothing new
                continue
            if len(current.rules) >= MAX_RULES_PER_GROUP:
                current.rules_dropped += 1
                out.rules_not_used += 1
                continue
            rule = _bounded_rule(allow, value, out)
            if rule is None:
                if allow:
                    current.rules_dropped += 1
                continue
            seen_rules.add((allow, value))
            current.rules.append(rule)
        elif key == "sitemap":
            if value:
                out.sitemaps.append(value[:MAX_PATTERN])
        else:
            out.lines_ignored += 1
    return out
