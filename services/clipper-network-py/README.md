# Clipper Network (CN) — `services/clipper-network-py`

The department that recruits, admits, tiers, equips, messages, disciplines
and offboards ZBC clippers, from application to exit. Built Sep 26, 2026
from the locked Clipper Network spec (rev 1). Architecture and every choice
made where the spec was silent: `docs/adr/0008-clipper-network-architecture.md`.

**Not certified for any real clipper.** Verification and Integrity,
Compliance (38), Creative Production, Finance (31), Legal (37), People (43),
the messaging provider, the clipper hub and the push channel are fail-closed
stand-ins unless a thin client is fully configured (V&I, Compliance,
Creative) — so on day one no clipper can be admitted, enrolled or messaged.
That is the intended day-one effect, not a bug. No LLM calls; every rule is
deterministic; every message is an Andre-approved, versioned template.

It never pays, never counts or verifies views, never drafts legal text and
never judges a clip. It stores no DOB, ID document, tax data, legal name, IP
address, payment detail or money value.

## What it decides

- **Admission** (`POST /cn/v1/clippers/{id}/admission`): all ten §C.2 checks,
  every time, each asking the owning department (V&I age / identity /
  connections / integrity, Compliance jurisdiction + `zbc_creator`
  activation, Legal agreement version, Finance tax-form status). A tick box
  never counts as age assurance; a caller's `age_verified` is a 422.
- **Tier** (`POST /cn/v1/tiers/run`): T0–T3 from V&I certifications and the
  strike mirror only; T3 by Andre's nomination.
- **Enrolment** (`POST /cn/v1/campaigns/{id}/enrolments`): §C.4, then
  Creative's signed kit of the live rulebook is delivered by reference.
- **Messages**: only from approved templates, only to members or opted-in
  people, only inside the recipient-local quiet window (08:00–20:00).
- **Discipline** (`POST /cn/v1/discipline/sync`): V&I strikes whose
  evidence resolves at V&I → warning / suspension / ban proposal; a ban only
  on Andre's approval (`POST /cn/v1/clippers/{id}/ban-decision`).
- **Disputes**: admissibility, routing and SLA; the outcome is a person's.
- **Offboarding**: the P4 clean exit — access revoked, open payouts flagged
  to Finance (it cannot close while Finance has open items), export,
  deletion of contact data after the retention period.

Every answer that refuses something lists `unmet` items
`{code, rule_id, message, source, evidence_ref}` and `unmet_lines`
(`cn/{rule_id}/{code}: {message}`); every rule id is one in force. Every
decision is recorded on the ledger (`department: clipper_network`) and in
the hash-chained local log **before** it takes effect; otherwise 503
`{"issued": false}` and nothing changed.

## Module map (`src/`)

| Module | Role |
|---|---|
| `api.py` | FastAPI app: bearer, caller, Andre and delegate identities; request limits; routes; `python3 -m api` runs it |
| `serve.py` | Hardened uvicorn launcher (copied from compliance-py) |
| `config.py` | Environment; refuses to start on anything it cannot honor (incl. §H values that are rule parameters) |
| `service.py` | State, record-first plumbing (ledger → contact store → log → apply), every operation, `PortCalls` (crossings) |
| `rules.py` | The rule register: seed proposal, rule / counsel-memo / template proposals, weakening, versions |
| `templates.py` | Template validation (closed variable types, required elements, no money/earnings) and rendering |
| `ports.py` | Port protocols and the fail-closed stand-ins (the only port implementations in `src/`) |
| `httpclients.py` | Thin HTTP clients for V&I, Compliance and Creative (compliance38.py pattern) |
| `contacts.py` | The contact store (email, display name, handles) — kept out of the append-only log so exit deletion is real |
| `store.py` | Append-only JSONL log with a verified hash chain (copied) |
| `ledger.py` | LedgerClient per BUILD_CONTRACTS §2, department `clipper_network` (copied) |
| `founder.py`, `clock.py`, `errors.py` | Andre's token gate, clock, typed errors |
| `textguard.py` | Control characters, injection patterns, money/earnings and contact detectors |
| `jurisdictions.py`, `data/iso3166.json` | ISO 3166 lists (copied) |
| `models.py` | Request models (strict; no money field) |
| `intelligences/i01…i10` | The ten intelligences (recruiting, admission, tiering, enrolment, kit delivery, comms, disputes, discipline, offboarding, evidence & audit) |

