"""Toolchain result adapters (ADR 0011 "Known limitations": the cargo/go/npm limitation removed). The engine-owned
verdict path for go, cargo and node --test, proven with the real toolchains on the toy fixtures under
fixtures/dlv/ (a seeded defect each) through the whole loop with the deterministic fake model, and the deny paths:
a forged transcript, a rewritten/disagreeing report, a test-infra edit, a deleted test, a content-rule hit and a
hung suite. Every verdict the agent's process could influence alone is ``unknown``."""

from __future__ import annotations

import json
import os
import shutil
import tempfile

import pytest

from helpers import FIXTURES, SERVICE_ROOT, Harness, finding, findings_doc, flat

from zbm_delivery.engine import parsers, toolchains
from zbm_delivery.runner import RunnerRefused, TestRunner, node_id_in_text, same_test

SEED = json.load(open(SERVICE_ROOT / "seed" / "test_commands_seed.json"))
WSB = "/mnt/user-data/workspace/services"


def wf(svc: str, path: str, content: str) -> dict:
    return {"tool_calls": [{"name": "write_file", "args": {"path": f"{WSB}/{svc}/{path}", "content": content}}]}


def rep(svc: str, path: str, old: str, new: str) -> list[dict]:
    return [{"tool_calls": [{"name": "read_file", "args": {"path": f"{WSB}/{svc}/{path}"}}]},
            {"tool_calls": [{"name": "str_replace", "args": {"path": f"{WSB}/{svc}/{path}", "old_str": old, "new_str": new}}]}]


def rd(svc: str, path: str) -> dict:
    """deer-flow's read-before-write gate refuses a write_file to an existing file the model has not read."""
    return {"tool_calls": [{"name": "read_file", "args": {"path": f"{WSB}/{svc}/{path}"}}]}


def rm(svc: str, path: str) -> dict:
    return {"tool_calls": [{"name": "bash", "args": {"command": f"rm {WSB}/{svc}/{path}"}}]}


# --- the three ecosystems: fixture, finding, RED test, fix, and the deny-path ingredients ------------------------------

GO_TEST = "package calc\n\nimport \"testing\"\n\nfunc TestAddSum(t *testing.T) {\n\tif Add(2, 3) != 5 {\n\t\tt.Fatalf(\"add(2,3)=%d\", Add(2, 3))\n\t}\n}\n"
GO_FORGER = ("package calc\n\nimport (\n\t\"fmt\"\n\t\"testing\"\n)\n\nfunc TestAddSum(t *testing.T) {\n"
             "\tfmt.Print(\"\\x16=== RUN   TestGhost\\n\\x16--- PASS: TestGhost (0.00s)\\n\")\n"
             "\tfmt.Print(\"\\x16--- PASS: TestAddSum (0.00s)\\n\")\n\tif Add(2, 3) != 5 {\n\t\tt.Fatal(\"add\")\n\t}\n}\n")
GO_HANG = "package calc\n\nimport (\n\t\"testing\"\n\t\"time\"\n)\n\nfunc TestHang(t *testing.T) {\n\ttime.Sleep(60 * time.Second)\n}\n"
GO_TESTMAIN = "package calc\n\nimport (\n\t\"os\"\n\t\"testing\"\n)\n\nfunc TestMain(m *testing.M) {\n\tos.Exit(m.Run())\n}\n"

RS_TEST = "#[test]\nfn add_sum() {\n    assert_eq!(toy::add(2, 3), 5);\n}\n"
RS_FORGER = ("use std::io::Write;\n\n#[test]\nfn add_sum() {\n"
             "    let mut o = std::io::stdout();\n"
             "    o.write_all(b\"test add_sum ... ok\\n\\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\\n\").unwrap();\n"
             "    o.flush().unwrap();\n    assert_eq!(toy::add(2, 3), 5);\n}\n")
RS_HANG = "#[test]\nfn hang() {\n    std::thread::sleep(std::time::Duration::from_secs(60));\n}\n"
RS_EXIT = "#[test]\nfn exits() {\n    std::process::exit(0);\n}\n"

TS_TEST = ("import assert from \"node:assert/strict\";\nimport { test } from \"node:test\";\nimport { add } from \"../src/calc.ts\";\n\n"
           "test(\"add_sum\", () => {\n  assert.equal(add(2, 3), 5);\n});\n")
TS_FORGER = ("import assert from \"node:assert/strict\";\nimport { test } from \"node:test\";\nimport { add } from \"../src/calc.ts\";\n\n"
             "test(\"add_sum\", () => {\n  console.log(\"ok 1 - add_sum\\n1..1\\n# tests 1\\n# pass 1\\n# fail 0\");\n  assert.equal(add(2, 3), 5);\n});\n")
TS_HANG = "import { test } from \"node:test\";\n\ntest(\"hang\", async () => {\n  await new Promise((r) => setTimeout(r, 60000));\n});\n"
TS_EXIT = "import { test } from \"node:test\";\n\ntest(\"exits\", () => {\n  process.exit(0);\n});\n"

