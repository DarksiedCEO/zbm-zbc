"""Self-test of devtools/licence_gate.py and devtools/licence_lib.py (owner licence ruling 2026-11-08). Standard
library only (unittest), offline:

    python3 -B -m unittest devtools/test_licence_gate.py -v

Each gate case builds a throwaway repository tree (policy, exceptions, inventory and lockfiles) in a private temp
dir, plants one condition and runs the gate on THAT tree; the last cases run the gate on the real checkout."""
from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import licence_gate as G  # noqa: E402
import licence_lib as L  # noqa: E402

POLICY = {
    "allowed": ["MIT", "Apache-2.0", "BSD-3-Clause"],
    "allowed_with_exceptions": ["LLVM-exception"],
    "flagged": ["MPL-2.0"],
    "forbidden_packages": {"python": ["langgraph-api", "forbiddenfruit"]},
    "components": {
        "py": {"lockfile": "svc/uv.lock", "kind": "uv"},
        "web": {"lockfile": "web/package-lock.json", "kind": "npm"},
        "req": {"lockfile": "deploy/requirements.lock.txt", "kind": "requirements"},
        "go": {"lockfile": "go/go.mod", "kind": "gomod", "max_requires": 0},
    },
}

UV_LOCK = textwrap.dedent('''\
    version = 1
    requires-python = ">=3.12"

    [[package]]
    name = "app"
    version = "0.1.0"
    source = { virtual = "." }
    dependencies = [
        { name = "fastapi" },
        { name = "langgraph-api", marker = "sys_platform == 'never'" },
        { name = "winonly", marker = "sys_platform == 'win32'" },
    ]

    [package.dev-dependencies]
    dev = [{ name = "pytest" }]

    [[package]]
    name = "fastapi"
    version = "1.0.0"
    source = { registry = "https://pypi.org/simple" }
    wheels = [{ url = "https://files.example.invalid/fastapi-1.0.0-py3-none-any.whl", hash = "sha256:6aee0e678a168eeae8df707e083a158fc23515bdd52eed77ec65b669baa82917" }]

    [[package]]
    name = "langgraph-api"
    version = "0.1.0"
    source = { registry = "https://pypi.org/simple" }
    wheels = [{ url = "https://files.example.invalid/langgraph-api-0.1.0-py3-none-any.whl", hash = "sha256:c844a18af4d15a3aaf0bb9a6071009e6bfbd1dc8d2874d0ac512ec96128c4156" }]
    dependencies = [{ name = "blockbuster" }]

    [[package]]
    name = "blockbuster"
    version = "1.0.0"
    source = { registry = "https://pypi.org/simple" }
    wheels = [{ url = "https://files.example.invalid/blockbuster-1.0.0-py3-none-any.whl", hash = "sha256:4d9137f0e5d29c1ab0b4a42524cfa9ad6d3f1569c65ad432f48b31174b1a9537" }]
    dependencies = [{ name = "forbiddenfruit", marker = "implementation_name == 'cpython'" }]

    [[package]]
    name = "forbiddenfruit"
    version = "0.1.4"
    source = { registry = "https://pypi.org/simple" }
    wheels = [{ url = "https://files.example.invalid/forbiddenfruit-0.1.4-py3-none-any.whl", hash = "sha256:67f611dfca84ab1ee6d2304f35c6d3b3b2f5b0ac65b4254efc7017082036f8d7" }]

    [[package]]
    name = "winonly"
    version = "2.0.0"
    source = { registry = "https://pypi.org/simple" }
    wheels = [{ url = "https://files.example.invalid/winonly-2.0.0-py3-none-any.whl", hash = "sha256:0f48151c63af2de83c316244ea783ad3556448f57cb57b67356fdcd92b6bfb99" }]

    [[package]]
    name = "pytest"
    version = "9.0.0"
    source = { registry = "https://pypi.org/simple" }
    wheels = [{ url = "https://files.example.invalid/pytest-9.0.0-py3-none-any.whl", hash = "sha256:2b01214d32c382dbc73bf6b493bedb7926324af27bad3ee888f66774b9b114c8" }]
    ''')

NPM_LOCK = {
    "lockfileVersion": 3,
    "packages": {
        "": {"name": "web"},
        "node_modules/next": {"version": "16.0.0", "license": "MIT", "integrity": "sha512-x"},
        "node_modules/sharp": {"resolved": "stubs/sharp", "link": True},
        "stubs/sharp": {"version": "0.0.0-stub", "license": "MIT"},
    },
}

