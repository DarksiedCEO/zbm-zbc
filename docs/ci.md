# Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and `integration-**`, on every pull request, and on
demand (`workflow_dispatch`). One workflow, one job per service, one `required` check at the end. Every command
below is the one the service's own README prescribes, run from the service directory; nothing was invented for CI.
Since fix wave 25 every suite runs inside `devtools/hygiene_check.py run` (section "Hygiene" below). The commands
were exercised locally before the workflow was written (wave 21: Python 3.12 and 3.13, cargo 1.95, go 1.24.7, Node
22.22; wave 25: every suite under the hygiene wrapper, see that wave's report) — the Docker live job and the macOS
entries are the ones that could not be run here (no daemon, no Mac). No CI run of this workflow exists yet.

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
| `README.md`, `.gitleaks.toml`, `.gitignore`, anything under `docs/`, `fixtures/`, `devtools/`, `.github/` | everything |
| `services/<name>-py/**` (any of the sixteen Python services) | all Python jobs (`python-tests`, `delivery-py`, `live-runs`, `delivery-docker-live`) |
| `services/ledger-rust/**` | `ledger-rust` **and** all Python jobs |
| `services/orchestrator-go/**` | `orchestrator-go` |
| `apps/dashboard-ts/**` | `dashboard-ts` |
| any other path | everything (fail safe) |

The Python services are treated as one cluster on purpose. Their tests read each other's sources, so a change in
one can break another: `finance-py/tests/test_protocol_contracts.py` and `legal-py/tests/contract_maps.py` import
`compliance-py`, `clipper-network-py`, `verification-py`, `creative-py` and `onboarding-py` `ports.py` /
`departments.py` by file path; `clipper-network-py/tests/test_cert_guardrail.py` compares against
`compliance-py/requirements.txt`; `clipper-network-py/tests/contract_vi_runner.py` imports `verification-py/src`
and `verification-py/tests/helpers.py` (fix wave 26b, W25-EB-R2); `onboarding-py/tests/test_unit_clients.py` reads
`detection-py/src/api.py`;
`creative-py/tests/test_fix_wave_5.py` reads `ledger-rust/src/bin/server.rs`; `onboarding-py`'s conftest builds
`ledger-rust` with cargo. Narrower per-service filters would have to enumerate those reads by hand and would go
silently green the first time one was missed.

The audit jobs (`audit-python`, `audit-rust`, `audit-node`) and `secret-scan` always run. With no reliable base
commit (first push of a branch, a force push, `workflow_dispatch`) every job runs.

## macOS (fix wave 21; runner moved in fix wave 25)

The founder's rule: the Mac checks run without him. Every job that ran only on Linux and has a Mac failure history
(or could have one) now also runs on **`macos-26`** — GitHub's Apple Silicon (arm64) runner, pinned by name like
`ubuntu-24.04`.

Why not `macos-14` any more (scout C3-2, HIGH): GitHub's runner-images README
(`https://raw.githubusercontent.com/actions/runner-images/main/README.md`, read 2026-10-01, sha256 `7691efc4…`)
marks "macOS 14 Arm64" (label `macos-14`) **deprecated** and carries the announcement "[macOS] The macOS 14 Sonoma
based runner images will begin deprecation on July 6th and will be fully unsupported by November 2nd for GitHub
Actions and Azure DevOps" (actions/runner-images#13518). From 2026-11-02 the twelve `macos-14` entries could get no
runner, and a brownout before that cancels them — `required` is red on a cancelled job. The same table lists
"macOS 26 Arm64" with the labels `macos-latest`, `macos-26` or `macos-26-xlarge` (GA, not deprecated) and "macOS 15
Arm64" (`macos-15`). `macos-26` was chosen: the newest GA arm64 image, the one `macos-latest` points to, so the
longest runway before the next forced move; it is pinned by name (not `macos-latest`) so a future image migration is
a deliberate change. Its image README (`images/macos/macos-26-arm64-Readme.md`, read the same day) lists Rust/Cargo
1.98.1, rustup 1.29.0 and Python 3.14.7 preinstalled; the jobs install their own Python (setup-python), Node 22
(setup-node) and Go (setup-go). Re-check the README's deprecation column when a wave touches CI.

| Job | macOS entries |
|---|---|
| `python-tests` | one per service (all fifteen) on Python 3.13, next to the 30 Linux entries (3.12 + 3.13) |
| `delivery-py` | one entry, Python 3.13, with the same Node 22 / Go / Rust / uv steps as on Linux |
| `ledger-rust` | `cargo test --locked` + clippy, same as Linux |
| `orchestrator-go` | `go vet` + `go test -race`, same as Linux |

They report as separate checks (e.g. `python-tests (creative-py, 3.13, macos-26)`, `ledger-rust (macos-26)`) and
all of them feed the one `required` check: a red Mac entry is a red `required`. One Python version on macOS keeps
the (roughly ten times more expensive) macOS minutes bounded; the 3.12/3.13 split is still covered on Linux.

GitHub's macOS runners have **no Docker daemon**, so `delivery-docker-live` stays Linux-only; delivery-py's
`tests/test_live_docker.py` skips on the macOS entry with its printed reason, exactly as it does in the Linux
`delivery-py` job, and must pass (no skip allowed) in `delivery-docker-live`. The `live-runs` and audit jobs are
unchanged (Linux only: they prove cross-process behaviour and dependency advisories, not OS behaviour).

What this does not prove: that the founder's own Mac (its macOS version, Python build, Homebrew toolchains, a
Docker Desktop VM) behaves like GitHub's `macos-26` image. The first CI run on this workflow is the first time
these suites run on macOS without a person; its results were not available when this was written.

