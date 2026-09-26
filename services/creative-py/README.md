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

All routes except `GET /health` need `Authorization: Bearer $CREATIVE_SERVICE_TOKEN`
(checked FIRST, before the body gate: an unauthenticated request is 401,
never 413 / 415 / 408 — fix wave 9, L4; any path, unknown ones included).
Andre's actions also need `X-Andre-Approval-Token: $CREATIVE_ANDRE_APPROVAL_TOKEN`.
`/docs`, `/redoc`, `/openapi.json` are disabled. Errors: 401 auth, 403
guardrail/founder, 404 not found, 409 out of order / frozen / blocked, 422
validation (including any id whose ledger subject couldn't fit: campaign
ids are at most 100 characters; client ids are lowercase `[a-z0-9_]`),
413 body over 1 MiB, 422 `PayloadTooManyMembers` (a JSON body with more
keys + items than the route's request model can legally hold, plus 25%:
computed per route from the model, from 64 for an actor-only body to
12,544 for a Moment Map and 20,672 for hook advice — fix wave 7, NEW-1;
the message names the route's number) / 400 `PayloadTooDeep` (nested
deeper than 32),
408 body not delivered within 30 s, 400/431 request
head over 16 KiB, 503 `Service Unavailable` (plain, no `took_effect`) when
more than `CREATIVE_MAX_CONCURRENCY` connections are open (fix wave 5),
**503 `took_effect: false` = evidence ledger record
certainly failed, decision did not take effect** (and no outside
department was called; this includes ledger-rust's own load-shed 503, which
answers without reading the request — fix wave 5); **503 `took_effect: "unknown"`** = the ledger
may or may not hold the record (response lost, timeout, 5xx): nothing
changed here yet, retry the IDENTICAL request and it takes effect exactly
once (fix wave 4); **409 `LedgerConflict`, `took_effect: "unknown"`** = the
ledger holds a different record under this decision's id; **503
`took_effect: "partial"`** = the decision and its outside request were
recorded and sent, but recording the outside party's answer failed, so
that answer is not applied (`effect` says what). Every creating POST
accepts `Idempotency-Key` (1-128 printable ASCII): a retry returns the
original response (`Idempotent-Replayed: true`), the same key with
different content is 409; without it, clips / clearances / licences are
keyed by their own id and other creations by actor + canonical request
(ADR 0005 decision 10).

