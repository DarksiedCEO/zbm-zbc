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

Round 19 R13 (N19-A-9): a ``.pth`` path line naming a directory outside the virtual environment is a problem, one
inside it is scanned as a further site directory; a top-level entry is covered by a distribution only when that
distribution's RECORD lists files under it WITH hashes that verify on disk (a bare line "covers" nothing); a
METADATA with two ``License-Expression`` / ``License`` fields is a problem; a metadata licence that contradicts every
bundled LICENSE file whose heading is recognised is a problem (the classifier-vs-file residual of wave 19 is closed).
"""

from __future__ import annotations

import base64
import hashlib
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
    "mozilla public license, version 2.0": "MPL-2.0",
    "gnu affero general public license": "AGPL-3.0", "gnu general public license": "GPL-3.0",
    "gnu lesser general public license": "LGPL-3.0", "gnu library general public license": "LGPL-2.0",
    "eclipse public license": "EPL-2.0", "the unlicense": "Unlicense", "server side public license": "SSPL-1.0",
    "business source license": "BUSL-1.1", "elastic license": "Elastic-2.0",
    "copyright (c)": "UNKNOWN",
}
_GPL_FAMILY = {"AGPL-3.0": ("AGPL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later"),
               "GPL-3.0": ("GPL-3.0", "GPL-3.0-only", "GPL-3.0-or-later", "GPL-2.0", "GPL-2.0-only", "GPL-2.0-or-later"),
               "LGPL-3.0": ("LGPL-3.0", "LGPL-3.0-only", "LGPL-3.0-or-later", "LGPL-2.1", "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-2.0"),
               "LGPL-2.0": ("LGPL-2.0", "LGPL-2.1", "LGPL-3.0"), "MPL-2.0": ("MPL-2.0",), "EPL-2.0": ("EPL-2.0", "EPL-1.0"),
               # a bare "BSD" in metadata normalises to 3-clause; a 2-clause file is the same permissive family
               "BSD-2-Clause": ("BSD-2-Clause", "BSD-3-Clause", "0BSD"), "BSD-3-Clause": ("BSD-3-Clause", "BSD-2-Clause", "0BSD")}


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


def licence_file_ids(dist_info: str) -> list[str]:
    """The SPDX id read from the heading of EVERY bundled LICENSE*/LICENCE*/COPYING* file (top level and
    ``licenses/``), unknown headings dropped: what the distribution itself ships, next to what its metadata says."""
    out: list[str] = []
    for sub in ("", "licenses", "license_files"):
        d = os.path.join(dist_info, sub) if sub else dist_info
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.upper().startswith(("LICENSE", "LICENCE", "COPYING")):
                continue
            try:
                with open(os.path.join(d, name), "r", encoding="utf-8", errors="replace") as fh:
                    for ln in fh:
                        if ln.strip():
                            first = " ".join(ln.strip().split()).lower()
                            found = ""
                            for k, v in _FIRST_LINE.items():
                                if first.startswith(k):
                                    found = v
                                    break
                            if not found:
                                found = normalise(ln.strip())
                            if found and found != "UNKNOWN":
                                out.append(found)
                            break
            except OSError:
                continue
    return out


def file_contradicts(metadata_ids: list[str], file_ids: list[str]) -> bool:
    """True when the bundled licence files name licences and NONE of them is one the metadata names (a GPL family
    heading matches any version of that family; a dual-licensed package that ships both files matches on either)."""
    if not file_ids:
        return False
    meta = {i for i in metadata_ids if i and i != "UNKNOWN"}
    if not meta:
        return False
    for fid in file_ids:
        family = set(_GPL_FAMILY.get(fid, (fid,)))
        if meta & family or any(m.startswith(fid) for m in meta) or any(fid.startswith(m) for m in meta):
            return False
    return True


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
    for field_name in ("License-Expression", "License"):
        if len(meta.get_all(field_name) or []) > 1:
            return Dist(name, version, "UNKNOWN", "none", dist_info, f"METADATA carries {field_name} twice (which one applies is undefined)")
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


_HASH_CAP = 4 * 1024 * 1024


def _record_lines(path: str):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            parts = ln.rstrip("\n").split(",")
            rel = parts[0].strip()
            if not rel or rel.startswith("..") or rel.startswith("/"):
                continue
            yield rel, (parts[1].strip() if len(parts) > 1 else ""), (parts[2].strip() if len(parts) > 2 else "")


def _hash_ok(site_packages: str, rel: str, stated: str, size: str) -> bool:
    """RECORD's ``sha256=<urlsafe b64, no padding>`` against the file on disk; a file over 4 MiB (a compiled
    library) is checked by its recorded size instead (the gate runs at every start; stated)."""
    if not stated.startswith("sha256="):
        return False
    full = os.path.join(site_packages, rel)
    try:
        st = os.stat(full)
        if st.st_size > _HASH_CAP:
            return size.isdigit() and int(size) == st.st_size
        with open(full, "rb") as fh:
            digest = hashlib.sha256(fh.read()).digest()
    except OSError:
        return False
    want = stated[len("sha256="):]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii") == want


def _recorded_top_levels(site_packages: str) -> set[str]:
    """Top-level names a distribution's RECORD accounts for — only when EVERY RECORD line under that name that
    names a file present on disk carries a sha256 that verifies (R13: a bare line covers nothing). egg-info
    ``SOURCES.txt`` / ``top_level.txt`` / ``installed-files.txt`` carry no hashes: they cover a name only when the
    distribution's own ``top_level.txt`` names it (its stated own top-levels)."""
    out: set[str] = set()
    for entry in os.listdir(site_packages):
        if not entry.endswith(_DIST_SUFFIXES):
            continue
        d = os.path.join(site_packages, entry)
        if not os.path.isdir(d):
            continue
        record = os.path.join(d, "RECORD")
        if os.path.isfile(record):
            per_top: dict[str, list[bool]] = {}
            try:
                for rel, stated, size in _record_lines(record):
                    top = rel.split("/")[0]
                    if top == entry or rel.endswith((".pyc", ".pth")) and not stated:
                        continue
                    if not os.path.isfile(os.path.join(site_packages, rel)):
                        continue
                    per_top.setdefault(top, []).append(_hash_ok(site_packages, rel, stated, size))
            except OSError:
                continue
            for top, oks in per_top.items():
                if oks and all(oks):
                    out.add(top)
            continue
        tl = os.path.join(d, "top_level.txt")
        if os.path.isfile(tl):
            try:
                with open(tl, "r", encoding="utf-8", errors="replace") as fh:
                    for ln in fh:
                        if ln.strip():
                            out.add(ln.strip())
            except OSError:
                continue
    return out


