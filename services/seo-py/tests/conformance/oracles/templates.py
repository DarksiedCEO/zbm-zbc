"""
O-TPL: the stored form of a URL path (SPEC 0.1 and B-CD-04, D-01 W), written from the spec text. A coded segment is
written ``{c:seg}`` here (the code value needs the service's key); a test compares after replacing every
``{c:seg:<hex>}`` of the service's answer with ``{c:seg}``.

Per segment, after one percent-decode, invisible characters removed and NFKC (the full-width at-sign folded):
  empty -> empty; a D-01 W well-known name -> as written; e-mail shape -> {email}; UUID -> {uuid}; 7+ digits in
  total -> {number}; only digits -> {id}; ``,`` or ``=`` -> {param}; ``;`` -> (head judged alone) + ``;{param}``;
  a file name with an extension of the closed list -> {file}.<ext>; token shapes -> {token}; else -> {c:seg}.
  Neighbouring all-digit segments with 7+ digits together -> {number} each.
"""

from __future__ import annotations

import unicodedata
from urllib.parse import unquote

WELL_KNOWN = frozenset({"robots.txt", "llms.txt", "llms-full.txt", "favicon.ico", "ads.txt", "app-ads.txt",
                        "security.txt", "humans.txt", "manifest.json", "site.webmanifest", "browserconfig.xml",
                        "apple-touch-icon.png", "rss.xml", "atom.xml", "feed", "index.html", "index.php",
                        ".well-known", "sitemap.xml", "sitemap_index.xml", "sitemap-index.xml", "wp-sitemap.xml"})
S_WORDS = ("index pages page posts post products product categories category tags tag images image video videos news "
           "blog misc main authors author taxonomies users").split()
FILE_EXT = frozenset({"css", "js", "mjs", "map", "svg", "ico", "woff", "woff2", "ttf", "eot", "otf", "xml", "txt",
                      "html", "htm", "php", "asp", "aspx", "jsp", "json", "webmanifest", "xhtml", "rss", "atom",
                      "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf", "csv",
                      "png", "jpg", "jpeg", "gif", "webp", "avif", "bmp", "tif", "tiff", "mp3", "mp4", "webm",
                      "mov", "avi", "wav", "ogg", "zip", "gz", "tar", "epub"})
HEXD = set("0123456789abcdefABCDEF")
TOKEN_CH = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-=+.~")


def _n(s: str) -> bool:
    return s == "" or (1 <= len(s) <= 3 and s.isascii() and s.isdigit())


def _sitemap_name(low: str) -> bool:
    base = low[:-3] if low.endswith(".gz") else low
    if base in ("sitemap.xml", "sitemap_index.xml", "sitemap-index.xml", "wp-sitemap.xml"):
        return True
    if not base.endswith(".xml"):
        return False
    stem = base[:-4]
    # sitemap[-_]<S>[-_]<n>   (the trailing [-_]<n> optional when <n> is empty)
    if stem.startswith("sitemap") and len(stem) > 8 and stem[7] in "-_":
        rest = stem[8:]
        if rest and _n(rest):                       # SPEC R-04: sitemap-12.xml (a 1-3 digit number) is readable
            return True
        for w in S_WORDS:
            if rest == w:
                return True
            if rest.startswith(w) and len(rest) > len(w) and rest[len(w)] in "-_" and _n(rest[len(w) + 1:]) \
                    and rest[len(w) + 1:]:
                return True
    # <S>-sitemap<n>
    for w in S_WORDS:
        if stem.startswith(w + "-sitemap") and _n(stem[len(w) + 8:]):
            return True
    # wp-sitemap-<S>-<S>-<n>
    if stem.startswith("wp-sitemap-"):
        parts = stem[11:].split("-")
        if len(parts) in (2, 3) and parts[0] in S_WORDS and parts[1] in S_WORDS and \
                (len(parts) == 2 or (_n(parts[2]) and parts[2])):
            return True
    return False


def well_known(seg: str) -> bool:
    if not seg.isascii():
        return False
    low = seg.lower()
    return low in WELL_KNOWN or _sitemap_name(low)


