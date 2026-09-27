# legal-py — Legal department (37)

Keeps ZBM/ZBC's document register and acceptance evidence, runs contract playbooks, tracks obligations and
filings, triages matters and litigation holds, runs the DMCA takedown desk, applies the music policy and records
counsel answers. **It never gives legal advice**: it emits counsel-approved documents (pinned by SHA-256), a
routing notice, dates and statuses of a party's own records, and — to Andre and other departments — codes
labelled `unreviewed` until a counsel memo or template id is attached. No LLM calls.

Built from `LEGAL_SPEC.md` rev 1. Architecture, the 40 numbered choices made where the spec was silent, the
pinned seed hashes, what is not built and the unlock list: `docs/adr/0010-legal-department-architecture.md`.

**Status:** built and tested; not in force for any real document. Day one: no rule version, no counsel engaged,
no document current, no acceptance evidence-sufficient, every music-bearing clip blocked, every Creative sign-off
refused, nothing deleted.

## Run

```bash
cd services/legal-py && python3 -m pytest -q        # 212 tests, no network
export LEGAL_SERVICE_TOKEN=<secret>                  # required: the service refuses to start without it
export LEGAL_ANDRE_APPROVAL_TOKEN=<Andre's secret>   # unset -> nothing can ever be approved
export LEGAL_CALLER_TOKENS='{"compliance_38": "<>=32 printable chars>", "clipper_network": "...", "hub": "...", "scheduler": "..."}'
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret>
export LEGAL_DATA_DIR=<dir>                          # unset -> in-memory; /health says so; nothing survives a restart
cd src && python3 -m api                             # LEGAL_BIND_ADDR (127.0.0.1), LEGAL_PORT (8420)
```

Caller names: `compliance_38`, `clipper_network`, `verification_integrity`, `creative_production`, `onboarding`,
`finance_31`, `hub`, `esign_gateway`, `scheduler`. Headers: `Authorization: Bearer <service token>` on every route
but `/health`; `X-LEGAL-Caller-Token` for callers; `X-Andre-Approval-Token` for Andre.

| Setting | Default | Notes |
|---|---|---|
| `LEGAL_COMPLIANCE_URL` / `_TOKEN` / `_CALLER_TOKEN` | unset | all three or none; none = stand-in (proposals stay `pending_delivery`) |
| `LEGAL_AGENT_MAX_FALLBACK` | 1 | 0 or 1; fallback_2, unmatched and walk-away always go to counsel |
| `LEGAL_DISPUTE_THRESHOLD` | `"5000.00"` | S4 threshold (money string) |
| `LEGAL_MEMO_REVIEW_DAYS` | 90 | 1..90 (L3) |
| `LEGAL_HOLD_RENOTICE_DAYS` | 90 | 1..90 |
| `LEGAL_FILING_ALERT_DAYS` | 60 | |
| `LEGAL_ESIGN_CONSENT_REQUIRED` | 1 | clipper agreements need the ESIGN §7001(c) block |
| `LEGAL_BUSINESS_TZ` | `America/Los_Angeles` | the calendar dates Legal counts in |
| `LEGAL_RECONCILE_MODE` | 0 | see "Reconciling" |
| `LEGAL_COUNSEL_CHANNEL`, `LEGAL_ESIGN_PROVIDER` | unset | not built: setting either refuses start |
| `LEGAL_PORTAL_FAQ` | 0 | 1 refuses start (route only, CQ-15) |
| `LEGAL_ALLOW_UNPINNED_SEED` + `LEGAL_SEED_SHA256` | unset | non-production rules seed only |

## Routes

