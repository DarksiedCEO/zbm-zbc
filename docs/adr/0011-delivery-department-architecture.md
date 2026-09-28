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
| `seed/prompts_manifest.json` | `7090c2d48d25f257231c2578e931594bcb30a095599aee08cf59b51e3ee99d37` (wave 21: `engine.system.md` rule 5, `brief.template.md` names the finding's reproduction argv, `reviewer.md` requires a runnable reproduction for every finding filed, `CHANGES.md`; wave 19: `engine.system.md` rules 2 and 5 restated for R1/R3) |
| `seed/tool_policy_seed.json` | `078560a463fd04a11fb3a358e8ebc5a1a782dcedfbd635192bc9a77738787f4d` (wave 20: `pylint`/`isort`/`pre-commit` in the exec allowlist, R6/R9 notes) |
| `seed/test_commands_seed.json` | `eed14d324f00cfac09dc085bd362113740138ac68ff5c7c1eb7669749ae54785` (wave 21: pytest `test_content_deny` gains `process_exit`, `fd_write`, `capture_bypass`; wave 20: pytest `test_content_deny` for re-plugging spellings, `src_content_deny`, `zbm_engine_plugin*` as test infra; toolchain amendment: go/cargo/npm `verified: true` with their engine argv, `collect`, `target_example`, `test_content_deny`, per-ecosystem `test_infra_globs`; pytest markers no longer include a bare `tests/`; `service_env` gains the toolchain determinism switches) |
| `seed/licence_allowlist.json` | `c02f367f3e6cd02949e18dbc2eaa2ceabcb917ac4aa5fc58ad73bec4ac6dfb6e` (wave 19: `unrecorded_allow`) |
| `seed/licence_exceptions.json` | `e93abd348a10f89838e11a19c7f52994e18a20f4d52a6809b49a0a6fafe53606` (wave 20 close-out: `speechrecognition` removed from the lock instead of excepted; wave 19: `dotenv`, `tiktoken`) |
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
  (`dotenv` 0.9.9 and `tiktoken` 0.14.0 added with reasons). Residual (closed by wave 20 R13 below): the
  classifier step of the spec's chain accepted a distribution whose classifier says MIT while its bundled file
  says otherwise.
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

## Round 19 amendments (fix wave 20, Sep 27, 2026; lead rulings R1-R15, binding)

Round 19 re-confirmed the five wave-19 closures and reproduced three more routes to `awaiting_review` with the
defect intact: a RED test never tied to the finding (a "fix" in a brand-new module the test imports), a pytest
plugin registered from inside the RED test that flips `failed` to `passed` with every wave-19 cross-check agreeing,
and the finding's own reproduction turned into a `skip` under `CHANGED_TEST:`; cancel/deadline did not stop the
verdict pipeline once the FIXED turn had returned. The principle of wave 19 stands and is extended: **the engine
never trusts anything the agent's process can emit, and now also never shares a process space or a writable
directory with it when computing a verdict.**

- **R1 — verdicts run in a fresh container** (`adapters/sandbox.py::start_engine_box/ship_tree/destroy_box`,
  `engine/loop.py::_verdict_box/_tree`, `runner.py`). Every engine run — `suite.before`, RED, GREEN, the
  verification checkout, the reverted checkout, the single-file revert, the two reproduction runs, the src-only
  check, the per-finding suite, the disproof re-run and `suite.after` — happens in a NEW container
  (`dlv-<run_id>-<tag>-<nonce>` on its own volume, the same `docker run` argv and run label as the agent's,
  reaper-covered) populated by `docker cp` from a tree the engine builds on the host: `git archive <sha> --
  services/<svc>` plus an overlay of exactly the worktree paths that run may see (RED/GREEN: everything the agent
  changed; verification: `src` + the RED test file; reverted: the RED test file; single-file revert: the fix minus
  the finding's file + the RED test; src-only: `src` alone; suites: the commit tree / the commit itself). The
  engine directory, the pytest ini and the engine plugin are written into that container by the engine; result
  files are read back from it with `docker cp`; the container and volume are destroyed right after the run and
  the elapsed time is recorded (`engine_box_started` / `engine_box_released`, `elapsed_ms`). `.dlv-engine` and
  `.dlv-verify` no longer exist in the agent's volume (`test_n19_a13_*`); the DF file tools' atomic write still
  stages under a transient random `.dlv-write-<nonce>` in the agent's volume for the `mv` — the resolve→mv
  window residual of R7 is unchanged and restated below. The agent's container and every engine box in flight are
  `docker kill`ed on cancel/deadline (`kill_run`, `sandbox_kill_requested`). **Cost, measured on the argv double
  (a directory plus a tar copy per container, pytest as a subprocess):** 0.54-0.70 s per verdict run
  (`engine_box_released.elapsed_ms`, almost all of it the pytest subprocess), 9 engine containers for a clean
  one-finding run (3 suites, RED, GREEN, verify, reverted, 2 reproduction runs, src-only), 18 for the S1
  two-finding scenario, which the reviewers' `probe_baseline.py` times at 14.6 s on this branch against 23.1 s at
  c353391 (both measured while the full suite ran concurrently; the double's container churn is cheap and the
  old per-operand `readlink` resolution is gone). No daemon on this box: the live per-container cost (`docker run`
  + `docker cp` + `docker rm` per verdict, roughly a second each on a warm daemon) is not measured.
- **R2 — the RED test is tied to the finding** (`engine/loop.py::_green_phase`, `states.py`). Ingestion already
  refuses a finding without `file` (422, schema); a file alone is accepted. For `fixed`: (a) the finding's file
  must carry a hunk of the fix diff (a source change or a `CHANGED_TEST` change; a NEW module the RED test imports
  is not a fix of the finding) — `finding_file_unchanged`; (b) **single-file revert**: a checkout with the whole
  fix EXCEPT the finding's file (+ the RED test) must make the RED test FAIL — `test_not_tied_to_file` (when the
  fix touches only that file the checkout is the reverted checkout itself, recorded `identical_to_reverted`);
  (c) when the finding's `reproduction` names a test node id, that reproduction is run ALONE, in its own container,
  in the verification checkout (must pass) and in the reverted checkout (must fail) — `reproduction_not_fixed` /
  `reproduction_passes_without_fix`. Each is its own ledger event (`single_file_revert_checked`,
  `reproduction_checked`) and finding record (`finding_file_hunk`, `single_file_revert`, `repro_check`); the
  `fixed` invariant requires all of them and refuses on any `unknown`. Added beyond the ruling, because the round's
  in-process route made it necessary: the **src-only check** — every baseline failure the fix claims (failing at
  the last verified suite, passing now, not attributable to another open finding) is re-run on base + the SOURCE
  changes alone, no test file of the agent's present (one container, one pytest run of all of them; go per target;
  cargo/node bare case names cannot be mapped back to a file — stated) — `fix_not_in_source_suite`. Without it a
  RED test module that monkeypatches the module under test at import time flips the baseline failure in the
  full suite while every other check passes.