def _words(low: str) -> list:
    out, cur = [], ""
    for c in low:
        if c.isalnum() or c == "_":
            cur += c
        else:
            if cur:
                out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def _email(s: str) -> bool:
    low = s.lower()
    if "@" in s or "%40" in low or "&commat;" in low:
        return True
    k = low.find("&#")
    while k >= 0:
        j = k + 2
        while j < len(low) and low[j] == "0":
            j += 1
        if low[j:j + 3] == "64;":
            return True
        k = low.find("&#", k + 1)
    for i, c in enumerate(low):
        if c in "([{<":
            j = i + 1
            while j < len(low) and low[j].isspace():
                j += 1
            if low[j:j + 2] == "at":
                j += 2
                while j < len(low) and low[j].isspace():
                    j += 1
                if j < len(low) and low[j] in ")]}>":
                    return True
    for i in range(1, len(low) - 3):
        if low[i - 1].isspace() and low[i:i + 2] == "at" and low[i + 2].isspace():
            return True
    ws = _words(low)
    return "at" in ws and "dot" in ws[ws.index("at") + 1:]


def _uuid(s: str) -> bool:
    i = 0
    for k, n in enumerate((8, 4, 4, 4, 12)):
        if k and i < len(s) and s[i] == "-":
            i += 1
        part = s[i:i + n]
        if len(part) != n or any(c not in HEXD for c in part):
            return False
        i += n
    return i == len(s)


def _digits(s: str) -> int:
    return sum(1 for c in s if c.isdigit())


def _token(s: str) -> bool:
    if len(s) >= 16 and all(c in HEXD for c in s):
        return True
    if len(s) > 40:
        return True
    if len(s) >= 20 and all(c in TOKEN_CH for c in s):
        return True
    letters = any(c.isalpha() for c in s)
    if len(s) >= 8 and letters and _digits(s) >= 2:
        return True
    stem = s.rsplit(".", 1)[0] if "." in s and s.rsplit(".", 1)[1].lower() in FILE_EXT else s
    return len(stem) >= 10 and letters and _digits(stem) >= 1


def segment(seg: str) -> str:
    if seg == "":
        return ""
    if well_known(seg):
        return seg
    if _email(seg):
        return "{email}"
    if ";" in seg:
        head = seg.split(";", 1)[0]
        return (segment(head) if head else "") + ";{param}"
    if _uuid(seg):
        return "{uuid}"
    if _digits(seg) >= 7:
        return "{number}"
    if seg.isdigit():
        return "{id}"
    if "," in seg or "=" in seg:
        return "{param}"
    if "." in seg:
        stem, ext = seg.rsplit(".", 1)
        if stem and ext.lower() in FILE_EXT:
            return "{file}." + ext.lower()
    if _token(seg):
        return "{token}"
    return "{c:seg}"


INVISIBLE = {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD}


def template(path: str):
    if not path.startswith("/"):
        return None
    p = unquote(path.split("?", 1)[0].split("#", 1)[0])
    p = "".join(c for c in p if ord(c) not in INVISIBLE)
    p = unicodedata.normalize("NFKC", p).replace("\ufe6b", "@")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in p):
        return None
    segs = p.split("/")[1:]
    out = [segment(s) for s in segs]
    i = 0
    while i < len(segs):
        j = i
        while j < len(segs) and segs[j].isdigit() and out[j] == "{id}":
            j += 1
        if j - i >= 2 and sum(_digits(segs[k]) for k in range(i, j)) >= 7:
            for k in range(i, j):
                out[k] = "{number}"
        i = max(j, i + 1)
    return "/" + "/".join(out)


def self_test() -> None:
    assert template("/") == "/" and template("/robots.txt") == "/robots.txt"
    assert template("/post-sitemap.xml") == "/post-sitemap.xml" and template("/sitemap-12.xml") == "/sitemap-12.xml"
    assert template("/wp-sitemap-posts-post-1.xml") == "/wp-sitemap-posts-post-1.xml"
    assert template("/sitemap-blog.xml") == "/sitemap-blog.xml" and template("/sitemap-zz.xml") == "/{file}.xml"
    assert template("/blog/how-to-choose") == "/{c:seg}/{c:seg}"
    assert template("/u/123/45/6789") == "/{c:seg}/{number}/{number}/{number}"
    assert template("/a;b=1") == "/{c:seg};{param}" and template("/report.pdf") == "/{file}.pdf"