## Jobs and what each one proves

Runner: `ubuntu-24.04` everywhere except the macOS entries above (what `ubuntu-latest` resolves to today; pinned by
name so a runner-image migration is a deliberate change). Preinstalled tools relied on, checked against the runner
image README (`https://raw.githubusercontent.com/actions/runner-images/main/images/ubuntu/Ubuntu2404-Readme.md`, read
2026-10-01 by fix wave 25, scout C3-11: "Image Version: 20260920.314.1", Docker Client and Server 28.0.4, Rustup
1.29.1, Cargo/Rust 1.98.1, GNU C++ 12.4/13.3/14.2 — the Go race detector needs cgo —, curl 8.5.0, Python 3.12.3).
Everything else is installed by a pinned step. **Rust is not pinned:** every Rust-using job runs `rustup toolchain
install stable`, which floats with the stable channel (scout C3-12; a new stable can add a clippy lint and turn
`ledger-rust` red without a code change — pin a version in `rust-toolchain.toml` if that becomes a problem).

| Job | Command(s) run (from the service directory) | Proves |
|---|---|---|
| `changes` | `git diff --name-only <base> <head>` + the classification script | which suites a change can affect |
| `python-tests (<svc>, 3.12/3.13, ubuntu-24.04)` and `(<svc>, 3.13, macos-26)` for detection-py, fulfillment-py, onboarding-py, creative-py, compliance-py, verification-py, clipper-network-py, finance-py, legal-py, security-py, sales-py, service-py, influencer-py, bizdev-py, clientfix-py | `python -m pip install -r requirements.txt` then `python devtools/hygiene_check.py run --suite python:<svc> --kind pytest --cwd services/<svc> -- python -m pytest -q -rs -p no:cacheprovider` | each department's suite (unit, scenario, attack, guardrail and live-launcher tests) on both interpreters. onboarding-py's entry installs Rust stable first because its conftest runs `cargo build --release --bin server` on ledger-rust and drives the real binary; without cargo those tests skip and the count is a lie. |
| `delivery-py (3.12/3.13, ubuntu-24.04)`, `delivery-py (3.13, macos-26)` | Node 22 (setup-node) + Go (setup-go, version from `fixtures/dlv/toy-go/go.mod`) + Rust stable (`rustup toolchain install stable --profile minimal`) → `uv sync --frozen --python <v>` (uv 0.8.17, the version `uv.lock` was produced with) → `uvx ruff@0.15.11 check src tests devtools` → the same pytest command under the hygiene check (the `.venv` is the one allowlisted ignored path) | the fix-engine suite plus lint, including `tests/test_toolchains.py`, which runs the REAL `go`, `cargo` and `node` on the toy fixtures through the whole loop (fix wave 20 / R15: without the three toolchains those tests fail on a missing binary instead of skipping, so the job installs them first). `tests/test_live_docker.py` skips here with its printed reason — that is expected; the next job is where it must pass. |
| `ledger-rust (ubuntu-24.04 / macos-26)` | `rustup toolchain install stable --profile minimal --component clippy` → `cargo test --locked` (under the hygiene check; `target/` allowlisted) → `cargo clippy --locked --all-targets -- -D warnings` | the unit and integration tests (counts: docs/test-counts.md; the integration tests spawn the compiled server over a real TCP socket, on a port the server picks and reports back), clippy clean, and `Cargo.lock` matches `Cargo.toml` (`--locked` fails on drift). |
| `orchestrator-go` | `go vet ./...` → `go test -race -count=1 -v ./...` under the hygiene check (Go version from `go.mod`, 1.24.7; `GOTELEMETRY=off`) | the orchestrator's tests (three packages) with the race detector; no cached results; the binary tests start the real orchestrator on port 0 and read its port file. |
| `dashboard-ts` | Node 22 → `npm ci` → `npm run lint` (`tsc --noEmit`) → `npm run build` → `npm test` under the hygiene check → `npm run check:dynamic` (job env `NODE_DISABLE_COMPILE_CACHE=1`: npm otherwise leaves `node-compile-cache` in the suite's private TMPDIR, an R3 failure; `NEXT_TELEMETRY_DISABLED=1`) | lockfile-exact install, typecheck, production build, the tests (the live ones start the built server on a port it picks; any skip fails the job; money vectors shared with the other three languages), and the package's own check that `/` and `/healthz` are dynamic routes. |
| `live-run (<svc>, 3.12/3.13)` for compliance-py, verification-py, clipper-network-py, finance-py, legal-py, security-py, sales-py, service-py, influencer-py, bizdev-py, clientfix-py | `cargo build --locked --release --bin server` in ledger-rust, then `LEDGER_BIN=<that binary> LIVE_WORK_DIR=$RUNNER_TEMP/live-work python devtools/live_run.py` under the hygiene check (`--kind none`: no test count; temp files and processes must not outlive the run), then the work dir is listed and removed | each department's own live integration run: the real ledger-rust binary and the department's production entrypoint as separate processes over real HTTP with real tokens, every narrated behaviour asserted, a restart with the ledger-anchor check, and `GET /ledger/verify` valid on every ledger at the end. Exit 0 only when every check held (each script prints its own N/N). verification-py's run also starts compliance-py's production entrypoint, so both requirement files are installed. |
| `delivery-docker-live` | base digest = the job's `RECORDED_BASE_DIGEST` (= ADR 0011; the `DLV_SANDBOX_BASE_DIGEST` repository variable, if set, must equal it or the step fails; `docker buildx imagetools inspect python:3.12-slim` only reports, as a notice, when upstream has moved — fix wave 26b, R26-4) → `docker run registry:2` on 127.0.0.1:5000 → `docker build -f services/delivery-py/docker/sandbox.Dockerfile --build-arg BASE_DIGEST=… --build-arg NODE_VERSION=… --build-arg NODE_SHA256=… --build-arg GO_VERSION=… --build-arg GO_SHA256=… --build-arg RUSTUP_VERSION=… --build-arg RUSTUP_INIT_SHA256=… --build-arg RUST_TOOLCHAIN=… -t 127.0.0.1:5000/zbm/dlv-sandbox:ci .` (every pin from the job's env = ADR 0011; `devtools/test_ci_docker_args.py` checks both) → `docker push` → `uv sync --frozen` → `DLV_LIVE_SANDBOX_IMAGE=127.0.0.1:5000/zbm/dlv-sandbox@sha256:<digest> .venv/bin/python -m pytest -q -rs tests/test_live_docker.py --junitxml=…` → a script that fails if any of the three tests was skipped | the sandbox properties only a Docker daemon can prove (ADR 0011 R4/R6, spec C.2): uid 65532 inside, seccomp on, `/var/run/docker.sock` absent, read-only root with `/tmp` and the workspace writable, no route out and no `curl`/`wget` in the image, no `.git` in the copied workspace, the `zbm.dlv.run` label on container and volume, the deadline kills a running process, the volume is gone after destroy. A skip is a failure here, by construction. |
| `audit-python` | `pip-audit==2.10.1`: `pip-audit -r services/<svc>/requirements.txt --strict` for every `*-py/requirements.txt`; for delivery-py, `uv export --frozen --no-hashes --no-emit-project` then `pip-audit -r … --no-deps --disable-pip --strict` on the registry packages | no known advisory against any pinned Python dependency (the two git-sourced deer-flow packages are excluded — see below). |
| `audit-rust` | `cargo install cargo-audit --locked --version 0.22.2` → `cargo audit` | no RustSec advisory against `Cargo.lock` (70 crates). |
| `audit-node` | `npm audit --audit-level=high` | no high or critical advisory against `package-lock.json`. |
| `secret-scan` | gitleaks 8.30.1 release binary (sha256 `551f6fc8…` verified before use) → `gitleaks git --no-banner --redact --exit-code 1 --config .gitleaks.toml .` over the full history | no secret in any commit. `.gitleaks.toml` allowlists exact strings only: four (two fake `AKIA…` shapes, one fake token, one fake cued password) that onboarding-py's redaction tests use as inputs, two fake Stripe test-key shapes used by finance-py's simulated Stripe, the retired test-only PII hash key still in sales-py's history (`tests/helpers.py`, replaced in 6072160), and the fake tax id still in bizdev-py's history (`tests/test_aegis_r1.py` in 22fe413, built from parts since); it exempts no file and no path. |
| `hygiene-static` | Python 3.13 (setup-python) + `pip install pytest==9.1.1` (the services' pin; the self-test drives a planted pytest suite through the plugin) → `python -B -m unittest devtools/test_hygiene_check.py` (and `devtools/test_ci_docker_args.py`; `-B`: no `devtools/__pycache__/` left in a developer's checkout, F-11) → `python devtools/hygiene_check.py lint --strict-allowlist` → `python devtools/hygiene_check.py counts --check` | the checker's self-test (each rule fails on a planted violation in a throwaway repository, the clean probe passes), then the static hygiene rules over the whole tree (below), then one row per suite in `docs/test-counts.md`; always runs. |
| `required` | reads the result of every job above | one green check for branch protection; red if any job failed or was cancelled; a job skipped by the path filter is fine. |

