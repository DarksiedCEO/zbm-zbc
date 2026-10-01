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
    `limit_concurrency` 128 (bounds in-flight 4 MiB bodies to 128; their
    buffered bytes are bounded to 64 MiB by Decision 22) and
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
    20 and a count, and must fix and resend to see the rest. *Superseded in
    part by Decision 21:* the worst-case junk body no longer costs a full
    parse (the shape pre-scan refuses it in ~4 ms) and no longer queues
    small bodies behind it.
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

## Fix wave 7, Sep 24 2026 — decision it adds

21. **Request parsing is admitted by size, and large bodies by a byte
    budget** (AEGIS NEW-4, MED, CONFIRMED). Decision 19's single parse slot
    was FIFO: a 1.7 KB legitimate `detect` queued behind every 3.4 MiB junk
    body ahead of it, and each junk body cost ~140 ms of full parse (jiter
    materializes 255 000 keys as Python objects) before pydantic could refuse
    it — 8 junk senders took legit p50 from 4 ms to 1.1 s (2.4 s on the fix
    box), 32 to 2.9–4.4 s. Decided, three parts:
    - *Shape pre-scan before the parse* (`api._json_shape`): a JSON body
      with more than 32 000 members (object keys + array items), more than
      4 000 objects/arrays, or nested deeper than 32 levels is refused with
      one bounded 422 (`json_too_many_members` / `json_too_many_containers`
      / `json_too_deep`) before the full parse. The scan is byte-level and
      runs in C passes (translate, replace, count, split, join), never a
      per-token Python loop over the body: strings are removed exactly
      (escapes handled by deleting `\\` pairs and then `\"`), so nothing
      inside a string counts, and a legitimate body is never refused — the
      caps are 32× / 4× the batch contract and every route's maximum batch
      uses well under two thirds of each. Measured: 1–52 ms on every 4 MiB
      shape tried, 3–11 ms for the AEGIS bodies; what passes the pre-scan
      parses in ≤ 32 ms (bounded by the caps and the 4 MiB limit).
    - *Two lanes*: bodies ≤ 64 KiB parse in their own lane (4 slots) and
      never wait behind a large body; bodies over 64 KiB draw, in arrival
      order (an `asyncio.Lock` is FIFO), on one token bucket of 32 MiB/s
      (burst 16 MiB) — a request whose Content-Length says it is large
      takes its whole size *before its body is read*; a chunked body pays
      per 64 KiB as it streams — and then parse one at a time. A large
      request not admitted and parsed within 2 s of arriving is answered
      503 + `Retry-After: 1`; uvicorn drains its unread body at the HTTP
      parser (~2.5 ms of loop time per 3.4 MiB, versus ~10–13 ms to receive
      one through the app) and the connection stays usable.
    - *Head-only refusals are held 250 ms* (413 from Content-Length, 431,
      400 bad Content-Length): they cost ~1 ms each and nothing else, so a
      client that loops on them got ~400 attempts/s through (AEGIS `objs`,
      4.39 MB, 8 senders: legit p50 4 → 45 ms). The hold caps such a client
      at 4 attempts/s per connection.

    *Why a byte rate and not only a slot:* receiving a large body costs the
    event loop ~10–13 ms of its own time whatever the body contains, and a
    sender that loops on the response sends as fast as the service answers
    — a cheaper refusal alone only raised the attempt rate (an immediate 503
    for the 9th concurrent large body took 32 senders from 40 to 230
    attempts/s and legit p50 from 36 to 344 ms). The budget caps the loop
    time spent receiving large bodies at ~13% whatever the number of
    senders; 32 MiB/s is 9 maximum 1000-task batches per second, far above
    what the outbound gate lets the service act on.

    **The fairness guarantee, exactly:** a small body waits for at most 4
    small parses ahead of it and never for a large one; the loop spends at
    most the budget's share of its time receiving large bodies. **Its
    limits:** (a) the loop and the GIL are still shared — one pre-scan or
    one bounded parse (tens of ms) or the reading of one large body in
    progress can delay a small request, not the queue of them; (b) there
    is no caller identity (one shared service token), so a legitimate large
    batch competes FIFO with junk for the budget and is 503'd like any
    other when more than ~2 s of large bodies (~19 maximum-size ones at
    32 MiB/s) are queued ahead of it — it succeeds on a retry when its turn
    comes (live: 1–2 attempts under 32 senders), and a client that keeps
    sending large bodies keeps everyone's large batches waiting for as long
    as it does; (c) a flood of *small* junk (≤ 64 KiB each) competes fairly
    in the small lane, bounded per attempt by size and the pre-scan, but
    has no budget and can still load the loop; (d) at ≥ 128 held sockets
    uvicorn's concurrency limit answers everything 503 (Decision 17), before
    any of this. Live after (`ful_slot6.py`, 10 s): 8 senders — legit p50
    4 ms, p90 9 ms; 32 — p50 4 ms, max 58 ms, `/health` p50 2 ms; 120 —
    every request 503 (the Decision 17 limit).

    *Idle memory, measured:* the README's "RSS settles at 93 MB" was never
    verified; after 32 senders × 10 s RSS stayed at 170 MB for 15 s (50 MB
    before). glibc's dynamic mmap threshold moves every body buffer after
    the first into the brk heap, where freed blocks are kept for reuse. A
    fixed 128 KiB threshold would return them but made every large body
    ~50% more CPU in page faults (19 vs 13 ms per 3.4 MiB); decided
    instead: one second after the last large parse, with none in flight,
    `malloc_trim(0)` returns the free pages (~15 ms for 130 MB, 0 ms when
    idle). Measured: idle 50 → 52–58 MB after every flood above, peak
    64–111 MB during them.

