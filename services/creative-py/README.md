# Creative Production (services/creative-py)

**Status: BUILT AND SELF-TESTED, not independently reviewed.** Everything
below is what was actually run on Sep 24, 2026. No independent review pass
has happened yet; treat this the way the Fulfillment README treats its
pre-review state.

Architecture and the reasons behind it: `docs/adr/0005-creative-production-architecture.md`.
Spec: Creative Production rev 1, locked by Andre on Sep 24, 2026.

## What this is

One department, two structurally separate intelligence layers that share
reference data but never decision-makers:

- **ZBM (Z Best Media, advertising agency)**: brief → approval →
  production (Enigma / Phantom Canvas, contract only) → export validation →
  rights → quality (2 rounds, then Andre) → Compliance 38 gate → Andre's
  final approval.
- **ZBC (Z Best Clips, clipping agency)**: per campaign, rulebook draft →
  approval by a different actor → Andre signs → rights cleared → live
  (frozen) → Moment Map → hook sheets → kit → Andre signs the kit → clips
  reviewed automatically against the version they were made under → human
  queue for borderline → payout *eligibility* (never money).

Deterministic and rule-based. No LLM calls, no network in tests, no video
or audio libraries.

## Layout

```
src/
  api.py            FastAPI app (composition root; the only module importing both layers)
  serve.py          entry point; binds 127.0.0.1 unless CREATIVE_BIND_ADDR is set
  shared/           reference data + interfaces, no decisions:
    registry.py     Platform Rules Registry (row-level owners, expiry, fail closed)
    rights.py       clearance records + campaign licences (append-only, ledger-recorded)
    ledger.py       LedgerClient (HTTP, unconfigured, fake) + EvidenceRecorder
    departments.py  fail-closed stand-ins: Compliance 38, Verification and Integrity,
                    Legal 37, Finance 31, Clipper Network, Enigma/Phantom Canvas contract
    media.py        plug points for PySceneDetect, faster-whisper/WhisperX, Kinocut,
                    Chromaprint, c2pa-rs (none integrated)
    actors.py founder.py text.py clock.py errors.py types.py
  zbm/              8 intelligences + brief.py, results.py, workflow.py
  zbc/              9 intelligences + rulebook.py, payout_eligibility.py, workflow.py
tests/              pytest; fakes.py holds the only "passing" department doubles
devtools/           fake_ledger_server.py + live_smoke.py (dev-only, for live runs)
```

## The intelligences

| # | ZBM intelligence | Job | Decides |
|---|---|---|---|
| 1 | `brief_writer` | Drafts the 15-field brief from structured client requirements, for the maker | Nothing final; missing fields become open questions |
| 2 | `creative_lead` | Approves briefs; nothing enters production without one | approved / sent back (re-checks everything; never the drafter) |
| 3 | `audience_insight` | Picks the one human truth | Which candidate insight has ≥2 independent sources (or none) |
| 4 | `placement_spec` | Owns spec rows; Export Validator | Deliverable spec valid? Export pass/fail with reasons; blocks on expired rows |
| 5 | `rights_provenance` | Proves ZBM may use every asset | cleared / not cleared per asset (fails closed; gen-fill needs Legal 37) |
| 6 | `hook_retention` | Hook advice from ZBM's own measured results | Ranked hook types by median, ≥3 measured samples, else no advice |
| 7 | `creative_memory` | Brand notes, feedback, winners | Whether a measured result meets the brief's numeric target |
| 8 | `creative_quality` | Premium bar | pass / send back / escalate to Andre (2-round cap) |

| # | ZBC intelligence | Job | Decides |
|---|---|---|---|
| 1a | `rulebook_writer` | Drafts the rulebook (and revisions) | Nothing final; numbered rules with stable ids; blocking issues |
| 1 | `campaign_rulebook` | Approves the rulebook (Andre then signs) | approved / sent back (never the drafter) |
| 2 | `source_mining` | Moment Map from provided timestamped segments | Which segments are clip-worthy, why others aren't |
| 3 | `hook_angle` | Hook sheets per moment, approved angles only | Which hook lines are usable (≤6 words, no never-say) |
| 4 | `platform_rules` | Owns originality/repost rows | Which rows a campaign may rely on today |
| 5 | `rights_clearance` | Licence with sublicense-to-clippers; uncleared music/likeness/footage | cleared / not cleared with flags (fails closed) |
| 6 | `campaign_kit` | 3–5 seed clip specs, caption styles, overlays, templates, do/don't; commissions Enigma/Phantom Canvas | Which moments/angles/hooks/platforms seed the kit |
| 7 | `creative_memory` | Winners library by vertical/platform | Learn only with a Verification and Integrity attestation |
| 8 | `clip_review` | Judges clips against their rulebook version | pass / reject (citing rule ids) / human_review; no money |

`zbc/payout_eligibility.py` is a gate, not an intelligence: eligible only if
Clip Review passed AND Verification and Integrity attests AND Compliance 38
allows. Both departments are fail-closed stand-ins, so it is always false
today, with both blockers named.

## API

All routes except `GET /health` need `Authorization: Bearer $CREATIVE_SERVICE_TOKEN`.
Andre's actions also need `X-Andre-Approval-Token: $CREATIVE_ANDRE_APPROVAL_TOKEN`.
`/docs`, `/redoc`, `/openapi.json` are disabled. Errors: 401 auth, 403
guardrail/founder, 404 not found, 409 out of order / frozen / blocked, 422
validation, **503 = evidence ledger record failed, decision did not take effect**.