| Route | Who | Purpose |
|---|---|---|
| `GET /health` | open | `status, rules_version, in_memory, rules_pinned, counsel_channel_wired, reconcile_*` |
| `GET /legal/v1/documents/{doc_id}/current` | any | protocol answer for Compliance / Clipper Network |
| `GET /legal/v1/documents/{doc_id}`, `.../versions/{v}` | any | register view (no text) |
| `GET .../versions/{v}/text` | Andre | the text |
| `POST /legal/v1/documents/{doc_id}/versions` | Andre (upload) or scheduler (template fill) | new draft |
| `POST .../versions/{v}/counsel-review`, `/counsel-signoff`, `/decision` | Andre | package; sign-off record; approve / retire / withdraw |
| `POST /legal/v1/acceptances`; `GET /legal/v1/acceptances/{id}` | hub, clipper_network, onboarding; readers | clickwrap evidence |
| `POST /legal/v1/envelopes`; `POST /legal/v1/esign/events` | Andre; esign_gateway | envelopes (provider not wired) |
| `POST /legal/v1/playbooks/proposals`, `/decisions`; `GET /legal/v1/playbooks/{doc_type}` | Andre; any | playbooks |
| `POST /legal/v1/playbooks/{doc_type}/reviews` | onboarding, scheduler, Andre | deviation classes |
| `GET /legal/v1/obligations`; `POST /legal/v1/obligations` | any; Andre (from a memo) | obligations |
| `POST /legal/v1/obligations/{id}/done`, `/waive` | owner department; Andre | |
| `GET`, `PUT /legal/v1/contracts/{client_id}/terms` | onboarding | ContractTerms storage |
| `GET /legal/v1/register`, `/register/{cq_id}`; `POST /register/{id}/invalidate` | any; compliance_38 | counsel-question mirror |
| `POST /legal/v1/memos`; `GET /legal/v1/memos/{id}` | Andre; any | memo intake |
| `POST /legal/v1/requests`; `GET /legal/v1/matters/{id}`; `POST .../close` | any; any; Andre | matters |
| `GET /legal/v1/holds/check`; `POST /holds/{id}/acknowledgments`, `/release` | any; hub; Andre + memo | holds |
| `POST /legal/v1/takedowns`, `/{id}/counter-notice`, `/claimant-action`, `/restore`, `/withdraw` | Andre, hub, clipper_network | takedown desk |
| `GET /legal/v1/takedowns/count`, `/{id}`; `POST /takedowns/outbound` | verification_integrity, any; Andre | |
| `GET`, `POST /legal/v1/filings`; `POST /filings/{id}/ready`, `/filed` | any; Andre | filings calendar |
| `POST /legal/v1/signoffs` | creative_production | standing sign-offs |
| `POST /legal/v1/music/rulings` | creative_production, compliance_38 | music policy |
| `GET /legal/v1/retention` | any | retention schedule |
| `POST /legal/v1/jobs/{obligations,filings,holds-renotice,retention,proposal-delivery}/run` | scheduler | once per UTC day |
| `GET /legal/v1/rules`; `POST /rules/proposals`, `/rules/decisions` | any; Andre | rule register |
| `GET`, `POST /legal/v1/reconcile` | Andre | see below |
| `GET /legal/v1/audit/export`, `/integrity`, `/intelligences` | any | audit |

## How the counsel gate works

1. Counsel engagement: Andre uploads the engagement letter (with clause `ENG-AI-01`), records counsel's
   countersigned copy (`counsel-signoff` with `countersignature_b64`; its `counsel_ref` becomes the engagement id)
   and approves it.
2. Memos: Andre files each counsel memo (`POST /legal/v1/memos`: the memo bytes plus the `answers` / `cites` he
   types from it). A memo changes only what its `cites` names; anything else is 409 `MEMO_DOES_NOT_CITE` and nothing
   is filed.
3. Documents: a document version becomes approvable only after a sign-off record whose memo cites
   `doc_id@version` and whose `doc_sha256` equals the version's hash; then Andre approves by that hash.
4. Compliance rows: a memo's typed rows go to Compliance as proposals (evidence `legal37://memos/<id>`); a
   Compliance-owned row turns verified in Legal's mirror only after Andre approves it at Compliance.

## Reconciling the local log with the ledger

Identical to verification-py / compliance-py (their READMEs, "Reconciling the local log with the ledger"), with
`LEGAL_RECONCILE_MODE=1` and `GET/POST /legal/v1/reconcile`: stop the service, start it in reconcile mode, read the
plan with Andre's token, POST exactly the plan (`request_id`, `head_sha256`, `void_lines`, `void_event_ids`;
anything else is 409), restart without the flag and check `GET /legal/v1/integrity` is green. A
`rules_version_published` event that matches no local decision record is voidable (N16-7): if Andre made that
decision and only its line is missing, append the kept `legal_log.jsonl.unwritten-<seq>` line instead of voiding.

## Tests and live run

`python3 -m pytest -q` runs the spec §G certification scenarios S1-S10, attacks A1-A12, guardrails G1-G8,
auth/limits/idempotency on every route, ledger/anchor/store failure = no effect on every write route,
reconcile/anchor/lease, a leak fuzz and details.

```bash
LEDGER_BIN=/path/to/ledger-rust/target/release/server python3 devtools/live_run.py --ports 19500,19501,19502,19503
```

Real ledger binary, the production entrypoint, a Compliance stub behind the real thin client; kills only the PIDs
it started; exit 0 only if every check holds and `GET /ledger/verify` is valid.