## Fix wave 8, Sep 24 2026 — decision it adds

22. **Request bodies are buffered only as they arrive, under one in-flight
    byte budget and a minimum-throughput rule** (AEGIS N7-2, MED-HIGH,
    CONFIRMED — a Decision 21 regression). Fix wave 7 pre-sized the body
    buffer from Content-Length (`bytearray(declared)`, a 4 MiB memset per
    request before any body byte), so 128 connections sending a head and
    one byte pinned 512 MiB (RSS 55 → 569 MB) for the 30 s body deadline at
    no bandwidth. Decided, three parts:
    - *Nothing is allocated ahead of the bytes received.* The buffer grows
      with the chunks (bytearray's amortized growth; grown pages are not
      touched until bytes land in them, so resident memory tracks bytes
      received). Measured 0.13 ms per 3.4 MiB in 64 KiB chunks against
      0.32 ms for the pre-sized, memset buffer — the "realloc cost" fix
      wave 7 avoided was not real. Rule, for every allocation in this
      service: sized from bytes received, never from a client-declared
      size (heads are capped by the parser before they are held; the
      query string is part of the head; there is no multipart).
    - *One in-flight byte budget* (`api._INFLIGHT_BODY_BYTES`, 64 MiB —
      the large lane's 2 s of budget at 32 MiB/s, the most that can be
      admitted for parsing within its wait anyway; before, the only bound
      was 128 × 4 MiB). Every chunk is reserved before it is buffered; a
      request that cannot buffer its next chunk within 2 s is answered 503
      + `Retry-After: 1` (uvicorn drains the rest at the parser); the
      reservation is released when the body has been parsed or the request
      ends. What is outside it: uvicorn's own ≤ 64 KiB per-connection
      buffer (≤ 256 connections).
    - *Minimum throughput* (`http_limits.BODY_MIN_BYTES_PER_S` 1 KiB/s,
      `BODY_MIN_RATE_GRACE_S` 5 s): a body that sends nothing for 5 s of
      waiting (a stall — front-loading 3.9 MB earns no credit) or has
      averaged under 1 KiB/s after 5 s of waiting (a trickle) is 408 by
      the middleware. The clock is time spent *waiting for the client*, so
      the service's own budget wait is never charged to the client. The
      protocol (`DeadlineH11Protocol`) applies the same rule to a body the
      app is not reading (after an early 401), judged
      `BODY_DEADLINE_GRACE_S` (5 s) later so an app-side 408 is written
      before the socket is closed; the 30 s + 5 s deadline remains the hard
      bound.

    *Measured after* (`ful_idle7b.py`, 40 s, 320 connections): RSS 50 →
    peak 52 MB, `/health` 200 in 0.10 s during the hold. 128 senders of
    3.9 MB then a stall: peak RSS 138–142 MB (base 54), 20 admitted bodies
    408'd at ~5 s, 108 refused 503, RSS back near base by ~10 s. Decision
    21's fairness re-checked (`ful_lanes7.py`): legit small p50 3–5 ms and
    `/health` p50 2 ms under 8 and 32 junk senders; the legit large batch
    under 32 continuous senders remains limit (b) of Decision 21 (15/33
    attempts over ten runs, 16/27 on the pre-fix tree; every refusal the
    large lane's budget, none the new one).

23. **Small bodies have their own in-flight reserve; slow large holders
    are preempted under contention; a body must be able to arrive by the
    deadline** (fix wave 9; AEGIS round 8 questions Q1/Q2, assessed with real
    sockets). Measured before: senders that declared 4 MiB, front-loaded
    1 MiB and then sent 2 KiB/s (above the 1 KiB/s floor) held the whole
    64 MiB budget to the 30 s deadline — legit 200-byte `detect` p99 1.94 s
    (N = 64), legit max batches 1/6; plain 2 KiB/s senders (N = 16, 64)
    starved nobody; N = 128 starved everyone through uvicorn's connection
    limit, not the budget. A max 4 MiB body at 64 KiB/s needs 64 s and was
    cut at 30 s; and every large body taking > 2 s to upload was 503'd after
    arriving (the large lane's admission window ran from arrival). Decided:
    - *Small reserve.* Each body's first `_SMALL_BODY_BYTES` (64 KiB) is
      reserved from `_SMALL_RESERVE_BYTES` = `LIMIT_CONCURRENCY` × 64 KiB,
      which the real launcher cannot exhaust; only bytes past 64 KiB draw
      on the shared 64 MiB. A small body never waits for bytes.
    - *Time-weighted charge, preemption.* Each body is charged shared bytes
      held × seconds the service spent waiting on its client (its own waits
      are not charged). A body that cannot reserve its next chunk preempts
      (408) the in-flight body with the largest charge if it is ≥
      `_PREEMPT_BYTE_SECONDS` (4 MiB·s) and above its own. Holding the
      budget against newcomers then costs ~1 024 / N MiB/s of real upload
      bandwidth (N holders), not time; a body at ≥ 2 MiB/s is never
      preempted; a legit slow large body is, under contention.
    - *Arrival projection; the deadline is not raised.* From the 5 s grace,
      a declared body whose remainder cannot arrive by the deadline at its
      observed rate is 408 at once, with a split hint. Rejected: raising the
      deadline to size / 128 KiB/s for large bodies — it would lengthen how
      long every slow sender holds memory. So a body needs ≥ size / 30 s (a
      4 MiB batch ≥ 136.5 KiB/s); slow clients split.
    - *Admission windows run from when each wait starts* (parse slot: body
      completion; chunked takes: each take), and declared bytes that never
      arrive are refunded to the 32 MiB/s large-lane budget.
    Not decided here: connection slots — 128 held connections still make
    uvicorn 503 everyone (Decision 17's trade-off); it needs per-client
    identity. Numbers and tests: README, "Fix wave 9".

Still open after the audit (not decided here): an approval gate for a
future real dialer/CRM adapter; idempotency keys for
`resolution-writeback/resolve` across retries; per-customer time zone
storage. (Bounded in-memory state: decided in Decision 14.)

## Fix wave 21, Sep 28 2026 — a test amendment AND a product change

(Corrected in fix wave 22, lead ruling G8; AEGIS round 21 N21-C-4. This heading used to say "test amendment (no
product change)". Wave 21 did change the product: `src/http_limits.py`'s protocol gained the graceful close
(lead ruling L1, ADR 0003 §8) — FIN after an answer, then a bounded drain of the client's remaining bytes (64 KiB /
1 s) — and the 128-sender test's CLIENT changed with it: it now sends while it reads, stops at the answer and
reads it to Content-Length (`_send_reading`), where it used to write 3.9 MB with a blocking `sendall` before
reading anything. Round 21 ruled that client legitimate (not a fake green) and showed that the stale 0/10
evidence of wave 21 predated the change; the numbers below it are from before either change. The product change
and its bounds are in ADR 0003 §8/§9; the blocking client's residual is pinned by
`tests/test_fix22_drain_residual.py` — see "Fix wave 22" below.)

AEGIS round 20 N20-M-4 / N20-M-5, `tests/test_fix8_n7_2_body_prealloc.py::
test_live_128_senders_of_3_9mb_that_then_stall_are_bounded_by_the_inflight_budget_and_cut`:

- **N20-M-5 (test defect, E3).** The test sampled RSS only while its
  sender threads were alive; under load every sender can have its answer
  (408/503) before the server has released the bytes, so no settled
  sample existed (`settled_at None`, 1/10 on a busy box). Sampling now
  runs until RSS settles (a sample after 2 s within 24 MiB of the
  baseline) or the test's own bound elapses
  (`BODY_MIN_RATE_GRACE_S + BODY_DEADLINE_GRACE_S + 3` = 13 s),
  whatever the senders are doing. The full line (codes, base, peak,
  growth, `settled_at`, bound, every sample) is printed and flushed
  before any assertion and is every assertion's message, so a failing
  run on another OS (the Mac) yields its evidence.
- **N20-M-4 (limit untouched).** The growth bound stays
  `_INFLIGHT_BODY_BYTES // MiB + 32` = 96 MiB. It rests on one Linux
  measurement (+84 MiB) plus 12 MiB; Linux growth measured 84–91 MiB.
  Whether macOS's allocator stays under it is UNDETERMINED (the relayed
  Mac evidence is E0); the macOS CI entry added in this wave
  (`python-tests (fulfillment-py, 3.13, macos-14)`) is where that is
  decided. Not raised to make a test pass.