`seed/cn_rules_seed.json` — 27 rules, 8 counsel holds, 17 templates
(SHA-256 `4ae4553c6f7ad8bd794cac1c01bb7a6bb9eb8174c8fb7a30d8da44e2c4aa4af4`,
checked at every start).

## Routes

Every route except `/health` needs `Authorization: Bearer $CN_SERVICE_TOKEN`.
"Caller" = `X-CN-Caller-Token`; "Andre" = `X-Andre-Approval-Token`;
"delegate" = `X-CN-Delegate-Token` (counts only when People 43 confirms it).
Every write body carries a `request_id` (identical retry within 15 min → the
stored answer; different body → 409; later → 409).

| Route | Who | Purpose |
|---|---|---|
| `GET /health` | none | `{status, service, rules_version, in_memory, rules_pinned, reconcile_mode, reconcile_required}` |
| `POST /cn/v1/opt-ins`, `POST /cn/v1/opt-outs` | hub | recruiting consent (opt-out honoured at once) |
| `POST /cn/v1/recruiting/campaigns` | Andre | channel, template, recipients (stored as HMACs) |
| `POST /cn/v1/recruiting/campaigns/{id}/send` | scheduler | refuse per recipient first, then Compliance `email_campaign`, then send |
| `POST /cn/v1/applications`, `GET /cn/v1/applications/{id}` | hub, onboarding | application (creates the one identity) |
| `POST /cn/v1/clippers/{id}/connections/start`, `/complete` | hub | relay to V&I (the OAuth code is never stored or logged) |
| `POST /cn/v1/clippers/{id}/age-check` | hub | relay to V&I (the DOB is never stored or logged) |
| `POST /cn/v1/clippers/{id}/agreement-acceptances` | hub | versioned clickwrap record (hashes only) |
| `POST /cn/v1/clippers/{id}/disclosure-training` | hub | attestation |
| `POST /cn/v1/clippers/{id}/admission` | hub, onboarding, scheduler | §C.2 ruling |
| `GET /cn/v1/clippers/{id}` | any caller | identity; contact data only to the hub |
| `GET /cn/v1/clippers/{id}/messages`, `GET /cn/v1/clippers/{id}/export` | hub | message metadata; the clipper's own data |
| `POST /cn/v1/clippers/{id}/tier-nomination` | Andre | T3 nomination |
| `PUT /cn/v1/campaigns/{id}/network-config` | Andre | B.4 config version (rate-card change ≥ 7 days ahead) |
| `POST /cn/v1/campaigns/{id}/rulebook-announcements` | creative_production | Creative's `announce_rulebook_version` |
| `POST`/`GET /cn/v1/campaigns/{id}/enrolments` | hub | §C.4 + kit delivery |
| `POST /cn/v1/enrolments/{id}/kit-acknowledgment` | hub | §C.5 receipt |
| `GET /cn/v1/rules`, `GET /cn/v1/inbox` | any caller | rules in force; open proposals |
| `POST /cn/v1/rules/proposals`, `POST /cn/v1/templates/proposals`, `POST /cn/v1/rules/decisions` | Andre | register changes (weakening needs `acknowledge_weakening: true`) |
| `POST /cn/v1/disputes`; `GET /cn/v1/disputes/{id}`; `POST /cn/v1/disputes/{id}/outcome` | hub; any; Andre or delegate | §C.7 |
| `POST /cn/v1/discipline/sync` | scheduler | pull V&I strikes, apply the table |
| `POST /cn/v1/clippers/{id}/ban-decision` | Andre | approve / reject a ban proposal |
| `POST /cn/v1/tiers/run`, `/messages/flush`, `/disputes/sla-run`, `/offboarding/run` | scheduler | nightly tiers; quiet-hour queue; SLA push; exits |
| `POST`/`GET /cn/v1/clippers/{id}/offboarding` | hub (clipper_request) or Andre (andre_decision); hub or Andre | §C.9 |
| `GET /cn/v1/integrity` | any caller | log chain, ledger verify, anchor problems |
| `GET`/`POST /cn/v1/reconcile` | Andre | the reconcile procedure below |
| `GET /cn/v1/audit/export` | any caller | ordered records with ledger ids (no contact data) |
| `GET /intelligences` | any caller | the ten intelligences |