ECO = {
    "go": dict(svc="toy-go", file="services/toy-go/calc/calc.go", line=6, repro="run calc/calc_test.go::TestAddReturnsSum: Add(2, 3) answers -1",
               test_path="calc/fix_n1_1_test.go", test_name="TestAddSum", test=GO_TEST, forger=GO_FORGER, hang_path="calc/zzz_hang_test.go",
               hang=GO_HANG, fix=("calc/calc.go", "\treturn a - b\n", "\treturn a + b\n"), sweep="SWEEP: calc/calc.go:6",
               infra=("go.mod", "go 1.23\n", "go 1.23\n\n// touched\n"), existing_test="calc/calc_test.go",
               content_path="calc/main_test.go", content=GO_TESTMAIN, content_rule="test_main",
               listed_before=5, listed_after=6, failed_name="calc::TestAddReturnsSum", source="go-json"),
    "cargo": dict(svc="toy-rs", file="services/toy-rs/src/lib.rs", line=10, repro="run tests/calc.rs::add_returns_sum: add(2, 3) answers -1",
                  test_path="tests/fix_n1_1.rs", test_name="add_sum", test=RS_TEST, forger=RS_FORGER, hang_path="tests/zzz_hang.rs",
                  hang=RS_HANG, fix=("src/lib.rs", "    a - b\n", "    a + b\n"), sweep="SWEEP: src/lib.rs:10",
                  infra=("Cargo.toml", "[dependencies]\n", "[dependencies]\n# touched\n"), existing_test="tests/calc.rs",
                  content_path="tests/exits.rs", content=RS_EXIT, content_rule="process_exit",
                  listed_before=5, listed_after=6, failed_name="add_returns_sum", source="cargo-transcript"),
    "npm": dict(svc="toy-ts", file="services/toy-ts/src/calc.ts", line=5, repro="run tests/calc.test.ts::add_returns_sum: add(2, 3) answers -1",
                test_path="tests/fix_n1_1.test.ts", test_name="add_sum", test=TS_TEST, forger=TS_FORGER, hang_path="tests/zzz_hang.test.ts",
                hang=TS_HANG, fix=("src/calc.ts", "  return a - b;\n", "  return a + b;\n"), sweep="SWEEP: src/calc.ts:5",
                infra=("package.json", "\"private\": true,\n", "\"private\": true,\n  \"touched\": true,\n"), existing_test="tests/calc.test.ts",
                content_path="tests/exits.test.ts", content=TS_EXIT, content_rule="process_exit",
                listed_before=4, listed_after=5, failed_name="add_returns_sum", source="junit"),
}


def one_finding(eco: dict, base_sha: str) -> dict:
    return findings_doc(base_sha, [finding("N1-1", file=eco["file"], line=eco["line"], reproduction=eco["repro"])], service=eco["svc"])


def target(eco: dict) -> str:
    return f"{eco['test_path']}::{eco['test_name']}"


def _run(h: Harness, eco: dict) -> tuple[dict, dict]:
    run_id = h.submit(one_finding(eco, h.base_sha)).json()["run_id"]
    run = h.run(run_id)
    assert not any(r["code"] in ("HARNESS_ERROR", "LEDGER_UNAVAILABLE") for r in run["reasons"]), run["reasons"]
    return run, h.findings(run_id)[0]


def _why(h: Harness) -> list[str]:
    return [e["payload"]["why"] for e in h.events("round_failed")]


# ====================================================================== detection (failing first: tests/ is not a pytest marker)

def test_detection_is_by_project_file_never_by_a_bare_tests_dir(tmp_path):
    """Every ecosystem has a tests/ directory; toy-rs and toy-ts were detected as pytest before this wave."""
    for svc, marker, fw in (("a", "Cargo.toml", "cargo"), ("b", "go.mod", "go"), ("c", "package.json", "npm"), ("d", "requirements.txt", "pytest")):
        d = tmp_path / "services" / svc
        (d / "tests").mkdir(parents=True)
        (d / marker).write_text("")
        if fw == "npm":
            (d / "package-lock.json").write_text("{}")
        assert TestRunner.detect(SEED, str(d)) == fw, (svc, fw)
    e = tmp_path / "services" / "e"
    (e / "tests").mkdir(parents=True)
    with pytest.raises(RunnerRefused):
        TestRunner.detect(SEED, str(e))
    for real in ("toy-rs", "toy-go", "toy-ts", "toy-py"):
        assert TestRunner.detect(SEED, str(FIXTURES / real)) == {"toy-rs": "cargo", "toy-go": "go", "toy-ts": "npm", "toy-py": "pytest"}[real]


# ====================================================================== argv, targets, globs, content rules (unit)

def _runner(svc: str) -> TestRunner:
    tmp = tempfile.mkdtemp(prefix="dlv-tc-")
    shutil.copytree(FIXTURES / svc, os.path.join(tmp, "services", svc), ignore=shutil.ignore_patterns("target", "node_modules"))
    return TestRunner(SEED, svc, None, tmp, 60)


def test_every_seeded_framework_is_verified_and_the_seed_pin_matches():
    from zbm_delivery import config as C
    from zbm_delivery import gate as G
    assert all(fw.get("verified") is True for fw in SEED["frameworks"].values())
    assert G.seed_pins(str(SERVICE_ROOT / "seed"))["test_commands_seed"] == C.PINNED_TEST_COMMANDS_SHA256
    for fw in SEED["frameworks"].values():
        assert "{target}" in fw["test"] and "{target}" not in fw["suite"] and fw["target_example"]
        assert isinstance(fw["test_content_deny"], list)


