# ADR 0002 — Fulfillment Department Architecture

**Status:** Accepted (Sep 22, 2026); amended by the Sep 24, 2026 audit (Decisions 7–10)
**Context:** Second department in the `zbm-zbc` monorepo. Built same night
as a scoped, single-pass build (basics, not a separate intelligence-layer
pass — see "Scope boundary" below), following the Revenue Recovery 1A
build discipline: real schema, real single-task agents, real tests, honest
gaps documented rather than hidden.

## Why this department, and what it competes against

Revenue Recovery 1A found money already earned but leaking out of a
client's existing systems. Fulfillment is upstream of that: it is the
mechanism that keeps a client from losing the *job* in the first place —
answering/returning the call, booking it, and confirming it actually got
done. The $1,500/mo retainer clients described Sep 20, 2026 have no
working mechanism for this today.

Competitive research done this session (voice session, Sep 22 2026)
found one company operating at real enterprise scale in this exact
space — Podium — with RingCentral as a second real heavyweight, and a
thinner field of sharper, smaller, vertical players worth stealing
mechanics from rather than competing with head-on:

- **Numa** — "resolve to completion + write back to the system of
  record" as the actual product promise, not just "we answered the
  phone." This department's `resolution_writeback` agent and the
  `ResolutionRecord` model exist specifically to make that promise
  structurally true here, not just marketing copy.
- **Beside** — a single shared customer dossier (call, text, job, and
  review history in one record) rather than siloed call logs. This
  department's `customer_dossier` agent and `CustomerDossier` model
  implement that directly.
- **Vocca** — proactive appointment-completion tracking, not just
  booking. This department's `appointment_tracking` agent implements
  that: an appointment that passed its scheduled time with no
  completion signal is a finding, not silence.

Grasshopper was evaluated and confirmed NOT a real competitor in this
category (call-forwarding/vanity-number product, no resolve-to-completion
mechanism).

## Decisions

1. **Self-hosted SIP telephony via `livekit/agents`, not a rented voice
   platform (Vapi).** Founder decision (voice session, Sep 22 2026),
   resolving a real tension against the Sep 20 2026 retainer-scoping
   note that had assumed Vapi: it conflicts with the founder's own
   standing doctrine — open source only where it can be fully owned —
   so self-hosted SIP-via-LiveKit was chosen instead. This is recorded
   as an explicit decision, not a silent substitution.
2. **Single-service build this pass**, not a 4-language stack repeat.
   Revenue Recovery 1A split detection/orchestration/ledger/dashboard
   across Python/Go/Rust/TypeScript because each language earned its
   place (statistical logic, concurrent orchestration, tamper-evident
   audit trail, live UI). Fulfillment's founder brief for tonight was
   explicit: "just the basics... versus the intelligence" — i.e. this
   pass is the domain model, the agents, and a real tested service, not
   a second full-stack orchestration/ledger/dashboard layer. Building
   that anyway, thinly, across four languages in one night would trade
   real depth for the appearance of parity with 1A. `services/fulfillment-py`
   is therefore a single FastAPI service — same internal single-task-
   agent discipline as detection-py, same fail-closed auth, same fixture-
   based real test suite — with the orchestrator/ledger/dashboard layer
   named explicitly as future work in the "Known gaps" section of the
   department README, not silently dropped.
3. **SIP/LiveKit is a defined integration seam, not a working
   integration.** This sandbox has no real phone number, no SIP trunk
   credentials, and no carrier account. `callback_orchestration.py`
   implements the real decision logic (whether, when, and via which
   configured line to call back) against an abstract `SipDialerPort`
   interface; the concrete LiveKit-backed implementation of that port
   is NOT built this pass and is documented as unverified/not-live-wired
   — exactly how the ledger's platform-agnostic seam was handled in
   Revenue Recovery 1A (ADR 0001, Decision 5).
4. **Generic system-of-record write-back seam, not a named CRM
   integration.** `resolution_writeback.py` implements the Numa-style
   completeness model — every resolved call/appointment gets a
   `ResolutionRecord`, and the agent always *attempts* a write-back —
   against an abstract `SystemOfRecordPort`, with a no-op/in-memory
   implementation for tests. No specific CRM (ServiceTitan, Jobber,
   HubSpot, etc.) is wired this pass; that is deliberately out of scope
   until a real client names which system it needs to write to.
