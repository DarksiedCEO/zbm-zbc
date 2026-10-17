# ooh-py — Out-of-Home & DOOH (15), waves OOH-0 and OOH-1

The department's constitution as enforced data and its operating spine: campaigns and their state machine, inventory
availability, the six separate price objects, the per-deal disclosed-markup commercial model, client approval, 100%
prepaid client funds, the section 12 purchase gate, and retry-safe order submission. Design record:
[ADR 0019](../../docs/adr/0019-ooh-dooh-department-constitution.md). **Not in force; no agent is built; nothing here
buys media** — Andre places every order with the vendor himself, and this service decides whether he may.

## Run

```bash
pip install -r requirements.txt
cd src && OOH_SERVICE_TOKEN=... OOH_DATA_DIR=/abs/dir LEDGER_SERVICE_URL=... LEDGER_SERVICE_TOKEN=... python3 -m api
python3 -m pytest -q                                   # from services/ooh-py; count: docs/test-counts.md
LEDGER_BIN=../ledger-rust/target/release/server python3 devtools/live_run.py
```

## Routes (`/ooh/v1`, bearer + `X-OOH-Caller-Token`; Andre = dashboard + `X-Andre-Approval-Token`)

| Route | Who |
|---|---|
| `GET /health` (no auth), `GET /status` | anyone / dashboard |
| `GET /constitution`, `GET /roster` | dashboard, agent, compliance / dashboard, compliance |
| `POST /tenants`, `GET /tenants/{tid}` | Andre / scoped (hub: its own tenant only) |
| `POST /vendors`, `GET /vendors` | Andre / dashboard, agent, compliance |
| `GET /markets`, `GET /markets/{mid}`; `POST /markets/{mid}/readiness` (MET: Andre; NOT_MET: any preparer, compliance) | readers |
| `POST /sources`, `POST /faces`, `POST /faces/{fid}/attributes`, `POST /faces/{fid}/avails`, `POST /vendors/{vid}/attributes` | agent, dashboard |
| `GET /sources/{sid}`, `GET /faces`, `GET /faces/{fid}`, `GET /vendors/{vid}`, `GET /inventory-port` (NOT_CONNECTED; `POST …/sync` refuses 503) | dashboard, agent, compliance (never the hub) |
| `GET /principal-agent`, `POST /principal-agent-determinations` (a CPA's / counsel's confirmation) | readers / Andre |
| `GET, POST /kill-switches` | dashboard, agent, compliance engage; Andre releases |
| `POST /tenants/{tid}/campaigns`, `GET …/campaigns/{cid}` (hub: client view), `GET …/exposure` | agent, dashboard |
| `POST …/transitions` (Andre for the transitions ADR 0019 decision 4 names) | agent, dashboard |
| `POST …/inventory`, `GET …/inventory/{uid}`, `POST …/inventory/{uid}/availability`, `…/flight`, `…/hold-evidence` | agent, dashboard |
| `POST …/inventory/{uid}/hold-decisions` (the only way to `HOLD_CONFIRMED`) | Andre |
| `POST …/prices` (sell price and buy limit: Andre) | agent, dashboard |
| `POST …/brief`, `…/markup-evidence`, `…/inventory/{uid}/rate-card-estimates`; `GET …/plan` (never the hub) | agent, dashboard (plan: + compliance) |
| `POST …/contracts` (draft from the vendor's IO); `GET …/contracts/{kid}`; `POST …/contracts/{kid}/approve` | agent, dashboard; readers; Andre |
| `POST …/commercial-model`, `…/client-approval`, `…/funds-verifications`, `…/funds-reconfirmations`, `…/authorizations` | Andre |
| `GET …/authorizations/{aid}`; `POST …/authorizations/{aid}/submit`, `…/outcome` | readers; Andre |
| `POST /jobs/integrity/run` | scheduler |
| `GET /audit/integrity`, `/audit/export`, `/audit/evidence` | dashboard, compliance |

## Environment

| Variable | Meaning |
|---|---|
| `OOH_SERVICE_TOKEN` | bearer token (32..512 printable ASCII); required |
| `OOH_CALLER_TOKENS` | JSON `{caller: token}` for `dashboard`, `ooh_agent`, `scheduler`, `hub`, `compliance_38` |
| `OOH_TENANT_TOKENS` | JSON `{tenant_id: token}` for the hub's client views |
| `OOH_ANDRE_APPROVAL_TOKEN` | Andre's approval token; unset or equal to any other token = every approval refused |
| `OOH_DATA_DIR` | absolute, owned, mode 700 directory for the hash-chained log; required in production |
| `OOH_NON_PRODUCTION` | `1` allows an in-memory run without a data directory (tests only) |
| `OOH_KILL_GLOBAL`, `OOH_KILLED_CAPABILITIES` | kill switches set at start (capabilities: campaigns, inventory, prices, funds, purchase, submission, registry); cannot be released at runtime |
| `OOH_MAX_PURCHASE_AMOUNT` | the per-authorization spend limit (default and locked ceiling 10000.00; may only be lowered) |
| `OOH_AUTHORITY_MAX_DAYS` | the longest an authorization may live (default and locked ceiling 7; may only be lowered) |
| `OOH_INVENTORY_MAX_PER_CAMPAIGN` | units per campaign (default 200) |
| `OOH_BIND_ADDR`, `OOH_PORT` | default 127.0.0.1:8510 |
| `OOH_REQUEST_HEAD_TIMEOUT_SECONDS`, `OOH_KEEP_ALIVE_TIMEOUT_SECONDS`, `OOH_LIMIT_CONCURRENCY`, `OOH_SWITCH_INTERVAL_SECONDS`, `OOH_DRAINS_MAX` | the hardened launcher (`src/serve.py`, seo-py's) |
| `LEDGER_SERVICE_URL`, `LEDGER_SERVICE_TOKEN` | the evidence ledger; unset = every write refused |

NOT_BUILT (setting any refuses start, rather than pretend): `OOH_FINANCE_URL` (Finance 31 funds client),
`OOH_VENDOR_PORT_URL` (vendor ordering), `OOH_ESIGN_URL` (e-signature), `OOH_INVENTORY_PROVIDER` (inventory / DOOH
supply data).

## Layout

`src/policy.py` the constitution as data · `src/svc_campaigns.py` the spine · `src/svc_inventory.py` the OOH-2
registry (markets, sources, faces, suppliers, avails; [ADR 0020](../../docs/adr/0020-ooh-inventory-operator-registry.md)) · `src/svc_planning.py` OOH-3
planning and pricing (brief, rate-card estimates, plan, markup evidence; [ADR 0021](../../docs/adr/0021-ooh-planning-and-pricing.md)) · `src/svc_contracts.py`
OOH-4 vendor contracts and the gate's contract checks ([ADR 0022](../../docs/adr/0022-ooh-buy-and-contract-controls.md)) · `src/svc_tenants.py` tenants and kill
switches · `src/service.py` record-first plumbing (seo-py's) · `src/api.py` routes and limits · `src/store.py`,
`src/ledger.py`, `src/founder.py`, `src/serve.py` (seo-py's) · `src/graceful_close.py`, `src/launch_guard.py`
(shared, byte-identical) · `tests/` (hostile tests of every gate; clocks injected) · `devtools/live_run.py` (the real
ledger-rust binary and this service's production entrypoint over HTTP).