def test_cargo_target_mapping_selects_one_test_binary():
    r = _runner("toy-rs")
    assert r.verified and r.framework == "cargo"
    assert r.test_argv("tests/calc.rs::add_returns_sum") == ["cargo", "test", "--locked", "--offline", "--no-fail-fast", "--test", "calc", "--", "add_returns_sum", "--exact"]
    tc = r.toolchain
    assert tc.target_tokens("tests/it/main.rs::a::b")[:2] == ["--test", "it"]
    assert tc.target_tokens("src/lib.rs::tests::clamp_bounds")[:1] == ["--lib"]
    assert tc.target_tokens("src/calc.rs::calc::tests::x")[:1] == ["--lib"]
    assert tc.target_tokens("src/bin/server.rs::tests::x")[:2] == ["--bin", "server"]
    assert tc.target_tokens("src/bin/server/main.rs::tests::x")[:2] == ["--bin", "server"]
    assert tc.target_tokens("src/main.rs::tests::x")[:1] == ["--bins"]
    assert tc.case_key("tests/calc.rs::add_returns_sum") == "add_returns_sum"
    argv = tc.run_argv(r.test_argv("tests/calc.rs::x"), r.cwd, None, "tests/calc.rs::x")
    assert argv.index("--target-dir") < argv.index("--") and argv[argv.index("--target-dir") + 1].startswith(r.engine_dir + "/target-")
    # never shared between checkouts (a stale artefact would answer for the reverted tree)
    assert tc.target_dir("/a") != tc.target_dir("/b")
    col = tc.collect_argv(r.cwd, "tests/calc.rs::x")
    assert col[-4:] == ["--", "x", "--exact", "--list"]
    assert tc.collect_argv(r.cwd, None)[-2:] == ["--", "--list"]
    assert r.example_test_argv()[-5:] == ["--test", "<file>", "--", "<test_fn>", "--exact"]


def test_go_target_mapping_and_module_relative_packages():
    r = _runner("toy-go")
    assert r.verified and r.framework == "go"
    tc = r.toolchain
    assert tc.module == "example.invalid/toy"
    assert r.test_argv("calc/calc_test.go::TestAdd") == ["go", "test", "-json", "-count=1", "-race", "-run", "^TestAdd$", "./calc"]
    assert tc.target_tokens("main_test.go::TestRoot") == ["-run", "^TestRoot$", "."]
    assert tc.target_tokens("a/b/c_test.go::TestX") == ["-run", "^TestX$", "./a/b"]
    assert tc.case_key("calc/x_test.go::TestAdd") == "calc::TestAdd" and tc.case_key("x_test.go::TestR") == ".::TestR"
    assert tc.rel_dir("example.invalid/toy") == "." and tc.rel_dir("example.invalid/toy/cmd/toy") == "cmd/toy"
    assert tc.collect_argv(r.cwd, None) == ["go", "test", "-json", "-count=1", "-list", ".*", "./..."]
    assert tc.collect_argv(r.cwd, "calc/x_test.go::TestAdd") == ["go", "test", "-json", "-count=1", "-list", "^TestAdd$", "./calc"]
    assert "-race" in r.suite_argv() and "-count=1" in r.suite_argv() and "-json" in r.suite_argv()


def test_node_target_mapping_and_reporters():
    r = _runner("toy-ts")
    assert r.verified and r.framework == "npm"
    tc = r.toolchain
    assert r.test_argv("tests/calc.test.ts::add_sum") == ["node", "--test", "--test-name-pattern=^add_sum$", "tests/calc.test.ts"]
    argv = tc.run_argv(r.test_argv("tests/calc.test.ts::add_sum"), r.cwd, "/x/run.xml", "tests/calc.test.ts::add_sum")
    assert argv[:7] == ["node", "--test", "--test-reporter=junit", "--test-reporter-destination=/x/run.xml", "--test-reporter=tap",
                        "--test-reporter-destination=stdout", "--test-name-pattern=^add_sum$"]
    assert tc.collect_argv(r.cwd, None) is None and not tc.has_listing       # node 22 has no collect-only: stated
    assert tc.case_key("tests/a.test.ts::x") == "x"
    assert tc.case_under("suite > x", "x") and tc.case_under("x #2", "x") and not tc.case_under("xy", "x")


def test_baseline_failure_attribution_knows_each_ecosystems_case_keys():
    """§C.8.4 step 5: a pre-existing failure attributable to another finding's reproduction may remain; the keys
    the verified counts use differ per ecosystem (before this wave only the pytest node id matched)."""
    assert same_test("calc::TestAddReturnsSum", "calc/calc_test.go::TestAddReturnsSum")
    assert same_test(".::TestRoot", "main_test.go::TestRoot")
    assert same_test("add_returns_sum", "tests/calc.rs::add_returns_sum")
    assert same_test("add_returns_sum", "tests/calc.test.ts::add_returns_sum") and same_test("group > add_returns_sum", "tests/a.test.ts::add_returns_sum")
    assert same_test("tests/test_calc.py::test_add[1]", "tests/test_calc.py::test_add")
    assert not same_test("calc::TestOther", "calc/calc_test.go::TestAddReturnsSum") and not same_test("x", "tests/a.rs")


