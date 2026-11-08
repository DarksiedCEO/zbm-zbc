"""Self-test of devtools/licence_gate.py and devtools/licence_lib.py (owner licence ruling 2026-11-08). Standard
library only (unittest), offline:

    python3 -B -m unittest devtools/test_licence_gate.py -v

Each gate case builds a throwaway repository tree (policy, exceptions, inventory and lockfiles) in a private temp
dir, plants one condition and runs the gate on THAT tree; the last cases run the gate on the real checkout."""
from __future__ import annotations

import json
import sys
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

    [[package]]
    name = "langgraph-api"
    version = "0.1.0"
    source = { registry = "https://pypi.org/simple" }
    dependencies = [{ name = "blockbuster" }]

    [[package]]
    name = "blockbuster"
    version = "1.0.0"
    source = { registry = "https://pypi.org/simple" }
    dependencies = [{ name = "forbiddenfruit", marker = "implementation_name == 'cpython'" }]

    [[package]]
    name = "forbiddenfruit"
    version = "0.1.4"
    source = { registry = "https://pypi.org/simple" }

    [[package]]
    name = "winonly"
    version = "2.0.0"
    source = { registry = "https://pypi.org/simple" }

    [[package]]
    name = "pytest"
    version = "9.0.0"
    source = { registry = "https://pypi.org/simple" }
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


class Tree:
    def __init__(self, tmp: Path, *, uv=UV_LOCK, npm=None, req=REQ_LOCK, gomod="module x\n\ngo 1.27\n",
                 inv=None, exceptions=None, policy=None):
        self.root = tmp
        files = {"svc/uv.lock": uv, "web/package-lock.json": json.dumps(npm or NPM_LOCK),
                 "deploy/requirements.lock.txt": req, "go/go.mod": gomod,
                 "devtools/licences/policy.json": json.dumps(policy or POLICY),
                 "devtools/licences/inventory.json": json.dumps(inv or inventory()),
                 "devtools/licences/exceptions.json": json.dumps({"exceptions": exceptions if exceptions is not None
                                                                   else [CERTIFI_OK]})}
        for rel, text in files.items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)

    def run(self) -> G.Result:
        d = self.root / "devtools" / "licences"
        return G.run(self.root, d / "policy.json", d / "exceptions.json", d / "inventory.json")


class GateCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def assertViolation(self, res: G.Result, needle: str):
        self.assertTrue(any(needle in v for v in res.violations), f"{needle!r} not in {res.violations}")
        self.assertEqual(res.exit_code(allow_pending=True), 1)

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
