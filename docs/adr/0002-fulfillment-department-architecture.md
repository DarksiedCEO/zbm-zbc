# ADR 0002 — Fulfillment Department Architecture

**Status:** Accepted (Sep 22, 2026); amended by the Sep 24, 2026 audit (Decisions 7–10), fix wave 1 (Decisions 11–14) and fix wave 4 (Decisions 15–16)
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
   sender must use the same gate — none exists today. *(Superseded in
   part by Decision 11: the supplied zone was trusted as-is, which AEGIS
   showed was a bypass.)*
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

## Fix wave 1, Sep 24 2026 — decisions it adds

11. **Every automated outbound contact is authorized by one gate
    (`src/outbound_gate.py`), and the caller decides neither the
    recipient's zone nor the redial key.** AEGIS (F3, High) reproduced
    calls to a Los Angeles number at 02:00 local because the caller
    claimed `UTC`/`Asia/Tokyo`, and five calls to one number via fresh
    task ids. Decided:
    - *Zone from the number, claim only narrows.* `+1` numbers must be
      valid geographic NANP numbers and the claimed zone must be on an
      explicit NANP allowlist; the window (Decision 7) must hold in every
      zone plausible for the number plus the claimed zone. Hawaii, Alaska
      and the US territories are mapped by area code; every other area
      code is treated as possibly anywhere in continental US/Canada.
      Rejected: a full area-code → zone table (several area codes span
      two zones, overlays and porting make rows silently wrong in the
      dangerous direction) and a new phone-metadata dependency. Cost: a
      continental `+1` day of 08:00–16:30 Pacific. Other country codes
      fail closed unless a zone set is configured for the code.
    - *Limits per phone number and per customer, across channels*: 3 per
      rolling 24 h, 2 h apart by default, configuration can only narrow.
      Consequence: the escalation SMS 10 minutes after a failed call
      (sequencing policy) now waits for the spacing.
    - *Enforced at the transport seam.* `SipDialerPort` and the new
      `MessageSenderPort` take a single-use, channel-bound
      `ContactAuthorization` instead of a number; its `redeem()` re-checks
      the window and limits on the gate's clock at the moment of contact
      and records the attempt atomically. This also closes a stale-clock
      gap (one `now` per request reused across a long batch). Task
      *creation* stays ungated: creating a task is not contact.
    - Same in-memory limitation as Decision 8.

12. **Money: parse-from-wire and construct-from-Decimal are separate
    paths (fix wave 1, F15).** `LabeledValue.amount_usd` still ran any
    string through `Decimal()` and rounded it (`"1e3"`, `" 12.30 "`,
    `"012.30"` accepted; `"12.345"` → `"12.35"`). It is now
    `PositiveMoney` from `src/fulfillment_schema/money.py`, a copy (not an
    import — services do not import each other) of detection-py's
    `zbm_schema/money.py`: a string must match
    `^(0|[1-9][0-9]{0,14})\.[0-9]{2}$` (ADR 0003 section 1a) and is never
    rounded; a JSON number is refused when parsed from JSON text; a
    computed `Decimal`/`int` (float only via `str()`) is quantized half-up
    under an explicit `MONEY_CONTEXT`, never the ambient context. Held to
    `fixtures/money_vectors.json`, both columns. No route accepts money in
    a request body today (a test walks every route's body model and fails
    if one ever does, so the HTTP-level vector run gets added with it).
13. **Every datetime is bounded to [2000-01-01, 2100-01-01) UTC (fix wave
    1, fuzz sweep).** `started_at = 9999-12-31T23:59:59Z` made
    missed-call detection overflow computing `started_at + 5 min` → 500.
    Bounding the input removes the whole class for every agent's
    timedelta arithmetic. `FollowUpTask.due_at`, the one field an agent
    derives from an input timestamp, gets one extra day of headroom so a
    valid late-2099 event cannot fail validation inside the agent.
14. **In-memory state is bounded and fails closed at its cap (fix wave
    1).** The gate's attempt history evicts attempts older than 24 h (a
    full sweep whenever the gate clock has moved a minute, or at the
    cap) and holds at most 100 000 numbers+customers; the dial dedupe
    and exhausted-escalation dedupe stores (`src/bounded_state.py`)
    evict after 24 h and hold at most 100 000 entries each; the dossier
    store holds at most 100 000 customers and 10 000 entries per history
    list and never evicts (it is a record). At a cap with nothing
    expired, the service refuses — no contact (gate refusal / skip
    reason), no write-back (503), no dossier update (503) — because
    dropping live history would silently re-open the redial limit or
    duplicate a record. Rejected: LRU eviction of live entries (same
    reason). Forgetting a task id after 24 h is accepted: the per-number
    gate limit is the real redial protection beyond that.

## Fix wave 4, Sep 24 2026 — decisions it adds

