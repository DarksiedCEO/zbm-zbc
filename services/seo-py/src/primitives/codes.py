"""
Store codes (spec wave1-spec/1 section 0.1, owner decision 3; replaces the word rule of safe_url.py).

Every value that comes from outside the service (crawled, uploaded, declared by a site) and is not on the closed
allowlist (spec D-01) is stored, in every stored or returned form, as a CODE or a value-destroying PLACEHOLDER:

  CODE         ``{c:<kind>:<16 lower hex>}``: the first 64 bits of HMAC-SHA256 under the tenant's codes key, an HKDF
               sub-key of the vault key (``vault.Coder``, info label ``seo-codes/v1``), over kind NUL canonical value.
               Deterministic per (tenant, key id, kind, value); the key id goes beside every report
               (``codes_key_id``). ``kind`` is from the closed set ``KINDS``; an IP address is never coded (D-04).
  PLACEHOLDER  ``{id}`` ``{number}`` ``{uuid}`` ``{email}`` ``{token}`` ``{param}`` ``{file}.<ext>`` ``{unreadable}``
               ``{host}``: the shape only, never a hash of the value.

``coding(coder, own=...)`` sets, for the code running inside it (one audit or one log ingest), the tenant's coder,
the tenant's registered domains (D-01 H: readable with their ``www.`` twin; every other host, the tenant's other
subdomains included, is coded) and the run's EVIDENCE MAP: {code: the raw value as observed} (at most
EVIDENCE_VALUE_MAX characters a value and EVIDENCE_MAX entries; the rest counted ``dropped``). The service keeps the
map as a vault blob of the owning tenant and reads it only for that tenant's owner displays (D-02f-h). Outside any
``coding`` block a process-random coder is used: values are still coded, never stored raw, and nothing is recorded.

Findings are computed on the RAW values in memory before anything is coded: coding changes no finding (O-01).
"""

from __future__ import annotations

import contextlib
import re
import unicodedata
from contextvars import ContextVar
from typing import Optional

KINDS = ("host", "seg", "path", "query", "rule", "text", "jsonld", "type", "lang", "date", "ctype", "ns", "root",
         "ua", "ref", "entity")
NEVER_CODED = ("ip",)
PLACEHOLDERS = ("{id}", "{number}", "{uuid}", "{email}", "{token}", "{param}", "{unreadable}", "{host}")
EVIDENCE_VALUE_MAX = 1024
EVIDENCE_MAX = 20_000
NOT_AVAILABLE = "[not available]"
CODE_RX = re.compile(r"\{c:(" + "|".join(KINDS) + r"):([0-9a-f]{16})\}")


class EvidenceMap:
    """{code: raw value as observed} of one run, bounded (spec 0.1)."""

    __slots__ = ("values", "dropped", "truncated")

    def __init__(self):
        self.values: dict = {}
        self.dropped = 0
        self.truncated = 0

    def add(self, code: str, raw: str) -> None:
        if code in self.values:
            return
        if len(self.values) >= EVIDENCE_MAX:
            self.dropped += 1
            return
        if len(raw) > EVIDENCE_VALUE_MAX:
            self.truncated += 1
            return                      # a value over the bound is not kept: the owner display shows [not available]
        self.values[code] = raw

    def as_blob(self) -> dict:
        return {"v": 1, "values": dict(self.values), "dropped": self.dropped, "over_bound": self.truncated}

    def __len__(self) -> int:
        return len(self.values)


class _Ctx:
    __slots__ = ("coder", "own", "evidence")

    def __init__(self, coder, own, evidence):
        self.coder, self.own, self.evidence = coder, own, evidence


_CTX: ContextVar = ContextVar("seo_codes", default=None)
_FALLBACK: dict = {}


def _fallback():
    c = _FALLBACK.get("c")
    if c is None:
        import vault
        c = _FALLBACK["c"] = vault.Coder(vault.ephemeral_vault_key(), "ephemeral-codes")
    return _Ctx(c, (), None)


def _ctx() -> _Ctx:
    return _CTX.get() or _fallback()


def _host_norm(h: str) -> Optional[str]:
    h = (h or "").strip().lower().rstrip(".")
    if not h:
        return None
    try:
        return h.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None


@contextlib.contextmanager
def coding(coder, own=(), evidence: Optional[EvidenceMap] = None):
    """Within this block every stored form is coded with ``coder`` (a ``vault.Coder``); ``own`` are the tenant's
    registered domains. Yields the run's EvidenceMap."""
    ev = evidence if evidence is not None else EvidenceMap()
    owns = tuple(x for x in (_host_norm(d) for d in own) if x)
    tok = _CTX.set(_Ctx(coder, owns, ev))
    try:
        yield ev
    finally:
        _CTX.reset(tok)


