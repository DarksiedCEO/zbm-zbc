"""Supply-chain collector and THIRD_PARTY_NOTICES generator (owner licence ruling 2026-11-08: licence notices kept).

    python3 devtools/third_party_notices.py collect --cache <dir>   # network: fetch + hash-verify every artefact
    python3 devtools/third_party_notices.py render  --cache <dir>   # offline: rewrite the committed outputs

``collect`` reads every lockfile the repository ships (devtools/licence_lib.py), downloads each locked artefact from
its registry — PyPI wheels/sdists, crates (.crate files), npm tarballs —
and VERIFIES each against the hash recorded in the lock (sha256 / sha512 integrity / crate checksum); an artefact
whose hash does not match is an error, never a warning. From each verified artefact it extracts the declared
licence (wheel METADATA License-Expression / License / classifiers; the .crate's own Cargo.toml ``license``, the
value ``cargo metadata`` reports; the npm tarball's own package.json ``license`` — package-lock's copy is recorded
beside it, never used in its place) and
every licence/notice file it ships (LICENSE*, COPYING*, NOTICE*, AUTHORS*, ThirdPartyNotices*, BUILD_LICENSES/**).
The vendored restic binary's Go module list comes from ``go version -m``; each module's licence text is fetched
from its upstream repository at the exact tag/commit.

``render`` writes, from the cache alone:

* devtools/licences/inventory.json — name, version, declared licence (raw + SPDX) and source per locked package;
  devtools/licence_gate.py checks the lockfiles against this offline;
* THIRD_PARTY_NOTICES.md — the index; third_party_notices/<component>.md — the full licence texts per shipped
  component (shared Python runtime, delivery-py, ledger-rust, dashboard, restic).

Neither step is wired into a Gate 5 Dockerfile (the seven Gate 5 images are pinned at f5fa44b); see
docs/runbooks/dependency-ownership.md "Follow-ups needing owner approval".
"""

from __future__ import annotations

import argparse
import base64
import email.parser
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import licence_lib as L  # noqa: E402

REPO = L.REPO
LICENCES_DIR = REPO / "devtools" / "licences"
INVENTORY = LICENCES_DIR / "inventory.json"
NOTICES_INDEX = REPO / "THIRD_PARTY_NOTICES.md"
NOTICES_DIR = REPO / "third_party_notices"

LICENCE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|COPYRIGHT|AUTHORS|UNLICENSE|PATENTS|ThirdPartyNotices"
                          r"|THIRD[-_]PARTY[-_]NOTICES)([.\-_ ].*)?$", re.IGNORECASE)
MAX_TEXT = 2 * 1024 * 1024

# Upstream of the vendored restic binary (deploy/docker/maintenance/vendor/restic, binary-manifest.json).
RESTIC_VERSION = "0.19.1"

# Classifier / free-text licence names -> SPDX. Anything not here (and without a License-Expression) is recorded
# as UNKNOWN and must be resolved by a reviewed entry in devtools/licences/exceptions.json.
TEXT_TO_SPDX = {
    "mit": "MIT", "mit license": "MIT", "the mit license": "MIT", "mit-0": "MIT-0", "expat": "MIT",
    "apache": "Apache-2.0", "apache 2": "Apache-2.0", "apache 2.0": "Apache-2.0", "apache-2": "Apache-2.0",
    "apache-2.0": "Apache-2.0", "apache 2.0 license": "Apache-2.0", "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0", "apache software license": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0", "apache license": "Apache-2.0", "asl 2.0": "Apache-2.0",
    "apache license version 2.0": "Apache-2.0", "apache-2.0 license": "Apache-2.0",
    "bsd": "BSD-3-Clause", "bsd license": "BSD-3-Clause", "bsd-3-clause": "BSD-3-Clause", "new bsd": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause", "3-clause bsd": "BSD-3-Clause", "bsd 3-clause": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause", "modified bsd": "BSD-3-Clause", "3-clause bsd license": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause", "2-clause bsd": "BSD-2-Clause", "simplified bsd": "BSD-2-Clause",
    "bsd 2-clause": "BSD-2-Clause", "psfl": "PSF-2.0", "psf": "PSF-2.0", "psf-2.0": "PSF-2.0",
    "python software foundation license": "PSF-2.0", "psf license": "PSF-2.0", "python-2.0": "Python-2.0",
    "isc": "ISC", "isc license": "ISC", "isc license (iscl)": "ISC", "mpl-2.0": "MPL-2.0", "mpl 2.0": "MPL-2.0",
    "mozilla public license 2.0": "MPL-2.0", "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0", "0bsd": "0BSD",
    "unlicense": "Unlicense", "the unlicense": "Unlicense", "the unlicense (unlicense)": "Unlicense",
    "hpnd": "HPND", "historical permission notice and disclaimer (hpnd)": "HPND", "zlib": "Zlib",
    "zlib/libpng license": "Zlib", "cc0 1.0 universal (cc0 1.0) public domain dedication": "CC0-1.0",
    "gnu affero general public license v3": "AGPL-3.0-only",
    "gnu general public license v3 (gplv3)": "GPL-3.0-only", "gnu general public license v2 (gplv2)": "GPL-2.0-only",
    "gnu lesser general public license v3 (lgplv3)": "LGPL-3.0-only",
    "gnu lesser general public license v2 or later (lgplv2+)": "LGPL-2.1-or-later",
    "gnu library or lesser general public license (lgpl)": "LGPL-2.1-or-later",
}