## Pins

- Actions, by commit SHA (tag in the trailing comment; resolved with `git ls-remote --tags` on 2026-09-27):
  `actions/checkout` v5.1.0, `actions/setup-python` v6.3.0, `actions/setup-node` v5.0.0, `actions/setup-go`
  v6.5.0, `astral-sh/setup-uv` v7.6.0, `Swatinem/rust-cache` v2.9.2. No other third-party action is used: Rust
  comes from the runner's `rustup`, gitleaks and cargo-audit are installed by version, path filtering is a script.
- Tools by version: uv 0.8.17, ruff 0.15.11, pip-audit 2.10.1, cargo-audit 0.22.2, gitleaks 8.30.1 (checksum
  verified), pytest 9.1.1 (hygiene-static). Language runtimes: Python 3.12 and 3.13 (both, because a 3.13-only
  breakage has been missed before), Go from `go.mod`, Node 22 — and Rust **stable, floating** (not pinned; see above).
  Not pinned either: `registry:2` and `python:3.12-slim` in `delivery-docker-live` (below).
- Concurrency: one run per ref; a newer push to a pull request cancels the older run. Pushes to `main` and
  `integration-*` are never cancelled.
- `permissions: contents: read` for the whole workflow.

## Deliberately NOT in CI yet

- **The Revenue Recovery three-process live run** (ledger-rust + detection-py + orchestrator-go, the run that
  produced `ledger_verify: valid: true` in the root README's review table). No script for it exists in the
  repository — it was driven by hand — so it is not wired. Writing one is the next step; do not add a job that
  claims it until the script exists and exits non-zero on a mismatch like the eleven department `live_run.py`s do.
- **creative-py's `devtools/live_smoke.py`** and **onboarding-py's live server** as separate live-run jobs. The
  smoke script needs an already-running creative-py and ledger and does not start them itself; onboarding-py's
  live ledger-rust tests run inside its pytest suite (and therefore in `python-tests`), so nothing is lost.
- **The deer-flow harness packages in the Python audit.** `deerflow-harness` and `deerflow-extension-api` are
  git-sourced at commit `345f08be` in `uv.lock`; pip-audit has no registry version to look up for them and refuses
  URL requirements, so they are filtered out of the export before the audit. Their transitive dependencies (187
  registry packages) are audited.
- **A pinned base digest for the sandbox image.** `docker/sandbox.Dockerfile` requires `python:3.12-slim`'s digest
  as a build argument. ADR 0011's "Pinned hashes" records one (2026-10-02, fix wave 26a W26-3; also
  `RECORDED_BASE_DIGEST` in the job), but the build uses the `DLV_SANDBOX_BASE_DIGEST` repository variable, which
  only the repository owner can set. Until it is set, `delivery-docker-live` resolves the current digest at run
  time and emits a workflow warning saying whether it equals the recorded one — an unpinned float, stated plainly.
  Likewise `registry:2` (the throwaway local registry) is pulled by tag, and the sandbox image's Go tarball and
  rustup installer are downloaded without a checksum (docs/findings/OPEN.md W26-3b); the Node tarball is checked
  against its pinned sha256 (`NODE_SHA256`, a required build argument — CI #2's docker-live build failed because
  it was not passed; `devtools/test_ci_docker_args.py` in `hygiene-static` now fails any `docker build` in this
  workflow that omits a build argument its Dockerfile declares without a default).
