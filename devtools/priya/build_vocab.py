"""Build the Priya vocabulary artifact V (priya-spec/2, section 2.3 V-1..V-3; cards CAL-1, CAL-1b).

Source: the wordfreq 3.1.1 wheel (data only, read straight from the zip; wordfreq is never imported).
Lists:  --wordlist small (default, CAL-1b): the `small` list of every language the wheel ships one for.
        --wordlist large (CAL-6, owner decision after CAL-5): the `large` list for every language that has one,
        the `small` list for the rest. No zipf cut; nothing removed.
        --min-zipf T (CAL-6e, needs --wordlist large): every `small` list in full, plus the words of each `large`
        list whose zipf frequency is >= T (cBpack band i has zipf 9 - i/100, so the cut is band <= 100 * (9 - T)).
Output: canonical form, sorted by code point, unique, one word per line, UTF-8, LF, gzip with mtime 0 and
        no file name.
Canonical: the shared gate function agents.priya_gate.canonical (CAN-1..CAN-6 with rulings CL-1 accent fold
        and CL-2 confusables / Highly Restrictive), imported from services/seo-py/src - no private copy (CL-3).
Pin:    the sha256 of the UNCOMPRESSED text is the pinned VOCAB_SHA256 (CL-4); the gzip hash is information only.
Licence: the word lists are wordfreq data, CC BY-SA 4.0 (owner decision 2026-11-14 10:30 PT); V ships under the
        same licence, see services/seo-py/THIRD_PARTY_NOTICES.md and priya_vocab.txt.gz.LICENSE beside V.

Usage:  python devtools/priya/build_vocab.py [--wordlist small|large] [--min-zipf T] WHEEL OUT.gz
Needs:  msgpack (to decode wordfreq's cBpack files).
"""

import decimal
import gzip
import hashlib
import io
import os
import re
import sys
import unicodedata
import zipfile
import zlib

import msgpack

_SRC = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "services", "seo-py", "src"
)
sys.path.insert(0, os.path.normpath(_SRC))

from agents import priya_gate  # noqa: E402

WHEEL_SHA256 = "4b1c6ecffc6198be3396d5cf871c4423ca71c907c231348d352dd54d62b97473"
LIST_RE = re.compile(r"^wordfreq/data/(small|large)_([A-Za-z_-]+)\.msgpack\.gz$")
WORDLISTS = ("small", "large")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def pick_lists(names, wordlist):
    """{lang: member name}: `small` everywhere, or `large` where the wheel has one (else `small`)."""
    found = {}
    for name in names:
        m = LIST_RE.match(name)
        if m:
            found[(m.group(1), m.group(2))] = name
    picked = {}
    for kind, lang in sorted(found):
        if kind == "small":
            picked[lang] = found.get((wordlist, lang), found[(kind, lang)])
    return picked


def read_bands(z, name):
    raw = gzip.decompress(z.read(name))
    data = msgpack.unpackb(raw, raw=False, strict_map_key=False)
    header = data[0]
    if header != {"format": "cB", "version": 1}:
        raise SystemExit("unexpected cBpack header in " + name)
    return data[1:]


def read_lists(wheel, wordlist, max_band=None):
    """{lang: (member, words)}; with max_band, a `large` list keeps bands <= max_band plus its whole `small` list."""
    langs = {}
    with zipfile.ZipFile(wheel) as z:
        for lang, name in sorted(pick_lists(z.namelist(), wordlist).items()):
            bands = read_bands(z, name)
            if max_band is not None and "/large_" in name:
                small = read_bands(z, "wordfreq/data/small_" + lang + ".msgpack.gz")
                words = [w for band in small for w in band]
                words += [w for band in bands[: max_band + 1] for w in band]
            else:
                words = [w for band in bands for w in band]
            langs[lang] = (name, words)
    return langs


def main(argv):
    args = argv[1:]
    wordlist, min_zipf, max_band = "small", None, None
    usage = "usage: build_vocab.py [--wordlist small|large] [--min-zipf T] WHEEL OUT.gz"
    if args[:1] == ["--wordlist"]:
        wordlist, args = args[1], args[2:]
    if args[:1] == ["--min-zipf"] and len(args) > 1:
        try:
            min_zipf = decimal.Decimal(args[1])
        except decimal.InvalidOperation:
            raise SystemExit(usage) from None
        args = args[2:]
        if wordlist != "large" or not decimal.Decimal(0) <= min_zipf <= 9:
            raise SystemExit(usage)
        max_band = int((100 * (9 - min_zipf)).to_integral_value(decimal.ROUND_FLOOR))
    if wordlist not in WORDLISTS or len(args) != 2:
        raise SystemExit(usage)
    wheel, out_path = args
    got = sha256_file(wheel)
    if got != WHEEL_SHA256:
        raise SystemExit("wheel sha256 mismatch: " + got)
    langs = read_lists(wheel, wordlist, max_band)
    raw_forms = sum(len(v[1]) for v in langs.values())
    vocab = set()
    for _name, words in langs.values():
        for w in words:
            c = priya_gate.canonical(w)
            if c:
                vocab.add(c)
    ordered = sorted(vocab)
    text = ("\n".join(ordered) + "\n").encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9
    ) as g:
        g.write(text)
    with open(out_path, "wb") as f:
        f.write(buf.getvalue())
    mixed = sum(
        1 for w in ordered if any(priya_gate.mixed_script(t) for t in w.split())
    )
    print("wheel_sha256", got)
    print("gate_module", priya_gate.__file__, sha256_file(priya_gate.__file__))
    print("confusables_sha256", priya_gate.CONFUSABLES_SHA256)
    print("wordlist", wordlist)
    if min_zipf is not None:
        print("min_zipf", min_zipf, "max_band", max_band)
    print("languages", len(langs), " ".join(sorted(langs)))
    print(
        "large_lists",
        " ".join(k for k, v in sorted(langs.items()) if "/large_" in v[0]) or "-",
    )
    print("raw_forms", raw_forms)
    print("VOCAB_N", len(ordered))
    print("text_sha256 (pin, CL-4)", hashlib.sha256(text).hexdigest())
    print("gzip_sha256 (info)", sha256_file(out_path))
    print("gzip_bytes", len(buf.getvalue()), "text_bytes", len(text))
    print("with_whitespace", sum(1 for w in ordered if any(ch.isspace() for ch in w)))
    print("mixed_script_after_canonical", mixed)
    print(
        "unicodedata",
        unicodedata.unidata_version,
        "python",
        sys.version.split()[0],
        "zlib",
        zlib.ZLIB_VERSION,
    )


if __name__ == "__main__":
    main(sys.argv)
