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
   - **Unknown outcome** (fix wave 5, NEW-4). Earlier, every httpx error counted as
     a certain failure. Through a lossy proxy the ledger returned 201 while the API
     said `proceeded: false`. `HttpLedgerClient` now classifies each failure:
     - Not recorded: local contract validation, connect/pool errors (nothing sent),
       4xx other than 409, and ledger-rust's own load-shed answer.
     - Unknown: timeout, reset or disconnect after sending, every other 5xx, and
       409.
     - **The shed rule is shared with creative-py** (fix wave 6, N6; ADR 0005,
       decision 21). Fix wave 5 counted ANY 503 as not recorded, reasoning that
       ledger-rust's only 503 is its load-shed. That is true only with no
       intermediary: a proxy or gateway between the services can forward the
       request, see the ledger append it, and still answer 503. So "not recorded"
       needs the exact answer `shed()` writes before it reads the request: status
       503 with the body `{"error": "ledger-rust is at its connection limit; retry
       shortly"}` (`LEDGER_SHED_BODY` in both `services/onboarding-py/src/ledger.py`
       and `services/creative-py/src/shared/ledger.py`). A 503 with any other body,
       like every other 5xx, is unknown. `test_fix_wave6.py` checks the constant
       against creative-py's and against `bin/server.rs`, and proves both cases
       against the real binary: `LEDGER_MAX_CONNECTIONS=1` with a held socket gives
       `proceeded: false, ledger_write: not_recorded`, and an intermediary that
       forwards then answers 503 with its own body gives `proceeded: "unknown"`
       while `GET /ledger/entries` shows the event was appended. Those live tests
       skipped silently when the binary was missing (AEGIS round 6); a session
       fixture now builds it with cargo into the git-ignored target dir, so they run
       by default, and a run without cargo skips them with a reason `-rs` prints
       (fix wave 7).
     An unknown outcome returns 503 `{"proceeded": "unknown", "ledger_write":
     "unknown", "retry": "..."}` with `Retry-After`. It never says "did not
     proceed". The staged state stays uncommitted and the subject's sequence does not
     advance, so the identical retry derives the same ids, gets 200 for what the
     ledger holds, and commits. A 409 carries a different instruction: retrying won't
     resolve it, so an operator must reconcile.
   - If a ledger write fails after an outside effect, nothing further happens. Only
     result records can fail at that point. State reflects what did happen (for
     example `payout_active` / `handoff_accepted`, so a retry never repeats it). The
     API returns 503 `{"proceeded": true, "completed": false, "outside_effects_done":
     [...]}`. It never says "did not proceed" when something did.
   - **Stage then commit** (fix wave 3, N2). State that depends on a record becomes
     visible only after that record is written. `start_client` builds the client
     record and its escalation first, and adds them to state only when every record
     is written. An escalation from any operation is attached only after its push
     result record. `apply_creator` adds the creator the same way. State the client
     learns from the reply (a tick's warning or breach notice, a soft-trigger
     check-in and the stall count, a commitment made on tick, an undelivered-attempt
     counter) is applied only when the operation completes.
   - **Result records are owed until written** (fix wave 3, N2/N7). A record of an
     effect's result (`contract_storage_ruling`, `andre_push_not_delivered`,
     `andre_push_result`, `promise_nudge_result` after a delivered nudge,
     `activation_handoff_ruling`, `payout_activation_ruling`, and since fix wave 4 the
     audit's `revenue_recovery_ruling` and `risk_ruling`) is queued with its
     deterministic id before it is written. If it fails after the effect, the next
     operation on that subject writes it first, with the same id. So an "already
     active / already accepted" retry path can't skip it, and it's never written
     twice. If the operation fails before any effect, the queued record is dropped,
     because nothing it describes happened.
   - **A retry finishes the job.** Retrying `start_client` after a partial failure
     writes the owed records. It then delivers a briefing that was never attempted,
     or makes the commitment whose record failed, and returns the start's reply.
     It never answers 409. A retry with a different body still gets 409, after the
     owed records are written. `apply_creator` works the same way and finishes the
     creator's activation (the payout is never activated twice). Retrying an
     operation that raised an escalation finds it by its deterministic id and
     completes it; it never pushes Andre again.
   - **The audit's rulings are owed, and a retry never re-sends the account data**
     (fix wave 4, A1). They were written with the plain record call, so after a
     failure the next operation didn't write them, and a retry sent the account data
     to detection again. Both rulings are now queued as owed before the first is
     written (`_record_results`), so a failure on the first leaves both owed, in order.
     The detection result is kept on the client (`audit_known`: the request digest
     and what detection returned, never the account data) from the moment it's known.
     A retry of the same audit uses it: no second request record, no second call.
     A different audit is new data and is sent. A failed detection call has no
     result, so its `revenue_recovery_failed` record stays a plain record and a retry
     calls again, like an undelivered push. Sweep: every other record written after
     an outside call is either already owed (the list above) or records a read or
     a ruling request (platform probe, contract lookup, Compliance, Billing, age
     verification). A retry repeats those reads; nothing changes outside.
   - Sweep (fix wave 3): every operation was checked for state changed before a
     record it depends on. Found and fixed:
     - `start_client`: client and escalation before `contract_storage_ruling` or the
       push result.
     - `_escalate` (message, audit, issue outcome, tick, recommend score): the
       escalation before its push result.
     - `apply_creator`: the referred creator before `andre_push_result`.
     - `message`, `website_scan`, `add_facts`, `add_document`: the injection counter
       before later records.
     - `recommend_score_submit`: the score before the soft-trigger record.
     - `tick`: the nudge failure counter before `promise_nudge_result`; the warning
       and breach state, the soft issue and stalls before later records of the tick.
     - `activate_client` / `activate_creator`: `activation` before
       `activation_outcome`; `handoff_accepted` / `payout_active` with their ruling
       never written on retry (N7).
     - All other operations write their record before changing state.
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
     - `epoch` (fix wave 4): for the operations that create a subject
       (`start_client`, `apply_creator`) it's the configured `ONBOARDING_INSTANCE_ID`
       (default `onboarding-1`). A restarted process's retry of them derives the same
       ids, and the ledger dedupes it (200). For every other operation it's the
       instance id plus a per-process boot id. State is in-process, so after a restart
       a subject's history starts again at sequence 0. With the stable id alone, a
       new event could take the id of an old one and be silently deduped or refused
       (409); a probe showed exactly that. Those operations can't be retried across
       a restart anyway: they need state the restart lost (404).
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
     - Limit: idempotency holds within one process lifetime (across a restart only for
       `start_client` and `apply_creator`), and only while the retry computes the same
       decision. A retry that crosses a time boundary, such as the noon
       cutoff, is a different decision and is recorded as one.
