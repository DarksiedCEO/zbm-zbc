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
   is the persistence layer named in gap 3.
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
(no phone, no transcript); orchestrate with no time zone →
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
