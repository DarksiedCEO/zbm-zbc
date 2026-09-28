# Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and `integration-**`, on every pull request, and on
demand (`workflow_dispatch`). One workflow, one job per service, one `required` check at the end. Every command
below is the one the service's own README prescribes, run from the service directory; nothing was invented for CI.
The commands were exercised locally on this box before the workflow was written (Python 3.12 and 3.13, cargo
1.95, go 1.24.7, Node 22.22) — the Docker live job is the one job that could not be run here (no daemon).

## Branch protection on `main`

GitHub → repository **Settings → Branches → Add branch ruleset** (or a classic branch protection rule) for `main`:

1. **Require a pull request before merging.** Required approvals: at least 1.
2. **Require status checks to pass before merging.** Add exactly one required check: **`required`** (the job of
   that name in `ci.yml`). Do not list the individual jobs — `required` needs every one of them, so it is red if
   any of them failed or was cancelled, and green when each either passed or was legitimately skipped by the
   path filter. Tick **Require branches to be up to date before merging** so the check ran against the merge
   result, not a stale base.
3. **Require linear history** (no merge commits on `main`; squash or rebase merges only).
4. **Do not allow bypassing the above settings** — the "include administrators" / "no bypass" toggle. Admins
   go through the same PR and the same `required` check as everyone else. In a ruleset this is the Bypass list:
   leave it empty.
5. Block force pushes and deletions of `main` (on by default in a ruleset; tick them in a classic rule).

The `required` job runs `if: always()`, so it reports even when upstream jobs are skipped or failed; branch
protection sees a single check name regardless of which matrix entries ran.

## How path filtering works

The `changes` job diffs the pull request base (or, on a push, the previous commit) against `HEAD` and classifies
every changed path with a short script inside the workflow (no third-party action). Rules, in order:

| Changed path | Runs |
|---|---|
| `README.md`, `BUILD_CONTRACTS.md`, `.gitleaks.toml`, `.gitignore`, anything under `docs/`, `fixtures/`, `.github/` | everything |
| `services/<name>-py/**` (any of the ten Python services) | all Python jobs (`python-tests`, `delivery-py`, `live-runs`, `delivery-docker-live`) |
| `services/ledger-rust/**` | `ledger-rust` **and** all Python jobs |
| `services/orchestrator-go/**` | `orchestrator-go` |
| `apps/dashboard-ts/**` | `dashboard-ts` |
| any other path | everything (fail safe) |

The Python services are treated as one cluster on purpose. Their tests read each other's sources, so a change in
one can break another: `finance-py/tests/test_protocol_contracts.py` and `legal-py/tests/contract_maps.py` import
`compliance-py`, `clipper-network-py`, `verification-py`, `creative-py` and `onboarding-py` `ports.py` /
`departments.py` by file path; `clipper-network-py/tests/test_cert_guardrail.py` compares against
`compliance-py/requirements.txt`; `onboarding-py/tests/test_unit_clients.py` reads `detection-py/src/api.py`;
`creative-py/tests/test_fix_wave_5.py` reads `ledger-rust/src/bin/server.rs`; `onboarding-py`'s conftest builds
`ledger-rust` with cargo. Narrower per-service filters would have to enumerate those reads by hand and would go
silently green the first time one was missed.

The audit jobs (`audit-python`, `audit-rust`, `audit-node`) and `secret-scan` always run. With no reliable base
commit (first push of a branch, a force push, `workflow_dispatch`) every job runs.

## Jobs and what each one proves

Runner: `ubuntu-24.04` everywhere (what `ubuntu-latest` resolves to today; pinned by name so a runner-image
migration is a deliberate change). Preinstalled tools relied on and verified against the runner image README
(image 20260920.314.1): Docker 28.0.4 client and server, rustup 1.29 with a stable toolchain, gcc (the Go race
detector needs cgo), `curl`, `python3`. Everything else is installed by a pinned step.

