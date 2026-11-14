"""
robots.txt parsing and matching, RFC 9309 (stdlib only; ``urllib.robotparser`` is not used: it does not implement
the RFC's longest-match rule or the ``*`` / ``$`` wildcards).

- groups start at one or more ``user-agent`` lines; ``allow`` / ``disallow`` rules belong to the current group;
  rules before any user-agent line are ignored; unknown lines are ignored; ``sitemap`` lines are global; a leading
  UTF-8 byte order mark is not part of the first line;
- a crawler uses the group(s) whose user-agent value matches its product token case-insensitively (all such groups
  are merged); if none, the ``*`` group(s); if none, everything is allowed;
- the most specific matching rule wins; on equal specificity ``allow`` wins; an empty rule value matches nothing;
  ``/robots.txt`` is always allowed.

Comparison (RFC 9309 section 2.2.2; AEGIS Wave-1 round 2 M1). Rule patterns and the URL path (with its query) are
compared as OCTET sequences in one canonical percent-encoded form (``canon``):
  - ``%XX`` of an UNRESERVED character (RFC 3986: A-Z a-z 0-9 ``-._~``) is decoded; every other ``%XX`` is kept,
    its hex upper-cased (``%2f`` -> ``%2F``; ``%2F`` never equals ``/``, ``%3F`` never ``?``);
  - a character outside ASCII is percent-encoded from its UTF-8 octets (a katakana letter equals ``%E3%83%84``); an octet
    that is not valid UTF-8 in the file (decoded with ``surrogateescape``) is percent-encoded as itself, so two
    different invalid octets never collide; nothing non-ASCII is ever decoded;
  - in a pattern, ``*`` and a final ``$`` are split off BEFORE normalising: ``%2A`` and ``%24`` are never wildcards;
    a raw ``*`` or ``$`` in a URL path is compared as ``%2A`` / ``%24`` (section 2.2.3: ``/a-%2A`` matches
    ``/a-*``), and so is a ``$`` that is not at a pattern's end;
  - a ``%`` that does not start a ``%XX`` escape is ``%25``; a space, a control character and ``"<>\\^`{|}`` are
    percent-encoded.
  Specificity ("most octets") is the length of the canonical pattern, wildcards and ``$`` counted as one octet
  each: a pattern written with that letter and one written ``/%E3%83%84`` are equally specific (10 octets).

Cost (AEGIS Wave-1 M-S1; round 2 M2/M3). Matching never uses a regular expression: a pattern is compiled ONCE,
when the file is parsed, into its literal pieces between ``*`` wildcards, and is matched by greedy leftmost search of
each piece in turn (``str.find``), which is correct for ``*``-only patterns and linear in the path and pattern
length per rule: no backtracking. Rules are indexed by their first literal piece, so only rules whose first piece
is a prefix of the path are tried. There is NO cap on rules per group, pattern length or wildcards: the parse limit
(``MAX_BYTES``, RFC 9309 section 2.5: at least 500 KiB) bounds all of them, and a file over it is read up to it
(its last, cut, line dropped) and reported (``truncated``; every decision from it is ``complete: False``).
The cost of a step is bounded by COUNTED WORK, not by a clock (``Work``): each decision is charged
``1 + len(path) // 64`` units for normalising, one per index probe, and ``1 + pieces + len(path) // 256`` per
rule tried; a decision whose charge would pass the step's limit is not made (``allowed: None``, ``undecided:
"work"``) and the caller reports ROBOTS_CHECK_INCOMPLETE / INSUFFICIENT_EVIDENCE. The same input always gets the
same answer, however slow the network or the machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

MAX_BYTES = 500 * 1024          # parse limit (RFC 9309 section 2.5); text past it is not read
BOM = "\ufeff"
# Work units one step may spend (counted, deterministic). Measured: the worst hostile robots.txt within MAX_BYTES
# spends 1,000,000 units in well under a second (ADR 0017, "Wave-1 certification round 2").
STEP_WORK_UNITS = 2_000_000
# B-RB-09 (b, c): one unit is about one microsecond of matching on the reference box (measured: str.find costs
# 0.36-0.56 ns per octet pair on the A-03 near-match shapes), so a step's work is bounded in CPU as well
FIND_COST_DIVISOR = 1024
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")
_ENCODE_ASCII = frozenset(' "<>\\^`{|}*$')
# a string of these characters only is already canonical (no escape, nothing to encode)
_PLAIN = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/?=&:;@+,!'()[]")


def canon(s: str, pattern: bool = False) -> str:
    """The canonical octet form of a path (with its query) or of one literal pattern piece (see module doc).
    ``pattern``: a piece of a pattern (its ``*`` and final ``$`` already split off); a ``$`` or ``*`` left in it is
    literal either way."""
    if _PLAIN.issuperset(s):
        return s
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "%":
            if i + 2 < n and s[i + 1] in _HEXDIGITS and s[i + 2] in _HEXDIGITS:
                h = s[i + 1:i + 3]
                ch = chr(int(h, 16))
                out.append(ch if ch in _UNRESERVED else "%" + h.upper())
                i += 3
                continue
            out.append("%25")
            i += 1
            continue
        o = ord(c)
        if 0xDC80 <= o <= 0xDCFF:                       # an octet that was not valid UTF-8 (surrogateescape)
            out.append("%%%02X" % (o - 0xDC00))
        elif o >= 0x80:
            out.append("".join("%%%02X" % b for b in c.encode("utf-8", "surrogatepass")))
        elif o < 0x20 or o == 0x7F or c in _ENCODE_ASCII:
            out.append("%%%02X" % o)
        else:
            out.append(c)
        i += 1
    return "".join(out)


_UNIT_BASE = 0xF0000          # Supplementary Private Use Area-A: one character per escaped octet (never data)


def _units(s: str, pattern: bool = False) -> str:
    """``canon(s)`` as a string of UNITS: each ``%XX`` escape of the canonical form is ONE character
    (``_UNIT_BASE + octet``), every other character stands for itself. A literal piece can then only match whole
    octets: an ``A`` never matches the hex digit inside ``%2A`` (B-RB-03 / B-RB-04, octet comparison; found by the
    lead-gate RFC 9309 differential). Every escape of the canonical form is upper-case hex and every non-ASCII
    character is escaped, so the mapping is one to one."""
    c = canon(s, pattern)
    if "%" not in c:
        return c
    out, i, n = [], 0, len(c)
    while i < n:
        ch = c[i]
        if ch == "%" and i + 2 < n:
            out.append(chr(_UNIT_BASE + int(c[i + 1:i + 3], 16)))
            i += 3
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _octets(u: str) -> int:
    """Octets of the canonical form of a unit string (an escape unit is the three octets ``%XX``)."""
    return len(u) + 2 * sum(1 for ch in u if ord(ch) >= _UNIT_BASE)


class Work:
    """Counted work for one step's robots.txt decisions (no clock): ``units`` spent, ``limit``, and how many
    decisions were left undecided because the next one would have passed the limit."""

    def __init__(self, limit: int = STEP_WORK_UNITS):
        self.limit = int(limit)
        self.units = 0
        self.undecided = 0

    def charge(self, units: int) -> bool:
        if self.units + units > self.limit:
            self.undecided += 1
            return False
        self.units += units
        return True


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str            # as written in the file (after ``**`` collapsed)
    length: int             # specificity: octets of the canonical pattern
    pieces: tuple           # the canonical literal pieces between wildcards, as unit strings (``_units``)
    anchored: bool          # ends with ``$``
    find_len: int = 0       # octets of the pieces searched with ``str.find`` (all but the first)


def _compile(allow: bool, pattern: str) -> Rule:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    pieces = tuple(_units(p, True) for p in body.split("*"))
    length = sum(_octets(p) for p in pieces) + len(pieces) - 1 + (1 if anchored else 0)
    return Rule(allow, pattern, length, pieces, anchored, sum(len(p) for p in pieces[1:]))


def _match(rule: Rule, path: str) -> bool:
    """Greedy leftmost matching of ``*``-only patterns on canonical strings: the first piece is a prefix, each next
    piece is found at its leftmost position after the previous one; with ``$`` the last piece must end the path at
    or after where the previous one ended. Linear per piece."""
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
        return len(path) - len(tail) >= pos and path.endswith(tail)
    return not tail or path.find(tail, pos) >= 0


@dataclass
class Group:
    agents: list = field(default_factory=list)
    rules: list = field(default_factory=list)        # [Rule]


class _RuleSet:
    """The rules that apply to one product token, indexed by first literal piece."""

    def __init__(self, rules: list):
        self.by_first: dict = {}
        for r in rules:
            self.by_first.setdefault(r.pieces[0], []).append(r)
        self.lengths = sorted({len(k) for k in self.by_first})
        self.count = len(rules)


@dataclass
class Robots:
    groups: list = field(default_factory=list)
    sitemaps: list = field(default_factory=list)
    lines_ignored: int = 0
    lines_total: int = 0
    bom_stripped: bool = False
    truncated: bool = False             # the text was longer than MAX_BYTES (or the fetch cut it there)
    bytes_read: int = 0
    _by_token: dict = field(default_factory=dict, repr=False)
    _by_groups: dict = field(default_factory=dict, repr=False)
    _memo: dict = field(default_factory=dict, repr=False)

    def limits(self) -> dict:
        """What a bound changed in this file: only the parse limit can (no per-rule caps)."""
        return {"truncated": self.truncated, "bytes_read": self.bytes_read,
                "caps": {"bytes": MAX_BYTES, "step_work_units": STEP_WORK_UNITS}}

    def limits_exceeded(self) -> bool:
        return self.truncated

    def _rules_for(self, token: str) -> tuple:
        """(rule set, group kind) for ``token``, merged once and kept."""
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
        rs = self._by_groups.get(gkey)            # tokens that fall into the same groups share one rule set
        if rs is None:
            rs = self._by_groups[gkey] = _RuleSet([r for g in named for r in g.rules])
        hit = (rs, kind)
        if len(self._by_token) < 256:
            self._by_token[t] = hit
        return hit

    def group_for(self, token: str) -> tuple[list, str]:
        """The rules that apply to ``token`` (allow, pattern as written) and which group kind matched."""
        rs, kind = self._rules_for(token)
        return [(r.allow, r.pattern) for rules in rs.by_first.values() for r in rules], kind

    def allowed(self, token: str, path: str, work: Optional[Work] = None) -> Optional[bool]:
        """True / False, or None when undecided (the step's counted work is spent)."""
        return self.decide(token, path, work)["allowed"]

    def decide(self, token: str, path: str, work: Optional[Work] = None) -> dict:
        """{"allowed": True / False / None, "group", "rule": "allow: <pattern>" as written in the file or None,
        "complete": False when the file was truncated at the parse limit (the decision is approximate)}."""
        rs, kind = self._rules_for(token)
        complete = not self.truncated
        p = path or "/"
        if work is not None and not work.charge(1 + len(p) // 64):
            return {"allowed": None, "group": kind, "rule": None, "complete": complete, "undecided": "work"}
        p = _units(p)
        if p == "/robots.txt":
            return {"allowed": True, "group": kind, "rule": None, "complete": True}
        mk = (id(rs), p)
        hit = self._memo.get(mk)
        if hit is None:
            cands = []
            for n in rs.lengths:
                if n > len(p):
                    break
                got = rs.by_first.get(p[:n])
                if got:
                    cands.extend(got)
            # B-RB-09: the charge follows the real matching cost: a piece searched with ``str.find`` costs up to
            # len(path) x len(piece) octet comparisons (CPython's search degrades to that on near-matches), charged
            # FIND_COST_DIVISOR of them a unit
            cost = len(rs.lengths) + sum(1 + len(r.pieces) + (len(p) * r.find_len) // FIND_COST_DIVISOR
                                         for r in cands)
            if work is not None and not work.charge(cost):
                return {"allowed": None, "group": kind, "rule": None, "complete": complete, "undecided": "work"}
            best = None
            for r in cands:
                if (best is None or r.length > best.length or (r.length == best.length and r.allow
                                                               and not best.allow)) and _match(r, p):
                    best = r
            hit = (True, None) if best is None else (best.allow, ("allow: " if best.allow else "disallow: ")
                                                     + best.pattern)
            if len(self._memo) < 4096:
                self._memo[mk] = hit
        return {"allowed": hit[0], "group": kind, "rule": hit[1], "complete": complete}


def _lines(text: str) -> list:
    """Lines ended by CR, LF or CRLF only (RFC 9309 2.2; no other Unicode line break), without a regular expression."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _collapse(pattern: str) -> str:
    while "**" in pattern:
        pattern = pattern.replace("**", "*")
    return pattern


def parse(text: str, truncated: bool = False) -> Robots:
    """Parse robots.txt text (decoded with ``surrogateescape`` by the fetcher). ``truncated``: the caller already
    cut the body at the parse limit. Text longer than MAX_BYTES characters is cut here. Either way the last line,
    which the cut may have shortened, is not read (a shortened rule is a rule not in the file)."""
    out = Robots()
    current = None
    last_was_agent = False
    if text.startswith(BOM):                     # AEGIS Wave-1 S-S1: a BOM is not part of the first line
        text = text[len(BOM):]
        out.bom_stripped = True
    raw = text.encode("utf-8", "surrogateescape")
    if len(raw) > MAX_BYTES:
        # B-RB-07: the parse limit is OCTETS of the decoded body, also when the text is multi-byte
        raw = raw[:MAX_BYTES]
        text = raw.decode("utf-8", "surrogateescape")
        truncated = True
    out.truncated = truncated
    out.bytes_read = len(raw)
    # RFC 9309 2.2: a line ends at CR, LF or CRLF only (``str.splitlines`` would also split on VT, FF, U+2028 ...)
    lines = _lines(text)
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if truncated and lines and not text.endswith(("\n", "\r")):
        lines = lines[:-1]
    out.lines_total = len(lines)
    seen_rules: set = set()
    for raw in lines:
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
            current.agents.append(value.split("/", 1)[0].strip() or "*" if value else value)
            last_was_agent = True
            continue
        if key in ("allow", "disallow"):
            # only a rule ends a run of user-agent lines: a sitemap line (global, any position) or an ignored line
            # between two user-agent lines leaves them one group (Google's parser; spec B-RB-06)
            last_was_agent = False
            if current is None:
                out.lines_ignored += 1
                continue
            allow = key == "allow"
            v = _collapse(value)
            if not v or (allow, v) in seen_rules:     # an empty value matches nothing; a repeat decides nothing new
                continue
            seen_rules.add((allow, v))
            current.rules.append(_compile(allow, v))
        elif key == "sitemap":
            if value:
                out.sitemaps.append(value)
        else:
            out.lines_ignored += 1
    return out
