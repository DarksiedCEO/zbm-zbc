"""
Is a cited host the tenant's site? (ADR 0017 W4-3). Exact host or a subdomain of it (with the ``www.`` twin), compared
as IDNA (A-labels), never by substring: ``evilacme.com``, ``acme.com.evil.com`` and ``acme.co`` are not ``acme.com``.

A host whose Unicode form FOLDS (the repo's shared lookalike table, ``lookalikes.py``) to the tenant's domain but is
not the same name — ``аcme.com`` with a Cyrillic а, ``xn--cme-8cd.com``, full-width letters NFKC does not reach — is a
LOOKALIKE: reported as such, never counted as a citation of the tenant.
"""

from __future__ import annotations

from typing import Optional

import lookalikes

_TABLE = lookalikes.Table()
EXACT, SUBDOMAIN, LOOKALIKE = "exact", "subdomain", "lookalike"


def _ascii(host: str) -> Optional[str]:
    """``host`` as lower-case A-labels without a trailing dot, or None for something that is not a host name."""
    if not isinstance(host, str):
        return None
    h = host.strip().rstrip(".").lower()
    if not h or len(h) > 253 or any(c in h for c in "/\\@:?#% \t\r\n"):
        return None
    try:
        a = h.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    labels = a.split(".")
    if any(not lb or len(lb) > 63 for lb in labels):
        return None
    return a


def _unicode(ascii_host: str) -> str:
    out = []
    for lb in ascii_host.split("."):
        if lb.startswith("xn--"):
            try:
                lb = lb.encode("ascii").decode("idna")
            except UnicodeError:
                pass
        out.append(lb)
    return ".".join(out)


def _site(d: str) -> str:
    return d[4:] if d.startswith("www.") else d


def match(host: str, domain: str) -> Optional[str]:
    """``exact`` (the domain or its www. twin), ``subdomain``, ``lookalike`` or None."""
    h, d = _ascii(host), _ascii(domain)
    if h is None or d is None:
        return None
    hs, ds = _site(h), _site(d)
    if hs == ds:
        return EXACT
    if h.endswith("." + ds):
        return SUBDOMAIN
    fh, fd = _TABLE.fold_cased(_unicode(hs)), _TABLE.fold_cased(_unicode(ds))
    if fh == fd or fh.endswith("." + fd):
        return LOOKALIKE
    return None


def on_site(host: str, domain: str) -> bool:
    return match(host, domain) in (EXACT, SUBDOMAIN)