- **cargo fmt.** The ledger crate is not rustfmt-clean today and the README does not require it; adding
  `cargo fmt --check` is a formatting change first, then a CI change.
- **Building or publishing container images** for the services (`docker/Dockerfile`), deployments, and any job
  that needs a secret. Nothing in this workflow uses a secret.
- **Coverage thresholds, benchmarks, mutation testing.**
- **delivery-py's `devtools/audit_gate.py` and `licence_gate.py`** as gates (the licence gate's allowlist and
  the advisory gate's `pip-audit` dependency need their own decision about what a failure means in CI).

## Hygiene (fix wave 25, founder ruling R-HYGIENE)

`devtools/hygiene_check.py` (standard library only). `run` wraps one suite: the suite gets a private TMPDIR
(`$RUNNER_TEMP/hyg-…/tmp`, outside `/tmp`), starts as its own session leader with an inherited environment marker,
and on Linux the wrapper is its child subreaper. The job fails when, for that run:

| Rule | Fails when |
|---|---|
| R1 tracked | `git status --porcelain` differs after the run from before it |
| R2 ignored | a new git-ignored path appears (`git status --porcelain --ignored`), except paths given with `--allow-ignored` (delivery-py's `.venv`, ledger-rust's `target/`) |
| R3 tmp | the private TMPDIR is not empty at the end, or a new entry appeared directly in `/tmp` |
| R4 procs | a process of the suite is still alive after its command exited (it is then listed and killed by PID) |
| R5 skips | a test was skipped (Python), skipped (Go `--- SKIP`), ignored (cargo) or skipped (node) for a reason not on the suite's `expected_skips` list in `devtools/hygiene_allowlist.json` — scout C3-5: before, `-rs` only printed skips |
| R6 counts | the suite's test count differs from its row in `docs/test-counts.md` (regenerate with `--counts write`), less the platform-only tests named for other OSes (below), which must be absent by name |

