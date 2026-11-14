"""
O-RFC: an independent RFC 9309 reference matcher, written from RFC 9309 sections 2.1-2.5 and RFC 3986 section 2.3.
It shares no code with src/ (no import from it). Input and comparison are OCTETS.

Model (from the RFC text):
  * 2.1 / 2.2.1: a group is one or more user-agent lines followed by rules; a crawler obeys the groups whose
    user-agent value equals its product token, case-insensitively (all of them combined); else the ``*`` groups;
    else there are no rules. Rules before the first user-agent line belong to no group.
  * 2.2.2: the most specific match (the most octets) wins; on a tie between allow and disallow, allow; an empty
    rule value matches nothing; ``/robots.txt`` is implicitly allowed. Percent-encoded octets of UNRESERVED
    characters are decoded before comparison; other percent-encodings stay as they are (hex case is not
    significant); octets outside US-ASCII and characters that must be encoded are percent-encoded first.
  * 2.2.3: ``*`` stands for any sequence of octets (an encoded ``%2A`` is the literal octet); ``$`` at the end of
    a pattern anchors at the end of the path.
Matching is a dynamic programme over the canonical units of the pattern and the path (no regular expressions, no
backtracking).
"""

from __future__ import annotations

UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
HEX = frozenset(b"0123456789abcdefABCDEF")
# characters RFC 3986 never allows raw in a URI (plus the two pattern metacharacters when they are data)
MUST_ENCODE = frozenset(b' "<>\\^`{|}')
WILD, END = "*", "$"


def _unit_bytes(b: int) -> str:
    return "%%%02X" % b


def canonical_units(raw: bytes, pattern: bool) -> list:
    """The canonical units: each is one data octet written as a 1-character string (printable ASCII kept) or as
    ``%XX`` (upper-case), or the WILD / END markers in a pattern."""
    out: list = []
    n = len(raw)
    end_anchor = pattern and n > 0 and raw[-1:] == b"$"
    body = raw[:-1] if end_anchor else raw
    i = 0
    while i < len(body):
        b = body[i]
        if pattern and b == 0x2A:                       # '*'
            out.append(WILD)
            i += 1
            continue
        if b == 0x25 and i + 2 < len(body) and body[i + 1] in HEX and body[i + 2] in HEX:
            v = int(body[i + 1:i + 3], 16)
            out.append(chr(v) if v in UNRESERVED else _unit_bytes(v))
            i += 3
            continue
        if b == 0x25:                                   # a stray '%'
            out.append("%25")
        elif b >= 0x80 or b < 0x21 or b == 0x7F or b in MUST_ENCODE:
            out.append(_unit_bytes(b))
        elif b in (0x2A, 0x24):                         # raw '*' / '$' as data (in a path, or an inner '$')
            out.append(_unit_bytes(b))
        else:
            out.append(chr(b))
        i += 1
    if end_anchor:
        out.append(END)
    if pattern:                                          # 2.2.3: "**" is the same as "*"
        dedup = []
        for u in out:
            if u == WILD and dedup and dedup[-1] == WILD:
                continue
            dedup.append(u)
        out = dedup
    return out


def specificity(units: list) -> int:
    return sum(1 if u in (WILD, END) else len(u) for u in units)


def matches(pat: list, path: list) -> bool:
    """Whether the pattern units match a PREFIX of the path units (or the whole path when END is last)."""
    anchored = bool(pat) and pat[-1] == END
    p = pat[:-1] if anchored else pat
    m = len(path)
    # reach[j]: the pattern prefix processed so far can end at path position j
    reach = [False] * (m + 1)
    reach[0] = True
    for u in p:
        if u == WILD:
            seen = False
            for j in range(m + 1):
                seen = seen or reach[j]
                reach[j] = seen
        else:
            nxt = [False] * (m + 1)
            for j in range(m):
                if reach[j] and path[j] == u:
                    nxt[j + 1] = True
            reach = nxt
        if not any(reach):
            return False
    return reach[m] if anchored else any(reach)