def test_target_grammar_covers_every_ecosystem_for_disproof():
    assert node_id_in_text("see tests/calc.rs::add_returns_sum here") == "tests/calc.rs::add_returns_sum"
    assert node_id_in_text("run calc/calc_test.go::TestAddReturnsSum: x") == "calc/calc_test.go::TestAddReturnsSum"
    assert node_id_in_text("tests/calc.test.ts::add_returns_sum fails") == "tests/calc.test.ts::add_returns_sum"
    assert node_id_in_text("tests/test_calc.py::test_add_returns_sum") == "tests/test_calc.py::test_add_returns_sum"
    assert node_id_in_text("../x.rs::y") is None and node_id_in_text("no target") is None


@pytest.mark.parametrize("svc,infra,tests,src", [
    ("toy-rs", ["Cargo.toml", "Cargo.lock", "build.rs", "src/build.rs", ".cargo/config.toml", "rust-toolchain.toml", "crates/a/Cargo.toml"],
     ["tests/calc.rs", "tests/it/main.rs", "benches/b.rs"], ["src/lib.rs", "src/calc.rs", "src/bin/x.rs"]),
    ("toy-go", ["go.mod", "go.sum", "go.work", "calc/testdata/x.json", "vendor/a/b.go", "internal/go.mod"],
     ["calc/calc_test.go", "cmd/toy/main_test.go"], ["calc/calc.go", "cmd/toy/main.go"]),
    ("toy-ts", ["package.json", "package-lock.json", ".npmrc", "tsconfig.json", "tsconfig.build.json", "node_modules/x/index.js",
                "src/package.json", ".nvmrc"],
     ["tests/calc.test.ts", "src/x.spec.mjs", "test/a.mjs"], ["src/calc.ts", "src/lib/x.mts"]),
])
def test_classification_knows_each_ecosystem(svc, infra, tests, src):
    r = _runner(svc)
    out = r.classify_paths([f"services/{svc}/{p}" for p in infra + tests + src])
    assert sorted(out["test_infra"]) == sorted(f"services/{svc}/{p}" for p in infra)
    assert sorted(out["test"]) == sorted(f"services/{svc}/{p}" for p in tests)
    assert sorted(out["src"]) == sorted(f"services/{svc}/{p}" for p in src)


def test_content_rules_refuse_the_cheap_forgery_routes():
    go = _runner("toy-go")
    assert go.denied_test_content(GO_TESTMAIN) == "test_main"
    assert go.denied_test_content("func TestX(t *testing.T) { os.Exit(0) }") == "process_exit"
    assert go.denied_test_content("fmt.Print(\"\\x16--- PASS: TestGhost\")") == "test2json_frame"
    assert go.denied_test_content(GO_TEST) is None
    rs = _runner("toy-rs")
    assert rs.denied_test_content(RS_EXIT) == "process_exit"
    assert rs.denied_test_content("use std::os::unix::io::FromRawFd; File::from_raw_fd(1)") == "raw_fd_write"
    assert rs.denied_test_content(RS_TEST) is None and rs.denied_test_content(RS_FORGER) is None   # the duplicate line catches it
    ts = _runner("toy-ts")
    assert ts.denied_test_content(TS_EXIT) == "process_exit"
    assert ts.denied_test_content("fs.writeSync(1, buf)") == "raw_stdout_fd"
    assert ts.denied_test_content(TS_TEST) is None and ts.denied_test_content(TS_FORGER) is None     # console.log is a TAP comment


# ====================================================================== the verifiers (unit: every disagreement is unknown)

GO_LIST = "\n".join(json.dumps(e) for e in [
    {"Action": "start", "Package": "example.invalid/toy/calc"},
    {"Action": "output", "Package": "example.invalid/toy/calc", "Output": "TestA\n"},
    {"Action": "output", "Package": "example.invalid/toy/calc", "Output": "TestB\n"},
    {"Action": "output", "Package": "example.invalid/toy/calc", "Output": "ExampleZ\n"},
    {"Action": "output", "Package": "example.invalid/toy/calc", "Output": "ok  \texample.invalid/toy/calc\t0.006s\n"},
    {"Action": "pass", "Package": "example.invalid/toy/calc", "Elapsed": 0.008},
]) + "\n"


def go_events(*items: dict) -> str:
    return "\n".join(json.dumps(e) for e in items) + "\n"


def _go() -> toolchains.GoToolchain:
    return toolchains.GoToolchain(SEED["frameworks"]["go"], str(FIXTURES / "toy-go"))


