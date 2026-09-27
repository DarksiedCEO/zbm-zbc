"""
Start-up gate (spec §C.1 items 3-6, 9-11; §0.4(c); §C.7.2; G10): the checks that need files, hashes, the installed
packages, the deer-flow distribution record or the Docker daemon. Every check returns a plain problem text; ``run``
raises ``RuntimeError`` with all of them (never a warning). The config YAML checks are pure functions of the parsed
document so the G10 mutation table can exercise them without touching disk.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

from zbm_delivery import config as C
from zbm_delivery import licences

FORBIDDEN_MODULES = ("app", "langgraph_api", "langchain_openviking", "openviking_sdk", "telegram", "forbiddenfruit")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@dataclass
class GateReport:
    config_sha256: str = ""
    extensions_sha256: str = ""
    skills_manifest_sha256: str = ""
    prompts_manifest_sha256: str = ""
    seeds: dict = field(default_factory=dict)
    deerflow_commit: str = ""
    deerflow_source: str = ""
    licence_ok: bool = False
    licence_problems: list = field(default_factory=list)
    manifest_skill_names: list = field(default_factory=list)
    problems: list = field(default_factory=list)


# --- deer-flow config (C.1.3) ------------------------------------------------------------------------------------------

def _get(d: Any, *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def resolve_env(doc: Any, env: dict) -> Any:
    """deer-flow's ``$NAME`` substitution (config/app_config.py resolve_env_variables), applied here so the gate
    checks the values deer-flow will see."""
    if isinstance(doc, str):
        if doc.startswith("$"):
            return env.get(doc[1:], f"<unset:{doc[1:]}>")
        return doc
    if isinstance(doc, dict):
        return {k: resolve_env(v, env) for k, v in doc.items()}
    if isinstance(doc, list):
        return [resolve_env(x, env) for x in doc]
    return doc


def config_problems(doc: dict, settings: C.Settings) -> list[str]:
    p: list[str] = []
    if not isinstance(doc, dict):
        return ["deer-flow config is not a mapping"]
    if _get(doc, "sandbox", "use") != C.SANDBOX_CLASS:
        p.append(f"sandbox.use must be {C.SANDBOX_CLASS}")
    if _get(doc, "sandbox", "allow_host_bash") is not False:
        p.append("sandbox.allow_host_bash must be false")
    img = _get(doc, "sandbox", "image")
    prob = C.image_problem(img if isinstance(img, str) else None, settings.image_registry)
    if prob:
        p.append(f"sandbox.image: {prob}")
    elif img != settings.sandbox_image:
        p.append("sandbox.image must equal DLV_SANDBOX_IMAGE")
    if _get(doc, "sandbox", "network") is not None:
        p.append("sandbox.network.* must be absent (the proxy sidecar is not used; network policy is Docker-level)")
    for k in ("port", "replicas", "mounts", "environment", "thread_data_mounts", "ownership"):
        if _get(doc, "sandbox", k) not in (None, [], {}):
            p.append(f"sandbox.{k} must be absent")
    bct = _get(doc, "sandbox", "bash_command_timeout")
    if not isinstance(bct, int) or bct > settings.cmd_timeout_s:
        p.append("sandbox.bash_command_timeout must be an integer <= DLV_CMD_TIMEOUT_S")
    if _get(doc, "guardrails", "enabled") is not True:
        p.append("guardrails.enabled must be true")
    if _get(doc, "guardrails", "fail_closed") is not True:
        p.append("guardrails.fail_closed must be true")
    if _get(doc, "guardrails", "provider", "use") != C.GUARDRAIL_CLASS:
        p.append(f"guardrails.provider.use must be {C.GUARDRAIL_CLASS}")
    if _get(doc, "guardrails", "passport") not in (None, ""):
        p.append("guardrails.passport must be absent")
    if _get(doc, "memory", "enabled") is not False:
        p.append("memory.enabled must be false")
    if _get(doc, "memory", "injection_enabled") is not False:
        p.append("memory.injection_enabled must be false")
    if _get(doc, "memory", "manager_class") != "noop":
        p.append("memory.manager_class must be noop")
    if _get(doc, "skill_evolution", "enabled") is not False:
        p.append("skill_evolution.enabled must be false")
    if _get(doc, "acp_agents", default={}) not in ({}, None):
        p.append("acp_agents must be empty")
    if _get(doc, "extensions", "mcp_servers") not in (None, {}) or _get(doc, "extensions", "mcpServers") not in (None, {}):
        p.append("extensions.mcp_servers must be empty")
    if _get(doc, "extensions", "middlewares") not in (None, []):
        p.append("extensions.middlewares must be empty (our middlewares are passed in code)")
    if _get(doc, "plugins", default=[]) not in ([], None):
        p.append("plugins must be empty")
    tools = _get(doc, "tools", default=[])
    if not isinstance(tools, list) or not tools:
        p.append("tools must list the sandbox group")
    else:
        for t in tools:
            name = t.get("name") if isinstance(t, dict) else None
            use = t.get("use") if isinstance(t, dict) else None
            if name not in C.SANDBOX_TOOLS or not isinstance(use, str) or not use.startswith("deerflow.sandbox.tools:"):
                p.append(f"tools: {str(name)[:40]!r} is outside the sandbox group")
    models = _get(doc, "models", default=[])
    if not isinstance(models, list) or len(models) != 1:
        p.append("models must have exactly one entry")
    elif models[0].get("use") != C.MODEL_CLASS:
        p.append(f"models[0].use must be {C.MODEL_CLASS}")
    elif models[0].get("name") != "engine":
        p.append("models[0].name must be engine")
    skills_path = _get(doc, "skills", "path")
    if not isinstance(skills_path, str) or os.path.abspath(skills_path) != os.path.abspath(settings.skills_root):
        p.append("skills.path must be our skills root (DLV_SKILLS_ROOT)")
    if _get(doc, "skills", "use") not in (None, "deerflow.skills.storage.local_skill_storage:LocalSkillStorage"):
        p.append("skills.use must be the local skill storage")
    if _get(doc, "verification", "receipts_enabled") is not True:
        p.append("verification.receipts_enabled must be true")
    if _get(doc, "verification", "judge_enabled") is True:
        p.append("verification.judge_enabled must not be true")
    rl = _get(doc, "recursion_limit")
    if not isinstance(rl, int) or rl > settings.recursion_limit:
        p.append("recursion_limit must be an integer <= DLV_RECURSION_LIMIT")
    mrl = _get(doc, "max_recursion_limit")
    if not isinstance(mrl, int) or mrl > settings.recursion_limit:
        p.append("max_recursion_limit must be an integer <= DLV_RECURSION_LIMIT")
    st = _get(doc, "subagents", "timeout_seconds")
    if not isinstance(st, int) or st > settings.subagent_timeout_s:
        p.append("subagents.timeout_seconds must be an integer <= DLV_SUBAGENT_TIMEOUT_S")
    mt = _get(doc, "subagents", "max_turns")
    if not isinstance(mt, int) or mt > settings.subagent_max_turns:
        p.append("subagents.max_turns must be an integer <= DLV_SUBAGENT_MAX_TURNS")
    mtp = _get(doc, "subagents", "max_total_per_run")
    if not isinstance(mtp, int) or mtp > settings.max_subagents_per_run:
        p.append("subagents.max_total_per_run must be an integer <= DLV_MAX_SUBAGENTS_PER_RUN")
    if _get(doc, "token_budget", "enabled") is not True:
        p.append("token_budget.enabled must be true")
    for key in ("tracing", "langfuse", "channel_connections", "agents_api", "authorization", "checkpointer", "stream_bridge"):
        if _get(doc, key) not in (None, {}, []):
            p.append(f"{key} must be absent")
    return p


def load_config_doc(path: str, env: dict) -> tuple[dict, str]:
    with open(path, "rb") as fh:
        raw = fh.read()
    doc = yaml.safe_load(raw) or {}
    return resolve_env(doc, env), sha256_bytes(raw)


# --- manifests (0.4(c), C.9, C.1.4-6) ----------------------------------------------------------------------------------

def walk_hashes(root: str) -> tuple[dict[str, str], list[str]]:
    """{relative path: sha256} for every file under ``root``; symlinks (file or dir) are reported as problems."""
    out: dict[str, str] = {}
    problems: list[str] = []
    if not os.path.isdir(root):
        return out, [f"{root} is not a directory"]
    for dirpath, dirnames, filenames in os.walk(root):
        for d in list(dirnames):
            if os.path.islink(os.path.join(dirpath, d)):
                problems.append(f"symlink under the root: {os.path.relpath(os.path.join(dirpath, d), root)}")
                dirnames.remove(d)
        for f in sorted(filenames):
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full):
                problems.append(f"symlink under the root: {rel}")
                continue
            out[rel] = sha256_file(full)
    return out, problems


def manifest_problems(root: str, manifest: dict, what: str) -> list[str]:
    actual, problems = walk_hashes(root)
    if not isinstance(manifest, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in manifest.items()):
        return [f"{what} manifest is not {{path: sha256}}"]
    for rel in sorted(set(actual) - set(manifest)):
        problems.append(f"{what}: extra file not in the manifest: {rel}")
    for rel in sorted(set(manifest) - set(actual)):
        problems.append(f"{what}: manifest file missing: {rel}")
    for rel in sorted(set(manifest) & set(actual)):
        if manifest[rel] != actual[rel]:
            problems.append(f"{what}: file changed: {rel}")
    return problems


def skill_names(manifest: dict) -> list[str]:
    names = set()
    for rel in manifest:
        parts = rel.split("/")
        if len(parts) >= 3 and parts[-1] == "SKILL.md":
            names.add(parts[1])
    return sorted(names)


def extensions_problems(doc: dict, names: list[str]) -> list[str]:
    p = []
    if not isinstance(doc, dict):
        return ["extensions_config.json is not an object"]
    if doc.get("mcpServers") not in ({}, None) or doc.get("mcp_servers") not in ({}, None):
        p.append("extensions_config.json: mcpServers must be empty")
    if doc.get("middlewares") not in ([], None) or doc.get("mcpInterceptors") not in ([], None):
        p.append("extensions_config.json: middlewares / mcpInterceptors must be empty")
    skills = doc.get("skills")
    if not isinstance(skills, dict):
        return p + ["extensions_config.json: skills must be an object"]
    for name, entry in skills.items():
        if name not in names:
            p.append(f"extensions_config.json: skill {str(name)[:40]!r} is not in the skills manifest")
        elif not isinstance(entry, dict) or not isinstance(entry.get("enabled"), bool):
            p.append(f"extensions_config.json: skill {name!r} needs an explicit boolean enabled")
    for name in names:
        if name not in skills:
            p.append(f"extensions_config.json: manifest skill {name!r} has no entry")
    return p


# --- installed packages (C.1.9-10, C.7.2) ------------------------------------------------------------------------------

def import_problems() -> list[str]:
    p = []
    for mod in FORBIDDEN_MODULES:
        try:
            spec = importlib.util.find_spec(mod)
        except (ImportError, ValueError):
            spec = None
        if spec is not None:
            p.append(f"forbidden module importable: {mod}")
    return p


def deerflow_commit_problems(settings: C.Settings) -> tuple[list[str], str, str]:
    try:
        dist = importlib.metadata.distribution("deerflow-harness")
    except importlib.metadata.PackageNotFoundError:
        return ["deerflow-harness is not installed"], "", "absent"
    raw = dist.read_text("direct_url.json")
    if not raw:
        if settings.allow_path_source and settings.non_production:
            return [], "", "path"
        return ["deerflow-harness has no direct_url.json (not installed from the pinned git source); set "
                "DLV_ALLOW_PATH_SOURCE=1 with DLV_NON_PRODUCTION=1 only for a non-production run"], "", "unknown"
    try:
        info = json.loads(raw)
    except ValueError:
        return ["deerflow-harness direct_url.json unreadable"], "", "unknown"
    vcs = info.get("vcs_info") or {}
    commit = str(vcs.get("commit_id") or "")
    if vcs.get("vcs") != "git" or not commit:
        if settings.allow_path_source and settings.non_production:
            return [], "", "path"
        return ["deerflow-harness was not installed from git (direct_url.json has no vcs_info.commit_id)"], "", "path"
    if commit != settings.deerflow_commit:
        return [f"deerflow-harness commit {commit[:12]} is not the pin {settings.deerflow_commit[:12]}"], commit, "git"
    return [], commit, "git"


def site_packages_dir() -> str:
    import sysconfig
    return sysconfig.get_paths()["purelib"]


def licence_problems(seed_dir: str, site_packages: Optional[str] = None) -> tuple[list[str], licences.Report]:
    with open(os.path.join(seed_dir, "licence_allowlist.json"), "rb") as fh:
        allow = json.loads(fh.read())
    with open(os.path.join(seed_dir, "licence_exceptions.json"), "rb") as fh:
        exc = json.loads(fh.read())
    rep = licences.check(site_packages or site_packages_dir(), allow, exc)
    return [f"licence gate: {x}" for x in rep.problems], rep


# --- the whole gate ----------------------------------------------------------------------------------------------------

def seed_pins(seed_dir: str) -> dict[str, str]:
    return {name: sha256_file(os.path.join(seed_dir, f"{name}.json"))
            for name in ("tool_policy_seed", "test_commands_seed", "licence_allowlist", "licence_exceptions",
                         "skills_manifest", "prompts_manifest")}


def run(settings: C.Settings, env: dict, *, site_packages: Optional[str] = None, check_packages: bool = True) -> GateReport:
    rep = GateReport()
    problems: list[str] = []
    # 3. deer-flow config
    try:
        doc, sha = load_config_doc(settings.deerflow_config, env)
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"deer-flow config unreadable: {type(exc).__name__}") from None
    rep.config_sha256 = sha
    expected = settings.deerflow_config_sha256 if settings.allow_unpinned_config else C.PINNED_DEERFLOW_CONFIG_SHA256
    if sha != expected:
        problems.append("deer-flow config sha256 does not match the pin (spec C.1.3; DLV_ALLOW_UNPINNED_CONFIG=1 + "
                        "DLV_DEERFLOW_CONFIG_SHA256 for a non-production run)")
    problems += [f"deer-flow config: {x}" for x in config_problems(doc, settings)]
    # seeds
    try:
        pins = seed_pins(settings.seed_dir)
    except OSError as exc:
        raise RuntimeError(f"seed missing or unreadable: {type(exc).__name__}") from None
    rep.seeds = pins
    for name, pinned in (("tool_policy_seed", C.PINNED_TOOL_POLICY_SHA256), ("test_commands_seed", C.PINNED_TEST_COMMANDS_SHA256),
                         ("licence_allowlist", C.PINNED_LICENCE_ALLOWLIST_SHA256),
                         ("licence_exceptions", C.PINNED_LICENCE_EXCEPTIONS_SHA256),
                         ("skills_manifest", C.PINNED_SKILLS_MANIFEST_SHA256), ("prompts_manifest", C.PINNED_PROMPTS_MANIFEST_SHA256)):
        if pins[name] != pinned:
            problems.append(f"seed/{name}.json sha256 does not match the pin in config.py")
    rep.skills_manifest_sha256 = pins["skills_manifest"]
    rep.prompts_manifest_sha256 = pins["prompts_manifest"]
    # 5. skills root vs manifest; 4. extensions config
    with open(os.path.join(settings.seed_dir, "skills_manifest.json"), "rb") as fh:
        skills_manifest = json.loads(fh.read())
    problems += manifest_problems(settings.skills_root, skills_manifest, "skills root")
    rep.manifest_skill_names = skill_names(skills_manifest)
    try:
        with open(settings.extensions_config, "rb") as fh:
            ext_raw = fh.read()
        rep.extensions_sha256 = sha256_bytes(ext_raw)
        if rep.extensions_sha256 != C.PINNED_EXTENSIONS_CONFIG_SHA256:
            problems.append("extensions_config.json sha256 does not match the pin")
        problems += extensions_problems(json.loads(ext_raw), rep.manifest_skill_names)
    except (OSError, ValueError):
        problems.append("extensions_config.json missing or unreadable")
    # 6. prompts vs manifest
    with open(os.path.join(settings.seed_dir, "prompts_manifest.json"), "rb") as fh:
        prompts_manifest = json.loads(fh.read())
    problems += manifest_problems(settings.prompts_dir, prompts_manifest, "prompts")
    for required in ("engine.system.md", "brief.template.md", "test-driven-development.md", "systematic-debugging.md",
                     "verification-before-completion.md", "executing-plans.md", "writing-plans.md", "reviewer.md", "CHANGES.md"):
        if required not in prompts_manifest:
            problems.append(f"prompts manifest lacks {required}")
    # the repository and the worktree directory (spec H: unset → refuse; here: must exist)
    if not settings.repo_path or not os.path.exists(os.path.join(settings.repo_path, ".git")):
        problems.append("DLV_REPO_PATH is not a git repository")
    if not settings.worktrees_dir or not os.path.isdir(settings.worktrees_dir):
        problems.append("DLV_WORKTREES_DIR is not a directory")
    # 9-10. packages
    if check_packages:
        problems += import_problems()
        cp, commit, source = deerflow_commit_problems(settings)
        problems += cp
        rep.deerflow_commit, rep.deerflow_source = commit, source
        lp, lrep = licence_problems(settings.seed_dir, site_packages)
        rep.licence_ok = lrep.ok
        rep.licence_problems = list(lrep.problems)
        problems += lp
    rep.problems = problems
    if problems:
        raise RuntimeError("refusing to start: " + "; ".join(problems[:12]) + (f"; +{len(problems) - 12} more" if len(problems) > 12 else ""))
    return rep