# Licence-text fingerprints for artefacts with no machine-readable licence (Go modules: LICENSE text only).
FINGERPRINTS = [
    ("Mozilla Public License Version 2.0", "MPL-2.0"), ("Mozilla Public License, version 2.0", "MPL-2.0"),
    ("GNU AFFERO GENERAL PUBLIC LICENSE", "AGPL-3.0-only"), ("GNU LESSER GENERAL PUBLIC LICENSE", "LGPL-3.0-only"),
    ("GNU GENERAL PUBLIC LICENSE", "GPL-3.0-only"), ("Apache License", "Apache-2.0"),
    ("Permission is hereby granted, free of charge", "MIT"), ("Permission to use, copy, modify, and/or distribute", "ISC"),
    ("Permission to use, copy, modify, and distribute", "ISC"),
]


def _bsd_kind(text: str) -> Optional[str]:
    if "Redistribution and use in source and binary forms" not in text:
        return None
    return "BSD-3-Clause" if re.search(r"Neither the name|names of (its|their) contributors", text) else "BSD-2-Clause"


def fingerprint(text: str) -> str:
    bsd = _bsd_kind(text)
    if bsd:
        return bsd
    for needle, spdx in FINGERPRINTS:
        if needle.lower() in text.lower():
            return spdx
    return "UNKNOWN"


# --------------------------------------------------------------------------------------------------------------
# fetching (hash-verified)
# --------------------------------------------------------------------------------------------------------------

def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "zbm-supply-chain/1"})
    with urllib.request.urlopen(req, timeout=120) as resp:   # noqa: S310 (https registry URLs from the locks)
        return resp.read()


def fetch_verified(url: str, cache: Path, *, sha256: Optional[str] = None, sri: Optional[str] = None) -> bytes:
    """Download (or reuse the cached copy of) ``url`` and verify it; raises on a mismatch."""
    name = hashlib.sha256(url.encode()).hexdigest()[:16] + "-" + url.rsplit("/", 1)[-1].split("?")[0]
    path = cache / "artefacts" / name
    data = path.read_bytes() if path.exists() else _get(url)
    if sha256 is not None and hashlib.sha256(data).hexdigest() != sha256:
        raise RuntimeError(f"sha256 mismatch for {url}")
    if sri is not None:
        algo, b64 = sri.split("-", 1)
        if base64.b64encode(hashlib.new(algo, data).digest()).decode() != b64:
            raise RuntimeError(f"integrity mismatch for {url}")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return data


def _text(data: bytes) -> str:
    return data[:MAX_TEXT].decode("utf-8", errors="replace")


# --------------------------------------------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------------------------------------------

_WHEEL = re.compile(r"^(?P<name>.+?)-(?P<ver>[^-]+)(-\d[^-]*)?-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$")


def wheel_score(filename: str) -> int:
    """Higher = closer to what the images install (CPython 3.12, manylinux x86_64). -1 = not a wheel."""
    m = _WHEEL.match(filename)
    if not m:
        return -1
    py, abi, plat = m.group("py"), m.group("abi"), m.group("plat")
    score = 0
    if plat == "any":
        score += 20
    elif "x86_64" in plat and ("manylinux" in plat or plat.startswith("linux")):
        score += 30
    elif "linux" in plat:
        score += 5
    if "cp312" in py.split("."):
        score += 10
    elif any(t in ("py3", "py2") for t in py.split(".")) or abi == "abi3":
        score += 8
    return score


def _python_meta_from_wheel(data: bytes) -> dict:
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    meta_name = next(n for n in names if n.count("/") == 1 and n.endswith(".dist-info/METADATA"))
    msg = email.parser.HeaderParser().parsestr(zf.read(meta_name).decode("utf-8", errors="replace"))
    files = {}
    for n in names:
        parts = n.split("/")
        base = parts[-1]
        in_distinfo = parts[0].endswith(".dist-info")
        bundled = len(parts) <= 4 and (LICENCE_FILE.match(base) or "BUILD_LICENSES" in parts)
        if (in_distinfo and ("licenses" in parts or LICENCE_FILE.match(base))) or bundled:
            if n.endswith("/"):
                continue
            files[n] = _text(zf.read(n))
    return {"metadata": msg, "files": files}


def _python_meta_from_sdist(data: bytes) -> dict:
    tf = tarfile.open(fileobj=io.BytesIO(data))
    files, msg = {}, None
    for member in tf.getmembers():
        parts = member.name.split("/")
        if not member.isfile():
            continue
        if len(parts) == 2 and parts[1] == "PKG-INFO":
            msg = email.parser.HeaderParser().parsestr(tf.extractfile(member).read().decode("utf-8", errors="replace"))
        elif len(parts) <= 3 and LICENCE_FILE.match(parts[-1]):
            files[member.name] = _text(tf.extractfile(member).read())
    return {"metadata": msg, "files": files}


