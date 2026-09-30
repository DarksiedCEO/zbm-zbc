# ZBM Fulfillment Department (services/fulfillment-py)

**Status: CONDITIONAL, not CERTIFIED.** One independent review pass has
now happened (Sep 22, 2026) and found real, CONFIRMED defects — ten of
them, all reproduced independently before being fixed, listed below.
This is stronger evidence than the self-built version had, but it is
still one review, from one reviewer, on one day. Treat every claim below
as what was actually run, not as a guarantee.

**Sep 24 2026:** a second, hostile audit found 12 more defects plus one
test-coverage gap (two High: calling-hours checked on a UTC clock, and a
caller-supplied clock that could bypass it). Each defect was reproduced
against the unmodified code before being fixed; 7 known limitations
remain open. See "Audit, Sep 24 2026" below. Still
CONDITIONAL.

**Sep 24 2026, fix wave 1 (F3, High):** an independent AEGIS review showed
the audit's calling-hours fix could still be bypassed — the caller chose
the recipient's time zone and the redial key. Fixed; see "Fix wave 1,
Sep 24 2026 — F3" below.

See `docs/adr/0002-fulfillment-department-architecture.md` for why this
department exists, what it competes against, and the architecture
decisions behind it.

## What this actually is

Missed-call detection → follow-up task creation → callback orchestration
→ escalation sequencing → customer dossier → appointment-completion
tracking → resolution/write-back, as six single-purpose agents behind
one authenticated FastAPI service. Mirrors the Revenue Recovery 1A build
discipline: real pydantic schema, real agent logic, real fixtures, real
tests — a single Python service this pass, not the 4-language stack
(see ADR 0002 Decision 2 for why).

## Independent review, Sep 22 2026 — findings and fixes

An independent reviewer (not this build) read every source and test
file, ran the suite, and reproduced ten defects against the live code.
Every one was independently re-reproduced here before being fixed — not
taken on the reviewer's word — and each fix has a regression test named
for what it proves. **64/64 `pytest` passing** after fixes (up from the
pre-review 52).

| # | Finding | Fixed? | How |
|---|---|---|---|
| 1 | **CRITICAL** — the live API defaulted to `InMemorySipDialer`/`InMemorySystemOfRecord` (test doubles), so callers got `dial_placed: true` / `write_back_status: "success"` for a call never placed and a write that never happened. | **Fixed** | `api.py` now defaults to the honest `NotWiredSipDialer`/`NotConfiguredSystemOfRecord`. The test doubles still exist but require an explicit opt-in (`FULFILLMENT_SIP_DIALER=in_memory`, `FULFILLMENT_SYSTEM_OF_RECORD=in_memory`) for local demos only. |
| 2 | `due_at` was accepted on the schema but never checked in `callback_orchestration.py` — a task due days out was dialed immediately, and the same still-PENDING task was dialed twice on repeat calls. | **Partially fixed, semantics corrected** | The reviewer's first-pass suggestion (block while `now < due_at`) was itself wrong and broke real on-time callbacks — `due_at` from `missed_call_detection` is a "call back by" deadline, not an earliest-dial time. Fixed instead: `due_at` breach is now surfaced (`sla_breached`), and a successful/failed dial now returns the task with status advanced to `SENT`/`FAILED` so a persisting caller won't redial. **Still open**: this agent has no datastore, so nothing *enforces* that a caller actually persists that status — see gap 6. |
| 3 | The ADR's "nothing terminal exits without a record" language overclaims relative to what's wired — no pipeline actually connects detect → dial → escalate → resolve automatically. | **Documentation corrected, not a code fix** | This was true when written and remains true: there is no orchestrator this pass (ADR 0002 Decision 2, gap 3 below). The guarantee applies to `resolution_writeback` itself (every terminal event handed to it gets a record), not to automatic connection between agents. |
| 4 | A failed `human_handoff` (the last escalation attempt) returned `None` from `followup_sequencing.escalate()` with no record, no incident, nothing. | **Fixed** | Added `is_sequence_exhausted()`; the `/agents/followup-sequencing/escalate` endpoint now automatically produces a `NO_RESOLUTION` `ResolutionRecord` (with an attempted write-back) when the sequence is genuinely exhausted, instead of silently returning `null`. |
| 5 | `missed_call_detection` filtered on call `status` only, never `direction` — an OUTBOUND call this business placed and failed to connect generated a backwards, duplicate "callback" task. | **Fixed** | Only `INBOUND` calls are now candidates; outbound misses also no longer count toward the repeat-miss urgency window. |
| 6 | Business-hours check uses `now.hour` in UTC with no per-line timezone — wrong behavior for any non-UTC client. | **Not fixed — real gap, unchanged** | Confirmed real by the reviewer; fixing it requires a schema change (per-line timezone data) this pass didn't make. See gap 5 below. |
| 7 | Unmatched callers lose shared history — no pending-identity record or later merge path. | **Not fixed — real gap, unchanged, description sharpened** | Confirmed real; caller-identity resolution remains out of scope this pass. See gap 4 below. |
| 8 | `LabeledValue.amount_usd` was a `float`. | **Fixed** | Changed to `Decimal`. This field is not currently populated by any agent (a stub for a future lifetime-value estimate), so the practical risk was theoretical, but the type is now correct from the start rather than retrofitted later. |
| 9 | `hmac.compare_digest` raised an unhandled `TypeError` on a non-ASCII bearer token, producing an unauthenticated HTTP 500 instead of 401. | **Fixed** | `require_auth` now catches the `TypeError` and treats it as an invalid token (401). **This same bug pattern almost certainly still exists in `services/detection-py/src/api.py`** — it was flagged as a known-but-unfixed gap in last night's Revenue Recovery review and has not been revisited; fixing it there is a five-minute follow-up, not done as part of this pass. |
| 10 | `resolution_id` was built from a per-batch index (`f"res-{type}-{id}-{i}"`) — two separate API calls resolving the same entity produced identical IDs. | **Fixed** | Now uses a `uuid4` suffix, unique across batches and callers. |
| — | `/docs`, `/redoc`, `/openapi.json` were reachable with no auth. | **Fixed** | Disabled outright (`docs_url=None`, etc.) rather than gated — no interactive-docs need for a private service-to-service API. **This same gap likely exists in `detection-py`** — flagged in last night's second RR review, not yet revisited. |
| — | `appointment_tracking`'s docstring claimed "a second pass" tracks state across scans; the code doesn't — it's purely elapsed-time-based. | **Fixed (comment accuracy)** | Docstring corrected to match actual behavior; also now explicitly notes this agent re-emits the same `task_id` every scan on a still-overdue appointment since there's no pass-tracking to dedupe against.

## Audit, Sep 24 2026 — findings and fixes

A second, hostile, evidence-first pass (different session from the Sep 22
review), held to the monorepo house rules in the Sep 24 build contracts
(fail-closed auth, docs disabled, loopback bind with env override,
Decimal money as a two-decimal string, no network in tests). Method for
each finding: reproduce against the unmodified code, write a failing
test, fix, re-run. Pre-fix reproductions were run as a standalone script
against the untouched tree; the outputs are quoted in the Evidence column.
Tests are in `tests/test_audit_2026_09_24.py` (in-process) and
`tests/test_live_server.py` (real process, real socket).

**Suite: 64 passed before → 116 passed after** (`python3 -m pytest -q`
in this directory). 52 new tests; 11 pre-existing tests were updated
because `orchestrate()` now requires the recipient's time zone and the API
no longer accepts a caller-supplied `now` (see A1/A2). No pre-existing
assertion was weakened; the only assertion text changed is
`"business hours"` → `"contact window"`.