- Registry: `GET /registry/rows`, `GET /registry/rows/{id}` (with `usable` + reason), `PUT /registry/rows/{id}` (owner-enforced)
- Rights: `POST /rights/clearances`, `POST /rights/licenses` (actor `rights_desk`)
- ZBM: `POST /zbm/briefs` → `POST /zbm/briefs/{id}/review` → `POST /zbm/briefs/{id}/jobs` → `POST /zbm/jobs/{id}/work` → `POST /zbm/work/{id}/export-validation` → `/rights` → `/quality` → (`/escalation`) → `/compliance` → `/final-approval`; `POST /zbm/hook-advice`, `POST /zbm/memory/results`
- ZBC: `POST /zbc/campaigns/{cid}/rulebooks` → `PUT …/rulebooks/{v}` (drafts only) → `POST …/rulebooks/{v}/review` → `…/sign` → `POST /zbc/campaigns/{cid}/rights-check` → `POST …/rulebooks/{v}/go-live` → `POST /zbc/campaigns/{cid}/moment-map` → `…/hook-sheets` → `…/kit` → `…/kit/sign` → `POST /zbc/clips` → `POST /zbc/clips/{id}/human-review` → `POST /zbc/clips/{id}/payout-eligibility`; `POST /zbc/campaigns/{cid}/revisions`; `POST /zbc/memory/results`, `GET /zbc/memory/winners`

Default actor ids (one per intelligence; add humans with
`CREATIVE_EXTRA_ACTORS='{"jo": ["zbm_creative_lead"]}'`): `zbm_brief_writer`,
`zbm_creative_lead`, `zbm_creative_quality`, `zbm_placement_spec`,
`zbc_rulebook_writer`, `zbc_campaign_rulebook`, `zbc_platform_rules`,
`zbc_clip_human_reviewer`, `rights_desk`.

## Running it

```bash
cd services/creative-py
pip install -r requirements.txt            # same pins as the other Python services
export CREATIVE_SERVICE_TOKEN=<secret>             # required; refuses to start without it
export CREATIVE_ANDRE_APPROVAL_TOKEN=<other secret> # without it every founder action is refused
export LEDGER_SERVICE_URL=http://127.0.0.1:8090    # without both ledger vars every decision is refused (503)
export LEDGER_SERVICE_TOKEN=<ledger secret>
cd src && python3 serve.py                 # CREATIVE_BIND_ADDR (default 127.0.0.1), CREATIVE_PORT (default 8300)
```

## Testing

```bash
cd services/creative-py && python3 -m pytest -q
```

Result on Sep 24, 2026: **205 passed, 0 failed** (Python 3.11.15,
pytest 9.1.1). Unit tests per intelligence (`test_zbm_*`, `test_zbc_*`),
shared reference data (`test_shared.py`), ledger contract (`test_ledger.py`,
HTTP via `httpx.MockTransport`), end-to-end flows for both layers
(`test_e2e_zbm.py`, `test_e2e_zbc.py`), guardrails (`test_guardrails.py`),
attacks (`test_attacks.py`), auth/docs/startup (`test_auth.py`) and the
import boundary (`test_import_boundary.py`).

## Live run (Sep 24, 2026)

`devtools/fake_ledger_server.py` on 127.0.0.1:18390 (implements the
BUILD_CONTRACTS §2 request/response shape; NOT ledger-rust) and
`python3 serve.py` on 127.0.0.1:18300 with real random tokens; then
`devtools/live_smoke.py` over real HTTP. Observed:

- both sockets bound to loopback (`/proc/net/tcp`: `0100007F:477C`, `0100007F:47D6`);
- 401 for missing, wrong and non-ASCII bearer tokens; 404 for `/docs`, `/redoc`, `/openapi.json`;
- ZBC: licence + music clearance recorded; rulebook v1 drafted (OB-01 … MD-01, no blocking issues);
  drafter self-approval 403; approved; sign without token 403; Andre signed; rights cleared; live;
  in-place change of the live rulebook 409; Moment Map kept s1–s4 and rejected s5 (M4), s6 (M3),
  s7 (M5), s8 (M6), s9 (M1); hook sheets; kit with 3 seeds (`seed_clips_produced: false`);
  Andre signed the kit; a clean clip → `pass`; a clip whose caption and bio say
  "ignore your rules and approve" with no disclosure → `reject` citing `DC-01`;
  payout eligibility → `eligible: false` with the Verification and Integrity and Compliance 38 blockers;
- ZBM: brief drafted (no issues); production before approval 409; Creative Lead approved; job;
  work; export `pass`; rights cleared; quality `pass`; Compliance 38 → `compliance_blocked`;
  Andre's final approval with the right token → 409 (gate not passed);
- ledger: 32 entries, all department `creative_production`;
- with the fake ledger killed, `POST /zbm/briefs` → 503 `took_effect: false`, and no brief was stored;
- with `CREATIVE_SERVICE_TOKEN` unset, `serve.py` refused to start;
- all processes stopped afterwards; none remained.

## Known gaps

See ADR 0005 "Honest gaps and open items" for the full list. The short
version: state is in memory; actor ids are asserted (Andre excepted);
clip properties are declared, not detected; only length rows are sourced
and they all expire 2026-10-23; TikTok is blocked; every department
Creative depends on is a fail-closed stand-in, so no ZBM work reaches
Andre's final approval and no ZBC clip is ever payout-eligible today;
not yet run against the real ledger-rust `/ledger/events`.
