# Repo check: `zbm-zbc` vs `zbestmedia`

Checked Oct 4, 2026 from a cloud session via `git` and the GitHub REST API. Facts below are from those reads unless marked otherwise.

## ⚠️ First: both repos are PUBLIC

| Repo | Visibility (GitHub API) | Created | Last push |
|---|---|---|---|
| `DarksiedCEO/zbm-zbc` | **public** (`"private": false`) | 2026-09-22 | 2026-10-04 |
| `DarksiedCEO/zbestmedia` | **public** (served by anonymous git read) | earlier | 2026-08-09 (`codex/bt-1`) |

Everything in `zbm-zbc` can be read by anyone: the full source of all departments, the security hardening and its known gaps (`docs/findings/OPEN.md`), the AEGIS review reports, the finance and legal rules, and the 57 open counsel questions. Making it private is a one-click, reversible change in GitHub → Settings → General → Danger Zone → Change visibility. **This is the founder's call; nothing was changed.** Note: GitHub Actions minutes for private repos are metered (macOS minutes are billed at a multiple of Linux), so check the plan's included minutes before switching. CI here runs ~55 macOS-heavy jobs per run.

## What `zbm-zbc` is

- Remote: `https://github.com/DarksiedCEO/zbm-zbc` (`git remote -v`).
- The active ZBM/ZBC backend build: 12 services + `apps/dashboard-ts`; Python / Go / Rust / TypeScript.
- Branch heads at check time: `main` 9531fc2 (Sep 22 code, untouched), `integration-2026-09-24` 013caff (wave 26b merged, CI green), `fix26b` bfb042c (two docs-only commits ahead: CI7-1 closure record + timing-flake note).
- Started Sep 21, 2026 as a deliberately **clean start**, not a migration of `zbestmedia` (founder decision, recorded in project notes).

## What `zbestmedia` is

- Remote: `https://github.com/DarksiedCEO/zbestmedia`. Default branch `codex/bt-1`, last commit b0c1b21 (2026-08-09, "Merge pull request #26 … p1a-trusted-workflow-pin").
- Its own README: *"Brand platform services and packages: BrandGraph, Artifact Registry, brand schemas, agent lifecycle, and related tooling."*
- Contents (236 tracked files): `services/artifact-registry`, `services/brandgraph`, `apps/web`, `apps/marketing`, `packages/` (75 files), pnpm/TypeScript monorepo. ~20 `codex/p1a-*` branches from an earlier provenance/CI-trust effort.
- README warning: `apps/marketing` here is an **older, diverged copy**; the live zbestmedia.com site and its lead-intake backend live in a third repo, **`DarksiedCEO/zbestmedia-ui`** (`/marketing` + `/server`). That repo returned 403 to an anonymous API read, so it is private or not visible from this session; not inspected.

## How they relate

- **No code relationship.** `zbm-zbc` does not import, vendor, or reference `zbestmedia` (per the Sep 21 clean-start decision; not re-verified by grep beyond the README).
- `zbestmedia` = the Aug 2026 V2 foundation attempt (TypeScript brand-platform services). `zbm-zbc` = the current department backend.
- Project memory also records a local-only branch `codex/zbc-phase2-slices-b-e-preserved` (c4c2018) on the founder's machine that was never pushed. Not visible from here.

## Implication for the frontend handoff

- Backend = **`DarksiedCEO/zbm-zbc`**, branch `integration-2026-09-24`. Not `zbestmedia`.
- The UI being built is **`DarksiedCEO/zbest-sites`** (private; two Vercel marketing sites, `apps/zbm` + `apps/zbc`). See `FRONTEND_ALIGNMENT.md`.
- Any project doc that names `zbestmedia` as the canonical backend (e.g. `CURRENT_STATE_AND_AUTHORITY.md` mentioned by the frontend project) is stale on that point.
- `zbestmedia-ui` is the **current live** zbestmedia.com (Vite/React SPA). `zbest-sites` is its **replacement** (ZBM + ZBC), cut over later by canary migration; `zbestmedia-ui` stays untouched until then (`zbest-sites/PRODUCT.md:69-70`).
