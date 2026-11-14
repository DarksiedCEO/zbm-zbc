"""
Build src/primitives/cldr_validity.txt from the CLDR validity data (common/validity/{language,script,region}.xml of
Unicode CLDR, Unicode License v3 / SPDX Unicode-3.0). Usage: python cldr_extract.py <dir with the three XML files>
<CLDR release name> <commit>. The output lists, per kind, every code whose idStatus is one of ACCEPTED (ranges such
as ``Brah~i`` expanded); ``reserved`` codes are left out (they are not assigned).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ACCEPTED = ("regular", "special", "macrolanguage", "macroregion", "deprecated", "private_use", "unknown")
ID = re.compile(r"<id type='(\w+)' idStatus='(\w+)'>([^<]*)</id>")


def expand(item: str) -> list:
    if "~" not in item:
        return [item]
    a, b = item.split("~")
    stem, cur = a[:len(a) - len(b)], a[len(a) - len(b):]
    out = []
    while True:
        out.append(stem + cur)
        if cur == b:
            return out
        cur = cur[:-1] + chr(ord(cur[-1]) + 1)


def main(src: str, release: str, commit: str) -> None:
    out = ["# CLDR validity data (language, script, region subtags), derived by devtools/conformance/cldr_extract.py",
           f"# Source: Unicode CLDR {release} (github.com/unicode-org/cldr, commit {commit}),",
           "# common/validity/{language,script,region}.xml. License: Unicode-3.0 (Unicode License v3);",
           "# see THIRD_PARTY_NOTICES.md. Statuses kept: " + " ".join(ACCEPTED) + ". One kind per line: kind code ..."]
    for kind in ("language", "script", "region"):
        text = re.sub(r"<!--.*?-->", "", (Path(src) / f"{kind}.xml").read_text(encoding="utf-8"), flags=re.S)
        got = set()
        for typ, status, body in ID.findall(text):
            if typ == kind and status in ACCEPTED:
                for it in body.split():
                    got.update(x.lower() for x in expand(it))
        out.append(kind + " " + " ".join(sorted(got)))
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main(*sys.argv[1:4])
