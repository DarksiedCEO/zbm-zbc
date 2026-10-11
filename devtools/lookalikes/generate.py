#!/usr/bin/env python3
"""Generate the SKELETON block of every service's ``src/lookalikes.py`` from the vendored Unicode confusables data.

Source: ``devtools/lookalikes/confusables-15.1.0.txt`` — Unicode Security Mechanisms (UTS #39) data file
``confusables.txt``, Version 15.1.0, dated 2023-08-11, copied unchanged from unicode-org/icu tag release-74-2
(``icu4c/source/data/unidata/confusables.txt``; the file Unicode publishes at
https://www.unicode.org/Public/security/15.1.0/confusables.txt). Its sha256 is pinned below; a different file is
refused. Terms of use: https://www.unicode.org/terms_of_use.html (the file's own header).

Rule: a line ``source ; prototype ; MA`` is kept when the source is ONE code point, outside ASCII, a letter (general
category L*), whose Unicode name does not start with "LATIN " (Latin-script letters are left to the services' named-
letter rule and hand tables, see lookalikes.py), and the prototype is ASCII letters only; the entry maps the source
to the prototype in lower case. Case is kept as the data has it: an upper-case lookalike (Greek capital eta "H")
is not a reason to fold its lower-case letter (eta looks like n, not h).

    python3 devtools/lookalikes/generate.py            # check every copy (exit 1 when one is stale)
    python3 devtools/lookalikes/generate.py --write    # rewrite the block in every copy

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SOURCE = HERE / "confusables-15.1.0.txt"
SOURCE_VERSION = "15.1.0"
SOURCE_SHA256 = "8289f833e4cf78fde56b2080dc0e42934ef5182c9c3f4dd1fbdf2bced69fd5ed"
SERVICES = ("clipper-network-py", "onboarding-py", "sales-py", "service-py", "verification-py")
BEGIN = "# BEGIN GENERATED SKELETON (devtools/lookalikes/generate.py; do not edit by hand)\n"
END = "# END GENERATED SKELETON\n"


def skeleton() -> dict[str, str]:
    raw = SOURCE.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SOURCE_SHA256:
        raise SystemExit(f"{SOURCE.name}: sha256 {digest} is not the pinned {SOURCE_SHA256}")
    out: dict[str, str] = {}
    for line in raw.decode("utf-8-sig").splitlines():
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        src, proto, _kind = (f.strip() for f in body.split(";"))
        cps = src.split()
        if len(cps) != 1:
            continue
        ch = chr(int(cps[0], 16))
        target = "".join(chr(int(x, 16)) for x in proto.split())
        if ord(ch) < 0x80 or not unicodedata.category(ch).startswith("L"):
            continue
        if unicodedata.name(ch, "").startswith("LATIN ") or not re.fullmatch(r"[A-Za-z]+", target):
            continue
        out[ch] = target.lower()
    return dict(sorted(out.items()))


def block() -> str:
    table = skeleton()
    lines = [BEGIN,
             f"# Unicode confusables.txt {SOURCE_VERSION} (UTS #39), sha256 {SOURCE_SHA256}: {len(table)} entries\n",
             f'SKELETON_SOURCE = "Unicode confusables.txt {SOURCE_VERSION}"\n',
             f'SKELETON_SOURCE_SHA256 = "{SOURCE_SHA256}"\n',
             "SKELETON: dict[str, str] = {\n"]
    row = "   "
    for ch, target in table.items():
        item = f' "\\u{ord(ch):04x}": "{target}",' if ord(ch) <= 0xFFFF else f' "\\U{ord(ch):08x}": "{target}",'
        if len(row) + len(item) > 118:
            lines.append(row + "\n")
            row = "   "
        row += item
    lines.append(row + "\n}\n")
    lines.append(END)
    return "".join(lines)


def render(text: str) -> str:
    a, b = text.index(BEGIN), text.index(END) + len(END)
    return text[:a] + block() + text[b:]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    stale = []
    for svc in SERVICES:
        p = REPO / "services" / svc / "src" / "lookalikes.py"
        cur = p.read_text(encoding="utf-8")
        new = render(cur)
        if new != cur:
            if a.write:
                p.write_text(new, encoding="utf-8")
                print(f"wrote {p.relative_to(REPO)}")
            else:
                stale.append(str(p.relative_to(REPO)))
    for s in stale:
        print(f"stale: {s} (run devtools/lookalikes/generate.py --write)")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
