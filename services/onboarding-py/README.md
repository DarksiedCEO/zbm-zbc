# services/onboarding-py — ZBM/ZBC Onboarding department

Client onboarding from signed contract to a fully running account, for three
lanes: **client** (Revenue Recovery, Digital Advertising, Out of Home),
**ZBC creator** (clippers) and **ZBC brand**. Built Sep 24 2026 from the
founder-locked spec. Architecture and decisions: `docs/adr/0004-onboarding-department-architecture.md`.

**Status: built and tested; NOT certified for any real client, clipper or
brand.** Certification needs four test types. This build supplies three
(scenario, attack, guardrail). The fourth, independent review, happens
outside this workstream. Nothing here is wired to a real platform, vault,
phone, or other department: every such dependency is a stand-in that fails
closed ("not allowed yet" / "not wired").

## The 15 intelligences (`src/intelligences/`, one module each)

| # | Module | Decides | Phase |
|---|---|---|---|
| 1 | `i01_client_understanding` | Profile; confidence per field from provenance; conflicts flagged, not resolved silently; short gap list with prefills (P13); drops fields the lane doesn't need | 1 |
| 2 | `i02_conversation` | Next question (versioned question bank, vertical wording); stuck/friction signal; reply intent (human, credential, guarantee, account change, Spanish); P6 recap | 1 |
| 3 | `i03_priority_fusion` | One merged plan in the client's order; disagreements shown side by side with a recommendation, applied only if the client chooses | 1 |
| 4 | `i04_platform_access` | Platforms to request (tag scan over supplied HTML); least-access role per job; steps routed to the login holder (P22); P21 grant verification with exact fixes | 1 |
| 5 | `i05_setup` | Tracking/dashboard/reporting proposals; a change needs the client's yes for that exact change | 2 |
| 6 | `i06_audit_baseline` | Baseline from Revenue Recovery findings (over HTTP); unlabeled findings rejected; double counts and uncertain causes excluded from totals; totals per classification | 1 |
| 7 | `i07_momentum_moment` | The one first win: provable only (observed/financially verified, high+, named cause, no double count), then scored | 2 |
| 8 | `i08_risk_anomaly` | Nothing / soft trigger / hard stop (fraud and ban signals; stated vs observed revenue beyond 25%) | 1 |
| 9 | `i09_promise_keeper` | "Today" vs "first thing tomorrow" (12:00 America/Los_Angeles); noon roll; nudge Andre; warn the client before a time passes; quiet hours in the client's own zone | 1 |
| 10 | `i10_escalation_briefing` | The briefing pack: exactly the 8 locked fields | 1 |
| 11 | `i11_creator_vetting` | Approve / decline / incomplete / send to Andre; under 18 is always declined, with no guardian path | 3 |
| 12 | `i12_brand_campaign` | Rented-first plan; owned posting offered as an add-on for regulated brands only; small proving campaign; approval per campaign; no scaling until proven | 3 |
| 13 | `i13_learning_loop` | Phase 1: log each escalation's snag and resolution (identifiers stripped). Phase 2: health score and early warning, P10 scorecard, proposed rules (need Andre's token) | 1 / 2 |
| 14 | `i14_contract_obligation` | Activation gate: terms available, signed, in term, plan and commitments inside the contract (drift), P23 clause present | 1 |
| 15 | `i15_compliance` | Activation gate: names every unmet requirement; the Compliance dept (38) stand-in rules "not allowed yet" | 1 |

`GET /intelligences` returns each module's number, phase and status. Every
status says it is not certified.

## What is enforced in code (and the test that proves it)

- **Activation needs both gates (14 and 15).** A blocked activation returns
  409 with the exact unmet list. No handoff or payout crossing happens until
  both pass. See `test_cert_guardrail.py::test_activation_*` and `test_gate_1[45]_fails_alone*`.
