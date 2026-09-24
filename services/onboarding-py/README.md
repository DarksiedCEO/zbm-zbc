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
- **Every crossing and gate ruling is written to the ledger first.** If the
  write fails, the action stops and state is unchanged. The API returns 503
  `{"proceeded": false}`. See `test_ledger_*`.
- **No credential appears in any output.** No model has a secret field.
  Inbound models reject unknown fields and scrub every string at ingest.
  Credential-looking IDs are refused. 422 bodies don't echo input. Unhandled
  errors log only the exception type. Every log record is scrubbed. The vault
  stand-in refuses to store. The credentials route never reads its body.
  Proven by a spray of 150+ requests (`test_credential_spray_never_leaks_anywhere`),
  which was mutation-checked: disabling either ingest scrubbing or the 422
  sanitizer makes it fail.
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
- **Time.** All time comes from the service clock. No request can set "now".
  The tests cover exactly noon, spring-forward and fall-back, and quiet hours
  in the client's time zone.
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

Optional configuration (all open items have fail-closed defaults; see `src/config.py`):

| Variable | What it sets |
|---|---|
| `ONBOARDING_DEAL_SIZE_THRESHOLD_USD` | Deal-size threshold |
| `ONBOARDING_STUCK_WINDOW_HOURS` | Stuck window |
| `ONBOARDING_SOFT_RESOLUTION_WINDOW_HOURS` | Wait after the one soft-trigger attempt |
| `ONBOARDING_COMMITMENT_CUTOFF`, `ONBOARDING_COMMITMENT_TZ` | Noon cutoff and its time zone |
| `ONBOARDING_1099_THRESHOLDS` | JSON map of year to threshold |
| `ONBOARDING_P1_WORDING_COUNSEL_APPROVED`, `ONBOARDING_P23_CLAUSE_COUNSEL_APPROVED` | Counsel sign-offs |
| `ONBOARDING_ANDRE_APPROVAL_KEY` | Playbook approval key; unset means the playbook can't change |
| `ONBOARDING_CONTRACT_STORAGE=in_memory` | Local demos only; not the decided storage |

For a local live run without ledger-rust's events endpoint,
`tools/fake_ledger_server.py` is a small stdlib fake of `POST /ledger/events`
(contract section 2). It is not the real ledger.

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
| `POST /onboarding/clients/{id}/issues/{iid}/outcome`, `/escalations/{eid}/acknowledge`, `/escalations/{eid}/resolve` | Soft-trigger outcomes and escalations |
| `GET /onboarding/escalations` | Andre's queue |
| `GET /onboarding/clients/{id}`, `GET .../health`, `DELETE .../memory` | Client view, health score, memory deletion |
| `POST .../exit` | P4 clean exit |
| `POST .../activate` | Runs gates 14 and 15, then the handoff |
| `POST /zbc/creators/applications`, `/zbc/creators/{id}/w9`, `/disclosure-training`, `/activate`, `/payments`, `/posts/check` | Creator lane |
| `POST /zbc/brands/{id}/campaigns`, `.../{cid}/approve`, `.../{cid}/proving-result` | Brand lane |
| `GET /playbook`, `POST /playbook/rules`, `GET /learning/proposals` | Playbook and learning loop |

## Tests

```bash
cd services/onboarding-py && python3 -m pytest -q     # 151 passed (Sep 24 2026)
```

Tests are organised by certification type:

| File | Covers | Tests |
|---|---|---|
| `test_cert_scenario.py` | The three named scenarios plus full lane flows | 7 |
| `test_cert_attack.py` | The four named attacks plus the credential spray | 8 |
| `test_cert_guardrail.py` | Guardrails | 52 |
| `test_unit_*.py` | Unit tests | 70 |
| `test_auth_and_entrypoint.py` | Auth, docs, real-socket bind | 14 |

No test makes a network call outside loopback. The entrypoint test spawns
the real process on 127.0.0.1/127.0.0.2 and reads `/proc/net/tcp`.
**Independent review (type 4) is not part of this build.**

## Known gaps and stand-ins, stated plainly

See ADR 0004 "Honest gaps" for the full list. The main ones:

- **Nothing live is wired.**
  - Platform probes (so no grant is ever "usable", and P21 can't pass
    live), site fetching and platform writes aren't built.
  - The vault refuses to store anything. There is no tier-2 or tier-3 access.
  - Push to Andre's phone isn't wired, so escalations queue at
    `GET /onboarding/escalations` and the client gets no promised time.
  - Not built, each behind a fail-closed stand-in: Compliance 38,
    Verification and Integrity, Billing, ZBC payouts/tax, handoff intake
    (RR/DA/Fulfillment), contract storage.
- **Platform steps and receipts aren't sent.** The shipped Google Ads, Meta
  and Shopify facts are drafts that were never verified, so steps and
  receipts are held from clients. There is no route to mark them verified.
- **State is in-process memory** and is lost on restart. It's one process
  with one lock.
- **Auth is one shared bearer token.** Any token holder can acknowledge or
  resolve escalations. Only playbook changes are bound to Andre, through the
  HMAC approval key.
- **Ledger payloads are hashed but not kept.** The ledger stores only
  `payload_sha256`, and this service doesn't persist the payload, so a hash
  can't be re-verified later. This also appears under contract items in the
  ADR.
- **Multi-event operations aren't atomic.** A ledger failure mid-operation
  leaves earlier events recorded without the action. That's the safe
  direction, but it's noise. Event ids are random per attempt.
- **Detection is pattern-based.** Guarantee, injection and credential
  detection are regexes: best effort for free text. The structural defences
  above don't depend on them.
- **Several values are drafts** for Andre to tune through the playbook: the
  vetting thresholds, momentum traits, health weights, proving-campaign size
  and the P9 caption rule.