@contextlib.contextmanager
def within(domain):
    """For an agent run outside any coding block (a unit test, a tool): the process-random coder with the audited
    domain as the one registered domain (D-01 H). Inside a coding block (the service) it changes nothing."""
    if _CTX.get() is not None:
        yield None
        return
    c = _fallback()
    tok = _CTX.set(_Ctx(c.coder, tuple(x for x in (_host_norm(domain or ""),) if x), None))
    try:
        yield None
    finally:
        _CTX.reset(tok)


def key_id() -> Optional[str]:
    c = _CTX.get()
    return None if c is None else c.coder.key_id


def canonical(kind: str, raw: str) -> str:
    if kind == "host":
        return _host_norm(raw) or raw.strip().lower()
    if kind in ("text", "jsonld", "entity"):
        return " ".join(raw.split())
    return raw


def code(kind: str, raw) -> str:
    """``{c:<kind>:<16 hex>}`` for ``raw`` (the raw value goes into the run's evidence map)."""
    if kind in NEVER_CODED or kind not in KINDS:
        raise ValueError("not a code kind")
    if not isinstance(raw, str):
        raw = str(raw)
    c = _ctx()
    out = "{c:" + kind + ":" + c.coder.code(kind, canonical(kind, raw)) + "}"
    if c.evidence is not None:
        c.evidence.add(out, raw)
    return out


def measure_text(raw) -> dict:
    """The measured attributes of a text field (B-CD-05): length in code points after white space is collapsed,
    and whether it is empty."""
    s = " ".join(raw.split()) if isinstance(raw, str) else ""
    return {"length": len(s), "empty": not s}


# ------------------------------------------------------------------------------------------- D-01 H, W, R


def own_host(host) -> bool:
    """D-01 H: the tenant's registered domains and their ``www.`` twin, exactly (lower-case IDNA, trailing dot
    removed). Outside a coding block nothing is own."""
    h = _host_norm(host) if isinstance(host, str) else None
    if h is None:
        return False
    own = _ctx().own
    return any(h == d or h == "www." + d for d in own)


WELL_KNOWN = frozenset({"robots.txt", "llms.txt", "llms-full.txt", "favicon.ico", "ads.txt", "app-ads.txt",
                        "security.txt", "humans.txt", "manifest.json", "site.webmanifest", "browserconfig.xml",
                        "apple-touch-icon.png", "rss.xml", "atom.xml", "feed", "index.html", "index.php",
                        ".well-known", "sitemap.xml", "sitemap_index.xml", "sitemap-index.xml", "wp-sitemap.xml"})
SITEMAP_WORDS = ("index pages page posts post products product categories category tags tag images image video "
                 "videos news blog misc main authors author taxonomies users").split()
_S = "(?:" + "|".join(SITEMAP_WORDS) + ")"
_N = "[0-9]{1,3}"
_SITEMAP_NAME = re.compile(
    r"(?:sitemap[-_]" + _S + r"(?:[-_]" + _N + r")?|sitemap[-_]" + _N + r"|" + _S + r"-sitemap(?:" + _N + r")?|"
    r"wp-sitemap-" + _S + "-" + _S + r"(?:-" + _N + r")?)\.xml(?:\.gz)?")


def well_known(seg) -> bool:
    """D-01 W: a well-known file name as a whole path segment (case-insensitive, closed grammar)."""
    if not isinstance(seg, str) or not seg.isascii():
        return False
    low = seg.lower()
    if low.endswith(".gz") and low[:-3] in ("sitemap.xml", "sitemap_index.xml", "sitemap-index.xml",
                                            "wp-sitemap.xml"):
        return True
    return low in WELL_KNOWN or bool(_SITEMAP_NAME.fullmatch(low))


_STRUCTURAL = frozenset({"/", "/*", "*", "/$", "/*$"})


def rule_readable(pattern) -> bool:
    """D-01 R: a robots pattern made only of ``/``, ``*`` and a final ``$`` of the closed list, or ``/`` + one
    well-known name."""
    if not isinstance(pattern, str):
        return False
    if pattern in _STRUCTURAL:
        return True
    return pattern.startswith("/") and "/" not in pattern[1:] and well_known(pattern[1:])


# ------------------------------------------------------------------------------------------- owner displays


def resolve(obj, values: Optional[dict]):
    """``obj`` with every code replaced by its raw value from ``values`` (an evidence map's ``values``), for the
    owning tenant's display only; a code without a value (map lost, value over the bound) is NOT_AVAILABLE."""
    if isinstance(obj, str):
        if "{c:" not in obj:
            return obj
        return CODE_RX.sub(lambda m: (values or {}).get(m.group(0), NOT_AVAILABLE), obj)
    if isinstance(obj, dict):
        return {resolve(k, values): resolve(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve(v, values) for v in obj]
    return obj


def has_codes(obj) -> bool:
    if isinstance(obj, str):
        return bool(CODE_RX.search(obj))
    if isinstance(obj, dict):
        return any(has_codes(k) or has_codes(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(has_codes(v) for v in obj)
    return False


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s)