- **Record first, then act.** The record of every decision is written to the
  ledger before any outside effect: payout, handoff, contract storage, push to
  Andre, or data sent to Revenue Recovery. For example, `activation_ruling` is
  recorded before the payout or handoff.
  - If a record fails before any effect, nothing happens. The API returns 503
    `{"proceeded": false, "ledger_write": "not_recorded"}`.
  - If the record's fate is unknown (fix wave 5), the API never says "did not
    proceed". This covers a reply lost after the request was sent, a 5xx other
    than 503, and a 409. It returns 503
    `{"proceeded": "unknown", "ledger_write": "unknown", "retry": "..."}` with
    `Retry-After`. Staged state stays uncommitted. Retrying the identical
    request is safe: the ids are deterministic, so the ledger answers 200 for
    what it already holds, and the retry finishes the operation. Only
    ledger-rust's own load-shed answer counts as not recorded (fix wave 6):
    status 503 with exactly its body `{"error": "ledger-rust is at its
    connection limit; retry shortly"}`, which `shed()` writes before reading
    the request. Any other 503 (a proxy or gateway in between may have
    forwarded the request first) is unknown, like every other 5xx. This is
    the same rule as creative-py (`services/creative-py/src/shared/ledger.py`,
    ADR 0005); `test_fix_wave6.py` checks both against `bin/server.rs`.
  - If a result record fails after an effect, nothing further happens. The API
    returns 503 `{"proceeded": true, "completed": false, "outside_effects_done": [...]}`,
    with `ledger_write` set to `not_recorded` or `unknown`.
  - Bus publishes and memory writes happen only when the operation completes.
  - Stage then commit (fix wave 3): state that depends on a record (a new
    client, an escalation, a creator, an activation, a warning the client
    reads in the reply) becomes visible only after that record is written.
  - A result record that fails after its effect is owed. The next operation
    on that subject writes it with the same id, so an "already done" retry
    path can't skip it. Retrying `start_client` or `apply_creator` after a
    partial failure finishes the job instead of answering 409. See
    `test_fix_wave3.py::test_n2_*` and `test_n7_*`.
  - Event ids are deterministic, so a retry after a lost ledger response records
    nothing twice.
  - See `test_fix_wave1.py::test_f9_*`: every write position of 21 call sites is
    failed in turn. See also `test_f2_*`, `test_f11_*` and `test_ledger_*`.
