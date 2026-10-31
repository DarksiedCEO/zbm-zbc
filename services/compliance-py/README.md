# Compliance (38) — `services/compliance-py`

The hard gate before **activation**, **payout** and **publish**, plus
Vanta-style **control monitoring**, over an **obligation register** that only
Andre can change. Built Sep 26, 2026 from the locked Compliance spec (rev 1).
Architecture and every choice made where the spec was silent:
`docs/adr/0006-compliance-department-architecture.md`.

**Client activation is live-wired; the rest stays fail closed.** Since ADR 0024 a client's activation
(service area, Andre's recorded OFAC search, Legal (37)'s current documents) can pass when Legal is wired
(`COMPLIANCE_LEGAL_*`). Verification and Integrity, Finance (31)'s tax and rail reads, the OFAC screening
provider and the accessibility checker are still fail-closed stand-ins, so creator activation, payouts and
site publish stay blocked — the spec's stated day-one effect (§D), not a bug. No LLM calls; every rule is
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

## Deployed gate and service area (ADR 0024, Oct 30, 2026)

- **Service area**: clients (and every other lane) only in a US jurisdiction: the 50 states, DC, Puerto Rico, Guam,
  the US Virgin Islands, American Samoa, the Northern Mariana Islands — `HR-05 parameters.operate`, the one allowed set
  (`US-CA` … , `US-DC`, each territory as `US-PR`/`PR`, `US-GU`/`GU`, `US-VI`/`VI`, `US-AS`/`AS`, `US-MP`/`MP`); a campaign target
  may also be the bare `US`. Everything else is refused by HR-07 (OFAC jurisdictions cite US-OFAC-04). 17 rows about
  other markets (HR-06 the EU kit, EU/FR/IT/ES/DE/NL/IE/UK/Canada-Quebec rows and their counsel questions) are
  `superseded`, kept for history.
