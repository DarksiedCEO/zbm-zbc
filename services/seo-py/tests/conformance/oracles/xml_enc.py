"""
XML encoding detection from XML 1.0 (Fifth Edition) Appendix F.1 (with a byte order mark / without one) and 4.3.3:
the first octets decide the family; the encoding declaration (read in that family) names the encoding.
Returns a Python codec name or None (undetectable).
"""

from __future__ import annotations

import codecs


def detect(data: bytes):
    b = data[:4]
    if b.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32-be"
    if b.startswith(b"\xff\xfe\x00\x00"):
        return "utf-32-le"
    if b.startswith(b"\xfe\xff"):
        return "utf-16-be"
    if b.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if b.startswith(b"\xef\xbb\xbf"):
        return "utf-8"
    if b == b"\x00\x00\x00\x3c":
        return "utf-32-be"
    if b == b"\x3c\x00\x00\x00":
        return "utf-32-le"
    if b == b"\x00\x3c\x00\x3f":
        return "utf-16-be"
    if b == b"\x3c\x00\x3f\x00":
        return "utf-16-le"
    if b == b"\x3c\x3f\x78\x6d":                        # '<?xm': ASCII-compatible; the declaration decides
        head = data[:200].decode("latin-1")
        k = head.find("encoding")
        if k >= 0 and head.find("?>") > k:
            rest = head[k + 8:].lstrip()
            if rest.startswith("="):
                rest = rest[1:].lstrip()
                if rest[:1] in ("'", '"'):
                    q = rest[0]
                    name = rest[1:].split(q, 1)[0]
                    try:
                        return codecs.lookup(name).name
                    except LookupError:
                        return None
        return "utf-8"
    return "utf-8"


def self_test() -> None:
    s = "<?xml version='1.0'?><a/>"
    for enc in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
        assert detect(s.encode(enc)) == enc, enc
        assert detect(codecs.BOM_UTF16_LE + s.encode("utf-16-le")) == "utf-16-le"
    assert detect(b"<?xml version='1.0' encoding='ISO-8859-1'?><a/>") == "iso8859-1"
    assert detect(b"\xef\xbb\xbf<a/>") == "utf-8"