def python_declared(msg) -> tuple[str, str, str]:
    """(spdx, raw, source) from a METADATA/PKG-INFO header set."""
    if msg is None:
        return "UNKNOWN", "", "none"
    expr = (msg.get("License-Expression") or "").strip()
    if expr:
        return expr, expr, "License-Expression"
    lic = (msg.get("License") or "").strip()
    if lic and "\n" not in lic and len(lic) < 120:
        mapped = TEXT_TO_SPDX.get(lic.lower().rstrip("."))
        if mapped:
            return mapped, lic, "License"
        try:
            L.spdx_parse(lic)
            if all(i in L_KNOWN_IDS for i in L.spdx_ids(lic)):
                return lic, lic, "License"
        except ValueError:
            pass
    ids = []
    for c in msg.get_all("Classifier") or []:
        m = re.match(r"^License :: (?:OSI Approved :: )?(.+)$", c)
        if m:
            mapped = TEXT_TO_SPDX.get(m.group(1).strip().lower())
            ids.append(mapped or f"UNKNOWN({m.group(1).strip()})")
    ids = [i for i in ids if i != "UNKNOWN(OSI Approved)"]
    if ids and not any(i.startswith("UNKNOWN") for i in ids):
        # several licence classifiers on one dist conventionally mean a choice (dual licensing)
        return " OR ".join(sorted(set(ids))), "; ".join(msg.get_all("Classifier") or []), "Classifier"
    if lic:
        return "UNKNOWN", lic[:300], "License(text)"
    return "UNKNOWN", "", "none"


L_KNOWN_IDS = {"MIT", "MIT-0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "ISC", "Zlib", "PSF-2.0",
               "Python-2.0", "Unlicense", "CC0-1.0", "MPL-2.0", "HPND", "MIT-CMU", "CNRI-Python", "Unicode-3.0",
               "BSL-1.0", "CC-BY-4.0", "LGPL-3.0-or-later", "LGPL-2.1-or-later", "GPL-2.0-only", "GPL-3.0-only"}


def _pypi_files(name: str, version: str) -> list[dict]:
    data = json.loads(_get(f"https://pypi.org/pypi/{name}/{version}/json"))
    return [{"filename": u["filename"], "url": u["url"], "sha256": u["digests"]["sha256"]} for u in data["urls"]]


def collect_python_package(name: str, version: str, candidates: list[dict], cache: Path) -> dict:
    wheels = sorted((c for c in candidates if c["filename"].endswith(".whl")),
                    key=lambda c: -wheel_score(c["filename"]))
    chosen = wheels[0] if wheels else next((c for c in candidates if c["filename"].endswith((".tar.gz", ".zip"))), None)
    if chosen is None:
        raise RuntimeError(f"{name}=={version}: no artefact in the lock")
    data = fetch_verified(chosen["url"], cache, sha256=chosen["sha256"])
    meta = _python_meta_from_wheel(data) if chosen["filename"].endswith(".whl") else _python_meta_from_sdist(data)
    spdx, raw, src = python_declared(meta["metadata"])
    home = ""
    if meta["metadata"] is not None:
        home = meta["metadata"].get("Home-page") or ""
        for pu in meta["metadata"].get_all("Project-URL") or []:
            if not home and "," in pu:
                home = pu.split(",", 1)[1].strip()
    return {"name": name, "version": version, "licence": spdx, "licence_raw": raw, "licence_source": src,
            "artefact": chosen["filename"], "artefact_sha256": chosen["sha256"], "homepage": home,
            "files": meta["files"]}


def collect_requirements_lock(rel: str, cache: Path) -> list[dict]:
    out = []
    for item in L.parse_requirements_lock((REPO / rel).read_text()):
        want = {h.split(":", 1)[1] for h in item.hashes}
        cands = [f for f in _pypi_files(item.name, item.version) if f["sha256"] in want]
        out.append(collect_python_package(item.name, item.version, cands, cache))
    return out


def collect_uv_lock(rel: str, cache: Path) -> list[dict]:
    lock = L.UvLock.load((REPO / rel).read_text())
    reach = lock.reachable_any(L.REAL_ENVS, lock.dev_groups())
    shipped = lock.reachable(L.SHIPPED_ENV)
    roots = {(L.pep503(r["name"]), r.get("version", "")) for r in lock.roots}
    out = []
    for key in sorted(reach):
        if key in roots:
            continue
        pkg = reach[key]
        src = pkg.get("source", {})
        if "git" in src:
            out.append(collect_git_python(pkg, cache))
        else:
            cands = [{"filename": w["url"].rsplit("/", 1)[-1], "url": w["url"], "sha256": w["hash"].split(":", 1)[1]}
                     for w in pkg.get("wheels", [])]
            if "sdist" in pkg:
                s = pkg["sdist"]
                cands.append({"filename": s["url"].rsplit("/", 1)[-1], "url": s["url"], "sha256": s["hash"].split(":", 1)[1]})
            out.append(collect_python_package(pkg["name"], pkg["version"], cands, cache))
        out[-1]["shipped"] = key in shipped
    return out


# The deer-flow git dependency (services/delivery-py/pyproject.toml): its LICENSE at the pinned commit, with the
# sha256 recorded in services/delivery-py/seed/licence_exceptions.json.
DEERFLOW_LICENSE_SHA256 = "b23dff4d4bac8d17b6efaaa7e88c2b730149c4dda45699ce676b719cfdffe7e1"


