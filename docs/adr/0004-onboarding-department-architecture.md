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
3. **Record-first ledger discipline** (revised in fix wave 1, findings F2/F9). Every
   crossing and every gate ruling goes through `LedgerClient.record_event` (department
   `onboarding`, contract section 2).
   - The record of every decision in an operation is written BEFORE any outside effect.
     Outside effects are: payout activation, client handoff, contract storage, pushes to
     Andre, and sending account data to Revenue Recovery. Each is preceded by the record
     that authorizes it (for example `activation_ruling` and `payout_activation_request`
     before the payout).
   - Each effect's result is recorded after it. A failed effect is recorded
     (`activation_outcome`, `andre_push_not_delivered`, `revenue_recovery_failed`) and
     reported.
   - In-process effects (bus publishes, client-memory writes, institutional memory) are
     deferred to the end of a successful operation.
   - If a ledger write fails before any outside effect, nothing happened. State is
     unchanged, and the API returns 503 `{"proceeded": false}`.
   - If a ledger write fails after an outside effect, nothing further happens. Only
     result records can fail at that point. State reflects what did happen (for
     example `payout_active` / `handoff_accepted`, so a retry never repeats it). The
     API returns 503 `{"proceeded": true, "completed": false, "outside_effects_done":
     [...]}`. It never says "did not proceed" when something did.
   - `tests/test_fix_wave1.py::test_f9_record_first_at_every_write_position` checks this.
     It fails the ledger at every write position of 21 call sites, and asserts:
     - no effect follows a failed write;
     - every effect follows its authorizing record;
     - zero effects happen when the failed write comes before the first effect;
     - the API's `proceeded` / `outside_effects_done` match what happened.
   - With no ledger configured, `UnconfiguredLedgerClient` refuses every write, so the
     service can serve reads but won't act.
   - **Event ids are deterministic** (F11), from `ledger.derive_event_id`:
     `"onb-" + SHA-256(json([epoch:operation, department, event_type, subject_id,
     subject_seq, occurrence, payload_sha256]))`.
     - `epoch` is random per service instance. State is in-process, so a restart is a
       new history.
     - `operation` is the service method.
     - `subject_seq` is the subject's operation counter. It advances only when an
       operation completes, including a recorded refusal.
     - `occurrence` counts identical events within one operation.
     - `payload_sha256` is taken over the scrubbed payload.
     - Ids that appear in payloads (escalation, commitment, issue, access-link and
       tracking ids) are derived the same way.
     - So a retry of an operation whose write committed but whose response was lost
       reproduces the same ids. The ledger answers 200, and each event is recorded once.
     - The same id with different content is still the ledger's 409, which is refused.
     - Limit: idempotency holds within one process lifetime, and only while the retry
       computes the same decision. A retry that crosses a time boundary, such as the noon
       cutoff, is a different decision and is recorded as one.