- **R3 — outcome deltas are verdicts** (`engine/loop.py::_outcome_regressions`). Baseline = `suite.before`'s
  per-case outcome map. Any test that was `pass`/`fail` at baseline and is `skip` (skipped or xfailed) or missing
  after the per-finding suite or `suite.after` is `outcome_regressed`: the round fails (finding back to `red`,
  `outcome_regressions` on the record) or the run fails (`OUTCOME_REGRESSED`); no `CHANGED_TEST:` excuses it. A
  `CHANGED_TEST:` on the file holding ANY open finding's reproduction node id is `changed_test_denied`. The
  `CHANGED_TEST` reason text is kept verbatim on the finding (`changed_tests[].why`, ≤ 400 chars) and printed in
  the report inside the untrusted fence, next to its sha.
- **R4 — pytest cannot be re-plugged from inside** (`adapters/tools/zbm_engine_plugin.py`, `runner.py`,
  `engine/parsers.py::plugin_record_problem`, `engine/toolchains.py`). Every pytest run carries
  `--disable-plugin-autoload` (no distribution entry point loads; a service whose tests need such a plugin cannot
  run under the engine — stated) and `-p zbm_engine_plugin`, the engine-owned plugin (hash pinned in `runner.py`,
  shipped into the fresh container's engine directory, which is FIRST on `pythonpath` — pytest inserts
  `pythonpath` before it imports `-p` plugins, so nothing in the tree can shadow it; a tree entry named
  `zbm_engine_plugin*` is test infra). The plugin writes `<junitxml>.zbm.json`: every `pytest_plugin_registered`
  after session start must be a `conftest.py` module or a pytest builtin, after `pytest_collection_finish` any
  registration is a violation; the plugin set and every hook's implementation list are snapshotted after
  collection and compared before every test and at session end; a set of `_pytest`/`pluggy` module and class
  attributes is snapshotted at plugin load and compared at the same points; per test phase it records
  `call.excinfo is None` (tryfirst non-wrapper `pytest_runtest_makereport`), the raw report (innermost wrapper),
  the final report (outermost wrapper) and the logged report (tryfirst `pytest_runtest_logreport`), and every
  logged report must have been built by a makereport it saw. The engine refuses (`unknown`) a missing or
  unparseable record, a plugin hash or path other than its own, a session that did not start/collect/finish
  with autoload disabled, any violation, and any case whose four positions disagree (raw vs excinfo; final vs raw
  except through pytest's xfail machinery; logged vs final) or disagree with junit. Proven with the reviewer's
  flip plugin spelled past the content rules: pytest prints `1 passed`, junit == collected == summary == exit,
  the record says `plugin registered after collection` → GREEN `unknown` (`test_n19_e2_*`). The cheap layer:
  `test_content_deny` for pytest names `pluginmanager`, `.register(`, `hookimpl`/`hookwrapper`/`pluggy`,
  `pytest_runtest_`/`pytest_collection`/`pytest_configure`/`pytest_plugin`, `conftest`, `_pytest`,
  `gc.get_objects`/`gc.get_referrers`, `sys.settrace`; a new `src_content_deny` names `_pytest`, `pluggy`,
  `pluginmanager`, `gc.get_objects` in a changed SOURCE file (`src_content_denied`). Both are stated as cheap.
- **R5 — liveness gates every phase** (`engine/loop.py`, `service.py::_require_live`). `_check_live` runs before
  every fresh container starts, before GREEN, the verification runs, the suites, the commit and the report; the
  service refuses (`Conflict`) `run_update` / `finding_update` / `finding_transition` on a run that is failed,
  reviewed, awaiting review or marked `unrecorded_failure` — the only run events accepted afterwards are the
  post-mortem `run_interrupted`, `sandbox_released`, `agent_usage`, `agent_usage_linked`. The reviewer's probe e4
  (cancel after the GREEN run) now ends with no commit, no `fixed`, no engine event after `fix_run_cancelled`
  except those and the container teardown, and the agent's container killed (`test_n19_e4_*`).
- **R6 — bash commands are single-line or refused** (`policy.py::has_line_separator`). A command containing
  `\n`, `\r`, NUL, `\f`, `\v`, U+2028, U+2029 or U+0085 is denied `multiline_command` BEFORE tokenising (the
  here-doc form included); a tab stays a word separator for bash and shlex alike. The other free-text arguments
  the guardrail classifies (`path`, `pattern`, `glob`) are denied on a control character or a line separator too.
  The tokeniser tests now cover every separator bash knows (`; && || | & |& ;; ( ) { } $( ) \` <( ) >( )`).
- **R7 — file tools contain to the write roots** (`adapters/sandbox.py::_contain/put_bytes`,
  `policy.py::exec_write_ok/adr_name_ok`). `write_file` / `str_replace` / `update_file` must land inside
  `services/<service>/` or exactly `docs/adr/00NN-*.md` — before AND after in-container symlink resolution — and the
  destination parent is re-checked against the same roots at write time; the reviewer's `ln -s <engine dir>
  services/svc/eng` + `write_file services/svc/eng/engine.ini` is refused (`test_n19_a2_*`). A bash write under
  `docs/adr/` must be a `00NN-*.md` file too (no subdirectory, no other name), for the classifier's operand check
  and the guardrail's resolved path alike (N19-A-8).
- **R8 — resolution is bounded and after the record** (`adapters/tools/resolve.py`, `adapters/sandbox.py::
  realpath_many`, `adapters/guardrail.py`). A call is capped at 16 write operands and 64 path components per
  operand (a breach is a deny with NO exec); the operands are resolved in ONE exec — `python3 -I
  /mnt/dlv/resolve.py -- <operands>`, a hash-pinned helper bind-mounted read-only from the engine's own package
  (`--mount type=bind,src=<pkg>/adapters/tools,dst=/mnt/dlv,ro`; verified against its pin before every `docker run`)
  that answers the real path of the longest existing prefix plus the remainder, and nothing for a dangling or
  looping link, a non-absolute or `..` path or a remainder hanging off a file — never a file the agent could
  rewrite. The guardrail records `tool_call_decided` with `decision: pending` BEFORE resolving and
  `tool_call_resolved` with the final decision after; a call that needs no resolution is decided directly. The
  reviewer's `mkdir -p` 500 deep cost 500 execs and 1001 records before any record of the decision; it is now a
  deny with none (`test_n19_a3_*`).
- **R9 — classifier gaps** (`policy.py::_split_options/_write_operands/_find_subcommands`). `-t DIR`,
  `-tDIR`, `--target-directory=DIR` and `--target-directory DIR` are the destination of `cp`/`mv`/`ln`/`install`
  (the sources are never the target); `chmod`/`chown`/`chgrp` operands are targets whatever the mode's spelling
  (`-x`, `-R -w`, `+x`, `644`, `--reference`); `sed` in-place is any short cluster containing `i`, `-i.bak`,
  `--in-place[=suffix]`, with `-e`/`-f` scripts taken out of the operands; a `cp`/`mv` with no destination is
  refused; the command a `find -exec/-execdir/-ok` would run is classified like any simple command (with `{}`
  standing for the start directory); `mypy`, `pylint`, `black`, `isort`, `pre-commit`, `ruff`, `gofmt` are
  `allow_opaque` (they load plugins or configuration from the tree; `pylint`/`isort`/`pre-commit` added to the
  exec allowlist so their record is honest). `touch -d/-t/-r` option arguments are no longer taken as operands.
- **R10 — reaper and record-first** (`adapters/sandbox.py::reap/_cp_in/_cp_out/destroy`). `docker ps` and
  `docker volume ls` use `--format` (tab-separated name and run label) WITHOUT `-q` (the CLI ignores the format
  under `-q` and printed ids only, so every reap recorded `run_id: ""`); a failing listing or removal is recorded
  `sandbox_reap_failed` and nothing is called reaped that was not; every `docker cp` in or out (copy-in, copy-out,
  the tree shipped to an engine container, the report read-back) is recorded `sandbox_exec_requested/completed`
  with `op: cp_in|cp_out`, `kind: sandbox_cp` BEFORE the daemon call, and a dead ledger stops it; `destroy` with a
  failing removal AND a failing ledger marks the run `unrecorded_failure` through `on_ledger_failure` instead of
  being swallowed.
- **R11 — git isolation** (`gitport.py`). Every git command runs with a private empty `HOME` (a per-process temp
  directory), `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, `XDG_CONFIG_HOME` under that HOME, and
  `-c core.hooksPath=<empty engine dir> -c core.fsmonitor=false` first on the argv (`git archive` included). The
  reviewer's gitchk repository (a tracked `.gitconfig` with `core.hooksPath=.hooks` and a `pre-commit` hook that
  prints `HOOK-RAN`) runs the hook under the wave-19 environment and does not under the engine's
  (`test_n19_a7_*`).