def test_go_verifier_cross_checks_events_against_the_listing_packages_and_exit():
    tc = _go()
    listing = tc.parse_listing(GO_LIST, 0, False, False)
    assert listing is not None and listing.total == 2 and listing.names == ["calc::TestA", "calc::TestB"]   # ExampleZ is not counted
    ok = go_events({"Action": "run", "Package": "example.invalid/toy/calc", "Test": "TestA"},
                   {"Action": "pass", "Package": "example.invalid/toy/calc", "Test": "TestA"},
                   {"Action": "run", "Package": "example.invalid/toy/calc", "Test": "TestB"},
                   {"Action": "output", "Package": "example.invalid/toy/calc", "Test": "TestB", "Output": "--- PASS: TestGhost (0.00s)\n"},
                   {"Action": "fail", "Package": "example.invalid/toy/calc", "Test": "TestB"},
                   {"Action": "fail", "Package": "example.invalid/toy/calc"})
    c = tc.verify(report=None, output=ok, listing=listing, exit_code=1, timed_out=False, truncated=False)
    assert c.ok and (c.passed, c.failed, c.skipped) == (1, 1, 0) and c.failed_names == ["calc::TestB"] and c.collected == 2
    assert c.verdict_for("calc::TestB", tc.case_under) == "fail" and c.verdict_for("calc::TestA", tc.case_under) == "pass"
    assert c.verdict_for("calc::TestC", tc.case_under) == "unknown"
    bad = {
        "exit 0 with a failed test": (ok, 0),
        "a result for a test the listing never enumerated": (go_events(
            {"Action": "pass", "Package": "example.invalid/toy/calc", "Test": "TestA"},
            {"Action": "pass", "Package": "example.invalid/toy/calc", "Test": "TestB"},
            {"Action": "pass", "Package": "example.invalid/toy/calc", "Test": "TestGhost"},
            {"Action": "pass", "Package": "example.invalid/toy/calc"}), 0),
        "2 terminal event(s)": (go_events(
            {"Action": "pass", "Package": "example.invalid/toy/calc", "Test": "TestA"},
            {"Action": "pass", "Package": "example.invalid/toy/calc", "Test": "TestA"},
            {"Action": "pass", "Package": "example.invalid/toy/calc", "Test": "TestB"},
            {"Action": "pass", "Package": "example.invalid/toy/calc"}), 0),
        "failed with no failed test": (go_events(
            {"Action": "pass", "Package": "example.invalid/toy/calc", "Test": "TestA"},
            {"Action": "pass", "Package": "example.invalid/toy/calc", "Test": "TestB"},
            {"Action": "fail", "Package": "example.invalid/toy/calc"}), 1),
        "0 package-level result(s)": (go_events(
            {"Action": "pass", "Package": "example.invalid/toy/calc", "Test": "TestA"},
            {"Action": "pass", "Package": "example.invalid/toy/calc", "Test": "TestB"}), 0),
        "build failed": (go_events({"ImportPath": "x", "Action": "build-fail"}, {"Action": "fail", "Package": "example.invalid/toy/calc"}), 1),
        "unparseable": ("not json\n", 0),
        "0 terminal event(s) for a listed test": (go_events(
            {"Action": "pass", "Package": "example.invalid/toy/calc", "Test": "TestA"},
            {"Action": "pass", "Package": "example.invalid/toy/calc"}), 0),
    }
    for why, (out, exit_code) in bad.items():
        c = tc.verify(report=None, output=out, listing=listing, exit_code=exit_code, timed_out=False, truncated=False)
        assert not c.ok and why in c.why, (why, c.why)
    assert "timed out" in tc.verify(report=None, output=ok, listing=listing, exit_code=124, timed_out=True, truncated=False).why
    assert "truncated" in tc.verify(report=None, output=ok, listing=listing, exit_code=1, timed_out=False, truncated=True).why
    assert "unavailable" in tc.verify(report=None, output=ok, listing=None, exit_code=1, timed_out=False, truncated=False).why
    empty = tc.parse_listing(go_events({"Action": "start", "Package": "example.invalid/toy/e"},
                                       {"Action": "output", "Package": "example.invalid/toy/e", "Output": "?   \tx\t[no test files]\n"},
                                       {"Action": "skip", "Package": "example.invalid/toy/e"}), 0, False, False)
    assert empty is not None and empty.total == 0
    assert "nothing collected" in tc.verify(report=None, output="", listing=empty, exit_code=0, timed_out=False, truncated=False).why
    assert tc.parse_listing(GO_LIST, 1, False, False) is None and tc.parse_listing("garbage\n", 0, False, False) is None


CARGO_LIST = ("     Running unittests src/lib.rs (x)\ntests::clamp_bounds: test\ntests::ignored_placeholder: test\n\n2 tests, 0 benchmarks\n"
              "add_returns_sum: test\npercent_basic: test\n\n2 tests, 0 benchmarks\nsrc/lib.rs - add (line 5): test\n\n1 test, 0 benchmarks\n")
CARGO_RUN = ("\nrunning 2 tests\ntest tests::ignored_placeholder ... ignored\ntest tests::clamp_bounds ... ok\n\n"
             "test result: ok. 1 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.00s\n\n"
             "\nrunning 2 tests\ntest percent_basic ... ok\ntest add_returns_sum ... FAILED\n\nfailures:\n\n---- add_returns_sum stdout ----\n"
             "assertion failed\n\nfailures:\n    add_returns_sum\n\n"
             "test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.20s\n\n"
             "\nrunning 1 test\ntest src/lib.rs - add (line 5) ... ok\n\n"
             "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.24s\n"
             + toolchains.STDERR_MARK + "error: test failed, to rerun pass `--test calc`\nerror: 1 target failed:\n")


def _cargo() -> toolchains.CargoToolchain:
    return toolchains.CargoToolchain(SEED["frameworks"]["cargo"], str(FIXTURES / "toy-rs"), "/e")


