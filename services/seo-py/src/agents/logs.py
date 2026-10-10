"""
Selene's log task — crawler access from FIRST-PARTY server logs (P2; Wave 2 stage 1).

A client uploads its access log in chunks (Common Log Format, Combined Log Format, or JSON lines). Each chunk is
parsed here, in memory, and only AGGREGATES leave this module:
  - per bot family (agents/bots.py, versioned): requests, status-code mix, distinct keyed-hash client IPs, the paths
    crawled (query strings dropped, bounded), and how many requests were verified / spoofed / merely claimed;
  - totals: lines, non-bot requests, requests without a User-Agent, quarantined lines by reason.
Nothing identifying leaves this module (AEGIS 1472041 H1): raw lines, IPs, IP hashes, User-Agent strings and raw URL
paths are never returned, stored or logged. A path is reduced to a TEMPLATE first (``template_path``: e-mail
addresses, UUIDs, numeric ids, long hex / base64 / token-like segments replaced by {email}, {uuid}, {id}, {token};
query strings dropped); exact paths are only compared, in memory, against the public sitemap sample fixed when the
ingest was created (its indices are kept, not the paths) and against robots.txt rules (a count is kept). Distinct
client IPs are estimated with a HyperLogLog sketch of keyed hashes (256 registers: no hash and no IP is kept).

Verification: a User-Agent is a CLAIM. A hit counts as ``verified`` only when the bot-verification port confirmed the
client IP (reverse DNS + forward-confirm against the operator's documented host names); ``spoofed`` when the port
showed the claim false; otherwise ``claimed`` (port NOT_CONNECTED, family not DNS-verifiable, DNS error, or the
per-ingest lookup budget used up). Log text is DATA: an injection string in a path or User-Agent changes nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import re
import unicodedata
from typing import Callable, Optional
from urllib.parse import unquote, urlsplit

from agents import bots

FORMATS = ("combined", "common", "jsonl")
LINE_MAX = 8192
TEMPLATES_PER_FAMILY = 500
PATH_MAX = 2000
HLL_M = 256
QUARANTINE_REASONS = ("LINE_TOO_LONG", "NOT_UTF8", "MALFORMED", "BAD_IP", "BAD_STATUS")
_CLF = re.compile(r'^(\S+) \S+ \S+ \[[^\]]{1,64}\] "([A-Z]{1,12}) (\S{1,4096})(?: HTTP/[0-9.]{1,5})?" (\d{3}) (?:\d+|-)'
                  r'(?: "([^"\\]*(?:\\.[^"\\]*)*)" "([^"\\]*(?:\\.[^"\\]*)*)")?\s*$')
_UA_RX = {t: re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", re.I) for t in bots.ua_families()}


_EMAIL = re.compile(r"[^/@\s]{1,64}@[^/@\s]{1,255}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
_HEX = re.compile(r"[0-9a-fA-F]{16,}")
_DIGITS = re.compile(r"\d{4,}")
_TOKENISH = re.compile(r"[A-Za-z0-9_\-=+.~]{20,}")
TEMPLATE_RULES = ("decode once; NFKC-normalise and fold confusable at-signs; drop query and fragment; per segment: "
                  "an e-mail in any spelling ('@', '(at)', '[at]', ' at ... dot', %40) -> {email}; UUID -> {uuid}; "
                  "7+ digits in total, whatever separates them -> {number}; only digits -> {id}; a file name with "
                  "a non-code extension -> {file}.ext; 16+ hex, 20+ token characters, 8+ characters mixing letters "
                  "and 2+ digits, or over 40 characters -> {token}; any run of 4+ digits left -> {id}; short numeric "
                  "segments next to each other with 7+ digits together -> {number}")


_AT_WORD = re.compile(r"[(\[{<]\s*at\s*[)\]}>]|\s+at\s+|%40|&#0*64;|&commat;|\bat\b(?=[^/]*\bdot\b)", re.I)
_FILE = re.compile(r"(.+)\.([A-Za-z0-9]{1,5})")
STATIC_EXT = frozenset({"css", "js", "mjs", "map", "svg", "ico", "woff", "woff2", "ttf", "eot", "otf", "xml", "txt",
                        "html", "htm", "php", "asp", "aspx", "jsp", "json", "webmanifest", "xhtml", "rss", "atom"})
# Fold what NFKC leaves: other "at" signs to "@".
_FOLD = str.maketrans({"\uff20": "@", "\ufe6b": "@", "\u24d0": "a"})


def _digits(s: str) -> int:
    return sum(c.isdigit() for c in s)


def _segment(seg: str) -> str:
    if not seg:
        return ""
    if "@" in seg or _EMAIL.search(seg) or _AT_WORD.search(seg):
        return "{email}"
    if _UUID.fullmatch(seg):
        return "{uuid}"
    if _digits(seg) >= 7:                      # phone numbers, SSNs, card numbers, long ids, across any separator
        return "{number}"
    if seg.isdigit():
        return "{id}"
    m = _FILE.fullmatch(seg)
    if m and m.group(2).lower() not in STATIC_EXT:
        return "{file}." + m.group(2).lower()  # a document or image file name may name a person
    if _HEX.fullmatch(seg) or len(seg) > 40 or _TOKENISH.fullmatch(seg) or (
            len(seg) >= 8 and _digits(seg) >= 2 and any(c.isalpha() for c in seg)):
        return "{token}"
    return _DIGITS.sub("{id}", seg)[:40]


def template_path(target: str) -> Optional[str]:
    """A path with every identifier-looking part replaced by a typed placeholder; None when not a path.
    NFKC-normalised and confusables folded first (fullwidth digits and at-signs become ASCII)."""
    p = _path(target)
    if p is None:
        return None
    p = unicodedata.normalize("NFKC", p).translate(_FOLD)
    segs = p.split("/")[1:]
    out = [_segment(s) for s in segs]
    # digits split over several short numeric segments (/123/45/6789) count together
    i = 0
    while i < len(segs):
        j = i
        while j < len(segs) and segs[j].isdigit() and out[j] == "{id}":
            j += 1
        if j - i >= 2 and sum(_digits(segs[k]) for k in range(i, j)) >= 7:
            for k in range(i, j):
                out[k] = "{number}"
        i = max(j, i + 1)
    return ("/" + "/".join(out))[:300]


def _hll_add(regs: list, key: bytes, ip: str) -> None:
    x = int.from_bytes(hmac.new(key, ip.encode("ascii"), hashlib.sha256).digest()[:8], "big")
    idx, w = x >> 56, x & ((1 << 56) - 1)
    rank = 56 - w.bit_length() + 1
    if rank > regs[idx]:
        regs[idx] = rank


def hll_estimate(regs: list) -> int:
    m = len(regs)
    est = (0.7213 / (1 + 1.079 / m)) * m * m / sum(2.0 ** -r for r in regs)
    zeros = regs.count(0)
    if est <= 2.5 * m and zeros:
        est = m * math.log(m / zeros)
    return int(round(est))


def classify(ua: Optional[str]) -> Optional[str]:
    if not ua:
        return None
    for tok in bots.ua_families():                     # longest first
        if _UA_RX[tok].search(ua):
            return tok
    return None


def _norm_ip(raw: str) -> Optional[str]:
    try:
        return str(ipaddress.ip_address(raw.strip("[]").split("%")[0]))
    except ValueError:
        return None


def _path(target: str) -> Optional[str]:
    try:
        p = urlsplit(target).path if "://" in target else target.split("?", 1)[0].split("#", 1)[0]
    except ValueError:
        return None
    p = unquote(p)[:PATH_MAX]
    if not p.startswith("/") or any(ord(c) < 0x20 or ord(c) == 0x7F for c in p):
        return None
    return p


def parse_line(fmt: str, line: str) -> Optional[dict]:
    """{ip, path, status, ua} or None (malformed). In memory only."""
    if fmt == "jsonl":
        try:
            d = json.loads(line)
        except (ValueError, RecursionError):
            return None
        if not isinstance(d, dict):
            return None
        ip = d.get("ip", d.get("remote_addr", d.get("client_ip")))
        path = d.get("path", d.get("uri", d.get("request_uri")))
        status = d.get("status", d.get("status_code"))
        ua = d.get("user_agent", d.get("http_user_agent", d.get("ua")))
        if isinstance(status, str) and status.isdigit():
            status = int(status)
        if not isinstance(ip, str) or not isinstance(path, str) or type(status) is not int:
            return None
        return {"ip": ip, "path": path, "status": status, "ua": ua if isinstance(ua, str) else None}
    m = _CLF.match(line)
    if not m:
        return None
    ua = m.group(6) if fmt == "combined" else None
    return {"ip": m.group(1), "path": m.group(3), "status": int(m.group(4)), "ua": ua if ua not in (None, "-") else None}


def _status_class(s: int) -> str:
    return f"{s // 100}xx" if 100 <= s <= 599 else "other"


def empty_delta() -> dict:
    return {"lines": 0, "non_bot": 0, "no_user_agent": 0, "quarantined": {}, "families": {}}


def _family() -> dict:
    return {"requests": 0, "verified": 0, "spoofed": 0, "claimed": 0, "status": {}, "templates": {},
            "templates_dropped": 0, "hll": [0] * HLL_M, "robots_blocked": 0, "sample_hits": []}


def process_chunk(fmt: str, data: bytes, key: bytes, verify: Callable[[str, str], str], seen: dict, budget: dict,
                  robots=None, sample: Optional[dict] = None) -> dict:
    """One chunk -> an aggregate delta with nothing identifying in it. ``verify(ip, token)`` is the port (raw IP in
    memory only); ``seen`` caches verdicts in memory for this ingest; ``budget["left"]`` bounds port lookups;
    ``robots`` the parsed robots.txt fixed at ingest creation; ``sample`` {sitemap path: index}."""
    out = empty_delta()
    q = out["quarantined"]
    sample = sample or {}
    for raw in data.split(b"\n"):
        raw = raw.rstrip(b"\r")
        if not raw.strip():
            continue
        out["lines"] += 1
        if len(raw) > LINE_MAX:
            q["LINE_TOO_LONG"] = q.get("LINE_TOO_LONG", 0) + 1
            continue
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            q["NOT_UTF8"] = q.get("NOT_UTF8", 0) + 1
            continue
        rec = parse_line(fmt, line)
        if rec is None:
            q["MALFORMED"] = q.get("MALFORMED", 0) + 1
            continue
        ip = _norm_ip(rec["ip"])
        if ip is None:
            q["BAD_IP"] = q.get("BAD_IP", 0) + 1
            continue
        if not 100 <= rec["status"] <= 599:
            q["BAD_STATUS"] = q.get("BAD_STATUS", 0) + 1
            continue
        if rec["ua"] is None:
            out["no_user_agent"] += 1
            continue
        tok = classify(rec["ua"])
        if tok is None:
            out["non_bot"] += 1
            continue
        fam = out["families"].setdefault(tok, _family())
        fam["requests"] += 1
        sc = _status_class(rec["status"])
        fam["status"][sc] = fam["status"].get(sc, 0) + 1
        exact = _path(rec["path"])
        tpl = template_path(rec["path"])
        if tpl is not None:
            if tpl in fam["templates"] or len(fam["templates"]) < TEMPLATES_PER_FAMILY:
                fam["templates"][tpl] = fam["templates"].get(tpl, 0) + 1
            else:
                fam["templates_dropped"] += 1
        if exact is not None:
            if robots is not None and not robots.allowed(tok, exact):
                fam["robots_blocked"] += 1
            i = sample.get(exact)
            if i is not None and i not in fam["sample_hits"]:
                fam["sample_hits"].append(i)
        _hll_add(fam["hll"], key, ip)
        h = hmac.new(key, ip.encode("ascii"), hashlib.sha256).hexdigest()[:32]
        key_v = f"{tok}|{h}"
        v = seen.get(key_v)
        if v is None:
            if budget["left"] > 0:
                budget["left"] -= 1
                try:
                    v = verify(ip, tok)
                except Exception:              # a port failure is "no claim either way"
                    v = "error"
            else:
                v = "budget"
            if len(seen) < 100_000:            # memory bound; past it every lookup counts against the budget
                seen[key_v] = v
        if v == "verified":
            fam["verified"] += 1
        elif v == "failed":
            fam["spoofed"] += 1
        else:
            fam["claimed"] += 1
    for f in out["families"].values():
        f["sample_hits"].sort()
    return out


def merge(total: dict, delta: dict) -> dict:
    """Fold a chunk delta into the ingest totals (pure; used by replay as well). Bounded: templates per family,
    256 sketch registers, sample indices bounded by the sample."""
    for k in ("lines", "non_bot", "no_user_agent"):
        total[k] += delta[k]
    for r, n in delta["quarantined"].items():
        total["quarantined"][r] = total["quarantined"].get(r, 0) + n
    for tok, f in delta["families"].items():
        t = total["families"].setdefault(tok, _family())
        for k in ("requests", "verified", "spoofed", "claimed", "robots_blocked", "templates_dropped"):
            t[k] += f[k]
        for s, n in f["status"].items():
            t["status"][s] = t["status"].get(s, 0) + n
        for p, n in f["templates"].items():
            if p in t["templates"] or len(t["templates"]) < TEMPLATES_PER_FAMILY:
                t["templates"][p] = t["templates"].get(p, 0) + n
            else:
                t["templates_dropped"] += n
        t["hll"] = [max(a, b) for a, b in zip(t["hll"], f["hll"])]
        t["sample_hits"] = sorted(set(t["sample_hits"]) | set(f["sample_hits"]))
    return total