5. **Auth built in from day 1, not bolted on after review.** Direct
   lesson from the two independent-review passes on Revenue Recovery
   1A: this service requires `FULFILLMENT_SERVICE_TOKEN` at startup and
   fails closed (refuses to start) if it isn't set, exactly matching
   detection-py's `ZBM_SERVICE_TOKEN` pattern, from the first commit —
   not discovered missing by a reviewer.
6. **Shared fixture pool**, same rationale as ADR 0001 Decision 4: call
   events, appointments, and dossiers are tested against one fixture
   pool so cross-agent behavior (a missed call becoming a follow-up
   task becoming an escalation becoming a resolution record) can be
   tested as a real sequence, not isolated units.

## Scope boundary — what "Fulfillment basics" means tonight

In scope: missed-call detection, follow-up task generation and
escalation sequencing, the callback-orchestration decision layer (seam
only, not live), the shared customer dossier, appointment-completion
tracking, and the resolution/write-back completeness model — each as a
real, independently tested single-task agent, exposed over a real
authenticated REST API.

Explicitly out of scope tonight (not silently dropped — named here as
future work): a Go orchestrator/pipeline layer equivalent to
`orchestrator-go`, a Rust tamper-evident ledger equivalent to
`ledger-rust`, a TypeScript dashboard equivalent to `dashboard-ts`, a
live LiveKit/SIP trunk connection, and any named CRM/system-of-record
integration. Also out of scope, per the founder's own instruction this
session: the ports-of-LA/Long-Beach logistics venture (parked pending
founder review) and anything Aaliyah-facing (the founder said he will
carry the SIP/LiveKit findings to her himself).

## Audit, Sep 24 2026 — decisions it adds

Full findings table with evidence and test names:
`services/fulfillment-py/README.md`, "Audit, Sep 24 2026". Summary of
what changed architecturally, and why:

7. **Outbound-contact quiet hours are a hard rule in code, evaluated in
   the recipient's local time, and fail closed.** Before the audit the
   only check was `8 <= now.hour < 20` on a UTC clock (a Los Angeles
   caller could be called back at 02:00 local — reproduced), and the API
   accepted a caller-supplied `now` that could bypass even that. Now
   `src/contact_window.py` gates every dial on 08:00–21:00 in the
   recipient's IANA zone (DST via zoneinfo), refuses to dial when the
   zone is unknown or invalid, uses only the server clock, and lets
   configuration (`FULFILLMENT_CONTACT_WINDOW`) narrow the window but
   never widen it. This replaces the old "business hours" rule rather
   than sitting next to it: a per-line business-hours policy would need
   per-line time zones, which still don't exist. Consequence: until a
   recipient time zone is stored per customer, callers must pass
   `timezone_by_call_id` or nothing is dialed. Any future SMS/email
   sender must use the same gate — none exists today.
8. **Duplicate suppression lives at the only place contact happens
   (the dial), in process memory, under a lock.** Duplicate call events,
   duplicate task_ids in a batch, the same task across requests, and
   concurrent requests are all dialed at most once, and a dialer
   exception becomes a FAILED outcome instead of a 500 that hid earlier
   dials. This does not survive a restart or span replicas; Decision 2's
   deferred persistence layer is still the durable fix.
9. **The service owns its bind address**: `python3 -m api` binds
   `FULFILLMENT_BIND_ADDR` (default `127.0.0.1`), matching
   `LEDGER_BIND_ADDR` / `ORCHESTRATOR_BIND_ADDR`, verified from
   `/proc/net/tcp` by a test that spawns the real process.
10. **Input is bounded and typed at the edge**: timezone-aware datetimes
    only, E.164 phones, bounded ids/text/batches, enums for
    `resolution_type`/`entity_type`, unknown request fields rejected,
    and 422 bodies carry no request payload (they previously echoed
    phone numbers and voicemail transcripts). Money follows the
    monorepo wire contract (Decimal, ROUND_HALF_UP, two-decimal string).

Still open after the audit (not decided here): an approval gate for a
future real dialer/CRM adapter; idempotency keys for
`resolution-writeback/resolve` across retries; per-customer time zone
storage; bounded in-memory state.

## Verified so far (Sep 22, 2026 build session)

See `services/fulfillment-py/README.md` for the actual test count, what
was run, and the full honest-gaps list — this ADR records the
architecture decisions, not the test results, so it doesn't go stale
independently of the code.