- **No credential appears in any output.**
  - No model has a secret field, and inbound models reject unknown fields.
  - Every inbound string is normalized, then refused (422, "never send
    credentials") if it is credential-shaped.
    - Normalization: NFKC, zero-width characters removed, spaced letters
      collapsed, casefolded.
    - Keywords are multilingual.
    - Credential-shaped means: a password, PIN or OTP after its label, a
      `user / secret` pair, a password-like token next to a login word, a
      Luhn-valid card number, or a key or token shape. Since fix wave 3 it
      also means a URL with a password in it, "get in with X and Y", "use X
      to log in", `p@ss`/`p4ssw0rd`, an SSN, and a bank account, routing
      number or IBAN.
  - Raw client free text is never kept (fix wave 3). Messages, documents,
    fact values, a clipper's bio and resolution text are stored, and served,
    only as `redaction.redact_text(...)`. That replaces URL userinfo, SSNs,
    bank and card numbers, high-entropy tokens of 10+ characters, and
    everything after a login or password word with `[REDACTED]`.
  - Every response, error body, log record, ledger payload and memory write is
    also scrubbed.
  - Credential-looking IDs are refused, and 422 bodies don't echo input.
  - Unhandled errors log only the exception type. The vault stand-in refuses to
    store, and the credentials route never reads its body.
  - Proven by `test_credential_spray_never_leaks_anywhere`, a spray of 1,000+
    requests that includes every AEGIS miss, and by
    `test_fix_wave3.py::test_n5_credential_spray_zero_occurrences_anywhere`.
    The second covers the round-2 shapes and a `creds.txt` document. It
    checks responses, logs, the exit export, the ledger and the service's
    in-memory state, with and without the intake refusal and output scrub.
  - Log records keep their shape when scrubbed, so uvicorn's access lines
    work, with the request path redacted (`test_d1_*` runs the real server).
- **Hostile input can't stall the service** (fix wave 4, R1). AEGIS found the
  credential scanner quadratic: 20,000 `a` took 7.6 s, a 60 KB message held
  the process 132 s, and a 30 KB URL blocked `/health` for 20.8 s without auth.
  - Every regex in the service is linear-time on hostile input. The
    quadratic ones were rewritten (anchored at run starts, possessive or
    atomic groups, or a two-step scan) with the same results. A differential
    run against the old code over 240,000 random texts found no difference
    except where the new form redacts more (a URL scheme starting with a digit).
  - Caps come before any scan. Field lengths are checked first (`Inbound`
    validates `mode="after"`, and every inbound string has a `max_length`).
    A body over 1 MiB is a 413 (Content-Length, or counted as it arrives). A
    path plus query over 8 KiB is a 414. A log line is cut to a bounded prefix
    before it is scrubbed.
  - Scanning never runs on the event loop. Bodies are validated in a sync
    dependency (the threadpool), exception handlers are sync, and `/health`
    is `async` and does no work.
  - Each body's credential checks run under a CPU budget (fix wave 5). It
    counts the request thread's own CPU time (`time.thread_time`), not
    wall-clock time, so waiting behind other requests for the GIL costs
    nothing. The budget is 1 s plus 10 ms per KB of body, about 5x the worst
    cost measured (~1.8 ms/KB). A body that can't be checked within it is
    refused (422 `scan_budget_exceeded`). The old 5 s wall-clock budget refused
    5 of 5 concurrent benign 416 KB bodies.
  - Scanning concurrency is a weighted budget (`ScanAdmission`, fix wave 6;
    fix wave 5's step gate at 64 KiB let 40 concurrent 58 KB bodies bypass it:
    light GET p50 3.1 s, `/health` 2.4 s). Every body takes
    `max(size, ONBOARDING_SCAN_MIN_COST_BYTES)` of `ONBOARDING_SCAN_INFLIGHT_BYTES`
    while it is checked, so many medium bodies are throttled like one large
    one. The defaults are measured, not derived: under one GIL each extra
    CPU-bound scan thread lengthens the event loop's wait for the GIL (40x16 KB
    bodies at 4 concurrent scans: `/health` p50 300 ms, light GET p50 790 ms;
    at 1: 16 ms / 70 ms), so the minimum cost equals the 64 KiB budget and
    large scans run one at a time. Up to 16 wait, each up to 30 s. Beyond
    that the API returns 503 with `Retry-After` and `proceeded: false` (busy,
    nothing done), never 422. Measured with the defaults on a real uvicorn:
    40 concurrent 58 KB bodies, `/health` p50 20 ms / p95 42 ms, light GET
    p50 27 ms. A clean string is scanned once per request, not twice, and
    the format-character strip no longer does a Python step per character.
    The 416 KB body dropped from 1.6 s to 0.5 s of CPU.
  - Small bodies have a lane of their own (`ScanLanes`, fix wave 7, NEW-5).
    With one scan at a time, every body waited its turn: one client's 416 KB
    bodies back-to-back put other clients' 40-byte messages at p50 294 ms (4
    uploaders 1.6 s, 12 uploaders 6 s), and once 16 large bodies were queued
    a tiny message was 503 — while the wave-6 doc claimed a small body never
    queues behind a large one. A body of at most
    `ONBOARDING_SCAN_SMALL_BODY_BYTES` (16 KiB; its scan is ~1 ms) now takes
    the small lane: `ONBOARDING_SCAN_SMALL_INFLIGHT` (1) at a time, at most
    `ONBOARDING_SCAN_SMALL_MAX_WAITING` (8) waiting in its own queue, the same
    30 s wait. It never waits for the large lane, and a flood of large bodies
    cannot fill its queue; large bodies stay serialized. Three things that
    made the same small message slow were fixed with it: the service redacted
    a large body's text again UNDER ITS LOCK (~1.5 s of CPU for 416 KB of
    profile fields, every other operation waiting) — that work is pure and
    now runs before the lock is taken, and the large lane is held through the
    handler so it stays one-at-a-time; one verdict memo now spans the whole
    request (body check, service redaction, response scrub), so a clean
    string is scanned once, not three times; and the launcher sets the
    interpreter's thread switch interval to 1 ms
    (`ONBOARDING_SWITCH_INTERVAL_SECONDS`), so a light request's several GIL
    turns beside a CPU-bound scan cost ~1 ms each, not ~5 ms (the scan pays
    +3-4% of CPU for it under contention). Measured on a real uvicorn and a
    real ledger-rust (`test_fix_wave7.py`, the AEGIS `onb_serial6` shape):
    small messages beside 1 / 4 / 12 clients uploading 416 KB bodies
    back-to-back are p50 17 / 24 / 12 ms, none 503, `/health` p50 5-7 ms, and
    4 concurrent 416 KB bodies finish 0.53 s apart (one scan each).
  - Intake facts are bounded per client (fix wave 6): at most
    `ONBOARDING_MAX_FACTS_PER_CLIENT` (2,000) stored facts, and each field
    keeps its latest `ONBOARDING_FACTS_HISTORY_PER_FIELD` (20) observations.
    A facts request that would pass the cap is refused whole with 409 (the
    message names the counts and the cap; nothing is scanned, flagged,
    recorded or stored), so a profile rebuild is O(cap) and repeated large
    submissions cannot grow memory. The size that counts is the one the
    client would hold AFTER the per-field trimming (fix wave 7, NEW-6: it was
    stored + requested, so at the cap a restatement of a field at its history
    limit — the correction the message asked for — was refused): restating a
    field that already has 20 observations replaces its oldest and adds
    nothing, restating one with fewer adds one, a new field adds one, and the
    409 body says so (`facts_after_request`). Older observations of a field
    fall out of the profile, so a value that must count should be confirmed,
    not repeated.
  - `tests/test_fix_wave4.py` runs every pattern against hostile shapes (runs
    of `a`, `a@`, `a/`, `a:`, alternating classes, and each pattern's own
    literals). It asserts under 50 ms of CPU per 100 KB. Fix wave 6 (an
    AEGIS run under a cargo build measured 5.4 ms against the 5 ms/10 KB
    bound): the harness times the thread's own CPU, not wall-clock time, the
    bounds are scaled by a slowdown factor measured on a known linear regex
    right before each pattern (never below 1x, never above 8x), and a
    machine-independent linearity ratio is asserted too (10x the input may
    cost at most 20x the CPU; since fix wave 23 a ratio over that is
    re-measured before it fails against ten 10 KB inputs timed back to back,
    the same work and duration as the 100 KB run, at most 2x — a single ~1 ms
    run measured the scheduler). Under three CPU-burning processes on a 2-vCPU
    box the old test failed six patterns; the new one passes all 78. It also
    asserts linear scaling to 1 MB for the whole scanners. A real-uvicorn test sends max-size hostile URLs
    and bodies while polling `/health`, which must answer within 1 s.