- Registry: `GET /registry/rows`, `GET /registry/rows/{id}` (with `usable` + reason), `PUT /registry/rows/{id}` (owner-enforced)
- Rights: `POST /rights/clearances`, `POST /rights/licenses` (actor `rights_desk`)
- ZBM: `POST /zbm/briefs` → `POST /zbm/briefs/{id}/review` → `POST /zbm/briefs/{id}/jobs` → `POST /zbm/jobs/{id}/work` → `POST /zbm/work/{id}/export-validation` → `/rights` → `/quality` → (`/escalation`) → `/compliance` → `/final-approval`; `POST /zbm/hook-advice`, `POST /zbm/memory/results`
- ZBC: `POST /zbc/campaigns/{cid}/rulebooks` → `PUT …/rulebooks/{v}` (drafts only) → `POST …/rulebooks/{v}/review` → `…/sign` → `POST /zbc/campaigns/{cid}/rights-check` → `POST …/rulebooks/{v}/go-live` → `POST /zbc/campaigns/{cid}/moment-map` → `…/hook-sheets` → `…/kit` → `…/kit/sign` → `POST /zbc/clips` → `POST /zbc/clips/{id}/human-review` (`…/human-review/withdraw`: the same reviewer clears an uncertain verdict the ledger confirms it doesn't hold, fix wave 5) → `POST /zbc/clips/{id}/payout-eligibility`; `POST /zbc/campaigns/{cid}/revisions`; `POST /zbc/memory/results`, `GET /zbc/memory/winners`
- ZBC rulebooks (fix wave 9, L2): `GET /zbc/campaigns/{cid}/rulebooks?offset=&limit=` is a page of version
  SUMMARIES (≤ 100; `total`, `next_offset`; each with `rule_count`, `retired_rule_count`,
  `rule_number_high_water`, status and dates); `GET …/rulebooks/{v}` one version (its rules,
  `rule_number_high_water`, `retired_rule_count`); `GET …/rulebooks/{v}/retired-rule-ids?offset=&limit=`
  its retired ids, ≤ 1,000 per page (derived from the high-water marks, never stored per version)
- Never-say cap (fix wave 10, N9-3): a goal may list up to 1,000 never-say entries and the draft keeps
  them all (with a warning above 30), but `POST …/rulebooks/{v}/review` answers 422 for a rulebook with
  more than `MAX_NEVER_SAY` = 30 (Clip Review's cost per clip grows with the list; ADR 0005 decision 44)
- Never-say length caps (fix wave 11, N10-4): the review also answers 422 for a phrase over
  `MAX_NEVER_SAY_WORDS` = 3 words or `MAX_NEVER_SAY_PHRASE_CHARS` = 30 characters, or a list over
  `MAX_NEVER_SAY_CHARS` = 300 characters in total, whichever path made the draft (first draft, revision,
  edit); the draft keeps every phrase and warns (ADR 0005 decision 49)

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
export CREATIVE_MAX_CONCURRENCY=256                # optional; connections + in-flight requests, beyond it 503
cd src && python3 serve.py                 # CREATIVE_BIND_ADDR (default 127.0.0.1), CREATIVE_PORT (default 8300)
```

Always launch with `serve.py`, never `uvicorn api:app`: serve.py pins
uvicorn's h11 parser with a 16 KiB request-head cap, a 10 s request-head
deadline (from connect or from the previous response), 5 s idle
keep-alive and `limit_concurrency` (fix wave 5, NEW-3; the same fix as
detection-py). uvicorn's default httptools parser buffered a 200 MB header
and kept idle / partial-head sockets open forever.

## Testing

```bash
cd services/creative-py && python3 -m pytest -q
```

Result on Sep 25, 2026 after fix wave 11: **703 passed, 0 failed, 0 skipped**
(`CREATIVE_TEST_PORTS=18650-18699`; 684 after fix wave 10 (`CREATIVE_TEST_PORTS=18500-18549`); 664 after fix wave 9, three consecutive
runs; Python 3.11.15, pytest 9.1.1; 641 after fix wave 8, 557 after fix wave 7, 515 after fix wave 6, 469 after fix wave 5,
428 after fix wave 4, 360 after fix wave 2, 310 after fix wave 1, 205
before it). No test in this service skips: none uses an env-provided
ledger binary (the real-ledger runs are the live runs below), and
`pytest -rs` reports no skip.
Real-socket tests bind ports from `CREATIVE_TEST_PORTS` ("lo-hi") when it
is set (fix wave 9; defaults: 20110-20119 and 20300-20319, OS-assigned for
the lossy-proxy tests); the fix-wave-9 runs used `CREATIVE_TEST_PORTS=20900-20919`.
`test_fix_wave_11.py` reproduces the AEGIS round-10 findings under the
wave-11 design ruling (fail closed on what the gate cannot read): N10-1
(every distinct round-10 symbol-alphabet MISS, 136 spellings in three
contexts, 0 automatic passes; both hand-picked alphabets, every phrase,
every field, 0 passes; Rule A names "unreadable symbols" and is not
diluted by punctuation, digits, emoji or invisibles, and does not depend
on the currency / math table; the ordinary set — the emoji blocks, the
ruling's explicit emoji list, Latin-1, NFKC-readable symbols — against
the not-ordinary blocks; Rule B dividers pass, a mixed run does not; an
exact reading through the currency / math table is a reject; a 39-caption
ordinary guard, 0 flagged), N10-2 (a regional-indicator phrase between,
inside or beside real flags → human_review; rows of real flags pass),
N10-3 (a regional phrase split over two fields, four layouts → human_review)
and N10-4 (a 60-word phrase, and a list over the total budget, refused with
a 422 on the draft, revision and edit paths; approval at the caps succeeds;
the costliest shapes within the caps, regional reading forced, cold,
routed: budget < 2 s CPU, measured 1.70 s alone / 1.94-1.98 s in-suite; the
test asserts 1.5x the worst measured, 3.0 s scaled by a measured slowdown, so
it is not flaky). Tests changed because they
enshrined the old behaviour: `test_fix_wave_10.py`
`test_n91_round9_currency_cases_go_to_a_human` (the round-9 currency
phrases read exactly through the table: now a reject, was human_review),
`test_n91_b_a_symbol_word_counts_toward_the_failsafe_a_price_does_not`
(the currency word is now Rule A's "unreadable symbols", no longer a
stripped share) and `test_n93_approval_at_the_cap_is_allowed` (its 30
phrases of 17 characters are over the new 300-character budget; now 30 of
10). Follow-up: `test_fix_wave_9.py` M1 cost bounds 1.5 s / 2.0 s → 2.25 s /
2.9 s (1.5x the worst of six in-suite runs; 1.5 s failed at 1.503 s on the
unmodified 978bcaa tree).
`test_fix_wave_10.py` reproduces the AEGIS round-9 findings: N9-1 (the
nine round-9 currency / math-symbol cases → human_review with a never-say
reason; every table symbol is a SKELETON reading of its letter; the
symbol-lettered word in the fail-safe; every phrase of lists A+B+C in the
table's symbols, caption and bio, 0 automatic passes; a 24-caption price /
percentage / math guard, 0 flagged), N9-2 (the five round-9 flag-pair
phrases and 30 glued / pair-spaced / first-letter / zero-width readings →
human_review, never reject; real flag rows pass and are not letter-like),
N9-7 (glued and interleaved Braille never pass; Braille letters are read,
an exact phrase is a reject; a run of stripped characters is judged on
its own; the Braille blank as a line spacer passes), N9-5 (the round-9
false positives pass; the exemptions open no hole: other tag sequences,
stray tags, emoji-letter phrases, hashtag phrases, non-Latin routing),
N9-3 (approval above `MAX_NEVER_SAY` is a 422 with the draft untouched;
approval at the cap succeeds; the round-9 generator at the cap, with and
without a regional indicator, automatic and routed: ≤ 2 s CPU scaled by a
measured slowdown) and N9-4 (two concurrent requests with one
Idempotency-Key, or one submission id, run one review; a failed first
attempt does not strand the waiter). Tests changed because they enshrined
the old behaviour, all in `test_fix_wave_9.py`: the fold test and the
map-coverage test (regional indicators are no longer folded or in
`LETTERLIKE`: they are `REGIONAL_LETTERS`, read only by
`regional_reading`), the AEGIS enclosed-phrases test and the
every-style test (a regional-indicator phrase is human_review with a
never-say reason, not a reject), the styled-text-is-a-signal test ("🇭🇪🇱🇱🇴
🇫🇷🇮🇪🇳🇩🇸" is also a row of flags and now passes; Braille letters added
as a signal) and the fail-safe test (the unmapped style is now 8-dot
Braille: the 26 grade-1 letters are read).
`test_fix_wave_9.py` reproduces the AEGIS round-8 findings: H1 (the
letter-like map against an independent derivation over every code point
of the blocks; the 48 AEGIS enclosed-style cases; every never-say phrase
in all 35 styles in the caption and the bio, 1,960 rejects; a mixed-style
fuzz with zero-width characters, 0 automatic passes; styled text alone is
a signal, a single flag is not; the stripped-share fail-safe on Braille,
block elements, private-use and Tai Xuan Jing text; the 170-caption
round-8 corpus), M2 (classes Q / R / S verbatim, the disclosure-first
variants, the rule's unit cases), M1 (the `ns_cost8.py` generator at 100
phrases, cold, this thread's CPU, ≤ 1.5 s scaled by a measured slowdown
factor; a clip review held on an event while a rulebook draft and a
brief go through; a registry write during the review makes the commit
review again), L2 (60 churn revisions: per-version metadata, flat
per-revision memory, the summarised list GET < 1 MiB and < 200 ms, the
retired ids paged) and L4 (401 before 415 / 413, in-process and on a real
socket); L3 is the rescaled `test_fix_wave_6.py` /health test (max and
p50 bounds). Tests changed because they enshrined the old behaviour:
`test_fix_wave_8.py` (four symbol cases the old boundary let pass, the
30-emoji-caption expectation, the cold-cache helper now also empties the
per-thread memos), `test_guardrails.py` (the retired ids of a revision are
paged, not inlined), `test_auth.py` (an anonymous `/docs` is 401 before it
is 404), `test_fix_wave_6.py` (the scaled /health bounds).
`test_fix_wave_8.py` reproduces the AEGIS round-7 findings: N7-1 (the
shape gate for every JSON content type — ten spellings — and 415 for
nine others and for none, before the body is read; the predicate fuzzed
against FastAPI's own decision; the heap trim), N7-3 (the 100th entry,
the maximal rulebook + 200 full-churn revisions, the `rev_wedge7` probe,
backward-compatible ids, fuzzed goals through the model and the API),
N7-4 (the ten AEGIS emoji cases, class M, the lexicon, symbols alone are
no phrase, decorative emoji, 30 ordinary emoji captions, four corpora),
N7-5 (the 13 class-C pairs in five field arrangements, 300 corpus
pairs), class B (0/30, the stacked rule, the readings, the sound-alike
guard of the two-consonant tolerance, the first-key table fuzzed) and
N7-7 (99 phrases × eight 50 KB transcripts, CPU with every cache
cleared, ≤ 1.5 s; the all-suffix DP and the masked scan against the
reference; the ASCII fast path; field edges at a giant token). Changed
expectations: "make" + "mny" across fields is now `human_review` (N7-5,
`test_fix_wave_7.py`); the bit-parallel scan's hits carry their distance
(`test_fix_wave_6.py`); a wave-5 test sends its slow body as JSON (a body
with no content type is now 415 before it is read).
`test_fix_wave_7.py` reproduces AEGIS round-6 NEW-1 (a legal Moment Map
of 820 and of 2,000 segments accepted; for EVERY route with a JSON body
the maximal legal body is built from the model, counts exactly
`worst_case_json_members`, passes the shape gate, and one member over
the route's cap is refused naming that cap; a model with an unbounded
list or dict cannot be added; the depth cap is unchanged), NEW-2 / NEW-3
(the 35 round-6 auto-passes and the whole of `ns_evade6.py` classes A /
B / C / E: 0 misses; the vowel-drop sweep 0/48, `ns_share6.py` two edits
in one word 0/248, this wave's stacked-class generator 4/600 = 0.7%
without transpositions and 11/600 = 1.8% with, listed; the share rule
never reduces a match; the skeleton, phonetic-key and share unit cases;
the never-say gate's false positives on all three corpora — round-6
`corpus6.py` 0/130 = 0.0%, round-5 3/111 = 2.7%, implementer 5/239 =
2.1% — with a 3% bound each; a 100 KB timing bound of 6 s for the two
new signals, measured 1.3 s; a brute-forced proof of the phonetic run
pass's pruning claim) and NEW-7 (`TEXT_FIELDS` covers every free-text
field of the model; the AEGIS bio cases; every prohibiting rule on every
field, naming it; must-say / keywords / disclosure look where they live;
the bio through the API). Two wave-6 tests were changed to the new
per-route caps (`test_n2_unknown_keys_are_counted_not_enumerated`: 500
unknown keys on a clip, not 3,000, since a clip's cap is 896;
`test_n2_other_error_bodies_are_bounded`: a 4,001-item `moment_ids` is
now refused by the shape gate and a 5,001-item rulebook body is not,
its cap being 10,176).
`test_fix_wave_6.py` reproduces AEGIS round-5 N1 (readable respellings of
never-say phrases: the 21 AEGIS cases with their required outcomes, the
probe's whole 79-candidate list, a 1,000+-case fuzz over the round-5
generators — lookalike + split, stretched, doubled, filler, plain split —
and the round-4 generators, the reject / human_review boundary, the
adjacency policy, a 100 KB timing bound of 8 s (measured 0.75 s worst),
and a randomised proof that the bit-parallel Damerau scan with
restricted starts agrees with a plain DP), N3 (4-letter entries exact-only
unless `fuzzy`; writer and reviewer warnings; the false-positive rate on
BOTH corpora — implementer 4/239 = 1.7%, AEGIS 3/111 = 2.7%, verbatim
copy of the review's `my_captions.py`, with "cure" on the list), N2 (a
1 MiB junk body and a 60,000-key body → 422 under 8 KiB in < 100 ms;
twenty concurrent of each on a real socket → `/health` < 500 ms, no 408
for a legitimate client; other error paths bounded) and N5 (a certainly
unrecorded verdict holds nothing; identical retry still reuses its
time; accurate wording for the uncertain case). Three wave-5 tests were
changed to assert the new policy (the budget tiers; "cure" opted in via
`fuzzy` in the fuzz / exhaustive sweeps, since a 4-letter entry is now
exact-only by design; the timing test uses the batch API that Clip
Review uses) and one wave-2 test (`test_n1_human_review_time_bound_to_verdict`
used to expect a different verdict to be refused for 15 minutes after a
CERTAIN failure). `test_fix_wave_5.py` reproduces AEGIS
round-4 NEW-1 (ASCII lookalike spellings of never-say phrases: the AEGIS
captions, an exhaustive every-single-edit sweep of the 16 phrases (every
substitution, insertion, deletion, adjacent transposition, word split /
join and multi-letter lookalike: 5,000+ variants, none auto-passes), a
seeded 3,000-case fuzz of the same edit kinds combined with lookalikes,
the false-positive rate on `tests/ordinary_captions.py` — 239 ordinary
marketing captions, **6/239 = 2.5%** sent to a human by this rule, none
rejected — and a 100 KB timing test), NEW-3 (real sockets against the
real launcher: a 200 MB header refused with RSS flat, idle / partial /
trickled heads closed within 12 s, keep-alive, bounded concurrency, 431 /
408 in the middleware) and LOW-D (ledger-rust's shed 503 is "not
recorded"; withdraw an uncertain human verdict, including a lost
withdrawal response retried as the identical record). `test_fix_wave_4.py` reproduces AEGIS round-3 / integration
run-3 findings: NS (symbol/digit/unfolded-Latin never-say variants, a
seeded 2,500-case substitution+insertion fuzz, an 11-caption
false-positive guard), LOST (lost ledger response over real HTTP through
`devtools/lossy_proxy.py`, retries 16 min and 40 days later, honest
`took_effect`), IDEM (Idempotency-Key / derived keys on every creating
POST), F8 (tolerance clones, client-id variants) and LIM (413 before
parsing, sync handlers, linear-time scanners and regexes).
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

## Live run — fix wave 4, against the REAL ledger-rust (Sep 24, 2026)

ledger-rust built from this tree into a private target dir, on
127.0.0.1:19940 (fresh log); `devtools/lossy_proxy.py` on :19941 in front
of it (forwards, lets the ledger COMMIT, then drops the response when
armed) and on :19945 in front of creative-py B (:19944); creative-py A on
:19942. Both creative instances used the ledger through the proxy.

- `devtools/live_smoke.py` on A → "LIVE SMOKE: ALL STEPS AS EXPECTED",
  37 entries, `/ledger/verify` valid.
- 98/98 live checks on B: every AEGIS `ns.py` variant (plus "#GetRich",
  "Get r!ch", "ri™sk") → `reject` or `human_review`, never `pass`; an
  ordinary caption with "$20" and "Don't" → `pass`. Ledger response to
  `clip_reviewed` dropped → 503 `took_effect: "unknown"`; identical retry
  → 201, one ledger event, a second retry replays the same body,
  different content → 409. Brief response dropped between client and
  service → retry (no key) returns the same brief, one `brief_drafted`;
  keyed retries replay with `Idempotent-Replayed: true`, same key with
  other content → 409; ledger response dropped on `brief_drafted` → 503
  unknown, retry → 201, one event. After two review rounds escalated:
  clones at 31 s, 29 s, m4v, 18:32 → 409; "Client_F8", "client_f8." →
  422. A 1 MiB + 1 byte body → 413.
- The AEGIS `lost.py` wedge, live: ledger response dropped on a clip,
  identical retry **16.5 minutes later** (past the old 15-minute window)
  → 201 three times with the first attempt's receipt time, exactly one
  `clip_reviewed` on the ledger, `GET` 200. Pre-fix this was 503 "did NOT
  take effect" forever.
- Final `/ledger/verify`: 111 entries, valid. All processes stopped (only
  PIDs this run started).

## Live run — fix wave 5, against the REAL ledger-rust (Sep 24, 2026)

ledger-rust built from this tree into a private target dir; :20100 (fresh
log) and a second instance on :20105 with `LEDGER_MAX_CONNECTIONS=1`;
`devtools/lossy_proxy.py` on :20103 in front of :20100; creative-py on
:20101 (ledger direct), :20104 (through the proxy), :20106 (the 1-slot
ledger).

- `devtools/live_smoke.py` on :20101 → all 39 steps OK, 37 entries,
  `/ledger/verify` valid.
- AEGIS `cr_ns_live.py` captions plus the offline ones (:20104, 5-phrase
  never-say list): "Guaranteed retums", "make rnoney fast", "guaranteecl
  returns", "vvealth secrets" → `reject` (NS rule cited, "with lookalike
  letters"); "make rnioney", "make nnoney", "miracle kure", "rniracle
  kure", "Guaranteed retrns", "get rjch", "Get rlch", "Guaranteed
  return$" → `human_review`; "Get rich" → `reject`; an ordinary caption →
  `pass`. Pre-fix the first three were `pass` (AEGIS round 4).
- AEGIS `bighead.py` (200 MB header) against :20101: refused `400 Invalid
  HTTP request received.` after 2 MB had been sent (connection reset by
  peer); RSS 72,480 → 72,608 KB (pre-fix 107 → 220 MB). A 10 KiB header →
  200; a 20 KiB header that arrives whole → 431 from the middleware.
  `lidle.py`: 10 idle, 10 partial-head and 10 trickling sockets each
  reported open by the server at t = 8 s and closed at t = 10 s, `/health`
  200 throughout. 300 idle sockets held against the default
  `CREATIVE_MAX_CONCURRENCY=256` → `/health` 503 until t ≈ 10 s, then 200
  once the head deadline closed them (see Known gaps).
- LOW-D: with the 1-slot ledger's slot held, `POST /rights/licenses` on
  :20106 → 503 `took_effect: false` ("ledger shed the request at its
  connection limit (HTTP 503, request not read)"); slot freed → 201.
  Through the proxy (:20104): a verdict whose ledger request got an
  injected 503 (not forwarded) → 503 `unknown`; a different verdict →
  409; withdraw at once → 409 ("sent 0 s ago"), without credential → 401,
  with another actor's credential → 403; after 125 s → 200 (the ledger
  held no such event), `clip_human_verdict_withdrawn` recorded, then a
  `reject` verdict → 200. A verdict the ledger COMMITTED (response
  dropped by the proxy) → withdraw 409 "the ledger holds the verdict",
  re-sending it → 200 `pass`, exactly one `clip_human_reviewed`. Final
  `/ledger/verify`: 69 entries, valid. All processes stopped (only PIDs
  this run started).

## Live run — fix wave 6, against the REAL ledger-rust (Sep 24, 2026)

ledger-rust built from this tree into a private target dir, on :20310
(fresh log); creative-py on :20300 (ledger direct). Only PIDs this run
started; both stopped at the end.

- `devtools/live_smoke.py` on :20300 → all 39 steps OK, 37 entries,
  `/ledger/verify` valid.
- AEGIS `cre_amp.py` (as written: N-field bodies, 20 concurrent × 3, RSS
  from the pid file, `/health` polled): 60,000 fields (868 KB) → `{422:
  60}` in 4.4 s, response 101 bytes, peak RSS 102 MB, `/health` p50 9 ms
  max 143 ms; 90,000 fields (1,307 KB) → `{413: 60}`, 73 bytes; 3,000
  fields → `{422: 60}`, 1,134 bytes, `/health` max 78 ms; the AEGIS
  "1 MiB junk" shape (one 1,000,000-character string field) → `{422:
  60}` in 1.6 s, 1,084 bytes, `/health` p50 32 ms max 32 ms. Pre-fix
  (same probe, this tree before the fix, in the test suite's real-socket
  run): 13.0–15.3 MB bodies, `/health` p50 30 s, six 408s and eleven
  read errors among the junk clients.
- Never-say on a fresh campaign with the round-5 ten-phrase list plus
  "cure" (rulebook draft `warnings` and review `review_warnings` named
  the 4-letter entry; review still `approved`): "geeet riiich", "rn ak
  em on ey", "pas si ve inc orne", "make r n oney", "cu re it" →
  `reject` (NS rule cited, "with lookalike letters"); "gget ricch", "ge
  t rl ch", "riskk freeee" → `human_review` (lookalike / small
  misspelling); "make big money", "guaranteed monthly returns" →
  `human_review` ("its words in order with other words between them");
  "a pure delight", "target rich environment" and an ordinary caption →
  `pass`. Final `/ledger/verify`: 67 entries, valid.
- Offline, the AEGIS probes against this tree: `ns_evade5.py` → 0
  automatic passes among its 79 candidates other than the bare word
  "cure", which is not on that probe's phrase list (36 reject, 40
  human_review; pre-fix 16 passes); `ns_evade5b.py` → lookalike+split
  0/84, stretched 0/100, two doublings 0/30 (pre-fix 47/84, 93/100,
  30/30); `ns_fp5.py` with the implementer's phrase list → 3/111 = 2.7%
  with "cure" on the list (pre-fix 13.5%; the three: "made money",
  "from one" ~ free money, "risk. here's" ~ risk free).

## Live run — fix wave 7, against the REAL ledger-rust (Sep 24, 2026)

ledger-rust built from this tree into a private target dir, on :20510
(fresh log); creative-py on :20500 (ledger direct); only PIDs this run
started, both stopped at the end.

- `devtools/live_smoke.py` on :20500 → "LIVE SMOKE: ALL STEPS AS
  EXPECTED" (twice, on the same ledger log), `/ledger/verify` valid.
- **NEW-1**, the AEGIS `live_creative6.py` Moment Map sizes on a live
  rulebook: 100 segments (503 members) → 200; 800 (4,003) → 200; **820
  (4,103) → 200** (was 422 `PayloadTooManyMembers`); 1,000 (5,003) →
  200; **2,000 (10,003 members, 185 KB, the model's maximum) → 200**,
  155 moments + 1,845 rejected segments; 2,001 → 422
  `RequestValidationError` (the model's own limit, not the shape gate).
- **NEW-7**, the AEGIS bio cases: bio "GET RICH with my link. guaranteed
  returns!" → `reject` NS-01 + NS-02 (was `pass`); bio "get rich" →
  `reject`; bio "GET RICH QUICK: link in bio" → `reject` NS-02 + NS-15;
  bio "mk mny with my link" → `human_review` (skeleton); caption "get
  #ad" + bio "rich" → `human_review` "spread over caption and
  account_bio" (was `pass`); on-screen "get" + caption "rich" →
  `human_review` "spread over on_screen_text and caption".
- **NEW-2 / NEW-3**, the AEGIS `ns_evade6.py` classes through
  `POST /zbc/clips` (16-phrase list with "cure"): A transpositions 0/29
  auto-pass (28 human_review, 1 reject), B vowel drops 0/26 (round 6:
  16/26), C homophones 0/36 (round 6: 15/36), E two edits in one word
  0/34 (round 6: 5/34); the vowel-drop sweep 0/48 (round 6: 30/48);
  `ns_share6.py` two-edits-in-one-word 0/248 (round 6: 3/248); a
  60-case sample of this wave's stacked generator: 1/60 without
  transpositions ("gtrkh quick"), 3/60 with ("gtrkh quick",
  "mcemoney", "get yrhc").
- False positives, full pipeline over HTTP, all three corpora:
  `corpus6.py` 130 captions → 126 pass, 4 human_review, all four for
  mixed symbol words ("30-day", "4XL", ".edu", "5-minute"), **never-say
  0/130**; the round-5 corpus 117 → 104 pass, 6 reject (the corpus's
  own exact hits: "Cure your", "Make money moves", "no-risk", "Doctor
  recommended?", "A cure", "Cure for"), 7 human_review of which
  **never-say 3/111 = 2.7%** ("make more", "risk. here's", "made
  money") and 4 mixed symbol words; the implementer corpus 239 → 233
  pass, 6 human_review of which **never-say 5/239 = 2.1%** ("make
  more", "risk. here's", "make videos about money", "no risky", "make
  your money last") and 1 mixed symbol word ("FALL15"). No corpus
  caption was rejected by a never-say rule other than the exact hits.
- Final `/ledger/verify`: 2,180 entries, valid. All processes stopped
  (only PIDs this run started).

## Live run — fix wave 8, against the REAL ledger-rust (Sep 25, 2026)

ledger-rust built from this tree into a private target dir
(`cargo build --release --offline`), on :20710 with a fresh log;
creative-py on :20700 (ledger direct). Only PIDs this run started, all
stopped at the end. The AEGIS round-7 probes re-run (round-7 numbers in
brackets; round 7 ran on another machine, so only the shape of the
numbers compares):

- `devtools/live_smoke.py` → "LIVE SMOKE: ALL STEPS AS EXPECTED" on the
  fresh ledger (37 entries, `/ledger/verify` valid) and again after a
  creative-py restart on the same ledger log after every probe below
  (380 entries, valid).
- **N7-1**, `cre_ct_bypass7.py` (20 senders × 60k-key body, 10 s each):
  `application/json` 263 × 422, `/health` p50 437 ms; **`application/hal+json`
  265 × 422, `/health` p50 423 ms** [47 requests, `/health` p50 3,685
  ms]; `application/vnd.api+json; charset=utf-8` 287 × 422, p50 400 ms;
  `text/plain` 5,063 × 415, p50 15 ms. RSS 112 MB before, 98 MB after
  the floods [590 MB, never released]. Single 60k-key requests
  (`cre_limits8.py`): `application/json`, `application/hal+json`,
  `Application/Problem+JSON; charset=utf-8`, `application/json;charset=UTF-8`,
  `application/+json` → 422 `PayloadTooManyMembers`, 117 B, 25-30 ms
  [hal+json: `RequestValidationError` with 60,001 errors];
  `text/plain`, `text/json`, form-encoded, `application/jsonx`, no type
  → 415 `UnsupportedMediaType`, 129 B, 1-2 ms.
- **N7-3**, `cre_limits8.py` (`cre_limits7.py` with each goal's campaign
  id matching its path): a goal at the maxima (1,000 never-say, 100
  must-say, 20 angles, 50 targets with one listed twice) → 201 in 0.11 s,
  1,109 rules, NS-1000, MS-100; never-say × 99 / 100 / 150 → 201, last
  NS-99 / **NS-100** / NS-150 [× 100 → 500]; must-say × 100 → 201,
  MS-100. `rev_wedge7.py` / `rev_wedge7b.py` (in-process): 19 full-churn
  revisions → v20, NS-400 [wedged at NS-100]. Through the live API
  (`rev_wedge8_live.py`), 25 full-churn revisions of the live rulebook,
  each drafted, reviewed, signed and taken live: 12.9 s, v26 live,
  NS-482..NS-501, 481 retired ids, none reused.
- **N7-7**, `cre_clipflood7.py` (4 senders × 25 s of 50 KB vowel-dropped
  clips): 150 × 201, clip p50 652 ms, p90 708 ms [70, p50 1,449 ms];
  `/health` p50 15 ms; brief POST p50 582 ms [1,140 ms]. `ns_cost7b.py`
  (in-process, one run each): 99 phrases × plain / symbols /
  vowel-dropped 0.47 / 0.62 / 1.11 s [0.92 / 1.03 / 5.86 s]; 16 phrases
  0.71 / 1.01 / 0.78 s [1.16 / 1.18 / 1.77 s].
- **N7-4 / N7-5 / class B**, `ns_evade7.py` (297 cases, full pipeline):
  **20 auto-pass = 6.7% [48 = 16.2%]**: A 0/32 [1], **B 0/30 [9]**, **C
  0/26 [10]**, D-G, J, L 0, **M 1/12 [9]** ("💰 make": the words in the
  other order), K 1/18 ("on rsk") [1], I 4/35 [4], H 14/42 (inflections
  and paraphrases) [14]. `ns_probe7b.py`: every class-C pair →
  `human_review` (caption + bio names both fields: "spread over caption
  and account_bio ('gt #ad ritch')") [caption + bio: all 12 `pass`]; the
  five stacked cases each caught [none]; the ten emoji-for-word cases →
  `human_review` "a symbol standing for one of its words" [all `pass`];
  the phonetic FP candidates 8 pass / 29 flagged [the same].
- False positives (`ns_fp7.py` and the tests; never-say gate, every
  signal): corpus7 0/130, corpus6 0/130, the round-5 corpus 3/111 = 2.7%,
  the implementer corpus 5/239 = 2.1% (all four as before this wave);
  full Clip Review on corpus7 130 × `pass`; 300 random caption + bio
  pairs: 0 spreads; 30 ordinary emoji captions: 2 lexicon hits ("Get 💸
  back on every referral", "Make 💰 moves this quarter").

## Live run — fix wave 9, against the REAL ledger-rust (Sep 25, 2026)

ledger-rust built from this tree into a private target dir
(`cargo build --release --offline`), on :20910 with a fresh log;
creative-py on :20900 (ledger direct). Only PIDs this run started, all
stopped at the end. Round-8 numbers in brackets:

- `devtools/live_smoke.py` → "LIVE SMOKE: ALL STEPS AS EXPECTED" on the
  fresh ledger (37 entries, valid) and again after a creative-py restart
  on the same log after every probe below (445 entries, valid).
- L3 / L4, `cre_ct_bypass` (20 senders × 60k-key body, 10 s each):
  `application/json` 258 × 422, `/health` p50 82 ms [437 ms];
  `application/hal+json` 292 × 422, p50 73 ms [423 ms]; `text/plain`
  5,036 × 415, p50 18 ms. Anonymous `text/plain` and anonymous 2 MB JSON
  → 401 [415 / 413].
- L2, `rev_wedge9_live.py`: 25 full-churn revisions through the live API
  in 13.0 s, v26 live, NS-482..NS-501, 481 retired ids (paged endpoint:
  481), none reused; `GET /zbc/campaigns/{cid}/rulebooks` 12,951 B in 3 ms.
- M1, `cre_clipflood9.py` (4 senders × 25 s of 50 KB vowel-dropped
  clips): 174 × 201, clip p50 518 ms; brief POST p50 230 ms [582 ms];
  `/health` p50 26 ms. `ns_cost8.py` (in-process, one run, 100 phrases):
  0.18 / 0.91 / 1.34 s [1.1 / 4.7 / 3.5 s]; 1,000 phrases 2.0 / 5.0 /
  3.8 s [10.5 / 275.6 / 61.5 s]. (CORRECTED in fix wave 10: AEGIS
  round 9's worst-case generator, `ns_cost8b.py`, measured 144.3 s at
  1,000 phrases on this code; see the fix-wave-10 run below.)
- H1 / M2, `live_aegis9.py` (28 clips): 18 styled-letter, Braille and
  symbol stand-in cases → 4 reject, 14 `human_review`, 0 pass; 10
  ordinary emoji captions → 10 pass. In-process: `ns_scripts8.py` 0
  misses in 15 styles; `ns_q8.py` / `ns_rs8.py` every case non-pass;
  `ns_evade8.py` gap classes 4/176 = 2.3% missed [22.7%], Q / R / S 0
  ["rnk nnny", "eezy nnoney", "noh side effex", "kwit your job" remain];
  `ns_fp8.py` unchanged (170 captions: 1 / 3 / 3; pairs 12/300).

## Live run — fix wave 10 (Sep 25, 2026)

ledger-rust release binary built from this tree for AEGIS round 9 (its
`src/` is identical to this branch's), on :18510 with a fresh log;
creative-py from this branch on :18500 (ledger direct). Only PIDs this run
started, both stopped at the end.

- `devtools/live_smoke.py` → "LIVE SMOKE: ALL STEPS AS EXPECTED" (37
  entries, valid), and again after a creative-py restart on the same log
  (147 entries, valid). No traceback in either service log.
- `live10.py` (scratch probe, over HTTP): a 31-phrase rulebook drafted
  (201, with the cap warning), its approval refused (422, draft
  untouched), edited to 30 phrases (every phrase the cases below use,
  the rest from the round-9 lists), approved, signed, live, kit signed;
  then clips: N9-1 currency cases 9 → 9 human_review; N9-2 flag pairs 5 →
  5 human_review; N9-7 glued / interleaved Braille 5 → 5 human_review,
  exact Braille 2 → 2 reject; N9-5 round-9 false positives 11 → 11 pass;
  real flag rows 2 → 2 pass; prices / percentages / math 24 → 24 pass;
  N9-4 two concurrent POSTs with one Idempotency-Key and a 49 KB
  transcript → 201 / 201, identical bodies, one `Idempotent-Replayed:
  true`, one `clip_reviewed` ledger event; ledger verify valid (110).
- In-process, AEGIS round-9 probes (unmodified copies): `ns_evade9.py`
  MISS 28 → 5 of 463 (the five: "make cash" / "easy cash" flag pairs,
  phrases the probe's own list lacks; "🏠 work from"; two symbol-in-
  another-field spreads — round-9 MISSes outside this wave's findings);
  `ns_fp9.py` corpus-9 emoji 6/40 → 0/40, hashtag 3/20 → 0/20, plain
  0/30, non-Latin 10/20, decor 8/30, corpus-8 3/170 unchanged;
  `ns_cost8b.py` at 1,000 phrases 1.5 / 142.7 / 6.1 s → 1.5 / 8.2 /
  6.2 s (the cliff was a memo eviction, ADR 0005 decision 44).

## Live run — fix wave 11 (Sep 25, 2026)

ledger-rust release binary built for AEGIS round 10 (its `src/` is
identical to this branch's), on :18651 with a fresh log; creative-py from
this branch on :18650 (ledger direct). Only PIDs this run started, both
stopped at the end; no traceback in either log.

- `devtools/live_smoke.py` → "LIVE SMOKE: ALL STEPS AS EXPECTED" (37
  entries, valid).
- `live11.py` (scratch probe, over HTTP): a rulebook with a 60-word
  never-say phrase drafted (201, with the length warning), its approval
  refused (422); edited to 30 phrases / 411 characters, refused (422: over
  the 300-character budget, and its eight added phrases have 4 words);
  edited to 22 phrases / 243 characters,
  approved, signed, live, kit signed; then clips: the first 40 round-10
  symbol MISSes → 40 human_review; a hand-picked symbol alphabet, 20
  phrases → 20 human_review; exact currency / math readings 3 → 3 reject;
  a phrase between real flags 12 → 12 human_review; a regional phrase
  across two fields 12 → 12 human_review; dividers and a 39-caption
  ordinary guard 43 → 43 pass; ledger verify valid (183 entries).

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
mixed-script clips, and any word mixing letters with symbols or digits
such as "mp4" or "Q4", go to the human queue); the pending-attempt and
idempotency stores are bounded in memory (10,000 entries each).
Fix wave 5 / 6 / 7: the never-say gate (visual similarity, consonant
skeleton, phonetic key, adjacency) sends 2.1% (implementer corpus), 2.7%
(AEGIS round-5 corpus) and 0.0% (AEGIS round-6 corpus) of ordinary
captions to a human (e.g. "made money" / "make more" vs "make money",
"make videos about money" under the adjacency policy); phrases of 3
letters or fewer get no edit budget (one edit from "win" is "in", "wine",
"won"), entries of 4 letters none unless the rulebook opts them in
(`{"phrase": "scam", "fuzzy": true}`; the writer and the reviewer warn),
so a misspelt short phrase is caught only if it is a lookalike spelling,
a split, or trips another backstop; a doubled letter ("gget ricch") is a
human's call, never a reject; the phrase's words more than two words apart
("make a lot of money", "get so very incredibly rich") or in another
order ("rich get", "💰 make") are not caught; a transposition stacked on other edits
in a 2-4 letter word ("nu riks") can pass (0.7-1.8% of the wave-7 stacked
generator); a JSON body may carry at most the members its route's model
admits plus 25%; inflections and paraphrases ("getting rich", "100 percent
guaranteed") are not lookalikes and are not caught. Fix wave 8 / 9: an
unknown symbol counts only directly beside a written word of the phrase,
and a phrase must keep a written content word ("🤑 💰" alone is not read);
18 ordinary emoji captions of the wave-8 corpus now go to a human; a never-say word
inside a hashtag ("get #rich") is a human's call, not a reject; the
stacked rule reads at most 32 relaxed hits per phrase, so enough decoy
windows before the real one hide it from that rule (not from the single
signals); retired ids are now derived from per-prefix high-water marks,
but every rulebook version still keeps its full rule list, so 60 churn
revisions of a 1,000-phrase list hold about 89 MiB (ADR 0005 gap 17);
Clip Review now runs off the workflow lock; a rulebook may carry at most
30 never-say phrases (fix wave 10: the Campaign Rulebook refuses more with
a 422; a review then costs at most about 1.56 s on the reference host,
measured, 1.77 s CPU inside the full test run) — the "2-5 s at 1,000 phrases" this README claimed after fix
wave 9 was wrong, 142.7 s measured (ADR 0005 gap 16). Fix wave 10: a
never-say phrase written in regional indicators is only ever a human's
call (a run of them can be flags); a phrase read through the currency /
math-symbol table is a human's call (fix wave 11: an EXACT reading is a
reject); a lone circled
letter without VS16 ("Warranty ⓘ") is still a styled-letter signal, and
a styled word made only of enclosed-letter emoji with VS16 that spells
no never-say phrase passes. Fix wave 11: a word made mostly of symbols the
gate cannot read (outside Latin-1, punctuation and the emoji blocks — a
block approximation of the emoji properties) goes to a human whatever it
spells, so decorations such as "★★★★☆", "♪♫", "✧˖°", "◆◇◆" or "₊˚⊹♡" and
math such as "x→∞" or "A→B→C" do too (a lone or repeated symbol — "→",
"€€€", "★★★★★" — and a repeated divider "━━━━" do not); a never-say list is capped at 3
words / 30 characters a phrase and 300 characters in all (a four-word
phrase such as "make money from home" cannot be approved), and a review at
the caps still costs up to 1.70 s CPU on the reference host, 1.94-1.98 s inside
a long-running process with a large heap (ADR 0005
decision 49). `CREATIVE_MAX_CONCURRENCY` bounds memory, but a client holding
that many idle sockets gets everyone else 503s until the 10 s head
deadline frees them — per-client limits belong in a proxy in front.
