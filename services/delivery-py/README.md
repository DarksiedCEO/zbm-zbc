# delivery-py — Client Delivery & Operations (28): agent runtime adapters + the AEGIS fix engine

Built from `DEPT28_SPEC.md` rev 1 and the AEGIS audit of deer-flow v2.1.0 / Superpowers v6.4.2. Architecture, every
pinned hash, the 28 choices made where the spec was silent, and what is not proven on this box:
`docs/adr/0011-delivery-department-architecture.md`.

## What it does

`POST /dlv/v1/fix-runs` takes an AEGIS findings document. The engine (never the agent) verifies the base commit,
opens a worktree on `fix<N>-<service>`, starts one Docker container per run (non-root, no capabilities, read-only
root, internal network, digest-pinned image), copies the worktree in, runs the suite baseline, and then — per
finding — has the embedded deer-flow engineer write a failing test (RED, run by the engine), fix the root cause
(GREEN, run by the engine), passes the revert check (stash the fix → must fail → restore → must pass), runs the
whole suite, and commits. The report is written from the engine's records; the run ends `awaiting_review` for
AEGIS (`POST …/review`), which can reopen findings into a new run on the same branch. Every tool call is decided
by our guardrail and recorded on the ledger BEFORE it runs; `git push`/`merge`/remote operations, network,
deletion outside the workspace, ACP/MCP and self-modification are denied unconditionally. Nothing is ever
marked fixed on the agent's word.

## Running it

```bash
cd services/delivery-py
uv sync --frozen --no-dev            # python 3.12; the harness comes from the pinned deer-flow git source (uv.lock)
.venv/bin/python -m pytest -q        # 391 tests, no network, no Docker needed (the Docker live module skips with its reason)
ruff check src tests devtools

# a clean environment: the gate refuses ANY name outside the allowlist (DLV_*, LEDGER_SERVICE_*, PATH, HOME, LANG,
# LC_ALL, LC_CTYPE, TZ, DOCKER_HOST, HTTPS_PROXY/HTTP_PROXY/NO_PROXY, SSL_CERT_FILE, TMPDIR)
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
| `POST /dlv/v1/fix-runs/{id}/review` | aegis | `pass` → terminal; `fail` → the reopened ∪ new findings enter a new run on the same branch (`next_run_id`) |
| `POST /dlv/v1/fix-runs/{id}/cancel` | aegis, andre_session | stops a live run (`failed`, evidence kept) |
| `GET /dlv/v1/policy` | any caller | tool classes, test commands, the pinned hashes (no seed text) |
| `GET /dlv/v1/audit/export` | any caller | the local log in order with ledger ids and the chain check |
| `GET|POST /dlv/v1/reconcile` | Andre token + `DLV_RECONCILE_MODE=1` | ADR 0006 N15 reconcile of a split local log |

Headers: `Authorization: Bearer <DLV_SERVICE_TOKEN>`, `X-DLV-Caller-Token`, `X-Andre-Approval-Token` (reconcile only).

## Layout

`src/zbm_delivery/` (see ADR 0011's module map) · `config/deerflow.engine.yaml` (pinned) · `seed/` (pinned) ·
`prompts/` (the Superpowers forks, `CHANGES.md`) · `skills/` (empty, manifested) · `docker/` · `devtools/` ·
`tests/` (scenarios S1-S12, attacks A1-A13, the 255-subset property, guardrails G1-G14, live L1/L2) ·
`docs/evidence/` (licence report, pip-audit result, `dept28/` evidence pack and the live launcher log).

Regenerating a manifest or a pin (an ADR amendment): `.venv/bin/python devtools/gen_manifests.py --write --pin`.
Licence gate on the venv: `.venv/bin/python devtools/licence_gate.py --write`. Advisory gate:
`.venv/bin/python devtools/audit_gate.py` (needs pip-audit on PATH; otherwise `audit: not_run`).