15. **The gate's tracked-number cap is protected by a new-key admission
    budget, never by relaxing the fail-closed rule.** AEGIS round 3
    (PLAUSIBLE/low): a holder of the service token could fill the
    100 000-key attempt history (Decision 14) with contacts to fresh
    numbers/customers; from then on every NEW number is refused for up to
    24 h. Refusing is correct (Decision 14) — the problem is how cheaply
    the cap could be reached (a burst of ~50 000 contacts). Decided:
    - *Rolling-hour budget for new keys*: at most
      `max_new_keys_per_hour` numbers+customers not already in the history
      may be admitted in any rolling hour, checked at `authorize()` and
      again at `redeem()`. Default `100 000 // 24 = 4 166`, so 24 h of
      admissions (99 984) cannot fill the cap: filling it now takes more
      than a day of sustained real contacts that are also kept alive by
      re-contacting, instead of a burst. Numbers already being contacted
      are unaffected — their own limits still decide. Cost: at most ~2 083
      new customers (number + customer key) per hour; a legitimate burst
      above that is refused (task stays PENDING, reason says retry later),
      which is the cap's own sustainable rate anyway.
    - *Capacity is visible*: `OutboundContactGate.status()` — served at
      authenticated `GET /gate/status` and as `gate` in every
      callback-orchestration response — reports utilization,
      `near_capacity` (≥ 80 %), `at_capacity`, and new keys used/allowed
      this hour; the API logs a warning when near capacity or out of
      budget. At the default budget the 80 % mark is reached no sooner
      than ~19 h into a fill, so an alert on it gives hours of warning.
    - *Rejected*: evicting live history or allowing contact without a
      record at the cap (re-opens the per-number limit — never); counting
      only numbers from "known call events" (the service stores no call
      events, and the same token holder can submit call events, so
      "known" would be attacker-controlled and protect nothing); a
      per-request cap on new numbers (defeated by sending more requests —
      the time budget bounds the total regardless of request count).
    - Not solved: a token holder can still make the service contact real
      numbers — that is the token's authority; this decision bounds the
      collateral denial of service, not that.
16. **Request bodies are bounded, and parsed off the event loop.** No
    route had a body limit, and FastAPI read, json-decoded and validated
    every body on the event loop before the (thread-pool) handler ran: a
    64 MiB body stalled `/health` for 2.9–3.3 s for every client. Now: a
    4 MiB limit (413 from `Content-Length` before any body byte is read,
    and while streaming for chunked bodies), sized so the worst-case
    1000-item batch of every route fits (largest: orchestrate, 3.57 MB
    with every field at its maximum); each route awaits only the body
    bytes and does parse + validate + agent work + response rendering in
    one thread-pool call; `/health` is a coroutine. Auth now runs before
    the body is parsed.

## Fix wave 5, Sep 24 2026 — decisions it adds