REQ_LOCK = "certifi==2026.10.19 \\\n    --hash=sha256:" + "a" * 64 + "\n"


def inventory(**overrides) -> dict:
    comps = {
        "py": {"packages": [{"name": "fastapi", "version": "1.0.0", "licence": "MIT"},
                            {"name": "winonly", "version": "2.0.0", "licence": "BSD-3-Clause"},
                            {"name": "pytest", "version": "9.0.0", "licence": "MIT"}]},
        "web": {"packages": [{"name": "next", "version": "16.0.0", "licence": "MIT"}]},
        "req": {"packages": [{"name": "certifi", "version": "2026.10.19", "licence": "MPL-2.0"}]},
    }
    comps.update(overrides)
    return {"components": comps}


CERTIFI_OK = {"component": "req", "name": "certifi", "version": "2026.10.19", "status": "approved-flagged",
              "reason": "MPL-2.0 CA bundle, unmodified"}


def _bind_artefacts(inv: dict, npm: dict) -> dict:
    """Give each inventory row the artefact hash its lock pins (what third_party_notices.py records), unless the test
    set one itself: uv rows the wheel hash in UV_LOCK, requirements rows the lock hash, npm rows the lock integrity."""
    inv = json.loads(json.dumps(inv))
    for cid, comp in inv["components"].items():
        for p in comp["packages"]:
            if cid == "py":
                p.setdefault("artefact_sha256", hashlib.sha256(p["name"].encode()).hexdigest())
            elif cid == "req":
                p.setdefault("artefact_sha256", ("a" if p["name"] == "certifi" else "b") * 64)
            elif cid == "web":
                p.setdefault("artefact_integrity", npm["packages"].get(f"node_modules/{p['name']}", {}).get("integrity"))
    return inv


class Tree:
    def __init__(self, tmp: Path, *, uv=UV_LOCK, npm=None, req=REQ_LOCK, gomod="module x\n\ngo 1.27\n",
                 inv=None, exceptions=None, policy=None):
        self.root = tmp
        inv = inv or inventory()
        if npm is None:
            # a licence-evaluation case changes the package's licence: the lock (the source of truth) says the same
            npm = json.loads(json.dumps(NPM_LOCK))
            for p in inv["components"].get("web", {}).get("packages", []):
                entry = npm["packages"].get(f"node_modules/{p['name']}")
                if entry is not None and p.get("licence"):
                    entry["license"] = p["licence"]
        inv = _bind_artefacts(inv, npm)
        files = {"svc/uv.lock": uv, "web/package-lock.json": json.dumps(npm),
                 "deploy/requirements.lock.txt": req, "go/go.mod": gomod,
                 "devtools/licences/policy.json": json.dumps(policy or POLICY),
                 "devtools/licences/inventory.json": json.dumps(inv or inventory()),
                 "devtools/licences/exceptions.json": json.dumps({"exceptions": exceptions if exceptions is not None
                                                                   else [CERTIFI_OK]})}
        for rel, text in files.items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)

    def run(self, verify_cache=None) -> G.Result:
        d = self.root / "devtools" / "licences"
        return G.run(self.root, d / "policy.json", d / "exceptions.json", d / "inventory.json", verify_cache=verify_cache)


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def assertViolation(self, res: G.Result, needle: str):
        self.assertTrue(any(needle in v for v in res.violations), f"{needle!r} not in {res.violations}")
        self.assertEqual(res.exit_code(allow_pending=True), 1)