def test_cargo_verifier_cross_checks_lines_against_the_listing_binaries_and_exit():
    tc = _cargo()
    listing = tc.parse_listing(CARGO_LIST, 0, False, False)
    assert listing is not None and listing.total == 5 and listing.binaries == 3 and "src/lib.rs - add (line 5)" in listing.names
    c = tc.verify(report=None, output=CARGO_RUN, listing=listing, exit_code=101, timed_out=False, truncated=False)
    assert c.ok and (c.passed, c.failed, c.skipped) == (3, 1, 1) and c.failed_names == ["add_returns_sum"] and c.collected == 5
    assert c.verdict_for("add_returns_sum", tc.case_under) == "fail" and c.verdict_for("tests::ignored_placeholder", tc.case_under) == "unknown"
    forged = CARGO_RUN.replace("test add_returns_sum ... FAILED", "test add_returns_sum ... ok\ntest add_returns_sum ... FAILED")
    c = tc.verify(report=None, output=forged, listing=listing, exit_code=101, timed_out=False, truncated=False)
    assert not c.ok and "printed twice: add_returns_sum" in c.why
    ghost = CARGO_RUN.replace("test percent_basic ... ok", "test percent_basic ... ok\ntest ghost ... ok")
    assert "unlisted: ghost" in tc.verify(report=None, output=ghost, listing=listing, exit_code=101, timed_out=False, truncated=False).why
    one_binary_missing = CARGO_RUN.rsplit("\nrunning 1 test", 1)[0]
    c = tc.verify(report=None, output=one_binary_missing, listing=listing, exit_code=101, timed_out=False, truncated=False)
    assert not c.ok and "listing" in c.why
    c = tc.verify(report=None, output=CARGO_RUN, listing=listing, exit_code=0, timed_out=False, truncated=False)
    assert not c.ok and "exit 0 with a failed test" in c.why
    lied = CARGO_RUN.replace("test result: FAILED. 1 passed; 1 failed;", "test result: ok. 2 passed; 0 failed;")
    assert "disagree" in tc.verify(report=None, output=lied, listing=listing, exit_code=101, timed_out=False, truncated=False).why
    assert "timed out" in tc.verify(report=None, output=CARGO_RUN, listing=listing, exit_code=124, timed_out=True, truncated=False).why
    assert "truncated" in tc.verify(report=None, output=CARGO_RUN, listing=listing, exit_code=101, timed_out=False, truncated=True).why
    assert "unavailable" in tc.verify(report=None, output=CARGO_RUN, listing=None, exit_code=101, timed_out=False, truncated=False).why
    none = tc.parse_listing("0 tests, 0 benchmarks\n0 tests, 0 benchmarks\n", 0, False, False)
    assert none is not None and none.total == 0
    assert "nothing collected" in tc.verify(report=None, output="", listing=none, exit_code=0, timed_out=False, truncated=False).why
    assert tc.parse_listing("a: test\n\n2 tests, 0 benchmarks\n", 0, False, False) is None      # the listing's own summary disagrees
    assert tc.parse_listing(CARGO_LIST, 101, False, False) is None


NODE_XML = ("<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<testsuites>\n\t<testcase name=\"clamp_bounds\" time=\"0.0009\" classname=\"test\"/>\n"
            "\t<testcase name=\"add_returns_sum\" time=\"0.005\" classname=\"test\"><failure type=\"testCodeFailure\" message=\"x\">x</failure></testcase>\n"
            "\t<testcase name=\"skipped_placeholder\" time=\"0.0001\" classname=\"test\"><skipped type=\"skipped\" message=\"placeholder\"/></testcase>\n"
            "\t<testsuite name=\"group\" tests=\"1\"><testcase name=\"inner_ok\" time=\"0.0006\" classname=\"test\"/></testsuite>\n"
            "</testsuites>\n")
NODE_TAP = ("TAP version 13\n# Subtest: clamp_bounds\nok 1 - clamp_bounds\n# Subtest: add_returns_sum\nnot ok 2 - add_returns_sum\n"
            "  ---\n  error: x\n  ...\n# Subtest: skipped_placeholder\nok 3 - skipped_placeholder # SKIP placeholder\n# Subtest: group\n"
            "    # Subtest: inner_ok\n    ok 1 - inner_ok\n    1..1\nok 4 - group\n1..4\n# tests 4\n# suites 1\n# pass 2\n# fail 1\n# cancelled 0\n"
            "# skipped 1\n# todo 0\n# duration_ms 636.9\n")


def _node() -> toolchains.NodeToolchain:
    return toolchains.NodeToolchain(SEED["frameworks"]["npm"], str(FIXTURES / "toy-ts"))


