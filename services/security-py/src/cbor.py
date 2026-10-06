"""
A strict, bounded CBOR (RFC 8949) decoder for the two structures WebAuthn hands us: the attestation object and
the COSE public key. Standard library only (ADR 0012 decision 13: no extra dependency for ~100 lines).

Supported: unsigned and negative integers, byte strings, text strings, arrays, maps, false/true/null — definite
lengths only. Refused: indefinite lengths, tags, floats, simple values other than false/true/null, non-minimal
lengths, duplicate map keys, nesting deeper than MAX_DEPTH, more than MAX_ITEMS items, and any input over
MAX_BYTES. Trailing bytes are refused by ``loads`` and returned by ``loads_prefix`` (authenticator data carries a
COSE key followed by optional extensions).
"""

from __future__ import annotations

MAX_BYTES = 16 * 1024
MAX_DEPTH = 8
MAX_ITEMS = 512


class CBORError(ValueError):
    pass


_SIMPLE = {20: False, 21: True, 22: None}


class _Reader:
    def __init__(self, data: bytes):
        if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_BYTES:
            raise CBORError("CBOR input missing or too large")
        self.data = bytes(data)
        self.pos = 0
        self.items = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise CBORError("CBOR input truncated")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def arg(self, info: int) -> int:
        if info < 24:
            return info
        sizes = {24: 1, 25: 2, 26: 4, 27: 8}
        if info not in sizes:
            raise CBORError("CBOR indefinite or reserved length refused")
        value = int.from_bytes(self.take(sizes[info]), "big")
        minimum = {24: 24, 25: 0x100, 26: 0x10000, 27: 0x100000000}[info]
        if value < minimum:
            raise CBORError("CBOR non-minimal length refused")
        return value

    def item(self, depth: int):
        if depth > MAX_DEPTH:
            raise CBORError("CBOR nested too deeply")
        self.items += 1
        if self.items > MAX_ITEMS:
            raise CBORError("CBOR has too many items")
        head = self.take(1)[0]
        major, info = head >> 5, head & 0x1F
        if major == 7:
            if info in _SIMPLE:
                return _SIMPLE[info]
            raise CBORError("CBOR float or simple value refused")
        n = self.arg(info)
        if major == 0:
            return n
        if major == 1:
            return -1 - n
        if major == 2:
            return self.take(n)
        if major == 3:
            try:
                return self.take(n).decode("utf-8")
            except UnicodeDecodeError:
                raise CBORError("CBOR text is not UTF-8") from None
        if major == 4:
            if n > MAX_ITEMS:
                raise CBORError("CBOR array too long")
            return [self.item(depth + 1) for _ in range(n)]
        if major == 5:
            if n > MAX_ITEMS:
                raise CBORError("CBOR map too long")
            out = {}
            for _ in range(n):
                key = self.item(depth + 1)
                if not isinstance(key, (int, str)):
                    raise CBORError("CBOR map key must be an integer or text")
                if isinstance(key, bool) or key in out:
                    raise CBORError("CBOR duplicate or boolean map key refused")
                out[key] = self.item(depth + 1)
            return out
        raise CBORError("CBOR tags are refused")


def loads_prefix(data: bytes):
    """Decode one item from the start of ``data``; returns (item, bytes consumed)."""
    r = _Reader(data)
    value = r.item(0)
    return value, r.pos


def loads(data: bytes):
    value, used = loads_prefix(data)
    if used != len(data):
        raise CBORError("CBOR has trailing bytes")
    return value