def collect_git_python(pkg: dict, cache: Path) -> dict:
    git = pkg["source"]["git"]
    m = re.match(r"https://github.com/([^/]+/[^/?#]+?)(?:\.git)?\?.*#([0-9a-f]{40})$", git)
    if not m:
        raise RuntimeError(f"unsupported git source {git}")
    repo, sha = m.group(1), m.group(2)
    url = f"https://raw.githubusercontent.com/{repo}/{sha}/LICENSE"
    expected = DEERFLOW_LICENSE_SHA256 if repo == "bytedance/deer-flow" else None
    text = _text(fetch_verified(url, cache, sha256=expected))
    return {"name": pkg["name"], "version": pkg["version"], "licence": fingerprint(text),
            "licence_raw": f"LICENSE at {repo}@{sha}", "licence_source": "git LICENSE (sha256-verified)",
            "artefact": f"git+https://github.com/{repo}@{sha}", "artefact_sha256": expected or "",
            "homepage": f"https://github.com/{repo}", "files": {f"{repo}@{sha[:12]}/LICENSE": text}}


# --------------------------------------------------------------------------------------------------------------
# Rust
# --------------------------------------------------------------------------------------------------------------

def _crate_bytes(name: str, version: str, checksum: str, cache: Path) -> bytes:
    """The .crate exactly as Cargo.lock names it: the local cargo registry cache if present, else static.crates.io;
    either way its sha256 must equal the lock's checksum (raises otherwise)."""
    for d in sorted((Path.home() / ".cargo" / "registry" / "cache").glob("*")):
        f = d / f"{name}-{version}.crate"
        if f.is_file():
            data = f.read_bytes()
            if hashlib.sha256(data).hexdigest() != checksum:
                raise RuntimeError(f"{f}: sha256 does not match the Cargo.lock checksum")
            return data
    return fetch_verified(f"https://static.crates.io/crates/{name}/{name}-{version}.crate", cache, sha256=checksum)


def crate_declared(manifest_text: str, files: dict) -> tuple[str, str, str]:
    """(spdx, raw, source) from a crate's own Cargo.toml `license` (the value `cargo metadata` reports)."""
    pkg = tomllib.loads(manifest_text).get("package", {}) if manifest_text else {}
    lic = pkg.get("license")
    if isinstance(lic, str) and lic.strip():
        return lic.replace("/", " OR "), lic, "Cargo.toml license"
    lf = pkg.get("license-file")
    if lf:
        return (fingerprint("\n".join(files.values())) if files else "UNKNOWN"), f"license-file {lf}", "license-file"
    return "UNKNOWN", "", "none"


def collect_cargo(rel: str, cache: Path) -> list[dict]:
    """Every registry crate in Cargo.lock, read from its .crate after verifying the sha256 against the lock's
    checksum: the declared licence is the crate's own Cargo.toml `license` (what `cargo metadata` reports), the
    texts are the licence files shipped inside the .crate."""
    out = []
    for lk in sorted(L.parse_cargo_lock((REPO / rel).read_text()), key=lambda p: (p.name, p.version)):
        if lk.source != "registry":
            continue
        if not lk.hashes:
            raise RuntimeError(f"{rel}: {lk.name} {lk.version} has no checksum")
        tf = tarfile.open(fileobj=io.BytesIO(_crate_bytes(lk.name, lk.version, lk.hashes[0], cache)))
        top = f"{lk.name}-{lk.version}"
        files, manifest = {}, ""
        for member in tf.getmembers():
            parts = member.name.split("/")
            if not member.isfile() or parts[0] != top:
                continue
            if len(parts) == 2 and parts[1] == "Cargo.toml":
                manifest = tf.extractfile(member).read().decode("utf-8", errors="replace")
            elif len(parts) == 2 and LICENCE_FILE.match(parts[1]):
                files[f"{top}/{parts[1]}"] = _text(tf.extractfile(member).read())
        pkg = tomllib.loads(manifest).get("package", {}) if manifest else {}
        lf = pkg.get("license-file")
        if lf and f"{top}/{lf}" not in files:
            try:
                files[f"{top}/{lf}"] = _text(tf.extractfile(f"{top}/{lf}").read())
            except KeyError:
                pass
        spdx, raw, src = crate_declared(manifest, files)
        out.append({"name": lk.name, "version": lk.version, "licence": spdx, "licence_raw": raw, "licence_source": src,
                    "artefact": f"{top}.crate", "artefact_sha256": lk.hashes[0],
                    "homepage": pkg.get("repository") or pkg.get("homepage") or "", "files": files})
    return out


# --------------------------------------------------------------------------------------------------------------
# npm
# --------------------------------------------------------------------------------------------------------------

def npm_package_json_licence(pkgjson: dict) -> str:
    """SPDX expression from a package.json: `license` (string or legacy {type}), else legacy `licenses` [{type}]
    (a list conventionally means a choice, joined with OR); UNKNOWN when absent."""
    lic = pkgjson.get("license")
    if isinstance(lic, dict):
        lic = lic.get("type")
    if isinstance(lic, str) and lic.strip():
        return lic.strip()
    legacy = pkgjson.get("licenses")
    if isinstance(legacy, list):
        ids = [x.get("type") if isinstance(x, dict) else x for x in legacy]
        ids = [i for i in ids if isinstance(i, str) and i.strip()]
        if ids:
            return " OR ".join(ids)
    return "UNKNOWN"


