"""Shared, standard-library-only helpers for the supply-chain tools (owner licence ruling 2026-11-08):

* lockfile readers for every ecosystem the repository ships — the shared hash-locked Python lock
  (deploy/python/requirements.lock.txt), every uv.lock, Cargo.lock, every npm package-lock.json, go.mod;
* uv.lock reachability: which locked packages are actually installable from the project root on a real platform
  (an override such as ``langgraph-api ; sys_platform == 'never'`` keeps a package in the lock but never installs it);
* a PEP 508 environment-marker evaluator (the subset uv writes into uv.lock);
* SPDX licence-expression parsing and evaluation against an allow-list.

Used by devtools/licence_gate.py (the offline gate) and devtools/third_party_notices.py (the collector/generator).
No third-party imports: the gate must run on a bare CI python3.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

REPO = Path(__file__).resolve().parent.parent

# Directories never scanned for lockfiles: build output, virtualenvs, installed node_modules, and the delivery-py
# engine's toy fixtures (tiny test inputs for the fix-engine toolchain tests; nothing in them ships).
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "target", ".next", "__pycache__", ".claude"}
SKIP_PREFIXES = ("fixtures/dlv/",)


def pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# --------------------------------------------------------------------------------------------------------------
# lockfile discovery
# --------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Lockfile:
    kind: str          # "requirements" | "uv" | "cargo" | "npm" | "gomod"
    path: str          # repo-relative, forward slashes


def discover_lockfiles(root: Path = REPO) -> list[Lockfile]:
    found: list[Lockfile] = []
    names = {"requirements.lock.txt": "requirements", "uv.lock": "uv", "Cargo.lock": "cargo",
             "package-lock.json": "npm", "go.mod": "gomod"}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if rel.startswith(SKIP_PREFIXES):
            continue
        kind = names.get(path.name) or ("requirements" if path.name.endswith("requirements.lock.txt") else None)
        if kind and path.is_file():
            found.append(Lockfile(kind, rel))
    return found


# --------------------------------------------------------------------------------------------------------------
# Python: hash-locked requirements file
# --------------------------------------------------------------------------------------------------------------

@dataclass
class Locked:
    name: str
    version: str
    hashes: list[str] = field(default_factory=list)
    source: str = "registry"          # registry | git | path | virtual
    extra: dict = field(default_factory=dict)


_REQ_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;\\]+)")


def parse_requirements_lock(text: str) -> list[Locked]:
    """``name==version \\ --hash=sha256:...`` blocks (uv pip compile --generate-hashes). Raises on any requirement
    line that is not an exact ``==`` pin, because an unpinned line cannot be licence-checked against a fixed version."""
    out: list[Locked] = []
    current: Optional[Locked] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash="):
            if current is None:
                raise ValueError(f"hash without a requirement: {raw!r}")
            current.hashes.append(line.split("=", 1)[1].rstrip(" \\"))
            continue
        if line.startswith("-"):
            raise ValueError(f"unsupported option line in a hash lock: {raw!r}")
        m = _REQ_LINE.match(line)
        if not m:
            raise ValueError(f"not an exact pin: {raw!r}")
        current = Locked(pep503(m.group(1)), m.group(2))
        out.append(current)
    for item in out:
        if not item.hashes:
            raise ValueError(f"{item.name}=={item.version} has no --hash (the lock must be hash-pinned)")
    return out


# --------------------------------------------------------------------------------------------------------------
# PEP 508 markers (the subset uv writes)
# --------------------------------------------------------------------------------------------------------------

_TOKEN = re.compile(r"\s*(\(|\)|and\b|or\b|not in\b|in\b|===|==|!=|<=|>=|~=|<|>|'[^']*'|\"[^\"]*\"|[A-Za-z_][A-Za-z0-9_.]*)")
_VERSION_VARS = {"python_version", "python_full_version", "implementation_version"}


def _tokens(marker: str) -> list[str]:
    pos, out = 0, []
    while pos < len(marker):
        if marker[pos:].strip() == "":
            break
        m = _TOKEN.match(marker, pos)
        if not m:
            raise ValueError(f"cannot parse marker at {marker[pos:]!r}")
        out.append(m.group(1))
        pos = m.end()
    return out


def _vtuple(v: str) -> tuple:
    parts = []
    for p in v.split("."):
        num = re.match(r"\d+", p)
        parts.append(int(num.group(0)) if num else 0)
    return tuple(parts)


def _compare(lhs: str, op: str, rhs: str, versiony: bool) -> bool:
    if op in ("in", "not in"):
        res = lhs in rhs
        return res if op == "in" else not res
    if versiony:
        if op in ("==", "!=") and rhs.endswith(".*"):
            prefix = _vtuple(rhs[:-2])
            res = _vtuple(lhs)[: len(prefix)] == prefix
            return res if op == "==" else not res
        a, b = _vtuple(lhs), _vtuple(rhs)
        width = max(len(a), len(b))
        a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
    else:
        a, b = lhs, rhs
    return {"==": a == b, "===": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b,
            "~=": a >= b}[op]


def eval_marker(marker: Optional[str], env: dict) -> bool:
    """Evaluate a PEP 508 marker against ``env`` (variable -> string). Empty marker = True."""
    if not marker or not marker.strip():
        return True
    toks = _tokens(marker)
    pos = 0

    def atom() -> str:
        nonlocal pos
        t = toks[pos]
        pos += 1
        if t[0] in "'\"":
            return t[1:-1]
        if t not in env:
            raise ValueError(f"unknown marker variable {t!r} in {marker!r}")
        return env[t]

    def comparison() -> bool:
        nonlocal pos
        if toks[pos] == "(":
            pos += 1
            v = disjunction()
            if toks[pos] != ")":
                raise ValueError(f"unbalanced marker {marker!r}")
            pos += 1
            return v
        lhs_tok = toks[pos]
        lhs = atom()
        op = toks[pos]
        pos += 1
        rhs_tok = toks[pos]
        rhs = atom()
        versiony = lhs_tok in _VERSION_VARS or rhs_tok in _VERSION_VARS
        return _compare(lhs, op, rhs, versiony)

    def conjunction() -> bool:
        nonlocal pos
        v = comparison()
        while pos < len(toks) and toks[pos] == "and":
            pos += 1
            v = comparison() and v
        return v

    def disjunction() -> bool:
        nonlocal pos
        v = conjunction()
        while pos < len(toks) and toks[pos] == "or":
            pos += 1
            v = conjunction() or v
        return v

    result = disjunction()
    if pos != len(toks):
        raise ValueError(f"trailing tokens in marker {marker!r}")
    return result


def _env(sys_platform: str, machine: str, py: str, impl: str = "cpython") -> dict:
    system = {"linux": "Linux", "darwin": "Darwin", "win32": "Windows"}[sys_platform]
    return {"sys_platform": sys_platform, "platform_system": system, "platform_machine": machine,
            "os_name": "nt" if sys_platform == "win32" else "posix", "implementation_name": impl,
            "platform_python_implementation": "CPython" if impl == "cpython" else "PyPy",
            "python_version": ".".join(py.split(".")[:2]), "python_full_version": py,
            "implementation_version": py, "platform_release": "", "platform_version": "", "extra": ""}


# The real platforms a uv.lock resolution is checked on: every OS/arch the lock covers, on each CPython the
# project's requires-python admits up to 3.14. `sys_platform == 'never'` is false on all of them.
REAL_ENVS = [_env(p, m, v) for p, m in (("linux", "x86_64"), ("linux", "aarch64"), ("darwin", "arm64"),
                                        ("darwin", "x86_64"), ("win32", "AMD64"))
             for v in ("3.12.0", "3.13.0", "3.14.0")]
SHIPPED_ENV = _env("linux", "x86_64", "3.12.0")   # what the images run: python:3.12-slim on linux/amd64


# --------------------------------------------------------------------------------------------------------------
# uv.lock
# --------------------------------------------------------------------------------------------------------------

@dataclass
class UvLock:
    packages: dict            # name -> list of package tables (a name can be locked at several versions)
    roots: list               # the workspace/virtual/editable project tables

    @classmethod
    def load(cls, text: str) -> "UvLock":
        data = tomllib.loads(text)
        pkgs: dict = {}
        roots = []
        for p in data.get("package", []):
            pkgs.setdefault(pep503(p["name"]), []).append(p)
            src = p.get("source", {})
            if "virtual" in src or "editable" in src:
                roots.append(p)
        return cls(pkgs, roots)

    def _resolve(self, dep: dict) -> Optional[dict]:
        cands = self.packages.get(pep503(dep["name"]), [])
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        for c in cands:
            if dep.get("version") in (None, c.get("version")) and (
                    dep.get("source") is None or dep.get("source") == c.get("source")):
                return c
        return cands[0]

    def reachable(self, env: dict, groups: Iterable[str] = ()) -> dict:
        """(name, version) -> package table for every package installable from the roots in ``env``, following
        dependency edges whose marker holds, requested extras (optional-dependencies) and the named
        dependency-groups of the roots."""
        seen: dict = {}
        stack: list = []
        for root in self.roots:
            edges = list(root.get("dependencies", []))
            for g in groups:
                edges += root.get("dev-dependencies", {}).get(g, [])
            stack += [(e, ()) for e in edges]
        while stack:
            dep, _ = stack.pop()
            if not eval_marker(dep.get("marker"), env):
                continue
            pkg = self._resolve(dep)
            if pkg is None:
                raise ValueError(f"uv.lock edge to unlocked package {dep['name']!r}")
            key = (pep503(pkg["name"]), pkg.get("version", ""))
            extras = tuple(dep.get("extra", []))
            visit_key = (key, extras)
            if visit_key in seen:
                continue
            seen[visit_key] = pkg
            edges = list(pkg.get("dependencies", []))
            for ex in extras:
                edges += pkg.get("optional-dependencies", {}).get(ex, [])
            stack += [(e, ()) for e in edges]
        out: dict = {}
        for (key, _extras), pkg in seen.items():
            out[key] = pkg
        return out

    def reachable_any(self, envs: Iterable[dict], groups: Iterable[str] = ()) -> dict:
        out: dict = {}
        for env in envs:
            out.update(self.reachable(env, groups))
        return out

    def dev_groups(self) -> list[str]:
        groups: set = set()
        for root in self.roots:
            groups |= set(root.get("dev-dependencies", {}).keys())
        return sorted(groups)


# --------------------------------------------------------------------------------------------------------------
# Cargo.lock / package-lock.json / go.mod
# --------------------------------------------------------------------------------------------------------------

def parse_cargo_lock(text: str) -> list[Locked]:
    data = tomllib.loads(text)
    out = []
    for p in data.get("package", []):
        src = p.get("source")
        kind = "registry" if src and src.startswith("registry+") else ("git" if src and src.startswith("git+") else "path")
        out.append(Locked(p["name"], p["version"], [p["checksum"]] if p.get("checksum") else [], kind))
    return out


def parse_package_lock(text: str) -> list[Locked]:
    data = json.loads(text)
    if data.get("lockfileVersion", 0) < 2:
        raise ValueError("package-lock.json lockfileVersion < 2 has no per-package licence/integrity table")
    out = []
    for key, p in data.get("packages", {}).items():
        if not key:
            continue
        if p.get("link"):
            continue                         # the target of the link is its own entry
        if "node_modules/" not in key:
            out.append(Locked(p.get("name") or key.rsplit("/", 1)[-1], p.get("version", ""), [], "path",
                              {"key": key, "license": p.get("license"), "dev": bool(p.get("dev"))}))
            continue
        name = key.rsplit("node_modules/", 1)[1]
        out.append(Locked(name, p.get("version", ""), [p["integrity"]] if p.get("integrity") else [], "registry",
                          {"key": key, "license": p.get("license"), "dev": bool(p.get("dev")),
                           "optional": bool(p.get("optional")), "resolved": p.get("resolved")}))
    return out


def parse_go_mod_requires(text: str) -> list[Locked]:
    out = []
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if in_block:
            if line == ")":
                in_block = False
                continue
            mod, ver = line.split()[:2]
            out.append(Locked(mod, ver))
        elif line.startswith("require ("):
            in_block = True
        elif line.startswith("require "):
            mod, ver = line[len("require "):].split()[:2]
            out.append(Locked(mod, ver))
    return out


# --------------------------------------------------------------------------------------------------------------
# SPDX expressions
# --------------------------------------------------------------------------------------------------------------

_SPDX_TOK = re.compile(r"\s*(\(|\)|AND\b|OR\b|WITH\b|[A-Za-z0-9.+\-:]+)")


class MissingLicence(ValueError):
    """A package with no licence value at all (None / empty): always an UNKNOWN-licence violation."""


def _spdx_tokens(expr) -> list[str]:
    if not isinstance(expr, str) or not expr.strip():
        raise MissingLicence(f"missing licence value ({expr!r}): treated as UNKNOWN")
    expr = expr.replace("/", " OR ")             # legacy cargo "MIT/Apache-2.0"
    pos, out = 0, []
    while pos < len(expr):
        if not expr[pos:].strip():
            break
        m = _SPDX_TOK.match(expr, pos)
        if not m:
            raise ValueError(f"cannot parse SPDX expression at {expr[pos:]!r}")
        tok = m.group(1)
        out.append(tok.upper() if tok.upper() in ("AND", "OR", "WITH") else tok)
        pos = m.end()
    return out


def spdx_parse(expr: str):
    """-> nested tuples: ('OR', a, b) / ('AND', a, b) / ('ID', 'MIT', exception_or_None)."""
    toks = _spdx_tokens(expr)
    pos = 0

    def primary():
        nonlocal pos
        if pos >= len(toks):
            raise ValueError(f"truncated SPDX expression {expr!r}")
        t = toks[pos]
        pos += 1
        if t == "(":
            node = disj()
            if pos >= len(toks) or toks[pos] != ")":
                raise ValueError(f"unbalanced SPDX expression {expr!r}")
            pos += 1
            return node
        if t in ("AND", "OR", "WITH", ")"):
            raise ValueError(f"unexpected {t!r} in SPDX expression {expr!r}")
        exc = None
        if pos < len(toks) and toks[pos] == "WITH":
            exc = toks[pos + 1]
            pos += 2
        return ("ID", t, exc)

    def conj():
        nonlocal pos
        node = primary()
        while pos < len(toks) and toks[pos] == "AND":
            pos += 1
            node = ("AND", node, primary())
        return node

    def disj():
        nonlocal pos
        node = conj()
        while pos < len(toks) and toks[pos] == "OR":
            pos += 1
            node = ("OR", node, conj())
        return node

    tree = disj()
    if pos != len(toks):
        raise ValueError(f"trailing tokens in SPDX expression {expr!r}")
    return tree


def spdx_ids(expr: str) -> list[str]:
    out: list[str] = []

    def walk(n):
        if n[0] == "ID":
            out.append(n[1])
        else:
            walk(n[1])
            walk(n[2])
    walk(spdx_parse(expr))
    return out


def spdx_with_exceptions(expr: str) -> list[str]:
    """The `WITH <exception>` ids in an expression."""
    out: list[str] = []

    def walk(n):
        if n[0] == "ID":
            if n[2]:
                out.append(n[2])
        else:
            walk(n[1])
            walk(n[2])
    walk(spdx_parse(expr))
    return out


def spdx_satisfied(expr: str, allowed: set, allowed_exceptions: set = frozenset()) -> bool:
    """True when the expression can be complied with using only ``allowed`` ids (OR: either branch; AND: both).
    A ``WITH`` exception only grants extra permissions; it is accepted when listed in ``allowed_exceptions``."""
    def ok(n) -> bool:
        if n[0] == "ID":
            return n[1] in allowed and (n[2] is None or n[2] in allowed_exceptions)
        if n[0] == "AND":
            return ok(n[1]) and ok(n[2])
        return ok(n[1]) or ok(n[2])
    return ok(spdx_parse(expr))


def load_json(path: Path):
    with open(path, "rb") as fh:
        return json.loads(fh.read())