4. **Revenue Recovery is reused, never rebuilt.**
   - Audit and Baseline calls detection-py's real routes over HTTP
     (`HttpRevenueRecoveryClient`). The route map is checked against
     `services/detection-py/src/api.py` by a test.
   - It consumes findings as JSON, with `amount_usd` as a two-decimal string (contract
     section 1). This is validated strictly (F14). Only the canonical
     `^(0|[1-9][0-9]*)\.[0-9]{2}$` string is accepted, up to 999,999,999,999.99.
     A JSON number, whitespace, leading zeros, a sign, an exponent or a third decimal
     rejects the finding. Nothing is rounded.
   - Request money (F15) is never rounded either. `"12.3"`, `12` and `49.99` are
     exact and accepted. `"12.345"`, `" 12.30"`, `"012.30"` and `0.1 + 0.2` are 422.
     Huge values are 422, not a `decimal.InvalidOperation` 500.
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
6. **Credentials: structural first, refused at intake second, scrubbed on output third**
   (revised in fix wave 1, F10).
   - No serializable model has a secret field.
   - Every inbound model rejects unknown fields.
   - Every inbound string is normalized, then checked by `redaction.find_credential`.
     - Normalization: NFKC; zero-width and other format characters removed; letters
       separated by spaces, dots or dashes collapsed; casefolded; a leetspeak variant
       for keywords.
     - Keywords are multilingual: password, contraseña, Passwort, mot de passe, пароль,
       パスワード and others.
     - The check refuses a password, PIN, OTP or key after its label, a `user / secret`
       pair after a login or creds word, a password-shaped token next to a login word,
       a Luhn-valid card number, and API-key or token shapes.
     - A credential-shaped value is rejected with 422. The message tells the client
       never to send credentials; the vault path is the only place for them, and it
       refuses today.
     - Only third-party content is scrubbed instead of refused: a website's HTML and raw
       account-pull rows.
   - Output scrub stays as a second layer. Every route's result, every error body, log
     record, ledger payload and summary, and memory write passes through `scrub`. If the
     normalized text is still credential-shaped, `scrub` replaces the whole string.
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
8. **Time: only the server's clock decides** (revised in fix wave 1, F1).
   - No request carries `now`, or any date that a decision is evaluated against.
     `ClipperApplication.applied_on` and `CreatorPaymentRequest.paid_on` were removed.
     Sending them is a 422, because inbound models forbid unknown fields.
   - **Clipper age (18+, written in stone, no guardian path) is computed on the server's
     date at UTC−12.** UTC−12 is the earliest calendar date anywhere on Earth, so an 18th
     birthday counts only once that day has begun everywhere. This is stricter than the
     America/Los_Angeles date and the UTC date. Nobody is approved while still 17 in
     their own time zone. The clock is injectable for tests.
   - The contract term (gate 14), the 1099 tax year and platform-fact shelf life use the
     server's business date in America/Los_Angeles.
   - Caller timestamps that are facts, not "now", are refused (422) when they are in
     the server's future. They are a fact's `observed_at`, a grant's
     `account_last_activity_at` and a contract's `signed_at`. A future value would
     out-rank newer facts, hide a stale account, or fake a signature date.
   - Stuck windows, promise deadlines, the noon cutoff, first-win and recommend-score
     timing already used only the service clock.
   - All wall-clock maths uses `zoneinfo`: the noon cutoff in America/Los_Angeles,
     quiet hours in the client's own zone.
9. **Actions attributed to Andre need Andre's own secret** (extended in fix wave 1, F4).
   - Playbook changes use an HMAC-SHA256 token over (rule_id, version, text), keyed by
     `ONBOARDING_ANDRE_APPROVAL_KEY`.
   - Acknowledging and resolving an escalation need the same kind of token, over the
     exact action (`memory.andre_action_token`):
     - acknowledge: `("escalation_acknowledge", client_id, escalation_id)`;
     - resolve: `("escalation_resolve", client_id, escalation_id, resolution,
       snag_category)`.
   - Tokens are compared in constant time. The shared service token alone gets 403, and
     the refusal is recorded (`escalation_action_refused`).
   - A token for one escalation, action or text is useless for another.
   - If the key is missing, nothing attributed to Andre is possible.
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
- Auth is one shared bearer token, plus Andre's approval key for actions attributed to
  him: playbook changes, and acknowledging or resolving escalations. There's no other
  per-caller identity.
- The ledger keeps only `payload_sha256`. Onboarding doesn't persist the payloads, so
  the hashes can't be re-verified later.
  - Payloads are scrubbed before hashing, as defence in depth.
  - Multi-event operations aren't atomic: a failure mid-way leaves the earlier events.
    Event ids are deterministic, so a retry replays them (200) instead of duplicating
    them.
  - A result record can fail after its outside effect has happened, for example the
    push reached Andre and then `client_commitment_made` failed. The API reports that
    exactly (`proceeded: true, completed: false`). The ledger then lacks that one result
    record, though the request record that authorized the effect is there.
- Fix wave 1 ran live against the real ledger-rust (built from this tree) and the real
  detection-py. `tools/fake_ledger_server.py` still exists for quick local runs. It
  validates exactly like ledger-rust, and C1 controls count as control characters, as
  they do in Rust's `char::is_control`.

**Detection is pattern-based**
- Injection, guarantee and credential detection are rules over normalized text, which
  is best effort on free text.
  - A password that is an ordinary word, typed with no cue, can't be recognised.
  - A 13–19 digit Luhn-valid number that stands alone is refused as a card number, even
    if it was something else.
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
- detection-py now emits money as canonical strings, and the fix wave 1 live run
  consumed them. A legacy float finding is now rejected, not converted. Section 1 allows
  the float fallback for fixtures, but findings from another service are held to the
  canonical wire form.
