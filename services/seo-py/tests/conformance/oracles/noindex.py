"""
O-NOIX: whether a page asks one crawler not to index it, written from Google's "Robots meta tag, data-nosnippet and
X-Robots-Tag specifications" and Bing's webmaster guidelines:

  * ``X-Robots-Tag`` may be sent several times; each header value is a comma-separated list of rules; a value may
    start with ``<user-agent>:`` and then its rules apply to that crawler only (a later ``<other>:`` in the same
    value switches to that crawler); without a user-agent the rules apply to every crawler. Header values are
    independent: a user-agent prefix in one header does not reach into the next.
  * ``<meta name="robots">`` applies to every crawler; ``<meta name="<crawler>">`` to that crawler only.
  * ``noindex`` and ``none`` (= noindex, nofollow) ask for no indexing. Names and rules are case-insensitive.
A rule name that is itself a known rule (``unavailable_after: <date>``, ``max-snippet: 0``) is not a user agent.
"""

from __future__ import annotations

RULES = frozenset({"all", "index", "noindex", "follow", "nofollow", "none", "noarchive", "nocache", "nosnippet",
                   "notranslate", "noimageindex", "nositelinkssearchbox", "indexifembedded", "unavailable_after",
                   "max-snippet", "max-image-preview", "max-video-preview", "noodp", "noydir", "noai", "noimageai"})


def header_rules(values: list) -> list:
    """[(user agent lower-cased or None, rule)] over every header value."""
    out = []
    for value in values:
        ua = None
        for item in value.split(","):
            it = item.strip()
            if not it:
                continue
            head, sep, rest = it.partition(":")
            h = head.strip().lower()
            if sep and h not in RULES:
                ua = h
                it = rest.strip()
                head, sep, rest = it.partition(":")
                h = head.strip().lower()
            out.append((ua, h))
    return out


def noindex(token: str, header_values: list, metas: list) -> bool:
    t = token.lower()
    for ua, rule in header_rules(header_values):
        if rule in ("noindex", "none") and (ua is None or ua == t):
            return True
    for name, content in metas:
        n = name.strip().lower()
        if n not in ("robots", t):
            continue
        for r in content.split(","):
            if r.strip().lower() in ("noindex", "none"):
                return True
    return False


def self_test() -> None:
    assert noindex("Googlebot", ["noindex"], [])
    assert not noindex("Googlebot", ["otherbot: noindex"], [])
    assert noindex("otherbot", ["otherbot: noindex"], [])
    assert noindex("Googlebot", ["otherbot: noindex", "noindex"], [])
    assert not noindex("Googlebot", ["unavailable_after: 25 Jun 2010 15:00:00 PST"], [])
    assert noindex("GPTBot", [], [("GPTBot", "noindex")]) and not noindex("Googlebot", [], [("GPTBot", "noindex")])
    assert noindex("Bingbot", [], [("robots", "NONE")])