def test_node_verifier_cross_checks_junit_against_tap_and_exit():
    tc = _node()
    c = tc.verify(report=NODE_XML, output=NODE_TAP, listing=None, exit_code=1, timed_out=False, truncated=False)
    assert c.ok and (c.passed, c.failed, c.skipped) == (2, 1, 1) and c.failed_names == ["add_returns_sum"] and c.collected == 4
    assert c.cases["group > inner_ok"] == "pass"
    assert c.verdict_for("add_returns_sum", tc.case_under) == "fail" and c.verdict_for("inner_ok", tc.case_under) == "pass"
    assert c.verdict_for("skipped_placeholder", tc.case_under) == "unknown" and c.verdict_for("ghost", tc.case_under) == "unknown"
    # a rewritten report (all green) disagrees with the transcript and the exit code
    green = NODE_XML.replace("<failure type=\"testCodeFailure\" message=\"x\">x</failure>", "")
    assert "disagrees" in tc.verify(report=green, output=NODE_TAP, listing=None, exit_code=1, timed_out=False, truncated=False).why
    assert "exit 0 with a failure" in tc.verify(report=NODE_XML, output=NODE_TAP, listing=None, exit_code=0, timed_out=False, truncated=False).why
    assert "missing" in tc.verify(report=None, output=NODE_TAP, listing=None, exit_code=1, timed_out=False, truncated=False).why
    assert "unparseable" in tc.verify(report="<nope", output=NODE_TAP, listing=None, exit_code=1, timed_out=False, truncated=False).why
    assert "no TAP summary" in tc.verify(report=NODE_XML, output="", listing=None, exit_code=1, timed_out=False, truncated=False).why
    fewer = NODE_TAP.replace("# tests 4", "# tests 3")
    assert "!= TAP tests" in tc.verify(report=NODE_XML, output=fewer, listing=None, exit_code=1, timed_out=False, truncated=False).why
    forged_line = NODE_TAP.replace("not ok 2 - add_returns_sum", "ok 2 - add_returns_sum")
    assert "top-level failures" in tc.verify(report=NODE_XML, output=forged_line, listing=None, exit_code=1, timed_out=False, truncated=False).why
    empty_xml = "<testsuites>\n<!-- tests 0 -->\n</testsuites>\n"
    empty_tap = "TAP version 13\n1..0\n# tests 0\n# suites 0\n# pass 0\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0\n"
    assert "nothing collected" in tc.verify(report=empty_xml, output=empty_tap, listing=None, exit_code=0, timed_out=False, truncated=False).why
    assert "timed out" in tc.verify(report=NODE_XML, output=NODE_TAP, listing=None, exit_code=124, timed_out=True, truncated=False).why
    assert "truncated" in tc.verify(report=NODE_XML, output=NODE_TAP, listing=None, exit_code=1, timed_out=False, truncated=True).why
    dup = parsers.parse_junit_node(NODE_XML.replace("<testsuite name=\"group\" tests=\"1\"><testcase name=\"inner_ok\"", "<testsuite name=\"group\" tests=\"1\"><testcase name=\"clamp_bounds\""))
    assert dup is not None and dup.cases["group > clamp_bounds"] == "pass" and dup.passed == 2


def test_summary_only_counts_stay_unknown_for_an_unverified_seed():
    c = parsers.parse_counts("go", "ok  \tx\t0.1s\n--- PASS: TestA (0.00s)\n")
    assert not c.ok and c.status == "unknown" and c.source == "summary"


# ====================================================================== the whole loop per ecosystem (real toolchains, fake model)

@pytest.mark.parametrize("name", ["go", "cargo", "npm"])
def test_full_loop_reaches_fixed_with_engine_owned_counts(name):
    eco = ECO[name]
    svc = eco["svc"]
    path, old, new = eco["fix"]
    scenario = flat([wf(svc, eco["test_path"], eco["test"]), {"text": f"TEST: {target(eco)}"},
                     rep(svc, path, old, new), {"text": f"{eco['sweep']}\nFIXED"}])
    h = Harness(scenario=scenario, service=svc)
    try:
        run, f = _run(h, eco)
        assert run["status"] == "awaiting_review", (run["reasons"], _why(h))
        assert f["state"] == "fixed" and f["red"]["verdict"] == "fail" and f["green"]["verdict"] == "pass"
        assert f["verification"]["verification_checkout"]["verdict"] == "pass" and f["verification"]["reverted_checkout"]["verdict"] == "fail"
        before, after = run["suite"]["before"]["counts"], run["suite"]["after"]["counts"]
        assert before["status"] == "ok" and before["source"] == eco["source"] and before["failed_names"] == [eco["failed_name"]]
        assert before["collected"] == eco["listed_before"] == before["passed"] + before["failed"] + before["errors"] + before["skips"]
        assert after["status"] == "ok" and after["failed"] == 0 and after["collected"] == eco["listed_after"]
        # the argv the engine ran is the seeded one plus the engine's own options, never the agent's
        red = f["red"]["argv"]
        assert red[:2] == SEED["frameworks"][name]["test"][:2]
        if name == "go":
            assert "-json" in red and "-race" in red and "-count=1" in red and red[-3:] == ["-run", "^TestAddSum$", "./calc"]
            assert f["red"]["junit_sha256"] is None                    # the report is the captured event stream
        if name == "cargo":
            assert "--no-fail-fast" in red and "--locked" in red and red[-3:] == ["--", "add_sum", "--exact"] and "--test" in red
            assert red[red.index("--target-dir") + 1].startswith("/mnt/user-data/workspace/.dlv-engine/")
        if name == "npm":
            assert red[2] == "--test-reporter=junit" and red[3].startswith("--test-reporter-destination=/mnt/user-data/workspace/.dlv-engine/")
            assert f["red"]["junit_sha256"] and f["green"]["junit_sha256"]
        # every engine exec that ran a toolchain is a seeded prefix (D7): the agent's own commands are bash -lc, opaque
        heads = {"go": ("go",), "cargo": ("cargo",), "npm": ("node",)}[name]
        seeded = [tuple(SEED["frameworks"][name][k][:2]) for k in ("test", "suite", "collect") if SEED["frameworks"][name].get(k)]
        ran = 0
        for c in h.docker.argv_of("exec"):
            if "-lc" in c:
                continue
            body = c[c.index("timeout") + 4:]
            if body[0] in heads:
                ran += 1
                assert tuple(body[:2]) in seeded, c
        assert ran >= 6                                                     # suite before, RED(+list), GREEN, 2 checkouts, suites
        rep_text = h.report(run["run_id"])
        assert "### N1-1" in rep_text and "fixed" in rep_text
    finally:
        h.close()


