"""
Is a cited host the tenant's site? (ADR 0017 W4-3). Exact host or a subdomain of it (with the ``www.`` twin), compared
as IDNA (A-labels), never by substring: ``evilacme.com``, ``acme.com.evil.com`` and ``acme.co`` are not ``acme.com``.

A host whose Unicode form FOLDS (the repo's shared lookalike table, ``lookalikes.py``) to the tenant's domain but is
not the same name — ``аcme.com`` with a Cyrillic а, ``xn--cme-5cd.com``, full-width letters NFKC does not reach — is a
LOOKALIKE: reported as such, never counted as a citation of the tenant.

Python's ``idna`` codec is IDNA2003 (nameprep): it maps ``ß`` to ``ss`` and ``ς`` to ``σ`` and silently DROPS
zero-width joiners and soft hyphens, so ``faß.de`` would "be" ``fass.de``, which IDNA2008/UTS46 (browsers, registries)
treat as two different names (AEGIS M1). No new dependency: a host is a host name here only when, after NFKC and lower
case (the mapping UTS46 also applies: ``ＳＩＴＥ.test`` is ``site.test``), every label survives the IDNA round trip
unchanged (a U-label encodes to an A-label that decodes back to it; an ``xn--`` A-label decodes to a U-label that
encodes back to it), and it holds no format character, default-ignorable code point (ZWJ, ZWNJ, soft hyphen, ...),
control character or space. Any other host is not the tenant: ``faß.de``, ``ς.com``, ``xn--fa-hia.de`` and
``ac\u200dme.com`` never match ``fass.de`` / ``σ.com`` / ``acme.com`` as exact or subdomain; when such a host,
loosened (invisibles removed, ``ß``→``ss``, ``ς``→``σ``), folds to the tenant's domain it is a LOOKALIKE. The cost: a
tenant whose own domain needs one of these characters (a final sigma, ``ß``) matches nothing; register its ASCII or
another form.
"""

from __future__ import annotations

import unicodedata
from encodings import idna as _idna
from typing import Optional

import lookalikes

_TABLE = lookalikes.Table()
EXACT, SUBDOMAIN, LOOKALIKE = "exact", "subdomain", "lookalike"

# AEGIS L3: a vendored SUBSET of the Public Suffix List (https://publicsuffix.org/list/, checked Oct 2026): the
# multi-label ICANN suffixes and the PRIVATE-section hosting platforms where customers' sites are subdomains. A tenant
# may not register one of these (svc_tenants: DOMAIN_INVALID), and if one is ever a tenant's domain (an old log) it
# matches nothing, so "github.io" can never claim every customer's "x.github.io" as its subdomain. Single-label TLDs
# are refused by the domain syntax already. The list is a subset, not the PSL: a suffix missing here is caught only by
# Andre registering the domain by hand (he registers every domain).
PUBLIC_SUFFIXES = frozenset("""
co.uk org.uk me.uk ltd.uk plc.uk net.uk ac.uk gov.uk sch.uk nhs.uk police.uk
com.au net.au org.au edu.au gov.au id.au asn.au co.nz net.nz org.nz govt.nz ac.nz
co.jp ne.jp or.jp ac.jp go.jp com.br net.br org.br gov.br com.cn net.cn org.cn gov.cn com.mx org.mx gob.mx
co.za org.za gov.za co.in net.in org.in gov.in firm.in com.sg edu.sg gov.sg com.hk org.hk gov.hk
com.tr org.tr gov.tr co.kr or.kr go.kr com.tw org.tw gov.tw co.il org.il gov.il com.ar com.co com.pe com.ph
com.my com.vn com.pk com.ng com.eg com.sa com.ua co.id or.id ac.id go.id co.th in.th go.th
github.io githubusercontent.com gitlab.io netlify.app netlify.com vercel.app now.sh pages.dev workers.dev
herokuapp.com herokussl.com azurewebsites.net azurestaticapps.net cloudapp.net trafficmanager.net
cloudfront.net amazonaws.com s3.amazonaws.com elasticbeanstalk.com awsapprunner.com amplifyapp.com
appspot.com web.app firebaseapp.com run.app cloudfunctions.net blogspot.com
wordpress.com wpengine.com wixsite.com squarespace.com myshopify.com webflow.io weebly.com
onrender.com fly.dev railway.app up.railway.app glitch.me repl.co replit.app replit.dev ngrok.io ngrok-free.app
ngrok.app surge.sh readthedocs.io gitbook.io notion.site carrd.co framer.website framer.app bubbleapps.io
dyndns.org no-ip.org duckdns.org ddns.net
""".split())


