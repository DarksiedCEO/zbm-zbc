"""
Installed-package licence gate (spec §C.7.2, D13, ST-01/02; test G12; round 18 R9). For every distribution in a
site-packages directory (``*.dist-info`` AND ``*.egg-info``, directory or file): read ``License-Expression``, else
``License``, else the licence classifiers; normalise to an SPDX id; refuse anything outside
``seed/licence_allowlist.json``. A distribution whose METADATA yields no usable id (UNKNOWN / a pasted licence text)
passes ONLY through an explicit entry in ``seed/licence_exceptions.json`` naming the proof file and its first line
(the bundled ``LICENSE*`` file is never trusted on its own). The METADATA ``Name`` must match the distribution
directory's name (a spoofed name is a problem, and the forbidden-distribution check keys on both). Every importable
top-level entry of site-packages must be covered by some distribution's ``RECORD`` (a vendored package with no
metadata is a problem) unless the allowlist names it under ``unrecorded_allow``. Run at start (``gate.py``) and by
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


def dir_name(entry: str) -> str:
    """The distribution name a ``*.dist-info`` / ``*.egg-info`` entry carries in its own name (normalised)."""
    base = os.path.basename(entry)
    for suffix in (".dist-info", ".egg-info"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return _pep503(base.split("-")[0])


def _pep503(name: str) -> str:
    """PEP 503 normalisation: runs of ``-``, ``_`` and ``.`` become one ``-``; lower-case."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _metadata_path(dist_info: str) -> Optional[str]:
    if os.path.isfile(dist_info):                         # a single-file egg-info IS the metadata
        return dist_info
    for name in ("METADATA", "PKG-INFO"):
        p = os.path.join(dist_info, name)
        if os.path.isfile(p):
            return p
    return None


def licence_of(dist_info: str) -> Dist:
    meta_path = _metadata_path(dist_info)
    name = dir_name(dist_info)
    version = ""
    try:
        if meta_path is None:
            raise OSError("no metadata")
        with open(meta_path, "r", encoding="utf-8", errors="replace") as fh:
            meta = HeaderParser().parse(fh)
    except OSError:
        return Dist(name, version, "UNKNOWN", "none", dist_info, "METADATA unreadable")
    stated = _pep503(meta.get("Name") or "")
    if stated and stated != name:
        return Dist(name, meta.get("Version") or "", "UNKNOWN", "none", dist_info,
                    f"METADATA Name {stated!r} does not match the distribution directory {name!r}")
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


_DIST_SUFFIXES = (".dist-info", ".egg-info")


def _find_dist(site_packages: str, name: str) -> Optional[Dist]:
    want = _pep503(name)
    for entry in os.listdir(site_packages):
        if entry.endswith(_DIST_SUFFIXES) and dir_name(entry) == want:
            return licence_of(os.path.join(site_packages, entry))
    return None


def _recorded_top_levels(site_packages: str) -> set[str]:
    """Top-level names every distribution's RECORD (or SOURCES.txt / top_level.txt for egg-info) accounts for."""
    out: set[str] = set()
    for entry in os.listdir(site_packages):
        if not entry.endswith(_DIST_SUFFIXES):
            continue
        d = os.path.join(site_packages, entry)
        if not os.path.isdir(d):
            continue
        for fname in ("RECORD", "SOURCES.txt", "top_level.txt", "installed-files.txt"):
            p = os.path.join(d, fname)
            if not os.path.isfile(p):
                continue
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    for ln in fh:
                        path = ln.split(",")[0].strip()
                        if not path or path.startswith(".."):
                            continue
                        out.add(path.split("/")[0])
            except OSError:
                continue
    return out


def _stated_first_line(dist_info: str, proof: str) -> Optional[str]:
    p = os.path.join(dist_info, proof)
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if ln.strip():
                    return " ".join(ln.strip().split())
    except OSError:
        return None
    return None


def check(site_packages: str, allowlist: dict, exceptions: dict) -> Report:
    rep = Report(site_packages=site_packages)
    allowed = set(allowlist["allowed"])
    forbidden = {n.lower() for n in allowlist.get("forbidden_distributions", [])}
    unrecorded_allow = set(allowlist.get("unrecorded_allow") or [])
    exc = {k.lower(): v for k, v in (exceptions.get("exceptions") or {}).items()}
    if not os.path.isdir(site_packages):
        rep.problems.append(f"site-packages not found: {site_packages}")
        return rep
    for entry in sorted(os.listdir(site_packages)):
        if not entry.endswith(_DIST_SUFFIXES):
            continue
        d = licence_of(os.path.join(site_packages, entry))
        if d.name in forbidden or dir_name(entry) in forbidden:
            d.problem = "forbidden distribution present (spec 0.3: dropped by the overlay)"
        elif d.problem is None:
            ids = _expression_ids(d.licence) if (" AND " in d.licence or " OR " in d.licence) else [d.licence]
            if d.name in exc and (d.licence in ("", "UNKNOWN") or d.source in ("file", "none", "license")):
                e = exc[d.name]
                kind = e.get("kind", "file")
                ok = False
                if kind == "file":
                    first, proof = _first_line_licence(d.dist_info)
                    stated = _stated_first_line(d.dist_info, e.get("proof", "")) or ""
                    ok = (bool(proof) and os.path.normpath(proof) == os.path.normpath(e["proof"]) and first == e["licence"]
                          and bool(e.get("first_line")) and stated.lower().startswith(e["first_line"].lower()))
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
                if d.source == "file":
                    d.problem = ("licence unknown in metadata (the bundled licence file reads %s): needs an explicit "
                                 "exception naming the proof file and its first line" % d.licence)
                elif any(i in ("", "UNKNOWN") for i in ids):
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
    recorded = _recorded_top_levels(site_packages)
    for entry in sorted(os.listdir(site_packages)):
        if entry.endswith(_DIST_SUFFIXES) or entry == "__pycache__" or entry in unrecorded_allow or entry in recorded:
            continue
        full = os.path.join(site_packages, entry)
        importable = os.path.isdir(full) or entry.endswith((".py", ".pth", ".so", ".pyd", ".egg", ".zip"))
        if importable:
            rep.problems.append(f"{entry}: importable top-level entry with no distribution record (vendored package "
                                "without metadata; spec C.7.2)")
    return rep
