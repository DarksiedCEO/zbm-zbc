# Dependency ownership

Owner ruling, 2026-11-08: only permissive licences (MIT, Apache-2.0, BSD, ISC, Zlib, PSF, Unlicense, CC0; MPL-2.0
allowed and flagged); no AGPL/GPL/LGPL in shipped services; no source-available licences (ELv2, BSL/BUSL, SSPL, ...);
**everything pinned with hashes and owned**; licence notices kept.

"Owned" means: every byte a build installs comes from a store this organisation controls, addressed by a hash
recorded in this repository, so an upstream deletion, re-tag or compromise cannot change or stop a build. This
runbook is the per-ecosystem plan, what exists today, and the follow-ups that need owner approval because they
touch `.github/workflows/**` or a Gate 5 image (the seven Gate 5 images are pinned at `f5fa44b` for the production
sitting and are not rebuilt by this work).

## What exists now (branch `claude/supply-chain-hardening-20261108`)

| Piece | Where | What it does |
|---|---|---|
| Licence gate | `devtools/licence_gate.py`, `devtools/licence_lib.py` | Offline, stdlib only. Reads every lockfile (shared Python lock, every `uv.lock`, `Cargo.lock`, every `package-lock.json`, `go.mod`, the vendored restic binary), checks each package's licence against `devtools/licences/policy.json`, requires a reviewed entry in `devtools/licences/exceptions.json` for MPL-2.0 (approved-flagged) and for anything the ruling does not name (pending-owner-decision), and fails if `langgraph-api`, `langgraph-runtime-inmem`, `langgraph-cli` or `forbiddenfruit` is installable from any Python lock on a real platform (it walks the `uv.lock` graph with marker evaluation; `sys_platform == 'never'` overrides are correctly not installable). Exit 0 clean, 1 violation, 2 only pending owner decisions. Self-test: `python3 -B -m unittest devtools/test_licence_gate.py -v`. |
| Inventory | `devtools/licences/inventory.json` (generated) | Declared licence of every locked package, read from the hash-verified artefact. |
| Notices | `THIRD_PARTY_NOTICES.md`, `third_party_notices/*.md` (generated) | Full licence/notice texts shipped inside each artefact, per component: shared Python runtime, delivery-py, delivery sandbox, ledger-rust, dashboard, Railway tooling, restic (+ its 79 Go modules and the Go runtime). Includes pypdfium2's `BUILD_LICENSES` and onnxruntime's `ThirdPartyNotices.txt`; MPL-2.0 components name their source location. |
| Generator | `devtools/third_party_notices.py` | `collect --cache <dir>` downloads every locked artefact and verifies it against the lock hash (wheel/sdist sha256; npm sha512 integrity; each `.crate` against its Cargo.lock checksum, the licence then read from the crate's own Cargo.toml — the value `cargo metadata` reports; the deer-flow LICENSE against its recorded sha256; restic against `binary-manifest.json`, and each of its Go licence texts at an immutable commit against the sha256 in `devtools/licences/go-licence-pins.json`; `--pin-new` records pins for new modules, to be reviewed); `render --cache <dir>` rewrites the inventory and notices from the cache. |
| Source-of-truth checks | `devtools/licence_gate.py` | The inventory is never trusted alone. Offline, always: npm `package-lock.json`'s own `license` must equal the inventory, and every inventory row must name an artefact hash the lock pins. With `--verify-sources --cache <dir>`: every licence is re-derived from the hash-verified artefact (Python METADATA License-Expression / License / classifiers, crate Cargo.toml, npm tarball, pinned Go texts) and any difference fails. A package with no licence value is an explicit UNKNOWN violation. |
| Dashboard | `apps/dashboard-ts` | `sharp` and its LGPL-3.0 `@img/sharp-libvips-*` binaries removed: `package.json` `optionalDependencies.sharp = file:./stubs/sharp` + `overrides.sharp = $sharp` (an empty, ZBM-owned stub that throws if loaded) and `images.unoptimized: true`. `npm ci` installs no `@img/*` package and no LGPL licence. |
| Toolchain | `services/ledger-rust/rust-toolchain.toml` | Pins Rust 1.99.1 (the Gate 5 ledger image's `rust:1.99.1-slim-bookworm`) for every cargo run in that directory — which is every cargo run CI makes (see F-1) — so CI's floating `stable` no longer applies. Not copied into the image. |
| delivery-py images | `services/delivery-py/docker/Dockerfile`, `sandbox.Dockerfile` | apt pinned to a dated `snapshot.debian.org` archive (`DEBIAN_SNAPSHOT`, default `20261101T000000Z`) with the installed versions recorded in `/usr/share/zbm/apt-packages.txt`; the invalid `--no-group extensions` removed; the sandbox's Python stack installed with `--require-hashes --no-deps --only-binary=:all:` from `docker/sandbox-requirements.lock.txt`. |

### Regenerating

Whenever a lockfile changes:

```sh
cache=$(mktemp -d)   # outside the repository; ~550 MB of artefacts
python3 devtools/third_party_notices.py all --cache "$cache"   # network: PyPI, crates (local registry), npm, GitHub raw
python3 devtools/licence_gate.py                               # offline
```

`collect` needs `go` (for `go version -m` on the restic binary) and `git` (only with `--pin-new`, for
`ls-remote`); crates come from `~/.cargo/registry/cache` or static.crates.io, checksum-verified either way. A new package with no machine-readable licence comes out `UNKNOWN`;
read its licence file in `third_party_notices/<component>.md` and add a `reviewed-metadata` entry.

### Open owner decisions (the gate exits 2 until each is ruled on)

| Package | Component (shipped in) | Licence | Recommendation |
|---|---|---|---|
| caniuse-lite 1.0.30001810 | dashboard (Next.js direct dependency) | CC-BY-4.0 (data) | Approve CC-BY-4.0 for data-only packages; attribution is kept in the notices. The alternative is replacing Next.js. |
| unicode-ident 1.0.26 | ledger-rust **build only** (proc-macro chain; not linked into ledger-server) | (MIT OR Apache-2.0) AND Unicode-3.0 | Approve Unicode-3.0 (attribution-only data licence). |
| pillow 13.0.0 | delivery-py | MIT-CMU | Approve as MIT family. |
| regex 2026.9.10 | delivery-py | Apache-2.0 AND CNRI-Python | Approve CNRI-Python as part of the PSF/Python licence stack. |
| pypdfium2 5.13.0 | delivery-py | BSD-3/Apache-2.0 + bundled PDFium: FTL, Unicode-3.0, IJG, libpng-2.0, libtiff, AGG 2.3, CC-BY-4.0 docs | All permissive, none copyleft; approve, or replace pypdfium2 if the owner wants no FTL/IJG credit clauses. |

When the owner rules: an approved id either goes into `policy.json` `allowed` (with a `family_variants` line) or the
entry's status becomes `approved-flagged`; a rejection means removing the package. The real-repository test in
`devtools/test_licence_gate.py` lists the pending set and must be updated with it.

## CI wiring (for owner review — not done here, `.github/workflows/**` is out of scope tonight)

Add to the `hygiene-static` job (it already has Python 3.13, `go` is not required — without it the restic module
list check is skipped and the binary sha256 check still runs):

```yaml
      - name: Licence gate (owner ruling 2026-11-08)
        shell: bash
        run: |
          set +e
          python3 -B -m unittest devtools/test_licence_gate.py -v 2>&1 | tee "$RUNNER_TEMP/licence-gate-test.log"
          code=${PIPESTATUS[0]}
          python3 devtools/ci_annotate.py --log "$RUNNER_TEMP/licence-gate-test.log" --code "$code" --suite "devtools:licence-gate"
          [ "$code" = 0 ] || exit "$code"
          python3 devtools/licence_gate.py --allow-pending --verify-sources --cache "$RUNNER_TEMP/licence-cache" \
            --json "$RUNNER_TEMP/licence-gate.json"
```

`--verify-sources` needs network (PyPI, npm, static.crates.io, raw.githubusercontent.com) and `go` on PATH for the
restic module list; cache `$RUNNER_TEMP/licence-cache` with actions/cache keyed on the lockfiles to keep it fast.

`--allow-pending` is the interim mode while the five decisions above are open (they still print as `PENDING OWNER
DECISION` lines in the log); drop the flag once they are ruled on so a new pending entry fails the build. The
`changes` job's shared-path list should treat `devtools/licences/**`, every lockfile and `THIRD_PARTY_NOTICES.md` as
inputs of this step.

## Scope limit: operating-system layer — PENDING OWNER DECISION

**The licence gate covers application dependencies only** (the lockfiles listed above and the vendored restic
binary). It does not inspect the Debian base layer of the images, and those layers contain copyleft system
packages. Nothing here was changed — this section only records them:

| Where | OS-layer components under copyleft licences | Notes |
|---|---|---|
| `python:3.12-slim` base: every Gate 5 Python image (seo, legal, finance, compliance, detection, admin/maintenance), client-portal, the runtime stage of ledger, orchestrator and stripe-gateway, and both delivery-py images | glibc (LGPL-2.1-or-later); bash, coreutils, dpkg, apt, tar, grep, sed, findutils, util-linux, gzip (GPL-2.0/GPL-3.0 family) | Unmodified Debian packages, used through their normal interfaces; the application neither links them statically nor modifies them. CPython itself is PSF-2.0. |
| delivery-py service image (apt) | `git` (GPL-2.0-only) | Runtime tool for the deer-flow harness; not a Gate 5 image. |
| delivery-py sandbox image (apt) | `build-essential` (gcc, g++, make, binutils, libc6-dev, dpkg-dev: GPL-3.0 family / LGPL), `pkg-config`, `libssl-dev`, `git`; `curl` is purged before the image is finished | Test-runner image for the delivery engine; not a Gate 5 image. |
| Build stages only (not shipped) | `rust:1.99.1-slim-bookworm`, `golang:1.27.3-bookworm` (gcc, binutils, ...) | Discarded after the binary is copied out. |

Each Debian package's own licence text is inside the image at `/usr/share/doc/<package>/copyright`.

**Carve-out — PENDING OWNER DECISION.** Recommended ruling: unmodified OS/system packages from the distribution, used
as-is, are allowed; the 2026-11-08 permissive-only rule binds the application layer (everything the licence gate
checks), which must stay permissive. If the owner instead wants no GPL/LGPL anywhere in shipped images, the options
are a distroless or static base for the Go and Rust binaries and a review of whether the Python images can drop to a
minimal base — a Gate 5 image change, after the sitting. THIRD_PARTY_NOTICES.md carries the same notice.

## Per-ecosystem ownership plan

### Python (pip / uv)

- **Today:** the shared runtime lock (`deploy/python/requirements.lock.txt`) and the sandbox lock are hash-locked and
  installed `--require-hashes --no-deps --only-binary=:all:`; `delivery-py/uv.lock` carries sha256 for every
  registry artefact; artefacts are fetched from pypi.org at build time.
- **Target:** a hosted, hash-addressed **wheelhouse** (GHCR OCI artifact or an object-store bucket under the
  organisation's account, write access limited to a release workflow). Populate it with
  `pip download --require-hashes --no-deps --only-binary=:all: -r <lock> -d wheelhouse/` (and
  `uv export --frozen --no-dev --format requirements-txt` + the same for delivery-py), verify every file against the
  lock, upload, and record the wheelhouse digest. Builds then install with `--no-index --find-links <wheelhouse>`
  plus `--require-hashes`, so PyPI is never contacted and a missing wheel fails closed.
- **deer-flow git dependency (`deerflow-harness`, `deerflow-extension-api` @ `345f08be00c8a9495079b732a39b46aa9af1584e`):**
  vendoring plan — build both wheels once from that exact commit (`uv build --wheel` in
  `backend/packages/harness` and `backend/packages/extension-api` of a checkout verified at the commit), record each
  wheel's sha256 in ADR 0011, publish them to the wheelhouse, and change `[tool.uv.sources]` to those wheel URLs
  (`{ url = "...whl" }`) so `uv.lock` pins them by hash instead of by git commit. Also put the upstream LICENSE
  (sha256 `b23dff4d4bac8d17b6efaaa7e88c2b730149c4dda45699ce676b719cfdffe7e1`, verified by the generator) into the
  wheel's `licenses/`. Not implemented tonight: it rewrites `uv.lock` (190 packages re-resolved), needs a hosted
  location first, and the git pin is already immutable by commit.

### Rust (cargo)

- **Today:** `Cargo.lock` has a sha256 checksum per crate; `--locked` builds; crates come from index.crates.io.
- **Target:** `cargo vendor --locked --versioned-dirs vendor/` (inside `services/ledger-rust`), commit or publish the
  vendored tree as an OCI artifact by digest, and add source replacement in `services/ledger-rust/.cargo/config.toml`:
  `[source.crates-io] replace-with = "vendored-sources"` / `[source.vendored-sources] directory = "vendor"`. Builds
  run `--offline --locked`. **This changes the ledger image's inputs (the Dockerfile must COPY `vendor/` and
  `.cargo/`), so it is a Gate 5 image change — follow-up F-7.**
- **RUSTSEC-2026-0386 (tokio `AsyncFd` constructors unsound):** ledger-rust locks tokio 1.53.1 (affected range).
  Neither `services/ledger-rust/src` nor any of its 69 locked dependencies' sources reference `AsyncFd` (searched
  2026-11-08), so the unsound constructors are not reachable. Fixed releases: 1.51.6 (1.51.x line) and **1.53.3**
  (on crates.io, not yanked). Upgrading changes `Cargo.lock`, which the Gate 5 ledger image copies — follow-up F-8.

### npm

- **Today:** `package-lock.json` (v3) carries sha512 integrity for every tarball; `npm ci` refuses a mismatch.
- **Target:** an organisation-owned registry mirror (GitHub Packages npm or a Verdaccio instance whose storage is
  backed up), with `.npmrc` `registry=` pointing at it, populated from the lock; or, for the dashboard alone, an
  offline cache: `npm ci --cache .npm-cache --prefer-offline` once, publish the cache tarball by digest, and build
  with `npm ci --offline --cache <restored cache>`. Integrity hashes in the lock keep either mode fail-closed.

### Go

- **Today:** `services/orchestrator-go/go.mod` has **no** requirements (standard library only); the gate fails if a
  `require` appears without review (`max_requires: 0` in `policy.json`).
- **Policy:** the day a dependency is added, commit `go.sum` and run `go mod vendor` (commit `vendor/`, build with
  `-mod=vendor`), with `GOFLAGS=-mod=vendor GOPROXY=off` in the Dockerfile so the build cannot reach a proxy; the
  gate's go.mod check is then extended to the vendored modules' licences.

### Base images

- **Today:** every Dockerfile `FROM` is pinned by digest (see `docs/ci-image-pins.md`), but pulled from Docker Hub /
  GHCR upstream.
- **Target:** mirror each base by digest into `ghcr.io/darksiedceo/mirror/<name>@sha256:<same digest>` with
  `crane copy <upstream>@sha256:<d> ghcr.io/darksiedceo/mirror/<name>:<tag>` (the digest is preserved, so the
  Dockerfile pin is verifiable against upstream), and point `FROM` at the mirror. Bases: `python:3.12-slim`
  (`dddfd7e0…`), `rust:1.99.1-slim-bookworm` (`24707e70…`), `golang:1.27.3-bookworm` (`d9b6a1be…`),
  `ghcr.io/astral-sh/uv:0.8.17` (`e4644cb5…`), and CI's `registry:2`. Changing `FROM` in a Gate 5 Dockerfile is a
  Gate 5 image change — follow-up F-6.

### Debian packages

- **Today (delivery-py images only):** apt reads a dated `snapshot.debian.org` archive (`DEBIAN_SNAPSHOT`), so each
  build resolves the same versions; the Gate 5 images install no apt packages.
- **Target:** after the first CI build, copy `/usr/share/zbm/apt-packages.txt` into the Dockerfiles as explicit
  `name=version` pins (belt and braces), and mirror the needed `.deb`s with their `Packages`-file sha256 into the
  owned store.

### restic (vendored binary in six Gate 5 images)

- **Today:** `deploy/docker/maintenance/vendor/restic` (0.19.1, linux/amd64) is checked at build time against
  `binary-manifest.json` sha256 `20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639` (the decompressed
  binary), and the gate re-checks it. restic is BSD-2-Clause; its 79 linked Go modules are listed with their licence
  texts in `third_party_notices/go-restic.md` (one MPL-2.0: `github.com/hashicorp/golang-lru/v2` v2.0.7, flagged).
- **Upstream verification to record:** restic's release `SHA256SUMS` lists `restic_0.19.1_linux_amd64.bz2` as
  `f415415624dc…585c` (abbreviated as reported by the audit; this box could not fetch the release asset — copy the
  full 64-hex value from `https://github.com/restic/restic/releases/download/v0.19.1/SHA256SUMS`). The procedure:
  download `restic_0.19.1_linux_amd64.bz2`, `SHA256SUMS` and `SHA256SUMS.asc`; verify the signature with restic's
  published release key (`gpg --verify SHA256SUMS.asc SHA256SUMS`); `sha256sum -c --ignore-missing SHA256SUMS`;
  `bunzip2`; the result must hash to the `binary-manifest.json` value above. Record both hashes and the key
  fingerprint in `binary-manifest.json` (a `deploy/docker/maintenance/**` change: Gate 5 images copy that file —
  follow-up F-9).

## Follow-ups that need owner approval

These touch `.github/workflows/**`, a file copied into a Gate 5 image, or behaviour outside tonight's scope.

| # | Item | Where | Change |
|---|---|---|---|
| F-1 | Floating Rust toolchain in CI | `.github/workflows/ci.yml` lines 195, 275, 324, 467, 749 (`rustup toolchain install stable`) | Install `1.99.1` explicitly (with `clippy` at 324) so the step installs what is used. **Already in effect without a workflow change:** every CI cargo invocation runs with `services/ledger-rust` as its working directory — the ledger-rust job (`working-directory`), the live runs' "Build the real ledger-rust server" (:472-474), audit-rust (`defaults.run.working-directory`, so `cargo install cargo-audit` too), and onboarding-py's suite (`tests/conftest.py:216` runs `cargo build` with `cwd=crate_dir`) — so rustup's directory-scoped `rust-toolchain.toml` makes all of them build with 1.99.1. Side effects to expect on the first run: each Rust job downloads 1.99.1 (+clippy) on top of the `stable` it installed (a minute or so; `stable` becomes unused), the rust-cache key changes so caches start cold once, clippy lints are those of 1.99.1 (not the newest stable), and `cargo audit` keeps reporting RUSTSEC-2026-0386 for tokio 1.53.1 (informational/unsound; see F-8). |
| F-2 | `registry:2` pulled by tag | `.github/workflows/ci.yml:575` | Pin `registry:2@sha256:<digest>` (mirror per "Base images"). |
| F-3 | Unhashed `pip install -r services/*/requirements.txt` in CI | `.github/workflows/ci.yml:203, 480, 837` (and 208, 482, 485) | Generate a per-service hash lock (or reuse `deploy/python/requirements.lock.txt` + a test lock for pytest, as `sandbox-requirements.lock.txt` does) and install with `--require-hashes --no-deps --only-binary=:all:`. |
| F-4 | Licence gate in CI | `.github/workflows/ci.yml` `hygiene-static` | The step in "CI wiring" above. |
| F-5 | Sandbox Go 1.23.1 is end-of-life (and older than orchestrator-go's `go 1.27.3`) | `services/delivery-py/docker/sandbox.Dockerfile` `GO_VERSION`, `ci.yml` `GO_VERSION`/`GO_SHA256`, ADR 0011 pin row | Move all three together to `1.27.3`, sha256 `65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40` (from docker-library/golang `versions.json`; re-verify against `https://go.dev/dl/?mode=json` when recording). Not done tonight: `devtools/test_ci_docker_args.py` requires the Dockerfile default, ci.yml and ADR 0011 to agree, and ci.yml is out of scope. |
| F-6 | Base images from upstream registries | every Dockerfile `FROM` (Gate 5 images included) | Mirror by digest into GHCR. |
| F-7 | Crates fetched from crates.io | `services/ledger-rust` (Gate 5 ledger image) | `cargo vendor` + source replacement; the Dockerfile copies `vendor/` and `.cargo/`. |
| F-8 | tokio 1.53.1 in RUSTSEC-2026-0386's range (not reachable: no `AsyncFd` use) | `services/ledger-rust/Cargo.lock` (Gate 5 ledger image) | `cargo update -p tokio --precise 1.53.3`, run the ledger suite, rebuild the ledger image after the sitting. |
| F-9 | restic upstream hash not recorded | `deploy/docker/maintenance/binary-manifest.json` (Gate 5 images) | Record the `.bz2` sha256, the signing-key fingerprint and the verification procedure above. |
| F-10 | Notices not in the images | Gate 5 Dockerfiles | `COPY THIRD_PARTY_NOTICES.md third_party_notices/ /usr/share/doc/zbm/` in each image (the shared-Python, restic and ledger-rust notices at least). |
| F-11 | `base64` + `exec` of a bootstrap script | `deploy/platform_probe/controller.py:24` | The probe's start command ships `admin_probe.py`/`bootstrap.py` as base64 and `exec`s it inside `python -c`. Replace with a file baked into the probe image (or mounted read-only) and a sha256 check before running, so the executed code is reviewable and pinned rather than an opaque blob. |
| F-12 | `pickle.loads` of a state file | `services/finance-py/devtools/live_server.py:72` | Devtools only (not in the finance image), but unpickling `FIN_DEVTOOLS_STATE_FILE` executes arbitrary code if that file is attacker-writable. Replace with JSON (the dev clock and fakes' state), or at minimum require the file to be owned by the current user with mode 0600 and in a private temp directory. |
| F-13 | Sandbox Rust 1.99.0 vs ledger toolchain file 1.99.1 | `sandbox.Dockerfile` `RUST_TOOLCHAIN`, `ci.yml`, ADR 0011 | The offline sandbox has 1.99.0; if the delivery engine ever runs cargo inside `services/ledger-rust`, rustup will ask for 1.99.1 and fail offline. Move the sandbox to 1.99.1 together (same three-place rule as F-5). |
| F-14 | Per-service `requirements.txt` are exact pins without hashes | `services/*-py/requirements.txt` | They are CI/test inputs, not image inputs (the images install the hash lock); fold into F-3. |
