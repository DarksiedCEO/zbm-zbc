"""
Rich text an agent may write into a client's store (AEGIS round 1 H1: the round-0 denylist was bypassed by
``<svg/onload>``, ``<img src=x/onerror>`` and entity- or whitespace-encoded ``javascript:``).

An ALLOWLIST rebuild, standard library only (``html.parser``): the value is parsed and re-serialised from a fixed
set of tags and attributes into ONE canonical form, and it is accepted only when ``sanitize(value) == value`` — so
whatever a browser parses is exactly the tree checked here. Consequences, all deliberate:

* tags: p, br, strong, em, b, i, u, s, ul, ol, li, h2..h6, blockquote, a, img — lower-case, nothing else (no svg,
  math, script, style, iframe, form, object, template, noscript, …); attributes: ``a`` href / title, ``img`` src /
  alt / width / height; no ``style``, no ``on*``, no namespaced attribute;
* a URL attribute is ``https://host[/path]`` or a same-site relative path (``/x``, never ``//`` or ``/\\``) or a
  fragment; no other scheme, no whitespace, no control character, no backslash, no ``%`` escape of ``/``, ``\\`` or a
  control character anywhere in it;
* character references are not accepted at all except ``&amp;`` ``&lt;`` ``&gt;`` ``&quot;`` (the serialiser's own):
  ``&#106;avascript:`` decodes to a different text and so never equals its rebuild;
* attribute values are double-quoted, each attribute once, ``<br>`` / ``<img …>`` with no ``/``, every other element
  explicitly closed in order, no comment, declaration, processing instruction or CDATA;
* a content model with no browser-repaired nesting (mutation XSS feeds on misnesting): ``ul``/``ol`` hold only
  ``li``; ``p``, ``h*``, ``a``, inline tags hold only inline content and text; ``a`` never nests ``a``.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Optional

INLINE = frozenset({"strong", "em", "b", "i", "u", "s", "a", "br", "img"})
BLOCK = frozenset({"p", "ul", "ol", "li", "h2", "h3", "h4", "h5", "h6", "blockquote"})
VOID = frozenset({"br", "img"})
ATTRS = {"a": ("href", "title"), "img": ("src", "alt", "width", "height")}
_HTTPS = re.compile(r"https://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?(?:[/?#][\x21-\x7e]{0,2000})?")
_REL = re.compile(r"/(?![/\\])[\x21-\x7e]{0,2000}|#[\x21-\x7e]{0,200}")
_BAD_ESCAPE = re.compile(r"%(?:2f|5c|0[0-9a-f]|1[0-9a-f]|7f|20)", re.I)


def _text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(s: str) -> str:
    return _text(s).replace('"', "&quot;")


def safe_url(v: str, allow_fragment: bool = True) -> bool:
    if not isinstance(v, str) or "\\" in v or _BAD_ESCAPE.search(v):
        return False
    if v.startswith("#") and not allow_fragment:
        return False
    return bool(_HTTPS.fullmatch(v) or _REL.fullmatch(v))


def _allowed_children(parent: Optional[str]) -> frozenset:
    if parent is None or parent in ("blockquote", "li"):
        return frozenset((BLOCK - {"li"}) | INLINE)
    if parent in ("ul", "ol"):
        return frozenset({"li"})
    if parent == "a":
        return frozenset(INLINE - {"a"})
    return INLINE                                   # p, h*, inline tags: inline only


class _Rebuild(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.bad = False

    def _parent(self) -> Optional[str]:
        return self.stack[-1] if self.stack else None

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs)

    def handle_startendtag(self, tag, attrs):     # "<br/>" is not canonical: refused by inequality, kept sane here
        self._start(tag, attrs)
        if tag not in VOID:
            self.bad = True

    def _start(self, tag, attrs):
        if tag not in INLINE | BLOCK or tag not in _allowed_children(self._parent()) \
                or ("a" in self.stack and tag == "a"):
            self.bad = True
            return
        allowed = ATTRS.get(tag, ())
        seen, parts = set(), [tag]
        for name, value in attrs:
            if name not in allowed or name in seen or value is None:
                self.bad = True
                return
            seen.add(name)
            if name in ("href", "src") and not safe_url(value, allow_fragment=(name == "href")):
                self.bad = True
                return
            if name in ("width", "height") and not re.fullmatch(r"[1-9][0-9]{0,3}", value):
                self.bad = True
                return
            parts.append(f'{name}="{_attr(value)}"')
        self.out.append("<" + " ".join(parts) + ">")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID or not self.stack or self.stack[-1] != tag:
            self.bad = True
            return
        self.stack.pop()
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._parent() in ("ul", "ol") and data.strip():
            self.bad = True
            return
        self.out.append(_text(data))

    def handle_comment(self, data):
        self.bad = True

    def handle_decl(self, decl):
        self.bad = True

    def handle_pi(self, data):
        self.bad = True

    def unknown_decl(self, data):
        self.bad = True


def sanitize(value: str) -> Optional[str]:
    """The canonical rebuild, or None when the value holds anything outside the allowlist."""
    if not isinstance(value, str) or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]", value):
        return None
    p = _Rebuild()
    try:
        p.feed(value)
        p.close()
    except Exception:                                # noqa: BLE001 — a parser error is a refusal
        return None
    if p.bad or p.stack or p.rawdata:
        return None
    return "".join(p.out)


def is_safe(value: str) -> bool:
    return sanitize(value) == value