class GateCase(_TmpCase):
    # --- clean baseline -------------------------------------------------------------------------------------
    def test_clean_tree_passes_with_mpl_flagged(self):
        res = Tree(self.tmp).run()
        self.assertEqual(res.violations, [])
        self.assertEqual(len(res.flagged), 1)
        self.assertIn("certifi", res.flagged[0])
        self.assertEqual(res.exit_code(allow_pending=False), 0)

    # --- licences -------------------------------------------------------------------------------------------
    def test_lgpl_package_in_npm_lock_fails(self):
        npm = json.loads(json.dumps(NPM_LOCK))
        npm["packages"]["node_modules/@img/sharp-libvips-linux-x64"] = {
            "version": "1.3.4", "license": "LGPL-3.0-or-later", "integrity": "sha512-y", "optional": True}
        inv = inventory(web={"packages": [{"name": "next", "version": "16.0.0", "licence": "MIT"},
                                          {"name": "@img/sharp-libvips-linux-x64", "version": "1.3.4",
                                           "licence": "LGPL-3.0-or-later"}]})
        self.assertViolation(Tree(self.tmp, npm=npm, inv=inv).run(), "licence not allowed")

    def test_mpl_without_exception_fails(self):
        self.assertViolation(Tree(self.tmp, exceptions=[]).run(), "flagged licence needs an approved-flagged entry")

    def test_unknown_licence_fails(self):
        inv = inventory(web={"packages": [{"name": "next", "version": "16.0.0", "licence": "UNKNOWN"}]})
        self.assertViolation(Tree(self.tmp, inv=inv).run(), "licence not allowed")

    def test_or_expression_with_one_allowed_branch_passes(self):
        inv = inventory(web={"packages": [{"name": "next", "version": "16.0.0", "licence": "GPL-2.0-only OR MIT"}]})
        self.assertEqual(Tree(self.tmp, inv=inv).run().violations, [])

    def test_and_expression_with_one_forbidden_part_fails(self):
        inv = inventory(web={"packages": [{"name": "next", "version": "16.0.0", "licence": "MIT AND GPL-3.0-only"}]})
        self.assertViolation(Tree(self.tmp, inv=inv).run(), "licence not allowed")

    def test_pending_owner_decision_is_reported_not_passed(self):
        inv = inventory(web={"packages": [{"name": "next", "version": "16.0.0", "licence": "CC-BY-4.0"}]})
        exc = [CERTIFI_OK, {"component": "web", "name": "next", "version": "16.0.0",
                            "status": "pending-owner-decision", "reason": "data licence"}]
        res = Tree(self.tmp, inv=inv, exceptions=exc).run()
        self.assertEqual(res.violations, [])
        self.assertEqual(len(res.pending), 1)
        self.assertIn("PENDING OWNER DECISION", res.pending[0])
        self.assertEqual(res.exit_code(allow_pending=False), 2)
        self.assertEqual(res.exit_code(allow_pending=True), 0)

    def test_reviewed_metadata_entry_supplies_the_licence(self):
        inv = inventory(web={"packages": [{"name": "next", "version": "16.0.0", "licence": "UNKNOWN"}]})
        exc = [CERTIFI_OK, {"component": "web", "name": "next", "version": "16.0.0", "status": "reviewed-metadata",
                            "licence": "MIT", "reason": "LICENSE file is MIT"}]
        self.assertEqual(Tree(self.tmp, inv=inv, exceptions=exc).run().violations, [])

    def test_exception_for_other_version_does_not_apply_and_is_stale(self):
        exc = [dict(CERTIFI_OK, version="2025.1.1")]
        res = Tree(self.tmp, exceptions=exc).run()
        self.assertViolation(res, "flagged licence needs an approved-flagged entry")
        self.assertViolation(res, "stale exception")

    def test_unneeded_exception_is_stale(self):
        exc = [CERTIFI_OK, {"component": "web", "name": "next", "version": "16.0.0", "status": "approved-flagged",
                            "reason": "not needed"}]
        self.assertViolation(Tree(self.tmp, exceptions=exc).run(), "passes without its approved-flagged entry")

    def test_exception_without_reason_fails(self):
        self.assertViolation(Tree(self.tmp, exceptions=[dict(CERTIFI_OK, reason="")]).run(), "lacks 'reason'")

    # --- inventory drift / coverage -------------------------------------------------------------------------
    def test_locked_package_missing_from_inventory_fails(self):
        inv = inventory(py={"packages": [{"name": "fastapi", "version": "1.0.0", "licence": "MIT"},
                                         {"name": "pytest", "version": "9.0.0", "licence": "MIT"}]})
        self.assertViolation(Tree(self.tmp, inv=inv).run(), "winonly==2.0.0 is locked but not in the inventory")

    def test_inventory_entry_no_longer_locked_fails(self):
        inv = inventory(web={"packages": [{"name": "next", "version": "16.0.0", "licence": "MIT"},
                                          {"name": "gone", "version": "1.0.0", "licence": "MIT"}]})
        self.assertViolation(Tree(self.tmp, inv=inv).run(), "gone==1.0.0 is in the inventory but no longer locked")

    def test_unreviewed_lockfile_fails(self):
        (self.tmp / "newsvc").mkdir()
        (self.tmp / "newsvc" / "Cargo.lock").write_text("version = 4\n")
        tree = Tree(self.tmp)
        self.assertViolation(tree.run(), "unreviewed lockfile newsvc/Cargo.lock")

    def test_go_mod_require_needs_review(self):
        gomod = "module x\n\ngo 1.27\n\nrequire github.com/pkg/errors v0.9.1\n"
        self.assertViolation(Tree(self.tmp, gomod=gomod).run(), "reviewed maximum is 0")

    def test_unhashed_requirements_lock_is_rejected(self):
        with self.assertRaises(ValueError):
            Tree(self.tmp, req="certifi==2026.10.19\n").run()

    # --- forbidden packages ---------------------------------------------------------------------------------
    def test_forbidden_package_behind_never_marker_is_not_installable(self):
        res = Tree(self.tmp).run()
        self.assertFalse(any("forbidden" in v for v in res.violations), res.violations)

    def test_forbidden_package_reachable_on_a_real_platform_fails(self):
        uv = UV_LOCK.replace("{ name = \"langgraph-api\", marker = \"sys_platform == 'never'\" }",
                             "{ name = \"langgraph-api\", marker = \"sys_platform == 'linux'\" }")
        inv = inventory(py={"packages": [{"name": n, "version": v, "licence": "MIT"} for n, v in
                                         (("fastapi", "1.0.0"), ("winonly", "2.0.0"), ("pytest", "9.0.0"),
                                          ("langgraph-api", "0.1.0"), ("blockbuster", "1.0.0"),
                                          ("forbiddenfruit", "0.1.4"))]})
        res = Tree(self.tmp, uv=uv, inv=inv).run()
        self.assertViolation(res, "forbidden package langgraph-api==0.1.0 is installable")
        self.assertViolation(res, "forbidden package forbiddenfruit==0.1.4 is installable from svc/uv.lock on linux")

    def test_forbidden_package_pinned_in_requirements_lock_fails(self):
        req = REQ_LOCK + "forbiddenfruit==0.1.4 \\\n    --hash=sha256:" + "b" * 64 + "\n"
        inv = inventory(req={"packages": [{"name": "certifi", "version": "2026.10.19", "licence": "MPL-2.0"},
                                          {"name": "forbiddenfruit", "version": "0.1.4", "licence": "MIT"}]})
        self.assertViolation(Tree(self.tmp, req=req, inv=inv).run(), "forbidden package forbiddenfruit==0.1.4 is pinned")