def _venv_root(site_packages: str) -> Optional[str]:
    """``<venv>/lib/pythonX.Y/site-packages`` → ``<venv>``; None for any other layout."""
    parts = os.path.normpath(site_packages).split(os.sep)
    if len(parts) >= 3 and parts[-1] == "site-packages" and parts[-2].startswith("python") and parts[-3] in ("lib", "lib64", "Lib"):
        return os.sep.join(parts[:-3]) or os.sep
    if len(parts) >= 2 and parts[-1] == "site-packages" and parts[-2] == "Lib":
        return os.sep.join(parts[:-2]) or os.sep
    return None


def pth_paths(site_packages: str) -> list[tuple[str, str]]:
    """(pth file, resolved directory) for every path line of every ``.pth`` (``import`` lines and comments are
    not paths; a relative line is relative to site-packages)."""
    out = []
    for entry in sorted(os.listdir(site_packages)):
        if not entry.endswith(".pth"):
            continue
        try:
            with open(os.path.join(site_packages, entry), "r", encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    line = ln.strip()
                    if not line or line.startswith("#") or line.startswith(("import ", "import\t")):
                        continue
                    out.append((entry, os.path.realpath(os.path.join(site_packages, line))))
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


def check(site_packages: str, allowlist: dict, exceptions: dict, *, _seen: Optional[set] = None) -> Report:
    rep = Report(site_packages=site_packages)
    seen = _seen if _seen is not None else set()
    seen.add(os.path.realpath(site_packages))
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
            if d.problem is None and d.source in ("expression", "license", "classifier"):
                files = licence_file_ids(d.dist_info)
                e = exc.get(d.name) or {}
                if e.get("kind") == "bundled_licence_file" and e.get("reason") and e.get("file_licence") in files and d.licence in allowed:
                    files = [x for x in files if x != e["file_licence"]]     # an explicitly ruled bundled component
                if file_contradicts(ids, files):
                    d.problem = f"metadata says {d.licence} but the bundled licence file(s) read {', '.join(sorted(set(files)))}"
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
    # R13: .pth path lines — outside the venv is a problem; inside it is another site directory to scan
    venv = _venv_root(site_packages)
    for pth, target in pth_paths(site_packages):
        if venv is None or not (target == venv or target.startswith(venv.rstrip(os.sep) + os.sep)):
            rep.problems.append(f"{pth}: adds {target} to sys.path, outside the virtual environment (never scanned by this gate)")
            continue
        if not os.path.isdir(target) or os.path.realpath(target) in seen:
            continue
        sub = check(target, allowlist, exceptions, _seen=seen)
        rep.dists.extend(sub.dists)
        rep.problems.extend(f"{pth} → {p}" for p in sub.problems)
    return rep
