# delivery-py — Client Delivery & Operations (28): agent runtime adapters + the AEGIS fix engine

Built from `DEPT28_SPEC.md` rev 1 and the AEGIS audit of deer-flow v2.1.0 / Superpowers v6.4.2. Architecture, every
pinned hash, the 28 choices made where the spec was silent, and what is not proven on this box:
`docs/adr/0011-delivery-department-architecture.md`.

## What it does

`POST /dlv/v1/fix-runs` takes an AEGIS findings document. The engine (never the agent) verifies the base commit,
opens a worktree on `fix<N>-<service>` (the repository must have no remotes), starts one Docker container per run
for the AGENT (non-root, no capabilities, read-only root, `--network none`, no `.git`, no `curl`/`wget`,
digest-pinned image, labelled `zbm.dlv.run=<run_id>` for the reaper), copies the worktree in, and then — per
finding — has the embedded deer-flow engineer write a failing test and fix the root cause. **Every verdict runs in
a FRESH container the agent never had a process in** (fix wave 20, R1): the engine builds the tree on the host
(`git archive` of the base + exactly the worktree paths that run may see), ships it by `docker cp` into a new
container on a new volume, runs, reads its result files back and destroys the container. The checks per finding:
RED (the test fails on the base tree), GREEN (passes with everything the agent changed), the verification
checkout (base + the SOURCE changes + the RED test file only: must pass), the reverted checkout (base + the RED
test only: must fail), the **single-file revert** (the whole fix EXCEPT the finding's file: the RED test must
fail — the test is tied to the finding's file, and that file must carry a hunk of the fix), the **finding's own
reproduction** — always: every finding must name a test node id (`<path>::<name>`) of the service's own test
runner that exists at the base commit, or the document is refused `422 reproduction_not_runnable` before a run
exists (wave 21; a `fail` review's reopened/new findings are checked the same way against the run's head before
the review is recorded) — which must pass in the verification checkout and fail in the reverted one, run alone in
its own container, the **src-only check** (every baseline failure the fix claims must
pass on base + the source changes alone), the whole suite on the exact tree that is then committed (its content
digest is recorded and re-derived from the commit; **outcome deltas are verdicts**: a test that was passed/failed at
baseline and is skipped or missing afterwards fails the round, and no `CHANGED_TEST:` excuses it or may touch an
open finding's reproduction), then the commit. Every changed path is classified (`src` / `test` / `test_infra` —
a test-infra change, a deleted test or a test/source file matching the content rules fails the round). Every test
run is the engine's own invocation with a per-ecosystem verdict (`src/zbm_delivery/engine/toolchains.py`): pytest —
`-c <engine ini>`, `--rootdir`, `-p no:cacheprovider`, `--disable-plugin-autoload`, `-p zbm_engine_plugin` (the
engine's own pinned plugin, first on `pythonpath`; its per-test four-position record must agree with junit, and
any plugin registered from inside the test session is a violation), `--junitxml` to an engine path, cross-checked
against `--collect-only`, the summary line and the exit code; Go — `go test -json -count=1 -race` events
cross-checked against `go test -json -list` (per package), the package results and the exit code; Rust (stable,
no nightly, no nextest) — `cargo test --locked --offline --no-fail-fast` per-test lines cross-checked against
`-- --list`, every binary's `running`/`test result` lines and the exit code, into an engine-owned `--target-dir`
with the tree touched before every run; Node 22 — `node --test --test-reporter=junit` to an engine path
cross-checked against the TAP stream and the exit code (Node has no collect-only mechanism; stated). Anything that
disagrees, times out, is truncated or collects nothing is `unknown` and never counts as green. A `DISPROOF:` is
honoured only when the finding's own reproduction (a test node id named in the findings document) passes on the
untouched base tree. The report is written from the engine's records; the run ends `awaiting_review` for AEGIS
(`POST …/review`), which can reopen findings into a new run on the same branch. Every tool call is decided by our
guardrail and recorded on the ledger BEFORE it runs (a bash command must be a single line; an interpreter/shell/
make/linter call is recorded `allow_opaque`, but a pipe or here-string INTO one is refused however it is spelled;
a hard link (`ln` without `-s`, `cp -l`/`--link`) or `mv` whose source is outside the write roots is refused; write operands are resolved inside the container by a pinned
read-only helper in ONE exec, after the decision is recorded, capped at 16 operands and 64 components; file-tool
writes are contained to `services/<service>/` and `docs/adr/00NN-*.md`); `git push`/`merge`/remote operations,
network, deletion outside the service directory, ACP/MCP, subagents and self-modification are denied
unconditionally. A cancel or the deadline kills the run's containers and nothing is recorded on the run afterwards.
Nothing the agent's process prints is ever a count, and the ENGINE never claims a finding is fixed (wave 23): its
per-finding end state is `candidate_passed_checks` — every check above passed, which is necessary, not sufficient;
the report says so in its header ("Checks passed are necessary, not sufficient. This diff has not been reviewed.").

**Wave 22 (AEGIS round 21).** The finding's reproduction is ALSO run outside the test runner — pytest services:
the pinned `adapters/tools/zbm_standalone_runner.py` calls the test function with pytest not importable and
`CI`/`PYTEST*`/`TEST*` removed from the process environment; go/cargo/node: the toolchain with the CI markers unset —
and must pass with the fix and fail on the reverted checkout; a fix that only works under the runner fails the
round, and a reproduction that needs pytest (fixtures, `pytest.raises`, parametrization, a `conftest.py` on its
path) ends `needs_review_runner_dependent` (committed; wave 23: a conftest no longer decides it — see below).
`src_content_deny` refuses the cheap runner-detection spellings in the lines a fix adds. A reviewer-authored
reproduction's RED check is part of admission, under the service lock: a check that cannot complete or verify is
`422 reproduction_red_unverified`, never an unchecked admission. An engine container whose start raced a cancel is
killed and recorded `engine_box_killed_after_cancel`, never `engine_box_started`. `DLV_TEST_PORT_RANGE` now reaches
the live tests; `DLV_DRAINS_MAX` (default 512) caps the HTTP graceful-close drains (ADR 0003 §9).

**Wave 23 (AEGIS round 22; founder design change D1-D3).** The engine flags instead of chasing detectors. *D1:*
`fixed` is renamed `candidate_passed_checks`; `accepted` (and `reopened`) are set ONLY by `POST …/review` from the
`aegis` caller with `finding_verdicts` — one `{finding_id, verdict: accept|reopen, note}` per finding (a pass needs an
accept for every finding); records written as `fixed` read as `candidate_passed_checks`. *D2:* every line a finding's
commit adds to a SOURCE file that uses one of the listed SPELLINGS of a construct that can observe the execution
context (a spelling list — wave 24: it proves nothing by its silence) (`sys.modules`, `sys.argv`, `sys.flags`, frames,
`inspect`, `traceback`, the environment, `__import__`/`importlib`, `globals()`/`vars()`/`getattr` on modules,
`__main__`, `atexit`, `signal`, `threading.enumerate`, `gc.get_objects`, `builtins`, names built from string pieces;
the Go/Rust/Node equivalents) is a review flag `<finding>-F001` (file:line, construct, reason), recorded
`review_flags_recorded`, listed FIRST in the report, and a review that accepts the finding must name each flag id in
`flags_addressed`. *D3:* a standalone run that executed is authoritative (a `fail` with the fix fails the round
whatever conftest is present); a pytest import refused from a SOURCE frame fails the round
(`fix_imports_test_runner`); a TEST that cannot run outside pytest ends `needs_review_runner_dependent` with the flag
`<finding>-RD`, accepted only with a review note. Also: a failing review while another run of the service is in
flight is refused 409 before anything is recorded, and its child run is created in the same operation; the
admission RED check of reviewer tests runs its containers WITHOUT the service lock (a recorded pending admission
holds the service's run slot); `wait_idle` waits for the engine thread to return; the kill of a container that
started after a cancel is recorded first (`sandbox_kill_requested`), and an unrecordable one still kills and marks
the run `unrecorded_failure`. ADR 0011, "Round 22 amendments".

**Wave 24 (AEGIS round 23; lead rulings E1-E6).** The flags are a spelling list and the engine claims nothing beyond
it: the report's flags section opens with "These flags come from a spelling list. They are an aid, not a guarantee:
absence of flags proves nothing. Read the full source diff below." and never says "none"; the scan also reads
aliases and star imports of the listed modules, `eval`/`exec`/`compile` of a non-literal, `/proc` and
`conftest`/`pytest`/`test` literals, and a source import of a test framework (also refused). Every report embeds the
COMPLETE source diff of the run (renames off: a moved file is shown in full) and its `src_diff_sha256`; a review that
accepts any finding must carry that hash (else `422 diff_not_attested`) and a real note on every flag
(`flags_addressed: [{flag_id, note}]`: ≥ 20 characters, not one repeated character, no two flag notes alike). A
runner-dependent fix checkout whose reverted checkout executed, or a `SkipTest` raised from source, fails the round;
the standalone runner's notion of "test side" is the engine's own (`path_class`, pinned). A legacy run awaiting review
is re-scanned at start-up (`run_rescanned_for_review`) and its old report is never served. Pending admissions can be
cancelled (the slot is freed at once); a replay of an admission that was cancelled, or refused after its RED check
ran, gets the same recorded answer and runs nothing again (`admission_closed`). The suite writes nothing into the source tree. ADR 0011, "Round 23
amendments".

## Running it

```bash
cd services/delivery-py
uv sync --frozen                     # python 3.12 or 3.13 (pytest is in the dev group); the harness comes from the pinned deer-flow git source (uv.lock)
.venv/bin/python -m pytest -q        # 651 tests (wave 23), no network, no Docker needed (the Docker live module skips with its reason);
                                     # cargo, go and node must be on PATH (the toolchain module runs the toy fixtures for real);
                                     # the live tests need a free port in 18800-18849 (DLV_TEST_PORT_RANGE=lo-hi moves them);
                                     # passes with TMPDIR behind a symlink too (wave 21, N20-D-4); live-run logs go to the
                                     # gitignored docs/evidence/dept28/_runs/ — no test rewrites a tracked file (N20-D-1)
ruff check src tests devtools

# a clean environment: the gate refuses ANY name outside the allowlist (DLV_*, LEDGER_SERVICE_*, PATH, HOME, LANG,
# LC_ALL, LC_CTYPE, TZ, DOCKER_HOST, HTTPS_PROXY/HTTP_PROXY/NO_PROXY, SSL_CERT_FILE, TMPDIR); DLV_REPO_PATH must be a
# local repository with NO remotes (round 18 R4: a run worktree shares its config, and `git push` must have nowhere
# to go); DLV_SANDBOX_NETWORK is `none` (the only value) and DLV_MAX_SUBAGENTS_PER_RUN stays unset (subagents are off)
env -i PATH="$PATH" HOME="$HOME" \
  DLV_SERVICE_TOKEN=<>=32 chars> DLV_CALLER_TOKENS='{"aegis": "<>=32>", "andre_session": "<>=32>", "scheduler": "<>=32>"}' \
  DLV_ANDRE_APPROVAL_TOKEN=<Andre's secret, reconcile only> \
  DLV_SANDBOX_IMAGE=<registry>/zbm/dlv-sandbox@sha256:<digest> DLV_IMAGE_REGISTRY=<registry> \
  DLV_REPO_PATH=/srv/zbm-zbc DLV_WORKTREES_DIR=/srv/zbm-worktrees DLV_BASE_REF=integration-2026-09-24 \
  DLV_DATA_DIR=/srv/dlv-data LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret> \
  DLV_LLM_PROVIDER=anthropic DLV_LLM_MODEL=<model id> DLV_LLM_API_KEY_REF=vault:<ref> DLV_EGRESS_ALLOW_HOSTS='["api.anthropic.com"]' \
  sh -c 'cd src && ../.venv/bin/python -m zbm_delivery.api'     # DLV_BIND_ADDR 127.0.0.1, DLV_PORT 8430
```

Day one: with no Docker daemon `/health` says `sandbox: unavailable` and every run is 503 `SANDBOX_UNAVAILABLE`;
with no provider key (the vault is not wired; `env:DLV_*` references only with `DLV_NON_PRODUCTION=1`) every run
is 503 `LLM_NOT_CONFIGURED`; with no ledger every write is 503. Nothing is queued for later.

## Routes

| Route | Caller | Purpose |
|---|---|---|
| `GET /health` | none | status, `in_memory`, `ledger`, `sandbox`, `llm`, `non_production`, the config/prompts hashes, policy version, deer-flow commit |
| `POST /dlv/v1/fix-runs` | aegis, andre_session | ingest a findings document → 202 `{run_id, status, request_id, facts_sha256}` |
| `GET /dlv/v1/fix-runs/{id}` · `/findings` · `/report` · `/evidence/{evidence_id}` | any caller | the run, its finding records, the report (markdown), an evidence file (content-addressed, hash-checked on read) |
| `POST /dlv/v1/fix-runs/{id}/review` | aegis | wave 23: `finding_verdicts` (accept/reopen + note per finding; a pass accepts EVERY finding, a runner-dependent accept needs a note ≥ 20 chars); wave 24: `src_diff_sha256` (the run's, when anything is accepted: else 422 `diff_not_attested`) and `flags_addressed` = `[{flag_id, note}]` for every flag of each accepted finding (a real note each); accepted findings → `accepted` (the only route); `pass` → terminal; `fail` → the reopened ∪ new findings enter a new run on the same branch (`next_run_id`, created in the same operation as the review); 409 before anything is recorded while another run of the service is in flight |
| `POST /dlv/v1/fix-runs/{id}/cancel` | aegis, andre_session | stops a live run (`failed`, evidence kept); wave 24: also cancels a pending admission (its id from `reproduction_red_check_started`), freeing the service's slot |
| `GET /dlv/v1/policy` | any caller | tool classes, test commands, the pinned hashes (no seed text) |
| `GET /dlv/v1/audit/export` | any caller | the local log in order with ledger ids and the chain check |
| `GET|POST /dlv/v1/reconcile` | Andre token + `DLV_RECONCILE_MODE=1` | ADR 0006 N15 reconcile of a split local log |

Headers: `Authorization: Bearer <DLV_SERVICE_TOKEN>`, `X-DLV-Caller-Token`, `X-Andre-Approval-Token` (reconcile only).

## Layout

`src/zbm_delivery/` (see ADR 0011's module map) · `config/deerflow.engine.yaml` (pinned) · `seed/` (pinned) ·
`prompts/` (the Superpowers forks, `CHANGES.md`) · `skills/` (empty, manifested) · `docker/` · `devtools/` ·
`tests/` (scenarios S1-S12, attacks A1-A13, the 255-subset property, guardrails G1-G14, live L1/L2, the round-18
findings N18-S-1..9 / N18-E-1..7 in `test_round18.py`, the round-19 findings N19-E-1..6 / N19-A-1..13 in
`test_round19.py` and `test_live_round19.py`) · `src/zbm_delivery/adapters/tools/` (the pinned resolver and pytest
plugin shipped into every container) ·
`docs/evidence/` (licence report, pip-audit result, `dept28/` evidence pack and the live launcher log).

Regenerating a manifest or a pin (an ADR amendment): `.venv/bin/python devtools/gen_manifests.py --write --pin`.
Licence gate on the venv: `.venv/bin/python devtools/licence_gate.py --write`. Advisory gate:
`.venv/bin/python devtools/audit_gate.py` (needs pip-audit on PATH; otherwise `audit: not_run`).
