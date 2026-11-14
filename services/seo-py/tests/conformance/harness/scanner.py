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


_B64CH = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/-_=")


def _b64_decodes_to(text: str, at: int, core: str, needle: bytes) -> bool:
    """rv-wave1v2r2 #4: a base64 form found at ``at`` is a READABLE plant only when the base64 token around it
    decodes (at one of its four alignments, standard or url-safe alphabet) to octets that hold the WHOLE needle.
    A core is the middle of the needle's base64 (8 characters = 6 octets for the shortest needles), so a random
    token (ciphertext, a nonce, an id) can hold one by coincidence; decoding tells the two apart, and every real
    base64 copy of the value still decodes to it."""
    i, j = at, at + len(core)
    while i > 0 and text[i - 1] in _B64CH:
        i -= 1
    while j < len(text) and text[j] in _B64CH:
        j += 1
    tok = text[i:j].rstrip("=").replace("-", "+").replace("_", "/")
    for off in range(4):
        part = tok[off:]
        part = part[:len(part) // 4 * 4]
        try:
            if needle in base64.b64decode(part, validate=False):
                return True
        except (binascii.Error, ValueError):
            continue
    return False


def hits(needles, text: str) -> list:
    low = text.lower()
    found = []
    for n in needles:
        for k, v in forms(n).items():
            if not v:
                continue
            if k.startswith("b64"):
                at = text.find(v)
                while at >= 0:
                    if _b64_decodes_to(text, at, v, n.encode("utf-8")):
                        found.append((n, k))
                        break
                    at = text.find(v, at + 1)
            elif v in low:
                found.append((n, k))
    return found


def self_test() -> None:
    """A base64 copy of a value is found in every alignment and alphabet; a random token holding the same core is
    not; raw, hex and percent forms are found as before."""
    v = "kobalitesu-mavoreni"
    for pad in ("", "a", "ab"):
        enc = base64.b64encode((pad + "<x>" + v + "</x>").encode()).decode()
        assert hits([v], "id=" + enc + ";"), pad
        assert hits([v], "id=" + enc.replace("+", "-").replace("/", "_") + ";"), pad
    core = _b64_cores(v.encode())[0]
    assert not hits([v], "AAAAQUJD" + core + "QUJDREVG")         # the core alone, inside a random-looking token
    assert hits([v], "x " + v.upper() + " y") and hits([v], binascii.hexlify(v.encode()).decode())