- **R12 — egress abort** (`adapters/egress.py::abort/_shutdown_socket`). `abort()` shuts the response's network
  socket down (`SHUT_RDWR`, via httpcore's `network_stream` extension) before closing it, so a reader blocked in
  `recv` with nothing arriving returns at once. Proven live on the assigned ports with the reviewer's
  headers-then-silence TLS server (`tests/test_live_round19.py`): 18.1 s after the abort before, < 2 s now.
- **R13 — licence gate** (`licences.py`). A `.pth` path line naming a directory outside the virtual environment
  is a problem (the gate never scans it); one inside the venv is scanned as a further site directory; a top-level
  entry is covered by a distribution only when every RECORD line under it that names a present file carries a
  sha256 that verifies (files over 4 MiB by recorded size; a bare `path,,` line covers nothing); a METADATA with
  two `License-Expression` or `License` fields is a problem; a metadata licence contradicted by every bundled
  `LICENSE*`/`COPYING*` file whose heading is recognised (MIT, Apache, BSD, ISC, MPL, the GPL/LGPL/AGPL families,
  EPL, Unlicense, SSPL, BUSL, Elastic) is a problem — the wave-19 residual is closed. Running it on the venv
  surfaced two facts: `nest-asyncio` declares `BSD` and ships a 2-clause file (the same permissive family; the
  gate treats BSD-2/3 as one family), and **`speechrecognition` 3.17.0 (a deer-flow transitive dependency)
  declares `BSD-3-Clause` and ships prebuilt FLAC encoder binaries under GPL-2.0 (`licenses/LICENSE-FLAC.txt`)**.
  Lead's ruling (wave 20 close-out): the distribution is removed, not excepted — `speechrecognition` is only
  reached through `markitdown[all]`'s audio path, which the engine never uses, so it joins the
  `override-dependencies` list (`sys_platform == 'never'`) and no longer resolves into `uv.lock`; the
  `bundled_licence_file` exception kind stays in the gate for future use but the seed carries no entry for it.
  The gate passes the resolved venv with no bundled-licence exception. The gate's start-up cost is now
  ~3 s warm / ~7 s cold on this venv (903 MiB, 48k files).
- **R14 — small.** A pure-deletion hunk (`+N,0`) covers no new line (`_hunk_lines`; N19-E-5); `fixed` requires a
  `verification` record (no `if v and …`; N19-E-6); the "suite after commit" flag is gone — the per-finding suite
  runs on the engine-built commit tree, its content digest (`_tree_digest`: sorted path + sha256 + exec bit) is
  recorded with the suite and again from `git archive <commit>` after the commit, and `fixed` requires
  `suite_tree_sha256 == commit_tree_sha256` (`commit_tree_mismatch` otherwise); `forbidden_run_token` now
  covers the reviewer's list (`--pid/--userns/--ipc/--cgroupns/--uts` in every spelling, `--security-opt` with
  anything but `no-new-privileges`, `-v/--volume/--mount` other than our volume and the two read-only binds,
  `--add-host`, `--gpus`, `--dns*`, `--sysctl`, `--device`, `--cap-add`, `--privileged`, `docker.sock`) and any
  unicode dash; the two bind sources are validated (absolute existing directory, no `,`, `=`, `:` or control
  character) before they are spliced into the CSV `--mount` option; `_run_scope` fails CLOSED (`EgressRefused`
  when no runtime, no effective user, no bound run or a run that is not live — no LLM call leaves the box without
  a run id and a deadline cap; N19-A-12). `assert_effective` remains the same-thread contextvar check the
  reviewer called tautological; `bound_user` sets it and the guardrail/sandbox check it independently.
