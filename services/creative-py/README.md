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
validation (including any id whose ledger subject couldn't fit: campaign
ids are at most 100 characters), **503 `took_effect: false` = evidence
ledger record failed, decision did not take effect** (and no outside
department was called); **503 `took_effect: "partial"`** = the decision
and its outside request were recorded and sent, but recording the outside
party's answer failed, so that answer is not applied (`effect` says what).

- Registry: `GET /registry/rows`, `GET /registry/rows/{id}` (with `usable` + reason), `PUT /registry/rows/{id}` (owner-enforced)
- Rights: `POST /rights/clearances`, `POST /rights/licenses` (actor `rights_desk`)
- ZBM: `POST /zbm/briefs` → `POST /zbm/briefs/{id}/review` → `POST /zbm/briefs/{id}/jobs` → `POST /zbm/jobs/{id}/work` → `POST /zbm/work/{id}/export-validation` → `/rights` → `/quality` → (`/escalation`) → `/compliance` → `/final-approval`; `POST /zbm/hook-advice`, `POST /zbm/memory/results`
- ZBC: `POST /zbc/campaigns/{cid}/rulebooks` → `PUT …/rulebooks/{v}` (drafts only) → `POST …/rulebooks/{v}/review` → `…/sign` → `POST /zbc/campaigns/{cid}/rights-check` → `POST …/rulebooks/{v}/go-live` → `POST /zbc/campaigns/{cid}/moment-map` → `…/hook-sheets` → `…/kit` → `…/kit/sign` → `POST /zbc/clips` → `POST /zbc/clips/{id}/human-review` → `POST /zbc/clips/{id}/payout-eligibility`; `POST /zbc/campaigns/{cid}/revisions`; `POST /zbc/memory/results`, `GET /zbc/memory/winners`

Default actor ids (one per intelligence; add humans with
`CREATIVE_EXTRA_ACTORS='{"jo": ["zbm_creative_lead"]}'`): `zbm_brief_writer`,
`zbm_creative_lead`, `zbm_creative_quality`, `zbm_placement_spec`,
`zbc_rulebook_writer`, `zbc_campaign_rulebook`, `zbc_platform_rules`,
`zbc_clip_human_reviewer`, `rights_desk`. Every actor action needs that
actor's own credential in `X-Creative-Actor-Token` (configured in
`CREATIVE_ACTOR_TOKENS`); a body `actor_id` is optional and must match it.

## Running it

```bash
cd services/creative-py
pip install -r requirements.txt            # same pins as the other Python services
export CREATIVE_SERVICE_TOKEN=<secret>             # required; refuses to start without it
export CREATIVE_ANDRE_APPROVAL_TOKEN=<other secret> # without it every founder action is refused
export CREATIVE_ACTOR_TOKENS='{"zbm_creative_lead": "<>=16 chars>", ...}'  # per-actor credentials (ADR 0005
                                                   # decision 18); unset = every actor action refused (403);
                                                   # callers send X-Creative-Actor-Token
export LEDGER_SERVICE_URL=http://127.0.0.1:8090    # without both ledger vars every decision is refused (503)
export LEDGER_SERVICE_TOKEN=<ledger secret>
export CREATIVE_SUPERSEDED_GRACE_HOURS=72          # optional; 0..720, else refuses to start (ADR 0005 decision 10)
cd src && python3 serve.py                 # CREATIVE_BIND_ADDR (default 127.0.0.1), CREATIVE_PORT (default 8300)
```

## Testing

```bash
cd services/creative-py && python3 -m pytest -q
```

Result on Sep 24, 2026 after fix wave 2: **360 passed, 0 failed**
(Python 3.11.15, pytest 9.1.1; 310 after fix wave 1, 205 before it).
`test_fix_wave_2.py` reproduces AEGIS round-2 N1 (receipt time bound to
content, 15-minute window, content-hashed clip event ids), F13 (backdating
route closed), N4 (per-actor credentials; review cap per client
deliverable) and N3 (every AEGIS never-say variant, the complete
Default_Ignorable table, and a seeded 1,500-case fuzz of ignorables and
lookalikes inserted into never-say phrases — never an automatic pass). `test_fix_wave_1.py`
holds one reproduction per fix-wave finding (F8 review cap, F9 record-first
per outside call site, F11 deterministic ids, F12 text evasion, F13 grace
window, F14 Decimal guard, F16 ledger-rust validation parity, integration
defects 2 and 3). Unit tests per intelligence (`test_zbm_*`, `test_zbc_*`),
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

