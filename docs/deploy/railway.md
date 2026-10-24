# Deploying the first-revenue services on Railway

Go-live plan, Days 1-2. This is the runbook for putting the services needed for first revenue on Railway, and the
dashboard on Vercel. Nothing in this repository holds a secret value. Nothing here has been applied to Railway or
Vercel: the steps in [Andre's steps](#andres-steps) do that.

What is in the repository:

| Piece | Where |
|---|---|
| One production Dockerfile per Railway service | `services/ledger-rust/Dockerfile`, `services/detection-py/Dockerfile`, `services/finance-py/Dockerfile`, `services/legal-py/Dockerfile`, `services/seo-py/Dockerfile`, `services/orchestrator-go/Dockerfile.orchestrator`, `services/orchestrator-go/Dockerfile.stripe-gateway` |
| The entrypoint every image starts with | `deploy/docker/zbm-entrypoint.sh` |
| The Python images' dependency lock (every transitive package, with hashes) | `deploy/python/requirements.in` → `deploy/python/requirements.lock.txt` |
| Railway infrastructure as code | `.railway/railway.ts` (SDK pinned in `.railway/package.json` + lockfile) |
| CI: build every image, smoke every container | job `railway-images` in `.github/workflows/ci.yml`, script `devtools/railway_smoke.py` |
| CI: these files agree with each other and with the code | `devtools/test_deploy_config.py` (job `hygiene-static`) |

**Why `.railway/railway.ts` and not `railway.json` / `railway.toml`.** Railway deprecated Config as Code: new services
cannot opt into it, and existing `railway.json` / `railway.toml` files stop being read on 2026-12-01 (docs.railway.com,
"Config as Code", read 2026-10-24). Its replacement is Infrastructure as Code, `.railway/railway.ts`, applied with the
Railway CLI (`railway config plan`, then `railway config apply`). A git push does not apply it. Every field used was
checked against the Railway docs and against the TypeScript types of the pinned SDK (`railway@3.13.1`); the file
typechecks and evaluates to the intended configuration with that SDK.

## The services

| Railway service | Code | Dockerfile | Port | Reachable from | Volume | Replicas |
|---|---|---|---|---|---|---|
| `ledger` | `services/ledger-rust` (`server` binary) | `services/ledger-rust/Dockerfile` | 8090 | private network | `ledger-data` at `/data` | 1 |
| `detection` | `services/detection-py` (Revenue Recovery detection agents) | `services/detection-py/Dockerfile` | 8000 | private network | — | 1 |
| `legal` | `services/legal-py` (Legal 37) | `services/legal-py/Dockerfile` | 8420 | private network | `legal-data` at `/data` | 1 |
| `finance` | `services/finance-py` (Finance 31) | `services/finance-py/Dockerfile` | 8410 | private network | `finance-data` at `/data` | 1 |
| `orchestrator` | `services/orchestrator-go`, `cmd/orchestrator` (Revenue Recovery orchestrator) | `services/orchestrator-go/Dockerfile.orchestrator` | 8080 | **public** (the dashboard on Vercel calls it) | — | 1 |
| `stripe-gateway` | `services/orchestrator-go`, `cmd/stripe-gateway` | `services/orchestrator-go/Dockerfile.stripe-gateway` | 8470 | **public** (Stripe calls it) | — | 1 |
| `seo` | `services/seo-py` (Search & Answer Intelligence 2) | `services/seo-py/Dockerfile` | 8500 | private network | `seo-data` at `/data` | 1 |
| dashboard | `apps/dashboard-ts` (Next.js) | — (Vercel builds it) | — | public, password session | — | Vercel |

**The Stripe gateway is in orchestrator-go — verified.** `services/orchestrator-go/cmd/stripe-gateway/main.go` is a
second binary in the orchestrator-go module (its own `main`, its own README section). It is the only piece Stripe
calls (`POST /webhooks/stripe`); it holds no Stripe secret and forwards the raw body and `Stripe-Signature` to Finance
(`POST /fin/v1/stripe/events`), which verifies the signature and reads the object back from Stripe. It is deployed as
its own Railway service from its own Dockerfile.

**The revenue recovery service is detection-py**, run behind orchestrator-go with ledger-rust as its evidence store
(root README; ADR 0001 decisions 2-3 and "Scope boundary"). The scan's only data source is the repository's fixture
pool (`fixtures/*.json`, copied into the detection image); a real-store translation layer is not built (ADR 0001
decision 5).

**One replica everywhere.** The ledger and the three departments each hold a single-writer lock on their data
directory (`<log>.lock` for the ledger, an flock in each department's data directory), and Railway does not allow
replicas on a service with a volume. The orchestrator's one-scan-at-a-time lock is in-process (ADR 0001, "Scans"), so
it runs one process per deployment. Railway also refuses overlapping deployments on the same volume, so a redeploy of a
volume service has a short downtime (docs.railway.com, "Volumes").

### Startup order and service URLs

The ledger comes first. finance-py, legal-py and seo-py verify their local log against the ledger at start, and refuse
to start when it cannot be read (`refusing to start: the local log cannot be verified against the evidence ledger`).
They are retried (restart policy below), but deploy the ledger first and wait for it to be Active.

```
ledger  ←  legal  ←  finance  ←  stripe-gateway          (each arrow: "calls")
   ↑          ↖________ seo
   ↑                      ↘ finance (invoice status)
   └──────  orchestrator  →  detection
                ↑
       dashboard (Vercel, over the orchestrator's public domain)
```

| From | To | URL (set by `.railway/railway.ts`) |
|---|---|---|
| orchestrator | detection | `http://detection.railway.internal:8000` |
| orchestrator, finance, legal, seo | ledger | `http://ledger.railway.internal:8090` |
| finance | legal | `http://legal.railway.internal:8420` |
| stripe-gateway, seo | finance | `http://finance.railway.internal:8410` |
| dashboard (Vercel) | orchestrator | `https://<orchestrator's Railway domain>` |
| Stripe | stripe-gateway | `https://<stripe-gateway's Railway domain>/webhooks/stripe` |

Railway's private network: `<service>.railway.internal`, encrypted with WireGuard, not routed from the internet;
Railway advises plain `http://` on it (docs.railway.com, "Private networking: how it works"). In environments created
after 2025-10-16 the names resolve to IPv4 and IPv6; older environments are IPv6-only. The services therefore bind
`::` (Python, stripe-gateway) or `[::]` (ledger, orchestrator: they join host and port with a colon, so the brackets
are needed), which on Linux is dual-stack. If the service names are changed in Railway, change the URLs with them
(and `STRIPE_GATEWAY_FINANCE_PRIVATE_NAME` with finance's name).

## How the images start

Every image runs `deploy/docker/zbm-entrypoint.sh`, then the service. The entrypoint adapts the platform to the
services; it changes no service guard:

- **Port.** Railway injects `PORT` and health-checks on it. The services read their own variable (`LEDGER_PORT`,
  `FIN_PORT`, ...); the entrypoint sets it from `PORT` (`--port-var`). Both set to different values refuses the start.
  `.railway/railway.ts` sets `PORT` to each service's usual port so the private URLs above are stable.
- **Bind.** Every service still binds `127.0.0.1` unless its own bind variable says otherwise (`LEDGER_BIND_ADDR`,
  `ORCHESTRATOR_BIND_ADDR`, `STRIPE_GATEWAY_BIND_ADDR`, `FIN_BIND_ADDR`, `LEGAL_BIND_ADDR`, `SEO_BIND_ADDR`, and the
  image's `DETECTION_BIND_ADDR` for detection-py's `--host`): the existing opt-in, set by `.railway/railway.ts`.
- **State on the volume.** The volume is mounted at `/data`; each service keeps its state in a subdirectory named by
  its own variable: `LEDGER_LOG_PATH=/data/ledger/ledger.jsonl` (the lock and head checkpoint sit next to it),
  `FIN_DATA_DIR=/data/finance`, `LEGAL_DATA_DIR=/data/legal`, `SEO_DATA_DIR=/data/seo`. The entrypoint creates the
  directory, owned by the service user, mode 0700 (seo-py refuses anything else). `requiredMountPath: "/data"` keeps
  Railway from deploying a stateful service without its volume.
- **Non-root.** The images run as uid/gid 10001; the code is root-owned and read-only to it. A Railway volume is
  mounted owned by root, which uid 10001 cannot write, so the four services with a volume set `RAILWAY_RUN_UID=0`
  (Railway's documented switch for this case): the entrypoint then starts as root only to create the data directory
  and the secret files and hand them to 10001, and starts the service as 10001 with `setpriv --reuid=10001
  --regid=10001 --clear-groups --no-new-privs`. The service process never runs as root; started as root without
  `setpriv`, the entrypoint refuses.
- **Signals.** The entrypoint stays PID 1 and forwards SIGTERM, and SIGINT as SIGTERM (a backgrounded child ignores
  SIGINT, so forwarding it as INT stopped nothing; AEGIS L2). Run directly as PID 1, ledger-rust ignores
  SIGTERM (it re-raises it with the default disposition, which the kernel drops for PID 1) and would be killed
  mid-write at the end of Railway's draining window; checked in a PID namespace on the build box.
- **`*_FILE` secrets** — next section.

### How `*_FILE` secrets map onto Railway

Some settings are a path to a file holding a secret, owner-only (the services refuse a group- or world-readable
file, a symlink, or a file owned by another user). Railway holds variables, not files. So: set the **value** in a
Railway variable named like the setting **without `_FILE`**; the entrypoint writes it to
`/run/zbm-secrets/<name>` (inside the container, mode 0600, owned by uid 10001), points the `*_FILE` setting at it,
and starts the service without the value variable. Setting both the value and the `*_FILE` variable refuses the
start.

What "without the value variable" does and does not cover (AEGIS L3): the service process's environment does not
hold the value, but the value stays (1) a Railway variable — visible in the dashboard and to anyone with `railway ssh`
on the service — and (2) in the entrypoint's own initial environment, `/proc/1/environ`, for the life of the
container. Only a different uid keeps the service from reading (2): the entrypoint runs as root and the service as
uid 10001. So the entrypoint writes a secret file only when it runs as root (`RAILWAY_RUN_UID=0`, which the three
services with these secrets — ledger, finance, seo — already set for their volume); started as any other user with a
value variable set, it refuses with `--secret-file <NAME>_FILE: <NAME> is set but this container runs as uid ...,
not root`. The CI smoke starts seo-py as root for this reason and checks that the file is 0600/10001, that the value
is not in the service's environment, that uid 10001 cannot read `/proc/1/environ`, and that the non-root start
refuses.

| Railway variable (the value) | Written to | Read by |
|---|---|---|
| `FIN_STRIPE_SECRET_KEY` | `FIN_STRIPE_SECRET_KEY_FILE` | finance |
| `FIN_STRIPE_WEBHOOK_SECRET` | `FIN_STRIPE_WEBHOOK_SECRET_FILE` | finance |
| `SEO_LOG_HASH_KEY` | `SEO_LOG_HASH_KEY_FILE` | seo |
| `LEDGER_CALLERS` | `LEDGER_CALLERS_FILE` | ledger |

Every other `*_FILE` setting the code reads (`SEO_OPENAI_API_KEY_FILE`, `SEO_ANTHROPIC_API_KEY_FILE`,
`SEO_GOOGLE_API_KEY_FILE`, `SEO_PERPLEXITY_API_KEY_FILE`, `SEO_SEARCH_CONSOLE_CREDENTIALS_FILE`,
`SEO_BING_WEBMASTER_KEY_FILE`) belongs to a port that is not built: setting it refuses the start. Leave them unset.

## Secrets

Generate every token on your own machine and paste it straight into Railway (or Vercel). Never commit one, never put
one in a chat or a ticket. Mark each one **Sealed** in Railway (its value is then hidden after it is set). Each must
be a different value: several services refuse to start when two of their tokens are equal.

| Secret | Generate with | Set on (owner) | Also used by (references, not copies) |
|---|---|---|---|
| `ZBM_SERVICE_TOKEN` | `openssl rand -hex 32` | detection | orchestrator `DETECTION_SERVICE_TOKEN` (reference) |
| `ORCHESTRATOR_SERVICE_TOKEN` | `openssl rand -hex 32` | orchestrator | Vercel `ORCHESTRATOR_SERVICE_TOKEN` (paste the same value; Vercel cannot reference Railway) |
| `LEDGER_SERVICE_TOKEN` (orchestrator's) | `openssl rand -hex 32` | orchestrator | its SHA-256 goes in `LEDGER_CALLERS` |
| `LEDGER_SERVICE_TOKEN` (finance's) | `openssl rand -hex 32` | finance | its SHA-256 goes in `LEDGER_CALLERS` |
| `LEDGER_SERVICE_TOKEN` (legal's) | `openssl rand -hex 32` | legal | its SHA-256 goes in `LEDGER_CALLERS` |
| `LEDGER_SERVICE_TOKEN` (seo's) | `openssl rand -hex 32` | seo | its SHA-256 goes in `LEDGER_CALLERS` |
| `LEDGER_CALLERS` | the script below (hashes only, no token) | ledger | — |
| `FIN_SERVICE_TOKEN` | `openssl rand -hex 32` | finance | stripe-gateway `STRIPE_GATEWAY_FINANCE_TOKEN`, seo `SEO_FINANCE_TOKEN` (references) |
| `FIN_ANDRE_APPROVAL_TOKEN` | `openssl rand -hex 32` | finance | you (keep it in your password manager) |
| `STRIPE_GATEWAY_CALLER_TOKEN` | `openssl rand -hex 32` | stripe-gateway | finance `FIN_CALLER_TOKENS` (`rail_gateway`, interpolated) |
| `SEO_FINANCE_CALLER_TOKEN` | `openssl rand -hex 32` | seo | finance `FIN_CALLER_TOKENS` (`seo_02`, interpolated) |
| `FIN_LEGAL_CALLER_TOKEN` | `openssl rand -hex 32` | finance | legal `LEGAL_CALLER_TOKENS` (`finance_31`, interpolated) |
| `LEGAL_SERVICE_TOKEN` | `openssl rand -hex 32` | legal | finance `FIN_LEGAL_TOKEN` (reference) |
| `LEGAL_ANDRE_APPROVAL_TOKEN` | `openssl rand -hex 32` | legal | you |
| `SEO_SERVICE_TOKEN` | `openssl rand -hex 32` | seo | — |
| `SEO_ANDRE_APPROVAL_TOKEN` | `openssl rand -hex 32` | seo | you |
| `SEO_CALLER_TOKENS` | `{"dashboard": "<openssl rand -hex 32>"}` | seo | you (the `dashboard` caller is how your approvals reach seo) |
| `SEO_LOG_HASH_KEY` | `openssl rand -hex 32` | seo | — (becomes `SEO_LOG_HASH_KEY_FILE`) |
| `FIN_STRIPE_SECRET_KEY` | Stripe dashboard → Developers → API keys → create a **restricted** key (test mode first: `rk_test_...`) with write on Checkout Sessions and read on PaymentIntents, Charges, Disputes, Payouts and Balance | finance | — (becomes `FIN_STRIPE_SECRET_KEY_FILE`) |
| `FIN_STRIPE_WEBHOOK_SECRET` | Stripe, when you register the webhook endpoint (step 7): the endpoint's signing secret, `whsec_...` | finance | — (becomes `FIN_STRIPE_WEBHOOK_SECRET_FILE`) |
| `DASHBOARD_SESSION_SECRET` | `openssl rand -hex 32` | Vercel | — |
| `DASHBOARD_PASSWORD_HASH` | `cd apps/dashboard-ts && npm run hash-password` (prompts for the password; prints `scrypt$...`) | Vercel | — |

`openssl rand -hex 32` gives 64 hex characters (256 bits): every service that checks a length wants at least 32, and
the ledger and the orchestrator cannot rate-limit guesses, so the token's entropy is the defence.

**`LEDGER_CALLERS`** (per-caller ledger tokens with department scopes; services/ledger-rust/README.md). It holds the
SHA-256 of each caller's token, never a token. Run this on your machine; it asks for the four ledger tokens without
echoing them and prints the JSON to paste into the ledger's `LEDGER_CALLERS`:

```bash
python3 - <<'PY'
import getpass, hashlib, json
callers = [("orchestrator-go", "revenue_recovery"), ("finance-py", "finance"), ("legal-py", "legal"), ("seo-py", "seo")]
out = {}
for name, department in callers:
    token = getpass.getpass(f"LEDGER_SERVICE_TOKEN you set on {name.split('-')[0]}: ")
    out[hashlib.sha256(token.encode()).hexdigest()] = {"caller": name, "departments": [department], "scope": "write"}
print(json.dumps(out))
PY
```

With `LEDGER_CALLERS` set and no `LEDGER_SERVICE_TOKEN` on the ledger, there is no shared token: each service can
write and read only its own department. Rotating one: new token on the service, re-run the script, update
`LEDGER_CALLERS`, redeploy the ledger, then the service.

**Interpolated maps.** `FIN_CALLER_TOKENS` and `LEGAL_CALLER_TOKENS` are JSON maps built by `.railway/railway.ts`
from other services' variables with Railway's reference syntax, e.g.
`{"rail_gateway": "${{stripe-gateway.STRIPE_GATEWAY_CALLER_TOKEN}}", "seo_02": "${{seo.SEO_FINANCE_CALLER_TOKEN}}"}`.
You never type those two; you set the tokens they point at.

## Environment variables

Every variable each deployed service reads, taken from its code (`devtools/test_deploy_config.py` fails when the code
reads one this page does not name). "IaC" = set by `.railway/railway.ts`; "you" = you set the value once in Railway;
"ref" = a Railway reference to another service's variable. Defaults are the code's; leave them unless a row says
otherwise. Examples are never real values.

### ledger (ledger-rust)

| Variable | Required | Secret | Value / example | Notes |
|---|---|---|---|---|
| `PORT` | yes | no | `8090` (IaC) | Railway's port; the entrypoint copies it to `LEDGER_PORT` |
| `LEDGER_PORT` | — | no | (from `PORT`) | do not set separately |
| `LEDGER_BIND_ADDR` | yes | no | `[::]` (IaC) | default `127.0.0.1` |
| `LEDGER_LOG_PATH` | yes | no | `/data/ledger/ledger.jsonl` (IaC; image default) | on the volume; `<log>.lock`, `<log>.head` next to it |
| `RAILWAY_RUN_UID` | yes | no | `0` (IaC) | entrypoint starts as root to hand `/data/ledger` to uid 10001 |
| `LEDGER_CALLERS` | yes | yes | (you; script above) | becomes `LEDGER_CALLERS_FILE` |
| `LEDGER_CALLERS_FILE` | — | — | (set by the entrypoint) | do not set yourself |
| `LEDGER_SERVICE_TOKEN` | no | yes | **leave unset** | the shared token; unset = only per-caller tokens work |
| `LEDGER_ALLOW_SHARED_TOKEN` | no | no | unset | `1` would accept the shared token next to the callers file; not needed |
| `LEDGER_MAX_CONNECTIONS` | no | no | `512` | beyond it an immediate 503 |
| `LEDGER_ALLOW_RESET` | no | no | unset | one-shot operator override after a restore; set only to the value a refusal prints, start once, unset (ledger README) |
| `LEDGER_MIGRATE_LEGACY` | no | no | unset | one-shot, for a pre-Oct-6-2026 log; not needed for a new ledger |
| `LEDGER_PORT_FILE` | no | no | unset | test harness only |

### detection (detection-py)

| Variable | Required | Secret | Value / example | Notes |
|---|---|---|---|---|
| `PORT` | yes | no | `8000` (IaC) | copied to `DETECTION_PORT` |
| `DETECTION_PORT` | — | no | (from `PORT`) | the image's name for `serve.py --port` (default 8000) |
| `DETECTION_BIND_ADDR` | yes | no | `::` (IaC) | the image's name for `serve.py --host` (default `127.0.0.1`) |
| `ZBM_SERVICE_TOKEN` | yes | yes | (you) | the token the orchestrator presents; refuses to start without it |
| `DETECTION_SWITCH_INTERVAL_SECONDS` | no | no | `0.001` | launcher tuning, 0.0001..0.05 |
| `DETECTION_DRAINS_MAX` | no | no | `512` | concurrent graceful-close drains |

### orchestrator (orchestrator-go `cmd/orchestrator`)

| Variable | Required | Secret | Value / example | Notes |
|---|---|---|---|---|
| `PORT` | yes | no | `8080` (IaC) | copied to `ORCHESTRATOR_PORT` |
| `ORCHESTRATOR_PORT` | — | no | (from `PORT`) | |
| `ORCHESTRATOR_BIND_ADDR` | yes | no | `[::]` (IaC) | default `127.0.0.1` |
| `ORCHESTRATOR_SERVICE_TOKEN` | yes | yes | (you) | callers (the dashboard) present it; refuses to start without it |
| `DETECTION_SERVICE_URL` | yes | no | `http://detection.railway.internal:8000` (IaC) | → detection |
| `DETECTION_SERVICE_TOKEN` | yes | yes | ref `detection.ZBM_SERVICE_TOKEN` (IaC) | |
| `LEDGER_SERVICE_URL` | yes | no | `http://ledger.railway.internal:8090` (IaC) | → ledger |
| `LEDGER_SERVICE_TOKEN` | yes | yes | (you) | the orchestrator's own ledger caller token |
| `ORCHESTRATOR_PORT_FILE` | no | no | unset | test harness only |

### stripe-gateway (orchestrator-go `cmd/stripe-gateway`)

| Variable | Required | Secret | Value / example | Notes |
|---|---|---|---|---|
| `PORT` | yes | no | `8470` (IaC) | copied to `STRIPE_GATEWAY_PORT` |
| `STRIPE_GATEWAY_PORT` | — | no | (from `PORT`) | |
| `STRIPE_GATEWAY_BIND_ADDR` | yes | no | `::` (IaC) | default `127.0.0.1` |
| `STRIPE_GATEWAY_FINANCE_URL` | yes | no | `http://finance.railway.internal:8410` (IaC) | → finance; a URL with a user or password (`user@host`) is refused in every mode (AEGIS L4), and the startup log prints it with any userinfo removed |
| `STRIPE_GATEWAY_ALLOW_RAILWAY_PRIVATE_HTTP` | yes on Railway | no | `1` (IaC) | without it plain http is accepted only to a loopback Finance (AEGIS L8); `1` also accepts exactly `http://<STRIPE_GATEWAY_FINANCE_PRIVATE_NAME>.railway.internal[:port]` (no path, query or user) and no other private-network host (AEGIS L1). Added for this deploy; **needs AEGIS review** |
| `STRIPE_GATEWAY_FINANCE_PRIVATE_NAME` | no | no | `finance` (IaC; the default when unset) | the one Railway service name the opt-in reaches: a single lowercase DNS label, never `localhost`. `ledger.railway.internal`, `localhost.railway.internal` or any other name is refused |
| `STRIPE_GATEWAY_FINANCE_TOKEN` | yes | yes | ref `finance.FIN_SERVICE_TOKEN` (IaC) | ≥ 32 characters |
| `STRIPE_GATEWAY_CALLER_TOKEN` | yes | yes | (you) | finance's `rail_gateway` caller token; must differ from the Finance token |

### finance (finance-py)

Set by IaC or by you:

| Variable | Required | Secret | Value / example | Notes |
|---|---|---|---|---|
| `PORT` | yes | no | `8410` (IaC) | copied to `FIN_PORT` |
| `FIN_PORT` | — | no | (from `PORT`) | |
| `FIN_BIND_ADDR` | yes | no | `::` (IaC) | default `127.0.0.1` |
| `FIN_DATA_DIR` | yes | no | `/data/finance` (IaC; image default) | unset would be in-memory (nothing survives a restart) |
| `RAILWAY_RUN_UID` | yes | no | `0` (IaC) | |
| `LEDGER_SERVICE_URL` | yes | no | `http://ledger.railway.internal:8090` (IaC) | → ledger |
| `LEDGER_SERVICE_TOKEN` | yes | yes | (you) | finance's own ledger caller token |
| `FIN_SERVICE_TOKEN` | yes | yes | (you) | ≥ 32 printable characters |
| `FIN_ANDRE_APPROVAL_TOKEN` | yes for anything to be approved | yes | (you) | header `X-Andre-Approval-Token` |
| `FIN_CALLER_TOKENS` | yes | yes | interpolated map (IaC): `rail_gateway`, `seo_02` | other callers (`scheduler`, `onboarding`, ...) are not deployed yet |
| `FIN_LEGAL_URL` | yes | no | `http://legal.railway.internal:8420` (IaC) | → legal. Without Legal no invoice can issue |
| `FIN_LEGAL_TOKEN` | yes | yes | ref `legal.LEGAL_SERVICE_TOKEN` (IaC) | |
| `FIN_LEGAL_CALLER_TOKEN` | yes | yes | (you) | Finance's `finance_31` caller token at Legal |
| `FIN_STRIPE_INCOMING` | yes | no | `1` (IaC) | Stripe client payments on; then the four settings below are required |
| `FIN_STRIPE_LIVE` | yes | no | `0` (IaC) → `1` at go-live | `1` only with `sk_live_`/`rk_live_` keys |
| `FIN_STRIPE_SECRET_KEY` | yes | yes | (you; Stripe) | becomes `FIN_STRIPE_SECRET_KEY_FILE` |
| `FIN_STRIPE_WEBHOOK_SECRET` | yes | yes | (you; Stripe) | becomes `FIN_STRIPE_WEBHOOK_SECRET_FILE` |
| `FIN_STRIPE_SECRET_KEY_FILE`, `FIN_STRIPE_WEBHOOK_SECRET_FILE` | — | — | (set by the entrypoint) | do not set yourself |
| `FIN_STRIPE_SUCCESS_URL` | yes | no | `https://zbestmedia.example/payment/thanks` (you) | the https page Stripe returns the client to |
| `FIN_STRIPE_CANCEL_URL` | yes | no | `https://zbestmedia.example/payment/cancelled` (you) | |

Everything else finance-py reads — leave at the default (services/finance-py/README.md has the meaning of each):

| Variable | Default | Notes |
|---|---|---|
| `FIN_REVENUE_MODEL`, `FIN_CUSTODY_MODEL`, `FIN_STRIPE_LOSSES` | `principal`, `own_deposit`, `stripe` | founder-locked: any other value refuses to start |
| `FIN_UNMATCHED_TIN_POLICY` | `block` | or `withhold_24` |
| `FIN_GL` | `qbo` | or `none`; the QBO adapter is a stand-in |
| `FIN_RAILS` | unset | may only name a subset of `stripe,trolley` |
| `FIN_RAIL_STRIPE`, `FIN_RAIL_TROLLEY`, `FIN_BANK_FEED`, `FIN_TAX_AGENT`, `FIN_VAULT`, `FIN_CN_URL`, `FIN_PEOPLE_URL`, `FIN_PUSH_URL`, `FIN_IDENTITY_HMAC_KEY` | unset | not built: setting any refuses to start |
| `FIN_RAIL_REVERSAL_ENABLED`, `FIN_LATE_FEES` | unset | `1` refuses to start |
| `FIN_REFUND_ADMIN_FEE_PCT`, `FIN_RESERVE_PCT` | `0` | must stay 0 |
| `FIN_VI_URL` / `FIN_VI_TOKEN` / `FIN_VI_CALLER_TOKEN`, `FIN_COMPLIANCE_URL` / `FIN_COMPLIANCE_TOKEN` / `FIN_COMPLIANCE_CALLER_TOKEN` | unset | thin clients to services not deployed; all three or none |
| `FIN_SECOND_APPROVER_TOKEN`, `FIN_DUAL_HUMAN_THRESHOLD` | unset, `0.00` | optional second approver for payout batches |
| `FIN_RUN_WEEKDAY`, `FIN_RUN_LOCAL_TIME`, `FIN_RECON_LOCAL_TIME`, `FIN_CLOSE_WORKDAYS` | `FRI`, `10:00`, `07:00`, `5` | schedule |
| `FIN_LIMIT_PAYEE_RUN`, `FIN_LIMIT_PAYEE_30D`, `FIN_LIMIT_FIRST_PAYOUT`, `FIN_LIMIT_BATCH_NET`, `FIN_LIMIT_BATCH_ITEMS` | `2500.00`, `10000.00`, `500.00`, `50000.00`, `500` | payout limits |
| `FIN_MIN_PAYOUT`, `FIN_FINAL_PAYOUT_MIN`, `FIN_RESTRICTED_BUFFER` | `10.00`, `0.01`, `0.00` | |
| `FIN_1099NEC_THRESHOLD_2026` (and optional `FIN_1099NEC_THRESHOLD_2027` … `_2030`) | `2000.00` | |
| `FIN_PAYEE_CHANGE_COOLING_OFF_H`, `FIN_RELEASE_DELAY_H`, `FIN_APPROVAL_TTL_H` | `72`, `12`, `48` | hours |
| `FIN_ACCESS_REVIEW_DAYS`, `FIN_OFAC_MAX_AGE_DAYS`, `FIN_CLAWBACK_WRITEOFF_MIN_DAYS`, `FIN_EVIDENCE_RETENTION_DAYS` | `90`, `1`, `180`, `2557` | days |
| `FIN_MAX_RATE_PER_1000`, `FIN_MAX_CERTIFIED_VIEWS`, `FIN_WITHHOLDING_BASIS` | `1000.00`, 10^12, `gross` | |
| `FIN_MEDIA_DEFAULT_MARKUP_PCT`, `FIN_MEDIA_MAX_MARKUP_PCT`, `FIN_MEDIA_RELEASE_HOLD_BD` | `15.00`, `100.00`, `2` | media buys |
| `FIN_CARD_PREPAYMENTS`, `FIN_CARD_MAX_INVOICE` | off, `5000.00` | card rule (founder M10) |
| `FIN_SEED_PATH`, `FIN_SEED_SHA256`, `FIN_ALLOW_UNPINNED_SEED` | the image's pinned seed, unset, unset | an unpinned seed is non-production only |
| `FIN_RECONCILE_MODE` | unset | `1` only while reconciling after a refused start (finance README) |
| `FIN_REQUEST_HEAD_TIMEOUT_SECONDS`, `FIN_KEEP_ALIVE_TIMEOUT_SECONDS`, `FIN_LIMIT_CONCURRENCY`, `FIN_SWITCH_INTERVAL_SECONDS`, `FIN_DRAINS_MAX` | `10`, `5`, `128`, `0.001`, `512` | launcher tuning |

### legal (legal-py)

| Variable | Required | Secret | Value / example | Notes |
|---|---|---|---|---|
| `PORT` | yes | no | `8420` (IaC) | copied to `LEGAL_PORT` |
| `LEGAL_PORT` | — | no | (from `PORT`) | |
| `LEGAL_BIND_ADDR` | yes | no | `::` (IaC) | default `127.0.0.1` |
| `LEGAL_DATA_DIR` | yes | no | `/data/legal` (IaC; image default) | |
| `RAILWAY_RUN_UID` | yes | no | `0` (IaC) | |
| `LEDGER_SERVICE_URL` | yes | no | `http://ledger.railway.internal:8090` (IaC) | → ledger |
| `LEDGER_SERVICE_TOKEN` | yes | yes | (you) | legal's own ledger caller token |
| `LEGAL_SERVICE_TOKEN` | yes | yes | (you) | |
| `LEGAL_ANDRE_APPROVAL_TOKEN` | yes for anything to be approved | yes | (you) | |
| `LEGAL_CALLER_TOKENS` | yes | yes | interpolated map (IaC): `finance_31` | |
| `LEGAL_COMPLIANCE_URL` / `LEGAL_COMPLIANCE_TOKEN` / `LEGAL_COMPLIANCE_CALLER_TOKEN` | no | — | unset | Compliance is not deployed: the stand-in (proposals stay `pending_delivery`) |
| `LEGAL_COUNSEL_CHANNEL`, `LEGAL_ESIGN_PROVIDER` | no | — | unset | not built: setting either refuses to start |
| `LEGAL_PORTAL_FAQ` | no | no | `0` | `1` refuses to start |
| `LEGAL_AGENT_MAX_FALLBACK`, `LEGAL_DISPUTE_THRESHOLD`, `LEGAL_MEMO_REVIEW_DAYS`, `LEGAL_HOLD_RENOTICE_DAYS`, `LEGAL_FILING_ALERT_DAYS`, `LEGAL_ESIGN_CONSENT_REQUIRED`, `LEGAL_BUSINESS_TZ` | no | no | `1`, `5000.00`, `90`, `90`, `60`, `1`, `America/Los_Angeles` | services/legal-py/README.md |
| `LEGAL_SEED_DIR`, `LEGAL_SEED_SHA256`, `LEGAL_ALLOW_UNPINNED_SEED` | no | no | the image's pinned seed, unset, unset | |
| `LEGAL_RECONCILE_MODE` | no | no | `0` | |
| `LEGAL_REQUEST_HEAD_TIMEOUT_SECONDS`, `LEGAL_KEEP_ALIVE_TIMEOUT_SECONDS`, `LEGAL_LIMIT_CONCURRENCY`, `LEGAL_SWITCH_INTERVAL_SECONDS`, `LEGAL_DRAINS_MAX` | no | no | `10`, `5`, `128`, `0.001`, `512` | launcher tuning |

### seo (seo-py)

| Variable | Required | Secret | Value / example | Notes |
|---|---|---|---|---|
| `PORT` | yes | no | `8500` (IaC) | copied to `SEO_PORT` (must be 1024..65535) |
| `SEO_PORT` | — | no | (from `PORT`) | |
| `SEO_BIND_ADDR` | yes | no | `::` (IaC) | default `127.0.0.1` |
| `SEO_DATA_DIR` | yes | no | `/data/seo` (IaC; image default) | owner-only, 0700 |
| `RAILWAY_RUN_UID` | yes | no | `0` (IaC) | |
| `LEDGER_SERVICE_URL` | yes | no | `http://ledger.railway.internal:8090` (IaC) | → ledger |
| `LEDGER_SERVICE_TOKEN` | yes | yes | (you) | seo's own ledger caller token |
| `SEO_SERVICE_TOKEN` | yes | yes | (you) | |
| `SEO_ANDRE_APPROVAL_TOKEN` | yes for anything to be approved | yes | (you) | only through the `dashboard` caller |
| `SEO_CALLER_TOKENS` | yes (for you to operate it) | yes | `{"dashboard": "<token>"}` (you) | callers `dashboard`, `seo_agent`, `scheduler`, `hub`, `finance_31`, `compliance_38` |
| `SEO_TENANT_TOKENS` | no | yes | unset | a client's read-only view through the hub (not deployed) |
| `SEO_LOG_HASH_KEY` | yes | yes | (you) | becomes `SEO_LOG_HASH_KEY_FILE` (required with a data dir) |
| `SEO_LOG_HASH_KEY_FILE` | — | — | (set by the entrypoint) | |
| `SEO_FINANCE_URL` | yes | no | `http://finance.railway.internal:8410` (IaC) | → finance (invoice status) |
| `SEO_FINANCE_TOKEN` | yes | yes | ref `finance.FIN_SERVICE_TOKEN` (IaC) | |
| `SEO_FINANCE_CALLER_TOKEN` | yes | yes | (you) | seo's `seo_02` caller token at Finance |
| `SEO_INVOICE_VERIFICATION` | no | no | `finance` | `trust` skips the invoice check (not for production) |
| `SEO_NON_PRODUCTION` | no | no | `0` | `1` = in-memory, tests only |
| `SEO_KILL_GLOBAL`, `SEO_KILLED_CAPABILITIES`, `SEO_KILLED_PROVIDERS` | no | no | `0`, empty, empty | kill switches at start (sticky) |
| `SEO_FETCH_TIMEOUT_SECONDS`, `SEO_FETCH_MAX_BYTES`, `SEO_FETCH_MAX_REDIRECTS`, `SEO_AUDIT_MAX_PAGES`, `SEO_PROBE_SAMPLES`, `SEO_BOT_INFO_URL` | no | no | `10`, 2 MiB, `5`, `10`, `5`, unset | fetcher and audit limits |
| `SEO_LOG_MAX_BYTES`, `SEO_LOG_RETENTION_DAYS`, `SEO_LOG_MAX_OPEN_INGESTS`, `SEO_LOG_MAX_INGESTS`, `SEO_LOG_TENANT_BYTES` | no | no | 256 MiB, `90`, `2`, `30`, 1 GiB | log ingests |
| `SEO_BOT_VERIFY_DNS`, `SEO_BOT_VERIFY_MAX` | no | no | `0`, `200` | |
| `SEO_SCHEDULE_BUDGET_RUNS`, `SEO_SCHEDULE_PERIOD_DAYS`, `SEO_FINANCE_TIMEOUT_SECONDS` | no | no | `8`, `30`, `5` | |
| `SEO_RENDERER`, `SEO_OPENAI_API_KEY_FILE`, `SEO_ANTHROPIC_API_KEY_FILE`, `SEO_GOOGLE_API_KEY_FILE`, `SEO_PERPLEXITY_API_KEY_FILE`, `SEO_SEARCH_CONSOLE_CREDENTIALS_FILE`, `SEO_BING_WEBMASTER_KEY_FILE`, `SEO_PROMPT_VOLUME_PROVIDER`, `SEO_ZERO_DAY_URL`, `SEO_ORCA_PUBLISH_URL`, `SEO_CLIENTFIX_URL` | no | — | unset | not built: setting any refuses to start |
| `SEO_REQUEST_HEAD_TIMEOUT_SECONDS`, `SEO_KEEP_ALIVE_TIMEOUT_SECONDS`, `SEO_LIMIT_CONCURRENCY`, `SEO_SWITCH_INTERVAL_SECONDS`, `SEO_DRAINS_MAX` | no | no | `10`, `5`, `128`, `0.001`, `512` | launcher tuning |

### dashboard (apps/dashboard-ts, on Vercel)

| Variable | Required | Secret | Value / example | Notes |
|---|---|---|---|---|
| `ORCHESTRATOR_URL` | yes | no | `https://orchestrator-production-1a2b.up.railway.app` | the orchestrator's public Railway domain (step 6); no trailing slash |
| `ORCHESTRATOR_SERVICE_TOKEN` | yes | yes | the orchestrator's value | server-side only (never `NEXT_PUBLIC_`) |
| `DASHBOARD_PASSWORD_HASH` | yes | yes (a hash, still keep it private) | `scrypt$...` from `npm run hash-password` | unset: every route answers 503 (fails closed) |
| `DASHBOARD_SESSION_SECRET` | yes | yes | `openssl rand -hex 32` | ≥ 32 bytes; must differ from `ORCHESTRATOR_SERVICE_TOKEN` |
| `DASHBOARD_TRUSTED_PROXY_HOPS` | decide | no | unset, or `1` | how many proxies in front append to `X-Forwarded-For` (1..5; anything else = 0). The login rate limiter keys on the entry that many hops from the end. **UNVERIFIED: whether Vercel overwrites or appends to a client-sent `X-Forwarded-For`.** With `1` and an overwriting Vercel, each client gets its own limiter key; if Vercel passes a client-written value through, a client can rotate keys. Unset on Vercel, every request shares one key "unknown" (`ZBM_DASHBOARD_PEER_HEADER` is set only by `scripts/serve.mjs`, which Vercel does not run): guessing stays globally rate-limited, but someone hammering the login can lock you out. Confirm Vercel's behaviour, then decide |
| `DASHBOARD_SESSION_TTL_SECONDS` | no | no | default | 300..604800 |
| `ORCHESTRATOR_TIMEOUT_MS` | no | no | `10000` | |
| `DASHBOARD_LEDGER_CACHE_MS` | no | no | default | `0` turns the cache off |
| `DASHBOARD_BIND_ADDR`, `ZBM_DASHBOARD_PEER_HEADER` | no | no | — | `scripts/serve.mjs` only (self-hosting); not used on Vercel |

## Andre's steps

Do these in order. Nothing in the repository creates anything; each step is yours.

1. **Merge this branch first.** `.railway/railway.ts` deploys branch `main` (`BRANCH` at the top of the file) and waits
   for its CI checks. `golive-deploy` is a work-in-progress branch: it goes through review (and AEGIS review of the
   stripe-gateway change) before it is merged.
2. **Tools.** Install the Railway CLI, version 5.42.1 or newer (`railway --version`), and Node 22. Railway plan:
   Hobby or Pro (volumes; the "Always" restart policy is not needed). Run `railway login`.
3. **Project.** In the Railway dashboard create an empty project (name `zbm-zbc`) and give Railway's GitHub app access
   to `DarksiedCEO/zbm-zbc`. In your checkout: `railway link` (pick the project and the `production` environment).
4. **Plan and apply the infrastructure.**
   ```bash
   cd .railway && npm ci && cd ..
   railway config plan      # read it: 7 services, 4 volumes, every variable; secrets show as preserved
   railway config apply     # creates the services and volumes; asks before anything destructive
   ```
   Services whose secrets are not set yet will fail their first deploy — expected, step 5 fixes it. If `plan` refuses
   because a preserved variable has no value yet (behaviour not documented by Railway; see Open items), stop there and
   bring the exact message back.
5. **Secrets.** Generate each value from [Secrets](#secrets) on your machine and set it in the Railway dashboard on the
   service in the "Set on" column (Variables tab → New Variable → mark Sealed). Do the four ledger tokens first, then
   `LEDGER_CALLERS` on the ledger with the script. When Railway offers to deploy the changes, wait until step 8.
6. **Public domains.** orchestrator → Settings → Networking → Generate Domain (port 8080). stripe-gateway → Generate
   Domain (port 8470). No other service gets a public domain.
7. **Stripe (test mode).** Create the restricted key and set `FIN_STRIPE_SECRET_KEY` on finance; set
   `FIN_STRIPE_SUCCESS_URL` / `FIN_STRIPE_CANCEL_URL`. Then Developers → Webhooks → Add endpoint:
   - URL: **`https://<stripe-gateway's Railway domain>/webhooks/stripe`**
   - events: `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
     `checkout.session.async_payment_failed`, `checkout.session.expired`, `payment_intent.succeeded`,
     `payment_intent.payment_failed`, `charge.failed`, `charge.dispute.created`, `charge.dispute.updated`,
     `charge.dispute.funds_withdrawn`, `charge.dispute.funds_reinstated`, `charge.dispute.closed`, `payout.created`,
     `payout.updated`, `payout.paid`, `payout.failed`, `payout.canceled` (services/finance-py/README.md)
   - copy the endpoint's signing secret (`whsec_...`) into `FIN_STRIPE_WEBHOOK_SECRET` on finance.
8. **Deploy, ledger first.** Deploy `ledger`; wait until it is Active and its log shows
   `ledger-rust: EVENT {"event":"ledger_created",...}` and `listening on [::]:8090`, and does **not** show the
   `LEDGER_CALLERS_FILE is not set` warning. Then deploy `detection` and `legal`; then `finance`; then `orchestrator`,
   `stripe-gateway` and `seo`. A department that crashed because the ledger was not up yet: Redeploy it.
9. **Volume backups.** The IaC declares a daily backup schedule for `ledger-data`, `finance-data`, `legal-data` and
   `seo-data` (`backupSchedules: ["DAILY"]` on each service's volume attachment, the SDK field; AEGIS L6). Check that
   `railway config plan` shows the schedule and that each volume → Backups shows Daily after the apply; if the plan
   does not carry it (the engine that applies it is in the Railway CLI, not the SDK, and has not been run against
   this file), enable daily backups there by hand. Restoring a backup later makes the ledger refuse to start until
   the one-shot `LEDGER_ALLOW_RESET` flow (services/ledger-rust/README.md).
10. **Restart policy check.** Each service's Settings should show restart policy On Failure, 10 retries, and
    healthcheck path `/health` (from the IaC file).
11. **Dashboard on Vercel.** Project root `apps/dashboard-ts`. Set the five variables in the dashboard table
    (`ORCHESTRATOR_URL`, `ORCHESTRATOR_SERVICE_TOKEN`, `DASHBOARD_PASSWORD_HASH`, `DASHBOARD_SESSION_SECRET`, and
    your decision on `DASHBOARD_TRUSTED_PROXY_HOPS`) for Production, then deploy.
12. **Smoke test** — the checklist below.
13. **Going live with Stripe** (later, after the test-mode smoke passes): live restricted key into
    `FIN_STRIPE_SECRET_KEY`, a new live-mode webhook endpoint (same URL, same events) and its secret into
    `FIN_STRIPE_WEBHOOK_SECRET`, `FIN_STRIPE_LIVE=1` (in `.railway/railway.ts`, then `railway config apply`), redeploy
    finance.

Operating the private services (your approval routes on finance, legal and seo) goes through `railway ssh` into that
service and a request to `127.0.0.1:<port>` from inside it; there is deliberately no public route to them.

## Smoke-test checklist

Test mode, after step 11. `$ORCH` = the orchestrator's domain, `$GW` = the stripe-gateway's domain.

- [ ] Every service is Active and passed its healthcheck (`/health`).
- [ ] ledger log: `ledger_created` on the first deploy, the callers listed (orchestrator-go, finance-py, legal-py,
      seo-py, each with its one department), no shared-token warning.
- [ ] finance, legal, seo logs: no `REFUSING TO START` / `refusing to start`.
- [ ] `curl -sS https://$ORCH/health` → 200 `{"status":"ok","service":"orchestrator-go"}`.
- [ ] `curl -sS -o /dev/null -w '%{http_code}\n' https://$ORCH/revenue-recovery/findings` → `401` (no token).
- [ ] `curl -sS -H "Authorization: Bearer <ORCHESTRATOR_SERVICE_TOKEN>" https://$ORCH/revenue-recovery/findings` → 200,
      `"ledger_verify": {"valid": true, ...}`.
- [ ] Do **not** run `POST /revenue-recovery/scan` against production as a test: it writes the fixture pool's findings
      into the production evidence ledger, which is append-only.
- [ ] `curl -sS https://$GW/health` → 200.
- [ ] `curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://$GW/webhooks/stripe -d '{}'` → `400` (no signature).
- [ ] Stripe CLI, test mode: `stripe trigger payment_intent.succeeded` → Stripe dashboard → the webhook endpoint shows
      the delivery answered `200`. (A `400` means Finance refused the signature: re-check `FIN_STRIPE_WEBHOOK_SECRET`.
      A `503` means Finance could not read Stripe back or was unreachable: check finance's log and the key's
      permissions.)
- [ ] finance `/health` from inside (`railway ssh` → `python3 -c "import urllib.request as u;
      print(u.urlopen('http://127.0.0.1:8410/health').read())"`): `"in_memory": false`,
      `"stripe_incoming": {"wired": true, "livemode": false}`.
- [ ] Dashboard: the Vercel URL shows the login page; the password logs in; the findings page renders (HTTP 200);
      `/healthz` answers 200 once logged in.
- [ ] Redeploy `ledger`: it comes back Active, its log says it loaded the existing entries with the chain verified, and
      the dashboard still loads.
- [ ] Volume backups: each of the four volumes → Backups shows the Daily schedule from the IaC.

## Open items and risks

- **Docker builds have not run yet.** The build box these files were written on has no Docker daemon and its egress
  proxy blocks every container registry. The first real build is the `railway-images` CI job. What was verified
  without Docker: each service started through the entrypoint with each Dockerfile's exact arguments, as root with
  root-owned data directories (what `RAILWAY_RUN_UID=0` gives), binding every interface with `PORT` set — health,
  uid 10001, data-dir ownership and mode, secret files 0600 and absent from the service's environment, a full scan through
  orchestrator → detection → ledger, stripe-gateway → finance with the `rail_gateway` caller, ledger restart with
  per-caller tokens, the second-writer refusal, every entrypoint refusal, and SIGTERM as PID 1 in a PID namespace.
- **Base images.** Runtime stages: `python:3.12-slim` pinned by the digest ADR 0011 records (2026-10-02); it gets no
  OS security updates until the digest is moved. Build stages `golang:1.27.3-bookworm` and `rust:1.99.1-slim-bookworm`
  are pinned by exact tag only (no digest could be resolved here): the CI job prints the digests it pulled — record
  them in the Dockerfiles as `@sha256:` pins. The Go and Rust runtime stages reuse the pinned Python base for
  `/bin/sh` and `setpriv`, so they carry a Python interpreter they do not use; move to a digest-pinned
  `debian:trixie-slim` once one can be resolved.
- **Go: 1.27.3** (AEGIS M1, done). Go 1.24.7 was past its end of support. `go.mod`, both CI Go jobs and both Go
  Dockerfiles now use Go 1.27.3, the newest release on go.dev/doc/devel/release (2026-10-19; 1.27.3 and 1.26.10 are
  the supported lines). The build stage is pinned by exact tag only (`golang:1.27.3-bookworm`, present in
  docker-library/official-images); no digest could be resolved from the build box — record the digest CI prints.
  `go.mod` says `go 1.27.3` with no separate `toolchain` line: `go mod tidy` (1.27.3) deletes a toolchain line equal
  to the go line, and the go line alone makes an older Go refuse to build (`go.mod requires go >= 1.27.3`, with the
  images' `GOTOOLCHAIN=local`) and is what setup-go reads. Keep the four places equal on every Go bump: `go.mod`, the delivery-py job's `go-version`, the two `FROM` lines
  (and the digest step in `railway-images`).
- **IPv6 bind not exercised here.** This box's kernel has no IPv6, so `::` / `[::]` could not be run; the same code
  paths were run with `0.0.0.0`. The CI job runs the production values. If a service on Railway fails with "Address
  family not supported", set its bind variable to `0.0.0.0` (IPv4 private networking works in new environments).
- **Railway behaviours taken from the docs or the SDK types, not observed:** `preserve()` on a variable that does not
  exist yet (first apply); `${{service.VARIABLE}}` interpolation inside the two JSON maps, including the hyphen in
  `stripe-gateway`; `requiredMountPath` (an SDK field, not described in the docs); `RAILWAY_RUN_UID=0`;
  `checkSuites: true` (wait for CI); `railway ssh` sessions having the service's variables; the CLI applying
  `backupSchedules` from a volume attachment (the SDK types it and emits it in the graph; checked by evaluating the
  file, not by a plan).
- **stripe-gateway private-network opt-in** (`STRIPE_GATEWAY_ALLOW_RAILWAY_PRIVATE_HTTP`) relaxes AEGIS launch-hardening
  L8 for one named host on the private network: `http://<STRIPE_GATEWAY_FINANCE_PRIVATE_NAME>.railway.internal[:port]`
  (IaC: `finance`; AEGIS L1). It needs AEGIS review before merge.
- **Two public services.** The orchestrator's token has no minimum-length check and no rate limit, so its entropy is
  the only defence (use `openssl rand -hex 32`). Anyone holding it can also start a scan.
- **No scheduler is deployed.** Finance's daily reconciliation and Stripe-session job, Legal's daily jobs and seo's
  schedule tick need a `scheduler` caller (and a cron service) that this deploy does not include.
- **Detection scans only the fixture pool** (ADR 0001): there is no real-store data source yet.
- **Dashboard login limiter** depends on `DASHBOARD_TRUSTED_PROXY_HOPS` and an unverified Vercel behaviour (above).
- **Version skew.** CI installs each service's `requirements.txt`, whose transitive dependencies float; the images
  install the hash lock. They can differ (the lock already takes certifi 2026.10.19 for PYSEC-2026-4320). `audit-python`
  audits the lock too.
- **Secrets are Railway variables.** The vault (Cybersecurity 22) is not built. `*_FILE` secrets are written inside the
  container, owner-only, but the values live in Railway (dashboard, `railway ssh`) and in the root-owned entrypoint's
  `/proc/1/environ` (see "How `*_FILE` secrets map onto Railway").
- **Deploy checks (AEGIS L5), the part not yet done.** Done: the smoke's "fail closed" requires exit 1 and each
  service's own refusal text; the IaC check refuses a literal secret inside a `${{...}}` reference map. Still open
  (as recorded by the fix engineer; reconcile with the AEGIS report's L5 list):
  - the smoke has never run (no Docker on the build box); its first run is the `railway-images` CI job;
  - stripe-gateway is smoked against a loopback Finance only, not through the private-network opt-in
    (`STRIPE_GATEWAY_ALLOW_RAILWAY_PRIVATE_HTTP=1`, `STRIPE_GATEWAY_FINANCE_PRIVATE_NAME`) to a real finance container;
  - the root-owned-volume path (`RAILWAY_RUN_UID=0`) is smoked for ledger-rust only, not finance, legal or seo, and the
    secret-file path for seo only, not `LEDGER_CALLERS` or the two `FIN_STRIPE_*` secrets;
  - no cross-service flow runs in CI (orchestrator → detection → ledger, stripe-gateway → finance); it was run once by
    hand on the build box without Docker;
  - `devtools/test_deploy_config.py` reads `.railway/railway.ts` as text; CI does not evaluate the file with the
    pinned SDK (it was evaluated by hand with `node --experimental-strip-types` and type-checked with `tsc --strict`).
- **Startup order** is enforced only by retries: a department that exhausted its retries before the ledger was up stays
  down until redeployed.