- **Only Andre can make Andre's decisions.**
  - Acknowledging or resolving an escalation needs his approval token
    (`approval_token`: HMAC-SHA256 keyed by `ONBOARDING_ANDRE_APPROVAL_KEY` over
    the exact action; see `memory.andre_action_token`).
  - The shared service token alone gets 403, and so does a token for another
    escalation, action or text. The same mechanism covers playbook changes.
- **Guarantee filter on all outbound text** (`guardrails.check_outbound`).
  It blocks guarantees, `100%`, and unlabeled dollar figures ($-figures
  without the LabeledValue suffix, or "N dollars"/"USD N").
- **Client content is data.** Injection text in a website, bio, document,
  message or fact is flagged, logged, ledgered and published, but no
  decision reads the flags. Tests compare outcomes with and without the
  injection text.
- **Honest identity.** The first message in every lane says it's an AI and
  offers Andre. The wording is pending counsel, so the Compliance gate stays
  unmet on it.
- **Escalation.**
  - Hard triggers escalate at once, with no agent attempt: the client asks
    for a human, or the deal size is over the threshold. The threshold is
    unset, so every deal escalates and the reason says so.
  - Soft triggers get one resolution attempt, then escalate if unresolved:
    stuck past 48h (configurable), friction, audit anomaly, and a recommend
    score of 6 or below.
  - The briefing is pushed before Andre engages. If the push isn't
    delivered, the client gets no promised time.
  - An undelivered briefing is retried on tick, up to
    `escalation_push_max_attempts` (3, first push included), with every
    attempt recorded. Once it is delivered, the commitment is made and
    returned in `escalation_deliveries`.
  - A second human request while one is open doesn't page Andre again.
- **Time.** All time comes from the service clock. No request can set "now",
  or any date a decision is evaluated against.
  - There is no `applied_on` and no `paid_on`; sending either is a 422.
  - Clipper age is computed on the server's date at UTC−12, the most
    conservative date anywhere on Earth.
  - The contract term and the 1099 tax year use the server's America/Los_Angeles
    date.
  - A future `observed_at`, `account_last_activity_at` or `signed_at` is a 422.
  - The tests cover exactly noon, spring-forward and fall-back, and quiet hours
    in the client's time zone.
- **Money is exactly the contract string** (ADR 0003 section 1a; fix wave 3).
  - Findings, request money and configuration accept only
    `^(0|[1-9][0-9]{0,14})\.[0-9]{2}$`, below 10^15. `"12.3"`, `"12"` and any
    JSON number (`0.1`, `12`) are 422. Nothing is rounded.
  - Checked against every vector in `fixtures/money_vectors.json`, in the
    model and over HTTP.