17. **The entrypoint owns its transport limits** (NEW-3, MED, CONFIRMED).
    `python3 -m api` ran uvicorn's defaults: the httptools parser (no
    request-head size limit — one 100–200 MB header was buffered in full,
    RSS 53 → 415 MB) and no head or body deadline (idle, partial-head and
    slow-body sockets held indefinitely), all before auth. Decided, same
    approach as detection-py's `serve.py`, values in
    `src/http_limits.py`: h11 parser with a 16 KiB head cap (400 while the
    head is being read; the middleware re-checks with 431 under any other
    launcher); a 10 s request-head deadline from connect / end of the
    previous response; 5 s idle keep-alive; a 30 s body deadline (408 from
    the middleware when the app is reading; the protocol closes the
    connection 5 s later regardless, e.g. after an early 401 — each
    trickled byte used to reset uvicorn's keep-alive timer);
    `limit_concurrency` 128 (bounds in-flight 4 MiB bodies to ~512 MiB) and
    a hard cap of 256 open sockets. `FULFILLMENT_BODY_READ_TIMEOUT_S` may
    only narrow the body deadline. *Trade-off, accepted:* ≥ 128 held
    sockets make the service answer 503 (including `/health`) until they
    are closed — at most 10 s for sockets that never send a head — instead
    of letting one client hold every file descriptor and unbounded memory
    indefinitely. *Not addressed:* a client that never reads its response
    (bounded by the socket send buffer and the 4 MiB response size, not
    by a deadline).
18. **New-key admission is burst-shaped, not only hourly** (AEGIS NEW-5,
    LOW, design trade-off). Decision 15's rolling-hour budget could be
    spent in one burst: 4 166 fresh numbers in 0.34 s blocked every new
    legitimate number for 60 minutes. The budget cannot be scoped per
    caller — there is one shared service token and no caller identity.
    Decided: new keys must *also* pass a token bucket of capacity 100
    (`new_key_burst`, default `min(100, max_new_keys_per_hour)`), refilled
    at `max_new_keys_per_hour` per hour, i.e. 1/60 of the hourly budget
    per minute (69.4/min); checked at `authorize()` and `redeem()`, spent
    at `redeem()`; a gate clock that moves backwards refills nothing. The
    rolling-hour budget (Decision 15) still applies on top, so nothing is
    admitted that it refuses — fail closed is unchanged.
    **New worst case:** a burst takes at most 100 new keys (50 new
    customers) at once, and a legitimate new number is refused for about
    1–2 s after the burst ends (live run: 5 000 fresh numbers in 0.24 s →
    50 contacted; a legitimate new number refused immediately, contacted
    2.5 s later). In any one minute at most 100 + 69.4 new keys are
    admitted (tokens that accrue while the hourly budget is the one
    refusing form the next burst). An attacker who *keeps* offering fresh
    numbers still competes with legitimate new numbers for every token,
    for as long as the attack lasts, up to the hourly budget — visible via
    `new_key_burst_exhausted` in `GET /gate/status` and the logged warning;
    fixing that needs per-caller identity (a per-caller token), which this
    service does not have. *Cost:* a legitimate orchestrate batch with more
    than 50 new customers gets the excess refused with "retry in a few
    seconds" (tasks stay PENDING); sustained, ~34 new customers per minute.

## Fix wave 6, Sep 24 2026 — decisions it adds

19. **Validation errors are reported bounded, never enumerated** (AEGIS N2,
    MED, CONFIRMED). The 422 handler had stopped echoing `input` (A8) but
    still listed every error: an 868 KB body of 60 000 unknown keys against
    an `extra="forbid"` request model became 60 000 `extra_forbidden`
    errors, a 5.4 MB response built on the event loop; a 1 MiB unknown key
    came back whole in its `loc`. 20 concurrent senders: RSS 821 MB and
    `/health` p50 838 ms (live); 3.9 MB bodies: 23.5 MB per 422, RSS 4.4 GB.
    Decided: a 422 body is bounded whatever the request was — the first 20
    errors plus the honest `error_count` (and `truncated: true`), `loc`
    cut to 8 items of 40 chars, `type`/`msg` bounded, the serialized body
    kept ≤ 8 KiB — and the causes of enumeration are refused before they
    are enumerated: a request object with more than 20 unknown keys is one
    `too_many_fields` error before any field is looked at (up to 20 unknown
    keys are still named, so Decision 10's "unknown request fields
    rejected" still tells a stale client that `now` is unknown), and a map field over its 1 000-entry cap is `too_long`
    before its entries are validated (pydantic validates every dict entry
    first; a list's length first). Parsing and the 422 render happen in the
    worker thread; bodies are parsed one at a time — a parse holds the GIL
    in Rust for its whole duration, so a second concurrent parse adds
    memory (a 4 MiB body of ~260k keys is ~45 MB as Python objects) and
    loop latency, not throughput; the agent work is not behind that slot.
    `python3 -m api` limits glibc to one malloc arena: per-thread arenas
    kept most of a parse's freed memory (+102 MB retained after 20
    concurrent 60k-key bodies with default arenas, +24 MB with one). Live
    after: 139-byte 422s, peak RSS 89 MB (idle 49), `/health` max 194 ms;
    with 3.9 MB bodies peak 224 MB, `/health` p50 78 ms, max 424 ms.
    *Cost, accepted:* a caller with more than 20 errors sees only the first
    20 and a count, and must fix and resend to see the rest; the worst-case
    junk body (4 MiB of unknown keys) still costs ~0.2–0.4 s of CPU per
    request, bounded by the body limit, and queues other bodies behind it.
20. **Over-cap connections are answered, not aborted** (AEGIS N7, INFO).
    Decision 17's hard cap aborted the 257th connection with no response
    (curl 000). Decided: it is written a minimal `503 Service Unavailable`
    (`Connection: close`, `Retry-After: 1`) and closed once its request
    bytes arrive or after 1 s, and is never counted as held. The exact
    behavior, now also stated in the README: below 128 open connections,
    normal service; with 128–255 sockets held, uvicorn answers every new
    request (`/health` included) 503 for as long as they are held — ≤ 10 s
    for sockets that never send a head, ≤ 5 s idle keep-alives, ≤ 35 s
    unfinished bodies, indefinitely for a client that keeps sending complete
    requests on 128 sockets; with 256 held, a new connection gets the
    minimal 503 and is closed. *Not addressed:* the 128–255 window itself,
    which needs per-client identity or a reserved health listener.

Still open after the audit (not decided here): an approval gate for a
future real dialer/CRM adapter; idempotency keys for
`resolution-writeback/resolve` across retries; per-customer time zone
storage. (Bounded in-memory state: decided in Decision 14.)

## Verified so far (Sep 22, 2026 build session)

See `services/fulfillment-py/README.md` for the actual test count, what
was run, and the full honest-gaps list — this ADR records the
architecture decisions, not the test results, so it doesn't go stale
independently of the code.
