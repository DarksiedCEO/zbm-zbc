# ADR 0004 — Onboarding Department Architecture

**Status:** Accepted for build (Sep 24, 2026). Not certified for real clients.
**Context:** The Onboarding department takes a client from signed contract to a fully
running account. The spec is the founder-approved "ZBM/ZBC Onboarding Department Spec"
and its "Intelligence Layer" tab (Andre, Sep 23 2026, with Sep 24 updates: 18+ locked,
ZBC rented-first). The shared build contracts are in `BUILD_CONTRACTS.md` (sections
0–3). The code is in `services/onboarding-py`.

A partial, untested draft from an interrupted run (commit `75de3d0`, "WIP UNVERIFIED")
was reviewed and kept where it was right. It was fixed where it was wrong:

- The `100%` guarantee pattern could never match.
- Log scrubbing was attached to parent loggers only, so child loggers weren't covered.
- The draft had no service layer, API, intelligences 11–15 or tests.

The WIP commit was replaced; it doesn't remain in history.

## Decisions

1. **One FastAPI service, same conventions as fulfillment-py and detection-py.**
   - pydantic v2 with the same pinned versions.
   - Fail-closed bearer auth (`ONBOARDING_SERVICE_TOKEN`) with
     `hmac.compare_digest` in `try/except TypeError`.
   - `/docs`, `/redoc` and `/openapi.json` are disabled.
   - `python3 -m api` binds `127.0.0.1` unless `ONBOARDING_BIND_ADDR` overrides it.
   - No new dependencies.
2. **15 single-task intelligences, one module each, deterministic, no model calls.**
   - Intelligences never call each other or another department. `service.py` passes
     results between them.
   - Knowledge, judgment and memory are kept apart:
     - Knowledge is dated and versioned: `QUESTION_BANK_VERSION`, `KNOWLEDGE_VERSION`
       with `last_verified`, `TRAITS_VERSION`, `THRESHOLDS_VERSION`.
     - Judgment is explicit rules in each module.
     - Memory lives in `memory.py`.
3. **Write-first ledger discipline.** Every crossing and every gate ruling goes through
   `LedgerClient.record_event` (department `onboarding`, contract section 2).
   - The request record is written before the crossing.
   - The ruling record is written before state changes.
   - A `LedgerWriteError` stops the operation and returns 503 `{"proceeded": false}`.
   - With no ledger configured, `UnconfiguredLedgerClient` refuses every write, so the
     service can serve reads but won't act.
4. **Revenue Recovery is reused, never rebuilt.**
   - Audit and Baseline calls detection-py's real routes over HTTP
     (`HttpRevenueRecoveryClient`). The route map is checked against
     `services/detection-py/src/api.py` by a test.
   - It consumes findings as JSON, with `amount_usd` as a two-decimal string (contract
     section 1). A legacy float is accepted only through `str()`.
   - A finding without both labels is rejected, not shown.
5. **Missing departments and infrastructure are ports with fail-closed stand-ins.**

   | Dependency | Stand-in answer |
   |---|---|
   | Compliance (38) | "not allowed yet" |
   | Verification and Integrity | "not allowed yet" |
   | Billing | "not allowed yet" |
   | ZBC payouts/tax | "not allowed yet" |
   | Handoff targets | "not accepted, no owner" |
   | Push to Andre | "not delivered" |
   | Contract storage | holds nothing, refuses to store |
   | Platform probe | "not verified" |
   | Site fetcher | refuses |
   | Platform writer | refuses |
   | Secrets vault | refuses to store, has no read method |

   Test doubles exist for each. The API uses them only where tests construct them
   explicitly. The one operator opt-in is `ONBOARDING_CONTRACT_STORAGE=in_memory`, for
   local demos.
6. **Credentials: structural first, pattern second.**
   - No serializable model has a secret field.
   - Every inbound model rejects unknown fields and scrubs every string at ingest.
   - Credential-looking identifiers are refused.
   - 422 bodies drop `input`/`ctx`.
   - Unhandled errors are caught by a middleware that logs only the exception type.
     A Starlette 500 handler would re-raise and let the server log the message.
   - A log-record factory scrubs every record from every logger.
7. **The activation gate is two independent gates.**
   - 14 (contract) and 15 (compliance) each produce a `GateResult`, and both are
     ledgered.
   - The unmet list is the union, prefixed `contract_14/` or `compliance_15/`.
   - The post-gate crossing (client handoff, or creator payout activation) runs only
     when both pass.
   - Risk (8) is kept separate: its hard stop feeds 15 as `no_hard_stop`.
8. **Time.**
   - There is no `now` on any request (lesson from the fulfillment-py Sep 24 audit).
   - All wall-clock maths uses `zoneinfo`: the noon cutoff in America/Los_Angeles,
     quiet hours in the client's own zone.
9. **The playbook changes only with Andre's approval token.**
   - The token is HMAC-SHA256 over (rule_id, version, text), keyed by
     `ONBOARDING_ANDRE_APPROVAL_KEY`.
   - If the key is missing, no change is possible.
   - Versions are append-only. Learning-loop output is only ever a proposal.
10. **Certification is organised by the spec's four types.**
    - Tests are grouped as scenario, attack and guardrail.
    - Independent review (type 4) is out of scope for this build.
    - Every intelligence's `STATUS` says "NOT certified".

## Choices made where the spec is silent (safest option, for Andre to confirm)

**Commitments and quiet hours**
- A "today" commitment is due at 17:00 local (LA). "First thing tomorrow" is 09:00 LA
  the next calendar day. Weekends and holidays aren't special-cased.
- Exactly 12:00:00 counts as after the cutoff, so it gets "first thing tomorrow".
- Promise Keeper nudges Andre 3h before a commitment is due and warns the client 1h
  before.
