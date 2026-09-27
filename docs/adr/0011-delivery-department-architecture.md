# ADR 0011 — Client Delivery & Operations (28): agent runtime adapters and the fix engine (`services/delivery-py`)

- **Status:** accepted for build (Sep 27, 2026); amended the same day by fix wave 19 (AEGIS round 18, rulings
  R1-R11 — see "Round 18 amendments" below) and by the toolchain amendment (engine-owned verdicts for Go, Rust
  and Node — see "Toolchain amendment" below). Built, tested (449 tests: 446 passed, 3 skipped with the printed
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
| `src/zbm_delivery/runner.py` | `TestRunner`: the seeded test/suite argv, no shell, framework detection, the verified run (report + listing + transcript + exit), path classes, content rules |
| `src/zbm_delivery/engine/toolchains.py` | the per-ecosystem result adapters (pytest, go, cargo, node): target expansion, engine options, collect-only equivalent, the cross-checked verdict |
| `src/zbm_delivery/engine/states.py` | run and finding states and the transition invariants (B.2, B.3; G4) |
| `src/zbm_delivery/engine/parsers.py` | the strict reply-line parser, the pytest junit/collect/summary cross-check, Node's junit reader, the summary-only fallbacks (never `ok`) |
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
| `config/deerflow.engine.yaml` | `e4d51379594e0f7dc2265a0fee0ff25aa7134b1e6aecabe91b291a1d18064f0e` (wave 19: `subagents.max_total_per_run: 1`, deer-flow's floor, with subagents disabled in code) |
| `config/extensions_config.json` | `4875b2992e9062d6f8084ac64d543f50a29624bd0e7eb82586e31b3056563807` |
| `seed/skills_manifest.json` | `26f51402a23232a6b3f6a5764829800c3570403e2694ee1a9f331fe23e040320` |
| `seed/prompts_manifest.json` | `a55a2a0f6ef5979a4ff4df702b126bb3d43d05c208cbae6d10e0da8bc420ee1e` (wave 19: `engine.system.md` rules 2 and 5 restated for R1/R3) |
| `seed/tool_policy_seed.json` | `5c8ac4620ab2ebdc961fb8e0bd00567fab0a25a500d8d9e8880786f7299b47d1` |
| `seed/test_commands_seed.json` | `18b4cdc0e913c8e7a71492d22499ddd30a47a4a8a0455f958a8411112a8f6cd5` (toolchain amendment: go/cargo/npm `verified: true` with their engine argv, `collect`, `target_example`, `test_content_deny`, per-ecosystem `test_infra_globs`; pytest markers no longer include a bare `tests/`; `service_env` gains the toolchain determinism switches) |
| `seed/licence_allowlist.json` | `c02f367f3e6cd02949e18dbc2eaa2ceabcb917ac4aa5fc58ad73bec4ac6dfb6e` (wave 19: `unrecorded_allow`) |
| `seed/licence_exceptions.json` | `10695daf388308309d5b32057e9d0b764817b80979e8459db68ad5b48ac0ef62` (wave 19: `dotenv`, `tiktoken`) |
| `docs/evidence/licences-2026-09-27.json` (the licence report) | `f154e1b56befac3a939d69cfeece434516d172fa0c6aedbfbaeab464d1ee0e8d` (wave 19: dotenv + tiktoken via file exceptions) |
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

## Round 18 amendments (fix wave 19, Sep 27, 2026; lead rulings R1-R11, binding)

The round proved the perimeter and disproved the fix semantics: a lazy agent reached `awaiting_review` with the
defect intact through a `conftest.py` monkeypatch, a forged pytest summary, a neutered `pytest.ini`, a deleted test,
a trivial `DISPROOF:` and a suite that timed out. The principle now in force: **the engine never trusts anything
the agent's own process can emit** — test output, summary lines, exit codes of agent-chosen commands or the test
tree's configuration. Every verdict is computed by the engine from artefacts it controls, and where that is
impossible the state is `unknown`, never green.

- **R1 — split-diff verification** (`engine/loop.py::_green_phase`, `runner.py::classify_paths`). After `FIXED`
  every changed path is classified `src` / `test` (the seeded test globs) / `test_infra` (`conftest.py` at any
  depth, `pytest.ini`, `pyproject.toml` when its `[tool.pytest*]` sections changed, `setup.cfg`, `tox.ini`,
  `.pytest_cache`, `sitecustomize.py`, `usercustomize.py`, `*.pth`, `setup.py`, `.coveragerc`; cargo: `Cargo.toml`,
  `Cargo.lock`, `build.rs`, `.cargo/`, `rust-toolchain*`; go: `go.mod`, `go.sum`, `go.work*`, `testdata/`; npm:
  `package.json`, the lockfile, `.npmrc`, jest/vitest/babel/mocha configs, `tsconfig*.json`). Any test-infra change
  fails the round `test_infra_changed`; a deleted existing test file fails it `test_deleted` (a test may be changed
  under `CHANGED_TEST:`, never removed); no source change fails it `no_source_change`. The engine then builds two
  fresh checkouts from `git archive HEAD -- services/<svc>` on the host and ships them into the sandbox under
  `/mnt/user-data/workspace/.dlv-verify/`: the **verification checkout** (HEAD tree + the `src` changes + the RED
  test file only) must PASS and the **reverted checkout** (HEAD tree + the RED test file only) must FAIL. GREEN in
  the agent's tree but RED in the verification checkout is `fix_not_in_source`. The stash-based revert check is
  replaced by the reverted checkout (the `stash` form stays in `GitPort`, unused by the engine). The classification
  and both outcomes are recorded `verification_run` and kept on the finding (`verification`, `revert_check`);
  `states.py` refuses `fixed` unless the checkout passed and the reverted one failed.
- **R2 — engine-owned invocation and counts** (`runner.py`, `engine/parsers.py::verified_counts`). Every pytest
  run (suite, RED, GREEN, both checkouts, disproof) carries `-c <engine ini>` (written by the engine under
  `.dlv-engine/<nonce>/`, `addopts` empty, absolute `pythonpath`/`testpaths` — pytest resolves `paths`-typed ini
  values against the ini file's directory), `--rootdir=<cwd>`, `-o addopts= -o python_files= -o testpaths= -o
  pythonpath=` from the seed, `-p no:cacheprovider`, `-rfE`, `--junitxml=<engine path>` (xunit1 family). The
  repository's own pytest configuration is never read. The junit file is read back with `docker cp` (the daemon,
  not a process in the box) and must agree with a separate `--collect-only -q` run (testcase count == collected),
  with the summary line (counts and `FAILED` names) and with the exit code (0 ⇔ no failure/error). Any
  disagreement, a missing/unparseable junit, exit 5, exit 124 / timeout or a truncated capture is `status:
  unknown` — never a valid RED, never GREEN, never a green suite; `suite.after` must be `ok` with no failure or the
  run ends `failed` (`SUITE_UNKNOWN` / `SUITE_NOT_GREEN`; a pre-existing failure nobody fixed can no longer ride
  a disproved finding to `awaiting_review`). cargo/go/npm had no engine-owned report in wave 19; the toolchain
  amendment below builds one for each. **Accepted residual:** a test module runs as the same uid in the same
  container as the engine's junit file; agent code inside pytest can read `sys.argv`, find the path and rewrite
  it. It must then also forge the collect-only count (a second process), the summary line and the exit code, and
  R1 rejects the only places (`conftest.py`, `pytest.ini`) where that code could live outside the RED test file
  — whose semantics AEGIS re-review reads.
- **R3 — a disproof is the finding's own reproduction** (`engine/loop.py::_disproof`,
  `runner.py::node_id_in_text`). `DISPROOF:` is honoured only when the findings document names a test node id in
  the finding's `reproduction` text; the engine runs the SEEDED argv with that target in a fresh checkout of the
  run's `base_sha` (the agent's argv is recorded `agent_argv_ignored`, never run) and the finding is `disproved`
  only when that reproduction PASSES there (the declared failure does not occur) with a statement ≥ 40 characters.
  A finding with no machine-runnable reproduction cannot be disproved by the engine (`disproof_not_machine_runnable`)
  and stays open for AEGIS. The report lists disproved findings in their own section with the argv, the base sha
  and the evidence id.
- **R4 — the sandbox network is `none`, full stop** (`config.py`, `adapters/sandbox.py`, `gitport.py`,
  `docker/sandbox.Dockerfile`). `DLV_SANDBOX_NETWORK` accepts only `none` (default); the argv is `--network none`;
  `forbidden_run_token` matches the joined argv against `--network(=|\s)+(host|bridge|container:…)` and any `--net`.
  The sandbox image purges `curl`/`wget` after the toolchain install (the live test asserts `command -v` fails and
  `docker inspect` says `NetworkMode: none`). The copy-in stream excludes `.git` (a linked worktree's `.git` is a
  FILE and was shipped before) and the engine verifies `test ! -e /mnt/user-data/workspace/.git` after copy-in.
  `GitPort.worktree_add` refuses when the repository or the new worktree lists any remote (`git remote`) — a
  linked worktree shares the repository's config, so **`DLV_REPO_PATH` must be a remote-less local repository**;
  the (empty) listing is recorded in `worktree_created`.
- **R5 — opaque exec is opaque** (`policy.py`, `adapters/guardrail.py`). An interpreter / shell / `make` /
  `find -delete` / `git -c|--git-dir|--work-tree` / `pytest` / `cargo|go|npm` invocation is `decision:
  allow_opaque`, class `exec`, `opaque: true` in `tool_call_decided` — a distinct decision value, never a
  classified `allow` or `read`; the binding counts them and the report prints the count per finding. Every
  write-capable operand (`rm`, `cp`/`mv`/`ln`/`install` destinations, `tee`, `touch`, `mkdir`, `chmod` paths,
  `sed -i` files, `dd of=`, `find -delete` start, redirections) is normalised against the cwd after `cd` (relative
  operands too), must land inside `services/<service>/` or `docs/adr/` (no longer anywhere in the workspace or
  `/tmp`), and anything unresolvable before execution (`* ? [ { } $ ~` and backticks) is refused
  `destructive_outside_workspace`. The guardrail then resolves the same operands INSIDE the container and denies
  when the resolver answers None or the path lands outside. The direct-form denies are unchanged (p1 re-run: 18 of
  38 forms allowed, every one of them an honestly recorded opaque exec, an in-service `ln`/`chmod` or a
  background `bash script &`, which is opaque too).
- **R6 — deadlines and reaping** (`adapters/egress.py`, `engine/loop.py`, `service.py`, `adapters/sandbox.py`).
  The egress client streams every response against a TOTAL wall-clock deadline (`default_timeout_s`;
  `llm_read_timeout_s` for LLM calls; never more than the run's remaining wall clock, which the model backend
  passes as `deadline_s`) and against the byte cap chunk by chunk; `abort(run_id)` closes every response in
  flight. A watchdog thread per run fails the run on the wall clock while a turn is in flight (`fix_run_deadline`),
  aborts its egress and flags the loop (`run_interrupted`); `POST …/cancel` does the same. `destroy` checks both
  exit codes and records `sandbox_release_failed` (raising) on failure; the container and the volume carry the
  label `zbm.dlv.run=<run_id>`; `sandbox.reap` removes everything with that label at service start and on
  `stop()`, one `sandbox_reaped` event (recorded first) per removal — never by name pattern.
- **R7 — fail closed on containment** (`adapters/sandbox.py`). `realpath` resolves the longest existing prefix
  with `readlink -f` inside the container; a path that cannot be resolved at all is refused (reads and writes).
  A write lands in `.dlv-stage/<nonce>/` first and is `mv`ed into place after the destination parent is
  re-resolved (twice: before and after `mkdir -p`). **Residual:** the window between the last `readlink` and the
  `mv` is milliseconds and needs a concurrent process inside the box (a `bash script &` is an opaque exec the
  agent can run); closing it needs `openat2(RESOLVE_BENEATH)` semantics no shell utility offers — documented, not
  closed.
- **R8 — subagents off** (`config.py`, `harness.py`, `gate.py`, the yaml). `DLV_MAX_SUBAGENTS_PER_RUN` accepts only
  `0`/unset; `DeerFlowClient(subagent_enabled=False)`; the guardrail denies the `subagent` class; the yaml keeps
  `max_total_per_run: 1` because deer-flow's schema refuses `0` (the task tool is never offered). Turning them on
  needs deer-flow's subagent executor to carry our `ZbmToolReceiptMiddleware` and `ZbmSystemPromptMiddleware`
  (`subagents/executor.py` builds its chain without `custom_middlewares`) — a proven middleware path on the
  subagent side, plus scenario coverage of `task`, before the switch exists again.
- **R9 — licence gate** (`licences.py`, the two seeds). `*.egg-info` (dir or file) is scanned; every importable
  top-level entry without a `RECORD`/`SOURCES.txt`/`top_level.txt` owner is a problem unless named in
  `unrecorded_allow` (`_virtualenv.py`, `_virtualenv.pth`); the METADATA `Name` must equal the directory's name
  (PEP 503 normalised) and the forbidden check keys on both; a metadata licence of UNKNOWN (or a pasted licence
  text) passes only through a `file` exception naming the proof file and its first line, both re-read
  (`dotenv` 0.9.9 and `tiktoken` 0.14.0 added with reasons). Residual: the classifier step of the spec's chain
  still accepts a distribution whose classifier says MIT while its bundled file says otherwise.
- **R10 — report integrity** (`engine/report.py`, `engine/loop.py`). Captured output is fenced with a backtick
  run one longer than the longest run in the content; `SWEEP:` sites are kept only when the file is in the diff
  and the line is inside a changed hunk (any line of a new untracked file) — the rest are dropped with the reason
  and counted; agent turn/tool-call/token/opaque-exec/deny counts are recorded as `agent_usage` and the report's
  agent line cites that event id; the "Captured output tail" fed back to the model sits between
  `--- BEGIN CAPTURED OUTPUT (untrusted) ---` / `--- END CAPTURED OUTPUT ---`.
- **R11 — small.** `identity.assert_effective` runs before every turn inside `bound_user`; the yaml pin is
  mandatory in every mode (`DLV_ALLOW_UNPINNED_CONFIG` and `DLV_DEERFLOW_CONFIG_SHA256` are refused as switches
  that do not exist).

Changed existing tests (they enshrined the disproved behaviour): S4 (the agent's `DISPROOF:` argv was run; now the
finding's reproduction is, and N1-1 is fixed first so `suite.after` is green), the docker-run argv token test
(labels, `--network none`), A3/A4 (`none`), A1 (`opaque` in the decision payload), G14 (the seeded `collect` argv,
the `remote`/`archive`/`show` git reads, `mv`/`test` execs), the runner argv test (`-rfE`).

## Toolchain amendment (Sep 27, 2026): engine-owned verdicts for Go, Rust and Node

Wave 19 left `cargo`, `go` and `npm` at `verified: false` — every count `unknown` by construction, so a finding
on ledger-rust, orchestrator-go or a Node service could never reach `fixed`. This amendment builds the R2
discipline for each in `engine/toolchains.py` (one adapter per ecosystem behind `TestRunner`), with the same rule
everywhere: the engine's invocation, an engine-read artefact, an independent enumeration where the ecosystem has
one, the transcript and the exit code must all agree, or the result is `unknown` (never green, never a valid RED,
never a green suite). The target grammar stays `<path>::<name>` for every ecosystem (`TEST:` lines, `DISPROOF`
reproductions, `node_id_in_text` now accepts `.rs`/`.go`/`.ts`/`.js` families).

- **Go** (`GoToolchain`): `go test -json -count=1 -race` (the repository's CI flags; `-run '^Name$' ./dir` for a
  target). The result is the `test2json` event stream captured by the engine: `pass`/`fail`/`skip` per
  package+test, and a test's own prints arrive as `output` events (proven: a printed `--- PASS:` line is text;
  only a `\x16`-framed line becomes an event, and that yields an unlisted or duplicate terminal event → unknown).
  Cross-checks: `go test -json -list` per package (the names the test binaries enumerate; `Test*` functions only,
  examples/benchmarks not counted; sub-tests recorded but counted under their parent) — exactly one terminal
  event per listed test, no event for an unlisted `Test*`; exactly one package-level result per listed package,
  consistent with its tests (`fail` with no failed test = panic/build failure → unknown; `skip` only for a
  package with no tests); a `build-fail` event or a non-event line on stdout → unknown; exit 0 ⇔ no failed
  test, else 1. Module-relative package dirs come from `go.mod` (`module` line). Zero listed tests → unknown.
- **Rust** (`CargoToolchain`): stable toolchain only — `cargo test -- -Z unstable-options --format json` needs
  nightly and is not used; `cargo nextest` is not a dependency (a new binary in the sandbox image with its own
  supply chain and licence entry, and it does not close the early-exit residual above; its per-process isolation
  buys nothing the cross-checks below do not already detect). `cargo test --locked --offline --no-fail-fast`
  (every test binary runs even when one fails), `--test <bin>` / `--lib` / `--bin <name>` / `--bins` selected
  from the target's path plus `-- <name> --exact`, and `--target-dir` under the engine directory **per checkout**
  (a shared directory handed the reverted checkout the verification checkout's binary: cargo's metadata hash does
  not separate two copies of a package at different paths and its freshness check is mtime-based; seen in the
  smoke run). Before every run the engine touches every file of the tree (`find … -exec touch`), because a file
  written in the same second as the previous build was served the previous binary (seen: the forger's binary
  answered for the clean test); files shipped into the box now carry fractional PAX mtimes for the same reason.
  Cross-checks: `cargo test … -- --list` (every `<name>: test` line per binary, doc-tests included, with each
  binary's `N tests, M benchmarks` summary agreeing with its lines) — the multiset of `test <name> ... ok|FAILED|
  ignored` lines must equal the listed multiset (a name printed twice — a forged line next to libtest's — or an
  unlisted name → unknown; a raw `write` to fd 1 bypasses libtest's capture, proven); the number of `running N
  tests` and `test result:` lines must equal the number of binaries the listing saw and their totals must equal
  the per-line counts; exit 0 ⇔ no failure, 101 with a failure (101 with none = compile error → unknown).
  Consequence for engineers: the RED test must be an integration test under `tests/` (a `#[cfg(test)]` unit test
  inside a source file cannot be split from the source by the R1 checkouts) — the brief's example says so.
- **Node** (`NodeToolchain`, `node --test`, Node 22.22 verified on this box): `--test-reporter=junit
  --test-reporter-destination=<engine path>` plus `--test-reporter=tap --test-reporter-destination=stdout`; a
  target is `--test-name-pattern='^name$' <file>` (non-matching tests are not reported in 22.22). Node has **no
  collect-only mechanism** (no dry run, no list; `--test-only` runs `only` tests) — stated: the cross-check is the
  junit file (read back by the engine) against the TAP summary (`# tests/pass/fail/cancelled/skipped/todo`), the
  TAP plan and top-level result lines, and the exit code (0 ⇔ no failure). Both reporters are produced by the
  runner process from the child's event stream, so a child's prints cannot forge them (proven: printed TAP lines
  become `# …` comments), but the cross-check binds the file to that process's transcript, not to an independent
  enumeration. Node's junit carries no file attribute and nests suites; cases are keyed `suite > name`, a
  duplicate name is a second case, and a target's verdict is every case of that name agreeing. Zero tests →
  unknown. The suite globs are seeded (`**/*.test.{ts,…}`, `**/*.spec.{…}`); `.ts` runs on Node 22's type
  stripping.
- **Detection defect fixed** (failing first: `test_detection_is_by_project_file_never_by_a_bare_tests_dir`):
  pytest's markers included a bare `tests/` directory, so toy-rs and toy-ts — every ecosystem has `tests/` — were
  detected as pytest. Markers are now project files only (`pytest.ini`, `pyproject.toml`, `conftest.py`,
  `requirements.txt`, `setup.cfg`, `tox.ini`; the three Python services without a `pytest.ini` have
  `requirements.txt`).
- **R1 for every ecosystem**: `test_infra_globs` per ecosystem (cargo: `Cargo.toml`/`Cargo.lock` at any depth,
  `build.rs`, `.cargo/`, `rust-toolchain*`, `.config/nextest.toml`, `clippy.toml`, `rustfmt.toml`; go: `go.mod`/
  `go.sum` at any depth, `go.work*`, `testdata/`, `vendor/`, `tools.go`, `.golangci*`; npm: `package.json` at any
  depth, the lockfile(s), `.npmrc`, `tsconfig*.json`, `node_modules/`, jest/vitest/babel/mocha configs, `.nvmrc`,
  snapshots) and `test_file_globs` (`tests/**/*.rs`, `benches/`, `examples/`; `**/*_test.go`; `**/*.test.*`,
  `**/*.spec.*`, `test/`, `tests/`) drive `runner.classify_paths`. New: **content rules** (`test_content_deny`,
  regexes per ecosystem) applied to every changed or new test file after `FIXED` — a hit fails the round
  `test_content_denied` with the rule name (Go: `TestMain`, `os.Exit`/`syscall.Exit`/`runtime.Goexit`, a `\x16`
  byte or its escapes; Rust: `process::exit`/`abort`, `from_raw_fd`/`libc::write`/`as_raw_fd`, `#![no_main]`/
  custom test frameworks; Node: `process.exit`/`abort`/`kill`, `v8.serialize`/`Serializer`, `writeSync(1, …)`/
  `process.stdout.write`). These are the cheap structural answers to the R2 residual for the routes we know;
  they are not a proof (an obfuscated spelling passes them, and so does a helper placed in a SOURCE file that the
  test calls — source files are what the fix is allowed to change) and the residual is restated under "Known
  limitations".
- **Baseline-failure attribution** (§C.8.4 step 5, `service.attributable_failures`): a pre-existing failure may
  remain only when it belongs to another open finding; the match now understands each ecosystem's case keys
  (`runner.same_test`: cargo's bare libtest name, go's `<pkg dir>::<Test>`, node's `suite > name`) next to the
  file match and the pytest node id.
- **Sandbox env**: `service_env` gains `NO_COLOR=1`, `FORCE_COLOR=0`, `CARGO_TERM_COLOR=never`,
  `CARGO_NET_OFFLINE=true`, `GOPROXY=off`, `GOTOOLCHAIN=local` (determinism and no toolchain download attempts;
  `EXTRA_ENV_ALLOWLIST` in `adapters/sandbox.py` names exactly these).
- **Proof**: `fixtures/dlv/toy-rs`, `toy-go`, `toy-ts` (each with the toy-py defects: `add` subtracts, `percent`
  divides by zero; a skipped/ignored test; toy-rs has a doc-test and two test binaries, toy-go two packages with a
  sub-test) run through the whole loop with the deterministic fake model and the real toolchains
  (`tests/test_toolchains.py`): `awaiting_review` with `suite.before` = 3/1/1 (go, cargo) and 2/1/1 (node),
  verified `ok`, and the deny paths per ecosystem — forged transcript at RED (`red_unknown` for go and cargo; a
  no-op comment for node), test-infra edit (`test_infra_changed`), deleted existing test (`test_deleted`),
  content rule (`test_content_denied`), hung suite (`suite_unknown`, "timed out"), then `fixed` once clean.

## Known limitations

- The tool-call classifier is a denylist over an unbounded language (df-exec F-03): it is the record and the
  first refusal; an interpreter/shell/make invocation is recorded as `allow_opaque` (R5) and the BOUNDARY is the
  sandbox (non-root, `--cap-drop=ALL`, default seccomp, `--read-only`, `--network none`, no socket mount, no
  `.git`, no `curl`/`wget`) — and those properties are unproven on this box.
- What a lazy or hostile agent can still do to reach `awaiting_review` with the defect intact (after wave 19 and
  the toolchain amendment): write a RED test whose body inspects the source text (or otherwise decides pass/fail
  without exercising the defect) and a source change that satisfies it — the engine proves RED→GREEN, the
  split-diff verification and a green suite, not that the test exercises the finding; or have agent code that runs
  inside the RED test process forge the ecosystem's whole transcript at once and end the process early (pytest:
  rewrite the junit file AND print a matching summary, exit code and collect-only count; Go: emit a `\x16`-framed
  test2json line for every listed test and `os.Exit(0)` before `testing` reports — `TestMain`, `os.Exit` and the
  `\x16` byte in a test file are refused by the content rules, so the route needs an obfuscated byte; Rust: write
  libtest's per-test and `test result:` lines for every listed test of every binary to fd 1 and `process::exit`
  — refused by the content rules for the plain spellings; Node: reproduce the runner's internal v8-serialized
  child→parent frame on stdout). All leave the finding's own reproduction (if one is named) and AEGIS re-review as
  the checks that catch them. The residual that no ecosystem closes: a test process that exits 0 after a complete
  forged transcript is indistinguishable from a passing one; `cargo nextest` would not close it either (a test
  process that exits 0 early is a pass there too), which is one of the two reasons it is not a dependency.
- Services outside `services/<name>/` (the dashboard lives in `apps/dashboard-ts/`) are outside the findings
  document's path grammar (§B.1: `file` under `services/<service>/`), so a run against `apps/dashboard-ts` cannot
  be submitted; the Node adapter is proven on `fixtures/dlv/toy-ts` and applies to any `services/<svc>` with a
  `package.json` + lockfile. A Node service's tests must run on Node's built-ins and the service's own sources:
  `node_modules` is never shipped to a verification checkout and `npm ci` is never run by the engine (the sandbox
  has no network). ledger-rust's crates.io dependencies need a populated `CARGO_HOME` registry cache in the
  sandbox image for `--offline` builds; the image does not vendor one yet (unchanged from wave 19).
- The sandbox image (`docker/sandbox.Dockerfile`) now installs Node 22 with a required `NODE_SHA256` build
  argument and sets `RUSTUP_HOME`/`CARGO_HOME` for uid 65532 (before this amendment the rustup proxy would not
  have resolved a toolchain as that uid: `HOME` is the workspace); neither change is proven here (no daemon).
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
guardrail, the argv-level Docker double, a scripted model, a temporary git repository). 425 tests: 422 passed,
3 skipped (the Docker live module, reason printed), ~14 min wall (wave 19 added the collect-only cross-check and
two verification checkouts per fix); the toolchain amendment brings it to 449 tests (446 passed, 3 skipped, +2 min:
`tests/test_toolchains.py` runs the real `cargo`, `go` and `node` on the toy fixtures through the whole loop).
`ruff check src tests devtools` clean. Round-18 findings: `tests/test_round18.py` (one failing-first test per
finding). Evidence: `services/delivery-py/docs/evidence/dept28/` (incl. the wave-19 live
log and the re-run of the reviewers' probes) and `docs/evidence/licences-2026-09-27.json`.
