"""
Installed-package licence gate (spec §C.7.2, D13, ST-01/02; test G12). For every distribution in a site-packages
directory: read ``License-Expression``, else ``License``, else the licence classifiers, else the bundled ``LICENSE*``
file's first line; normalise to an SPDX id; refuse anything outside ``seed/licence_allowlist.json``; an empty/UNKNOWN
id is accepted only for a distribution named in ``seed/licence_exceptions.json`` whose proof file's first line
matches; any of the forbidden distributions present at all is a refusal. Run at start (``gate.py``) and by
``devtools/licence_gate.py`` (which writes the JSON report under ``docs/evidence/``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from email.parser import HeaderParser
from typing import Optional

_NORMALISE = {
    "mit": "MIT", "mit license": "MIT", "the mit license": "MIT", "mit-0": "MIT-0",
    "apache": "Apache-2.0", "apache 2": "Apache-2.0", "apache 2.0": "Apache-2.0", "apache-2": "Apache-2.0",
    "apache-2.0": "Apache-2.0", "apache 2.0 license": "Apache-2.0", "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0", "apache software license": "Apache-2.0", "apache software license 2.0": "Apache-2.0",
    "apache license": "Apache-2.0", "asl 2.0": "Apache-2.0", "apache license, version 2.0.": "Apache-2.0",
    "bsd": "BSD-3-Clause", "bsd license": "BSD-3-Clause", "bsd-3-clause": "BSD-3-Clause", "new bsd": "BSD-3-Clause",
    "3-clause bsd": "BSD-3-Clause", "bsd 3-clause": "BSD-3-Clause", "bsd 3-clause license": "BSD-3-Clause", "modified bsd": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause", "2-clause bsd": "BSD-2-Clause", "simplified bsd": "BSD-2-Clause", "bsd 2-clause": "BSD-2-Clause",
    "3-clause bsd license": "BSD-3-Clause", "bsd-3": "BSD-3-Clause", "psfl": "PSF-2.0",
    "psf": "PSF-2.0", "psf-2.0": "PSF-2.0", "python software foundation license": "PSF-2.0", "psf license": "PSF-2.0",
    "python-2.0": "Python-2.0", "python software foundation": "PSF-2.0",
    "isc": "ISC", "isc license": "ISC", "isc license (iscl)": "ISC",
    "mpl-2.0": "MPL-2.0", "mpl 2.0": "MPL-2.0", "mozilla public license 2.0": "MPL-2.0", "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "0bsd": "0BSD", "unlicense": "Unlicense", "the unlicense": "Unlicense", "hpnd": "HPND",
    "historical permission notice and disclaimer (hpnd)": "HPND",
    "agpl-3.0": "AGPL-3.0", "agpl-3.0-only": "AGPL-3.0-only", "agpl-3.0-or-later": "AGPL-3.0-or-later",
    "gnu affero general public license v3": "AGPL-3.0", "gnu affero general public license v3 or later (agplv3+)": "AGPL-3.0-or-later",
    "gpl-3.0": "GPL-3.0", "gpl-3.0-only": "GPL-3.0-only", "gpl-3.0-or-later": "GPL-3.0-or-later", "gplv3": "GPL-3.0",
    "gnu general public license v3 (gplv3)": "GPL-3.0", "gnu general public license v3 or later (gplv3+)": "GPL-3.0-or-later",
    "gpl-2.0": "GPL-2.0", "lgpl-3.0": "LGPL-3.0", "lgpl-3.0-only": "LGPL-3.0-only", "lgpl-2.1": "LGPL-2.1",
    "gnu lesser general public license v3 (lgplv3)": "LGPL-3.0", "gnu library or lesser general public license (lgpl)": "LGPL-2.1",
    "elastic-2.0": "Elastic-2.0", "elastic license 2.0": "Elastic-2.0", "elastic license 2.0 (elv2)": "Elastic-2.0",
    "sspl-1.0": "SSPL-1.0", "busl-1.1": "BUSL-1.1",
}
_CLASSIFIER = re.compile(r"^License :: (?:OSI Approved :: )?(.+)$")
_FIRST_LINE = {
    "mit license": "MIT", "the mit license (mit)": "MIT", "apache license": "Apache-2.0", "bsd 3-clause license": "BSD-3-Clause",
    "bsd 2-clause license": "BSD-2-Clause", "isc license": "ISC", "mozilla public license version 2.0": "MPL-2.0",
    "copyright (c)": "UNKNOWN",
}


@dataclass
class Dist:
    name: str
    version: str
    licence: str            # SPDX id, "UNKNOWN" or "LicenseRef-..."
    source: str             # expression | license | classifier | file | none
    dist_info: str
    problem: Optional[str] = None


@dataclass
class Report:
    site_packages: str
    dists: list[Dist] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict:
        return {"site_packages": self.site_packages, "ok": self.ok, "problems": self.problems,
                "distributions": [{"name": d.name, "version": d.version, "licence": d.licence, "source": d.source,
                                   "problem": d.problem} for d in self.dists]}


def normalise(text: str) -> str:
    t = " ".join(text.strip().split())
    if not t:
        return ""
    low = t.lower().rstrip(".")
    if low in _NORMALISE:
        return _NORMALISE[low]
    if low.startswith("licenseref-"):
        return t
    # an SPDX expression with a single id
    if re.fullmatch(r"[A-Za-z0-9.+\-]+", t):
        for k, v in _NORMALISE.items():
            if k == low:
                return v
        return t
    # a long licence text pasted into License:: take the first line
    first = t.split(". ")[0].lower()
    for k, v in _NORMALISE.items():
        if first == k:
            return v
    return "UNKNOWN"


def _classifier_ids(meta) -> list[str]:
    out = []
    for c in meta.get_all("Classifier") or []:
        m = _CLASSIFIER.match(c)
        if m:
            out.append(normalise(m.group(1)))
    return [x for x in out if x]


def _first_line_licence(dist_info: str) -> tuple[str, str]:
    for sub in ("", "licenses"):
        d = os.path.join(dist_info, sub) if sub else dist_info
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.upper().startswith(("LICENSE", "LICENCE", "COPYING")):
                try:
                    with open(os.path.join(d, name), "r", encoding="utf-8", errors="replace") as fh:
                        for ln in fh:
                            if ln.strip():
                                first = " ".join(ln.strip().split()).lower()
                                for k, v in _FIRST_LINE.items():
                                    if first.startswith(k):
                                        return v, os.path.join(sub, name) if sub else name
                                return normalise(ln.strip()), os.path.join(sub, name) if sub else name
                except OSError:
                    continue
    return "", ""


def licence_of(dist_info: str) -> Dist:
    meta_path = os.path.join(dist_info, "METADATA")
    name = os.path.basename(dist_info).split("-")[0].replace("_", "-").lower()
    version = ""
    try:
        with open(meta_path, "r", encoding="utf-8", errors="replace") as fh:
            meta = HeaderParser().parse(fh)
    except OSError:
        return Dist(name, version, "UNKNOWN", "none", dist_info, "METADATA unreadable")
    name = (meta.get("Name") or name).replace("_", "-").lower()
    version = meta.get("Version") or ""
    expr = meta.get("License-Expression")
    if expr:
        return Dist(name, version, normalise(expr) if " " not in expr.strip() else expr.strip(), "expression", dist_info)
    lic = meta.get("License")
    if lic and lic.strip().upper() not in ("UNKNOWN", "", "LICENSE", "LICENSE.TXT", "LICENSE.MD", "SEE LICENSE"):
        ids = _classifier_ids(meta)
        n = normalise(lic)
        if n == "UNKNOWN":
            n = _field_list(lic)
        if n == "UNKNOWN" and ids:
            return Dist(name, version, ids[0], "classifier", dist_info)
        if n and n != "UNKNOWN":
            return Dist(name, version, n, "license", dist_info)
    ids = _classifier_ids(meta)
    ids = [i for i in ids if i != "UNKNOWN"]
    if ids:
        return Dist(name, version, ids[0], "classifier", dist_info)
    first, _ = _first_line_licence(dist_info)
    if first and first != "UNKNOWN":
        return Dist(name, version, first, "file", dist_info)
    return Dist(name, version, "UNKNOWN", "none", dist_info)


def _field_list(lic: str) -> str:
    """A License field holding several licences: ``A AND B`` / ``A OR B`` are kept as expressions; a comma list
    (``MIT License, Apache License, Version 2.0``) is dual licensing → ``A OR B``. Unknown parts are dropped when at
    least one part is known; nothing known → UNKNOWN."""
    t = " ".join(lic.split())
    if re.search(r"\s(AND|and)\s", t):
        ids = [normalise(x) for x in re.split(r"\s(?:AND|and)\s", t)]
        return " AND ".join(ids) if all(i and i != "UNKNOWN" for i in ids) else "UNKNOWN"
    if re.search(r"\s(OR|or)\s", t):
        ids = [normalise(x) for x in re.split(r"\s(?:OR|or)\s", t)]
        known = [i for i in ids if i and i != "UNKNOWN"]
        return " OR ".join(known) if known else "UNKNOWN"
    parts = [x.strip() for x in t.split(",") if x.strip()]
    # greedy merge: "Apache License, Version 2.0" is one licence
    merged: list[str] = []
    i = 0
    while i < len(parts):
        j = len(parts)
        found = None
        while j > i:
            cand = normalise(", ".join(parts[i:j]))
            if cand and cand != "UNKNOWN":
                found = (cand, j)
                break
            j -= 1
        if found:
            merged.append(found[0])
            i = found[1]
        else:
            i += 1
    return " OR ".join(dict.fromkeys(merged)) if merged else "UNKNOWN"


def _expression_ids(expr: str) -> list[str]:
    return [normalise(tok) for tok in re.split(r"\s+(?:AND|OR|and|or)\s+|[()]", expr) if tok.strip()]


def _find_dist(site_packages: str, name: str) -> Optional[Dist]:
    want = name.replace("_", "-").lower()
    for entry in os.listdir(site_packages):
        if entry.endswith(".dist-info") and entry.split("-")[0].replace("_", "-").lower() == want:
            return licence_of(os.path.join(site_packages, entry))
    return None


def check(site_packages: str, allowlist: dict, exceptions: dict) -> Report:
    rep = Report(site_packages=site_packages)
    allowed = set(allowlist["allowed"])
    forbidden = {n.lower() for n in allowlist.get("forbidden_distributions", [])}
    exc = {k.lower(): v for k, v in (exceptions.get("exceptions") or {}).items()}
    if not os.path.isdir(site_packages):
        rep.problems.append(f"site-packages not found: {site_packages}")
        return rep
    for entry in sorted(os.listdir(site_packages)):
        if not entry.endswith(".dist-info"):
            continue
        d = licence_of(os.path.join(site_packages, entry))
        if d.name in forbidden:
            d.problem = "forbidden distribution present (spec 0.3: dropped by the overlay)"
        else:
            ids = _expression_ids(d.licence) if (" AND " in d.licence or " OR " in d.licence) else [d.licence]
            if d.name in exc and (d.licence in ("", "UNKNOWN") or d.source in ("file", "none", "license")):
                e = exc[d.name]
                kind = e.get("kind", "file")
                ok = False
                if kind == "file":
                    first, proof = _first_line_licence(d.dist_info)
                    ok = bool(proof) and os.path.normpath(proof) == os.path.normpath(e["proof"]) and first == e["licence"]
                elif kind == "sibling":
                    sib = _find_dist(site_packages, e["sibling"])
                    ok = sib is not None and sib.source == "expression" and sib.licence == e["licence"]
                elif kind == "stated":
                    ok = bool(e.get("reason")) and e["licence"] in allowed
                elif kind == "license_field":
                    ok = e["licence"] in _expression_ids(d.licence) if d.licence not in ("", "UNKNOWN") else False
                if ok and e["licence"] in allowed:
                    d.licence, d.source = e["licence"], f"exception:{kind}"
                    ids = [d.licence]
                else:
                    d.problem = "exception entry does not match the installed proof"
            if d.problem is None:
                if any(i in ("", "UNKNOWN") for i in ids):
                    d.problem = "licence unknown (no expression, License field, classifier or licence file)"
                elif " OR " in d.licence or " or " in d.licence:
                    if not any(i in allowed for i in ids):
                        d.problem = f"no alternative of {d.licence!r} is on the allowlist"
                else:
                    bad = [i for i in ids if i not in allowed and not (i.startswith("LicenseRef-") and d.name in exc)]
                    if bad:
                        d.problem = f"licence {', '.join(bad)} is not on the allowlist"
        rep.dists.append(d)
        if d.problem:
            rep.problems.append(f"{d.name} {d.version}: {d.problem}")
    return rep