_DOTS = str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": "."})


def _bad_char(c: str) -> bool:
    """Format, default-ignorable, control, surrogate, private-use, unassigned or space characters."""
    return lookalikes.is_invisible(c) or unicodedata.category(c) in ("Cc", "Cs", "Co", "Cn", "Zs", "Zl", "Zp")


def _label(lb: str) -> Optional[str]:
    """One label as its lower-case A-label, or None when the IDNA round trip changes it (IDNA2003 mappings)."""
    try:
        if lb.isascii():
            if not lb.startswith("xn--"):
                return lb
            u = lb[4:].encode("ascii").decode("punycode")
            if u.isascii() or any(_bad_char(c) for c in u) or unicodedata.normalize("NFKC", u).lower() != u:
                return None
            return lb if _idna.ToASCII(u).decode("ascii").lower() == lb else None
        a = _idna.ToASCII(lb).decode("ascii").lower()
        if not a.startswith("xn--") or a[4:].encode("ascii").decode("punycode") != lb:
            return None                                   # ß -> ss, ς -> σ, a dropped joiner: not this name
        return a
    except (UnicodeError, ValueError):
        return None


def _ascii(host: str) -> Optional[str]:
    """``host`` as lower-case A-labels without a trailing dot, or None for something that is not a host name (see
    the module docstring for the IDNA round-trip rule)."""
    if not isinstance(host, str):
        return None
    h = host.strip().rstrip(".").lower()
    if not h or len(h) > 253 or any(c in h for c in "/\\@:?#% \t\r\n"):
        return None
    if any(_bad_char(c) for c in h):
        return None
    h = h.translate(_DOTS)
    n = unicodedata.normalize("NFKC", h).lower()
    if n.count(".") != h.count(".") or any(_bad_char(c) for c in n):
        return None                                       # NFKC must not invent a label separator (U+2488 "1.")
    labels = [_label(lb) for lb in n.rstrip(".").split(".")]
    if any(lb is None or not lb or len(lb) > 63 for lb in labels):
        return None
    a = ".".join(labels)
    return a if len(a) <= 253 else None


def _loose(host: str) -> Optional[str]:
    """A refused host, loosened the way IDNA2003 would (invisibles removed, ß->ss, ς->σ), for the lookalike check."""
    if not isinstance(host, str):
        return None
    h = unicodedata.normalize("NFKC", lookalikes.strip_invisible(host)).lower().translate(_DOTS)
    h = h.replace("ß", "ss").replace("ς", "σ").strip().rstrip(".")
    out = []
    for lb in h.split("."):
        if lb.startswith("xn--"):
            try:
                lb = lb[4:].encode("ascii").decode("punycode").replace("ß", "ss").replace("ς", "σ")
            except (UnicodeError, ValueError):
                return None
        out.append(lb)
    h = ".".join(out)
    return h if h and len(h) <= 253 and not any(c in h for c in "/\\@:?#% \t\r\n") else None


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


def is_public_suffix(domain: str) -> bool:
    """The domain (or its www. twin) is on the vendored public-suffix subset (AEGIS L3)."""
    d = _ascii(domain)
    return d is not None and (d in PUBLIC_SUFFIXES or _site(d) in PUBLIC_SUFFIXES)


def match(host: str, domain: str) -> Optional[str]:
    """``exact`` (the domain or its www. twin), ``subdomain``, ``lookalike`` or None. A public-suffix tenant domain
    matches nothing."""
    h, d = _ascii(host), _ascii(domain)
    if d is None or d in PUBLIC_SUFFIXES or _site(d) in PUBLIC_SUFFIXES:
        return None
    if h is None:
        loose = _loose(host)
        if loose is None:
            return None
        fh, fd = _TABLE.fold_cased(loose), _TABLE.fold_cased(_unicode(_site(d)))
        fh = fh[4:] if fh.startswith("www.") else fh
        return LOOKALIKE if fh == fd or fh.endswith("." + fd) else None
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