Status codes: 401 bearer, 403 caller/Andre, 404, 409, 413, 414/431, 415,
422, 503 (ledger, local store or contact store could not record: nothing
changed).

## Run

```bash
cd services/clipper-network-py/src
export CN_SERVICE_TOKEN=<secret>                         # required
export CN_IDENTITY_HMAC_KEY=<>=32 chars, secret>          # required (email HMACs; one identity per person)
export CN_ANDRE_APPROVAL_TOKEN=<Andre's own secret>      # unset = no approvals possible
export CN_CALLER_TOKENS='{"hub":"<>=32 chars>","onboarding":"...","creative_production":"...","scheduler":"..."}'
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret>   # unset = nothing recorded
export CN_DATA_DIR=/var/lib/clipper-network             # unset = in memory; nothing in force after restart
python3 -m api                                          # CN_BIND_ADDR (127.0.0.1), CN_PORT (8400)
```

Then Andre approves the seed (`GET /cn/v1/inbox`, then
`POST /cn/v1/rules/decisions` with his token). Optional: thin clients
(`CN_VI_URL` + `CN_VI_SERVICE_TOKEN` + `CN_VI_CALLER_TOKEN`,
`CN_COMPLIANCE_*` likewise, `CN_CREATIVE_URL` + `CN_CREATIVE_SERVICE_TOKEN`;
all or none), `CN_POSTAL_ADDRESS` and `CN_OPT_OUT_URL` (recruiting),
`CN_CHANNELS` (may only narrow), `CN_DELEGATE_TOKENS`. Must stay unset (no
adapter built; the service refuses to start): `CN_MESSAGE_PROVIDER`,
`CN_FINANCE_URL`, `CN_LEGAL_URL`, `CN_PEOPLE_URL`, `CN_HUB_URL`,
`CN_PUSH_URL`. The §H values (`CN_TIER_*`, `CN_MAX_ENROLMENTS_*`,
`CN_APPEAL_*`, `CN_S2_SUSPENSION_DAYS`, `CN_RATE_NOTICE_DAYS`,
`CN_QUIET_WINDOW`, `CN_*_RETENTION_DAYS`, `CN_ADMISSION_REQUIRES_CONNECTION`,
`CN_TIER_PLATFORM_ANCHORS`) are rule parameters: the environment may only
restate the seed value; change them through a rule proposal Andre approves.
The seed is pinned; `CN_RULES_SEED_PATH` / `CN_RULES_SEED_SHA256` name
another seed only with `CN_ALLOW_UNPINNED_SEED=1` (then `rules_pinned:
false` everywhere — never in production).

## Reconciling the local log with the ledger

Exactly compliance-py's procedure (its README, "Reconciling …"; ADR 0006
N15-1): stop the service; start with `CN_RECONCILE_MODE=1` (starts only if
every problem is voidable; answers reads and the reconcile route only);
`GET /cn/v1/reconcile` with Andre's token; `POST /cn/v1/reconcile` with
`{request_id, head_sha256, void_lines, void_event_ids}` — exactly the plan;
restart without the flag. A rule version on the ledger above the local one
(a rollback), a rewritten line, a missing cited event or another log's
anchors are fatal. A contact store whose values differ from the hashes the
log recorded refuses start-up too (restore the file from backup); values the
log never referenced are purged at start.

## Tests

```bash
cd services/clipper-network-py && python3 -m pytest -q
```

Spec §G (S1–S10, A1–A12, G1–G6), property, fuzz, auth, limits, idempotency,
record-first (ledger / log / contact-store failure = no effect) and
reconcile tests; no network (a socket guard fails any test that tries).
Live run with the real ledger binary:

```bash
cd services/clipper-network-py && LEDGER_BIN=/path/to/ledger-rust/target/release/server \
  python3 devtools/live_run.py --ports 19350,19351,19352,19353
```

It starts ledger A + CN through the production entrypoint (stand-ins:
admission blocked, restart with anchor check), and ledger B + CN through
`devtools/live_server.py` (the test fakes behind a fixture file): admitted,
tiered, enrolled, kit delivered and acknowledged, S3 strike mirrored,
suspension, Andre's ban approval, offboarding, restart, exit closed; ends
with `GET /ledger/verify` on both ledgers. It stops only the processes it
started.

## Gaps

See ADR 0008 "Spec items not built", "Known limitations" and "Unlock list".