4. **Revenue Recovery is reused, never rebuilt.**
   - Audit and Baseline calls detection-py's real routes over HTTP
     (`HttpRevenueRecoveryClient`). The route map is checked against
     `services/detection-py/src/api.py` by a test.
   - It consumes findings as JSON, with `amount_usd` as a two-decimal string (contract
     section 1). This is validated strictly (F14).
   - **One money rule** (fix wave 3, F15), for findings, requests and configuration:
     the contract string of ADR 0003 section 1a,
     `^(0|[1-9][0-9]{0,14})\.[0-9]{2}$`, so amounts are below 10^15 and the largest
     is `"999999999999999.99"`. `"12.3"`, `"12"`, a JSON number (`12.30`, `0.1`,
     `12`, `1e3`), whitespace, leading zeros, a sign, an exponent or a third decimal
     is 422. Nothing is rounded, and there's never a `decimal.InvalidOperation` 500.
     Zero is allowed only where the field allows it. `tests/test_fix_wave3.py` checks
     every string and JSON vector of `fixtures/money_vectors.json`, in the model and
     over HTTP. A figure the client types as an intake fact, such as stated monthly
     revenue, isn't contract money. `client_stated_amount` reads it only for the Risk
     comparison.
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
   - Fix wave 3 (N5) refuses these shapes too:
     - a URL with a password in its userinfo;
     - "get in with X and Y" and "use X to log in";
     - leetspeak password words (`p@ss`, `p4ssw0rd`);
     - SSNs;
     - bank account or routing numbers after a bank word, and IBANs;
     - an `admin / <mixed-case+digit secret>` pair.
   - **Raw client free text is never stored or served** (fix wave 3, N5). Messages,
     documents, fact values and evidence, a clipper's bio, and Andre's resolution text
     are kept only as `redaction.redact_text(...)`, and GET and the exit export return
     only that form. It replaces the following with `[REDACTED]`:
     - URL userinfo, and secret-named or password-shaped URL parts;
     - SSNs, bank and routing numbers, IBANs, Luhn-valid cards, and 9–19 digit runs;
     - high-entropy tokens of 10 or more characters that mix letters and digits;
     - everything after a login, password, PIN, secret, credentials or bank word, in
       any supported language, up to the end of that sentence;
     - the few words before "to log in".
     Emails and plain URLs are kept. A fact outside the lane's profile keeps its name
     but not its value. `test_n5_credential_spray_zero_occurrences_anywhere` sprays
     every AEGIS shape and a `creds.txt` document. It asserts zero occurrences in
     responses, logs, the exit export, the ledger and the service's in-memory state,
     including a run with intake refusal and the output scrub removed.
   - Output scrub stays as the last layer. Every route's result, every error body, log
     record, ledger payload and summary, and memory write passes through `scrub`. If the
     normalized text is still credential-shaped, `scrub` replaces the whole string.
   - Credential-looking identifiers are refused.
   - 422 bodies drop `input`/`ctx`.
   - Unhandled errors are caught by a middleware that logs only the exception type.
     A Starlette 500 handler would re-raise and let the server log the message.
   - A log-record factory scrubs every record from every logger (fixed in fix wave 3,
     D1). String args are scrubbed one by one and non-strings are kept, so args stay
     a tuple of the same shape. A message that is still credential-shaped is rewritten
     with `args = ()`, never `None`. The old `None` crashed uvicorn's access formatter
     on every request. For `uvicorn.access` the request path goes through
     `redact_url`, which handles userinfo, secret-named query values and
     password-shaped path segments, percent-encoded ones included.
     `test_d1_real_uvicorn_access_log_*` runs the real server on a real socket.
   - **Hostile input is bounded before it is scanned** (fix wave 4, R1). AEGIS found
     the credential scanner quadratic: 20,000 `a` took 7.6 s in `find_credential`, a
     60 KB message held the process for 132 s (the check ran in a `mode="before"`
     validator, before `max_length`), and the access-log scrubber let a 30 KB URL
     block `/health` for 20.8 s without auth. Fixed at the root:
     - Every regex in the service is linear-time on hostile input. The quadratic ones
       (`_SLASH_PAIR`, `_EMAIL_PAIR`, `_EMAIL_TOKEN`, `_LOGIN_PAIR`, `_CREDS_PAIR`,
       `_URL_USERINFO*`, the JWT shape, `redact_url`'s head split, the guardrail's
       worded dollar figure, the injection role marker, memory's e-mail and domain
       strippers, the Meta pixel tag) now start once per run (lookbehind anchors),
       use possessive or atomic groups, or scan in two steps. Superlinear non-regex
       code was fixed too: the login-word window re-split the whole text per keyword,
       the span redaction and the `$` label check re-sliced the text per match.
     - The new forms give the old results. A differential run of the final code against
       the base code over 240,000 random texts built from credential, URL, dollar, tag
       and change-request fragments (`find_credential`, `scrub`, `redact_text`,
       `redact_url`, the outbound guardrails, the injection scan, the identifier
       stripper, the change-request detector, the tag scan) differed only where the new
       form redacts more: a URL scheme that starts with a digit (`4821://user@`) now has
       its userinfo redacted.
     - Caps first. `Inbound` validates `mode="after"`, so type, pattern and
       `max_length` checks run before any credential scan, and every inbound string
       has a `max_length`. `InputLimits`, the outermost middleware, answers 414 to a
       path plus query over 8 KiB and 413 to a body over 1 MiB. It checks
       Content-Length, then reads the body itself and stops at the cap, so a chunked
       body is cut off too. A log line is cut to a bounded prefix (2,048 characters of
       path, 8,192 of message) before it is scrubbed.
     - Never on the event loop. FastAPI validates declared body models on the event
       loop, so bodies are now validated in a sync dependency, which runs in the
       threadpool. Exception handlers that scrub are sync too. `/health` is `async`
       and does no work, so it answers even when every worker thread is busy.
     - A CPU budget (revised in fix wave 5, NEW-2). Each body's credential checks run
       under `scan_budget`, and `find_credential` checks it between rules. A body not
       checked within it is refused (422 `scan_budget_exceeded`), never accepted.
       - The wave 4 budget was 5 s of wall-clock time. Every request shares the GIL,
         so concurrent requests spent each other's budget: 5 concurrent benign 416 KB
         bodies were all refused.
       - The budget now counts the request thread's own CPU time
         (`time.thread_time`). It is `ONBOARDING_SCAN_BUDGET_SECONDS` (1 s) plus
         `ONBOARDING_SCAN_CPU_MS_PER_KB` (10 ms) per KB of body.
       - Measured cost after the fix: 1.0–1.3 ms/KB for benign max-size bodies and
         ~1.8 ms/KB at worst for hostile ones, so the budget is about 5x headroom.
         The scanners are linear, so the size cap is the real time bound; the budget
         is a backstop.
       - Concurrency is bounded explicitly (revised in fix wave 6, N4). Fix wave 5's
         `HeavyScanGate` was a step: bodies over 64 KiB took one of 2 slots, smaller
         ones nothing, so 40 concurrent 58 KB bodies bypassed it, filled the 40-thread
         pool and starved light GETs (p50 3.1 s) and `/health` (2.4 s). It is now
         `ScanAdmission`, a weighted budget: every body takes `max(size,
         scan_min_cost_bytes)` (capped at the budget) out of `scan_inflight_bytes`
         while it is validated and scanned, so many medium bodies are throttled like
         one large one and no size class bypasses it. At most 16 wait, each up to
         30 s, granted oldest-first among those that fit (each waiter is woken
         once). Beyond that the API returns 503 with `Retry-After` and
         `proceeded: false`, never 422.
       - The defaults are measured, not derived (real uvicorn, real ledger-rust, 40
         concurrent clients, `test_fix_wave6.py`). Under one GIL, N concurrent scans
         add no throughput, and every extra CPU-bound thread lengthens the event
         loop's wait for the GIL: 40x16 KB bodies at 4 concurrent scans gave `/health`
         p50 300 ms and light GET p50 790 ms, at 2 concurrent 320 / 700 ms, at 1
         concurrent 16 / 70 ms; 40x58 KB at 1 concurrent gave `/health` p50 20 ms,
         p95 42 ms, light GET p50 27 ms. So the minimum cost equals the 64 KiB budget:
         one scan at a time whatever the size (a tiny message body is ~1 ms, so 40 of
         them queue for ~40 ms; a max-size body is ~0.5 s, so 12 concurrent max-size
         bodies finish in ~7 s, within the wait). The weighting is kept and
         configurable — raising the budget above the minimum cost admits several small
         bodies at once — because it is the right shape on a runtime without a GIL; on
         CPython it measurably slows light requests, and the README says so.
       - **Small bodies have their own lane** (fix wave 7, AEGIS round 6 NEW-5). Wave
         6 wrote that "a small body never queues behind a large one"; with the
         minimum cost equal to the budget that was false — every body waited its
         turn. Measured: one client's 416 KB bodies back-to-back put other clients'
         40-byte messages at p50 294 ms, 4 uploaders 1.6 s, 12 uploaders 6 s, and 16
         queued large bodies answered a tiny message 503. `ScanLanes` now routes a
         body of at most `scan_small_body_bytes` (16 KiB, ~1 ms of scan) to a small
         lane with its own budget (`scan_small_inflight`, 1) and its own queue
         (`scan_small_max_waiting`, 8, reserved so a flood of large bodies cannot fill
         it); large bodies stay serialized in the large lane. Three more causes of the
         same symptom were found and fixed with it, because the class is "a small
         request waits behind a large request's CPU work", not the gate alone:
         (a) `add_facts` redacted the body's text again UNDER THE SERVICE LOCK — ~1.5 s
         of CPU for 416 KB of profile fields, during which every operation waited
         (the AEGIS probe's `note_*` fields are dropped by the lane, so it did not
         see this; a body of real fields would) — the scan and redaction are pure and
         now run before the lock is taken, and the large lane is held through the
         handler (a generator dependency) so that work stays one-at-a-time and is not
         a second uncounted CPU-bound thread; (b) one verdict memo now spans the
         whole request (set by the outermost middleware; the body check, the
         service's redaction and the response scrub share it), so a clean string is
         scanned once, not three times; (c) the launcher sets the interpreter's
         thread switch interval to 1 ms (`ONBOARDING_SWITCH_INTERVAL_SECONDS`): a
         light request takes several GIL turns beside a CPU-bound scan, each up to a
         switch interval, so 5 ms slices were ~50-70 ms of the small message's time;
         the scan pays +3-4% of CPU under contention. Measured after (real uvicorn,
         real ledger-rust, `test_fix_wave7.py`, the `onb_serial6` shape): small
         messages beside 1 / 4 / 12 uploaders p50 17 / 24 / 12 ms, none 503, `/health`
         p50 5-7 ms; 4 concurrent 416 KB bodies finish 0.53 s apart (serialized).
       - Per-request cost dropped: 1.65 s to 0.50 s of CPU for the 416 KB body. A
         clean string is scanned once per request, not twice (the ingest check and
         the `scrub` layer share a per-request verdict memo). The format-character
         strip classifies only the distinct non-ASCII characters.
     - The server itself (fix wave 5, NEW-3). `python3 -m api` runs `src/serve.py`,
       modelled on detection-py's launcher. Default uvicorn (httptools) buffered a
       100–200 MB header unauthenticated and never closed idle or half-sent sockets.
       The launcher provides:
       - h11, with a 16 KiB incomplete-head cap;
       - a request-head deadline from connect and after each response (10 s);
       - a keep-alive timeout (5 s);
       - `limit_concurrency` (128).
       `InputLimits` adds an exact 16 KiB head check (431) and a body-read deadline
       (30 s, 408).
     - `tests/test_fix_wave4.py` runs every pattern (78) against hostile shapes and
       asserts under 50 ms per 100 KB. It checks that the scanners scale linearly to
       1 MB, and it runs a real uvicorn that must answer `/health` within 1 s while
       max-size hostile URLs and bodies arrive.
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
   - **Intake facts are bounded** (fix wave 6, a wave-5 leftover). Facts appended
     without a cap and the profile was rebuilt over all of them on every request, so
     repeated large submissions grew memory and latency without bound. A client holds
     at most `max_facts_per_client` (2,000) facts; a request that would pass it is
     refused whole with 409 (the message names the counts and the cap) before
     anything is scanned, flagged, recorded or stored. Each field keeps its latest
     `facts_history_per_field` (20) observations in arrival order, so restating a
     field that already has 20 replaces its oldest observation rather than adding
     one, and a profile rebuild is O(cap). The size the cap is checked against is
     the one the client would hold after that trimming (fix wave 7, NEW-6: wave 6
     checked stored + requested, before the trimming, so at the cap a correction to
     a field at its history limit was refused by a message that asked for exactly
     that; the 409 now reports `facts_after_request` and says what adds and what
     does not). Consequence, stated in the README: an observation that falls
     out of a field's history no longer counts in the profile (a conflict can be
     aged out by 20 newer consistent values), so a value that must count should be
     confirmed (`client_confirmed` / `contract` provenance), not repeated.
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
- A nudge counts only when the push to Andre is confirmed delivered (fix wave 2).
  `andre_nudged` is set on delivery only. An undelivered push, including a channel that
  raises, is counted in `andre_nudge_failures` and recorded as `promise_nudge_result`
  (`delivered`, `attempt`, `max_attempts`, `will_retry`). It is retried on the next tick,
  up to `andre_nudge_max_attempts` (default 3) per nudge. After that it stops, and the
  last record says `will_retry: false`. The breach nudge gets its own budget; while it
  is undelivered the commitment carries `breach_nudge_pending` and is retried after it
  is breached. Resolving the escalation clears it. Nudges never gate the client
  warning: it fires on time in the same tick whatever the push does. A ledger failure
  still stops the whole tick (record-first).
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
- A commitment exists in state only after its `client_commitment_made` record is
  written (fix wave 2). This follows the record-first rule: state reflects what did
  happen. If the push to Andre is delivered but that record fails, the operation stops
  with 503 `{"proceeded": true, "outside_effects_done": ["andre_push", ...]}`. The
  client gets no reply with a time. The escalation keeps `push_delivered: true`,
  `commitment_id: null` and `client_message_status` "held: ... the client has not been
  told a time". No commitment is stored, so Promise Keeper never nudges, warns or
  breaches on a promise the client never received. The time is only ever given in an
  API reply, so the commitment is made by the next operation whose reply carries it:
  a retry of the same operation, or a tick (`escalation_deliveries[].client_message`).
  Fix wave 3 closed the two wave-2 gaps:
  - A human-request message while one is open (or a retry of one that stopped part
    way) reuses the open escalation. It never pushes Andre a second briefing.
  - Retrying `start_client` completes it.