## Live run — fix wave 1, against the REAL ledger-rust (Sep 24, 2026)

ledger-rust built from this tree (`cargo build --release`, own target
dir) on 127.0.0.1:19240 with a fresh log and two finding entries
appended first; the ORIGINAL service (exported from the pre-fix commit)
and the fixed one side by side, each as `serve.py` and as a harness that
wraps every department port in a call-counting spy. Observed:

- **Integration defect 3:** original `live_smoke.py` → `KeyError: 'department'`
  on the finding entries; fixed `live_smoke.py` → every step as expected,
  `71 entries (2 finding(s), 69 event(s))`, `/ledger/verify` → `valid: true`.
- **F8:** original — after round 2 escalated to Andre, a new job on the same
  brief → 201 and work in it → 201 as *round 1*; fixed → 409 "brief … has
  work escalated to Andre … until Andre resolves it".
- **F12:** transcript with "Guarant<Cyrillic е>ed returns": original `pass`,
  fixed `reject` NS-01; "g u a r a n t e e d  r e t u r n s": original
  `pass`, fixed `human_review` (possible never-say + obfuscation signal).
- **Integration defect 2:** 126-character campaign id with a healthy ledger:
  original 503 "decision did NOT take effect: the evidence ledger record
  failed"; fixed 422 (path pattern `{1,100}`); ledger `/health` 200 throughout.
- **F9:** real ledger stopped: original open-job → 503 but
  `creative_agents.commission` called twice, go-live → 503 but
  `clipper_network.announce_rulebook_version` called; fixed → both 503
  with **zero** outside calls, rulebook still `signed`. Ledger restarted on
  the same log (chain verified at load, 149 entries): fixed open-job → 201
  `job-0006` (no id consumed by the failed attempt), `production_opened`
  (seq 152) before `crossing_creative_agents` (153); go-live `rulebook_live`
  (154) before `crossing_clipper_network` (155). Final `/ledger/verify`:
  156 entries, valid.
- all processes stopped afterwards (only PIDs this run started).

## Live run — fix wave 2, against the REAL ledger-rust (Sep 24, 2026)

ledger-rust built from this tree into a private target dir
(`cargo build --release`), on 127.0.0.1:19601 with a fresh log; the
pre-fix service (`git archive ae75124`) on :19602 and the fixed one on
:19600, same probe script over HTTP (AEGIS cr2/cr4/cr5 as live calls):

- **N1:** ledger-rust stopped (its own PID), `clip_r` with junk → 503;
  ledger restarted on the same log. Pre-fix: different content under
  `clip_r` → 201, recorded as a new decision. Fixed → 409 "submission id
  clip_r was already used … with different content"; an identical retry of
  another outage attempt inside 15 min → 201 with the first attempt's
  receipt time, one ledger event. (The 30-day half of the scenario needs a
  controlled clock: covered by `test_n1_f13_aegis_backdating_route_is_closed`.)
- **N3** (never-say "get rich"): pre-fix, the Hangul-filler, small-capital
  G, halfwidth-filler, RLO, stroked-letter and Cherokee variants all →
  `pass`. Fixed → `reject` NS-02 (filler, ɢ, ǥ/ħ, Cherokee Ꮐ) or
  `human_review` (split by a filler; RLO).
- **N4:** pre-fix, a body-asserted `zbm_creative_lead` with no credential
  approved the brief (200) and a cloned brief while an escalation was open
  → 201. Fixed: no credential → 401, a forged credential → 401, the
  drafter's credential claiming the Creative Lead → 403; the clone → 409
  (same spec fingerprint), a renamed clone with aspect "18:32" → 409;
  after Andre killed the escalation a new brief → 201.
- `devtools/live_smoke.py` (now sending per-actor credentials, with three
  new refusal steps) → "LIVE SMOKE: ALL STEPS AS EXPECTED", 37 entries,
  `/ledger/verify` valid. All processes stopped (only PIDs this run started).

## Known gaps

See ADR 0005 "Honest gaps and open items" for the full list. The short
version: state is in memory; actor credentials are static bearer tokens
(no rotation/expiry);
clip properties are declared, not detected; only length rows are sourced
and they all expire 2026-10-23; TikTok is blocked; every department
Creative depends on is a fail-closed stand-in, so no ZBM work reaches
Andre's final approval and no ZBC clip is ever payout-eligible today;
a lost outside answer is reported as `took_effect: "partial"` and not
re-asked automatically; obfuscation handling is conservative (some honest
mixed-script clips go to the human queue).
