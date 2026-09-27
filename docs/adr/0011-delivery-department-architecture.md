# ADR 0011 — Client Delivery & Operations (28): agent runtime adapters and the fix engine (`services/delivery-py`)

- **Status:** accepted for build (Sep 27, 2026). Built, tested (391 tests: 388 passed, 3 skipped with the printed
  reason) and live-run on this box; **not certified for a fix run against `main`** and not certified for any run
  at all until the Docker properties of §C.2 are proven on a machine with a daemon (see "Known limitations") and a
  provider key exists.
- **Spec:** `DEPT28_SPEC.md` rev 1 (binding), the AEGIS audit of deer-flow v2.1.0 / Superpowers v6.4.2
  (`scratchpad/audit28/VERDICT.md` + four reports), `FIX_WAVE_1_COMMON.md`, BUILD_CONTRACTS.md, ADR 0006 / 0009.
- **Base:** branch `delivery-department` from `integration-2026-09-24` @ `71f121e`.
- **Third-party pins:** deer-flow `345f08be00c8a9495079b732a39b46aa9af1584e` (tag v2.1.0); Superpowers
  `8ca22dba9a94f28898bbce59f2537ff4d87c747d` (v6.4.2, prompt texts only).

## Context

The department runs the AEGIS fix engine: it ingests a findings document, opens an engineer worktree, runs one
agent engineer per finding inside our Docker sandbox under our guardrails, has the ENGINE (never the agent) run
the failing test, the passing test, the revert check and the whole suite, commits on a `fix<N>-<service>` branch,
writes a report from its records and hands the run to AEGIS re-review. It also owns the runtime adapters every
later agent department will use. The audit's ruling (VERDICT) was "adopt with adapters, no fork of runtime code";
this ADR records the adapters, the choices the spec left open, and what could not be proven here.

## Decision

