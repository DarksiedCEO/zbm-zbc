# Compliance (38) — `services/compliance-py`

The hard gate before **activation**, **payout** and **publish**, plus
Vanta-style **control monitoring**, over an **obligation register** that only
Andre can change. Built Sep 26, 2026 from the locked Compliance spec (rev 1).
Architecture and every choice made where the spec was silent:
`docs/adr/0006-compliance-department-architecture.md`.

**Not certified for real use.** Verification and Integrity, Finance (31),
Legal (37), the OFAC screening provider and the accessibility checker are
fail-closed stand-ins, so on day one almost everything is blocked — that is
the spec's stated day-one effect (§D), not a bug. No LLM calls; every rule is
deterministic.

## What it decides

- **Activation** (`POST /compliance/v1/rule`, caller `onboarding`): client,
  ZBC campaign (`zbc_brand`, subject = campaign id) or clipper (`zbc_creator`).
- **Payout** (`POST /compliance/v1/review`, `subject_kind: zbc_clip`, caller
  `creative_production`): one clip; "allowed" means "may be handed to
  Finance for payment consideration" and carries no amount.
- **Publish** (`POST /compliance/v1/review`, `subject_kind: zbm_work`): one
  ad, site, page, form, portal, message campaign, checkout or chatbot.
- **Controls** (17, A.4): green only with a passing result inside the SLA and
  every feeding obligation in force.

Every answer is `allowed` + `unmet[]`; each unmet item cites a register
obligation id, its title, its `source_url` (null only for house rules and
URL-less unverified rows), the row's effective status and a ≤ 200-char
message. `unmet_lines` renders them as
`compliance_38/<id>/<code>: <message> [<source url or "no source url">]`.
Every ruling is recorded on the evidence ledger **before** it is answered;
if the ledger is down the answer is 503 `{"issued": false}` and nothing is
stored.

Behaviour fixed in AEGIS round 14 (details: ADR 0006, "Amendment — AEGIS
round 14"):
- payout and publish use the subject's **latest** activation ruling — a
  refused re-activation voids an earlier allowed one; **every** publish needs
  the client's current allowed activation covering each target and
  platform; a clip's platform must be one its campaign was activated for;
- jurisdiction codes are checked against shipped ISO 3166 lists
  (`src/data/iso3166.json`, `src/jurisdictions.py`); unknown codes such as
  `CA-PQ` / `CA-QUE` / `US-XX` are refused;
- every local log line is anchored on the ledger before it is written; a
  truncated, rewritten, emptied or foreign log, or a register version behind
  the ledger's, refuses start-up and turns C-11 red (one disk-backed
  instance per ledger);
- evidence dates (`verified_at`, `evidence.fetched_at`) more than 1 day in
  the future → 422 for everyone;
- negated or compound disclosure labels (`not sponsored`, `NOT an ad`,
  `no #ad`, `ad-free`) fail;
- proposals that may weaken a rule are flagged `weakening: true` (with
  reasons) and Andre's approval must carry `acknowledge_weakening: true`;
  counsel questions resolve only through a counsel-memo supersede;
- the Change Watcher drafts at most 50 proposals per cycle / 20 per source,
  then files one `watch_notice` ("source flooded") for Andre;
- the seed hash is pinned (see Run);
- all dates are UTC dates whatever the clock's timezone;
- a replayed `request_id` returns the stored ruling only if re-evaluation
  gives the same outcome; otherwise a new ruling is issued.

## Module map (`src/`)