| Job | Command(s) run (from the service directory) | Proves |
|---|---|---|
| `changes` | `git diff --name-only <base> <head>` + the classification script | which suites a change can affect |
| `python-tests (<svc>, 3.12/3.13)` for detection-py, fulfillment-py, onboarding-py, creative-py, compliance-py, verification-py, clipper-network-py, finance-py, legal-py | `python -m pip install -r requirements.txt` then `python -m pytest -q -rs -p no:cacheprovider` | each department's suite (unit, scenario, attack, guardrail and live-launcher tests) on both interpreters. onboarding-py's entry installs Rust stable first because its conftest runs `cargo build --release --bin server` on ledger-rust and drives the real binary; without cargo those tests skip and the count is a lie. |
| `delivery-py (3.12/3.13)` | Node 22 (setup-node) + Go (setup-go, version from `fixtures/dlv/toy-go/go.mod`) + Rust stable (`rustup toolchain install stable --profile minimal`) → `uv sync --frozen --python <v>` (uv 0.8.17, the version `uv.lock` was produced with) → `uvx ruff@0.15.11 check src tests devtools` → `.venv/bin/python -m pytest -q -rs -p no:cacheprovider` | the fix-engine suite plus lint, including `tests/test_toolchains.py`, which runs the REAL `go`, `cargo` and `node` on the toy fixtures through the whole loop (fix wave 20 / R15: without the three toolchains those tests fail on a missing binary instead of skipping, so the job installs them first). `tests/test_live_docker.py` skips here with its printed reason — that is expected; the next job is where it must pass. |
| `ledger-rust` | `rustup toolchain install stable --profile minimal --component clippy` → `cargo test --locked` → `cargo clippy --locked --all-targets -- -D warnings` | 60 unit + 31 integration tests (the integration tests spawn the compiled server over a real TCP socket), clippy clean, and `Cargo.lock` matches `Cargo.toml` (`--locked` fails on drift). |
| `orchestrator-go` | `go vet ./...` → `go test -race -count=1 ./...` (Go version from `go.mod`, 1.24.7) | the orchestrator's tests (three packages) with the race detector; no cached results. |
| `dashboard-ts` | Node 22 → `npm ci` → `npx tsc --noEmit` → `npm run build` → `npm test` → `npm run check:dynamic` | lockfile-exact install, typecheck, production build, the 27 tests (3 start the built server; money vectors shared with the other three languages), and the package's own check that `/` and `/healthz` are dynamic routes. |
| `live-run (<svc>, 3.12/3.13)` for compliance-py, verification-py, clipper-network-py, finance-py, legal-py | `cargo build --locked --release --bin server` in ledger-rust, then `LEDGER_BIN=<that binary> python devtools/live_run.py` | each department's own live integration run: the real ledger-rust binary and the department's production entrypoint as separate processes over real HTTP with real tokens, every narrated behaviour asserted, a restart with the ledger-anchor check, and `GET /ledger/verify` valid on every ledger at the end. Exit 0 only when every check held (33/33 for finance, 28/28 for verification, and so on). verification-py's run also starts compliance-py's production entrypoint, so both requirement files are installed. |
| `delivery-docker-live` | `docker buildx imagetools inspect python:3.12-slim` (base digest, unless the `DLV_SANDBOX_BASE_DIGEST` repository variable is set) → `docker run registry:2` on 127.0.0.1:5000 → `docker build -f services/delivery-py/docker/sandbox.Dockerfile --build-arg BASE_DIGEST=… -t 127.0.0.1:5000/zbm/dlv-sandbox:ci .` → `docker push` → `uv sync --frozen` → `DLV_LIVE_SANDBOX_IMAGE=127.0.0.1:5000/zbm/dlv-sandbox@sha256:<digest> .venv/bin/python -m pytest -q -rs tests/test_live_docker.py --junitxml=…` → a script that fails if any of the three tests was skipped | the sandbox properties only a Docker daemon can prove (ADR 0011 R4/R6, spec C.2): uid 65532 inside, seccomp on, `/var/run/docker.sock` absent, read-only root with `/tmp` and the workspace writable, no route out and no `curl`/`wget` in the image, no `.git` in the copied workspace, the `zbm.dlv.run` label on container and volume, the deadline kills a running process, the volume is gone after destroy. A skip is a failure here, by construction. |
| `audit-python` | `pip-audit==2.10.1`: `pip-audit -r services/<svc>/requirements.txt --strict` for every `*-py/requirements.txt`; for delivery-py, `uv export --frozen --no-hashes --no-emit-project` then `pip-audit -r … --no-deps --disable-pip --strict` on the registry packages | no known advisory against any pinned Python dependency (the two git-sourced deer-flow packages are excluded — see below). |
| `audit-rust` | `cargo install cargo-audit --locked --version 0.22.2` → `cargo audit` | no RustSec advisory against `Cargo.lock` (70 crates). |
| `audit-node` | `npm audit --audit-level=high` | no high or critical advisory against `package-lock.json`. |
| `secret-scan` | gitleaks 8.30.1 release binary (sha256 `551f6fc8…` verified before use) → `gitleaks git --no-banner --redact --exit-code 1 --config .gitleaks.toml .` over the full history | no secret in any commit. `.gitleaks.toml` allowlists four exact strings (two fake `AKIA…` shapes, one fake token, one fake cued password) that onboarding-py's redaction tests use as inputs; it exempts no file and no path. |
| `required` | reads the result of every job above | one green check for branch protection; red if any job failed or was cancelled; a job skipped by the path filter is fine. |