| # | Finding | Severity | Fixed? | Evidence (pre-fix) → test |
|---|---|---|---|---|
| A1 | **Quiet hours were checked on a UTC clock**, not the recipient's local time: `8 <= now.hour < 20` in UTC. A missed call from Los Angeles got called back at 02:00 local. No time-zone input existed at all. | **High** (TCPA-style calling-hours exposure the moment a real dialer is wired) | **Fixed** | `orchestrate(..., now=09:00Z)` → `attempted=True, calls_placed=1`. Now a hard rule in `src/contact_window.py`: default **08:00–21:00 in the recipient's IANA time zone** (`timezone_by_call_id`), start-inclusive/end-exclusive, DST via zoneinfo; **unknown/invalid time zone → not dialed**. `FULFILLMENT_CONTACT_WINDOW=HH:MM-HH:MM` can narrow it, never widen it; a bad value refuses startup. Tests: `test_callback_at_2am_recipient_local_is_blocked_even_though_utc_is_daytime`, `test_callback_at_1pm_recipient_local_is_allowed_even_though_utc_is_evening`, `test_unknown_or_invalid_recipient_timezone_fails_closed` (×4), `test_contact_window_edges_are_start_inclusive_end_exclusive_in_local_time`, `test_contact_window_follows_dst`, `test_contact_window_config_can_narrow_but_never_widen_past_8_to_21`, `test_contact_window_rejects_naive_now`, `test_malformed_contact_window_refuses_to_start`. |
| A2 | **The caller could supply the clock.** `OrchestrateRequest.now` ("override for deterministic testing") let any authenticated caller evaluate the calling-hours check against a time of its choosing. | **High** | **Fixed** | `POST /agents/callback-orchestration/run` with `"now"` → 200. Field removed; all request models are `extra="forbid"` so a stale client sending `now` gets 422, not a silent ignore. The server clock is `api._now()`. Tests: `test_api_refuses_caller_supplied_now_which_could_bypass_quiet_hours`, `test_api_quiet_hours_use_the_server_clock`. |
| A3 | **Duplicate call events / tasks were dialed twice.** The same `CallEvent` twice in a batch produced two `fu-{call_id}` tasks; the same task twice in one batch was dialed twice; the same PENDING task in two API requests was dialed twice (README gap 6); concurrent requests could all pass any check-then-dial. | **Medium** | **Fixed within process lifetime** | Pre-fix: `['fu-c', 'fu-c']`; one batch → 2 dials; two requests → 2 dials. Now: `detect()` dedupes by `call_id`; `orchestrate()` dedupes `task_id` per batch; the API remembers task_ids it handed to the dialer and holds a lock across check-and-dial. **Not persisted** — lost on restart (still no datastore). Tests: `test_duplicate_call_event_in_one_batch_produces_one_task`, `test_duplicate_task_id_in_one_batch_is_dialed_once`, `test_api_does_not_redial_the_same_task_across_requests`, `test_api_concurrent_requests_for_the_same_task_dial_once` (verified to fail with 4 dials when the lock is replaced by a no-op). |
| A4 | **A dialer exception mid-batch lost earlier dials.** Only `NotImplementedError` was caught; any other exception (a real dialer's network error) aborted the batch with a 500 after earlier tasks had already been dialed, so the caller never learned about them and a retry would redial. | **Medium** (latent: no real dialer exists yet) | **Fixed** | Flaky dialer raising on task 2 → HTTP 500, task 1 already dialed. Now the exception becomes an outcome: `attempted=True`, task `FAILED` (outcome unknown — never left PENDING for an automatic redial), only the exception type is reported (a dialer message could contain the number). Test: `test_dialer_exception_mid_batch_keeps_earlier_outcomes_and_marks_unknown_as_failed`. |
| A5 | **Lost updates in the in-memory dossier store.** `global _dossiers` read-modify-write in a sync handler (thread pool), no lock. | **Medium** | **Fixed** | 4 concurrent updates for 4 customers → store kept `['cust_1']`. Now under a lock. Test: `test_concurrent_dossier_updates_do_not_lose_writes` (verified to fail when the lock is a no-op). |
| A6 | **Unhandled 500s on bad input.** Naive datetimes accepted, then crashed aware-vs-naive comparisons; unknown `resolution_type` crashed `ResolutionType(...)` in the handler; escalating a non-FAILED task raised `ValueError` → 500. | **Medium** | **Fixed** | Pre-fix: naive `scheduled_at` → 500; `resolution_type:"nope"` → 500; escalate PENDING → 500. Now: every datetime is `AwareDatetime` (422); `resolution_type` is the enum and `entity_type` a `Literal` (422); escalate of a non-FAILED task → 409. Tests: `test_naive_datetime_is_422_not_500`, `test_mixed_naive_and_aware_call_times_is_422_not_500`, `test_unknown_resolution_type_is_422_not_500`, `test_unknown_entity_type_is_422`, `test_escalating_a_non_failed_task_is_409_not_500`. |
| A7 | **No input bounds or formats.** Phone numbers were free text (`"call me"` accepted and, via `phone_by_call_id`, handed to the dialer); every id/string and every list unbounded. | **Medium** | **Fixed** | `phone_number:"call me"` → 200. Now: phones E.164 (`^\+[1-9][0-9]{1,14}$`) on `CallEvent` and on `phone_by_call_id` values; ids 1–128 chars `[A-Za-z0-9._:-]` (task ids 256, since they are derived and grow per escalation); transcript ≤10 000; reason ≤2 000; batches ≤1 000. Tests: `test_phone_number_must_be_e164` (×5), `test_orchestrate_phone_map_values_must_be_e164`, `test_oversized_id_and_oversized_batch_are_rejected`. |
| A8 | **422 bodies echoed caller PII.** FastAPI's default validation error includes each error's `input`; for a missing field that is the whole submitted object, so a malformed call event echoed the phone number and voicemail transcript back. | **Medium** | **Fixed** | Pre-fix 422 body contained `+15550199` and `Jordan`. Custom handler returns only `loc`/`type`/`msg`. Test: `test_validation_errors_do_not_echo_caller_pii`. |
| A9 | **Full phone number copied into free-text `FollowUpTask.reason`** (`"missed inbound call from +15550199 …"`), which is the field that ends up in logs, CRM notes and dashboards. | **Low** | **Fixed** | Now `***0199`; the full number is still reachable structurally via `source_call_id`. Test: `test_task_reason_does_not_carry_the_full_phone_number`. |
| A10 | **Money not to contract section 1.** `LabeledValue.amount_usd` rounded ROUND_HALF_EVEN (`"0.125"`→`"0.12"`, `"1.005"`→`"1.00"`) and checked `gt=0` **before** rounding, so `"0.004"` became a positive-only `"0.00"`. | **Low** (field is populated by no agent and no route accepts it) | **Fixed** | Now ROUND_HALF_UP to 0.01, float only via `str()`, positive check after rounding, NaN/Inf/bool/out-of-range rejected; serializes as `"12.30"`. Tests: `test_money_is_half_up_two_decimal_string` (×6), `test_positive_money_rejects_zero_after_rounding_and_non_finite` (×6). |
| A11 | **Retrying an exhausted escalation minted a second NO_RESOLUTION record and a second write-back.** | **Low** | **Fixed within process lifetime** | Two identical requests → two different `resolution_id`s. Now the first record is returned. Test: `test_exhausted_escalation_retry_returns_the_same_resolution`. |
| A12 | **The service did not own its bind address.** No entrypoint; README said `uvicorn api:app --reload --port 8091`. | **Low** — *not* an exposure: uvicorn's CLI default host is 127.0.0.1, verified live (`0100007F`). It broke the house rule (env override) and put a dev file-watcher in the run instructions. | **Fixed** | `python3 -m api` now binds `FULFILLMENT_BIND_ADDR` (default `127.0.0.1`) on `FULFILLMENT_PORT` (default 8091). Tests (real process, `/proc/net/tcp`): `test_entrypoint_binds_loopback_by_default` (`0100007F`), `test_bind_addr_env_override_is_honored` (`127.0.0.2` → `0200007F`). |
| A13 | **No test above `TestClient`** — the layer where Revenue Recovery's bind/auth bugs hid. | Coverage gap | **Fixed** | `tests/test_live_server.py`: spawns the real entrypoint and checks health 200, no/wrong token 401, correct token 200, non-ASCII token 401 (not 500), `/docs` `/redoc` `/openapi.json` 404, bound address — all over a real socket. Also `test_startup_fails_closed_without_token_in_a_real_process`. |

**Checked and found correct (no change made), with evidence:**

- **Fail-closed startup.** `_load_required_token()` raises at import when the token is unset or empty. Now pinned by a real-process test (above); live: exit code 1 with the RuntimeError.
- **Constant-time compare + non-ASCII guard.** `hmac.compare_digest` wrapped in `try/except TypeError` → 401. Pre-existing `TestClient` test plus the new real-socket test (non-ASCII → 401).
- **`/docs`, `/redoc`, `/openapi.json` disabled.** 404 in-process and over a real socket.
- **SIP dialer and system of record fail closed.** Defaults are `NotWiredSipDialer` (raises → `attempted=false`, `"dialer not wired"`) and `NotConfiguredSystemOfRecord` (`not_configured`). The only alternatives are the in-memory test doubles behind an exact-match `in_memory` opt-in; any other value falls back to the stand-in. **No code path in this repo can place a real call or write to a real external system** — there is no real adapter to reach.
- **No logging of request bodies.** `src/` has no `logging`/`print` calls; uvicorn's access log records method/path/status only.

**Left open, on purpose:**

- **No approval gate exists for a future real dialer/CRM adapter**, because no real adapter exists. When `LiveKitSipDialer` or a CRM adapter is added it must be behind an explicit opt-in *and* a human approval step; `_build_dialer()` today has no concept of one. Not built speculatively.
- ~~**Quiet hours are enforced only where contact actually happens** … any future sender must call `ContactWindow.allows()`.~~ Superseded by fix wave 1 F3: every transport (dialer, SMS/email sender) needs a `ContactAuthorization` from `OutboundContactGate`.
- **The recipient time zone has to be supplied by the caller** (`timezone_by_call_id`). There is no per-customer time zone on `CustomerDossier` or `CallEvent` yet. Since fix wave 1 F3 it is only a *claim*, checked against the number; see that section.
- **Redial/duplicate protection (A3, A11) lives in process memory.** Restart and it is gone; multiple replicas would not share it. Real fix = the persistence layer (gap 6 below).
- **`/agents/resolution-writeback/resolve` is not idempotent across retries.** `resolution_id` is a fresh uuid per call (the Sep 22 fix for ID *collisions*), so a retried request writes a second record. A deterministic idempotency key is a design decision (can the same entity legitimately resolve twice?) — not made here.
- **`_attempted_task_ids`, `_exhausted_resolutions` and `_dossiers` grow without bound**, and the dossier route returns the whole store on every call. Acceptable for a non-live service; not for production.
- **A non-ASCII `FULFILLMENT_SERVICE_TOKEN`** would make every request 401 (the `TypeError` guard fires on every compare). Fails closed, so not a vulnerability; not changed.

## Fix wave 1, Sep 24 2026 — F3: quiet-hours bypass and unlimited redials

**Finding (AEGIS, High, CONFIRMED; latent because no live dialer ships).**
`timezone_by_call_id` was trusted as "the recipient's local zone" without
any check against the number, and the only redial protection was keyed by
the caller-chosen `task_id`. AEGIS probe at 02:00 America/Los_Angeles
dialing `+12135550101`: zone `"UTC"` → `attempted: true`; `"Asia/Tokyo"` →
`attempted: true`; three fresh task ids → **5 calls placed at 02:00 LA
local**. The suite's own tests enshrined it: every dialing test paired
`"+15550101"` (not even a 10-digit NANP number) with `"UTC"`.

**Fix — one gate for every automated contact** (`src/outbound_gate.py`,
`src/recipient_zones.py`; ADR 0002 Decision 11):

- **Zone must fit the number.** For `+1`, the number must be a valid
  10-digit NANP number with a geographic area code (toll-free, premium,
  personal-communications, N11, reserved → refused), and the claimed zone
  must be on an explicit NANP allowlist (US, Canada, US territories,
  Caribbean NANP members; `UTC`, `Asia/Tokyo`, `US/Eastern` … refused).
  The window must then hold in **every zone the number could be in plus
  the claimed zone**: Hawaii (808), Alaska (907), Puerto Rico (787/939),
  USVI (340), Guam (671), CNMI (670) and American Samoa (684) use their
  own zones; every other area code is treated as possibly anywhere in
  continental US/Canada. **Trade-off, accepted:** no area-code table, so
  a continental `+1` number is contacted only 08:00–16:30 Pacific
  (= 12:30–21:00 Newfoundland) — a narrower day, never a night call. A
  claimed zone can narrow that further (a 212 number claimed to be in
  Honolulu needs both New York and Honolulu daytime), never widen it.
- **Other country codes fail closed** unless `FULFILLMENT_COUNTRY_ZONES`
  configures a zone set for that code (e.g.
  `44=Europe/London;61=Australia/Perth,Australia/Sydney`); the claimed
  zone must be in the set and the window must hold in every zone of it.
- **Attempt limits keyed by the phone number (and customer), not the
  task.** Across calls, SMS and email together: at most 3 automated
  contacts per number per rolling 24 h and at least 2 h between them
  (`FULFILLMENT_CONTACT_MAX_ATTEMPTS_PER_24H`,
  `FULFILLMENT_CONTACT_MIN_SPACING_MINUTES` may only narrow). The same
  limits apply per `customer_id` when one is present, so neither a fresh
  task id, a fresh call id nor a fresh customer id buys another contact.
  Thread-safe (one lock around check-and-record). Consequence: the
  escalation sequence's "SMS 10 minutes after a failed call" now waits for
  the 2 h spacing.
- **Enforced where contact happens.** `SipDialerPort.place_call` and the
  new `MessageSenderPort.send` (SMS/email; no sender exists) take a
  single-use, channel-bound `ContactAuthorization`, never a number. Only
  the gate can mint one; the transport gets the number from `redeem()`,
  which re-checks window and limits **on the gate's clock at that moment**
  and records the attempt atomically. SMS/email *task creation* is not
  gated — a task created at 02:00 may properly go out at 09:00 — but no
  sender can reach anyone without passing the gate.
- **Sweep, same class — stale clock (fixed).** The route read the clock
  once per request and reused it for the whole batch, so with a real
  dialer a batch started at 20:59 kept dialing after 21:00. The gate now
  reads the clock per task, at authorization and again at redeem.
  Test: `test_window_is_rechecked_at_dial_time_not_once_per_request`.

**Tests** — `tests/test_fix_wave_1_f3_api.py` (HTTP route; 15 of its 16
tests failed on the pre-fix code, the 16th is the positive control) and
`tests/test_outbound_gate.py` (81 unit tests: window edges in the
strictest zones in daylight and standard time, Hawaii/Alaska edges, zone
allowlist, invalid/non-geographic numbers, country rules, limits,
channel binding, forgery, single use, redeem-time recheck, 16-thread
race, startup refusal of bad config). Mutation-checked: trusting the
claimed zone alone fails 21 tests; dropping the 808 row fails 8; removing
the limits fails 9; removing the recheck at redeem fails 2; removing the
redeem lock fails the race test.

**Tests changed, and why:** every dialing test in
`test_callback_orchestration.py`, `test_audit_2026_09_24.py` and
`test_api.py` used `"+15550101"`/`"+15551234"` with `"UTC"`, the exact
pairing the gate now refuses; they now use real LA/NY numbers in their
real zones at times inside the strict window (`NOON_UTC`/`LATE_NIGHT_UTC`
became `LA_11AM`/`LA_11PM`). The three custom test dialers take an
authorization instead of a number. `FlakyDialer`'s two tasks got
different customers (one customer can no longer be called twice in two
hours). `test_api_quiet_hours_use_the_server_clock` now also asserts the
refusal is for the contact window, not for an invalid number. No
assertion was weakened.

**Suite:** 116 passed before → 213 passed after.

**Still open:** state is in process memory (restart/replicas forget the
attempt history — the persistence layer is still the durable fix);
escalation state (`status`, `attempt_number`) is caller-asserted, which
can mint extra SMS/email *tasks* or NO_RESOLUTION records but no extra
contact, since every contact passes the per-number gate; a person whose
number's area code and claimed zone are both wrong (e.g. a 212 mobile
used in Honolulu, claimed as New York) can still be reached at a time
that is night where they physically are — no data this service has can
detect that.