- **Other rules.**
  - Momentum never picks an unproven win.
  - The recommend score is asked only after the first real win.
  - Client memory is walled per client: client A's markers never reach client B.
  - Institutional memory strips identifiers.
  - Playbook changes need an HMAC approval token keyed to Andre, and every
    version is kept.
  - Clippers under 18 are declined. A guardian field is rejected with 422.
  - The 1099 threshold is configuration ($2,000.00 for 2026). An
    unconfigured year is treated as reportable.
  - A W-9 must be on file before payout activation.
  - Payments are tracked only for a creator whose activation is complete:
    vetting approved, gates 14 and 15 both passed, payout account active. A W-9
    alone isn't enough. Otherwise the answer is 409 with the reason, and
    nothing is recorded (fix wave 4, owner ruling: refuse).
  - A date of birth before 1900, or one that makes the applicant older than
    120 on the server's date, is a 422 (fix wave 4). `0001-01-01` used to be
    approved.
  - Spanish is off, and turning it on refuses startup.

## Run

```bash
cd services/onboarding-py
pip install -r requirements.txt
export ONBOARDING_SERVICE_TOKEN=<shared secret>            # required; refuses to start without it
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger token>   # else every recorded action -> 503
export DETECTION_SERVICE_URL=http://127.0.0.1:8000 DETECTION_SERVICE_TOKEN=<ZBM_SERVICE_TOKEN>  # else audit -> 502
cd src && python3 -m api        # ONBOARDING_BIND_ADDR (default 127.0.0.1), ONBOARDING_PORT (default 8200)
```

`python3 -m api` runs the hardened launcher, `src/serve.py` (fix wave 5,
NEW-3). Don't start it with a plain `uvicorn api:app`: default uvicorn uses
httptools, which buffered a 100–200 MB header and never closed idle or
half-sent connections. The launcher provides:
- the h11 parser, with the request head capped at 16 KiB (431 or 400);
- a request-head deadline counted from connect and after every response
  (`ONBOARDING_REQUEST_HEAD_TIMEOUT_SECONDS`, default 10);
- a keep-alive idle timeout (`ONBOARDING_KEEP_ALIVE_TIMEOUT_SECONDS`, default 5);
- `limit_concurrency` (`ONBOARDING_LIMIT_CONCURRENCY`, default 128);
- a 1 ms interpreter thread switch interval
  (`ONBOARDING_SWITCH_INTERVAL_SECONDS`, fix wave 7): light requests get
  their GIL turns beside a CPU-bound body scan promptly. Since fix wave 25
  only 0.0001 .. 0.05 s starts (it was any positive number), and the
  interval in force is checked and printed before serving.

The body must arrive within `ONBOARDING_BODY_READ_TIMEOUT_SECONDS` (default
30), or the API returns 408.

Optional configuration (all open items have fail-closed defaults; see `src/config.py`):

| Variable | What it sets |
|---|---|
| `ONBOARDING_DEAL_SIZE_THRESHOLD_USD` | Deal-size threshold, in the contract money form (e.g. `5000.00`) |
| `ONBOARDING_STUCK_WINDOW_HOURS` | Stuck window |
| `ONBOARDING_SOFT_RESOLUTION_WINDOW_HOURS` | Wait after the one soft-trigger attempt |
| `ONBOARDING_COMMITMENT_CUTOFF`, `ONBOARDING_COMMITMENT_TZ` | Noon cutoff and its time zone |
| `ONBOARDING_1099_THRESHOLDS` | JSON map of year to threshold, amounts as contract money strings (`{"2026": "2000.00"}`) |
| `ONBOARDING_P1_WORDING_COUNSEL_APPROVED`, `ONBOARDING_P23_CLAUSE_COUNSEL_APPROVED` | Counsel sign-offs |
| `ONBOARDING_ANDRE_APPROVAL_KEY` | Andre's approval key (playbook changes, acknowledging and resolving escalations); unset means none of them is possible |
| `ONBOARDING_CONTRACT_STORAGE=in_memory` | Local demos only; not the decided storage |
| `ONBOARDING_MAX_BODY_BYTES` | Request body cap, default 1048576 (1 MiB); over it is a 413 |
| `ONBOARDING_MAX_REQUEST_TARGET_BYTES` | Path plus query cap, default 8192; over it is a 414 |
| `ONBOARDING_SCAN_BUDGET_SECONDS`, `ONBOARDING_SCAN_CPU_MS_PER_KB` | CPU budget for one body's credential checks: seconds plus ms per KB, defaults 1 and 10 |
| `ONBOARDING_SCAN_INFLIGHT_BYTES`, `ONBOARDING_SCAN_MIN_COST_BYTES`, `ONBOARDING_SCAN_MAX_WAITING`, `ONBOARDING_SCAN_WAIT_SECONDS` | Scan admission budget: in-flight scanned bytes, per-body minimum cost, waiters, wait; defaults 65536, 65536 (one scan at a time; see above), 16, 30; busy is a 503 with Retry-After |
| `ONBOARDING_SCAN_SMALL_BODY_BYTES`, `ONBOARDING_SCAN_SMALL_INFLIGHT`, `ONBOARDING_SCAN_SMALL_MAX_WAITING` | The small lane (fix wave 7): bodies up to this size (default 16384; 0 disables the lane) are admitted separately, this many at a time (1), with this many waiting (8) |
| `ONBOARDING_MAX_FACTS_PER_CLIENT`, `ONBOARDING_FACTS_HISTORY_PER_FIELD` | Stored intake facts per client (default 2000; a request past it is a 409) and observations kept per field (default 20) |
| `ONBOARDING_INTAKE_CHANNEL` | Read into the config (`intake_channel`) and used by nothing yet: the intake channel (chat, voice or both) is an undecided spec item (ADR 0004, open items) |
| `ONBOARDING_SPANISH_ENABLED` | Must stay off: a true value (`1`, `true`, `yes`, `on`) refuses startup — Spanish (P24) is wave 2/3 and no reviewed Spanish content exists |
| `ONBOARDING_INSTANCE_ID` | Stable instance id in the event ids of `start_client` and `apply_creator`, default `onboarding-1`. Give each concurrently running instance its own. |