`lint` (job `hygiene-static`): L1 a test asserting an upper bound on a wall-clock delta against a literal — or
against a name bound only to a literal (`PROMPT = 1.0`, `const PROMPT: Duration = Duration::from_secs(1)`, `const
bound = 2 * time.Second`) — (lower bounds are not flagged: load can only lengthen elapsed time); L2 a test binding or
targeting a hard-coded port; L3 a hand-written test count in README / docs/ci.md / an ADR / a service README /
ci.yml comments, also when it is wrapped across two lines (a count tied to a resolvable commit in the same paragraph
is history and allowed); L4 the shared files (`graceful_close.py` and its pin, the two shared graceful-close test
files, `tests/_procinfo.py`, `tests/test_procinfo.py`, `tests/test_shared_ports.py`) differing between services.
Exceptions need an entry with a reason in `devtools/hygiene_allowlist.json`; `--strict-allowlist` also fails an
entry that matches nothing. `docs/test-counts.md` counts every test of a suite; a test that exists on some OSes
only is NAMED in the allowlist's `platform_only_tests` (test name, `only_on`, reason) and in the generated file's
last column (`counts --check` fails if the two differ). On another OS, R6 expects exactly those tests absent — by
name — and the count lower by that many, and prints that in its summary line; a listed test that runs where it
should not, or is missing where it should run, fails the job. Today: ledger-rust's
`f5_real_sigxfsz_kill_mid_write_leaves_a_torn_tail_that_recovers` (`#[cfg(target_os = "linux")]`), so macOS expects
one test fewer. (Fix wave 26a, W26-5: this replaced `count_os_delta`, an anonymous per-OS number applied without a
word in the output — CI #2's macOS ledger-rust job counted one fewer and R6 rightly passed, but nothing said why,
and a lost test would have been hidden had the platform gate been removed at the same time.)