## Fix wave 22, Sep 28 2026 — what the reading client added, and the peak's variance (product changes)

AEGIS round 21 N21-C-1 (lead ruling G5; the 96 MiB bound is NOT raised): the 128-sender test failed under load
(2/10 module, 5/20 single) and the reviewer traced it to the wave-21 reading client. The lead's hypothesis — 128
concurrent drains each allocating a read buffer of up to 64 KiB, ~8 MiB — was checked and is NOT what the
measurements show:

- *tracemalloc at the server's traced peak* (w22 `ful_attr.py`: the server under `-X tracemalloc`, the test's own
  reading client, 128 senders): 68.4 MiB in the app's body buffers (the 64 MiB in-flight budget, bytearray
  over-allocation included), 15.6 MiB in uvicorn's `cycle.body` across ~100 connections (~150 KiB each: uvicorn
  buffers a request's body up to its 64 KiB high-water mark PLUS the read that crossed it — the event loop reads up
  to 256 KiB — and ~100 of the 128 are answered 503 before their app ever runs), 7.7 MiB of receive-path copies of
  those buffers. No drain allocation appears: drained bytes were never retained. What the reading client adds is
  bigger reads (it sends 64 KiB at a time with the default send buffer, where the old client's `sendall` was
  throttled by a 64 KiB `SO_SNDBUF`), so more unread body per connection before backpressure — and, with the
  wave-21 drain, that buffered body stayed referenced until the drain ended (up to 1 s).