def _lines(data: bytes) -> list:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    out, cur = [], bytearray()
    i = 0
    while i < len(data):
        c = data[i]
        if c in (0x0A, 0x0D):
            out.append(bytes(cur))
            cur = bytearray()
            if c == 0x0D and i + 1 < len(data) and data[i + 1] == 0x0A:
                i += 1
        else:
            cur.append(c)
        i += 1
    out.append(bytes(cur))
    return out


AGENT_KEYS = {b"user-agent", b"useragent", b"user_agent"}


def groups(data: bytes) -> list:
    """[(set of lower-cased user-agent values, [(allow: bool, value bytes)])] in file order."""
    gs: list = []
    cur = None
    last_was_agent = False
    for line in _lines(data):
        line = line.split(b"#", 1)[0]
        if b":" not in line:
            continue
        k, v = line.split(b":", 1)
        k, v = k.strip().lower(), v.strip()
        if k in AGENT_KEYS:
            if cur is None or not last_was_agent:
                cur = (set(), [])
                gs.append(cur)
            cur[0].add(v.lower())
            last_was_agent = True
            continue
        if k in (b"allow", b"disallow"):
            if cur is not None:
                cur[1].append((k == b"allow", v))
            last_was_agent = False
            continue
        if k == b"sitemap":
            continue                                    # global; does not end a group
    return gs


def rules_for(data: bytes, token: str) -> list:
    t = token.lower().encode("ascii")
    gs = groups(data)
    named = [r for agents, rules in gs if t in agents for r in rules]
    if any(t in agents for agents, _ in gs):
        return named
    return [r for agents, rules in gs if b"*" in agents for r in rules]


def allowed(data: bytes, token: str, path: bytes) -> bool:
    """The decision for ``path`` (path plus query, octets as written) for ``token``."""
    pu = canonical_units(path, pattern=False)
    if pu == list("/robots.txt"):
        return True
    best = None                                         # (specificity, allow)
    for allow, value in rules_for(data, token):
        if not value:
            continue
        units = canonical_units(value, pattern=True)
        if matches(units, pu):
            key = (specificity(units), allow)
            if best is None or key > best:
                best = key
    return True if best is None else best[1]


def self_test() -> None:
    """The RFC's own examples (section 2.2.2 table and 5.x)."""
    f = b"User-agent: *\nAllow: /publications/\nDisallow: /\n"
    assert allowed(f, "x", b"/publications/a") and not allowed(f, "x", b"/b")
    f = b"User-agent: *\nDisallow: /example/page.html\nDisallow: /example/disallowed.gif\n"
    assert not allowed(f, "x", b"/example/page.html") and allowed(f, "x", b"/example/allowed.gif")
    f = b"User-agent: *\nAllow: /example/page/\nDisallow: /example/page/disallowed.gif\n"
    assert allowed(f, "x", b"/example/page/a") and not allowed(f, "x", b"/example/page/disallowed.gif")
    f = b"User-agent: *\nAllow: /this/*/exactly\nDisallow: /this/\n"
    assert allowed(f, "x", b"/this/is/exactly") and not allowed(f, "x", b"/this/is")
    f = b"User-agent: *\nDisallow: /*.gif$\n"
    assert not allowed(f, "x", b"/a.gif") and allowed(f, "x", b"/a.gifx")
    f = b"User-agent: *\nDisallow: /foo/bar?baz=quz\n"
    assert not allowed(f, "x", b"/foo/bar?baz=quz") and allowed(f, "x", b"/foo/bar?baz=qux")
    f = b"User-agent: *\nDisallow: /path/file-with-a-%2A.html\n"
    assert not allowed(f, "x", b"/path/file-with-a-*.html")
    f = b"User-agent: *\nDisallow: /%E3%83%84\n"
    assert not allowed(f, "x", "/\u30c4".encode("utf-8"))
    f = b"User-agent: *\nDisallow: /\n"
    assert allowed(f, "x", b"/robots.txt")
    f = b"User-agent: a\nUser-agent: b\nDisallow: /x\nUser-agent: *\nDisallow: /\n"
    assert not allowed(f, "B", b"/x") and allowed(f, "B", b"/y") and not allowed(f, "c", b"/y")