class SourceOfTruthCase(_TmpCase):
    """Review M1 (PR #48): the gate trusted inventory.json for licences. The inventory is now cross-checked against
    the lockfile itself (offline) and, with --verify-sources, against the hash-verified artefacts."""

    def test_reviewer_injection_gpl_npm_package_recorded_as_mit_fails(self):
        # the reviewer's injection: the lock says GPL, the inventory was edited to say MIT
        npm = json.loads(json.dumps(NPM_LOCK))
        npm["packages"]["node_modules/next"]["license"] = "GPL-3.0-only"
        self.assertViolation(Tree(self.tmp, npm=npm).run(),
                             "inventory licence 'MIT' differs from package-lock.json license 'GPL-3.0-only'")

    def test_real_inventory_tampered_to_mit_fails(self):
        # same injection on the committed tree: copy the real files, relabel caniuse-lite (CC-BY-4.0) as MIT
        real = L.load_json(L.REPO / "devtools/licences/inventory.json")
        for p in real["components"]["npm-dashboard"]["packages"]:
            if p["name"] == "caniuse-lite":
                p["licence"] = "MIT"
        tampered = self.tmp / "inventory.json"
        tampered.write_text(json.dumps(real))
        res = G.run(L.REPO, G.DEFAULT_POLICY, G.DEFAULT_EXCEPTIONS, tampered)
        self.assertTrue(any("caniuse-lite" in v and "differs from package-lock.json license 'CC-BY-4.0'" in v
                            for v in res.violations), res.violations)

    def test_npm_lock_without_license_is_unknown(self):
        npm = json.loads(json.dumps(NPM_LOCK))
        del npm["packages"]["node_modules/next"]["license"]
        self.assertViolation(Tree(self.tmp, npm=npm).run(), "package-lock.json has no license field — UNKNOWN")

    def test_inventory_row_for_a_different_artefact_fails(self):
        inv = inventory(py={"packages": [{"name": "fastapi", "version": "1.0.0", "licence": "MIT",
                                          "artefact_sha256": "c" * 64},
                                         {"name": "winonly", "version": "2.0.0", "licence": "BSD-3-Clause"},
                                         {"name": "pytest", "version": "9.0.0", "licence": "MIT"}]})
        self.assertViolation(Tree(self.tmp, inv=inv).run(), "fastapi==1.0.0: inventory artefact sha256")

    def test_missing_licence_value_is_an_explicit_unknown_violation(self):
        # review L2: a row with no licence used to crash with AttributeError
        inv = inventory(web={"packages": [{"name": "next", "version": "16.0.0"}]})
        npm = json.loads(json.dumps(NPM_LOCK))
        res = Tree(self.tmp, inv=inv, npm=npm).run()
        self.assertViolation(res, "next==16.0.0: no licence value in the inventory — UNKNOWN licence")

    def test_verify_sources_catches_gpl_wheel_recorded_as_mit(self):
        # the injection on the Python side: the wheel the lock pins declares GPL-3.0-only in its METADATA
        import third_party_notices as T
        cache = self.tmp / "cache"
        declared = {("fastapi", "1.0.0"): "GPL-3.0-only", ("winonly", "2.0.0"): "BSD-3-Clause",
                    ("pytest", "9.0.0"): "MIT"}
        uv, rows = UV_LOCK, []
        for (name, ver), lic in declared.items():
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr(f"{name}-{ver}.dist-info/METADATA",
                            f"Metadata-Version: 2.4\nName: {name}\nVersion: {ver}\nLicense-Expression: {lic}\n")
            wheel = buf.getvalue()
            h = hashlib.sha256(wheel).hexdigest()
            url = f"https://files.example.invalid/{name}-{ver}-py3-none-any.whl"
            art = cache / "artefacts" / (hashlib.sha256(url.encode()).hexdigest()[:16] + f"-{name}-{ver}-py3-none-any.whl")
            art.parent.mkdir(parents=True, exist_ok=True)
            art.write_bytes(wheel)
            uv = uv.replace(f'hash = "sha256:{hashlib.sha256(name.encode()).hexdigest()}"', f'hash = "sha256:{h}"')
            # the injection: the inventory says MIT for the GPL wheel
            rows.append({"name": name, "version": ver, "licence": "MIT" if name == "fastapi" else lic,
                         "artefact_sha256": h})
        inv = inventory(py={"packages": rows})
        tree = Tree(self.tmp / "repo", uv=uv, inv=inv)
        offline = tree.run()
        self.assertEqual(offline.violations, [], "offline the lock alone cannot reveal a Python licence")
        saved = T.REPO
        T.REPO = tree.root
        try:
            res = G.Result()
            G.verify_sources("py", {"lockfile": "svc/uv.lock", "kind": "uv"},
                             {(r["name"], r["version"]): r for r in rows}, cache, res)
        finally:
            T.REPO = saved
        self.assertEqual(res.violations, ["py: fastapi==1.0.0: inventory licence 'MIT' differs from the hash-verified "
                                          "artefact's declared licence 'GPL-3.0-only'"])

    def test_artefact_with_wrong_hash_is_refused(self):
        import third_party_notices as T
        cache = self.tmp / "cache"
        url = "https://files.example.invalid/x-1.0-py3-none-any.whl"
        art = cache / "artefacts" / (hashlib.sha256(url.encode()).hexdigest()[:16] + "-x-1.0-py3-none-any.whl")
        art.parent.mkdir(parents=True)
        art.write_bytes(b"tampered")
        with self.assertRaises(RuntimeError):
            T.fetch_verified(url, cache, sha256="0" * 64)

    def test_go_licence_text_must_match_its_pinned_sha256(self):
        # review L1: restic's Go licence texts are fetched at a pinned commit and checked against a recorded sha256
        import third_party_notices as T
        cache = self.tmp / "cache"
        pin = {"repo": "o/r", "commit": "1" * 40, "file": "LICENSE", "sha256": "0" * 64}
        url = f"https://raw.githubusercontent.com/o/r/{'1' * 40}/LICENSE"
        art = cache / "artefacts" / (hashlib.sha256(url.encode()).hexdigest()[:16] + "-LICENSE")
        art.parent.mkdir(parents=True)
        art.write_bytes(b"MIT License\n\nPermission is hereby granted, free of charge")
        with self.assertRaises(RuntimeError):
            T.pinned_licence_text("m@v1", "o/r", ["v1"], cache, {"m@v1": pin}, pin_new=False)
        with self.assertRaisesRegex(RuntimeError, "no licence pin"):
            T.pinned_licence_text("other@v1", "o/r", ["v1"], cache, {}, pin_new=False)

    def test_crate_licence_comes_from_its_own_manifest(self):
        import third_party_notices as T
        self.assertEqual(T.crate_declared('[package]\nname = "a"\nlicense = "MIT/Apache-2.0"\n', {})[0],
                         "MIT OR Apache-2.0")
        self.assertEqual(T.crate_declared('[package]\nname = "a"\n', {})[0], "UNKNOWN")