- **Client activation** also needs HR-14 (Andre's recorded OFAC search: `POST /compliance/v1/sanctions/attestations`,
  Andre only; fact `sanctions_attestation_id`) and HR-15 (Legal confirms the accepted `client_msa` is current and the
  client's; fact `legal_documents`; `src/legal_client.py`, `COMPLIANCE_LEGAL_*`).
- **`GET /compliance/v1/clients/{client_id}/activation`** (`finance_31`, `seo_02`, `revenue_recovery`, `onboarding`,
  or Andre's token):
  the client's CURRENT decision, ALLOW or REFUSE with reasons and the recorded jurisdiction, re-evaluated on every call
  (rule fingerprints, the service area in force now, holds, C-11, the OFAC search's age and lane).
  `sanctions_grounded` is true when a refusal rests on sanctions (an open sanctions_match hold, or the client's place
  on HR-07's sanctions list): Finance never refunds or releases such a payment without an OFAC authorization.
  `sanctions_ground` says which: `match` (an OFAC list match) or `place` (a sanctioned place or target); match wins. Finance (invoices,
  billing readiness, Stripe pages), seo (client runs and reports) and detection (store reads) refuse anything but a
  current ALLOW.
  AEGIS r9 R9-4: the reasons and both sanctions fields go to `finance_31` and Andre's token only; `seo_02`,
  `revenue_recovery` and `onboarding` get one generic refusal reason and null sanctions fields
  (`api.generic_activation`).
- **Rule expiry early warning** (money red team RTM-06): readiness `expiry_warnings` (30 / 14 / 7 days, and the rules a
  client's activation relies on, with the next one), a `WARNING` line at each start and run, `rules_expiring` in
  `/health`.
- **Identity before body** (RTM-11): every route's caller and Andre dependencies run before its body is validated.
- **`GET /compliance/v1/readiness`** (any caller) and the lines printed at start: expired rules and the gates they
  affect, what closes every gate, and a plain US web client's rule set with its expiry dates (`src/readiness.py`).
- **`GET /compliance/v1/sanctions/status`** (`finance_31`, `onboarding`): a payee's latest screen and its freshness.
- **Scheduler** (`src/scheduler.py`, `COMPLIANCE_SCHEDULER_ENABLED=1`, `COMPLIANCE_SCHEDULER_INTERVAL_SECONDS`):
  internal controls at start and every 6 h, the Change Watcher daily; in process, no `scheduler` token.
- **Callers** `seo_02` and `revenue_recovery` added. A caller token that is an unresolved Railway reference refuses
  the start.
- **Deploy**: `Dockerfile`, Railway service `compliance` (private), runbook "Compliance (38): the hard gate";
  `devtools/reverify_proposal.py` builds a re-verification proposal from a saved page.

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
| `readiness.py` | ADR 0024: expired rules, the gates they affect, a US web client's rule set |
| `scheduler.py` | ADR 0024: the in-process scheduler (controls, Change Watcher) |
| `legal_client.py` | ADR 0024: the Legal (37) thin client (current version, a client's acceptance) |
| `intelligences/i01…i11` | The 11 intelligences (register keeper, three gates, control monitor, change watcher, jurisdiction resolver, sanctions, disclosure, accessibility, evidence/audit) |
| `intelligences/engine.py` | The shared gate algorithm (C.2) and all C.10 checks |

`seed/compliance_obligations_seed.json` — seed v2 (ADR 0024: the founder decisions of
Oct 30, 2026 written into the spec's seed v1, whose SHA-256 `4e3821d0…584d` it records), SHA-256
`bc6d371aa2703a231e03180b1c09907fc25a20dc09f44cdeb5a82f854df070f5`, checked at every start.

## Routes

Every route except `/health` needs `Authorization: Bearer $COMPLIANCE_SERVICE_TOKEN`.
"Caller" = `X-Compliance-Caller-Token`; "Andre" = `X-Andre-Approval-Token`.
Every POST body carries a `request_id` (idempotency: identical retry within
15 min → the stored answer; different body → 409; later → 409).

| Route | Who | Purpose |
|---|---|---|
| `GET /health` | none | `{status, service, register_version_in_force, in_memory, seed_pinned, production, reconcile_mode, reconcile_required, log_write_fault, scheduler}`; 503 with `status: unhealthy` when the scheduler thread is dead or no controls run succeeded for two intervals |
| `POST /compliance/v1/rule` | onboarding | activation (onboarding protocol) |
| `POST /compliance/v1/review` | creative_production | payout (`zbc_clip`) / publish (`zbm_work`) (creative protocol) |
| `POST /compliance/v1/gates/{activation,payout,publish}` | same | native form, same bodies |
| `GET /compliance/v1/rulings/{id}` | any caller | one ruling |
| `POST /compliance/v1/sanctions/screen` | onboarding, finance_31 | screen a payee or owner (DOB goes to the provider, never stored) |
| `POST /compliance/v1/sanctions/attestations` | Andre only | ADR 0024: record his OFAC search for a client (HR-14); a hit opens a permanent hold |
| `GET /compliance/v1/sanctions/status` | finance_31, onboarding | ADR 0024: a payee's latest screen and its freshness |
| `GET /compliance/v1/clients/{client_id}/activation` | `finance_31`, `seo_02`, `revenue_recovery`, `onboarding`, Andre | ADR 0024: the client's current activation decision (reasons: Finance and Andre only) |
| `GET /compliance/v1/readiness` | any caller | ADR 0024: expired rules and the gates they affect |
| `POST /compliance/v1/accessibility/checks` | creative_production | run the checker for an exact content hash |
| `POST /compliance/v1/jurisdictions/resolve` | any caller | resolver answer (recorded) |
| `GET /compliance/v1/register` | any caller | rows in force + effective status; `gate`, `jurisdiction`, `status`, `domain`, `page` (100/page) |
| `GET /compliance/v1/register/versions` | any caller | version list (metadata, hash chain) |
| `GET /compliance/v1/register/{id}` | any caller | one row + its history |
| `POST /compliance/v1/register/proposals` | legal_37 or Andre | create a proposal (never changes the register) |
| `GET /compliance/v1/inbox` | any caller | undecided proposals, oldest effective date first, with `content_sha256` |
| `POST /compliance/v1/register/decisions` | Andre only | ≤ 200 approve/reject decisions, atomic; approving a proposal that is weakening (flagged, or weakening NOW against the current row, or changed since drafted) needs `acknowledge_weakening: true` (the 422 shows the current diff in `recheck`) |
| `GET /compliance/v1/reconcile`, `POST /compliance/v1/reconcile` | Andre only | show / void the voidable mismatches between the local log and the ledger (see below) |
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
export COMPLIANCE_DATA_DIR=/var/lib/compliance              # unset = in memory; nothing in force after restart (flocked: compliance.lock, one writer)
# with a data dir, start-up reads GET /ledger/entries and refuses if the log does not match the ledger
# (COMPLIANCE_RECONCILE_MODE=1 starts a log with only voidable mismatches for Andre to reconcile; see below)
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
`COMPLIANCE_WATCHER_MAX_PROPOSALS_PER_SOURCE` (20). The launcher (`src/serve.py`; fix wave 25, scout A P4 —
these were documented only in its docstring): `COMPLIANCE_REQUEST_HEAD_TIMEOUT_SECONDS` (10; the request head must
arrive within it), `COMPLIANCE_KEEP_ALIVE_TIMEOUT_SECONDS` (5), `COMPLIANCE_LIMIT_CONCURRENCY` (128; uvicorn's
`limit_concurrency`), `COMPLIANCE_SWITCH_INTERVAL_SECONDS` (0.001; only 0.0001 .. 0.05 starts, and the interval in
force is checked and printed before serving — fix wave 25, C5-3) and `COMPLIANCE_DRAINS_MAX` (512; graceful closes
draining at once, ADR 0003). `COMPLIANCE_FIXTURE_DIR` is read only by `devtools/live_server.py` (the live run's
fixture fetcher), never by the service. The seed is pinned to the
spec's hash: `COMPLIANCE_SEED_PATH` / `COMPLIANCE_SEED_SHA256` may name
another seed ONLY together with `COMPLIANCE_ALLOW_UNPINNED_SEED=1` (and an
explicit `COMPLIANCE_SEED_SHA256`); the service then reports
`seed_pinned: false, production: false` in `/health` and `seed_pinned: false`
in every ruling — never use that in production. `COMPLIANCE_SANCTIONS_PROVIDER`,
`COMPLIANCE_A11Y_PROVIDER`, `COMPLIANCE_AUTO_REVERIFY_UNCHANGED` and
`COMPLIANCE_WAYBACK_CAPTURE` must stay unset: no adapter is built, and the
service refuses to start rather than pretend.

## Reconciling the local log with the ledger (operator procedure)

Fix wave A (ADR 0025): crash residue announced by the intent journal is rolled back automatically (finance-py README,
same section) and a voided ruling id is never issued again (the retry gets a new one); `/health` is 503 while
Compliance cannot write; seo_02 and revenue_recovery reach only `GET /compliance/v1/clients/{id}/activation` and
`/health`. Token rotation (`COMPLIANCE_SERVICE_TOKEN_NEXT`, `COMPLIANCE_CALLER_TOKENS_NEXT`,
`COMPLIANCE_ANDRE_APPROVAL_TOKEN_SHA256`, `COMPLIANCE_ANDRE_APPROVAL_TOKEN_NEXT_SHA256`): docs/runbooks/token-rotation.md.
AEGIS wave A: `COMPLIANCE_ROTATION_ENDS_AT` ends a rotation window (required with any `_NEXT`); an interrupted write
that reached its anchor is held for Andre (`interrupted_write_anchored`), never voided; Railway health-checks
`/livez`. `COMPLIANCE_LEDGER_ANCHOR_KEY`: the HMAC key of the daily off-ledger anchor store
(`<COMPLIANCE_DATA_DIR>/ledger_anchors.jsonl`, `src/ledger_anchor.py`); a ledger that no longer extends the stored
anchor turns `/health` 503 (`ledger_anchor_mismatch`) until Andre resolves it (ADR 0025 section 7, F-5); a failing
job is `anchor_check_failing`, 26 h without a good run `ledger_anchor_stale`, a store without the key the warning
`ledger_anchor_unkeyed`, and reconcile mode is 503 `maintenance` (ADR 0025 section 8;
docs/runbooks/held-service-and-ledger-anchor.md).

Every local-log line is anchored on the ledger before it is written. If a
commit fails after its anchor (disk error, crash, a ledger answer lost), or
another instance ran on a copy of the data directory, the ledger holds
anchors, rulings or an instance lease the local log does not. Nothing on the
ledger alone can withdraw them. The service then refuses to start (and C-11
is red, which blocks every gate) until Andre reconciles:

1. Stop the service. Make sure no other instance runs on a copy of this data
   directory (the refusal names a newer lease when one did).
2. Start it with `COMPLIANCE_RECONCILE_MODE=1`. It starts only if every
   problem is voidable; it answers reads and the reconcile route only.
   A local line the ledger does not anchor, a missing cited event or another
   log's anchors are FATAL: it refuses even in this mode.
3. `GET /compliance/v1/reconcile` with Andre's token: `problems`, `fatal`,
   `head_seq`, `head_sha256`, `register_version` and `voidable` (`lines`,
   `event_ids`). Review them.
4. `POST /compliance/v1/reconcile` with Andre's token and
   `{"request_id", "head_sha256", "void_lines", "void_event_ids"}` — exactly
   the plan (anything else is 409, nothing voided). It records a `reconcile`
   event on the ledger (payload hash binds the register version, the log head
   and the voided lines and ids) and a `reconcile` line in the local log.
5. Restart without `COMPLIANCE_RECONCILE_MODE`, run the internal controls:
   C-11 passes. Start-up and C-11 honour the voids only while the ledger's
   `reconcile` event matches the local line's payload hash.

A running service can be reconciled the same way (steps 3–4) when C-11 goes
red after a failed commit.

Register-version events (AEGIS N16-7, shared with verification-py and
clipper-network-py). A `register_version_published` event is honoured only
when its id and payload hash match a decision record in the local log. Any
other one in this log's epoch — posted by anyone holding the ledger token, or
left by a decision the local log no longer holds (a rollback) — is VOIDABLE,
never fatal: a normal start refuses and names it ("… register version
event(s) on the ledger match no decision in the local log (version N; local
version M) …"); `COMPLIANCE_RECONCILE_MODE=1` starts and the plan lists it
under `voidable.event_ids` (`cmp-ver-<epoch>-<n>-…`). Before voiding, Andre
checks which it is:

- he made that decision and only its line never reached the disk: do NOT
  void it. The unwritten line is kept as
  `compliance_log.jsonl.unwritten-<seq>` — append that exact line to the log
  (its hash is the ledger's anchor) and restart; if more lines were written
  after it, have Andre make another decision before the process stops (it
  republishes that version number);
- he made no such decision (a forgery): POST the plan (step 4). The
  `reconcile` event is recorded on the ledger with actor `andre` and binds the
  voided ids; the next start is normal at the local version and C-11 passes.

Voiding a genuine version is a deliberate, recorded rollback of the register.

The callers opt in with `COMPLIANCE_SERVICE_URL`, `COMPLIANCE_SERVICE_TOKEN`
and `COMPLIANCE_CALLER_TOKEN` in onboarding-py / creative-py; without all
three they keep their "not allowed yet" stand-ins. The clients refuse any
answer that does not echo their `request_id` and the SHA-256 of the facts
they sent (`facts_sha256`), and any ruling with `seed_pinned: false` unless
the caller sets `COMPLIANCE_ACCEPT_UNPINNED=1` (default off; AEGIS N15-8).

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

The run's work directory (ledger and service logs) is removed when the run ends, passed or failed; set
`LIVE_WORK_DIR=<dir>` to keep it inside `<dir>` (the run prints where; fix wave 26b, C6-2).

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

## Bug sweep C (Oct 7, 2026)

`GET /compliance/v1/audit/evidence?event_type=&limit=&offset=` (any caller): every Compliance ledger event, `committed`
(named by an anchored local line), `cited` or `attempted` (recorded first, never committed: unanchored evidence =
attempted, not done). `/health` adds `log_write_fault`. The data directory is single-writer (`compliance.lock`). The
Change Watcher's fetches and C-11's ledger reads run outside the service lock. Details: ADR 0006, "Bug sweep C fixes".