- *The peak's variance* (RSS A/B under 3 busy loops, 8 runs each, the test's client): wave-21 code 87-92 MiB
  growth (mean 89.4), with bounded reads (below) 80-89 (83.8), with bounded reads and a fixed mmap threshold
  80-82 (81.0). The tail that crossed 96 was glibc's DYNAMIC mmap threshold: once a large body buffer is freed the
  threshold rises to its size, later buffers come from the heap, and freed heap blocks below the top stay resident
  (`tests/test_fix22_mmap_threshold.py`: 95 freed 512 KiB blocks under a survivor — 47 MiB resident with the
  dynamic threshold, 0 with it fixed).

Decided (product):
- the transport (ADR 0003 §9, `src/graceful_close.py`, shared by the ten Python services): every read goes through
  one 16 KiB buffer, so a connection's unread body is bounded by uvicorn's 64 KiB mark + 16 KiB; drained bytes are
  discarded in that buffer; the answered request's body is released when the drain starts; the concurrency slot is
  released before the drain; at most `FULFILLMENT_DRAINS_MAX` (512) drain at once;
- ~~`python3 -m api` fixes `M_MMAP_THRESHOLD` at 128 KiB~~ — WITHDRAWN in fix wave 23 (see below): it contradicted
  Decision 21, which stands as written.

