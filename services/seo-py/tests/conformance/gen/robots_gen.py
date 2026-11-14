"""
Seeded robots.txt generators (SUITE_PLAN 3): the regimes plain, query, case, ties, pct_unreserved, pct_reserved,
invalid_utf8, non_ascii, plus hostile shapes for A-01..A-07. Files are BYTES (what the server sends); paths are bytes
as written in the request target.
"""

from __future__ import annotations

import random

TOKENS = ("Googlebot", "Bingbot", "GPTBot", "ClaudeBot", "CCBot", "PerplexityBot")
ALPHA = "abcxyz"
RESERVED = ("%2A", "%2F", "%3F", "%24", "%23", "%25", "%2a", "%2f", "%3f")
UNRES = ("%61", "%62", "%7E", "%2D", "%41", "%7e")
INVALID = (b"%FF", b"%FE", b"%C0%AF", b"%80")
REGIMES = ("plain", "query", "case", "ties", "pct_unreserved", "pct_reserved", "invalid_utf8", "non_ascii")


def _piece(r: random.Random, regime: str) -> bytes:
    k = r.random()
    if regime == "pct_reserved" and k < 0.4:
        return r.choice(RESERVED).encode()
    if regime == "pct_unreserved" and k < 0.4:
        return r.choice(UNRES).encode()
    if regime == "invalid_utf8" and k < 0.4:
        return r.choice(INVALID) if r.random() < 0.5 else bytes([r.choice((0xFF, 0xFE, 0x80, 0xC0))])
    if regime == "non_ascii" and k < 0.4:
        return r.choice(("\u00e9", "\u30c4", "\u00fc")).encode("utf-8") if r.random() < 0.6 else \
            r.choice((b"%C3%A9", b"%E3%83%84", b"%c3%bc"))
    if regime == "query" and k < 0.2:
        return r.choice((b"?", b"=", b"&", b"?q=", b"?sort="))
    if regime == "case" and k < 0.3:
        return r.choice("ABCXYZ").encode()
    return r.choice(ALPHA).encode()


def pattern(r: random.Random, regime: str) -> bytes:
    out = b"/"
    for _ in range(r.randint(0, 5)):
        x = r.random()
        if x < 0.15:
            out += b"*"
        elif x < 0.25:
            out += b"/"
        else:
            out += _piece(r, regime)
    if r.random() < 0.15:
        out += b"$"
    if r.random() < 0.05:
        out = out.replace(b"/", b"", 1)                 # a pattern without the leading slash
    return out


def path(r: random.Random, regime: str) -> bytes:
    out = b"/"
    for _ in range(r.randint(0, 7)):
        out += b"/" if r.random() < 0.2 else _piece(r, regime)
    if r.random() < 0.08:
        out += r.choice((b"*", b"$", b"%"))
    return out


def ruleset(r: random.Random, regime: str = None) -> bytes:
    regime = regime or r.choice(REGIMES)
    lines = []
    if r.random() < 0.1:
        lines.append(b"Disallow: /before-any-group")
    for _ in range(r.randint(1, 4)):
        for _ in range(r.randint(1, 2)):
            tok = r.choice(TOKENS + ("*",))
            key = r.choice((b"User-agent", b"user-agent", b"USER-AGENT", b"useragent", b"User_agent"))
            lines.append(key + b": " + (tok.lower() if r.random() < 0.3 else tok).encode())
        for _ in range(r.randint(0, 5)):
            kind = r.choice((b"Allow", b"Disallow", b"allow", b"disallow"))
            val = pattern(r, regime) if r.random() > 0.05 else b""
            lines.append(kind + b": " + val)
        if r.random() < 0.1:
            lines.append(b"Sitemap: http://site.test/sitemap.xml")
    if regime == "ties" and len(lines) > 1:
        p = pattern(r, "plain")
        lines += [b"User-agent: *", b"Allow: " + p, b"Disallow: " + p]
    sep = r.choice((b"\n", b"\r\n", b"\r"))
    return sep.join(lines) + sep


def regime_case(r: random.Random, regime: str):
    data = ruleset(r, regime)
    return data, [path(r, regime) for _ in range(6)]


# ------------------------------------------------------------------------------------------- hostile shapes


def k_star(r: random.Random, k: int, plen: int) -> tuple:
    """A-01: k wildcards between near-matching literal pieces; a path of the near-match alphabet."""
    pat = "/" + "*".join("ab" for _ in range(k)) + "c"
    p = "/" + "".join(r.choice("ab") for _ in range(plen))
    return f"User-agent: *\nDisallow: {pat}\n".encode(), p.encode()


def flood(r: random.Random, n: int, shape: str) -> bytes:
    """A-02 rule floods within 512,000 octets."""
    lines = [b"User-agent: *"]
    for i in range(n):
        if shape == "distinct":
            lines.append(b"Disallow: /" + ("p%06d" % i).encode())
        elif shape == "shared":
            lines.append(b"Disallow: /shared/prefix/" + ("%06d" % i).encode())
        elif shape == "wild10":
            lines.append(b"Disallow: /" + b"*a" * 10 + ("%d" % i).encode())
        elif shape == "repeat":
            lines.append(b"Disallow: /same")
        else:
            lines.append(b"Disallow: /a*" + ("%05d" % i).encode())
    return b"\n".join(lines) + b"\n"


def needle(m: int, rules: int = 4348) -> bytes:
    """A-03 costliest-per-unit (rv-wave1c cost1 shape): a needle of m octets ``a``*(m-2) + ``ba``."""
    pat = b"/*" + b"a" * (m - 2) + b"ba"
    # the distinguishing number after a wildcard: every rule is tried, and every one searches the near-match
    return b"User-agent: *\n" + b"".join(b"Disallow: " + pat + b"*%d\n" % i for i in range(rules))