Live runs should use the real ledger-rust (`cargo build --release` in
`services/ledger-rust`, then `LEDGER_SERVICE_TOKEN=... LEDGER_PORT=... target/release/server`).
`tools/fake_ledger_server.py` is a small stdlib fake of `POST /ledger/events`
for quick local runs. It validates exactly like ledger-rust: C1 controls are
control characters there too, ids are fullmatched, and it follows the same
200/409 rules. It is not the real ledger.

## Routes

Every route except `/health` needs `Authorization: Bearer <token>`.
`/docs`, `/redoc` and `/openapi.json` are disabled.

| Route group | What it does |
|---|---|
| `POST /onboarding/clients` | Contract signed: first message (P1), access link routed (P22), contract stored, deal-size ruling. Also the front end for the brand lane (`lane: zbc_brand`). |
| `POST /onboarding/clients/{id}/intake/facts`, `/intake/documents`, `/messages`, `/recap` | Intake conversation |
| `POST /onboarding/clients/{id}/access/website-scan`, `/access/grants`, `/access/credentials` | Access. The credentials route always refuses. |
| `POST /onboarding/clients/{id}/audit`, `/plan`, `/plan/choices` | Audit via Revenue Recovery, then the merged plan |
| `POST /onboarding/clients/{id}/setup-plan`, `/account-changes`, `/momentum`, `/first-win`, `/recommend-score` | Setup, account changes, first win, recommend score |
| `POST /onboarding/clients/{id}/tick` | Stuck check and Promise Keeper |
| `POST /onboarding/clients/{id}/issues/{iid}/outcome`, `/escalations/{eid}/acknowledge`, `/escalations/{eid}/resolve` | Soft-trigger outcomes and escalations. Acknowledge and resolve need Andre's `approval_token` in the body; without it, 403. |
| `GET /onboarding/escalations` | Andre's queue |
| `GET /onboarding/clients/{id}`, `GET .../health`, `DELETE .../memory` | Client view, health score, memory deletion |
| `POST .../exit` | P4 clean exit |
| `POST .../activate` | Runs gates 14 and 15, then the handoff |
| `POST /zbc/creators/applications`, `/zbc/creators/{id}/w9`, `/disclosure-training`, `/activate`, `/payments`, `/posts/check` | Creator lane |
| `POST /zbc/brands/{id}/campaigns`, `.../{cid}/approve`, `.../{cid}/proving-result` | Brand lane |
| `GET /playbook`, `POST /playbook/rules`, `GET /learning/proposals` | Playbook and learning loop |

## Tests

```bash
cd services/onboarding-py && python3 -m pytest -q     # current counts: docs/test-counts.md (generated by CI)
```