| Module | Role |
|---|---|
| `api.py` | FastAPI app: auth (bearer, caller token, Andre token), request limits (`InputLimits`), routes, error shaping; `python3 -m api` runs it |
| `serve.py` | Hardened uvicorn launcher (copied from onboarding-py) |
| `config.py` | Environment; refuses to start on anything it cannot honor |
| `service.py` | State, record-first plumbing (ledger → local log → apply), every operation, `PortCalls` (crossing records) |
| `register.py` | Row schema, shelf lives, effective status, hashing, versions, seed checks |
| `facts.py` | Strict fact schemas per gate/lane (C.1) and the validator |
| `controls.py` | Control catalog (A.4) and status arithmetic |
| `ports.py` | V&I, Finance 31, Legal 37, sanctions and accessibility ports — **stand-ins only** |
| `fetcher.py` | Change Watcher fetch port, its refusal rules, the source list and watch terms |
| `ledger.py` | LedgerClient per BUILD_CONTRACTS §2 (HTTP + unconfigured), deterministic ids |
| `store.py` | Append-only JSONL log with a verified hash chain |
| `founder.py` | Andre approval token gate |
| `textguard.py` | Control-character, normalization and injection-pattern helpers |
| `models.py` | Request models (strict; no money fields) |
| `intelligences/i01…i11` | The 11 intelligences (register keeper, three gates, control monitor, change watcher, jurisdiction resolver, sanctions, disclosure, accessibility, evidence/audit) |
| `intelligences/engine.py` | The shared gate algorithm (C.2) and all C.10 checks |

`seed/compliance_obligations_seed.json` — the spec's seed, unchanged
(SHA-256 `4e3821d018a42be76ede1bf501004f0a8c5590327fa98ddd9f8c3f16845f584d`,
checked at every start).

## Routes

Every route except `/health` needs `Authorization: Bearer $COMPLIANCE_SERVICE_TOKEN`.
"Caller" = `X-Compliance-Caller-Token`; "Andre" = `X-Andre-Approval-Token`.
Every POST body carries a `request_id` (idempotency: identical retry within
15 min → the stored answer; different body → 409; later → 409).

| Route | Who | Purpose |
|---|---|---|
| `GET /health` | none | `{status, service, register_version_in_force, in_memory, seed_pinned, production}` |
| `POST /compliance/v1/rule` | onboarding | activation (onboarding protocol) |
| `POST /compliance/v1/review` | creative_production | payout (`zbc_clip`) / publish (`zbm_work`) (creative protocol) |
| `POST /compliance/v1/gates/{activation,payout,publish}` | same | native form, same bodies |
| `GET /compliance/v1/rulings/{id}` | any caller | one ruling |
| `POST /compliance/v1/sanctions/screen` | onboarding, finance_31 | screen a payee or owner (DOB goes to the provider, never stored) |
| `POST /compliance/v1/accessibility/checks` | creative_production | run the checker for an exact content hash |
| `POST /compliance/v1/jurisdictions/resolve` | any caller | resolver answer (recorded) |
| `GET /compliance/v1/register` | any caller | rows in force + effective status; `gate`, `jurisdiction`, `status`, `domain`, `page` (100/page) |
| `GET /compliance/v1/register/versions` | any caller | version list (metadata, hash chain) |
| `GET /compliance/v1/register/{id}` | any caller | one row + its history |
| `POST /compliance/v1/register/proposals` | legal_37 or Andre | create a proposal (never changes the register) |
| `GET /compliance/v1/inbox` | any caller | undecided proposals, oldest effective date first, with `content_sha256` |
| `POST /compliance/v1/register/decisions` | Andre only | ≤ 200 approve/reject decisions, atomic; approving a `weakening: true` proposal needs `acknowledge_weakening: true` |
| `GET /compliance/v1/controls`, `/controls/{id}` | any caller | control status |
| `POST /compliance/v1/controls/{id}/results` | the control's owner | push a result |
| `POST /compliance/v1/controls/internal/run` | scheduler | compute Compliance-owned controls; draft re-verification proposals |
| `GET /compliance/v1/trust-center` | any caller | `{control_id, title, status, last_passed_at}` only |
| `GET /compliance/v1/holds`, `POST /holds/{id}/release` | any caller; Andre | holds |
| `POST /compliance/v1/watcher/run` | scheduler | one Change Watcher cycle (only when `COMPLIANCE_WATCHER_ENABLED=1`) |
| `GET /compliance/v1/audit/export` | any caller | ordered records with ledger event ids and register versions (500/page; personal data only as hashes) |
| `GET /intelligences` | any caller | the 11 intelligences |

Status codes: 401 bearer, 403 caller/Andre, 404, 409, 413, 414/431, 415, 422,
503 (ledger or local store could not record: nothing issued).

## Run