def collect_npm(rel: str, cache: Path) -> list[dict]:
    out = []
    for item in L.parse_package_lock((REPO / rel).read_text()):
        if item.source == "path":
            continue                              # the repository's own stub packages (apps/*/stubs)
        data = fetch_verified(item.extra["resolved"], cache, sri=item.hashes[0])
        tf = tarfile.open(fileobj=io.BytesIO(data))
        files, pkgjson = {}, {}
        for member in tf.getmembers():
            parts = member.name.split("/")
            if not member.isfile():
                continue
            if len(parts) == 2 and parts[1] == "package.json":
                pkgjson = json.loads(tf.extractfile(member).read())
            elif len(parts) == 2 and LICENCE_FILE.match(parts[1]):
                files[f"{item.name}@{item.version}/{parts[1]}"] = _text(tf.extractfile(member).read())
        # The licence is the one in the hash-verified tarball's own package.json (the source of truth);
        # package-lock's `license` is a copy npm made of it and is recorded beside it for the gate to compare.
        lic = npm_package_json_licence(pkgjson)
        repo = pkgjson.get("repository")
        home = pkgjson.get("homepage") or (repo.get("url") if isinstance(repo, dict) else repo) or ""
        out.append({"name": item.name, "version": item.version, "licence": lic, "licence_raw": lic,
                    "lock_licence": item.extra.get("license"),
                    "licence_source": "tarball package.json license", "artefact": item.extra["resolved"].rsplit("/", 1)[-1],
                    "artefact_integrity": item.hashes[0], "homepage": home, "dev": item.extra.get("dev", False),
                    "files": files})
    return out


# --------------------------------------------------------------------------------------------------------------
# Go: the vendored restic binary
# --------------------------------------------------------------------------------------------------------------

VANITY = [
    (r"^golang\.org/x/([^/]+)", lambda m: f"golang/{m.group(1)}"),
    (r"^cloud\.google\.com/go(/.*)?$", lambda m: "googleapis/google-cloud-go"),
    (r"^google\.golang\.org/api", lambda m: "googleapis/google-api-go-client"),
    (r"^google\.golang\.org/grpc", lambda m: "grpc/grpc-go"),
    (r"^google\.golang\.org/protobuf", lambda m: "protocolbuffers/protobuf-go"),
    (r"^google\.golang\.org/genproto", lambda m: "googleapis/go-genproto"),
    (r"^go\.opentelemetry\.io/otel", lambda m: "open-telemetry/opentelemetry-go"),
    (r"^go\.opentelemetry\.io/contrib", lambda m: "open-telemetry/opentelemetry-go-contrib"),
    (r"^go\.opentelemetry\.io/auto", lambda m: "open-telemetry/opentelemetry-go-instrumentation"),
    (r"^go\.uber\.org/([^/]+)", lambda m: f"uber-go/{m.group(1)}"),
    (r"^go\.yaml\.in/yaml", lambda m: "yaml/go-yaml"),
    (r"^gopkg\.in/([^.]+)\.v\d+", lambda m: f"go-{m.group(1)}/{m.group(1)}"),
    (r"^github\.com/([^/]+/[^/]+)", lambda m: m.group(1)),
]


def go_module_refs(module: str, version: str) -> tuple[str, list[str]]:
    """(owner/repo, candidate git refs) for a module version: the module-path tag for a nested module, the plain
    tag, or the commit of a pseudo-version."""
    repo = None
    for pat, fn in VANITY:
        m = re.match(pat, module)
        if m:
            repo = fn(m)
            break
    if repo is None:
        raise RuntimeError(f"no upstream mapping for Go module {module}")
    ver = version.replace("+incompatible", "")
    pseudo = re.match(r"^v\d+\.\d+\.\d+-(?:0\.)?\d{14}-([0-9a-f]{12})$", ver) or re.search(r"\.\d{14}-([0-9a-f]{12})$", ver)
    if pseudo:
        return repo, [pseudo.group(1)]
    prefix = {"googleapis/google-cloud-go": "cloud.google.com/go", "open-telemetry/opentelemetry-go": "go.opentelemetry.io/otel",
              "open-telemetry/opentelemetry-go-contrib": "go.opentelemetry.io/contrib",
              "open-telemetry/opentelemetry-go-instrumentation": "go.opentelemetry.io/auto"}.get(repo)
    sub = ""
    if prefix and module.startswith(prefix):
        sub = module[len(prefix):].strip("/")
    elif module.startswith("github.com/"):
        sub = "/".join(module.split("/")[3:])
    sub = re.sub(r"(^|/)v\d+$", "", sub)
    refs = [f"{sub}/{ver}", ver] if sub else [ver]
    return repo, refs


GO_PINS = LICENCES_DIR / "go-licence-pins.json"
LICENCE_NAMES = ("LICENSE", "LICENSE.txt", "LICENSE.md", "COPYING", "LICENCE")


def _ls_remote(repo: str, ref: str) -> Optional[str]:
    """The commit a tag names (peeled for annotated tags), via ls-remote; a 7-40 hex ref is already a commit."""
    if re.fullmatch(r"[0-9a-f]{7,40}", ref):
        return ref
    out = subprocess.run(["git", "ls-remote", f"https://github.com/{repo}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"],
                         capture_output=True, text=True, timeout=60).stdout
    rows = {}
    for line in out.splitlines():
        if "\t" in line:
            sha, name = line.split("\t", 1)
            rows[name] = sha
    return rows.get(f"refs/tags/{ref}^{{}}") or rows.get(f"refs/tags/{ref}")