class LibCase(unittest.TestCase):
    def test_markers(self):
        linux = L._env("linux", "x86_64", "3.12.4")
        self.assertFalse(L.eval_marker("sys_platform == 'never'", linux))
        self.assertTrue(L.eval_marker("sys_platform != 'win32' and implementation_name == 'cpython'", linux))
        self.assertTrue(L.eval_marker("python_full_version < '3.13'", linux))
        self.assertFalse(L.eval_marker("python_full_version >= '3.14' or sys_platform == 'win32'", linux))
        self.assertTrue(L.eval_marker("(sys_platform == 'win32' or sys_platform == 'linux') and python_version == '3.12'", linux))
        self.assertTrue(L.eval_marker("python_full_version == '3.12.*'", linux))
        self.assertFalse(L.eval_marker("python_full_version == '3.13.*'", linux))
        self.assertTrue(L.eval_marker("platform_machine == 'x86_64' or sys_platform == 'never' and extra == 'x'", linux))
        with self.assertRaises(ValueError):
            L.eval_marker("no_such_var == '1'", linux)

    def test_spdx(self):
        allowed = {"MIT", "Apache-2.0"}
        self.assertTrue(L.spdx_satisfied("MIT/Apache-2.0", allowed))
        self.assertTrue(L.spdx_satisfied("(MIT OR GPL-2.0-only) AND Apache-2.0", allowed))
        self.assertFalse(L.spdx_satisfied("MIT AND Unicode-3.0", allowed))
        self.assertFalse(L.spdx_satisfied("Apache-2.0 WITH LLVM-exception", allowed))
        self.assertTrue(L.spdx_satisfied("Apache-2.0 WITH LLVM-exception", allowed, {"LLVM-exception"}))
        self.assertEqual(L.spdx_ids("(MIT OR Apache-2.0) AND Unicode-3.0"), ["MIT", "Apache-2.0", "Unicode-3.0"])
        for missing in (None, "", "   "):
            with self.assertRaises(L.MissingLicence):
                L.spdx_satisfied(missing, allowed)
        for bad in ("MIT AND", "(MIT", "MIT OR OR Apache-2.0", ""):
            with self.assertRaises(ValueError):
                L.spdx_parse(bad)

    def test_requirements_lock_needs_exact_pins(self):
        with self.assertRaises(ValueError):
            L.parse_requirements_lock("fastapi>=1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n")