- **R15 — CI.** The `delivery-py` job installs Node 22 (`actions/setup-node`, SHA-pinned like the others), Go
  (`actions/setup-go`, version from `fixtures/dlv/toy-go/go.mod`) and Rust stable before `uv sync`, so
  `tests/test_toolchains.py` runs the real toolchains instead of failing on a missing binary; `docs/ci.md` says so
  (`actionlint` clean).

Changed existing tests (they enshrined the disproved behaviour): `test_n18_e2_fix_that_lives_in_a_test_helper`
(the refusal is now `finding_file_unchanged`, before GREEN runs), the docker-run argv token test (the `/mnt/dlv`
read-only bind), `test_file_operations_contain_paths` (writes are contained to the write roots, not the
workspace), G14's engine exec heads (`python3 -I /mnt/dlv/resolve.py --` is the only `python3` the engine runs;
`readlink` is gone), the `TestRunner` constructor (no sandbox argument: a runner holds no container).

Pins after this wave: `seed/tool_policy_seed.json` `078560a463fd04a11fb3a358e8ebc5a1a782dcedfbd635192bc9a77738787f4d`,
`seed/test_commands_seed.json` `6c3a39edc981c969d1b1d538049ec097c59dd36dd51b27bc2407440568de5a30`,
`seed/licence_exceptions.json` `e93abd348a10f89838e11a19c7f52994e18a20f4d52a6809b49a0a6fafe53606`; the resolver
`adapters/tools/resolve.py` and the plugin `adapters/tools/zbm_engine_plugin.py` are pinned in
`adapters/sandbox.py` / `runner.py` (`RESOLVE_HELPER_SHA256`, `PLUGIN_SHA256`).