def pinned_licence_text(key: str, repo: str, refs: list, cache: Path, pins: dict, pin_new: bool) -> tuple:
    """(text, "repo@commit/file") for an upstream licence text. A recorded pin in devtools/licences/go-licence-pins.json
    (immutable commit + sha256 of the file) is fetched at that commit and verified; a key without a pin is an error
    unless ``pin_new``, which resolves the tag to its commit, fetches the file at the commit and records the pin."""
    pin = pins.get(key)
    if pin:
        url = f"https://raw.githubusercontent.com/{pin['repo']}/{pin['commit']}/{pin['file']}"
        return _text(fetch_verified(url, cache, sha256=pin["sha256"])), f"{pin['repo']}@{pin['commit']}/{pin['file']}"
    if not pin_new:
        raise RuntimeError(f"no licence pin for {key} in devtools/licences/go-licence-pins.json; "
                           "rerun `collect --pin-new` and review the new pins")
    for ref in refs:
        commit = _ls_remote(repo, ref)
        if not commit:
            continue
        for fname in LICENCE_NAMES:
            url = f"https://raw.githubusercontent.com/{repo}/{commit}/{fname}"
            try:
                data = _get(url)
            except urllib.error.HTTPError:
                continue
            pins[key] = {"repo": repo, "ref": ref, "commit": commit, "file": fname,
                         "sha256": hashlib.sha256(data).hexdigest()}
            print(f"PINNED {key}: {repo}@{commit}/{fname}", file=sys.stderr)
            return _text(fetch_verified(url, cache, sha256=pins[key]["sha256"])), f"{repo}@{commit}/{fname}"
    raise RuntimeError(f"no LICENSE found for {key} at {repo} {refs}")


def collect_restic(cache: Path, pin_new: bool = False) -> list[dict]:
    binary = REPO / "deploy" / "docker" / "maintenance" / "vendor" / "restic"
    manifest = L.load_json(REPO / "deploy" / "docker" / "maintenance" / "binary-manifest.json")
    if hashlib.sha256(binary.read_bytes()).hexdigest() != manifest["sha256"]["restic"]:
        raise RuntimeError("vendored restic does not match binary-manifest.json")
    info = subprocess.run(["go", "version", "-m", str(binary)], capture_output=True, text=True, check=True).stdout
    go_version = info.splitlines()[0].split(": ", 1)[1].strip()
    pins = L.load_json(GO_PINS)["pins"] if GO_PINS.exists() else {}
    src = "upstream LICENSE at a pinned commit (sha256-verified, fingerprinted)"
    out = []
    text, used = pinned_licence_text(f"github.com/restic/restic@v{RESTIC_VERSION}", "restic/restic",
                                     [f"v{RESTIC_VERSION}"], cache, pins, pin_new)
    out.append({"name": "github.com/restic/restic", "version": f"v{RESTIC_VERSION}", "licence": fingerprint(text),
                "licence_raw": used, "licence_source": src, "artefact": "restic binary",
                "artefact_sha256": manifest["sha256"]["restic"], "homepage": "https://github.com/restic/restic",
                "files": {f"restic@v{RESTIC_VERSION}/LICENSE": text}})
    text, used = pinned_licence_text(f"go@{go_version}", "golang/go", [go_version], cache, pins, pin_new)
    out.append({"name": "go (standard library and runtime)", "version": go_version, "licence": fingerprint(text),
                "licence_raw": used, "licence_source": src, "artefact": "linked into restic",
                "homepage": "https://go.dev", "files": {f"go@{go_version}/LICENSE": text}})
    for line in info.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0] != "dep":
            continue
        module, version = parts[1], parts[2]
        repo, refs = go_module_refs(module, version)
        text, used = pinned_licence_text(f"{module}@{version}", repo, refs, cache, pins, pin_new)
        out.append({"name": module, "version": version, "licence": fingerprint(text), "licence_raw": used,
                    "licence_source": src, "artefact": "linked into restic",
                    "go_sum": parts[3] if len(parts) > 3 else "", "homepage": f"https://github.com/{repo}",
                    "files": {f"{module}@{version}/{used.rsplit('/', 1)[-1]}": text}})
    if pin_new:
        GO_PINS.write_text(json.dumps({"_doc": (
            "Licence texts of the code linked into the vendored restic binary, pinned for "
            "devtools/third_party_notices.py: upstream repository, the tag / pseudo-version ref, the immutable commit "
            "it resolved to (ls-remote, peeled tag), the licence file and its sha256. collect refuses an unpinned "
            "module and verifies every fetched text against its sha256. (The module zips are identified by the h1 "
            "go.sum hashes that `go version -m` prints; proxy.golang.org was not reachable from the recording box, so "
            "the texts are pinned by commit + sha256 instead of being read from the h1-verified module zip.)"),
            "pins": dict(sorted(pins.items()))}, indent=1) + "\n")
    return out


# --------------------------------------------------------------------------------------------------------------
# components and outputs
# --------------------------------------------------------------------------------------------------------------