class RealRepositoryCase(unittest.TestCase):
    """The committed tree: no violations, and the pending set is exactly the reviewed list (a new pending entry or a
    resolved one must update this test, so the owner's open decisions stay visible)."""

    def test_repository_gate(self):
        res = G.run()
        self.assertEqual(res.violations, [])
        pending = sorted(p.split(" [", 1)[0].replace("PENDING OWNER DECISION ", "") for p in res.pending)
        self.assertEqual(pending, ["npm-dashboard: caniuse-lite==1.0.30001810", "python-delivery: pillow==13.0.0",
                                   "python-delivery: pypdfium2==5.13.0", "python-delivery: regex==2026.9.10",
                                   "rust-ledger: unicode-ident==1.0.26"])

    def test_dashboard_lock_has_no_sharp_or_lgpl(self):
        lock = L.parse_package_lock((L.REPO / "apps/dashboard-ts/package-lock.json").read_text())
        registry = [p for p in lock if p.source == "registry"]
        self.assertFalse([p.name for p in registry if p.name == "sharp" or p.name.startswith("@img/")])
        self.assertFalse([p.name for p in lock if "GPL" in (p.extra.get("license") or "")])
        stub = [p for p in lock if p.source == "path" and p.name == "sharp"]
        self.assertEqual(len(stub), 1, "sharp must resolve to the local stub (apps/dashboard-ts/stubs/sharp)")


if __name__ == "__main__":
    unittest.main()
