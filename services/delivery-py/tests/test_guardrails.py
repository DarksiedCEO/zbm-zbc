"""Guardrail certification (spec §F G1-G14): static scans of src/ and prompts/, the enum, the pins, the config
mutation table, the licence gate on the real venv and on a planted AGPL fixture, import hygiene after a run."""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from helpers import FAKE_KEY, SERVICE_ROOT, SITE_PACKAGES, Harness, finding, findings_doc

from zbm_delivery import DEERFLOW_COMMIT, config as C, gate as G, licences
from zbm_delivery.engine import states

SRC = SERVICE_ROOT / "src" / "zbm_delivery"
PROMPTS = SERVICE_ROOT / "prompts"


def _py_files(root: Path):
    return sorted(p for p in root.rglob("*.py"))


def test_g1_no_shell_true_os_system_eval_exec_in_src():
    for p in _py_files(SRC):
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
                assert name not in ("eval", "exec", "system", "popen"), (p, node.lineno)
                for kw in node.keywords:
                    if kw.arg == "shell":
                        assert isinstance(kw.value, ast.Constant) and kw.value.value is False, (p, node.lineno)


def test_g2_no_llm_sdk_import_outside_model_adapter():
    banned = ("anthropic", "openai", "langchain_anthropic", "langchain_openai")
    for p in _py_files(SRC):
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for m in mods:
                assert not any(m == b or m.startswith(b + ".") for b in banned), (p, m)
    # adapters/model.py imports only BaseChatModel from the LangChain model classes
    tree = ast.parse((SRC / "adapters" / "model.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "language_models" in node.module:
            assert [a.name for a in node.names] == ["BaseChatModel"]


def test_g3_no_fake_or_passing_class_in_src_and_stand_ins_refuse():
    for p in _py_files(SRC):
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                assert not node.name.startswith(("Fake", "Passing", "Stub", "Mock")), (p, node.name)
    from zbm_delivery.ports import LLMNotConfigured, MemoryOff, NoChatBackend, NotWiredVault, Principal, VaultUnavailable
    with pytest.raises(LLMNotConfigured):
        NoChatBackend().complete(None)
    with pytest.raises(VaultUnavailable):
        NotWiredVault().secret("x")
    assert MemoryOff().context(Principal("service", "aegis", "zbm"), "t").available is False
    from zbm_delivery.ledger import LedgerNotRecorded, UnconfiguredLedgerClient
    with pytest.raises(LedgerNotRecorded):
        UnconfiguredLedgerClient().record_event("e", "delivery", "t", "a", "s", {}, "x")
    # the tests' fakes are not importable from src/ (src never adds tests/ to the path)
    for p in _py_files(SRC):
        assert "from fakes" not in p.read_text() and "import fakes" not in p.read_text()


def test_g4_finding_state_enum_has_no_parking_state():
    names = {m.value for m in states.FindingState} | {m.value for m in states.RunStatus}
    for bad in ("parked", "deferred", "minor", "ruling", "skipped"):
        assert bad not in names
    engine_src = "\n".join(p.read_text() for p in _py_files(SRC / "engine"))
    for bad in ("parked", "deferred", "minor", "ruling", "skipped"):
        assert not re.search(rf"""["']{bad}["']""", engine_src), bad


def test_g5_prompts_contain_no_forbidden_strings():
    forbidden = ["git push", "git pull", "git merge", "branch -D", "worktree remove", "npm install", "pip install", "rm -rf",
                 "human partner", "Do not re-run", "park"]
    for p in sorted(PROMPTS.iterdir()):
        text = p.read_text()
        for bad in forbidden:
            assert bad not in text, (p.name, bad)
        # "gh " as a command invocation (start of line or after a space/quote), not inside "through "/"enough "
        assert not re.search(r"(?m)(^|[\s`'\"(])gh\s", text), p.name
    # every fork carries its header naming the original and its hash
    for name in ("test-driven-development.md", "systematic-debugging.md", "verification-before-completion.md", "executing-plans.md",
                 "writing-plans.md", "reviewer.md"):
        assert (PROMPTS / name).read_text().startswith("# Forked from obra/superpowers@8ca22dba "), name
    assert (PROMPTS / "CHANGES.md").exists()


def test_g6_no_code_path_deletes_under_the_evidence_root():
    # static: every rmtree/remove/unlink call site is in fsops.py; fsops refuses protected roots at runtime
    for p in _py_files(SRC):
        text = p.read_text()
        if p.name in ("fsops.py", "policy.py"):        # policy.py names the calls only as deny-list DATA strings
            continue
        assert "shutil.rmtree" not in text and "os.remove(" not in text and "os.unlink(" not in text and ".unlink(" not in text, p
        assert "rm -rf" not in text.replace('["rm", "-rf", "--", posixpath.join(WORKSPACE, rel)]', ""), p
    from zbm_delivery import fsops
    import tempfile
    root = tempfile.mkdtemp()
    ev = os.path.join(root, "evidence")
    os.makedirs(os.path.join(ev, "run1"))
    Path(ev, "run1", "f").write_text("x")
    fsops.protect(ev)
    with pytest.raises(fsops.ProtectedPath):
        fsops.delete_tree(os.path.join(ev, "run1"), within=root)
    with pytest.raises(fsops.ProtectedPath):
        fsops.delete_file(os.path.join(ev, "run1", "f"), within=root)
    with pytest.raises(fsops.ProtectedPath):
        fsops.delete_tree(root, within=root)
    other = os.path.join(root, "wt", ".git")
    os.makedirs(other)
    with pytest.raises(fsops.ProtectedPath):
        fsops.delete_tree(other, within=os.path.join(root, "wt"))
    assert os.path.exists(os.path.join(ev, "run1", "f"))
    # the API has no delete route
    from zbm_delivery.api import create_app
    h = Harness(wire_harness=False)
    try:
        routes = {(r.path, tuple(sorted(r.methods))) for r in create_app(h.svc, h.settings).routes if hasattr(r, "methods")}
        assert not any("DELETE" in m for _, m in routes)
    finally:
        h.close()


def test_g7_g8_payload_shapes_and_no_secret_in_any_record_evidence_or_log():
    h = Harness(llm="anthropic", scenario=None)
    try:
        # a run that is refused (no scripted backend behind 'anthropic'? no: the backend is real-wire → the fake key
        # is read); the model call goes through the egress client whose transport is real → refused by the socket
        # guard. Either way: the key must not appear anywhere.
        r = h.submit(findings_doc(h.base_sha, [finding("N1-1")]))
        assert r.status_code == 202
        blob = json.dumps(h.ledger.events, default=str)
        assert FAKE_KEY not in blob and "sk-test" not in blob
        log = Path(h.env["DLV_DATA_DIR"], "dlv_log.jsonl").read_text()
        assert FAKE_KEY not in log
        for root, _, files in os.walk(h.svc.evidence_root):
            for f in files:
                assert FAKE_KEY not in Path(root, f).read_text(errors="replace")
        for tok in ("DLV_SERVICE_TOKEN", "DLV_ANDRE_APPROVAL_TOKEN"):
            assert h.env[tok] not in blob and h.env[tok] not in log
        # G8: every payload is ids / hashes / enums / ints / bools / argv lists — no free text
        for e in h.ledger.events:
            _assert_shape(e["payload"], e["event_type"])
    finally:
        h.close()


_FREE_TEXT_OK = {"problem", "why", "message"}          # short enum-like reasons ≤ 160 chars, never prompt/tool text


def _assert_shape(obj, where, depth=0):
    assert depth < 6
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert re.fullmatch(r"[a-z_0-9]{1,40}", k), (where, k)
            if k in _FREE_TEXT_OK:
                assert isinstance(v, str) and len(v) <= 160
                continue
            _assert_shape(v, f"{where}.{k}", depth + 1)
    elif isinstance(obj, list):
        assert len(obj) <= 500
        for v in obj:
            _assert_shape(v, where, depth + 1)
    elif isinstance(obj, str):
        assert len(obj) <= 200 and "\n" not in obj, (where, obj[:80])
    else:
        assert obj is None or isinstance(obj, (int, bool)), (where, type(obj))


def test_g9_deerflow_pin_constant():
    assert DEERFLOW_COMMIT == "345f08be00c8a9495079b732a39b46aa9af1584e"
    assert C.Settings.deerflow_commit == DEERFLOW_COMMIT
    with pytest.raises(RuntimeError, match="audited pin"):
        from helpers import base_env
        C.load({**base_env("/tmp", "/tmp"), "DLV_DEERFLOW_COMMIT": "deadbeef" * 5})


MUTATIONS = [
    ("sandbox.allow_host_bash", True), ("sandbox.use", "deerflow.sandbox.local:LocalSandboxProvider"),
    ("sandbox.use", "deerflow.community.aio_sandbox.aio_sandbox_provider:AioSandboxProvider"),
    ("sandbox.image", "registry.test/zbm/dlv-sandbox:latest"), ("sandbox.image", "docker.io/library/python@sha256:" + "0" * 64),
    ("sandbox.network", {"mode": "proxy"}), ("sandbox.mounts", [{"source": "/", "target": "/host"}]), ("sandbox.environment", {"A": "b"}),
    ("sandbox.bash_command_timeout", 100000), ("sandbox.port", 8080), ("sandbox.thread_data_mounts", True),
    ("guardrails.enabled", False), ("guardrails.fail_closed", False), ("guardrails.provider.use", "deerflow.guardrails.builtin:AllowlistProvider"),
    ("guardrails.passport", "/tmp/passport"), ("guardrails", None),
    ("memory.enabled", True), ("memory.injection_enabled", True), ("memory.manager_class", "deermem"), ("memory", None),
    ("skill_evolution.enabled", True), ("acp_agents", {"claude": {"command": "npx", "args": ["-y", "x"]}}),
    ("extensions", {"mcp_servers": {"gh": {"command": "npx"}}}), ("extensions", {"middlewares": ["x:y"]}), ("plugins", ["x"]),
    ("tools+", {"name": "web_fetch", "group": "web", "use": "deerflow.community.jina_ai.tools:web_fetch_tool"}),
    ("tools+", {"name": "web_search", "group": "web", "use": "deerflow.community.ddg_search.tools:web_search_tool"}),
    ("tools+", {"name": "image_search", "group": "web", "use": "deerflow.community.image_search.tools:image_search_tool"}),
    ("tools+", {"name": "browser_navigate", "group": "browser", "use": "deerflow.community.browser:navigate"}),
    ("tools", []), ("tools", None),
    ("models+", {"name": "second", "use": "langchain_openai:ChatOpenAI", "model": "gpt"}),
    ("models.0.use", "langchain_anthropic:ChatAnthropic"), ("models.0.use", "langchain_openai:ChatOpenAI"), ("models.0.name", "other"),
    ("models", []), ("skills.path", "/tmp/other-skills"), ("skills.use", "deerflow.skills.storage.remote:RemoteSkillStorage"),
    ("verification.receipts_enabled", False), ("verification.judge_enabled", True),
    ("recursion_limit", 1000), ("max_recursion_limit", 1000), ("subagents.timeout_seconds", 1800), ("subagents.max_turns", 200),
    ("subagents.max_total_per_run", 50), ("token_budget.enabled", False), ("tracing", {"langfuse": {"enabled": True}}),
    ("channel_connections", {"telegram": {}}), ("agents_api", {"enabled": True}), ("authorization", {"enabled": True}),
]


def _mutate(doc: dict, key: str, value):
    d = json.loads(json.dumps(doc))
    if key.endswith("+"):
        d[key[:-1]].append(value)
        return d
    parts = key.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur[int(p)] if p.isdigit() else cur.setdefault(p, {})
    last = parts[-1]
    if value is None:
        cur.pop(last, None) if not last.isdigit() else cur.pop(int(last))
    else:
        if last.isdigit():
            cur[int(last)] = value
        else:
            cur[last] = value
    return d


@pytest.mark.parametrize("key,value", MUTATIONS, ids=[f"{k}={str(v)[:30]}" for k, v in MUTATIONS])
def test_g10_config_mutation_refuses_naming_the_key(key, value):
    from helpers import base_env
    env = base_env("/tmp", "/tmp")
    settings = C.load(env)
    doc, _ = G.load_config_doc(str(SERVICE_ROOT / "config" / "deerflow.engine.yaml"), env)
    assert G.config_problems(doc, settings) == []
    mutated = _mutate(doc, key, value)
    problems = G.config_problems(mutated, settings)
    assert problems, (key, value)
    top = key.rstrip("+").split(".")[0]
    assert any(top in p for p in problems), (key, problems)


def test_g10b_the_shipped_yaml_is_pinned_and_passes():
    from helpers import base_env
    env = base_env("/tmp", "/tmp")
    settings = C.load(env)
    doc, sha = G.load_config_doc(settings.deerflow_config, env)
    assert sha == C.PINNED_DEERFLOW_CONFIG_SHA256
    assert G.config_problems(doc, settings) == []
    assert len(MUTATIONS) >= 40


def test_g11_engine_path_never_imports_gateway_or_dropped_packages():
    h = Harness()
    try:
        run_id = h.submit(findings_doc(h.base_sha, [finding("N1-1")]), wait=True).json()["run_id"]
        assert h.run(run_id)["status"] in ("awaiting_review", "failed")
        for mod in ("app", "langgraph_api", "langchain_openviking", "openviking_sdk", "telegram", "forbiddenfruit", "langgraph_cli",
                    "langgraph_runtime_inmem"):
            assert mod not in sys.modules, mod
        # fastapi is imported by api.py only (the test process imports api); deerflow must not have imported it
        import zbm_delivery.engine.loop as loop_mod
        assert "fastapi" not in loop_mod.__dict__
        # tiktoken.load's downloader never ran (the conftest patches it to raise)
    finally:
        h.close()


def test_g12_licence_gate_passes_on_the_venv_and_fails_on_a_planted_agpl_dist(tmp_path):
    allow = json.load(open(SERVICE_ROOT / "seed" / "licence_allowlist.json"))
    exc = json.load(open(SERVICE_ROOT / "seed" / "licence_exceptions.json"))
    real = licences.check(SITE_PACKAGES, allow, exc)
    assert real.ok, real.problems
    assert len(real.dists) > 100
    # a copy of a few dist-infos plus a planted AGPL one, an Elastic one and a forbidden name
    sp = tmp_path / "site-packages"
    sp.mkdir()
    for entry in sorted(os.listdir(SITE_PACKAGES))[:40]:
        if entry.endswith(".dist-info"):
            shutil.copytree(os.path.join(SITE_PACKAGES, entry), sp / entry)
    d = sp / "evilpkg-1.0.dist-info"
    d.mkdir()
    (d / "METADATA").write_text("Metadata-Version: 2.1\nName: evilpkg\nVersion: 1.0\nLicense-Expression: AGPL-3.0-only\n")
    d2 = sp / "studio_thing-2.0.dist-info"
    d2.mkdir()
    (d2 / "METADATA").write_text("Metadata-Version: 2.1\nName: studio-thing\nVersion: 2.0\nClassifier: License :: Other/Proprietary License\nLicense: Elastic License 2.0 (ELv2)\n")
    d3 = sp / "langgraph_api-0.10.0.dist-info"
    d3.mkdir()
    (d3 / "METADATA").write_text("Metadata-Version: 2.1\nName: langgraph-api\nVersion: 0.10.0\nLicense-Expression: MIT\n")
    rep = licences.check(str(sp), allow, exc)
    assert not rep.ok
    assert any("evilpkg" in p and "AGPL-3.0-only" in p for p in rep.problems)
    assert any("studio-thing" in p and "Elastic-2.0" in p for p in rep.problems)
    assert any("langgraph-api" in p and "forbidden" in p for p in rep.problems)
    # the five dropped distributions are absent from the real venv
    present = {d.name for d in real.dists}
    for name in ("langchain-openviking", "openviking-sdk", "langgraph-api", "langgraph-runtime-inmem", "langgraph-cli", "forbiddenfruit",
                 "python-telegram-bot"):
        assert name not in present


def test_g13_socket_and_httpx_client_construction_sites():
    for p in _py_files(SRC):
        text = p.read_text()
        if p.name not in ("egress.py", "ledger.py"):
            assert "httpx.Client(" not in text and "httpx.AsyncClient(" not in text, p
        if p.name != "egress.py":
            for bad in ("import socket", "import requests", "import aiohttp", "urllib.request"):
                assert bad not in text, (p, bad)
    assert "import socket" not in (SRC / "adapters" / "egress.py").read_text()      # not even there: httpx does the I/O


def test_g14_every_runner_argv_is_seeded_and_every_git_call_is_allowlisted():
    h = Harness()
    try:
        run_id = h.submit().json()["run_id"]
        assert h.run(run_id)["status"] == "awaiting_review"
        seed = json.load(open(SERVICE_ROOT / "seed" / "test_commands_seed.json"))
        allowed_prefixes = [tuple(fw["suite"]) for fw in seed["frameworks"].values()]
        allowed_prefixes += [tuple(fw["collect"]) for fw in seed["frameworks"].values() if fw.get("collect")]   # R2 cross-check
        engine_execs = [c for c in h.docker.argv_of("exec") if "-lc" not in c]
        for c in engine_execs:
            body = c[c.index("timeout") + 4:]
            head = body[0]
            assert head in ("pytest", "cat", "find", "grep", "mkdir", "rm", "mv", "test", "python3") or head.startswith("/bin/"), c
            if head == "python3":                          # wave 20 R8: only the pinned resolver, in isolated mode
                assert body[:4] == ["python3", "-I", "/mnt/dlv/resolve.py", "--"], c
            if head == "pytest":
                assert any(tuple(body[:len(p)]) == p for p in allowed_prefixes), c
        git_ok = {"rev-parse", "merge-base", "for-each-ref", "worktree", "status", "diff", "log", "add", "commit", "stash",
                  "remote", "archive", "show"}                # remote (R4 listing), archive/show (R1 verification checkouts)
        for argv in h.git.calls:
            assert argv[:2] == ["git", "-c"] and argv[2].startswith("core.hooksPath=") and argv[3:6] == ["-c", "core.fsmonitor=false", "-C"], argv
            argv = argv[4:]                                       # wave 20 R11: the isolation -c pair precedes -C
            assert argv[3] in git_ok, argv
            if argv[3] == "stash":
                assert argv[4] in ("push", "pop")            # the one revert-check form; never a remote push
            else:
                assert "push" not in argv and "fetch" not in argv and "pull" not in argv
            assert "--force" not in argv and "--hard" not in argv and "-D" not in argv
    finally:
        h.close()


def test_pins_match_the_files_on_disk():
    pins = G.seed_pins(str(SERVICE_ROOT / "seed"))
    assert pins["tool_policy_seed"] == C.PINNED_TOOL_POLICY_SHA256
    assert pins["test_commands_seed"] == C.PINNED_TEST_COMMANDS_SHA256
    assert pins["licence_allowlist"] == C.PINNED_LICENCE_ALLOWLIST_SHA256
    assert pins["licence_exceptions"] == C.PINNED_LICENCE_EXCEPTIONS_SHA256
    assert pins["skills_manifest"] == C.PINNED_SKILLS_MANIFEST_SHA256
    assert pins["prompts_manifest"] == C.PINNED_PROMPTS_MANIFEST_SHA256
    assert G.sha256_file(str(SERVICE_ROOT / "config" / "extensions_config.json")) == C.PINNED_EXTENSIONS_CONFIG_SHA256
    manifest = json.load(open(SERVICE_ROOT / "seed" / "prompts_manifest.json"))
    assert G.manifest_problems(str(PROMPTS), manifest, "prompts") == []
    doc = yaml.safe_load(open(SERVICE_ROOT / "config" / "deerflow.engine.yaml"))
    assert doc["sandbox"]["use"] == C.SANDBOX_CLASS and doc["guardrails"]["provider"]["use"] == C.GUARDRAIL_CLASS