COMPONENTS = [
    # id, title, lockfile (or None), collector, shipped-in
    ("python-shared", "Shared Python runtime (deploy/python/requirements.lock.txt)", "deploy/python/requirements.lock.txt",
     "requirements", "Gate 5 images seo, legal, finance, compliance, detection, admin/maintenance; client-portal"),
    ("python-delivery", "delivery-py (services/delivery-py/uv.lock)", "services/delivery-py/uv.lock", "uv",
     "delivery-py image (not a Gate 5 image)"),
    ("python-sandbox", "delivery-py fix-engine sandbox (services/delivery-py/docker/sandbox-requirements.lock.txt)",
     "services/delivery-py/docker/sandbox-requirements.lock.txt", "requirements",
     "dlv-sandbox image (test runner for the delivery engine; not a Gate 5 image)"),
    ("rust-ledger", "ledger-rust (services/ledger-rust/Cargo.lock)", "services/ledger-rust/Cargo.lock", "cargo",
     "Gate 5 ledger image (ledger-server binary)"),
    ("npm-dashboard", "Revenue Recovery dashboard (apps/dashboard-ts/package-lock.json)",
     "apps/dashboard-ts/package-lock.json", "npm", "dashboard (Next.js; not a Gate 5 image)"),
    ("npm-railway", "Railway deploy tooling (.railway/package-lock.json)", ".railway/package-lock.json", "npm",
     "CI/deploy tooling only (not shipped in an image)"),
    ("go-restic", f"restic {RESTIC_VERSION} (deploy/docker/maintenance/vendor/restic)", None, "restic",
     "Gate 5 images ledger, legal, finance, compliance, seo, admin (/opt/zbm-maintenance/bin/restic)"),
]


def collect_component(kind: str, lock: Optional[str], cache: Path, pin_new: bool = False) -> list[dict]:
    """Rows for one component, every artefact verified against its lock hash (also used by licence_gate.py
    --verify-sources to re-derive each declared licence from the source of truth)."""
    if kind == "restic":
        return collect_restic(cache, pin_new)
    fn = {"requirements": collect_requirements_lock, "uv": collect_uv_lock, "cargo": collect_cargo,
          "npm": collect_npm}[kind]
    return fn(lock, cache)


def collect(cache: Path, pin_new: bool = False) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    for cid, _title, lock, kind, _where in COMPONENTS:
        rows = collect_component(kind, lock, cache, pin_new)
        (cache / f"{cid}.json").write_text(json.dumps(rows, indent=1, sort_keys=True))
        print(f"{cid}: {len(rows)} packages", file=sys.stderr)


def _anchor(s: str) -> str:
    return re.sub(r"[^a-z0-9-]", "", re.sub(r"[\s/@.]+", "-", s.lower()))


