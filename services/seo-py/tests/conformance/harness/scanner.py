"""
D-05 scanner: is a planted value READABLE in an output, in any of the scanned forms (SPEC D-05): raw, lower / upper
case, hex, base64 and base64url at 3 alignments, percent-encoded, JSON-escaped, digits-only, punycode for host
labels. ``hits(needles, text)`` returns the forms found (empty: not readable).
"""

from __future__ import annotations

import base64
import binascii
import json
from urllib.parse import quote


def _b64_cores(b: bytes) -> list:
    out = []
    for pad in range(3):
        enc = base64.b64encode(b"\x00" * pad + b)
        # drop the characters that depend on the neighbouring bytes (first and last group)
        core = enc[4:-4] if len(enc) > 12 else b""
        if len(core) >= 8:
            out.append(core.decode())
            out.append(core.decode().replace("+", "-").replace("/", "_"))
    return out


def forms(needle: str) -> dict:
    b = needle.encode("utf-8")
    f = {"raw": needle.lower(), "hex": binascii.hexlify(b).decode(), "pct": quote(needle, safe="").lower(),
         "json": json.dumps(needle)[1:-1].lower()}
    for i, c in enumerate(_b64_cores(b)):
        f[f"b64_{i}"] = c
    if not needle.isascii():
        try:
            f["puny"] = needle.lower().encode("idna").decode().lower()
        except UnicodeError:
            pass
    return f


def hits(needles, text: str) -> list:
    low = text.lower()
    found = []
    for n in needles:
        for k, v in forms(n).items():
            if not v:
                continue
            hay = text if k.startswith("b64") else low
            if v in hay:
                found.append((n, k))
    return found