- An undelivered briefing is retried on tick (fix wave 3), like the nudge. Each retry
  writes `andre_push_retry_request` before the push and `andre_push_result`
  (`delivered`, `attempt`, `max_attempts`, `will_retry`) after it. Retries stop at
  `escalation_push_max_attempts` (default 3, initial push included) or when Andre
  acknowledges or resolves the escalation. On delivery, the commitment is made with a
  fresh time, because the client hears it now. With the real stand-in (push not
  wired), every escalation is tried 3 times and then waits in Andre's queue.
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
- Payments are tracked only for a creator whose activation is complete: vetting
  approved, gates 14 and 15 passed, payout account active (fix wave 4, owner ruling
  by the founder's operator: refuse). A W-9 alone isn't enough. Otherwise 409 with the
  reason, and nothing is recorded as a tracked payment.
- A date of birth before 1900-01-01, or one that makes the applicant older than 120
  on the server's age date (UTC−12), is a 422 before anything is recorded (fix wave 4).
  `0001-01-01` used to be approved.
  A date of birth after the server's date is also a 422 (fix wave 5, LOW-E).
  `9999-12-31` used to be accepted and declined as "under 18". The server's date here
  is the latest calendar date on Earth (UTC+14), so no real birth date is refused.
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
  - Verified in fix wave 4: before it, a restarted process answered the same
    `start_client` with a new set of ledger events (a random epoch per process).
    Now a restarted process's `start_client` or `apply_creator` derives the same
    ids and the ledger dedupes it (`test_i1_*`). Persistence isn't built: every
    other operation needs the state the restart lost.
  - One lock serializes operations. A long one (about 2 s for a 1 MiB hostile body)
    delays others, never `/health`.
- Auth is one shared bearer token, plus Andre's approval key for actions attributed to
  him: playbook changes, and acknowledging or resolving escalations. There's no other
  per-caller identity.
- The ledger keeps only `payload_sha256`. Onboarding doesn't persist the payloads, so
  the hashes can't be re-verified later.
  - Payloads are scrubbed before hashing, as defence in depth.
  - Multi-event operations aren't atomic: a failure mid-way leaves the earlier events.
    Event ids are deterministic, so a retry replays them (200) instead of duplicating
    them.
  - After a failed or unknown write, the identical retry resolves it (fix wave 5,
    NEW-4). Nothing forces the client to send it. If a different operation on the same
    subject completes first, the subject's sequence advances. Any record the earlier
    attempt left in the ledger then stays an orphan: a decision recorded for an action
    that was never committed. Ledger-rust has no lookup by event id, so the service
    can't tell afterwards. This was already true for certain mid-way failures.
  - A result record can fail after its outside effect has happened. The API reports
    that exactly (`proceeded: true, completed: false`). Since fix wave 3 the record is
    owed, not lost: the next operation on that subject writes it with the same id.
    It is lost only if the process restarts first, because state is in-process.
- Fix wave 1 ran live against the real ledger-rust (built from this tree) and the real
  detection-py. `tools/fake_ledger_server.py` still exists for quick local runs. It
  validates exactly like ledger-rust, and C1 controls count as control characters, as
  they do in Rust's `char::is_control`.

**Detection is pattern-based**
- Injection, guarantee and credential detection are rules over normalized text, which
  is best effort on free text.
  - A password that is an ordinary word, typed with no cue, can't be recognised. Some
    cued phrasings are still accepted at intake, for example "my password is correct
    horse battery staple" ("correct" reads as an ordinary answer). The stored copy is
    still redacted after the cue word, and nothing raw is kept.
  - The stored redacted copy is deliberately aggressive. Order numbers, phone numbers
    of 9+ digits, ISO timestamps and whatever follows "login" are also replaced in the
    stored messages and documents.
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

## Fix wave 22, Sep 28 2026 (AEGIS round 21; lead rulings G3, G6, G9) — tests and transport

- **G9 (N21-C-8, the `guardrails._DOLLAR` linearity flake).** The failing assertion was the harness's 10 KB → 100 KB
  ratio (20.2 against 20, `$1,` × N, under load). The pattern is linear (`\$\s?\d[\d,]*(?:\.\d+)?`: each match is
  3 characters, nothing is retried). The harness timed `[m.span() for m in p.finditer(s)]`: on 100 KB that is 33,333
  retained span tuples, which drive the interpreter's cyclic GC — 15 collections inside the timed call, each walking
  the growing list — so the ratio measured the harness's garbage. `tests/redos_harness.py` now drains the matches
  without keeping them (`_drain_matches`: every match produced and its span taken, as the service uses them) and
  times each run with the cyclic GC off; the 10 KB base is best-of-5 like the 100 KB run. Measured with
  `w22/g9_probe.py` (30 pairs each, the machine loaded): ratio median 13.0 / max 15.7 before, 9.8 / 13.5 after
  (ideal 10); 0 collections inside a timed 100 KB run after, 15 before. The bounds (50 ms / 100 KB, ratio 20) are
  unchanged. Honest caveat: the 30× loop of that test under three busy loops passed 30/30 on the wave-21 harness as
  well as on this one — the flake did not reproduce on this machine; the evidence is the distribution above.
  The same class, missed in that first sweep and caught by the 3.12 suite on 657f70e:
  `test_r1_scanners_linear_up_to_1mb[normalize]` failed once (100 KB 0.006 s, 1 MB 0.127 s: ratio 22.2 > 20), both
  points single samples. `normalize` is linear (20 single-sample pairs: median ratio 13.3, max 19.6; best of 3:
  max 14.5–15.1, on 3.12 and 3.13). The ratio's 100 KB base is now best of 3 and a 1 MB sample over the bound is
  re-measured best of 3 before it fails (bbcbbda; 48e1166 sampled every 1 MB point 3× and nearly tripled the test's
  time under load). Limits unchanged.
- **G3 (N21-C-6).** The round-21 review found `ledger-rust` still running hours after a suite: `proxied_stack`
  (`tests/test_fix_wave6.py`) started the ledger and then, outside any `try`, picked the API's port — a narrow
  `ONBOARDING_TEST_PORT_RANGE` whose ports sat in TIME_WAIT made `free_test_port()` skip, and the ledger was
  orphaned. `proxied_stack`, `RealStack` and the wave-5 `Stack` now start everything inside one try/finally (a
  failure or a skip at any step stops what was started), and `free_test_port()` probes with `SO_REUSEADDR`, as the
  servers bind, so a port in TIME_WAIT is free.
- **G6.** `serve.py`'s graceful close is the module shared by the ten Python services (`src/graceful_close.py`;
  ADR 0003 §9): the concurrency slot is given back before the drain, at most `ONBOARDING_DRAINS_MAX` (default 512)
  drain at once, reads are bounded to 16 KiB and drained bytes are discarded in one buffer.

## Contract observations (reported, not changed)

- **Section 2** defines `payload_sha256` but no home for the payload itself. Without
  one, a ledger event proves a payload existed but nobody can later show which. A
  payload store (or payload-in-ledger for non-sensitive fields) needs deciding.
- **Section 1** says "accept `str`" for money but doesn't say whether exponent strings
  (`"1e3"`) are valid. Onboarding rejects them, and since fix wave 3 accepts only the
  section 1a contract string (`fixtures/money_vectors.json`).
- detection-py now emits money as canonical strings, and the fix wave 1 live run
  consumed them. A legacy float finding is now rejected, not converted. Section 1 allows
  the float fallback for fixtures, but findings from another service are held to the
  canonical wire form.