The live tests against the REAL ledger-rust (`test_fix_wave6.py`,
`test_fix_wave7.py`) run by default: a session fixture (`ledger_bin`,
`tests/conftest.py`) builds it with `cargo build --release --bin server`
into `CARGO_TARGET_DIR` if set, else `services/ledger-rust/target` (both
git-ignored; a warm build takes under a second), taking the artifact path
from cargo's own `--message-format=json` output rather than guessing it
(fix wave 8, N7-6: the guess ignored `CARGO_TARGET_DIR`, so the run failed
with "exit 0" or silently used a stale binary), or uses the binary named
by `ONBOARDING_LEDGER_RUST_BIN`. Cargo runs on every session, so a binary
older than the sources is always rebuilt. They skip only when cargo is not
on PATH (or the named binary is missing, or the only binary without cargo
is older than the sources), and then say so: `pytest.ini` sets `-rs`, so
every skip's reason is in the summary (fix wave 7; before, they skipped
silently). A failed build is a failure, not a skip. With cargo present the
one expected skip is `tests/test_procinfo.py`'s IPv6 check on a machine
with no `::1` loopback (fix wave 25, scout A O11: this said "0 skipped").

Tests are organised by certification type (per-file counts are not kept here — fix wave 25, scout A O10: the
hand-written column had drifted and four files were missing; `docs/test-counts.md` has the generated totals):

