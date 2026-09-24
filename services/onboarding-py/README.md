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
    what it already holds, and the retry finishes the operation. A 503 from
    ledger-rust is its load-shed, sent before the request is read, so it
    counts as not recorded.
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
  - Heavy scans (bodies over 64 KiB) are capped: 2 run at once, up to 16
    wait, each for up to 30 s. Beyond that the API returns 503 with
    `Retry-After` and `proceeded: false` (busy, nothing done), never 422. A
    clean string is scanned once per request, not twice, and the format-character
    strip no longer does a Python step per character. The 416 KB body dropped
    from 1.6 s to 0.5 s of CPU.
  - `tests/test_fix_wave4.py` runs every pattern against hostile shapes (runs
    of `a`, `a@`, `a/`, `a:`, alternating classes, and each pattern's own
    literals). It asserts under 50 ms per 100 KB, and linear scaling to 1 MB
    for the whole scanners. A real-uvicorn test sends max-size hostile URLs
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
    vetting approved, gates 14 and 15 passed, payout account active. A W-9
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
- `limit_concurrency` (`ONBOARDING_LIMIT_CONCURRENCY`, default 128).

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
| `ONBOARDING_HEAVY_BODY_BYTES`, `ONBOARDING_HEAVY_SCAN_SLOTS`, `ONBOARDING_HEAVY_SCAN_MAX_WAITING`, `ONBOARDING_HEAVY_SCAN_WAIT_SECONDS` | Heavy-scan gate, defaults 65536, 2, 16, 30; busy is a 503 with Retry-After |
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
cd services/onboarding-py && python3 -m pytest -q     # 624 passed (fix wave 5, Sep 24 2026)
```

Tests are organised by certification type:

| File | Covers | Tests |
|---|---|---|
| `test_cert_scenario.py` | The three named scenarios plus full lane flows | 7 |
| `test_cert_attack.py` | The four named attacks plus the credential spray | 8 |
| `test_cert_guardrail.py` | Guardrails | 52 |
| `test_fix_wave1.py` | Fix wave 1 regressions (F1, F2, F4, F9, F10, F11, F14–F16, L1), including the record-first harness | 104 |
| `test_fix_wave2.py` | Fix wave 2: nudge counted only on delivery, bounded retries, warning unaffected (L2); no commitment without its record (L3) | 10 |
| `test_fix_wave3.py` | Fix wave 3: stage then commit and retries that finish (N2), owed result records (N7), credential shapes, redacted storage and the state-inspecting spray (N5), the shared money vectors (F15), the real-server access log (D1), human-request dedupe and briefing retry on tick | 183 |
| `test_fix_wave4.py` | Fix wave 4: linear-time scanning, input caps, off-loop validation, scan budget and a real-uvicorn `/health` test under attack (R1); owed audit rulings, no second detection call (A1); payments only after activation (P1); DOB plausibility (D1); restart-stable ids (I1). `redos_harness.py` builds the hostile inputs. | 115 |
| `test_fix_wave5.py` | Fix wave 5 covers four findings. NEW-2: the CPU-time budget, one scan per clean string, busy as 503 not 422, and 12 concurrent max-size bodies on a real server. NEW-3: real-socket header, idle, partial-head, trickled-head and trickled-body probes against `python3 -m api`. NEW-4: `proceeded: "unknown"` on a lost reply, then a retry with no duplicate. LOW-E: a future DOB. | 47 |
| `test_unit_*.py` | Unit tests | 84 |
| `test_auth_and_entrypoint.py` | Auth, docs, real-socket bind | 14 |

No test makes a network call outside loopback. The entrypoint test spawns
the real process on 127.0.0.1/127.0.0.2 and reads `/proc/net/tcp`. Tests
that start a real server bind only ports in `ONBOARDING_TEST_PORT_RANGE`
(default 19920–19939).
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