Kept: the reading client (ruled legitimate in round 21) and the 96 MiB bound. The blocking client's residual —
a 3.9 MB blocking `sendall` answered 503 early is reset once the bounded drain ends and never reads the answer — is
the design and is pinned by `tests/test_fix22_drain_residual.py` (with the reading client's clean 503 beside it).

## Fix wave 23, Sep 30 2026 — the mmap-threshold call withdrawn; Decision 21 stands (AEGIS round 22 N22-C-1/C-2)

Decision 21 (fix wave 7) measured a fixed 128 KiB mmap threshold and REJECTED it: every large body ~50% more CPU in
page faults (19 vs 13 ms per 3.4 MiB); it kept glibc's dynamic threshold and returns freed heap pages with
`malloc_trim(0)` one idle second after the last large parse. Wave 22 fixed the threshold anyway
(`api._fix_mmap_threshold`, 1391b5c) to narrow the 128-sender test's peak variance — a product change bought to
steady a test, and one Decision 21 had already priced: round 22 re-measured it (alloc/free of 256 KiB blocks ~15x
slower, a growing 4 MiB bytearray ~5x, a mixed 200 KiB/1 KiB churn ~6-7x; the reviewers' `mmap_cost.log`). The call, its constant and
`tests/test_fix22_mmap_threshold.py` (with its musl/stub-libc question, N22-C-2) are removed; `src/api.py` is
byte-identical to its pre-1391b5c blob. **The fix of N21-C-1 is the 16 KiB bounded reads** of `src/graceful_close.py`
(ADR 0003 §9: every read through one 16 KiB buffer, drains discard in it, the answered body released before the
drain, the concurrency slot released before the drain, at most 512 drains). `tests/test_fix23_no_mmap_threshold.py`
pins that the launcher leaves the threshold alone and that Decision 21's arena limit and idle trim stay.

Measured with the call removed (e96bf0b and later; HEAD c71e363), Python 3.13.13, three busy loops, the test's own
reading client, 96 MiB bound unchanged: `test_live_128_senders_of_3_9mb_that_then_stall_are_bounded_by_the_inflight_budget_and_cut`
**20x alone: 0 failed**, growth 82-92 MiB (median 88); `tests/test_fix8_n7_2_body_prealloc.py` **10x as a module:
0 failed**, growth 81-90 MiB (median 86). The margin is real but thin: the worst run is 4 MiB under the bound (wave
22 with the fixed threshold measured 77-80; round 22's own no-threshold runs 81-94). The bound was not raised; a
future change that adds per-connection buffering will show here first.

Full suite (3.13.13 and 3.12.3): 1036 tests (recorded in a5fb681) — 1034 passed, 1 skipped, 1 failed each —
`test_fix7_new4_parse_fairness.py::test_live_junk_flood_does_not_starve_small_legit_requests[8-oversized]`: all 192
junk requests end `BrokenPipeError`, none reads its 413. It fails identically on 540a64e on this box (3/3 per Python,
A/B alternating with this wave's tree: `services/delivery-py/docs/evidence/dept28/round22/ab-*`), so it is not this
wave's change; it passed in round 22's runs on 540a64e. The test expects a blocking 4 MiB `sendall` to read an early
413 — the residual ADR 0003 §9 accepted and `tests/test_fix22_drain_residual.py` pins for 503 (the client is reset
once the bounded drain ends) — so whether any 413 is read depends on how much of the 4 MiB the socket buffers absorb
before the drain ends. Reported, not changed: making the test accept zero 413s would weaken it without a ruling.

**Correction (fix wave 24, AEGIS N23-S-4):** the paragraph above was true when written and then overtaken in the same
wave. On Sep 30 the lead ruled the junk-flood client change legitimate (FIX_WAVE_23b brief: the same class as the
fix8 128-sender test, waves 21/22 — the server answers the over-limit Content-Length at once, drains at most 64 KiB /
1 s and closes, so a client that blocks on sending the whole 4 MiB before reading is reset by design), and commit
1e1fc59 changed the TEST, not the product: the junk sender of `test_live_junk_flood_does_not_starve_small_legit_requests`
now sends while reading and reads each answer to its Content-Length (`_send_reading`, reusing the connection only
when the server kept it open), and its codes assertion no longer admits resets — every junk request must end
413/422/503, every `oversized` one 413. The blocking client's residual is pinned live by
`test_live_blocking_sendall_oversized_client_is_reset_before_reading_its_413` (a blocking `sendall` is reset; the same
bytes from a reading client get their 413) beside the synthetic `tests/test_fix22_drain_residual.py`. After 1e1fc59
the full suite had no failure on this box (the reviewers' round-23 runs: 1036 passed (recorded in 34daeeb), 1 skipped on 3.12.3 and on
3.13.13).

## Fix wave 24, Oct 1 2026 — every request-body byte inside the one 64 MiB budget, by construction (AEGIS round 23 N23-S-1)

**The finding.** The 128-sender test's 96 MiB growth bound held by allocator luck: round 23 measured 84-114 MiB under
three busy loops (6/20 single runs and 3/10 module runs failed). The in-flight budget counted only the bytes the
app had taken past each body's first 64 KiB. Outside it were the 8 MiB small reserve (a separate pool), the body bytes
uvicorn's protocol buffers before the app reads them (its 64 KiB high-water mark plus the read that crossed it, per
connection — tracemalloc: 15.6 MiB in `cycle.body` across the burst), the chunk the app held while it waited to
reserve it, and the growing body `bytearray`'s headroom and realloc copies.

**Decision (Decision 21 amended; the 96 MiB bound unchanged; no fixed mmap threshold).**
- `_INFLIGHT_BODY_BYTES` (64 MiB) is the WHOLE budget: the small reserve (`_SMALL_RESERVE_BYTES`, 8 MiB) is carved out
  of it and the shared pool is the rest (56 MiB).
- `BodySizeLimitMiddleware` owns each body's budget bytes (`_BodyHold`): a chunk is counted the moment the app
  receives it — while it waits to be covered, in the pool's `over` — and the body is not read further until the
  budget covers it (503 after `_INFLIGHT_WAIT_S`, preemption as before; the wait is the service's, not charged to
  the client). `_off_loop` no longer reserves; it releases the hold once the parse has dropped the body.
- Backpressure at the socket: under `python3 -m api` uvicorn's protocol (`http_limits.DeadlineH11Protocol.
  handle_events`) pauses reading as soon as it holds any unread body, so it reads a body only when the app asks, one
  read (`READ_BUFFER_BYTES`, 16 KiB) at a time — it used to read on to 64 KiB + a read whether or not the app asked.
- The body is kept as the chunks received (uvicorn's `bytes` objects, exact size) and joined once inside the parse
  slot (one body at a time in the large lane): no growth headroom, no realloc copies left in reused heap pages.
- Decision 21's release to the OS stays as written: `malloc_trim(0)` one idle second after the last large body
  (parsed or refused) with none in flight; the arena limit stays.

**The bound, derived.** By construction, the request-body bytes the process holds are at most: 64 MiB counted
(`used` <= each pool's limit) + `over` <= one 16 KiB chunk per body waiting for the budget (<= LIMIT_CONCURRENCY:
2 MiB) + one unasked 16 KiB read per open connection in uvicorn's buffer (<= MAX_OPEN_CONNECTIONS: 4 MiB) = 70 MiB;
plus, when a body is parsed, one joined copy of one body (<= 4 MiB; no parse happens in this scenario). The fixed
term was measured, not assumed: the 128-sender scenario against `python3 -m api` started with the budget patched to
X MiB (`probes/ful_budget_sweep.py` in the wave-24 scratch evidence), three busy loops, 3 runs per budget, X = 8.06,
16, 32, 48, 64: growth 8-9 / 17 / 33 / 48-49 / 64-65 MiB — least squares **growth = 0.998·X + 0.86 MiB** (largest
residual 0.28 MiB). Before the chunk change the same sweep gave 1.165·X + 0.9 (the bytearray's headroom and copies),
and at the wave-23 code 39-43 MiB at X = 16 and 60-67 MiB at X = 32 (the small reserve and uvicorn's buffers on top
of X). Derived bound at 64 MiB: 70 MiB by construction + 0.9 MiB measured fixed term = 71 MiB, leaving 25 MiB of
the 96 MiB bound as stated margin for what the derivation does not count (per-connection objects beyond the sweep's
128, allocator variance on another libc, another Python). The 96 MiB bound is reachable and was not changed.

**Correction (fix wave 25; AEGIS round 24 N24-S-1, N24-S-15).** The "60/60" below was the implementing engineer's
own run. On the same code (b51f307) AEGIS round 24 measured **7 failures in 60** under busy loops plus an uncontrolled
co-tenant — every one on liveness (settle time, senders left unanswered within the client's 20 s), never on memory —
and a liveness regression against 01e2851 (N24-S-2). The paragraph stands as a record of what was claimed; fix wave 25
below is the answer. "The five in-process tests" at the end of this section was also wrong: the wave-24 commit
(79fe238) made **six** edits to existing tests — five `_INFLIGHT_BODY_BYTES` patches
(`test_fix10_body_clock_and_disconnect.py` one, `test_fix8_n7_2_body_prealloc.py` three, `test_fix9_inflight_fairness.py`
one) and one `child_env()` line in `test_fix10_body_clock_and_disconnect.py`'s launcher.

**Measured on the final code** (Python 3.13.13, this 2-CPU box, the test's own reading client, 96 MiB bound):
`test_live_128_senders_of_3_9mb_that_then_stall_are_bounded_by_the_inflight_budget_and_cut` alone and
`tests/test_fix8_n7_2_body_prealloc.py` as a module — (A) under three busy loops: single **20/20 passed** (recorded in 34daeeb) (growth
64-65 MiB, median 65), module **10/10** (57-62, median 60); (B) three busy loops AND a concurrent heavy tenant
(creative-py's full suite looping; load average up to 10.7): single **20/20** (64-67, median 65), module **10/10**
(58-62, median 62). Every one of the 60 runs answered all 128 senders (408 or 503) and settled within 11.3 s (the
test's bound 13 s). On the intermediate code (budget fixed, bytearray kept) the same batches gave 65-85 MiB and one
module run failed at load 11 (the settle window and the in-process stalled-bodies test's 50 ms ordering sleep —
neither the memory bound); that run is not on the final code.

Not proven: other allocators (macOS) and other Pythons for the growth numbers (3.12 runs the suite, not the
batches); at budgets well below 64 MiB under three busy loops some senders were answered later than the probe's 20 s
client timeout (bounded by the 30 s body deadline + 5 s; the wave-23 code showed unanswered senders under the same
probe too) — per-chunk budget waits of <= 2 s each can add up to the body deadline under heavy contention.

Tests: `tests/test_fix24_body_memory_accounted.py` (3, all failing on 01e2851: the pools summed to 72 MiB; the chunk
in hand was not counted; uvicorn buffered 81 733 body bytes for an app that never asked). Changed: the five
in-process tests that patch `_INFLIGHT_BODY_BYTES` to size the SHARED pool now add `api._SMALL_RESERVE_BYTES` (the
total includes the small reserve; their shared pool is what it was).

## Fix wave 25, Oct 1 2026 — liveness restored, parsed models counted, loop lag not charged to the client (AEGIS round 24 N24-S-1..-4, -12, -13; FIX_WAVE_25 H1-H4)

Ruling record: `FIX_WAVE_23b.md` (the wave-23 rulings, recorded Oct 1 because round 24 found them only in a dispatch
prompt) and `FIX_WAVE_25.md` (H1-H4; R-LOAD: load proofs under **2** busy loops, heavier co-tenant runs informational).
The first wave-25 commits (cd5fb49, b6790d3, cc5582e, 0c4784c) were made without a report; the E-A engineer re-verified
them, found one regression in them and fixed it (H3 below), and ran the proofs below on the result.

**H1 — liveness (N24-S-1/-2).** Wave 24 paused the protocol as soon as it buffered ANY body byte the app had not taken,
so every 16 KiB read waited for a round trip through the app; under CPU contention bodies arrived more slowly and
stalled bodies were cut later. Now the app reserves up to `_READ_GRANT_BYTES` (64 KiB, uvicorn's own high-water mark)
AHEAD of what it has taken — only from free budget and never while another body waits for it
(`_BodyHold.grant`, `_InFlightBytes.try_reserve`) — and the protocol keeps reading while the bytes it buffers plus the
bytes the app has taken are below the bytes covered (`http_limits.DeadlineH11Protocol.handle_events`; the hold is in the
request scope under `BODY_HOLD_SCOPE_KEY`). Covered bytes stream; at most one read past them is ever buffered (the
wave-24 bound is unchanged); a grant past the end of a chunked body is given back when it completes (`trim`). A budget
wait never runs past the body deadline (it is cut there, 408), so every sender is answered by the 30 s deadline +
5 s however many budget waits it met. The launcher also sets the GIL switch interval it never set (1 ms; scout C5-2;
`FULFILLMENT_SWITCH_INTERVAL_SECONDS`, only 100 us .. 50 ms, checked in force in whole microseconds and printed).

**H2 — the test measures something (N24-S-13).** The 128-sender test's settle check starts only after the peak phase
(a sample at base + 32 MiB); a run that never reached it is reported INVALID and fails. The senders run on one thread
(a selector), so a starved client process no longer decides the result. The settle bound stays the fixed 13 s. (The
stopped engineer's draft extended it by the server loop's measured run-queue wait; the E-A review reverted that — it
raises a test bound — and the wait is only printed now, `/proc/<pid>/task/<pid>/schedstat`, so a slow server and a
starved one read differently in the log.)

**H3 — parsed models (N24-S-4).** The derivation left out the parsed model, which outlives the body's budget bytes and
can be ~5x the body (CPython stores a string with one astral character at 4 bytes a character, plus its UTF-8 copy).
Each model's size is measured right after the parse (`_retained_bytes`: everything reachable, each object once, +1/16;
against tracemalloc it counts 1.03-1.06x what the model holds — `tests/test_fix25_liveness.py`) and counted in the
budget until the model is dropped (after the agent's work). Measured worst models per request model: PENDING-E-A-H3 (E-A `h3_models` probe). A large body's model waits for
the budget like a chunk in hand (503 after `_INFLIGHT_WAIT_S`).
**Regression found and fixed in review:** as first committed (cd5fb49) a SMALL body's model waited for the shared pool
too — a 64 KiB valid body whose model is ~0.44 MB was answered 503 whenever the shared pool was held (by stalled senders,
the very attack the small reserve exists for), breaking Decision 23 ("a small body never waits for in-flight bytes");
on b51f307 the same request was 200 (`test_a_small_body_never_waits_for_the_shared_budget_even_when_its_model_is_larger`,
failing on cd5fb49..da30363). A small body's model is now counted without waiting (`_BodyHold.cover_now`): what the
pools have free, the rest in the shared pool's `over` until the model is dropped.
**Chosen: count, not reserve ahead.** Reserving the worst model (5.5x + up to 3 MiB) BEFORE the parse would put the
parse in progress inside the budget too; it was built and measured and is not used: with 64 front-loaded slow
senders holding the shared pool, every legit large batch was refused 503 (3/3, `test_live_q1_slow_senders_..._do_not_
starve_legit_clients[1MiB-front-...]`, Decision 23's other half), because a ~1 MiB body then needs ~9 MiB of budget and
preemption frees one holder at a time.

**The bound, restated (H3).** Request memory the process holds at most:
- counted, `used` <= the limit: **64 MiB** — body bytes, read-ahead grants, and every parsed model after its parse;
- `over` (in memory, outside the limit): <= one 16 KiB chunk per body waiting to be covered (<= 128: **2 MiB**); a large
  body's model while it waits to be covered (one at a time: the large lane has one slot; <= 21.2 MiB measured worst —
  the same model as the next term, not in addition to it); small bodies' models past what the pools had free (each
  <= ~0.7 MiB measured; how many coexist depends on how many small requests are in the agent's work at once — NOT
  bounded structurally, see "Not proven");
- uncounted: one unasked 16 KiB read per open connection in uvicorn's buffer (<= 256: **4 MiB**); the parse in
  progress — per large slot (one) the joined body copy (<= **4 MiB**) and the model being built (<= **21.2 MiB** measured
  worst), per small slot (four) <= 64 KiB + ~0.7 MiB (<= **3 MiB**);
- the measured fixed term (wave-24 budget sweep): **0.9 MiB**.
Sum with no parse in flight (the 128-sender scenario the 96 MiB test bound is for): 64 + 2 + 4 + 0.9 = **71 MiB**, margin
25 MiB. With the worst large parse in flight as well: 71 + 4 + 21.2 + 3 = **99 MiB** — above 96 MiB: the 96 MiB bound is
the 128-sender scenario's (bodies only, no parse), not a bound on every mix. Measured (2 busy loops, the reviewer's probes): PENDING-E-A-ASTRAL.

**H4 — loop lag is not the client's time (N24-S-12).** The rules that judge a client by time (the app's stall,
trickle and arrival rules, the preemption charge, the protocol's stall and rate rules for unread bodies) counted every
second of wall time spent waiting for the client's bytes, including time the event loop could not run. Measured: a
client sending 32 KiB every 0.5 s with the server process stopped (SIGSTOP) 6 s mid-body was 408 "stalled for 5s" 3/3.
`http_limits.LoopLag` measures the time the loop was behind (a 50 ms tick; lateness past 50 ms is `lost`) and those
rules subtract it; the hard deadlines (30 s + 5 s, the head deadline) stay wall-clock. Consequence, accepted by the
ruling: on a starved box stalled bodies are cut later in wall-clock seconds, by about the starvation, never later than
the hard deadline. Residual: the protocol's `_body_last_lost` is read in `data_received` before the late tick of the
same loop pass has run, so after a freeze the protocol's own stall clock (a backstop at 10 s) can credit the freeze
once more.

**Proofs** — PENDING-E-A-CAMPAIGN (filled from the E-A campaign logs; the draft's numbers were the stopped
engineer's own and are not repeated here).

**Not proven / could still be wrong.** Small-body models are counted but not bounded structurally (above). Other
allocators and Pythons (macOS; the batches ran on 3.13). Whether a request model shape exists whose model is larger
than the measured worst (the probe tried the long-string and many-object extremes of every request model, not every
mix). The protocol's double credit after a freeze (above). Uncontrolled co-tenants were present for every load run.

## Verified so far (Sep 22, 2026 build session)

See `services/fulfillment-py/README.md` for the actual test count, what
was run, and the full honest-gaps list — this ADR records the
architecture decisions, not the test results, so it doesn't go stale
independently of the code.