def render(cache: Path) -> None:
    inventory = {"_doc": ("GENERATED by devtools/third_party_notices.py render — do not edit. Declared licence of every "
                          "locked package, per component, as read from the hash-verified artefact. devtools/licence_gate.py "
                          "checks the lockfiles against this offline; fix a wrong value in devtools/licences/exceptions.json."),
                 "components": {}}
    NOTICES_DIR.mkdir(exist_ok=True)
    index = ["# Third-party notices", "",
             "This repository ships third-party software under the licences listed below. The full licence and notice "
             "texts of every package, as shipped inside each package's own artefact, are in `third_party_notices/`. "
             "Generated by `devtools/third_party_notices.py` (see `docs/runbooks/dependency-ownership.md`); do not edit "
             "by hand.", "",
             "Licence policy (owner ruling 2026-11-08): permissive licences only (MIT, Apache-2.0, BSD, ISC, Zlib, PSF, "
             "Unlicense, CC0); MPL-2.0 allowed and flagged; no AGPL/GPL/LGPL in shipped services; no source-available "
             "licences. Enforced by `devtools/licence_gate.py`.", "",
             "| Component | Shipped in | Packages | Licences | Notices |", "|---|---|---|---|---|"]
    reviewed = {(e["component"], e["name"], e["version"]): e
                for e in L.load_json(LICENCES_DIR / "exceptions.json")["exceptions"]}
    for cid, title, lock, kind, where in COMPONENTS:
        rows = json.loads((cache / f"{cid}.json").read_text())
        for r in rows:
            e = reviewed.get((cid, r["name"], r["version"]))
            r["effective"] = (e or {}).get("licence") or r["licence"]
            r["status"] = (e or {}).get("status", "")
        inventory["components"][cid] = {
            "lockfile": lock, "kind": kind,
            "packages": [{k: r[k] for k in ("name", "version", "licence", "licence_raw", "licence_source",
                                            "artefact_sha256", "artefact_integrity")
                          if k in r} | ({"shipped": r["shipped"]} if "shipped" in r else {})
                         | ({"dev": r["dev"]} if r.get("dev") else {}) for r in rows]}
        counts: dict = {}
        for r in rows:
            counts[r["effective"]] = counts.get(r["effective"], 0) + 1
        summary = ", ".join(f"{k} ({v})" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        index.append(f"| {title} | {where} | {len(rows)} | {summary} | [third_party_notices/{cid}.md](third_party_notices/{cid}.md) |")
        body = [f"# Third-party notices: {title}", "", f"Shipped in: {where}.", "",
                "Generated by `devtools/third_party_notices.py`; each licence text below is the file shipped inside "
                "the named, hash-verified artefact.", "", "| Package | Version | Licence | Artefact |", "|---|---|---|---|"]
        for r in rows:
            extra = " (dev only)" if r.get("dev") else (" (not installed on linux/amd64)" if r.get("shipped") is False else "")
            tag = {"approved-flagged": " FLAGGED", "pending-owner-decision": " PENDING OWNER DECISION",
                   "reviewed-metadata": " (reviewed)"}.get(r["status"], "")
            body.append(f"| [{r['name']}](#{_anchor(r['name'] + '-' + r['version'])}) | {r['version']} | "
                        f"{r['effective']}{tag}{extra} | `{r.get('artefact', '')}` |")
        body.append("")
        # A text shipped byte-for-byte identically by several packages (the Apache-2.0 text, most often) is printed
        # once under "Shared licence texts" and referenced; every text that differs (own copyright line) is inline.
        uses: dict = {}
        for r in rows:
            for text in r["files"].values():
                h = hashlib.sha256(text.encode()).hexdigest()[:16]
                uses[h] = uses.get(h, 0) + 1
        shared: dict = {}
        for r in rows:
            body += [f"## {r['name']} {r['version']}", "", f"<a id=\"{_anchor(r['name'] + '-' + r['version'])}\"></a>", "",
                     f"Licence: **{r['effective']}**"
                     + (f" ({r['status']}; see devtools/licences/exceptions.json)" if r["status"] else "")
                     + f" (declared:`{(r.get('licence_raw') or '')[:200]}`, from "
                     f"{r.get('licence_source', '')})" + (f". Upstream: {r['homepage']}" if r.get("homepage") else ""), ""]
            if not r["files"]:
                body += ["_The artefact ships no licence file; the declared licence above applies._", ""]
            for fname, text in sorted(r["files"].items()):
                h = hashlib.sha256(text.encode()).hexdigest()[:16]
                if uses[h] > 1:
                    shared.setdefault(h, text)
                    body += [f"### `{fname}`", "", f"Identical to [shared text {h}](#text-{h}).", ""]
                    continue
                fence = "````" if "```" in text else "```"
                body += [f"### `{fname}`", "", fence + "text", text.rstrip("\n"), fence, ""]
        if shared:
            body += ["## Shared licence texts", ""]
            for h, text in shared.items():
                fence = "````" if "```" in text else "```"
                body += [f"### Text {h}", "", f"<a id=\"text-{h}\"></a>", "", fence + "text", text.rstrip("\n"), fence, ""]
        (NOTICES_DIR / f"{cid}.md").write_text("\n".join(body) + "\n", encoding="utf-8")
    index += ["", "## Notes", "",
              "* MPL-2.0 components are listed with the source location of the covered files (MPL-2.0 §3.2); "
              "they are used unmodified.",
              "* The pypdfium2 wheel's `BUILD_LICENSES` (PDFium and its bundled libraries) and onnxruntime's "
              "`ThirdPartyNotices.txt` are reproduced in `third_party_notices/python-delivery.md`.",
              f"* restic {RESTIC_VERSION} is BSD-2-Clause; its linked Go modules and the Go runtime are listed in "
              "`third_party_notices/go-restic.md`.", "",
              "## Operating-system layer (not covered by the licence gate) — PENDING OWNER DECISION", "",
              "The gate and the tables above cover **application dependencies only** (the lockfiles and the vendored "
              "restic binary). Every image also contains its Debian base layer, which carries copyleft system "
              "packages that the application never links or modifies:", "",
              "* `python:3.12-slim` (all Python images, and the runtime stage of the ledger, orchestrator and "
              "stripe-gateway images): glibc (LGPL-2.1-or-later), bash, coreutils, dpkg, apt, tar, grep, sed, "
              "findutils, util-linux (GPL-2.0/GPL-3.0 family), plus permissive libraries (OpenSSL Apache-2.0, "
              "zlib, ...); CPython itself is PSF-2.0.",
              "* delivery-py image: apt installs `git` (GPL-2.0-only) and `ca-certificates`; the delivery sandbox "
              "image also installs `curl` (purged before the image is finished), `build-essential` (gcc, make, "
              "binutils, libc6-dev: GPL-3.0 family / LGPL), `pkg-config` and `libssl-dev`.",
              "* Build stages only (not in the shipped images): `rust:1.99.1-slim-bookworm`, "
              "`golang:1.27.3-bookworm`.", "",
              "Each Debian package's own licence is in the image at `/usr/share/doc/<package>/copyright`. "
              "**Carve-out PENDING OWNER DECISION** — recommended: allow unmodified OS/system packages from the "
              "distribution, used as-is through their normal interfaces (the 2026-11-08 ruling then binds the "
              "application layer, which must stay permissive). See docs/runbooks/dependency-ownership.md.", ""]
    NOTICES_INDEX.write_text("\n".join(index), encoding="utf-8")
    LICENCES_DIR.mkdir(exist_ok=True)
    INVENTORY.write_text(json.dumps(inventory, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["collect", "render", "all"])
    ap.add_argument("--cache", required=True, type=Path, help="artefact cache directory (outside the repository)")
    ap.add_argument("--pin-new", action="store_true",
                    help="record commit + sha256 pins for Go licence texts not yet in devtools/licences/go-licence-pins.json")
    args = ap.parse_args(argv)
    if args.command in ("collect", "all"):
        collect(args.cache, args.pin_new)
    if args.command in ("render", "all"):
        render(args.cache)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