The test counts are a committed, generated file: after a change that adds or removes tests, run the suite under
`hygiene_check.py run … --counts write` and commit the changed row. Every CI test job checks its row; when two
branches that each changed tests are merged, the merge commit regenerates the rows (CI says which row and the new
number).

What the static rules cannot see (fix wave 26b, H9-R3m / F-9 — stated, not fixed): L1 in Python follows clock readings through this module's imports (`import time as tm`, `from time import monotonic as now`), names, and helpers that `return` a clock delta; it does not follow a delta stored in an attribute or a container, passed to another function, or a bound held in a local name computed from a constant (`bound = TIMEOUT + 2`). L1 for Rust, Go and TypeScript is line patterns over `elapsed`, `time.Since`, `open` and `Date.now()` only — a delta in another name is not seen. L2 finds a port literal, or a name bound to one, at a bind/connect, in a PORT-named place or a literal address; a port computed at run time or passed as an argument is not seen. L3 reads English count phrases (`N tests`, `N passed`, `N unit`, `N integration`, `N/M passed`) and lets a resolvable commit id pin the counts of its own sentence only.

What the dynamic rules cannot see: a file a suite writes OUTSIDE the checkout, its TMPDIR and `/tmp` (e.g. `~/.cache`,
`~/.config/go/telemetry` — the Go job sets `GOTELEMETRY=off`); on macOS (no subreaper there) a process that left
the suite's process group AND either scrubbed its environment or is an Apple platform binary (`/bin/sleep`, `/bin/sh`,
whose environment macOS withholds) — the marker is read from every other process of the user through sysctl
`KERN_PROCARGS2` since fix wave 26b (R26-1; before, ANY orphan that left the group escaped on macOS, delivery-py's
`start_new_session=True` children included). On a shared machine another process can create `/tmp` entries
during a run; R3 prints the names (a CI runner is the job's alone).

The `live-runs` job runs each department's `devtools/live_run.py` under the same wrapper (`--kind none`). Those
scripts `mkdtemp` a work directory (ledger and service logs: the run's evidence). Since fix wave 26b (scout C6-2) a
script removes it when the run ends, passed or failed, unless `LIVE_WORK_DIR=<dir>` names a directory to keep it in:
then the run's directory is made inside `<dir>`, kept, and its path printed at the end ("work dir kept
(LIVE_WORK_DIR): …"). Every `live_run.py` reads `LIVE_WORK_DIR`; finance-py's old name `FIN_LIVE_WORKDIR` (C5-7)
is still honoured with a deprecation notice on stderr, and ignored (with a notice) when `LIVE_WORK_DIR` is set too.
The job keeps the directory in `LIVE_WORK_DIR=$RUNNER_TEMP/live-work`, outside the wrapper's private TMPDIR and
outside `/tmp`, and a following step (`if: always()`) lists what it holds into the log and removes it (fix wave 25,
E-C). R3 is not relaxed: anything else the run leaves in its TMPDIR or `/tmp` still fails the job; a local run
without the variable leaves nothing.

## Known behaviours worth knowing before you debug a red run

- delivery-py's venv: the READMEs and CI both run `uv sync --frozen` (the `dev` dependency group, which holds
  `pytest`, included); `--no-dev` would leave no `pytest` in the venv. (Fix wave 26b, W25-EB-R1: this bullet used to
  say the READMEs had `--no-dev`.)
- delivery-py's `tests/test_live_launcher.py` spawns the service with the interpreter running the suite
  (`sys.executable`, since fix wave 22), so the venv may live anywhere. Its live log goes to
  `$TMPDIR/dlv-live-runs` (fix wave 24; `DLV_LIVE_LOG_DIR` moves it) — never into the source tree; no test rewrites
  a tracked file, and `tests/test_live_tracked_files.py` fails if the live tests change `git status --porcelain`.
  (Fix wave 26b, W25-EB-R1: this bullet used to say it needed `services/delivery-py/.venv` and logged under
  `docs/evidence/dept28/_runs/`.)
- Ports. ledger-rust's integration tests and (since fix wave 25) orchestrator-go's binary tests start the server
  on port 0 and read the bound port back from `LEDGER_PORT_FILE` / `ORCHESTRATOR_PORT_FILE`; the dashboard's live
  tests start `next start -p 0` and use the port the child printed; none picks a port. The Python live tests of
  creative-py, detection-py, fulfillment-py and onboarding-py pick with the shared helper in `tests/_procinfo.py`
  since fix wave 26b (C5-6): one knob, `ZBM_TEST_PORT_RANGE` ("lo-hi"; each suite's older knob —
  `CREATIVE_TEST_PORTS`, `DETECTION_LIVE_TEST_PORTS`, `FULFILLMENT_TEST_PORT_RANGE`, `ONBOARDING_TEST_PORT_RANGE` —
  is read when it is unset), and a child's port is trusted only once that child holds it: its own announced bind
  ("Uvicorn running on …", creative/detection/fulfillment launchers) or the owner check of
  `_procinfo.start_owned` (onboarding-py's launchers, creative-py's lossy-proxy stack), with a retry on another
  port when the child lost the race. A range hands each port out once, then any port of it that is free again.
  delivery-py's live tests still use their own picker (`DLV_TEST_PORT_RANGE`) and are not moved yet
  (docs/findings/OPEN.md).
- onboarding-py's suite is the slow one (~6 minutes plus a cold ledger-rust release build); the
  `test_procinfo.py` copies in creative-py, fulfillment-py and onboarding-py skip one IPv6 case on hosts without
  an IPv6 loopback and say so (an expected skip in `devtools/hygiene_allowlist.json`).
- `-rs` is passed everywhere so any skip prints its reason in the log; an unexpected skip fails the job (R5), and a
  skip in `delivery-docker-live` fails that job.