## Fix wave 1, Sep 24 2026 — F15 money, 5xx sweep, bounded state

| ID | Finding | Fix | Tests (each failed on the pre-fix code) |
|---|---|---|---|
| F15 (fulfillment part) | `LabeledValue.amount_usd` accepted `"1e3"`, `" 12.30 "`, `"012.30"`, `"12.3"` and rounded `"12.345"` up to `"12.35"` (any string `Decimal()` parses, then quantized). A10 above fixed the rounding mode but not this. | `src/fulfillment_schema/money.py` (copy of detection-py's `zbm_schema/money.py` rules): a wire string must be canonical (`^(0\|[1-9][0-9]{0,14})\.[0-9]{2}$`, max `999999999999999.99`), never rounded; a JSON number is refused when parsed from JSON text; computed `Decimal`/`int` values are rounded half-up under an explicit context. `LabeledValue.amount_usd` is `PositiveMoney`. | `tests/test_fix_wave_1_f15_money.py` runs every vector in `fixtures/money_vectors.json` (63 strings × Money/PositiveMoney × Python/JSON text, 9 raw JSON values). 196 of its cases failed before. **No route accepts money in a request body** (only `/fixtures/dossiers` and `/agents/customer-dossier/update` *return* `lifetime_value`, always `null` today); `test_no_http_route_accepts_money_in_its_request_body` walks every route's body model and fails if one is added. |
| Sweep: 500s | Fuzzing every route found one class: `started_at` at the edge of Python's datetime range (`9999-12-31T23:59:59+00:00`) → `started_at + 5 min` overflowed → **500**. Years 0001/9999 were otherwise accepted. | All datetimes bounded to [2000-01-01, 2100-01-01) UTC (`BoundedAwareDatetime`); `FollowUpTask.due_at` (derived by an agent) gets one extra day. | `tests/test_fix_wave_1_fuzz.py` (malformed JSON, wrong types, 2 MB strings, 20 000-digit numbers, NaN/Infinity, lone surrogates, NUL, BOM, invalid UTF-8, 5 000-deep nesting, extreme datetimes, oversized batches, junk query strings — 276 cases). `tests/test_fix_wave_1_live_fuzz.py` replays the corpus against the real process over TCP plus raw malformed HTTP. |
| Sweep: memory | Gate attempt history kept every number/customer and pruned only every 256th contact (21 keys still held 24 h later; 40 000 keys for 20 000 numbers, no cap). Dial dedupe (`set`), exhausted-escalation dedupe (`dict`) and the dossier store grew forever. | Gate: 24 h eviction on its own clock + cap 100 000 keys, refused at authorize and redeem when full. Dedupe stores: `src/bounded_state.py`, 24 h eviction, cap 100 000; at the cap new tasks are not dialed (skip reason) and new exhausted records are 503 before any write-back. Dossiers: cap 100 000 customers / 10 000 per history list, 503 with nothing applied. | `tests/test_fix_wave_1_bounded_state.py` (18). |

**Tests changed, and why:** `test_money_is_half_up_two_decimal_string`
fed the *strings* `"0.125"`, `"1.005"`, `"12.3"` and expected them rounded
— it enshrined F15. It now feeds `Decimal("0.125")`/`Decimal("1.005")`
(computed values, still rounded half-up) and the canonical `"12.30"`; the
three strings moved to the rejection test. Four tests that reset
`api._attempted_task_ids` to `set()` now reset it to `api._new_dedupe()`
(same meaning, new type). No assertion was weakened.
`tests/test_live_server.py` honors `FULFILLMENT_TEST_PORT_RANGE=LO-HI`
for assigned port ranges.

**Suite:** 213 passed before → 815 passed after
(`FULFILLMENT_TEST_PORT_RANGE=19661-19679 python3 -m pytest`).

**Live run:** the 249-case POST corpus against `python3 -m api` on
127.0.0.1:19660: pre-fix code `{200: 68, 400: 31, 409: 2, 422: 146, 500: 2}`,
fixed code `{200: 18, 400: 31, 409: 2, 422: 198}` — no 5xx.

**Still open:** state is still process memory (restart/replicas forget
it). (The dossier whole-store copy was fixed in fix wave 4, below.)

## Fix wave 4, Sep 24 2026 — classes found in sibling services, checked here

AEGIS round 3 found no fulfillment defects; these are the classes it found
elsewhere, each either fixed here (failing test first) or proven absent.
Tests: `tests/test_fix4_limits.py`, `tests/test_fix4_live.py` (real
`python3 -m api` over TCP).

| Class | Before (evidence) | Now |
|---|---|---|
| Body size | No limit. A `Content-Length: 4194305` request with no body bytes sent got no answer (server waiting); an 8 MiB chunked body was read and json-decoded (422). | 4 MiB limit (`BodySizeLimitMiddleware`, `src/api.py`): 413 from `Content-Length` before any body byte is read, 413 while streaming a chunked body. Sized from the max batch: every route's worst-case 1000-item batch (all bounded fields at maximum) fits and returns 200 — largest is orchestrate at 3.57 MB. `voicemail_transcript` cannot be at its 10 000-char maximum across 1000 events (~3 400 chars average fits); such a batch gets 413 and must be split. Item caps (1000 per list/map) were already 422; now tested for every list and map. |
| Event-loop blocking | FastAPI decoded and validated every body on the event loop (only the handler ran in the thread pool) and serialized responses there. `/health` took **3.32 s** while a 64 MiB body was in flight (live test). | No route declares a body parameter: it awaits the bytes, then one thread-pool call parses (pydantic JSON mode), runs the agent, and renders the response. `/health` is a coroutine. Live: worst `/health` 0.15 s with a 4.1 MB valid batch plus a 64 MiB body (with and without `Content-Length`) in flight. Side effect: auth runs before the body is parsed (anonymous invalid JSON: 422 → 401). Non-JSON `Content-Type` is 415. |
| Regex | Every regex site in `src/` inventoried (a test fails if one is added untimed): `E164`, `_NANP`, country-code pattern (`recipient_zones.py`), `_WINDOW_RE` (`contact_window.py`), `_FLOAT_TEXT`, `WIRE_PATTERN` (`money.py`), `_ID_PATTERN` and the `PhoneE164` pattern (pydantic-core's linear Rust engine). None has nested/overlapping quantifiers; all are < 50 ms on 17 adversarial 100 KB inputs each (they passed before the fix — class absent). There is no PII-redaction or error-scrubbing regex in this service (422 bodies are built field-by-field, no `re.sub`). One real defect found: `E164`/`_NANP` used `.match`, where `$` also matches before a trailing `\n`, so the gate accepted `"+12125550101\n"` as a separate key from `"+12125550101"` (API input was already rejected by pydantic). Now `.fullmatch`. |
| Gate cap fill (AEGIS PLAUSIBLE/low) | The 100 000-key history could be filled by a burst of ~50 000 contacts, then every new number was refused for 24 h. | New-key admission budget: ≤ 4 166 new numbers+customers per rolling hour (24 h of admissions < the cap), checked at authorize and redeem; tracked numbers unaffected; still fail closed. `GET /gate/status` (auth) and `gate` in each orchestrate response report utilization / `near_capacity` (≥ 80 %) / budget use; a warning is logged. ADR 0002 Decision 15. A scaled simulation (cap 240, budget 9/h, attacker 100 fresh numbers every 10 min for 24 h) never reaches the cap and a real customer is never refused for capacity. |
| Dossier update cost | Deep-copied the whole store and returned every customer's dossier on every request: **0.96 s** for one update against 20 000 small dossiers (and it disclosed every customer to every caller). | `customer_dossier.apply_updates` copies and returns only the dossiers the request touches; history de-duplication uses sets. Same request: well under 0.25 s, one dossier returned. |

**Found while profiling (not in the brief), fixed test-first:**
missed-call detection rescanned every call from the same number for every
event — quadratic, **3.47 s CPU** for a 1000-call batch from one number;
now two binary searches per event (results checked against the old
definition on random batches). `resolve_timezone` re-read tz files from
disk on every check (zoneinfo strongly caches only 8 zones; the gate checks
44 per +1 number): a 1000-task orchestrate batch held the dial lock for
**4.2 s**; now an `lru_cache(maxsize=1024)` (a cached `None` still refuses).

**Tests changed, and why:** `test_concurrent_dossier_updates_do_not_lose_writes`
slowed `build_or_update`; the route now calls `apply_updates`, so the test
slows that instead (same assertion). No assertion was weakened.
**API behavior changes:** `customer-dossier/update` returns only the
affected dossiers; orchestrate responses gain a `gate` object; anonymous
requests are 401 before any body parsing; oversized bodies are 413; a
non-JSON `Content-Type` is 415.

**Suite:** 815 passed before → 880 passed after
(`FULFILLMENT_TEST_PORT_RANGE=19980-19999 python3 -m pytest`).

**Live run** (`python3 -m api`, `FULFILLMENT_SIP_DIALER=in_memory`, random
token, pre-fix tree on 127.0.0.1:19991 → fixed on :19992, then terminated):
anonymous invalid JSON 422 → 401; `Content-Length: 4194305` with no body no
response (30 s) → 413; 4 MiB+1 chunked 422 → 413; 4.1 MB valid batch 200 →
200; `/health` worst 2.878 s → 0.152 s during max + two 64 MiB bodies;
second dossier update returned `[a, b, c]` → `[b]`; `/gate/status` 404 →
401 anonymous / 200 authenticated; orchestrate at 19:57Z dialed one NY
number and reported `gate.tracked_keys: 2`.

## What was actually verified (Sep 22, 2026, post-fix)

- **64/64 `pytest` passing** — `cd services/fulfillment-py && pip install
  -r requirements.txt && PYTHONPATH=src pytest tests/ -v`.
- **Every one of the ten review findings was independently reproduced
  against the live code before being fixed** (not accepted on the
  reviewer's report alone) — e.g. the non-ASCII-token 500 was reproduced
  via `TestClient`, the resolution-ID collision via two live API calls,
  the repeat-dial via two `orchestrate()` calls on the same task.
- **Live process smoke test, post-fix**: booted `uvicorn api:app` as a
  real OS process, confirmed `/docs` now 404s, confirmed
  `/agents/resolution-writeback/resolve` now returns
  `write_back_status: "not_configured"` (not `"success"`) over real
  HTTP, and confirmed a non-ASCII bearer token now returns 401 (not 500)
  over real HTTP — all three were the critical/high findings, verified
  fixed outside the test suite too, not just inside it.
- **Fail-closed startup verified live**: ran the module with
  `FULFILLMENT_SERVICE_TOKEN` unset and confirmed it refuses to start.

## Known gaps — read before treating this as more than it is

1. **SIP/LiveKit telephony is NOT live.** The decision logic in
   `callback_orchestration.py` is real and tested — there is no concrete
   LiveKit-backed implementation. This sandbox has no phone number or
   carrier account. `NotWiredSipDialer` fails loudly rather than
   pretending, and — as of this review — is now also the API's actual
   default, not just the test suite's.
2. **No system-of-record / CRM integration wired**, and this is now the
   API's honest default too (finding #1 above), not something a caller
   could mistake for a working integration.
3. **No orchestrator/ledger/dashboard layer.** No cross-agent pipeline
   runner connects detect → dial → escalate → resolve automatically —
   deliberate scope decision (ADR 0002 Decision 2), sharpened by finding
   #3 above: the ADR's completeness language was corrected to not
   overclaim what this implies.
4. **No caller-identity resolution.** Confirmed real by review (finding
   #7): an unmatched caller gets a callback task but no dossier entry,
   and there's no pending-identity record to merge into one once
   identified.
5. ~~Business-hours check assumes UTC-normalized timestamps.~~ **Fixed
   Sep 24 2026 (audit A1)**: calls are gated on 08:00–21:00 in the
   recipient's local time and fail closed without a known time zone. What
   remains: the time zone must be supplied per request
   (`timezone_by_call_id`); nothing stores it per customer yet. Since fix
   wave 1 F3 that claim is checked against the number and can only
   narrow the window.
6. **In-memory state only.** `api.py` keeps dossiers, dial attempts, and
   write-backs in process memory. Since the Sep 24 audit (A3) the API
   refuses to redial a task_id it already dialed, and since fix wave 1
   F3 limits contacts per phone number and per customer — but only for
   the life of the process; a restart or a second replica forgets. The durable fix
   is the persistence layer named in gap 3. Since fix wave 1 all of it
   is bounded (24 h eviction and hard caps, fail closed at a cap).
7. **No real client connected.** Every endpoint takes request-supplied
   data or serves from `fixtures/fulfillment_*.json`.
8. **Single shared-secret bearer token, not a real auth system.**
   Adequate for a private network, not for anything internet-facing.
9. ~~Same non-ASCII-token-500 and docs bugs in `services/detection-py`.~~
   Stale as of Sep 24 2026: `detection-py/src/api.py` now has the
   `except TypeError` guard and `docs_url=None` (root README findings
   #1 and #2).
10. **Two review passes now (Sep 22, Sep 24), not certification.** Both
    were done by Claude sessions, not by a human security reviewer. A
    different reviewer may find different things.

## Fix wave 5, Sep 24 2026 — transport limits (NEW-3) and burst-shaped new-key budget (NEW-5)

Tests first, failing on the pre-fix tree, then passing:
`tests/test_fix5_http_limits_live.py` (real `python3 -m api` over TCP) and
`tests/test_fix5_new_key_burst.py`. ADR 0002 Decisions 17 and 18.

| Finding | Before (evidence) | Now |
|---|---|---|
| NEW-3 (MED): unauthenticated, unbounded request heads; no idle / partial-head / slow-body deadline | `python3 -m api` ran uvicorn defaults (httptools, no deadlines). Pre-fix run of the new tests: a 150 MB header was accepted in full; 0/20 idle + partial-head sockets closed within 15.5 s; a slow body got no 408 and, unauthenticated (early 401), was held > 10.5 s; 0 of 296 sockets closed (no connection cap). | **Transport limits** (`src/http_limits.py`, used by `python3 -m api`): h11 parser, 16 KiB head cap (400 while reading; middleware 431 under other launchers); 10 s head deadline; 5 s idle keep-alive; 30 s body deadline (408, and the connection is closed 5 s later even if the app never reads the body); `limit_concurrency` 128; hard cap 256 open sockets. Live: 150 MB header → 400 after the first 1 MiB, RSS 51 088 → 51 176 KiB; 10 idle sockets closed at 10.02 s; `/health` < 1 s throughout. Trade-off: ≥ 128 held sockets → 503 (incl. `/health`) until they are closed (≤ 10 s if they never send a head). |
| NEW-5 (LOW): one burst spends the hour's new-key budget | 4 166 fresh numbers in 0.34 s admitted all 4 166; a legitimate new number was then refused at +2 s, +30 min and +59 min 59 s. | Token bucket on new keys: burst 100, refill 1/60 of the hourly budget per minute (69.4/min); the rolling-hour budget still applies on top. Same burst: ≤ 100 new keys admitted, a legitimate number admitted at +2 s. Live (`python3 -m api`): 5 000 fresh numbers+customers in 0.24 s → 50 contacted, legitimate new number refused at once, contacted 2.5 s later. Still open: a *sustained* attacker competes for every token (no per-caller identity exists to scope by). `/gate/status` adds `new_key_burst`, `new_key_tokens_available`, `new_key_refill_per_minute`, `new_key_burst_exhausted`. |

**Tests changed, and why:** `test_fix4_limits.py::test_gate_decisions_for_a_max_batch_do_not_reread_the_tz_database`
now passes `new_key_burst=10_000`: it measures tz-file reads for 1 000
new numbers at one instant, which the new bucket would otherwise stop
after 100. No assertion was weakened.
**API behavior changes:** an oversized head is 400/431; a slow body is 408
or closed; over 128 concurrent connections/requests is 503; a batch with
more than 50 new customers at once gets the excess refused ("retry in a
few seconds").

**Suite:** 880 passed before → 901 passed after
(`FULFILLMENT_TEST_PORT_RANGE=20140-20159 python3 -m pytest`).

## Fix wave 6, Sep 24 2026 — 422 amplification (N2) and over-cap connections (N7)

Tests first, failing on the pre-fix tree, then passing:
`tests/test_fix6_n2_422_amplification.py` (in-process and against the real
`python3 -m api` over TCP) and `tests/test_fix6_n7_over_cap_503.py` (real
process). ADR 0002 Decisions 19 and 20.

| Finding | Before (evidence) | Now |
|---|---|---|
| N2 (MED, CONFIRMED): 422 amplification | The A8 handler stripped `input` but listed every error. A 868 KB body of 60 000 unknown keys against an `extra="forbid"` request model → 60 000 `extra_forbidden` errors, a **5 388 973-byte** 422, built on the event loop; a single 1 MiB unknown key echoed whole in its `loc` (1 048 743-byte 422); 1 000 empty events → 518 KB. Live (`ful_amp.py`, 20 senders × 5 × 60k-key bodies): peak RSS **821 MB**, `/health` p50 838 ms, max 2.58 s; with 3.9 MB bodies of 260k keys: 23.5 MB per 422, RSS 4.4 GB, `/health` up to 11.5 s, ten 408s. | **Bounded 422** (`src/api.py`, `_bounded_validation_body`): at most 20 errors listed (pydantic's order) plus the honest `error_count` and `truncated: true`; each `loc` ≤ 8 items of ≤ 40 chars; `type` ≤ 64, `msg` ≤ 200 chars; the serialized body is kept ≤ 8 KiB. **Not enumerated:** a request object with more than 20 unknown keys is refused with one `too_many_fields` error before any field is looked at (up to 20 unknown keys are still named — a stale client sending `now` still learns that); a map field over 1 000 entries is refused by size before its entries are validated (pydantic checked every entry of a dict first — 250 000 errors for a 4 MiB map — but a list's length first). **Off-loop, one at a time:** parsing + the 422 render happen in the worker thread; bodies are parsed one at a time (a parse holds the GIL in Rust for its whole duration — a second one adds memory and loop latency, not throughput; the agent work itself is not behind that slot) and the body is held once (bytearray), not twice. `python3 -m api` also limits glibc to one malloc arena: per-thread arenas kept tens of MB of freed parse memory. Live, same probe: 60k × 20: 422s of **139 bytes**, peak RSS 89 MB (49 MB idle), `/health` p50 56 ms, max 194 ms; 260k × 20: 140 bytes, peak 224 MB, `/health` p50 78 ms, max 424 ms, no 408. (This row originally also claimed "RSS settles at 93 MB"; that was not verified — fix wave 7 measured that it did not settle, and fixed it. See below.) |
| N7 (INFO): over-cap connections aborted | With 256 sockets held, a new connection was aborted with no response (curl exit 000, "connection reset"). Between 128 and 256 held sockets every new request, `/health` included, is 503. | A connection made while 256 are held is answered a minimal `503 Service Unavailable` (`Connection: close`, `Retry-After: 1`) and closed as soon as its request bytes arrive, or after 1 s if none do; it is never counted as held. The 128–256 behavior is unchanged and now stated exactly below ("Transport limits, exactly"). |

**Transport limits, exactly** (values in `src/http_limits.py`):
- fewer than 128 open connections: normal service;
- 128 or more held sockets (and fewer than 256): uvicorn answers every
  *new* request — `/health` included — 503 for as long as they are held.
  Sockets that never send a head are closed after 10 s; idle keep-alives
  5 s after their last response; unfinished bodies 35 s after their head
  (fix wave 8: stalled or trickling ones ~5 s after they stop, 10 s when
  the app is not reading them).
  A client that keeps sending complete requests on 128 sockets keeps the
  service at 503 for everyone else for as long as it does;
- a connection made while 256 are held: minimal 503, then closed (above);
  `/health` from a fresh connection is therefore 503, not a reset, in that
  state too.

**Tests changed, and why:**
`test_fix5_http_limits_live.py::test_connection_count_is_bounded_and_health_recovers`
asserted a bare EOF on over-cap sockets — i.e. the abort. It now asserts
a 503 followed by EOF within 1 s. Nothing was weakened.
**API behavior changes:** 422 bodies carry `error_count` (and `truncated`
when the list is cut); more than 20 unknown top-level keys is one
`too_many_fields` error at `["body"]`; a >1 000-entry map is `too_long`
before its entries are checked. Otherwise the documented shape is
unchanged (`{"detail":[{"loc":[...],"type":"...","msg":"..."}],"error_count":1}`).
**Sweep** of every other path that could echo or enumerate request
content: 413/408/431/400/415 bodies are fixed strings; 401/409/503
`detail`s are fixed strings or an enum value; the escalate 409 reports the
task status enum; orchestrate skip reasons come from the gate/dialer
(a dialer exception surfaces its type only, A4); `recipient_zones`
messages include the claimed zone, bounded to 64 chars by `TimezoneName`;
the only log line with request-derived content is the gate capacity dict.
uvicorn's own access log prints the request line (path ≤ 16 KiB head) —
1:1, not amplification. No 500 handler renders anything (FastAPI's default
`Internal Server Error`).

**Suite:** 901 passed before → 926 passed after
(`FULFILLMENT_TEST_PORT_RANGE=20320-20339 python3 -m pytest`).

## Fix wave 7, Sep 24 2026 — the parse slot starved small requests (NEW-4)

Tests first, failing on the pre-fix tree, then passing:
`tests/test_fix7_new4_parse_fairness.py` (43 tests: pre-scan, lanes, budget,
head-refusal hold in-process; the AEGIS scenario against the real
`python3 -m api` over TCP with 8 and 32 senders of each junk kind). ADR 0002
Decision 21.

| Finding | Before (evidence) | Now |
|---|---|---|
| NEW-4 (MED, CONFIRMED): the single parse slot is FIFO, so authenticated junk bodies starve legitimate requests linearly | Fix wave 6 parsed one body at a time, in arrival order, and every body reached the full parse: a 3.4 MiB body of 255 000 unknown keys cost ~140 ms (300k one-key objects ~230 ms) to be told `too_many_fields`. AEGIS `ful_slot6.py`: 8 senders looping such bodies → legit small `detect` p50 **1.08 s** (baseline 4 ms); 32 → 4.4 s. Pre-fix run of the new live test on the fix box: 8 senders p50 1 093 ms, 32 senders p50 2 851 ms / p99 6 375 ms; 12 and 6 legit requests completed in 6 s. Also: RSS after a flood did **not** settle (170 MB after 32 × 10 s, unchanged 15 s later; the "settles at 93 MB" claim above was unverified), and a 4.39 MB body 413'd from Content-Length cost ~1 ms each with nothing to slow the sender: 8 looping senders got ~400 attempts/s through and legit p50 went 4 → 45 ms. | **Shape pre-scan** (`src/api.py`, `_json_shape`): > 32 000 members, > 4 000 objects/arrays or > 32 levels is one bounded 422 (`json_too_many_members` / `json_too_many_containers` / `json_too_deep`) before the full parse — byte-level, C passes only, strings removed exactly (a legitimate body is never refused; every route's maximum batch uses < ⅔ of each cap), 1–52 ms on every 4 MiB shape tried, 3–11 ms for the AEGIS bodies. **Two lanes**: bodies ≤ 64 KiB parse in their own lane (4 slots), never behind a large one; bodies > 64 KiB take their Content-Length from a FIFO token bucket of **32 MiB/s** (burst 16 MiB) *before being read*, then parse one at a time; not admitted and parsed within 2 s → **503 + `Retry-After: 1`** (uvicorn drains the unread body at the parser, ~2.5 ms per 3.4 MiB; the connection stays usable). **Head-only refusals held 250 ms** (413 from Content-Length, 431, 400 bad Content-Length): a looping client gets 4 attempts/s per connection. **Idle memory**: 1 s after the last large parse with none in flight, `malloc_trim(0)` returns freed heap pages. Live (`ful_slot6.py`, 10 s each): 8 × keys — legit p50 **4 ms**, p90 9 ms, max 91 ms, `/health` p50 2 ms, RSS peak 67 MB, idle 51 MB 2 s later; 32 × keys — p50 4 ms, p90 11 ms, max 58 ms, `/health` p50 2 ms, max 143 ms, peak 111 MB, idle 52 MB; 32 × array / deep — p50 5 / 4 ms; 8 and 32 × 4.39 MB (413) — p50 4 ms; 120 × keys — every request 503 (uvicorn's 128-connection limit, fix wave 5, unchanged). New live test, 32 senders: legit p50 3 ms, p99 69 ms, `/health` p99 39 ms, a maximum 1000-task orchestrate batch posted mid-flood: 200 in 2–5 s (1–2 attempts, honoring `Retry-After`). |

**The fairness guarantee and its limits** (ADR 0002, Decision 21): a small
body waits for at most 4 small parses ahead of it and never for a large one;
the loop spends at most the budget's share of its time receiving large
bodies. Not solved: the loop and the GIL are still shared, so one pre-scan,
one bounded parse or the reading of one large body in progress (tens of ms)
can delay a small request — not the queue of them; there is no caller
identity, so a legitimate large batch competes FIFO with junk for the budget
and is 503'd like any other when more than ~2 s of large bodies (~19
maximum-size ones) are queued ahead of it, succeeding on a retry when its
turn comes — a client that keeps sending large bodies keeps everyone's large
batches waiting for as long as it does; a flood of *small* junk (≤ 64 KiB,
bounded per attempt) has no budget and can still load the loop; at ≥ 128
held sockets everything is 503 before any of this.

**Tests changed, and why:**
`test_fix6_n2_422_amplification.py::test_60k_unknown_keys_is_one_small_422_not_60k_errors`
asserted the error type `too_many_fields`; a 60 000-key object is now over
the 32 000-member shape cap and is refused earlier by the pre-scan as
`json_too_many_members` (one error, 139 bytes, faster). The 21-key case in
the same file still asserts `too_many_fields`. Nothing was weakened.
**API behavior changes:** the three `json_*` 422 types above; large bodies
may get 503 + `Retry-After: 1` under load and must retry; 413/431/400
(bad Content-Length) answers arrive 250 ms later.

**Suite:** 926 passed before → 969 passed after
(`FULFILLMENT_TEST_PORT_RANGE=20520-20539 python3 -m pytest`).

## Fix wave 8, Sep 24 2026 — Content-Length pre-allocation pinned memory (N7-2)

Tests first, failing on the pre-fix tree, then passing:
`tests/test_fix8_n7_2_body_prealloc.py` (20 tests: allocation, the in-flight
budget and the throughput rule in-process against the ASGI app with
hand-driven chunk delivery; the AEGIS scenario, a trickle, a front-loaded
stall by 128 senders, and legit large batches against the real
`python3 -m api` over TCP). ADR 0002 Decision 22.

| Finding | Before (evidence) | Now |
|---|---|---|
| N7-2 (MED-HIGH, CONFIRMED; a fix wave 7 regression): the body buffer was pre-allocated from the client's Content-Length | Fix wave 7 sized the buffer up front — `bytearray(declared)`, a 4 MiB memset per request before a byte of the body had arrived. AEGIS `ful_idle7b.py`: 128 connections each sending a head with `Content-Length: 4194304` plus ONE byte → RSS **55 → 569 MB**, held until the 30 s body deadline, at ~zero bandwidth. Pre-fix run of the new tests: 32 such requests in-process traced 21 MiB at 0.3 s (the large-lane budget admits ~8/s, each allocating 4 MiB on admission); live, 128 idle bodies: RSS **50 → 138 MB** after 4 s. Two more things the deadline alone allowed: a body trickling 1 byte per 20 s, or 3.9 MB sent at once and then nothing, was held for the whole 30 s; and the only bound on bytes buffered across connections was 128 × 4 MiB = 512 MiB. | **Nothing is allocated ahead of the bytes received** (`src/api.py`, `_off_loop`): the buffer grows as chunks arrive (bytearray's own amortized growth — grown pages are not touched until bytes land in them, so resident memory tracks bytes received; measured 0.13 ms per 3.4 MiB in 64 KiB chunks vs 0.32 ms for the pre-sized-and-memset buffer). **One in-flight byte budget** (`_INFLIGHT_BODY_BYTES`, 64 MiB = the large lane's 2 s at 32 MiB/s): every chunk is reserved from it before it is buffered; a request that cannot buffer its next chunk within 2 s gets **503 + `Retry-After: 1`**; the reservation is released when the body is parsed or the request ends (disconnect included). **Minimum throughput** (`http_limits.BODY_MIN_BYTES_PER_S` 1 KiB/s, `BODY_MIN_RATE_GRACE_S` 5 s): a body that sends nothing for 5 s of waiting (a stall, however much it sent first) or has averaged under 1 KiB/s after 5 s of waiting (a trickle) is **408** — measured on time spent waiting for the client only, so the service's own budget wait is never charged to the client; for a body the app is not reading (after an early 401) the protocol closes the socket on the same rule judged 5 s later, so an app-side 408 is always written first. Live after (`ful_idle7b.py`, 40 s, 320 connections): RSS **50 → peak 52 MB**, 53 MB after close, `/health` 200 in 0.10 s during the hold (the idle bodies are cut, freeing their slots). 128 senders of 3.9 MB then a stall: peak RSS 138–142 MB (base 54: the 64 MiB budget plus uvicorn's own per-connection buffers), 20 admitted bodies 408'd at ~5 s, 108 refused 503, RSS back under base + 24 MB by ~10 s — not 30. |

**Sweep** (allocations driven by a client-declared size): the body buffer was
the only one. Heads are capped at 16 KiB by the parser before anything is
allocated for them (`h11_max_incomplete_event_size`) and re-checked by the
middleware; the query string is part of that head; there is no multipart;
the 422 builder and the JSON pre-scan allocate from bytes actually received
(the pre-scan's split is bounded by its quote-count check). Bytes uvicorn
itself buffers before the app reads them (≤ 64 KiB per connection, its
flow-control high-water mark; ≤ 256 connections) are outside the in-flight
budget and are the remaining per-connection cost.

**Fairness re-checked** (`ful_lanes7.py`, 10 s runs after the fix): 8 hot
junk senders — legit small p50 **5 ms**, p90 8 ms, `/health` p50 2 ms, legit
large batch 8/8, RSS peak 67 MB, 58 MB 2 s after; 32 hot — legit small p50
5 ms, p90 8 ms, `/health` p50 2 ms, RSS peak 89 MB, 58 MB after; 32 polite —
legit small p50 3–4 ms, `/health` p50 2 ms. The legit *large* batch under 32
continuous senders is the wave-7 limit (b), unchanged: over ten 10 s runs it
succeeded 15/33 attempts (the pre-fix tree, same box, alternated: 16/27);
every refusal was the large lane's 32 MiB/s budget (`detail` says so), never
the new in-flight budget. **Harness:** `tests/test_live_server._start` piped
the server's stdout to an undrained `PIPE`; uvicorn's access log filled the
64 KiB pipe and blocked the server (measured: after ~1 100 `/health`
requests). It now writes to a temp file (read back on an early exit), like
the DEVNULL starter in `test_fix5_http_limits_live.py`; a new test sends
1 500 requests through it.

**Tests changed, and why:** none weakened. The wave-7 test
`test_understated_overstated_or_missing_content_length_still_parses_the_bytes_sent`
keeps its assertions (its docstring no longer describes a pre-sized buffer).
**API behavior changes:** 408 for a stalled or trickling body after 5 s (was:
only at 30 s); 503 + `Retry-After: 1` when 64 MiB of body bytes are already
buffered and a chunk cannot be admitted within 2 s.

**Suite:** 969 passed before → 990 passed after
(`FULFILLMENT_TEST_PORT_RANGE=20720-20739 python3 -m pytest`).

## Fix wave 9, Sep 25 2026 — in-flight budget fairness and slow uploads (AEGIS round 8, Q1/Q2)

AEGIS round 8 could not finish two questions about the wave-8 body handling;
assessed here with real sockets against `python3 -m api` (ports 20920–20939),
then fixed. Tests first, failing on the pre-fix tree, then passing:
`tests/test_fix9_inflight_fairness.py` (15 tests: 12 in-process against the
ASGI app with hand-driven chunk delivery, 3 against the real launcher over
TCP). ADR 0002 Decision 23. Harness numbers below are 25–30 s runs: N
authenticated senders on `detect` reconnecting whenever answered; a legit
200-byte `detect` every 0.1 s (sequential) and a legit 3.57 MB
`callback-orchestration/run` max batch every ~1 s; 2-core box.

| Question | Before (evidence) | Now |
|---|---|---|
| **Q1**: can an authenticated client exhaust the 64 MiB in-flight budget and starve legit clients while staying above the 1 KiB/s floor? | **Plain 2 KiB/s, Content-Length 4 MiB — no in-flight starvation:** N=16: small p50 3.7 ms, p99 20 ms, large 18/18; N=64: p50 3.7 ms, p99 29 ms, large 18/18. At 2 KiB/s a body holds ≤ 60 KiB by the 30 s deadline; 64 × 60 KiB ≪ 64 MiB. **N=128: total starvation, but not via the byte budget** — uvicorn's `limit_concurrency` (128) answers 503 before any app code: small 182/186 503, large 0/19 (see "Not fixed"). **Front-loaded senders — yes, starvation:** declare 4 MiB, send 1 MiB at once, then 2 KiB/s (the 1 KiB/s rule credits the front-load for ~1 000 s): N=64 → small p99 **1 940 ms** (waiting on the shared budget at the 2 s refusal edge; 100 requests in 24 s instead of ~225), large **1/6**. Declaring only what is sent (1.06 MiB, 1 MiB front) N=64: small p99 **1 975 ms**, large **1/7**; N=100 × 0.6 MiB: small p99 **1 973 ms**, large **0/7**. | **Small bodies never touch the shared budget:** each body's first 64 KiB comes from a reserve of `LIMIT_CONCURRENCY` × 64 KiB (8 MiB) that the real launcher's concurrency limit cannot exhaust. **Time-weighted charge + preemption:** each body is charged (shared bytes held) × (seconds the service waited on *its client*); when a body cannot reserve its next chunk, the in-flight body with the largest charge — if ≥ 4 MiB·s and more than the waiter's — is cut with 408 (`detail` says "preempted") and its bytes go to the waiter. **Arrival projection** (Q2 below) refuses a declared body that cannot arrive in time at ~5 s. **Refund:** declared bytes never received go back to the large lane's 32 MiB/s budget (otherwise senders cut early would drain it faster). Same runs after: front 1 MiB N=64 → small p99 **35 ms**, large **22/22**; fit 1.06 MiB N=64 → p99 **62 ms**, large **22/22** (all 64 senders answered 408 — this shape is not refused by the projection until ~28 s, so preemption is what freed the bytes); N=100 × 0.6 MiB → p99 **25 ms**, large **23/23**; plain 2 KiB/s N=16/64 → p99 18/48 ms, large 19/19, 17/17 (the senders now get 408 at ~5 s from the projection). N=128: unchanged (connection slots). |
| **Q2**: does a legit slow mobile upload of a max body succeed? | **No, and worse than the question assumed.** 4 MiB at 64 KiB/s needs 64 s > the 30 s deadline: 408 at **30.0 s**, after uploading ~1.9 MB. At 0.85 × the rate the size needs: 408 at 30.0 s. And a **new defect** found here: at **1.15 ×** that rate (160 KiB/s, arrives in 26 s) the body was **503 at 26.1 s** after arriving whole — `_off_loop` measured the large lane's 2 s admission window from the request's *arrival* and applied it to the parse-slot wait after the body was complete (and to every pay-as-it-streams take of a chunked body), so **every large body that took > 2 s to upload was refused** (in-process: a 600 kB body over 2.6 s → 503, with or without Content-Length). | **Decision: keep the 30 s deadline; a body must arrive at ≥ size / 30 s** (a 4 MiB batch ≥ 136.5 KiB/s; a 64 KiB/s client must split into requests of ≤ ~1.5–1.9 MB). A longer deadline for large bodies would lengthen how long every slow sender holds memory — the budget Q1 is about. A body with a Content-Length that, from the 5 s grace on, cannot arrive by the deadline at its observed rate (bytes ÷ time waiting on the client) is **408 at once**: `request body of N bytes is arriving at ~R bytes/s and cannot complete within the 30s body deadline (this size needs >= N/30 bytes/s); split the batch into requests of at most ~0.8·R·30 bytes at this rate, or send faster`. The admission windows now run from when each wait starts (parse slot: from body completion; chunked takes: per take). Live, same 4 186 017-byte body, in parallel: 1.15 × → **200 at 26.2 s**; 0.85 × → **408 at 5.0 s**; 64 KiB/s → **408 at 5.0 s**, both with the split message. In-process at a scaled boundary (3 s deadline, 0.5 s grace): 1.3 × → 200, 0.75 × → 408 at ~0.5 s. |

**The guarantee, exactly** (ADR 0002 Decision 23). Under `python3 -m api`:
(1) a body ≤ 64 KiB never waits for in-flight bytes; (2) a large body is
refused 503 for in-flight bytes only when every body holding them has
accrued < 4 MiB·s — so keeping the budget full against newcomers needs
bodies that turn over: N holders of 64 MiB / N each must finish within
4 MiB·s ÷ (64 MiB / N), i.e. a sustained **1 024 / N MiB/s of real upload
bandwidth** (8 MiB/s at N = 128, 16 MiB/s at N = 64) — bounded by bandwidth
spent, no longer by time held; (3) a body arriving at ≥ 2 MiB/s is never
preempted (a max 4 MiB body at 2 MiB/s accrues < 4 MiB·s); (4) a declared
body that cannot arrive in time is told so at ~5 s.
**Limits:** (a) a legit *slow* large body (a mobile client at a few hundred
KiB/s) accrues byte-seconds like an attacker's and **is** preempted under
contention (408; retry or split) — nothing is preempted unless someone is
waiting; (b) no caller identity (one shared token), so this is not
per-client fairness; (c) a chunked body has no size to project and is bound
by the 1 KiB/s floor, the deadline and preemption; (d) the projection uses
the average rate so far — a client that would speed up later is refused
anyway; (e) the small reserve cannot be exhausted only because of
`limit_concurrency`; under another launcher it can (503 after the wait, as
before).

**Not fixed — connection slots (N = 128).** 128 held connections, at any
rate ≥ 1 KiB/s (or idle heads for 10 s, or keep-alives for 5 s), make
uvicorn answer every new request 503 before the app runs — the fix wave 5
trade-off in `src/http_limits.py`, unchanged here: measured before and after
this fix, small 180–182/186 503, large 0/19. The projection cuts declared
slow senders at ~5 s instead of 30 s, but a sender that reconnects at once
keeps its slot. Fixing it needs per-client identity (separate tokens) or a
fronting proxy with per-client connection limits; not in scope of this wave.

**Tests changed, and why:** three wave-8 tests in
`test_fix8_n7_2_body_prealloc.py` exercised the shared budget with 8–16 KiB
bodies — which, by design now, never draw on it (that was the flaw). They
now set `_SMALL_BODY_BYTES` to 1 KiB for the test so the same bodies count
as large and the shared budget is still what they exercise; assertions
unchanged: `test_stalled_bodies_exhaust_the_inflight_budget_and_the_next_chunk_is_503_until_released`,
`test_inflight_budget_is_released_when_a_sender_disconnects_mid_body` (would
otherwise pass vacuously), `test_time_spent_waiting_for_the_inflight_budget_is_not_charged_to_the_client`.
**API behavior changes:** 408 at ~5 s (not 30 s) for a declared body that
cannot arrive in time, with a split hint; 408 "preempted" for the heaviest
slow large body when the shared budget is contended; large bodies that take
> 2 s to upload are no longer 503'd after arriving.

**Suite:** 990 passed before → 1005 passed after
(`FULFILLMENT_TEST_PORT_RANGE=20920-20939 python3 -m pytest`).

## Fix wave 10, Sep 25 2026 — one clock for the early 408 (N9-6), disconnect log noise (N9-8)

AEGIS round 9. Tests first, failing on ea708a1, then passing:
`tests/test_fix10_body_clock_and_disconnect.py` (10 tests: 8 in-process
against the ASGI app, 2 against the real launcher with its log captured).

| Finding | Before (evidence) | Now |
|---|---|---|
| **N9-6**: the early 408 mixed clocks | The arrival projection (fix wave 9) took the rate on the time the service spent *waiting on the client* (`received / waited`) and compared `now + (declared − received) / rate` with the *wall-clock* deadline. Whenever the service itself holds a body (large-lane admission, a wait for in-flight bytes) the two clocks part: the hold left the wall budget but not the rate's denominator, and the bytes the client sent during it were still unread. New test, scaled boundary (3 s deadline, 0.5 s grace, 300 kB body needs 100 kB/s), client steady at 1.3×, service holds the body 1.3 s: **408 at 2.1 s** "arriving at ~133 581 bytes/s … needs >= 100 000 bytes/s" — the client had sent its last byte at 2.3 s. Round-9 probe showed the same inconsistency live (r138000: "arriving at ~141 102 bytes/s … needs >= 139 810"). | **One clock: the time the service spent waiting for the client's bytes** — the clock of every other rule that judges the client (the stall and 1 KiB/s rules, the preemption charge). A declared body is 408 from the grace on iff `waited + (declared − received) / (received / waited) > 30 s`, i.e. iff its rate on that clock × 30 s < its size (`src/api.py`, `BodySizeLimitMiddleware`, rule (c)). 30 s is the most waiting any body can get, because waiting only happens before the wall-clock deadline. **Why not wall time for both:** the rule's job is to tell a *client* it is too slow; time the service holds the body is not the client's, and at the end of a hold the client's bytes sent meanwhile are still unread, so a wall-clock rate reads low exactly then. The wall-clock 30 s deadline stays, unchanged, as the hard bound (408, and the protocol closes 5 s later). Same test after: **200**. A 0.8× client with the same hold: still 408 at the grace (0.5 s). No hold: 1.1× → 200, 0.9× → 408 at the grace; the 408's reported rate is now always below the needed rate. |
| **N9-8**: `ClientDisconnect` traceback per disconnect | A client that went away mid-body raised starlette's `ClientDisconnect` out of the app; uvicorn logged `Exception in ASGI application` and a ~60-line traceback each time. Pre-fix live run of the round-9 probes on this box: **78** tracebacks, all `ClientDisconnect`. | When the client's `http.disconnect` actually reached the app, one line at WARNING, no traceback, nothing answered: `client disconnected mid-body: route=<path> bytes_received=<n> declared=<Content-Length or none> elapsed_s=<s since the request reached the app>` (the path is escaped and cut to 200 chars — it is client-supplied). A `ClientDisconnect` without a disconnect, and every other exception, propagate unchanged, so the launcher still logs their tracebacks (tested live with a deliberate `RuntimeError`: 500 + traceback). Same probes after: **0** tracebacks, 78 one-line warnings. |

**Live probes** (round-9 `ful_rate9.py`, `ful_reserve9.py`, `starve9.py`,
ports 18580/18581, before = ea708a1): `ful_rate9` (8 concurrent 4 MiB − 16
byte bodies): ≥ 139 810 B/s → 200 both; 120 000 and 65 536 B/s → 408 at
5.4–5.6 s both; **138 000 B/s (0.987× the needed rate): before 408 at
14.7 s, after 408 at 24.6 s** (having sent 3.4 of 4.2 MB), reported rate
139 809 B/s — see limit (1). `ful_reserve9` N=64: legit p50 3 ms both.
`starve9` front 1 MiB N=64 / front 3.72 MiB N=16: small p99 28.6 → 23.1 ms /
14.9 → 28.8 ms, large all 200 both (run-to-run noise; no preemption either
run).

**Limits, exactly.** (1) Bytes that arrive while the service is not waiting
(its own processing between reads, its holds) are credited at no waiting
time, so the rate on this clock reads slightly high — without contention
~1 % on the probe box (138 000 B/s measured as 139 809). A body within that
margin below the needed rate is refused later than the grace, or cut by the
30 s deadline instead; before this fix the wall-clock remainder happened to
offset that margin (14.7 s vs 24.6 s above). (2) The projection no longer
counts service holds against the client, so a body the service delays past
the wall-clock deadline is cut by the deadline (408 "not received within
30s"), not refused early by the projection.

**Tests changed:** none. **API behavior changes:** a declared body a service
hold used to push over the projection is no longer refused 408; a client
disconnect mid-body no longer produces an error-level log with a
traceback. The disconnect line is the only new log line with
request-derived content (the escaped path).

**Suite:** 1005 passed before → 1015 passed after
(`FULFILLMENT_TEST_PORT_RANGE=18550-18569 python3 -m pytest`).

## Fix wave 22, Sep 28 2026 — the 128-sender RSS bound held by the product (N21-C-1), drains bounded (N21-C-3)

AEGIS round 21: `test_live_128_senders_of_3_9mb_that_then_stall_are_bounded_by_the_inflight_budget_and_cut`
exceeded its 96 MiB growth bound under load (2/10 module, 5/20 single), driven by the wave-21 reading client, and
the wave-21 "0/10" predated the change. The bound is unchanged. What the client added and the fix are in ADR 0002
("Fix wave 22") and ADR 0003 §9; in short: bigger reads left more unread body buffered per connection (uvicorn's
64 KiB high-water + the up-to-256 KiB read that crossed it), held through the drain; and the peak's run-to-run
variance was glibc's dynamic mmap threshold. Product changes: `src/graceful_close.py` (the module shared by the ten
Python services: 16 KiB reads through one buffer, drains discard in it, the slot and the answered body released
before the drain, at most `FULFILLMENT_DRAINS_MAX`=512 drains) and `api._fix_mmap_threshold` (M_MMAP_THRESHOLD
fixed at 128 KiB by `python3 -m api`, glibc only).

Evidence, 3 busy loops (`w22/ful_loops.sh`; single = the test alone, module = `test_fix8_n7_2_body_prealloc.py`):

| Code | single 20x | module 10x | growth (single / module) |
|---|---|---|---|
| wave 21 (e49d506) | **2 failed** (106, 97 MiB) | 0 failed | 83-106, median 86 / 81-86 |
| bounded reads (8424e27) | 0 failed | 0 failed | 81-94, median 87.5 / 79-87 |
| + fixed mmap threshold (1391b5c) | 0 failed | 0 failed | **77-80, median 79 / 74-77** |

New tests: `tests/test_live_graceful_close_module.py` (the shared module: pinned, identical in every service; the
slot released before the drain; the drain cap; allocation-free drained reads), `tests/test_fix22_drain_residual.py`
(pins the blocking-client residual — a blocking `sendall` of 3.9 MB answered 503 early is reset after the bounded
drain — and the reading client's clean 503), `tests/test_fix22_mmap_threshold.py` (the allocator mechanism in a
child process: 47 MiB resident with the dynamic threshold, 0 with it fixed; the launcher sets it). Tests changed: none
of the existing ones; `test_live_server._free_port` now probes with `SO_REUSEADDR` (`_procinfo.port_free`).
Not determined: macOS (not glibc: the threshold call is a no-op there and the bound's margin on its allocator is
unmeasured until the CI's macOS entry runs).

## Fix wave 23, Sep 30 2026 — the mmap-threshold call withdrawn (N22-C-1, N22-C-2)

Wave 22's `api._fix_mmap_threshold` (and `tests/test_fix22_mmap_threshold.py`) are removed: they contradicted ADR 0002
Decision 21, which measured and rejected a fixed threshold for its CPU cost (round 22 re-measured it: 256 KiB
alloc/free ~15x slower). The fix of N21-C-1 is the 16 KiB bounded reads of `src/graceful_close.py`. With the call
removed, under three busy loops (Python 3.13.13; the 96 MiB bound unchanged): the 128-sender test alone **20x, 0
failed** (growth 82-92 MiB, median 88); `tests/test_fix8_n7_2_body_prealloc.py` as a module **10x, 0 failed**
(81-90 MiB). The worst run is 4 MiB under the bound — the margin is thin and stated. New test:
`tests/test_fix23_no_mmap_threshold.py` (3). `src/graceful_close.py`'s docstring now names the pinning test that
exists (`tests/test_live_graceful_close_module.py`; new pin in that test, identical in all ten services).
Full suite: 1034 passed, 1 skipped, **1 failed** on 3.13.13 and on 3.12.3 — the `[8-oversized]` junk-flood case sees
no 413 (every request `BrokenPipeError`); it fails the same way on 540a64e on this box (ADR 0002, "Fix wave 23").

## Running it

```bash
cd services/fulfillment-py
pip install -r requirements.txt

# Tests: do NOT pre-set FULFILLMENT_SERVICE_TOKEN — tests/conftest.py
# sets a fixed test-only token via os.environ.setdefault(...), and it
# will silently lose to any value already in the environment, breaking
# the auth tests with a token mismatch (verified).
python3 -m pytest -q                     # 213 tests (tests/conftest.py puts src/ on sys.path)

# Live service: THIS is where you set your own real shared secret.
# Leave FULFILLMENT_SIP_DIALER / FULFILLMENT_SYSTEM_OF_RECORD unset for
# the honest default; set them to "in_memory" only for a local demo.
export FULFILLMENT_SERVICE_TOKEN=<your-shared-secret>
PYTHONPATH=src python3 -m api
#   FULFILLMENT_BIND_ADDR      default 127.0.0.1 (never 0.0.0.0 by default)
#   FULFILLMENT_PORT           default 8091
#   FULFILLMENT_CONTACT_WINDOW default 08:00-21:00 recipient local time;
#                              may be narrowed, never widened; a bad value
#                              refuses startup
#   FULFILLMENT_CONTACT_MAX_ATTEMPTS_PER_24H  default 3 per phone number
#                              (and per customer); 1-3 only
#   FULFILLMENT_CONTACT_MIN_SPACING_MINUTES   default 120; 120-1440 only
#   FULFILLMENT_COUNTRY_ZONES  unset = only +1 numbers are ever contacted;
#                              e.g. "44=Europe/London"; bad value refuses startup
#   FULFILLMENT_BODY_READ_TIMEOUT_S  default 30; may only narrow (0 < s <= 30)
# Request bodies over 4 MiB are refused (413). Transport limits (fix wave 5,
# src/http_limits.py): request head <= 16 KiB, complete within 10 s; idle
# keep-alive 5 s; body complete within 30 s and never stalled for 5 s or
# under 1 KiB/s after 5 s (408, fix wave 8); a body with a Content-Length
# that cannot arrive by the 30 s deadline at its observed rate is 408 at ~5 s
# with "split the batch" (fix wave 9: a body needs >= size / 30 s, i.e. a
# 4 MiB batch >= 136.5 KiB/s; fix wave 10: rate and time both measured on
# the time the service waited on the client); a client that disconnects
# mid-body is one "client disconnected mid-body" warning line; at most 64 MiB of body bytes past each body's
# first 64 KiB buffered across all requests (503 + Retry-After: 1 beyond; the
# slowest large holder may be preempted with 408 instead — fix wave 9; bodies
# <= 64 KiB never wait for it); 503 for every new
# request (incl. /health) while >= 128 sockets are held; a connection made
# while 256 are held gets a minimal 503 and is closed (fix wave 6; see
# "Transport limits, exactly"). 422 bodies are <= 8 KiB (fix wave 6). A JSON
# body over 32 000 members / 4 000 containers / 32 levels is a 422 before it
# is parsed; bodies over 64 KiB share a 32 MiB/s budget and one parse slot
# and get 503 + Retry-After: 1 when not admitted within 2 s (fix wave 7). Run it with
# `python3 -m api` — a bare `uvicorn api:app` gets none of the parser cap or
# deadlines (only the middleware's 431/408/413). Monitor GET /gate/status
# (authenticated): alert on near_capacity, new_key_budget_exhausted or
# new_key_burst_exhausted.
curl http://127.0.0.1:8091/health
curl -H "Authorization: Bearer $FULFILLMENT_SERVICE_TOKEN" http://127.0.0.1:8091/fixtures/call-events
```

`POST /agents/callback-orchestration/run` takes
`{"tasks": [...], "phone_by_call_id": {"<call_id>": "+E164"},
"line_by_call_id": {...}, "timezone_by_call_id": {"<call_id>": "America/Chicago"}}`.
A call with no (or an invalid) time zone is **not dialed**. There is no
`now` field; the server clock is used. The time zone is a claim checked
against the number, and a number already contacted in the last 2 h (or 3
times in 24 h) is not dialed again, whatever the task id — see "Fix wave
1, Sep 24 2026 — F3".

### Live run, Sep 24 2026 (post-audit)

Real process via `PYTHONPATH=src FULFILLMENT_SERVICE_TOKEN=<random 43-char
token> FULFILLMENT_PORT=18091 python3 -m api`, probed with curl, then killed:
`/health` 200; `/fixtures/call-events` with token 200, without 401, wrong
token 401, non-ASCII token (`café`, raw UTF-8 bytes) 401; `/docs`,
`/redoc`, `/openapi.json` 404; orchestrate with `"now"` 422; bad
`resolution_type` 422; naive datetime 422; escalate a PENDING task 409;
422 body for a missing field = `{"detail":[{"loc":["body","call_events",0,"direction"],"type":"missing","msg":"Field required"}]}`
(no phone, no transcript; since fix wave 6 the body also carries `"error_count":1`); orchestrate with no time zone →
`attempted:false`, `"recipient time zone unknown or invalid — not dialed
(fail closed)"`. `/proc/net/tcp` LISTEN row: `0100007F:46AB` (127.0.0.1).
Startup without the token: exit code 1, RuntimeError. Startup with
`FULFILLMENT_CONTACT_WINDOW=06:00-23:00`: RuntimeError, refused. For
comparison, the old README command (`uvicorn api:app --port 18092`, no
`--host`) also bound `0100007F` — uvicorn's CLI default is loopback.

### Live run, fix wave 1 F3 (Sep 24 2026)

Two real `python3 -m api` processes with `FULFILLMENT_SIP_DIALER=in_memory`,
real HTTP on 127.0.0.1:19260 and :19261, then terminated. On :19260 the
test harness pinned `api._now` before `api.main()` (not a request field)
to replay the AEGIS probe: at 09:00Z (02:00 LA) `+12135550101` claimed
`UTC`, `Asia/Tokyo`, `America/Los_Angeles`, and three fresh task ids →
all `attempted:false` (`'UTC' is not a valid zone for a +1 (NANP)
number`, `outside permitted contact window … in America/New_York`); at
06:00Z a 212 number claimed `Pacific/Honolulu` → refused (New York
02:00); at 18:00Z five fresh task ids to one LA number → 1 call, 4
refused (`minimum spacing not met for this phone number`). On :19261,
fully unpatched, real clock 17:43Z: `UTC`/`Asia/Tokyo` claims, a 212
number claimed Honolulu (07:43 HST), `+44…`, and toll-free `+1800…` all
refused; four fresh task ids to one NY number → 1 call, 3 refused.

## Next steps, in priority order

1. Implement `LiveKitSipDialer(SipDialerPort)` against a real LiveKit
   SIP trunk once a phone number/carrier account exists — behind an
   explicit opt-in **and** a human approval gate (neither exists yet).
   It receives a `ContactAuthorization` and must get the number from
   `redeem(TaskChannel.CALL)` — the calling-hours and attempt-limit gate
   cannot be skipped.
2. Name the first real client's CRM/job-management system and implement
   one concrete `SystemOfRecordPort` adapter for it, with an idempotency
   key decision for `resolution_id` (Sep 24 audit, left open).
3. Store the recipient time zone per customer so callers don't have to
   supply `timezone_by_call_id` on every request; a sourced area-code →
   zone table would widen the strict continental +1 window.
4. Build the persistence/orchestration layer named in gap 3/6 — this is
   what makes the redial protection survive a restart.
5. A review by a human (or at least a non-Claude) reviewer before any
   claim stronger than CONDITIONAL is made.