```bash
cd services/compliance-py/src
export COMPLIANCE_SERVICE_TOKEN=<secret>                     # required
export COMPLIANCE_ANDRE_APPROVAL_TOKEN=<Andre's own secret>  # unset = no approvals possible
export COMPLIANCE_CALLER_TOKENS='{"onboarding":"<>=32 chars>","creative_production":"...","legal_37":"...","scheduler":"...", ...}'
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret>   # unset = nothing can be recorded
export COMPLIANCE_DATA_DIR=/var/lib/compliance              # unset = in memory; nothing in force after restart
# with a data dir, start-up reads GET /ledger/entries and refuses if the log does not match the ledger
python3 -m api      # COMPLIANCE_BIND_ADDR (127.0.0.1), COMPLIANCE_PORT (8380)
```

Then: Andre approves the seed (`GET /compliance/v1/inbox`, then
`POST /compliance/v1/register/decisions` with his token), and the scheduler
calls `POST /compliance/v1/controls/internal/run` (C-11 evidence integrity
blocks every gate until it has passed once; C-04 blocks payout and creator
activation until a sanctions provider exists).

Other settings (all fail closed by default): `COMPLIANCE_SANCTIONS_FRESHNESS_DAYS`
(1), `COMPLIANCE_DISCLOSURE_MAX_OFFSET_S` (3), `COMPLIANCE_A11Y_MAX_AGE_DAYS`
(30), `COMPLIANCE_WATCHER_ENABLED` (0), `COMPLIANCE_SITE_OWNER_CALLER`
(creative_production), `COMPLIANCE_WATCHER_MAX_PROPOSALS_PER_CYCLE` (50),
`COMPLIANCE_WATCHER_MAX_PROPOSALS_PER_SOURCE` (20). The seed is pinned to the
spec's hash: `COMPLIANCE_SEED_PATH` / `COMPLIANCE_SEED_SHA256` may name
another seed ONLY together with `COMPLIANCE_ALLOW_UNPINNED_SEED=1` (and an
explicit `COMPLIANCE_SEED_SHA256`); the service then reports
`seed_pinned: false, production: false` in `/health` and `seed_pinned: false`
in every ruling — never use that in production. `COMPLIANCE_SANCTIONS_PROVIDER`,
`COMPLIANCE_A11Y_PROVIDER`, `COMPLIANCE_AUTO_REVERIFY_UNCHANGED` and
`COMPLIANCE_WAYBACK_CAPTURE` must stay unset: no adapter is built, and the
service refuses to start rather than pretend.

The callers opt in with `COMPLIANCE_SERVICE_URL`, `COMPLIANCE_SERVICE_TOKEN`
and `COMPLIANCE_CALLER_TOKEN` in onboarding-py / creative-py; without all
three they keep their "not allowed yet" stand-ins.

## Tests

```bash
cd services/compliance-py && python3 -m pytest -q
```

Certification (§H 1–31), property, fuzz, mutation, auth, limits,
idempotency and record-first tests; no network (a socket guard fails any
test that tries). Live run with the real ledger binary:

```bash
cd services/compliance-py && LEDGER_BIN=/path/to/ledger-rust/target/release/server python3 devtools/live_run.py --ports 18950,18951,18952,18953
```

It starts the ledger, compliance-py through its production entrypoint, a
restart of that compliance-py against the same ledger (anchors verified),
and a second compliance-py through `devtools/live_server.py` on its OWN
ledger (the fourth port; one disk-backed instance per ledger) (a fixture fetcher for
the Change Watcher leg, no network); drives all gates, a register approval,
the onboarding and creative thin clients, a watcher proposal → Andre
approval, and ends with `GET /ledger/verify`. It stops only the processes it
started.

## Gaps

See ADR 0006 "Spec items not built" and "Known limitations". In short: no
real sanctions or accessibility provider; V&I, Finance and Legal do not
exist; the caller changes of spec §F.2 are not made (the spec says not to in
this build); OpenStates/Regulations.gov adapters, the Competition Bureau feed
and the TikTok newsroom are not configured; there is no evidence field for
AI/synthetic disclosure, so those clips and assets stay blocked.