Embed the deer-flow **harness** in-process (`deerflow.client.DeerFlowClient`; D1) with our classes at every seam
the harness exposes by class path — sandbox provider, guardrail provider, model class — plus our LangChain
middlewares passed in code; never install the gateway, nginx, the frontend or the IM channels. A FastAPI + pydantic
v2 service with the house conventions of finance-py (hardened `serve.py`, refuse-to-start config, bearer + caller
tokens, FounderGate on one route, per-route body limits, strict models, 15-minute `request_id` idempotency,
record-first on the evidence ledger — department `delivery`, ids `dlv-<abbrev>-<40 hex>` — and a hash-chained,
ledger-anchored local log with the instance lease and Andre's reconcile).

### Module map (`services/delivery-py`)

| Path | Role |
|---|---|
| `pyproject.toml`, `uv.lock` | the dependency overlay (spec 0.3): the harness from the pinned git source, the five dropped packages overridden with `sys_platform == 'never'`, the audited langchain/langgraph family pinned (choice 1) |
| `src/zbm_delivery/config.py` | `load(env)`: pure, fails closed; the env allowlist; every `DLV_*` setting; the pinned hashes |
| `src/zbm_delivery/gate.py` | start-up gate: the deer-flow YAML checks (C.1.3, G10), manifests (skills, prompts), extensions config, seeds, forbidden modules, the deer-flow commit (`direct_url.json`), the licence gate, repo/worktree dirs |
| `src/zbm_delivery/adapters/sandbox.py` | `ZbmDockerSandboxProvider` / `ZbmDockerSandbox` (C.2): fixed docker argv, tar-stream copy-in as uid 65532, record-first exec, path containment with in-container `readlink -f`; `RealDockerCli` |
| `src/zbm_delivery/adapters/guardrail.py` | `ZbmGuardrailProvider` (C.3): run lookup by thread, identity check, classification, `tool_call_decided` recorded first, deny on record failure |
| `src/zbm_delivery/adapters/receipts.py` | `ZbmToolReceiptMiddleware` (C.3.4): `tool_result_recorded` after every tool result, before the model sees it |
| `src/zbm_delivery/adapters/prompt.py` | `ZbmSystemPromptMiddleware`: the engineer's system prompt is ours (choice 9) |
| `src/zbm_delivery/adapters/egress.py` | `EgressClient` (C.4): the only outbound client; allowlist before DNS; record-first; no redirects; caps; retry policy |
| `src/zbm_delivery/adapters/model.py` | `EgressChatModel(BaseChatModel)` + the two hand-written wire backends (Anthropic Messages, OpenAI-compatible); key at call time; no SDK |
| `src/zbm_delivery/adapters/identity.py` | `Principal` per run; deer-flow user context bound around every turn; never `"default"` |
| `src/zbm_delivery/policy.py` | the tool-call classifier over `seed/tool_policy_seed.json` (B.5) |
| `src/zbm_delivery/registry.py` | the process-level bridge between the service and the classes deer-flow instantiates by class path; per-run bindings with the in-memory run token |
| `src/zbm_delivery/gitport.py` | `GitPort`: fixed argv, the allowlisted subcommands only, attribution trailer |
| `src/zbm_delivery/runner.py` | `TestRunner`: the seeded test/suite argv, no shell, framework detection, counts parsed |
| `src/zbm_delivery/engine/states.py` | run and finding states and the transition invariants (B.2, B.3; G4) |
| `src/zbm_delivery/engine/parsers.py` | the strict reply-line parser and the pytest/cargo/go/npm suite parsers |
| `src/zbm_delivery/engine/brief.py` | the brief compiler (C.8.3) and the system prompt assembly from `prompts/` |
| `src/zbm_delivery/engine/report.py` | the report writer (C.8.6): records only, never the agent's prose |
| `src/zbm_delivery/engine/loop.py` | `FixEngine`: prepare → sandbox → suite before → per finding → suite after → report; the exception boundary |
| `src/zbm_delivery/harness.py` | the `DeerFlowClient` construction, the deer-flow env preparation, the provider singleton |
| `src/zbm_delivery/service.py` | state (event-sourced), record-first plumbing, ingest, views, review, cancel, audit export, reconcile, the worker thread |
| `src/zbm_delivery/api.py` | routes, auth, input limits, `build_service` wiring |
| `src/zbm_delivery/{ledger,store,evidence_audit,founder,textguard,clock,errors,reasons,fsops,licences,models,ports,serve}.py` | copied/ported house modules; `fsops` is the one deletion site (G6); `licences` the licence gate; `ports` the stand-ins and `MemoryPort`/`MemoryOff` |
| `config/deerflow.engine.yaml`, `config/extensions_config.json` | the pinned harness configuration and the (empty) extensions file |
| `seed/*.json` | tool policy, test commands, licence allowlist/exceptions, skills manifest, prompts manifest — all hash-pinned in `config.py` |
| `prompts/` | the Superpowers forks + ours (`engine.system.md`, `brief.template.md`) + `CHANGES.md` |
| `skills/custom/.gitkeep` | the empty skills root (the manifest lists this one file) |
| `docker/Dockerfile`, `docker/sandbox.Dockerfile` | the service image and the sandbox image (digest is a required build argument) |
| `devtools/{gen_manifests,licence_gate,audit_gate,evidence_pack}.py` | manifest/pin generation, the licence gate CLI, pip-audit gate, the evidence pack |
| `fixtures/dlv/toy-py/` (repo root) | the fixture service with two planted defects the certification suite fixes |

### Pinned hashes (spec §I)

| What | SHA-256 |
|---|---|
| `config/deerflow.engine.yaml` | `798529f704cb03fbbc9d30f38a4efb844593ac193fec7e94059bb43878c90c51` |
| `config/extensions_config.json` | `4875b2992e9062d6f8084ac64d543f50a29624bd0e7eb82586e31b3056563807` |
| `seed/skills_manifest.json` | `26f51402a23232a6b3f6a5764829800c3570403e2694ee1a9f331fe23e040320` |
| `seed/prompts_manifest.json` | `064a5e3e3b3ea696cce1a586eef792a9a73e26ed8b45dabe9dfa20fa2d295440` |
| `seed/tool_policy_seed.json` | `5c8ac4620ab2ebdc961fb8e0bd00567fab0a25a500d8d9e8880786f7299b47d1` |
| `seed/test_commands_seed.json` | `019cfba7c9bdb11130f0bae0f7b884c2de0b9e551a254550e189f2e7d3c2a112` |
| `seed/licence_allowlist.json` | `010eef6c7bfc084426303513372debf845e003df85e54220dc43a355fe31bc0b` |
| `seed/licence_exceptions.json` | `53b6f62c221517143db32c4f6a27b40cb3efbfa68de7b073cf0981f138d4104d` |
| `docs/evidence/licences-2026-09-27.json` (the licence report) | `12f02de71571aef82b7293a6e432552a72ffb1f0ca692e951d9a9178e462e51f` |
| `uv.lock` | `f01aa750572f5b6662b8cb1b52d370574474d83379b137bee44e9e183264457b` |
| deer-flow commit (`DLV_DEERFLOW_COMMIT`, G9) | `345f08be00c8a9495079b732a39b46aa9af1584e` |
| service image digest | **not resolvable here** (Docker Hub is refused by this box's proxy; no daemon): the digest is a required `--build-arg BASE_DIGEST` and is recorded here at first build |
| sandbox image digest | **not resolvable here** (same); `DLV_SANDBOX_IMAGE` must name it |

Every prompt file's sha256 beside its original's hash: `docs/evidence/dept28/prompts-sha256.txt`.

### Resolved dependency versions (spec 0.3, §G.8)

anyio **4.15.1** (floor ≥ 4.14.2 held), langchain-anthropic **1.4.6** (floor ≥ 1.4.6 held, exactly), soupsieve
**2.10** (floor ≥ 2.9.0 held), websockets 16.0, langgraph 1.2.9, langchain 1.3.14, langchain-core 1.4.9,
langchain-openai 1.2.1, langgraph-checkpoint 4.1.1, langgraph-prebuilt 1.1.0, deerflow-harness 2.1.0,
deerflow-extension-api 0.2.1, fastapi 0.141.1, pydantic 2.13.3, uvicorn 0.46.0, httpx 0.28.1, tiktoken 0.14.0.
`uv lock` ran on this box against the real git source (the proxy allowed the fetch); `uv lock --check` passes;
`uv export --frozen` lists the five dropped packages with `sys_platform == 'never'` and none is in site-packages
(`docs/evidence/dept28/dependencies.txt`). pip-audit: **not run** (pip-audit is not on this box and is not a
runtime dependency; `devtools/audit_gate.py` reports `audit: not_run`, exit 2 — the build is not green on that
gate).

### The §0.4 table, so this ADR stands alone

| Seam | Our class / key | Where deer-flow reads it |
|---|---|---|
| SandboxProvider | `sandbox.use: zbm_delivery.adapters.sandbox:ZbmDockerSandboxProvider`, `allow_host_bash: false`, `image: $DLV_SANDBOX_IMAGE` (digest on our registry) | `sandbox/sandbox_provider.py:181-182` `resolve_class`; `config/sandbox_config.py:174-185` |
| GuardrailProvider | `guardrails: {enabled: true, fail_closed: true, provider.use: zbm_delivery.adapters.guardrail:ZbmGuardrailProvider}` | `agents/middlewares/tool_error_handling_middleware.py:261-281` |
| Model | `models: [{name: engine, use: zbm_delivery.adapters.model:EgressChatModel, model: engine}]` | `models/factory.py:208,343` |
| Receipts + prompt | `DeerFlowClient(middlewares=[ZbmSystemPromptMiddleware, ZbmToolReceiptMiddleware])` | `client.py:189`, `build_middlewares` |
| Identity | `client.stream(..., user_id="zbm--<run_id>")` inside `set_current_user` / `reset_current_user` | `client.py:84-94, 945-957`; `runtime/user_context.py:56, 98` |
| Memory off | `memory: {enabled: false, injection_enabled: false, manager_class: noop}` | `config/memory_config.py:58-98`; `agents/lead_agent/agent.py:645-654` |
| Tools | the sandbox group only (`ls`, `read_file`, `glob`, `grep`, `write_file`, `str_replace`, `bash`) | `tools/tools.py:73-201` |
| Skills | `skills.path: $DLV_SKILLS_ROOT` (empty root, hash manifest), `extensions_config.json` pinned, `available_skills=set()` | `config/skills_config.py:40-64`; `client.py:188` |
| Extensions | not loaded (`plugins: []`, `extensions.mcp_servers: {}`); our middlewares are passed in code | `config/reload_boundary.py:46` |

## Numbered choices where the spec was silent or the facts differed (safest option taken)

1. **Dependency family pinned to the audited versions.** The spec's literal TOML resolved langgraph 1.2.12 /
   langchain 1.4.2 / langchain-core 1.6.5 / langchain-anthropic 1.7.4; deer-flow itself warns at import that its
   InMemorySaver patch "was validated against 1.2.9". The overlay pins langgraph 1.2.9, langchain 1.3.14,
   langchain-core 1.4.9, langgraph-checkpoint 4.1.1, langgraph-prebuilt 1.1.0, langchain-anthropic 1.4.6,
   langchain-openai 1.2.1, langchain-google-genai 4.2.2, langchain-deepseek 1.0.1, langchain-mcp-adapters 0.2.2 —
   the set the audit's 17,024 tests ran against. All three security floors still hold (above). A bump of any of
   these is a re-audit (§C.7.4).
2. **Package layout.** The service is a package (`src/zbm_delivery/`) because the harness reaches our classes by
   dotted class path (`zbm_delivery.adapters.sandbox:...`); the entrypoint is `cd src && python3 -m
   zbm_delivery.api` (the other services use `python3 -m api`). `api.py`, `config.py`, `ledger.py`, `store.py`
   import nothing from `deerflow`.
3. **The workspace path is `/mnt/user-data/workspace`, not the spec's `/workspace`.** deer-flow's tools
   hard-code its virtual prefix (`config/paths.py:12`; the bash tool prepends `cd /mnt/user-data/workspace`) —
   with the volume at `/workspace` every tool would address a path that does not exist. Spec defect, reported.
4. **User id shape `zbm--<run_id>`, not `zbm:<run_id>`.** deer-flow's `_validate_user_id` (`config/paths.py:35`)
   allows only `[A-Za-z0-9_-]`. Spec defect, reported.
5. **deer-flow's per-turn `provider.release()` is a lease return.** `SandboxMiddleware.after_agent` releases the
   sandbox after EVERY agent turn; ours keeps the container for the run and tears it down (`docker rm -f` +
   `docker volume rm`) only in `destroy`, called by the runner after the evidence and the diff are out.
6. **Skill isolation flag.** `supports_agent_skill_isolation = True` on our provider: deer-flow refuses to run
   with `available_skills` set (the prompt-level filter of §0.4(c).4) unless the provider claims it; our mount is
   the hash-pinned empty root, so the claim is true by construction.
7. **In-container `timeout -k 5 <secs>`** bounds every exec (min of the caller's timeout, `DLV_CMD_TIMEOUT_S`
   and the remaining run wall clock) with a host-side subprocess timeout as the backstop; `docker exec` has no
   timeout flag and we never kill by pattern.
8. **Copy-in is a tar stream owned by uid 65532** (`docker cp - <container>:/mnt/user-data/workspace`), so the
   engineer can write without a `chown` (which `--cap-drop=ALL` would refuse); copy-out is `docker cp
   <container>:<path> -` extracted with the `data` filter; only `services/<service>` and `docs/adr` come back.
9. **The engineer's system prompt replaces deer-flow's lead-agent template** (a `wrap_model_call` middleware):
   the DF template carries guidance contrary to the engine's rules (installing packages, skills, memory). The
   prompt is `prompts/engine.system.md` + the five skill forks + the environment and the policy summary; its
   sha256 is recorded in `prompts_loaded` and it is evidence (`prompt_manifest`).
10. **One run in flight per process** (a single worker thread), which satisfies "one run at a time per service"
    and keeps the model backend process-global; a second run for the same service is 409 `RUN_IN_PROGRESS`,
    another service's run queues.
11. **`DLV_LLM_PROVIDER=fake` means "a scripted backend injected by the embedding process"**: with none injected
    the backend is `NoChatBackend` → `LLM_NOT_CONFIGURED`. `FakeChatModel` lives in `tests/fakes.py` and drives
    the REAL `EgressChatModel` through the `ChatBackend` port; nothing fake is importable from `src/` (G3).
12. **The revert check stashes with `--include-untracked`** (`git stash push --include-untracked -- <non-test
    paths>`), so a new non-test module the engineer created is reverted too; the sandbox service directory is
    replaced from the host worktree before each of the two runs.
13. **`for-each-ref` is a GitPort read** (to compute D8's `N`); the spec's subcommand list omitted it. `commit
    --no-gpg-sign` and `--quiet` are the only extra flags.
14. **A suite failure attributable to ANOTHER not-yet-fixed finding of the same run may remain** (its file equals
    that finding's file, or its node id is named in that finding's reproduction); every other failure blocks
    `fixed` and is a `new_defect` on the run.
15. **A disproof "demonstrably contradicts" the finding** when the engine's reproduction (a seeded test binary
    argv, no shell, no `python -c`) exits 0 and the statement is ≥ 40 characters; it is flagged "disproof —
    verify" for the reviewer, who re-runs it. The engine cannot judge semantics.
16. **A recursion-limit overflow of a turn is a failed round**, not a run failure (the model looped without a
    contract line); D3's limit is passed to `stream(recursion_limit=...)`.
17. **`ln`/`cp`/`mv` write only their last operand**; a source outside the workspace is a read (the sandbox's
    own filesystem), the destination must be inside. A recursive `rm` target is resolved with `readlink -f`
    INSIDE the sandbox before the decision (a symlink escape is denied).
18. **Env allowlist companions.** `LC_ALL` and `LC_CTYPE` are allowed beside `LANG` (CPython's PEP 538 coercion
    writes `LC_CTYPE` into a child's environment); lower-case proxy names beside the upper-case ones.
19. **The service sets deer-flow's environment itself** after the gate: `DEER_FLOW_EXTENSIONS_CONFIG_PATH`,
    `DEER_FLOW_HOME` (thread data under the data dir), `DEER_FLOW_CONFIG_PATH`, `DLV_SKILLS_ROOT`,
    `DLV_SANDBOX_IMAGE` (the YAML references the last two as `$NAME`; deer-flow substitutes at load).
20. **An empty local log with an unreadable ledger starts** (spec C.1.2: `/health` says `ledger: unconfigured`,
    every run 503); a non-empty log with an unreadable ledger refuses to start (finance-py rule).
21. **A run found live at restart is recorded `fix_run_failed`** (S9: no run survives a restart); a run whose
    ledger write fails mid-flight is marked failed in memory only (`unrecorded_failure: true`) and recorded at the
    next start.
22. **Licence allowlist additions** beyond D13: `MIT-0` (cffi), `MIT-CMU` (pillow), `Zlib` and `CC0-1.0`
    (numpy's bundled components), `CNRI-Python` (regex) — all permissive, all needed by the resolved runtime set;
    recorded in the seed with the reason.
23. **Licence exceptions beyond the spec's two.** The spec says the exception list "today must list only
    lark-oapi and agent-client-protocol"; the resolved set needed `deerflow-harness` and `deerflow-extension-api`
    (git-built dist-info with no licence metadata; the repository LICENSE at the pin is MIT, sha256
    `b23dff4d…`), `protobuf-py-ext` (the native extension of `protobuf-py`, Apache-2.0 by the sibling's
    expression and the repository LICENSE) and `sqlite-vec` (a comma-list License field). Exception kinds
    `file` / `sibling` / `stated` / `license_field` are defined in the seed. `lark-oapi` is not in our runtime
    set at all (gateway-only) — its entry is inert.
24. **G5's `gh ` check** is a word-boundary match (`(^|\s|quote)gh\s`), since a naive substring also matches
    "through " and "enough ". The forbidden strings are therefore named obliquely in `prompts/CHANGES.md`.
25. **Every `read_before_write` gate of deer-flow stays on** (its default): a real engineer reads before editing;
    the scenarios do too.
26. **Andre's token is honoured on `POST /dlv/v1/reconcile` only** (§C.3.3); it is not an identity on any other
    route (a request carrying only it is 403).
27. **The fixture repository lives at `fixtures/dlv/toy-py/`** (repo root, as the spec names it) and is copied into
    a temporary git repository by the tests; nothing in it is production code.
28. **Reruns reuse the original worktree** (a branch cannot be checked out in two worktrees); the child run's
    `base_sha` is the branch head and its findings are the reopened ∪ new set.

## Spec items not built, and why

- **Docker live properties (§C.2 list).** No daemon on this box; `tests/test_live_docker.py` skips with the exact
  reason and lists the seven unproven properties. Proven here only by the argv-level double.
- **pip-audit (§C.7.3).** Tool absent; `audit: not_run` reported, not green.
- **Image digests (§C.7.5).** Not resolvable (registry blocked); both Dockerfiles require the digest as a build
  argument and fail without it.
- **A retention job (D10)** — a later scheduler job; the number is recorded (`DLV_EVIDENCE_RETENTION_DAYS=2557`).
- **The vault (D.1)** — `NotWiredVault`; `env:` key references only with `DLV_NON_PRODUCTION=1`.
- **A real memory adapter (§C.6)** — the `MemoryPort` seam with `MemoryOff` and the tested contract only.
- **OpenBot / ECC (R28-4)** — not audited, not built.

## Known limitations

- The tool-call classifier is a denylist over an unbounded language (df-exec F-03): it is the record and the
  first refusal; the BOUNDARY is the sandbox (non-root, `--cap-drop=ALL`, default seccomp, `--read-only`,
  `--network <internal>`, no socket mount) — and those properties are unproven on this box.
- No retention job; one run in flight per process; a harness crash is a run failure (worker-thread exception
  boundary), never a service crash; a ledger failure mid-run leaves the run failed in memory and unrecorded until
  the next start.
- deer-flow swallows a failing model call into an error turn: an LLM timeout ends as a blocked finding after the
  rounds run out (the run fails, never hangs) rather than as an immediate `HARNESS_ERROR`.
- `tiktoken` is imported at module level by `langchain_openai` (spec 0.4(a) caveat); the suite asserts its loader
  never runs; nothing downloads.

## Spec defects found

1. `/workspace` as the volume mount (§C.2) — deer-flow addresses `/mnt/user-data/workspace` (choice 3).
2. `tenant:run_id` as the user id (§C.5) — refused by deer-flow's user-id validator (choice 4).
3. `git worktree add <path> -b <branch> <sha>` argv order (§C.8.2) — git needs `-b <branch>` before the path;
   the port uses `worktree add -b <branch> -- <path> <sha>`.
4. The exception list "must list only lark-oapi and agent-client-protocol" (§C.7.2) — false for the resolved set
   (choice 23); D13's allowlist misses five permissive ids the set needs (choice 22).
5. The spec's TOML resolves a langchain/langgraph family newer than the one audited (choice 1).
6. G5's literal `gh ` grep would fail on ordinary English (choice 24).
7. §F A6 asks that "delete tests/" be stopped by A2: a delete INSIDE the service directory is not outside the
   workspace; it is a changed test the `CHANGED_TEST:` rule and the review catch (the test targets the repo root
   through `..`, which A2 denies).

## Routes

`GET /health` · `POST /dlv/v1/fix-runs` (aegis, andre_session) · `GET /dlv/v1/fix-runs/{id}` · `…/findings` ·
`…/report` · `…/evidence/{evidence_id}` · `POST …/review` (aegis) · `POST …/cancel` (aegis, andre_session) ·
`GET /dlv/v1/policy` · `GET /dlv/v1/audit/export` · `GET|POST /dlv/v1/reconcile` (Andre token,
`DLV_RECONCILE_MODE=1`). Every POST answer echoes `request_id` and `facts_sha256`; every answer carries
`policy_version` and `prompts_manifest_sha256`.

## Testing

`cd services/delivery-py && .venv/bin/python -m pytest -q` (no network; the real deer-flow harness, the real
guardrail, the argv-level Docker double, a scripted model, a temporary git repository). 391 tests: 388 passed,
3 skipped (the Docker live module, reason printed), 201 s wall. `ruff check src tests devtools` clean.
Evidence: `services/delivery-py/docs/evidence/dept28/` and `docs/evidence/licences-2026-09-27.json`.
