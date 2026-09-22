# ZBM Fulfillment Department (services/fulfillment-py)

**Status: CONDITIONAL, not CERTIFIED.** One independent review pass has
now happened (Sep 22, 2026) and found real, CONFIRMED defects — ten of
them, all reproduced independently before being fixed, listed below.
This is stronger evidence than the self-built version had, but it is
still one review, from one reviewer, on one day. Treat every claim below
as what was actually run, not as a guarantee.

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
5. **Business-hours check assumes UTC-normalized timestamps, no per-line
   timezone.** Confirmed real by review (finding #6) — unchanged this
   pass.
6. **In-memory state only, and callback_orchestration's status-advance
   fix depends on the caller persisting it.** `api.py` keeps dossiers,
   dial attempts, and write-backs in process memory. Finding #2's fix
   makes the *correct next state* available on every outcome, but
   nothing enforces that a caller actually saves it — that requires the
   persistence layer named in gap 3.
7. **No real client connected.** Every endpoint takes request-supplied
   data or serves from `fixtures/fulfillment_*.json`.
8. **Single shared-secret bearer token, not a real auth system.**
   Adequate for a private network, not for anything internet-facing.
9. **The same non-ASCII-token-500 and unauthenticated-docs bugs almost
   certainly still exist in `services/detection-py`** (Revenue
   Recovery). Both were flagged in last night's second independent
   review of that service and left unfixed pending founder review; both
   are now confirmed-and-fixed patterns here. Porting the fix there is
   a five-minute follow-up, not yet done.
10. **This is one review pass.** A second, independent reviewer looking
    at the same code might find different things, or disagree with how
    some of the above were fixed (particularly #2/#3, where the
    reviewer's own suggested fix was itself wrong and had to be
    corrected here — see the table above).

## Running it

```bash
cd services/fulfillment-py
pip install -r requirements.txt

# Tests: do NOT pre-set FULFILLMENT_SERVICE_TOKEN — tests/conftest.py
# sets a fixed test-only token via os.environ.setdefault(...), and it
# will silently lose to any value already in the environment, breaking
# the auth tests with a token mismatch (verified).
PYTHONPATH=src pytest tests/ -v          # 64 tests

# Live service: THIS is where you set your own real shared secret.
# Leave FULFILLMENT_SIP_DIALER / FULFILLMENT_SYSTEM_OF_RECORD unset for
# the honest default; set them to "in_memory" only for a local demo.
export FULFILLMENT_SERVICE_TOKEN=<your-shared-secret>
PYTHONPATH=src uvicorn api:app --reload --port 8091
curl -H "Authorization: Bearer $FULFILLMENT_SERVICE_TOKEN" http://localhost:8091/health
```

## Next steps, in priority order

1. Implement `LiveKitSipDialer(SipDialerPort)` against a real LiveKit
   SIP trunk once a phone number/carrier account exists.
2. Name the first real client's CRM/job-management system and implement
   one concrete `SystemOfRecordPort` adapter for it.
3. Port the non-ASCII-token-500 fix and the disabled-docs fix to
   `services/detection-py` — both bugs are now confirmed patterns, not
   hypothetical ones.
4. Build the persistence/orchestration layer named in gap 3/6 — this is
   what would let callback_orchestration's status-advance fix (finding
   #2) actually prevent a repeat dial, instead of just making the
   correct next state available.
5. A second independent review pass, ideally from a different reviewer
   than the first, before any claim stronger than CONDITIONAL is made.
