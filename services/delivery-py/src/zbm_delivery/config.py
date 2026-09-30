"""
Environment configuration (spec §C.1, §H). Every rule fails closed: a configuration the service cannot honour refuses
start-up with a plain message (finance-py ``config.py`` shape: ``_int``/``_flag``/``_only``/``_off`` helpers copied).

``load(env)`` is pure (no I/O, no deerflow import): it validates the environment and returns ``Settings``. The
checks that need files, hashes, the installed packages, git or Docker live in ``gate.py`` and run right after.

Pinned hashes (spec §C.1.3-6, §C.9, §I): the shipped deer-flow config, the extensions config, the skills and prompts
manifests and the three seeds. Any of them differing from its pin refuses start-up unless the operator states the
pin — there is no override in any mode (round 18 R11: the yaml pin is mandatory everywhere; the former
``DLV_ALLOW_UNPINNED_CONFIG`` switch no longer exists).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from zbm_delivery import DEERFLOW_COMMIT

CALLER_NAMES = ("aegis", "andre_session", "scheduler")
TOKEN_MIN = 32
HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DEFAULT_DEERFLOW_CONFIG = os.path.join(SERVICE_ROOT, "config", "deerflow.engine.yaml")
DEFAULT_EXTENSIONS_CONFIG = os.path.join(SERVICE_ROOT, "config", "extensions_config.json")
DEFAULT_SKILLS_ROOT = os.path.join(SERVICE_ROOT, "skills")
DEFAULT_PROMPTS_DIR = os.path.join(SERVICE_ROOT, "prompts")
DEFAULT_SEED_DIR = os.path.join(SERVICE_ROOT, "seed")

# --- pins (spec §I: every pinned hash is recorded in ADR 0011; devtools/gen_manifests.py prints them) ---------------
PINNED_DEERFLOW_CONFIG_SHA256 = "e4d51379594e0f7dc2265a0fee0ff25aa7134b1e6aecabe91b291a1d18064f0e"
PINNED_EXTENSIONS_CONFIG_SHA256 = "4875b2992e9062d6f8084ac64d543f50a29624bd0e7eb82586e31b3056563807"
PINNED_SKILLS_MANIFEST_SHA256 = "26f51402a23232a6b3f6a5764829800c3570403e2694ee1a9f331fe23e040320"
PINNED_PROMPTS_MANIFEST_SHA256 = "eff72a748472a4fb97f68dfc7158c41473cc00ebcf4186b04481dd9c5a7b6717"
PINNED_TOOL_POLICY_SHA256 = "078560a463fd04a11fb3a358e8ebc5a1a782dcedfbd635192bc9a77738787f4d"
PINNED_TEST_COMMANDS_SHA256 = "7af32cf3eeb92446638d3c66ba6efe5af06e4aa1d70afdd18d1c4241487bb0f3"
PINNED_LICENCE_ALLOWLIST_SHA256 = "c02f367f3e6cd02949e18dbc2eaa2ceabcb917ac4aa5fc58ad73bec4ac6dfb6e"
PINNED_LICENCE_EXCEPTIONS_SHA256 = "e93abd348a10f89838e11a19c7f52994e18a20f4d52a6809b49a0a6fafe53606"
POLICY_VERSION = 1

SANDBOX_CLASS = "zbm_delivery.adapters.sandbox:ZbmDockerSandboxProvider"
GUARDRAIL_CLASS = "zbm_delivery.adapters.guardrail:ZbmGuardrailProvider"
MODEL_CLASS = "zbm_delivery.adapters.model:EgressChatModel"
SANDBOX_TOOLS = ("ls", "read_file", "glob", "grep", "write_file", "str_replace", "bash")

LLM_PROVIDERS = ("anthropic", "openai_compatible", "fake")
PROVIDER_HOSTS = {"anthropic": "api.anthropic.com"}

# Environment allowlist (spec §C.1.7, ST-06): a name outside it refuses start-up. Lower-case proxy names are the
# conventional spellings of the same three variables (ADR 0011 choice).
ENV_PREFIXES = ("DLV_", "LEDGER_SERVICE_")
ENV_NAMES = ("PATH", "HOME", "LANG", "TZ", "DOCKER_HOST", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy",
             "http_proxy", "no_proxy", "SSL_CERT_FILE", "TMPDIR", "DEER_FLOW_SKILLS_PATH",
             # locale companions of LANG: CPython's PEP 538 coercion writes LC_CTYPE into a child's environment
             "LC_ALL", "LC_CTYPE")
# Forbidden names (spec §0.5): gateway/tracing/MCP switches that must never reach this process.
FORBIDDEN_ENV_EXACT = ("DEER_FLOW_AUTH_DISABLED", "DEER_FLOW_INTERNAL_AUTH_TOKEN", "AUTH_JWT_SECRET",
                       "DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST")
FORBIDDEN_ENV_PREFIXES = ("GATEWAY_", "LANGSMITH_", "LANGFUSE_", "MONOCLE_")

_IMAGE_RE = re.compile(r"^(?P<host>[A-Za-z0-9.\-]+(?::[0-9]{1,5})?)/(?P<path>[a-z0-9._/\-]+)@sha256:(?P<digest>[0-9a-f]{64})$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")


@dataclass
class Settings:
    service_token: str
    caller_tokens: dict = field(default_factory=dict)
    andre_token: Optional[str] = None
    data_dir: Optional[str] = None
    ledger_url: Optional[str] = None
    ledger_token: Optional[str] = None
    reconcile_mode: bool = False
    non_production: bool = False
    # runtime (D1, D2, D11)
    runtime: str = "deerflow_embedded"
    sandbox: str = "docker"
    memory: str = "off"
    # deer-flow files and pins
    deerflow_config: str = DEFAULT_DEERFLOW_CONFIG
    extensions_config: str = DEFAULT_EXTENSIONS_CONFIG
    skills_root: str = DEFAULT_SKILLS_ROOT
    prompts_dir: str = DEFAULT_PROMPTS_DIR
    seed_dir: str = DEFAULT_SEED_DIR
    deerflow_commit: str = DEERFLOW_COMMIT
    allow_path_source: bool = False
    # sandbox (D2, §C.2)
    sandbox_image: Optional[str] = None
    image_registry: Optional[str] = None
    sandbox_network: str = "none"            # R4: the only value; the LLM is reached by the engine process, never the box
    sandbox_mem: str = "4g"
    sandbox_cpus: str = "2"
    # clocks and limits (D3, D12)
    run_wall_clock_s: int = 2700
    cmd_timeout_s: int = 600
    subagent_timeout_s: int = 900
    subagent_max_turns: int = 50
    recursion_limit: int = 200
    max_findings: int = 200
    max_rounds_per_finding: int = 5
    max_subagents_per_run: int = 0           # R8: subagents are off in this build; the gate refuses any other value
    # egress + LLM (D4, D5)
    egress_allow_hosts: tuple = ()
    egress_extra_hosts: tuple = ()
    egress_default_timeout_s: int = 10
    egress_llm_read_timeout_s: int = 60
    llm_provider: Optional[str] = None
    llm_api_key_ref: Optional[str] = None
    llm_api_base: Optional[str] = None
    llm_model: Optional[str] = None
    vault: Optional[str] = None
    # git (D8)
    repo_path: Optional[str] = None
    worktrees_dir: Optional[str] = None
    base_ref: str = "integration-2026-09-24"
    # evidence (D10)
    evidence_retention_days: int = 2557
    # ports (D9)
    port: int = 8430
    live_port_range: tuple = (18800, 18849)


def _int(env, name, default, lo, hi) -> int:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        v = int(raw)
    except ValueError:
        v = lo - 1
    if not lo <= v <= hi:
        raise RuntimeError(f"{name} must be an integer {lo}..{hi}")
    return v


def _flag(env, name, default: bool) -> bool:
    raw = (env.get(name) or "").strip()
    if raw == "":
        return default
    if raw not in ("0", "1"):
        raise RuntimeError(f"{name} must be 0 or 1")
    return raw == "1"


def _only(env, name, allowed: tuple, default: str, why: str) -> str:
    raw = (env.get(name) or "").strip().lower() or default
    if raw not in allowed:
        raise RuntimeError(f"{name}={raw[:40]!r}: only {', '.join(allowed)} is built ({why}); refusing to start")
    return raw


def _off(env, name, what: str) -> None:
    raw = (env.get(name) or "").strip().lower()
    if raw not in ("", "none", "0"):
        raise RuntimeError(f"{name}={raw[:40]!r}: {what} is not built in this service; leave it unset (the fail-closed "
                           "stand-in answers 'unavailable')")


def _printable(tok: str) -> bool:
    return len(tok) >= TOKEN_MIN and all(0x21 <= ord(c) <= 0x7e for c in tok)


def _tokens(env, name) -> dict:
    raw = env.get(name)
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except ValueError:
        d = None
    if not isinstance(d, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in d.items()):
        raise RuntimeError(f'{name} must be a JSON object {{"name": "token", ...}}')
    for k, tok in d.items():
        if k not in CALLER_NAMES:
            raise RuntimeError(f"{name}: unknown caller name (allowed: {', '.join(CALLER_NAMES)})")
        if not _printable(tok):
            raise RuntimeError(f"{name}: the {k} token must be >= {TOKEN_MIN} printable ASCII characters")
    return d


def _hosts(env, name) -> tuple:
    raw = env.get(name)
    if not raw:
        return ()
    try:
        d = json.loads(raw)
    except ValueError:
        d = None
    if not isinstance(d, list) or not all(isinstance(h, str) for h in d):
        raise RuntimeError(f"{name} must be a JSON list of host names")
    out = []
    for h in d:
        host, _, port = h.partition(":")
        if not _HOST_RE.fullmatch(host) or (port and not (port.isdigit() and 1 <= int(port) <= 65535)):
            raise RuntimeError(f"{name}: {h[:60]!r} is not a lower-case DNS host name (optionally :port)")
        out.append(h)
    if len(set(out)) != len(out):
        raise RuntimeError(f"{name}: duplicate host")
    return tuple(out)


def env_problems(env: dict) -> list[str]:
    """Names in ``env`` the allowlist does not cover, and forbidden names (spec §C.1.7, §0.5)."""
    bad = []
    for k in env:
        if k in FORBIDDEN_ENV_EXACT or any(k.startswith(p) for p in FORBIDDEN_ENV_PREFIXES):
            bad.append(f"{k} (forbidden: a gateway/tracing/MCP switch)")
        elif not (k in ENV_NAMES or any(k.startswith(p) for p in ENV_PREFIXES)):
            bad.append(k)
    return sorted(bad)


def image_problem(image: Optional[str], registry: Optional[str]) -> Optional[str]:
    if not image:
        return "DLV_SANDBOX_IMAGE is not set (the sandbox image is pinned by digest; there is no default image)"
    m = _IMAGE_RE.fullmatch(image)
    if not m:
        return "DLV_SANDBOX_IMAGE must be <registry-host>/<path>@sha256:<64 hex> (a tag-only reference is refused, ST-03)"
    if not registry:
        return "DLV_IMAGE_REGISTRY is not set (the sandbox image's host must be ours)"
    if m.group("host") != registry:
        return f"DLV_SANDBOX_IMAGE host {m.group('host')!r} is not DLV_IMAGE_REGISTRY {registry!r}"
    return None


def load(env: Optional[dict] = None) -> Settings:
    env = dict(os.environ) if env is None else env
    bad = env_problems(env)
    if bad:
        raise RuntimeError("environment contains names outside DLV_ENV_ALLOWLIST (spec C.1.7, ST-06): "
                           + ", ".join(b[:60] for b in bad[:20]) + "; start the service with a clean environment")
    token = env.get("DLV_SERVICE_TOKEN")
    if not token:
        raise RuntimeError("DLV_SERVICE_TOKEN is not set. delivery-py refuses to start without an auth token "
                           "(fail closed, not open).")
    if not _printable(token):
        raise RuntimeError(f"DLV_SERVICE_TOKEN must be >= {TOKEN_MIN} printable ASCII characters")
    andre = env.get("DLV_ANDRE_APPROVAL_TOKEN") or None
    if andre is not None and not _printable(andre):
        raise RuntimeError(f"DLV_ANDRE_APPROVAL_TOKEN must be >= {TOKEN_MIN} printable ASCII characters")
    callers = _tokens(env, "DLV_CALLER_TOKENS")
    every = list(callers.values())
    if len(set(every)) != len(every):
        raise RuntimeError("DLV_CALLER_TOKENS: every caller token must be distinct")
    specials = [t for t in (token, andre) if t]
    if len(set(specials)) != len(specials) or any(t in every for t in specials):
        raise RuntimeError("the service, Andre and caller tokens must all be distinct (spec §D)")
    non_production = _flag(env, "DLV_NON_PRODUCTION", False)
    runtime = _only(env, "DLV_RUNTIME", ("deerflow_embedded",), "deerflow_embedded",
                    "D1: the harness is embedded in-process; the gateway is never installed")
    sandbox = _only(env, "DLV_SANDBOX", ("docker",), "docker",
                    "D2: Docker only, our provider; no local fallback exists")
    memory = _only(env, "DLV_MEMORY", ("off",), "off", "D11: MemoryOff is the only implementation")
    _off(env, "DLV_VAULT", "the Cybersecurity 22 vault")
    for name in ("DLV_ALLOW_HOST_BASH", "DLV_ALLOW_LOCAL_SANDBOX", "DLV_ALLOW_GIT_REMOTE", "DLV_ALLOW_NETWORK",
                 "DLV_ALLOW_PUSH", "DLV_ALLOW_UNSAFE", "DLV_ALLOW_UNPINNED_CONFIG", "DLV_DEERFLOW_CONFIG_SHA256"):
        if env.get(name) not in (None, ""):
            raise RuntimeError(f"{name}: no such switch exists; nothing unlocks the unconditional denies or the config "
                               "pin (spec C.3.2; round 18 R11: the yaml pin is mandatory in every mode)")
    provider = (env.get("DLV_LLM_PROVIDER") or "").strip().lower() or None
    if provider is not None and provider not in LLM_PROVIDERS:
        raise RuntimeError(f"DLV_LLM_PROVIDER={provider[:20]!r}: only {', '.join(LLM_PROVIDERS)} is built")
    if provider == "fake" and not non_production:
        raise RuntimeError("DLV_LLM_PROVIDER=fake needs DLV_NON_PRODUCTION=1 (a scripted model is never production)")
    key_ref = env.get("DLV_LLM_API_KEY_REF") or None
    vault = (env.get("DLV_VAULT") or "").strip() or None
    if key_ref is not None:
        if not re.fullmatch(r"(env:[A-Z][A-Z0-9_]{0,63}|vault:[A-Za-z0-9._/\-]{1,128})", key_ref):
            raise RuntimeError("DLV_LLM_API_KEY_REF must be env:<NAME> (non-production only) or vault:<ref>")
        if key_ref.startswith("env:"):
            if not non_production:
                raise RuntimeError("DLV_LLM_API_KEY_REF=env:… names an environment variable; that is allowed only with "
                                   "DLV_NON_PRODUCTION=1 (production keys come from the vault, spec D.1)")
            if not key_ref[4:].startswith("DLV_"):
                raise RuntimeError("DLV_LLM_API_KEY_REF=env:<NAME>: the name must start with DLV_ (the env allowlist)")
        if key_ref.startswith("vault:") and vault is None:
            raise RuntimeError("DLV_LLM_API_KEY_REF=vault:… needs a wired vault (DLV_VAULT); none is built, so the key "
                               "is unavailable (LLM_NOT_CONFIGURED)")
    api_base = env.get("DLV_LLM_API_BASE") or None
    if api_base is not None and not re.fullmatch(r"https://[a-z0-9.\-]+(:[0-9]{1,5})?(/[A-Za-z0-9._/\-]*)?", api_base):
        raise RuntimeError("DLV_LLM_API_BASE must be an https URL with a lower-case host")
    if provider == "openai_compatible" and api_base is None:
        raise RuntimeError("DLV_LLM_PROVIDER=openai_compatible needs DLV_LLM_API_BASE")
    model = env.get("DLV_LLM_MODEL") or None
    if model is not None and not re.fullmatch(r"[A-Za-z0-9._:\-]{1,128}", model):
        raise RuntimeError("DLV_LLM_MODEL must be 1-128 characters of [A-Za-z0-9._:-]")
    allow_hosts = _hosts(env, "DLV_EGRESS_ALLOW_HOSTS")
    extra_hosts = _hosts(env, "DLV_EGRESS_EXTRA_HOSTS")
    if provider == "anthropic":
        if PROVIDER_HOSTS["anthropic"] not in allow_hosts:
            raise RuntimeError("DLV_LLM_PROVIDER=anthropic: DLV_EGRESS_ALLOW_HOSTS must list api.anthropic.com")
    provider_host = None
    if provider == "openai_compatible" and api_base:
        provider_host = api_base.split("//", 1)[1].split("/", 1)[0]
    elif provider == "anthropic":
        provider_host = PROVIDER_HOSTS["anthropic"]
    allowed_set = set(extra_hosts) | ({provider_host} if provider_host else set())
    stray = [h for h in allow_hosts if h not in allowed_set]
    if stray:
        raise RuntimeError("DLV_EGRESS_ALLOW_HOSTS lists a host outside {provider host} ∪ DLV_EGRESS_EXTRA_HOSTS: "
                           + ", ".join(h[:60] for h in stray[:5]) + " (spec C.4: nothing else leaves the box)")
    allow_path = _flag(env, "DLV_ALLOW_PATH_SOURCE", False)
    if allow_path and not non_production:
        raise RuntimeError("DLV_ALLOW_PATH_SOURCE=1 needs DLV_NON_PRODUCTION=1 (spec C.1.10)")
    commit = (env.get("DLV_DEERFLOW_COMMIT") or DEERFLOW_COMMIT).strip().lower()
    if commit != DEERFLOW_COMMIT:
        raise RuntimeError(f"DLV_DEERFLOW_COMMIT={commit[:12]}… is not the audited pin {DEERFLOW_COMMIT[:12]}…; moving "
                           "the pin needs a new AEGIS audit folder and an ADR 0011 amendment (spec C.7.4)")
    image = env.get("DLV_SANDBOX_IMAGE") or None
    registry = env.get("DLV_IMAGE_REGISTRY") or None
    problem = image_problem(image, registry)
    if problem:
        raise RuntimeError(problem)
    network = (env.get("DLV_SANDBOX_NETWORK") or "none").strip().lower()
    if network != "none":
        raise RuntimeError(f"DLV_SANDBOX_NETWORK={network[:40]!r}: only none is built (round 18 R4: the sandbox has no "
                           "network at all; the LLM is reached by the engine process on the host, never from the box)")
    sub_n_raw = (env.get("DLV_MAX_SUBAGENTS_PER_RUN") or "").strip()
    if sub_n_raw not in ("", "0"):
        raise RuntimeError("DLV_MAX_SUBAGENTS_PER_RUN: subagents are off in this build (round 18 R8: deer-flow's subagent "
                           "chain carries neither our receipt middleware nor our prompt); only 0 or unset is accepted")
    mem = (env.get("DLV_SANDBOX_MEM") or "4g").strip()
    cpus = (env.get("DLV_SANDBOX_CPUS") or "2").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,3}[mg]", mem):
        raise RuntimeError("DLV_SANDBOX_MEM must be like 512m or 4g")
    if not re.fullmatch(r"(0\.[1-9]|[1-9][0-9]?(\.[0-9])?)", cpus):
        raise RuntimeError("DLV_SANDBOX_CPUS must be 0.1..99.9")
    repo = env.get("DLV_REPO_PATH") or None
    worktrees = env.get("DLV_WORKTREES_DIR") or None
    base_ref = (env.get("DLV_BASE_REF") or "integration-2026-09-24").strip()
    if not repo or not worktrees:
        raise RuntimeError("DLV_REPO_PATH and DLV_WORKTREES_DIR must be set (spec H: the engine needs the repo and a "
                           "worktree directory; there is no default)")
    if not re.fullmatch(r"[A-Za-z0-9._/\-]{1,200}", base_ref) or base_ref.startswith("-") or ".." in base_ref:
        raise RuntimeError("DLV_BASE_REF must be a plain ref name")
    if base_ref in ("main", "master"):
        raise RuntimeError("DLV_BASE_REF=main: the engine never works from main (spec §0.1.4); use the integration branch")
    wall = _int(env, "DLV_RUN_WALL_CLOCK_S", 2700, 60, 86400)
    cmd = _int(env, "DLV_CMD_TIMEOUT_S", 600, 1, 3600)
    sub_t = _int(env, "DLV_SUBAGENT_TIMEOUT_S", 900, 1, 3600)
    sub_n = _int(env, "DLV_SUBAGENT_MAX_TURNS", 50, 1, 50)
    rec = _int(env, "DLV_RECURSION_LIMIT", 200, 10, 200)
    if cmd > wall:
        raise RuntimeError("DLV_CMD_TIMEOUT_S must not exceed DLV_RUN_WALL_CLOCK_S")
    lo_hi = (env.get("DLV_LIVE_PORT_RANGE") or "18800-18849").split("-")
    try:
        lo, hi = int(lo_hi[0]), int(lo_hi[1])
    except (ValueError, IndexError):
        raise RuntimeError("DLV_LIVE_PORT_RANGE must be lo-hi") from None
    if not (1024 <= lo <= hi <= 65535):
        raise RuntimeError("DLV_LIVE_PORT_RANGE must be 1024 <= lo <= hi <= 65535")
    skills_env = env.get("DEER_FLOW_SKILLS_PATH")
    skills_root = env.get("DLV_SKILLS_ROOT") or DEFAULT_SKILLS_ROOT
    if skills_env not in (None, "") and os.path.abspath(skills_env) != os.path.abspath(skills_root):
        raise RuntimeError("DEER_FLOW_SKILLS_PATH is set to a path that is not our skills root (spec 0.4(c).1)")
    return Settings(
        service_token=token, caller_tokens=callers, andre_token=andre,
        data_dir=env.get("DLV_DATA_DIR") or None,
        ledger_url=env.get("LEDGER_SERVICE_URL") or None, ledger_token=env.get("LEDGER_SERVICE_TOKEN") or None,
        reconcile_mode=_flag(env, "DLV_RECONCILE_MODE", False), non_production=non_production,
        runtime=runtime, sandbox=sandbox, memory=memory,
        deerflow_config=env.get("DLV_DEERFLOW_CONFIG") or DEFAULT_DEERFLOW_CONFIG,
        extensions_config=env.get("DLV_EXTENSIONS_CONFIG") or DEFAULT_EXTENSIONS_CONFIG,
        skills_root=skills_root, prompts_dir=env.get("DLV_PROMPTS_DIR") or DEFAULT_PROMPTS_DIR,
        seed_dir=env.get("DLV_SEED_DIR") or DEFAULT_SEED_DIR,
        deerflow_commit=commit, allow_path_source=allow_path,
        sandbox_image=image, image_registry=registry, sandbox_network=network, sandbox_mem=mem, sandbox_cpus=cpus,
        run_wall_clock_s=wall, cmd_timeout_s=cmd, subagent_timeout_s=sub_t, subagent_max_turns=sub_n,
        recursion_limit=rec,
        max_findings=_int(env, "DLV_MAX_FINDINGS", 200, 1, 200),
        max_rounds_per_finding=_int(env, "DLV_MAX_ROUNDS_PER_FINDING", 5, 1, 5),
        max_subagents_per_run=0,
        egress_allow_hosts=allow_hosts, egress_extra_hosts=extra_hosts,
        egress_default_timeout_s=_int(env, "DLV_EGRESS_DEFAULT_TIMEOUT_S", 10, 1, 10),
        egress_llm_read_timeout_s=_int(env, "DLV_EGRESS_LLM_READ_TIMEOUT_S", 60, 1, 60),
        llm_provider=provider, llm_api_key_ref=key_ref, llm_api_base=api_base, llm_model=model, vault=vault,
        repo_path=repo, worktrees_dir=worktrees, base_ref=base_ref,
        evidence_retention_days=_int(env, "DLV_EVIDENCE_RETENTION_DAYS", 2557, 2557, 36500),
        port=_int(env, "DLV_PORT", 8430, 1024, 65535), live_port_range=(lo, hi),
    )