| File | Covers |
|---|---|
| `test_cert_scenario.py` | The three named scenarios plus full lane flows |
| `test_cert_attack.py` | The four named attacks plus the credential spray |
| `test_cert_guardrail.py` | Guardrails |
| `test_fix_wave1.py` | Fix wave 1 regressions (F1, F2, F4, F9, F10, F11, F14–F16, L1), including the record-first harness |
| `test_fix_wave2.py` | Fix wave 2: nudge counted only on delivery, bounded retries, warning unaffected (L2); no commitment without its record (L3) |
| `test_fix_wave3.py` | Fix wave 3: stage then commit and retries that finish (N2), owed result records (N7), credential shapes, redacted storage and the state-inspecting spray (N5), the shared money vectors (F15), the real-server access log (D1), human-request dedupe and briefing retry on tick |
| `test_fix_wave4.py` | Fix wave 4: linear-time scanning, input caps, off-loop validation, scan budget and a real-uvicorn `/health` test under attack (R1); owed audit rulings, no second detection call (A1); payments only after activation (P1); DOB plausibility (D1); restart-stable ids (I1). `redos_harness.py` builds the hostile inputs. |
| `test_fix_wave5.py` | Fix wave 5 covers four findings. NEW-2: the CPU-time budget, one scan per clean string, busy as 503 not 422, and 12 concurrent max-size bodies on a real server. NEW-3: real-socket header, idle, partial-head, trickled-head and trickled-body probes against `python3 -m api`. NEW-4: `proceeded: "unknown"` on a lost reply, then a retry with no duplicate. LOW-E: a future DOB. |
| `test_fix_wave6.py` | Fix wave 6 (AEGIS round 5). N6: only ledger-rust's exact shed body is "not recorded", any other 5xx is unknown, the rule matches creative-py and `server.rs`; live against the REAL ledger-rust (`LEDGER_MAX_CONNECTIONS=1` shed, and an intermediary's 503 that hid a real append). N4: the weighted `ScanAdmission` (cost floor and cap, waiters, no over-admission in-process) and the live 40x58 KB flood with `/health` and light-GET bounds. Facts caps: 409 past the cap with nothing stored, latest-N per field, bounded cost over 30 large submissions. The live tests use the ledger-rust binary the `ledger_bin` fixture builds (see above). |
| `test_fix_wave7.py` | Fix wave 7 (AEGIS round 6). NEW-5: the small lane (config, routing, a tiny message answered while the large lane is held and its queue full, no 503 for small bodies under a large flood, the large lane held through the handler and no redaction under the service lock, one scan per string per request, the launcher's switch interval) and, live against the real ledger-rust, the `onb_serial6` scenario with 1 / 4 / 12 uploaders (small p50 < 50 ms, none 503) and 4 concurrent 416 KB bodies still serialized. NEW-6: a correction to a field at its history limit accepted at the cap, the boundary of the post-trim count, refusals still cheap. Skips: the binary is built by the fixture and a skip's reason is printed. |
| `test_fix_wave8.py` | Fix wave 8 (AEGIS round 7). N7-6: the ledger-rust fixture resolves the binary from cargo's reported artifact path: `CARGO_TARGET_DIR` set with no binary at the crate's default path, `CARGO_TARGET_DIR` set with a stale binary planted at the default path (not used), a source change after the build rebuilds, `CARGO_TARGET_DIR` unset uses the crate default, without cargo a fresh binary under `CARGO_TARGET_DIR` is used and one older than the sources is a printed skip, a broken crate fails with cargo's error (never "exit 0"). Driven in a fresh interpreter against a copy of the crate in pytest's temp dir. |
| `test_unit_*.py` | Unit tests |
| `test_auth_and_entrypoint.py` | Auth, docs, real-socket bind |
| `test_compliance38_client.py` | The thin HTTP client for Compliance (38) and its bind rulings (ADR 0006) |
| `test_fix21_graceful_close.py`, `test_live_graceful_close_module.py` | The graceful close shared by the ten Python services (answer, then a bounded drain) |
| `test_procinfo.py` | The portable socket-table and process helpers the live tests use (one IPv6 check skips without `::1`) |
| `test_fix25_switch_interval.py` | Fix wave 25: `ONBOARDING_SWITCH_INTERVAL_SECONDS` only within 100 us .. 50 ms, the interval in force checked in whole microseconds and printed |
| `test_fix25_fake_ledger_matches_rust.py` | Fix wave 25: one set of accept/reject cases put to the REAL ledger-rust, `tools/fake_ledger_server.py` and `ledger.ledger_rust_accepts`; all three agree |
| `test_fix25_test_token.py` | Fix wave 25: the suite uses its own token even when the shell exports `ONBOARDING_SERVICE_TOKEN` |

No test makes a network call outside loopback. The entrypoint test spawns
the real process on 127.0.0.1/127.0.0.2 and reads `/proc/net/tcp`. Tests
that start a real server bind OS-assigned ports, or only ports in
`ONBOARDING_TEST_PORT_RANGE` ("lo-hi") when it is set (fix wave 25: the
literal default 19920–19939 is gone, and an exhausted range fails instead of
skipping).
**Independent review (type 4) is not part of this build.**

## Known gaps and stand-ins, stated plainly

See ADR 0004 "Honest gaps" for the full list. The main ones:

- **Nothing live is wired.**
  - Platform probes (so no grant is ever "usable", and P21 can't pass
    live), site fetching and platform writes aren't built.
  - The vault refuses to store anything. There is no tier-2 or tier-3 access.
  - Push to Andre's phone isn't wired, so escalations queue at
    `GET /onboarding/escalations` after 3 recorded tries, and the client gets
    no promised time.
  - Not built, each behind a fail-closed stand-in: Compliance 38,
    Verification and Integrity, Billing, ZBC payouts/tax, handoff intake
    (RR/DA/Fulfillment), contract storage.
- **Platform steps and receipts aren't sent.** The shipped Google Ads, Meta
  and Shopify facts are drafts that were never verified, so steps and
  receipts are held from clients. There is no route to mark them verified.
- **State is in-process memory** and is lost on restart. It's one process
  with one lock, so a long operation (up to about 2 s for a 1 MiB hostile
  body) delays others, though never `/health`.
  - After a restart, a retried `start_client` or `apply_creator` derives
    the same event ids (from `ONBOARDING_INSTANCE_ID`), so the ledger
    dedupes it (fix wave 4). Every other operation needs the state the
    restart lost (404); their ids also carry a per-process boot id, so a new
    event after a restart never takes the id of an old one.
  - An owed result record is still lost if the process restarts before the
    next operation writes it. Persistence isn't built.
- **Auth is one shared bearer token**, plus Andre's HMAC approval key for the
  actions attributed to him: playbook changes, and acknowledging or resolving
  escalations. There's no other per-caller identity.
- **Ledger payloads are hashed but not kept.** The ledger stores only
  `payload_sha256`, and this service doesn't persist the payload, so a hash
  can't be re-verified later. This also appears under contract items in the
  ADR.
- **Multi-event operations aren't atomic.** A ledger failure mid-operation
  leaves earlier events recorded. Ids are deterministic, so a retry replays
  them instead of duplicating them. A result record can fail after its effect;
  the API reports exactly which effects happened, and the next operation on
  that subject writes the owed record. It is lost only if the process restarts
  first.
- **Detection is pattern-based.** Guarantee, injection and credential
  detection are rules over normalized text: best effort for free text.
  - A password that is an ordinary word with no cue can't be recognised.
    Some cued phrasings pass intake ("my password is correct horse battery
    staple"). Only the redacted copy is kept.
  - The redacted copy of messages and documents is deliberately aggressive:
    long digit runs, ISO timestamps and whatever follows "login" are
    replaced too.
  - A standalone 13–19 digit Luhn-valid number is refused as a card.
  - The structural defences above don't depend on these rules.
- **Several values are drafts** for Andre to tune through the playbook: the
  vetting thresholds, momentum traits, health weights, proving-campaign size
  and the P9 caption rule.