## Round 20 amendments (fix wave 21, Sep 28, 2026; lead rulings R1-R6, binding)

- **R1 — the reproduction is mandatory (N20-D-3).** The reviewer reproduced the admitted residual of wave 20: a
  finding whose `reproduction` was prose skipped the reproduction check, so a marker-constant "fix" with a
  tautological RED test reached `awaiting_review` with `percent(1, 0)` still raising (`probe_d3c`). The prose route
  is removed. `POST /dlv/v1/fix-runs` refuses (`422`, `code: reproduction_not_runnable`, the finding id and the node
  id in the body; `fix_run_refused` with `REPRODUCTION_NOT_RUNNABLE`, rule `DLV-19`, on the ledger when it answers)
  any finding whose reproduction does not resolve at the base commit (`runner.reproduction_problem`): a
  `<path>::<name>` node id in the text; a seeded framework detected at base by its marker files; the path a source
  file of that runner (`.py`; `.rs`; `_test.go`; `.ts`/`.js` family), not test infrastructure, present at base;
  the test's own name occurring in that file. A `fail` review's reopened and new findings are checked the same way
  against the run's head BEFORE the review is recorded (a refused review changes nothing). In the loop the check
  is unconditional: a finding that somehow has no runnable reproduction fails the round
  `reproduction_not_runnable` and can never be fixed, and `states.finding_transition_problem(…, "fixed")` refuses a
  finding without a passing-with/failing-without reproduction record. The brief names the reproduction's argv; the
  reviewer prompt says every finding filed must name one. **Consequence, stated for the lead:** a genuinely new
  defect found at review has no failing test in the run's head (the head's suite is green by construction), so the
  review route can no longer carry a NEW finding the engine can fix; the reviewer lands the failing test on the base
  branch and files a new findings document. The toy fixture gains N1-2's reproduction
  (`tests/test_percent.py::test_percent_zero_whole`); single-finding harnesses leave it out (`pct_repro=False`).
