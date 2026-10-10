"""
Selene's log task — crawler access from FIRST-PARTY server logs (P2; Wave 2 stage 1).

A client uploads its access log in chunks (Common Log Format, Combined Log Format, or JSON lines). Each chunk is
parsed here, in memory, and only AGGREGATES leave this module:
  - per bot family (agents/bots.py, versioned): requests, status-code mix, distinct keyed-hash client IPs, the paths
    crawled (query strings dropped, bounded), and how many requests were verified / spoofed / merely claimed;
  - totals: lines, non-bot requests, requests without a User-Agent, quarantined lines by reason.
Raw lines, raw IPs and User-Agent strings are never returned, stored or logged. An IP is kept only as
HMAC-SHA256(SEO_LOG_HASH_KEY, ip), truncated; rotating that key (the retention rule, ADR 0017) makes earlier hashes
unlinkable to any IP.

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
import re
from typing import Callable, Optional
from urllib.parse import unquote, urlsplit

from agents import bots

FORMATS = ("combined", "common", "jsonl")
LINE_MAX = 8192
PATHS_PER_FAMILY = 2000
PATH_MAX = 300
HASHES_PER_FAMILY = 5000
QUARANTINE_REASONS = ("LINE_TOO_LONG", "NOT_UTF8", "MALFORMED", "BAD_IP", "BAD_STATUS")
_CLF = re.compile(r'^(\S+) \S+ \S+ \[[^\]]{1,64}\] "([A-Z]{1,12}) (\S{1,4096})(?: HTTP/[0-9.]{1,5})?" (\d{3}) (?:\d+|-)'
                  r'(?: "([^"\\]*(?:\\.[^"\\]*)*)" "([^"\\]*(?:\\.[^"\\]*)*)")?\s*$')
_UA_RX = {t: re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", re.I) for t in bots.ua_families()}


def ip_hash(key: bytes, ip: str) -> str:
    return hmac.new(key, ip.encode("ascii"), hashlib.sha256).hexdigest()[:32]


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
    """{ip, method, path, status, ua} or None (malformed)."""
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


def process_chunk(fmt: str, data: bytes, key: bytes, verify: Callable[[str, str], str], seen_hashes: dict,
                  budget: dict) -> dict:
    """One chunk -> an aggregate delta. ``verify(ip, token)`` is the port (raw IP in memory only); ``seen_hashes``
    caches verification per hashed IP for this ingest; ``budget["left"]`` bounds port lookups."""
    out = empty_delta()
    q = out["quarantined"]
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
        h = ip_hash(key, ip)
        fam = out["families"].setdefault(tok, {"requests": 0, "verified": 0, "spoofed": 0, "claimed": 0,
                                               "status": {}, "paths": {}, "ip_hashes": []})
        fam["requests"] += 1
        sc = _status_class(rec["status"])
        fam["status"][sc] = fam["status"].get(sc, 0) + 1
        p = _path(rec["path"])
        if p is not None and (p in fam["paths"] or len(fam["paths"]) < PATHS_PER_FAMILY):
            fam["paths"][p] = fam["paths"].get(p, 0) + 1
        if h not in fam["ip_hashes"] and len(fam["ip_hashes"]) < HASHES_PER_FAMILY:
            fam["ip_hashes"].append(h)
        key_v = f"{tok}|{h}"
        v = seen_hashes.get(key_v)
        if v is None:
            if budget["left"] > 0:
                budget["left"] -= 1
                try:
                    v = verify(ip, tok)
                except Exception:              # a port failure is "no claim either way"
                    v = "error"
            else:
                v = "budget"
            seen_hashes[key_v] = v
        if v == "verified":
            fam["verified"] += 1
        elif v == "failed":
            fam["spoofed"] += 1
        else:
            fam["claimed"] += 1
    return out


def merge(total: dict, delta: dict) -> dict:
    """Fold a chunk delta into the ingest totals (pure; used by replay as well)."""
    for k in ("lines", "non_bot", "no_user_agent"):
        total[k] += delta[k]
    for r, n in delta["quarantined"].items():
        total["quarantined"][r] = total["quarantined"].get(r, 0) + n
    for tok, f in delta["families"].items():
        t = total["families"].setdefault(tok, {"requests": 0, "verified": 0, "spoofed": 0, "claimed": 0,
                                               "status": {}, "paths": {}, "ip_hashes": []})
        for k in ("requests", "verified", "spoofed", "claimed"):
            t[k] += f[k]
        for s, n in f["status"].items():
            t["status"][s] = t["status"].get(s, 0) + n
        for p, n in f["paths"].items():
            if p in t["paths"] or len(t["paths"]) < PATHS_PER_FAMILY:
                t["paths"][p] = t["paths"].get(p, 0) + n
        have = set(t["ip_hashes"])
        for h in f["ip_hashes"]:
            if h not in have and len(t["ip_hashes"]) < HASHES_PER_FAMILY:
                t["ip_hashes"].append(h)
                have.add(h)
    return total
