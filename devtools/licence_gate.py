"""Repository licence gate (owner licence ruling 2026-11-08). Offline, standard library only.

    python3 devtools/licence_gate.py                    # exit 0 clean, 1 violation, 2 only pending owner decisions
    python3 devtools/licence_gate.py --allow-pending    # pending owner decisions reported, exit 0
    python3 devtools/licence_gate.py --json report.json

What it checks, for every lockfile in the repository (devtools/licence_lib.py discover_lockfiles):

1. every lockfile is a reviewed component in devtools/licences/policy.json (a new lockfile fails until listed);
2. the package set the lockfile installs equals the set in devtools/licences/inventory.json (generated from the
   hash-verified artefacts by devtools/third_party_notices.py) — a lock change without a regenerated inventory fails;
   for a uv.lock the set is what is REACHABLE from the project root on a real platform (overrides with an
   unsatisfiable marker such as ``sys_platform == 'never'`` are not installable and are not counted);
3. each package's licence (inventory, or a reviewed exceptions.json entry) is satisfiable from policy 'allowed';
   a 'flagged' id (MPL-2.0) passes only with an approved-flagged entry; anything else needs an entry, and a
   pending-owner-decision entry is reported as PENDING (exit 2 unless --allow-pending), never passed silently;
   an owner-approved entry (approved_by, approved_on, attribution required) passes for that exact package version
   and is listed as APPROVED on every run — it can never admit a policy 'forbidden_ids' licence;
4. none of policy 'forbidden_packages' is installable from any Python lock on any real platform;
5. go.mod files have no more requires than reviewed; the vendored restic binary matches its manifest sha256 and,
   when a ``go`` toolchain is on PATH, its module list matches the inventory;
6. no exceptions entry is stale (matches nothing, or is not needed);
7. the inventory agrees with the lockfiles themselves (offline, always): npm package-lock `license` == inventory
   licence; every inventory row names an artefact hash the lock pins (sha256 / Cargo.lock checksum / npm integrity);
8. with ``--verify-sources --cache <dir>``: every declared licence is re-derived from the hash-verified artefact
   (wheel/sdist METADATA, the .crate's Cargo.toml — the `cargo metadata` value —, the npm tarball's own
   package.json, restic's pinned Go licence texts) and must equal the inventory; for npm the tarball value must
   also equal package-lock's `license`. CI should run this mode (it needs network for uncached artefacts).
A package with no licence value at all is an explicit UNKNOWN-licence violation.

CI runs it in the `licence-gate` job of .github/workflows/ci.yml (docs/runbooks/dependency-ownership.md, "CI wiring").
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import licence_lib as L  # noqa: E402

DEFAULT_POLICY = L.REPO / "devtools" / "licences" / "policy.json"
DEFAULT_EXCEPTIONS = L.REPO / "devtools" / "licences" / "exceptions.json"
DEFAULT_INVENTORY = L.REPO / "devtools" / "licences" / "inventory.json"


@dataclass
class Result:
    violations: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    flagged: list = field(default_factory=list)
    approved: list = field(default_factory=list)
    checked: dict = field(default_factory=dict)

    def exit_code(self, allow_pending: bool) -> int:
        if self.violations:
            return 1
        if self.pending and not allow_pending:
            return 2
        return 0


def locked_set(root: Path, comp: dict, policy: dict, res: Result, cid: str) -> dict:
    """(name, version) -> what the lockfile itself records for the package: 'hashes' (the artefact hashes the lock
    accepts) and, for npm, 'license' (package-lock's own licence field)."""
    kind, rel = comp["kind"], comp.get("lockfile")
    forbidden = {L.pep503(n) for n in policy["forbidden_packages"]["python"]}
    if kind == "requirements":
        items = L.parse_requirements_lock((root / rel).read_text())
        for it in items:
            if it.name in forbidden:
                res.violations.append(f"{cid}: forbidden package {it.name}=={it.version} is pinned in {rel}")
        return {(it.name, it.version): {"hashes": {h.split(":", 1)[-1] for h in it.hashes}} for it in items}
    if kind == "uv":
        lock = L.UvLock.load((root / rel).read_text())
        reach = lock.reachable_any(L.REAL_ENVS, lock.dev_groups())
        for env in L.REAL_ENVS:
            for (name, ver) in lock.reachable(env, lock.dev_groups()):
                if name in forbidden:
                    res.violations.append(
                        f"{cid}: forbidden package {name}=={ver} is installable from {rel} on "
                        f"{env['sys_platform']}/{env['platform_machine']} python {env['python_full_version']}")
        roots = {(L.pep503(r["name"]), r.get("version", "")) for r in lock.roots}
        out = {}
        for k, pkg in reach.items():
            if k in roots:
                continue
            arts = list(pkg.get("wheels", [])) + ([pkg["sdist"]] if "sdist" in pkg else [])
            out[k] = {"hashes": {a["hash"].split(":", 1)[-1] for a in arts if a.get("hash")},
                      "git": "git" in pkg.get("source", {})}
        return out
    if kind == "cargo":
        return {(p.name, p.version): {"hashes": set(p.hashes)}
                for p in L.parse_cargo_lock((root / rel).read_text()) if p.source != "path"}
    if kind == "npm":
        return {(p.name, p.version): {"hashes": set(p.hashes), "license": p.extra.get("license")}
                for p in L.parse_package_lock((root / rel).read_text()) if p.source != "path"}
    if kind == "gomod":
        reqs = L.parse_go_mod_requires((root / rel).read_text())
        if len(reqs) > comp.get("max_requires", 0):
            res.violations.append(f"{cid}: {rel} has {len(reqs)} require(s) ({', '.join(r.name for r in reqs)}); "
                                  f"reviewed maximum is {comp.get('max_requires', 0)} — add a licence review first")
        return {}
    if kind == "restic":
        binary, manifest = root / comp["binary"], L.load_json(root / comp["manifest"])
        if hashlib.sha256(binary.read_bytes()).hexdigest() != manifest["sha256"]["restic"]:
            res.violations.append(f"{cid}: {comp['binary']} sha256 does not match {comp['manifest']}")
        return {}
    raise ValueError(f"unknown component kind {kind!r}")


def restic_modules(root: Path, comp: dict) -> set | None:
    go = shutil.which("go")
    if not go:
        return None
    out = subprocess.run([go, "version", "-m", str(root / comp["binary"])], capture_output=True, text=True, check=True).stdout
    mods = {(p[1], p[2]) for p in (ln.split() for ln in out.splitlines()) if len(p) >= 3 and p[0] == "dep"}
    first = out.splitlines()[0].split(": ", 1)[1].strip()
    return mods | {("go (standard library and runtime)", first)}


def cross_check_lock(cid: str, kind: str, locked: dict, inv_pkgs: dict, res: Result) -> None:
    """Offline: the inventory must agree with what the lockfile itself records. npm: package-lock's own `license`
    field must equal the inventory licence. Every ecosystem: the artefact the inventory read its licence from must be
    one the lock pins (sha256 among the lock's hashes / the Cargo.lock checksum / the npm integrity), so an inventory
    row cannot describe a different file than the one that is installed."""
    for key, pkg in sorted(inv_pkgs.items()):
        lk = locked.get(key)
        if lk is None:
            continue
        who = f"{cid}: {pkg['name']}=={key[1]}"
        if kind == "npm":
            lock_lic = lk.get("license")
            if not isinstance(lock_lic, str) or not lock_lic.strip():
                res.violations.append(f"{who}: package-lock.json has no license field — UNKNOWN licence")
            elif lock_lic != pkg.get("licence"):
                res.violations.append(f"{who}: inventory licence {pkg.get('licence')!r} differs from package-lock.json "
                                      f"license {lock_lic!r} (the lock is the source of truth; regenerate the inventory)")
            if pkg.get("artefact_integrity") not in lk["hashes"]:
                res.violations.append(f"{who}: inventory artefact integrity is not the lock's integrity")
        elif kind in ("requirements", "uv", "cargo"):
            if lk.get("git"):
                continue                         # git-sourced: bound by commit in the lock (deer-flow; sha256 of LICENSE)
            if pkg.get("artefact_sha256") not in lk["hashes"]:
                res.violations.append(f"{who}: inventory artefact sha256 {pkg.get('artefact_sha256')!r} is not one the "
                                      "lock pins (regenerate the inventory from the hash-verified artefacts)")


def verify_sources(cid: str, comp: dict, inv_pkgs: dict, cache: Path, res: Result) -> None:
    """--verify-sources: re-derive every declared licence from the source of truth — the hash-verified artefact
    (wheel/sdist METADATA License-Expression / License / classifiers; the .crate's Cargo.toml `license`, which is
    what `cargo metadata` reports; the npm tarball's OWN package.json `license`; restic's Go licence texts at their
    pinned commit + sha256) — and fail on any difference from the inventory. For npm the tarball value must also
    equal package-lock's `license` (both directions: a lock+inventory edited together still differ from the
    tarball)."""
    import third_party_notices as T
    try:
        fresh = T.collect_component(comp["kind"], comp.get("lockfile"), cache)
    except Exception as exc:                              # noqa: BLE001 — any failure to verify is a violation
        res.violations.append(f"{cid}: cannot verify sources: {exc}")
        return
    key = (lambda r: (r["name"], r["version"])) if comp["kind"] in ("npm", "cargo", "restic") else \
        (lambda r: (L.pep503(r["name"]), r["version"]))
    fresh_map = {key(r): r for r in fresh}
    for k, pkg in sorted(inv_pkgs.items()):
        r = fresh_map.get(k)
        if r is None:
            res.violations.append(f"{cid}: {pkg['name']}=={k[1]}: not produced from the lock's artefacts")
            continue
        if r["licence"] != pkg.get("licence"):
            res.violations.append(f"{cid}: {pkg['name']}=={k[1]}: inventory licence {pkg.get('licence')!r} differs from "
                                  f"the hash-verified artefact's declared licence {r['licence']!r}")
        if comp["kind"] == "npm" and r["licence"] != r.get("lock_licence"):
            res.violations.append(f"{cid}: {pkg['name']}=={k[1]}: package-lock.json license {r.get('lock_licence')!r} "
                                  f"differs from the hash-verified tarball's package.json license {r['licence']!r}")


def run(root: Path = L.REPO, policy_path: Path = DEFAULT_POLICY, exceptions_path: Path = DEFAULT_EXCEPTIONS,
        inventory_path: Path = DEFAULT_INVENTORY, verify_cache: Path | None = None) -> Result:
    policy = L.load_json(policy_path)
    exceptions = L.load_json(exceptions_path)["exceptions"]
    inventory = L.load_json(inventory_path)["components"]
    allowed = set(policy["allowed"])
    allowed_exc = set(policy.get("allowed_with_exceptions", []))
    flagged = set(policy["flagged"])
    forbidden_ids = set(policy.get("forbidden_ids", []))
    comps = {k: v for k, v in policy["components"].items() if not k.startswith("_")}
    res = Result()

    by_lock = {c["lockfile"]: cid for cid, c in comps.items() if c.get("lockfile")}
    for lf in L.discover_lockfiles(root):
        if lf.path not in by_lock:
            res.violations.append(f"unreviewed lockfile {lf.path} ({lf.kind}): add it to devtools/licences/policy.json "
                                  "components and regenerate the inventory")

    exc_index: dict = {}
    for e in exceptions:
        for key in ("component", "name", "version", "status", "reason"):
            if not e.get(key):
                res.violations.append(f"exceptions.json entry {e} lacks '{key}'")
        if e.get("status") not in ("approved-flagged", "reviewed-metadata", "pending-owner-decision", "owner-approved"):
            res.violations.append(f"exceptions.json entry {e.get('name')}: unknown status {e.get('status')!r}")
        if e.get("status") == "owner-approved":
            for key in ("approved_by", "approved_on", "attribution"):
                if not e.get(key):
                    res.violations.append(f"exceptions.json entry {e.get('name')}: owner-approved needs '{key}'")
        if e.get("status") == "reviewed-metadata" and not e.get("licence"):
            res.violations.append(f"exceptions.json entry {e.get('name')}: reviewed-metadata needs 'licence'")
        exc_index[(e.get("component"), e.get("name"), e.get("version"))] = e
    used: set = set()

    for cid, comp in comps.items():
        if comp.get("lockfile") and not (root / comp["lockfile"]).exists():
            res.violations.append(f"{cid}: lockfile {comp['lockfile']} is missing")
            continue
        locked = locked_set(root, comp, policy, res, cid)
        inv = inventory.get(cid)
        if comp["kind"] == "gomod":
            continue
        if inv is None:
            res.violations.append(f"{cid}: no inventory (run devtools/third_party_notices.py all --cache <dir>)")
            continue
        inv_pkgs = {(p["name"] if comp["kind"] in ("npm", "cargo", "restic") else L.pep503(p["name"]), p["version"]): p
                    for p in inv["packages"]}
        if comp["kind"] == "restic":
            mods = restic_modules(root, comp)
            if mods is not None and mods != set(inv_pkgs) - {("github.com/restic/restic", p["version"])
                                                             for p in inv["packages"] if p["name"] == "github.com/restic/restic"}:
                res.violations.append(f"{cid}: restic module list differs from the inventory (regenerate it)")
        else:
            for k in sorted(set(locked) - set(inv_pkgs)):
                res.violations.append(f"{cid}: {k[0]}=={k[1]} is locked but not in the inventory (regenerate it)")
            for k in sorted(set(inv_pkgs) - set(locked)):
                res.violations.append(f"{cid}: {k[0]}=={k[1]} is in the inventory but no longer locked (regenerate it)")
        res.checked[cid] = len(inv_pkgs)
        cross_check_lock(cid, comp["kind"], locked, inv_pkgs, res)
        if verify_cache is not None:
            verify_sources(cid, comp, inv_pkgs, verify_cache, res)
        for (name, ver), pkg in sorted(inv_pkgs.items()):
            entry = exc_index.get((cid, pkg["name"], ver)) or exc_index.get((cid, name, ver))
            expr = (entry or {}).get("licence") or pkg.get("licence")
            label = f"{cid}: {pkg['name']}=={ver} [{expr}]"
            try:
                ok_plain = L.spdx_satisfied(expr, allowed, allowed_exc)
                ok_flag = L.spdx_satisfied(expr, allowed | flagged, allowed_exc)
            except L.MissingLicence:
                res.violations.append(f"{cid}: {pkg['name']}=={ver}: no licence value in the inventory — UNKNOWN "
                                      "licence (regenerate the inventory, or add a reviewed-metadata exception)")
                continue
            except ValueError:
                ok_plain = ok_flag = False
            status = (entry or {}).get("status")
            if entry is not None:
                used.add((entry["component"], entry["name"], entry["version"]))
            if ok_plain:
                if status in ("approved-flagged", "pending-owner-decision", "owner-approved"):
                    res.violations.append(f"stale exception: {label} passes without its {status} entry — remove it")
                continue
            if ok_flag and status == "approved-flagged":
                res.flagged.append(f"FLAGGED {label} — {entry['reason']}")
            elif status == "owner-approved":
                # an owner approval covers a permissive licence the ruling did not name; it can never admit a
                # forbidden (copyleft / source-available) id
                try:
                    ids = set(L.spdx_ids(expr))
                    clean = L.spdx_satisfied(expr, ids - forbidden_ids, allowed_exc | set(L.spdx_with_exceptions(expr)))
                except ValueError:
                    clean = False
                if clean and all(entry.get(k) for k in ("approved_by", "approved_on", "attribution")):
                    res.approved.append(f"APPROVED {label} — {entry['approved_by']} {entry['approved_on']}; "
                                        f"attribution: {entry['attribution']}")
                elif clean:
                    pass                                  # the missing field is already a violation (entry check)
                else:
                    res.violations.append(f"{label}: owner-approved entry cannot admit a forbidden licence id")
            elif status == "pending-owner-decision":
                res.pending.append(f"PENDING OWNER DECISION {label} — {entry['reason']}")
            elif ok_flag:
                res.violations.append(f"{label}: flagged licence needs an approved-flagged entry in exceptions.json")
            else:
                res.violations.append(f"{label}: licence not allowed by the 2026-11-08 ruling (or unparseable/UNKNOWN)")

    for key in exc_index:
        if key not in used:
            res.violations.append(f"stale exception: {key[0]} {key[1]}=={key[2]} matches no locked package — remove it")
    return res


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Repository licence gate (owner ruling 2026-11-08)")
    ap.add_argument("--allow-pending", action="store_true",
                    help="report pending-owner-decision entries but exit 0 (interim CI mode until the owner rules)")
    ap.add_argument("--json", type=Path, help="also write the report as JSON to this path")
    ap.add_argument("--root", type=Path, default=L.REPO)
    ap.add_argument("--verify-sources", action="store_true",
                    help="also re-derive every licence from the hash-verified artefacts (network for uncached ones)")
    ap.add_argument("--cache", type=Path, help="artefact cache for --verify-sources (outside the repository)")
    args = ap.parse_args(argv)
    if args.verify_sources and not args.cache:
        ap.error("--verify-sources needs --cache <dir>")
    res = run(args.root, verify_cache=args.cache if args.verify_sources else None)
    for line in res.flagged + res.approved + res.pending:
        print(line)
    for line in res.violations:
        print(f"VIOLATION {line}")
    total = sum(res.checked.values())
    print(f"licence gate: {total} packages in {len(res.checked)} components; {len(res.violations)} violation(s), "
          f"{len(res.pending)} pending owner decision(s), {len(res.flagged)} flagged, {len(res.approved)} owner-approved")
    if args.json:
        args.json.write_text(json.dumps({"violations": res.violations, "pending": res.pending, "flagged": res.flagged,
                                         "approved": res.approved, "checked": res.checked}, indent=2) + "\n")
    return res.exit_code(args.allow_pending)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