@pytest.mark.parametrize("name", ["go", "cargo", "npm"])
def test_deny_paths_infra_edit_deleted_test_content_rule_then_fixed(name):
    """One run, four FIXED replies: (1) a test-infra edit → test_infra_changed; (2) an existing test deleted →
    test_deleted; (3) a test file that exits the process / defines TestMain → test_content_denied; (4) clean → fixed."""
    eco = ECO[name]
    svc = eco["svc"]
    path, old, new = eco["fix"]
    ipath, iold, inew = eco["infra"]
    existing = open(FIXTURES / svc / eco["existing_test"], encoding="utf-8").read()
    scenario = flat([
        wf(svc, eco["test_path"], eco["test"]), {"text": f"TEST: {target(eco)}"},
        rep(svc, path, old, new), rep(svc, ipath, iold, inew), {"text": f"{eco['sweep']}\nFIXED"},                    # 1
        rep(svc, ipath, inew, iold), rm(svc, eco["existing_test"]), {"text": f"{eco['sweep']}\nFIXED"},               # 2
        wf(svc, eco["existing_test"], existing), wf(svc, eco["content_path"], eco["content"]), {"text": f"{eco['sweep']}\nFIXED"},  # 3
        rm(svc, eco["content_path"]), {"text": f"{eco['sweep']}\nFIXED"},                                            # 4
    ])
    h = Harness(scenario=scenario, service=svc, extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "5"})
    try:
        run, f = _run(h, eco)
        whys = _why(h)
        assert whys[:3] == ["test_infra_changed", "test_deleted", "test_content_denied"], (whys, run["reasons"])
        rf = [e["payload"] for e in h.events("round_failed")]
        assert rf[0]["paths"] == [f"services/{svc}/{ipath}"]
        assert rf[1]["paths"] == [f"services/{svc}/{eco['existing_test']}"]
        assert rf[2]["paths"] == [f"services/{svc}/{eco['content_path']} ({eco['content_rule']})"]
        assert run["status"] == "awaiting_review" and f["state"] == "fixed", (run["reasons"], whys)
        assert f["verification"]["classification"]["test_infra"] == [] and f["commit_sha"]
    finally:
        h.close()


@pytest.mark.parametrize("name", ["go", "cargo", "npm"])
def test_deny_paths_forged_transcript_and_hung_test_are_unknown_never_red(name):
    """The RED test forges the ecosystem's own result lines: go emits test2json frames (an unlisted/duplicate
    event), cargo writes libtest's lines to fd 1 (a name printed twice), node prints TAP (a comment: harmless, the
    real result stands). The test is then rewritten clean within the same second and must be REBUILT (cargo's
    mtime-based freshness once served the forger's binary for the clean file: the engine touches the tree before
    every run and ships files with fractional mtimes). Then a hung test in the suite → the per-finding suite is
    unknown → no fixed."""
    eco = ECO[name]
    svc = eco["svc"]
    path, old, new = eco["fix"]
    scenario = flat([
        wf(svc, eco["test_path"], eco["forger"]), {"text": f"TEST: {target(eco)}"},
        rd(svc, eco["test_path"]), wf(svc, eco["test_path"], eco["test"]), {"text": f"TEST: {target(eco)}"},
        rep(svc, path, old, new), wf(svc, eco["hang_path"], eco["hang"]), {"advance_clock_s": 2688}, {"text": f"{eco['sweep']}\nFIXED"},
    ])
    h = Harness(scenario=scenario, service=svc)
    try:
        run, f = _run(h, eco)
        reds = [e["payload"] for e in h.events("test_run") if e["payload"]["phase"] == "red"]
        assert len(reds) == 2
        if name == "npm":
            assert reds[0]["verdict"] == "fail" and "red_unknown" not in _why(h)     # printed TAP is a comment, never a result
        else:
            assert reds[0]["verdict"] == "unknown" and _why(h)[0] == "red_unknown"
            assert reds[0]["exit"] != 0
        assert reds[1]["verdict"] == "fail"
        per = [e["payload"] for e in h.events("suite_run") if e["payload"]["phase"] == "per_finding"]
        assert per and per[-1]["status"] == "unknown" and "timed out" in per[-1]["why"]
        assert "suite_unknown" in _why(h) and f["state"] != "fixed" and run["status"] == "failed"
    finally:
        h.close()