- The warning moves to the latest non-quiet minute before the due time. If there is
  none, it's sent anyway and flagged `quiet_hours_override`. A silently missed promise
  is worse than a message in quiet hours.
- An acknowledged escalation still gets the pre-due client warning unless it's resolved.
  There's no "on track" route yet.
- Default quiet hours are 21:00–08:00 in the client's own zone. Replies to a client who
  is messaging right now go out immediately. Proactive messages are held.

**Escalation**
- After the one attempt on a soft trigger, the service waits 24h
  (`soft_resolution_window_hours`) before escalating. An explicit
  `issues/{id}/outcome {resolved:false}` escalates at once.
- A Risk hard stop pauses work and escalates immediately as `audit_anomaly`. The spec
  lists audit anomaly as soft, but a hard stop can't wait on an attempt.
- If the briefing push isn't delivered, the client isn't promised a time. The human-
  request reply says Andre will be brought in and that a time will follow.
- A deal with an unknown size escalates.

**Access and platform facts**
- Platform facts (roles, steps, what ZBM can and can't see, revoke paths) for Google
  Ads, Meta and Shopify are drafts that have never been verified. Until a person
  verifies them, access steps and permission receipts are held (`quotable: false`), per
  the "facts expire, never quote stale" rule.
- Other defaults: stale-account window 90 days; platform-fact shelf life 90 days.

**Numbers**
- Revenue-mismatch tolerance is 25% of the larger figure.
- Money fields are never echoed back to a client as a confirm-prefill, because that
  would put an unlabeled dollar figure in front of them.

**Creators and brands**
- Clipper vetting thresholds (fake followers 15%/30%, pods 0.4/0.7, 10-post history) are
  a draft.
- Contract gate 14 for creators checks only that the clipper agreement was signed. No
  clipper terms are stored.
- The proving campaign is capped at $500.00, 3 creators, 14 days (draft).
- A non-regulated brand that asks for owned posting doesn't get it. The request is noted
  for Andre.

**Scoring drafts**
- The P9 caption rule: a disclosure marker within the first 100 characters. Draft,
  pending counsel/FTC review.
- The health score weights and bands are a draft.

## Open items (not decided; configuration with fail-closed defaults)

| Item | Default in code |
|---|---|
| Deal-size threshold | **Unset**. Every deal escalates, and the reason says so. |
| Stuck window | 48h (suggested, not confirmed). `ONBOARDING_STUCK_WINDOW_HOURS`. |
| Intake channel (chat, voice or both) | Undecided. The access link is issued, not delivered. |
| Noon cutoff | 12:00 America/Los_Angeles, to be revisited. `ONBOARDING_COMMITMENT_CUTOFF` / `_TZ`. |
| Contract storage location | Undecided. The stand-in holds nothing, so gate 14 is unmet. |
| Compliance department (38) | Not built. The stand-in rules "not allowed yet", so gate 15 is unmet. |
| Counsel: P1 AI-disclosure wording (Cal. B&P 17941) | Not approved. Gate 15 is unmet. |
| Counsel: P23 CCPA/CPRA clause | Not approved. Gate 15 is unmet. |
| Accountant: P8 1099 threshold | $2,000.00 for 2026. 2027 onwards is unset and treated as reportable. |
| Spanish (P24) | Off. Enabling it refuses startup (wave 2/3). |
| Target onboarding time (7–14 days) | Not enforced (spec: suggested, not confirmed). |

## Honest gaps

**Nothing live**
- No platform API: grants never reach `usable` against the real stand-in, so P21 can't
  pass live.
- No site fetching, no platform writes, no vault (tier 2/3 impossible), no push to
  Andre's phone (escalations queue at `GET /onboarding/escalations`).
- Compliance, Verification, Billing, Payouts, handoff intake and contract storage are
  stand-ins.
- The shared event bus is in-process only. The risk watcher and AEGIS consumers aren't
  built.

**State, auth and the ledger**
- State is in-process memory, one process. It's lost on restart.
- Auth is one shared bearer token. Any holder can acknowledge or resolve escalations.
  There's no per-caller identity.
- The ledger keeps only `payload_sha256`. Onboarding doesn't persist the payloads, so
  the hashes can't be re-verified later.
  - Payloads are scrubbed before hashing, as defence in depth.
  - Multi-event operations aren't atomic: a failure mid-way leaves earlier events.
  - Event ids are random per attempt, so an API-level retry produces new events rather
    than an idempotent replay.
- A briefing can be pushed and then its commitment write can fail. Andre got the push,
  but the client has no commitment. This is the safe direction.
- The live run used `tools/fake_ledger_server.py`, not ledger-rust. The real
  `POST /ledger/events` is being built in parallel and hasn't been run against this
  service.

**Detection is pattern-based**
- Injection, guarantee and credential detection are regexes: best effort on free text.
- Decisions never read injection flags. No secret-bearing field exists. These
  structural guarantees don't depend on the regexes.
- A figure with no `$`, "dollars" or "USD" marker (e.g. "10k more sales") isn't caught
  by the money filter.

**Not built**
- Document-to-facts extraction needs a model. Documents are only stored and scanned.
- There's no route to mark platform facts verified. Today that means editing
  `PLATFORM_KNOWLEDGE` under review.

## Contract observations (reported, not changed)

- **Section 2** defines `payload_sha256` but no home for the payload itself. Without
  one, a ledger event proves a payload existed but nobody can later show which. A
  payload store (or payload-in-ledger for non-sensitive fields) needs deciding.
- **Section 1** says "accept `str`" for money but doesn't say whether exponent strings
  (`"1e3"`) are valid. Onboarding rejects them.
- The detection-py in this branch still emits float money. Onboarding already consumes
  both forms, so it's compatible before and after the Decimal workstream lands.