- **R2 — symlinked roots (N20-D-4).** Two causes, both fixed. (a) The Docker CLI test double mapped only the volume
  path as spelled back to the container path; a process started under a symlinked `TMPDIR` reports the physical
  path, so host paths reached the engine (`tests/fakes.py::_map_out` maps the realpath too). (b) The engine gave
  pytest `--rootdir=<cwd as spelled>` and an ABSOLUTE `testpaths`; pytest resolves explicit targets against the
  process's physical working directory, so node ids in the terminal summary (`../../<link>/…`) and junit disagreed
  and every verdict was `unknown` (fail closed, but nothing could pass). Now `--rootdir=.` and `testpaths` stay
  relative (it is an `args` value pytest globs against the working directory and uses only when that directory is
  the rootdir); `pythonpath` stays absolute (a `paths` value resolved against the ini's directory). Proven by
  `test_round20.py::test_n20_d4_whole_loop_with_tmpdir_behind_a_symlink_reaches_awaiting_review` and by the whole
  suite under `TMPDIR=<symlink>`. Production container paths are not symlinks; this matters for the test double and
  for any host whose temp or checkout path is (macOS `/var` → `/private/var`).
- **R3 — tests never rewrite tracked files (N20-D-1).** `test_live_launcher.py` wrote its run log over the tracked
  `docs/evidence/dept28/live-launcher-run.log` on every green run; it now writes `docs/evidence/dept28/_runs/`
  (gitignored). The committed `live-launcher-run*.log` files are frozen artefacts of the waves that produced them.
  `tests/test_live_tracked_files.py` runs the live modules in a child pytest and fails if `git status --porcelain`
  (untracked files included) changed.
- **R4 — a pipe into an interpreter is denied however spelled (N20-D-5).** `|  bash`, `| /bin/bash`,
  `| python3` were `allow_opaque` (the raw-string rule knew `| bash`/`|bash` only). The tokeniser now records, per
  simple command, whether its stdin is a pipe (`|`, `|&`) or a here-string/here-document (`<<<`, `<<`); a command
  so fed whose argv[0] basename (any path spelling, after shlex has normalised whitespace and quotes) is in
  `OPAQUE_ARGV0` or any `*sh` name is refused `pipe_to_interpreter`. Direct execution (`python3 x.py`,
  `bash x.sh`) stays `allow_opaque`. Denying every `OPAQUE_ARGV0` member (not only shells and Python) is
  deliberate: `sed -f -`, `awk -f -`, `make -f -` also read a program from stdin; a plain `| sed 's/a/b/'` is
  refused too (use a file).
- **R5 — hard links (N20-D-6).** `ln` without `-s`/`--symbolic` (including `-P`/`-L`) and `cp` with `-l`/`--link`
  (alone or in a cluster: `-al`, `-la`, `-rl`, `-a --link`) are write-class on BOTH operands: every source must
  normalise inside the write roots and is added to the operands the guardrail re-resolves in the container.
  `link` is not in the seed (denied `unknown`). Swept in the same class: `mv` REMOVES its sources, and
  `mv services/other/a services/<svc>/b` was allowed — sources of `mv` are now checked the same way. Symbolic links
  (`ln -s`, `cp -s`) are unchanged: the link is inside the roots and the target is resolved by the in-box resolver
  and bounded by the tar/sync code on the host.
- **R6 — docs == implementation (N20-D-8).** The engine note says a test must not exit the process or write to the
  runner's transcript; the pytest seed had rules for neither. `test_content_deny` (pytest) gains `process_exit`
  (`os._exit`, `_exit(`, `sys.exit`, `SystemExit`, `pytest.exit`, `os.abort`/`kill`/`killpg`, bare
  `exit()`/`quit()`, `from os|sys|builtins import …exit…`, the quoted names), `fd_write` (`os.write`/`writev`/
  `pwrite`/`dup`/`dup2`/`fdopen`/`sendfile`, `sys.__stdout__`/`__stderr__`, `/dev/stdout|stderr|tty|fd/`,
  `/proc/self/fd`, `open(1|2)`, `.fileno()`) and `capture_bypass` (`capsys`/`capfd`, `.disabled()`, the terminal
  reporter). Stated as what they are: a cheap layer, a regex over the test file's text — the verdict still comes
  from the engine's junit file cross-checked with its own plugin record, collect-only and the exit code.
- **N20-D-9 (broad excepts).** Audited every `except Exception` in `src/`: each either fails the run/round closed
  (`HARNESS_ERROR`, `mark_failed_unrecorded`), denies (guardrail, egress, sandbox record-first paths), or is
  best-effort bookkeeping after a verdict (the `agent_usage` record, the reaper, the watchdog's own error, which
  leaves the loop's `_check_live`). None turns an exception into `pass`, `fixed` or `disproved`; none is in
  `runner.py`, `engine/parsers.py` or `engine/toolchains.py`. No change.
- **N20-D-10 (hard-coded test ports).** The live tests read `DLV_TEST_PORT_RANGE` (`lo-hi`, inclusive; default
  18800-18849, unchanged) through `tests/helpers.py::live_ports()`.
- **N20-D-7** passed review (resolver cap/pin); no change. **N20-D-2** is named by the lead's wave brief but has no
  entry in the round-20 verdicts or the reviewers' evidence; nothing was changed for it.

Changed existing tests (they enshrined the prose route or assumed its fixture): `test_n18_e1_finding_without_a_
machine_runnable_reproduction_…` (now refused at ingestion), `test_n19_e1_ingestion_rejects_a_finding_without_a_file`
(prose is 422), `test_n19_e6_fixed_invariant_…` (the good record carries a reproduction record), the round-19 tests
that used a prose reproduction to keep `tests/test_calc.py` editable (their reproduction now lives in
`tests/test_add_repro.py`, committed by the harness), `test_s5_review_fail_reopens_…` (a prose new finding is
refused before the review is recorded; with a runnable but passing reproduction the new finding is never fixed),
`test_s12_…`, `test_a6_…` (the injected text still carries a node id), S1's counts (N1-2's reproduction is a
second baseline failure), the late-plugin probe's host tree (4 tests, without `test_percent.py`) and six
single-finding scenarios (`pct_repro=False`).

Pins after this wave: `seed/test_commands_seed.json` `eed14d324f00cfac09dc085bd362113740138ac68ff5c7c1eb7669749ae54785`,
`seed/prompts_manifest.json` `7090c2d48d25f257231c2578e931594bcb30a095599aee08cf59b51e3ee99d37`;
`seed/tool_policy_seed.json` unchanged (`078560a4…`).

## Known limitations

- The tool-call classifier is a denylist over an unbounded language (df-exec F-03): it is the record and the
  first refusal; an interpreter/shell/make invocation is recorded as `allow_opaque` (R5) and the BOUNDARY is the
  sandbox (non-root, `--cap-drop=ALL`, default seccomp, `--read-only`, `--network none`, no socket mount, no
  `.git`, no `curl`/`wget`) — and those properties are unproven on this box.
- What a lazy or hostile agent can still do to reach `awaiting_review` with the defect intact (after wave 20):
  for a finding whose `reproduction` names NO test node id, write a RED test that is a tautology tied to the
  finding's file (it asserts a marker the "fix" adds to that file) — the file hunk, the single-file revert, the
  verification/reverted checkouts, the src-only check and a green suite all hold, because nothing machine-readable
  says what the defect is; the engine proves the test is tied to the FILE, not to the DEFECT, and only the
  finding's own reproduction (when named) or AEGIS re-review reads semantics. With a reproduction node id, the
  reproduction must pass on base + source alone in its own process, which closes that route for the tests that
  exist at baseline. Inside the verification container the agent's test code still runs as the same uid as the
  engine's files: a source module (the fix is allowed to change source) that monkeypatches a pytest/pluggy
  internal the plugin does not snapshot, or that spells its way past the cheap content rules and forges the whole
  transcript at once (junit + plugin record + summary + collect-only count + exit code, all consistent), is not
  detected — the plugin record raises that bar (four positions per test, a self-hash, a session shape) but is not
  a proof. Go/Rust/Node keep their wave-19 residual (a process that exits 0 after a complete forged transcript;
  the plain spellings are refused by the content rules) and have no plugin-record equivalent; cargo and node have
  no src-only check (their case keys do not map back to a file). A `bash script &` daemon in the AGENT's container
  can still race the file tools' resolve→mv window there (R7 residual), but it can no longer touch any verdict.
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
finding). Fix wave 20 brings it to 489 tests (486 passed, 3 skipped, ~20 min wall: every verdict now starts a
fresh container on the double, `tests/test_round19.py` adds 39 failing-first tests for N19-E-1..6 / N19-A-1..13
and `tests/test_live_round19.py` one live egress-abort test on the assigned ports). Evidence:
`services/delivery-py/docs/evidence/dept28/` (incl. the wave-19 and wave-20 live logs, `round18/` and `round19/`
with the re-runs of the reviewers' probes) and `docs/evidence/licences-2026-09-27.json`.