## Pins

- Actions, by commit SHA (tag in the trailing comment; resolved with `git ls-remote --tags` on 2026-09-27):
  `actions/checkout` v5.1.0, `actions/setup-python` v6.3.0, `actions/setup-node` v5.0.0, `actions/setup-go`
  v6.5.0, `astral-sh/setup-uv` v7.6.0, `Swatinem/rust-cache` v2.9.2. No other third-party action is used: Rust
  comes from the runner's `rustup`, gitleaks and cargo-audit are installed by version, path filtering is a script.
- Tools by version: uv 0.8.17, ruff 0.15.11, pip-audit 2.10.1, cargo-audit 0.22.2, gitleaks 8.30.1 (checksum
  verified). Language runtimes: Python 3.12 and 3.13 (both, because a 3.13-only breakage has been missed before),
  Go from `go.mod`, Rust stable, Node 22.
- Concurrency: one run per ref; a newer push to a pull request cancels the older run. Pushes to `main` and
  `integration-*` are never cancelled.
- `permissions: contents: read` for the whole workflow.

## Deliberately NOT in CI yet

- **The Revenue Recovery three-process live run** (ledger-rust + detection-py + orchestrator-go, the run that
  produced `ledger_verify: valid: true` in the root README's review table). No script for it exists in the
  repository — it was driven by hand — so it is not wired. Writing one is the next step; do not add a job that
  claims it until the script exists and exits non-zero on a mismatch like the five department `live_run.py`s do.
- **creative-py's `devtools/live_smoke.py`** and **onboarding-py's live server** as separate live-run jobs. The
  smoke script needs an already-running creative-py and ledger and does not start them itself; onboarding-py's
  live ledger-rust tests run inside its pytest suite (and therefore in `python-tests`), so nothing is lost.
- **The deer-flow harness packages in the Python audit.** `deerflow-harness` and `deerflow-extension-api` are
  git-sourced at commit `345f08be` in `uv.lock`; pip-audit has no registry version to look up for them and refuses
  URL requirements, so they are filtered out of the export before the audit. Their transitive dependencies (187
  registry packages) are audited.
- **A pinned base digest for the sandbox image.** `docker/sandbox.Dockerfile` requires `python:3.12-slim`'s digest
  as a build argument and ADR 0011 says it is recorded at first build; nothing is recorded yet. Until the
  `DLV_SANDBOX_BASE_DIGEST` repository variable is set, `delivery-docker-live` resolves the current digest at run
  time and emits a workflow warning with the value — an unpinned float, stated plainly. Record it in ADR 0011 and
  set the variable to close the gap. Likewise `registry:2` (the throwaway local registry) is pulled by tag.
- **cargo fmt.** The ledger crate is not rustfmt-clean today and the README does not require it; adding
  `cargo fmt --check` is a formatting change first, then a CI change.
- **Building or publishing container images** for the services (`docker/Dockerfile`), deployments, and any job
  that needs a secret. Nothing in this workflow uses a secret.
- **Coverage thresholds, benchmarks, mutation testing.**
- **delivery-py's `devtools/audit_gate.py` and `licence_gate.py`** as gates (the licence gate's allowlist and
  the advisory gate's `pip-audit` dependency need their own decision about what a failure means in CI).

## Known behaviours worth knowing before you debug a red run

- The READMEs say `uv sync --frozen --no-dev` for delivery-py; that leaves no `pytest` in the venv (`pytest` is
  in the `dev` dependency group in `pyproject.toml` / `uv.lock`). CI runs `uv sync --frozen` (dev group included).
- delivery-py's `tests/test_live_launcher.py` requires the venv to be at `services/delivery-py/.venv` (it spawns
  `.venv/bin/python`); CI uses uv's default location, so this holds. It also rewrites the tracked file
  `services/delivery-py/docs/evidence/dept28/live-launcher-run.log` when it runs — harmless in CI.
- onboarding-py's suite is the slow one (~6 minutes plus a cold ledger-rust release build); the
  `test_procinfo.py` copies in creative-py, fulfillment-py and onboarding-py skip one IPv6 case on hosts without
  an IPv6 loopback and say so.
- `-rs` is passed everywhere so any skip prints its reason in the log; a skip in `delivery-docker-live` fails the
  job.
