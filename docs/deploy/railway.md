# Deploying the first-revenue services on Railway

Go-live plan, Days 1-2. This is the runbook for putting the services needed for first revenue on Railway, and the
dashboard on Vercel. Nothing in this repository holds a secret value. Nothing here has been applied to Railway or
Vercel: the steps in [Andre's steps](#andres-steps) do that.

What is in the repository:

| Piece | Where |
|---|---|
| One production Dockerfile per Railway service | `services/ledger-rust/Dockerfile`, `services/detection-py/Dockerfile`, `services/finance-py/Dockerfile`, `services/legal-py/Dockerfile`, `services/seo-py/Dockerfile`, `services/compliance-py/Dockerfile`, `services/orchestrator-go/Dockerfile.orchestrator`, `services/orchestrator-go/Dockerfile.stripe-gateway` |
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
| `compliance` | `services/compliance-py` (Compliance 38: the hard gate before a client is served, ADR 0024) | `services/compliance-py/Dockerfile` | 8380 | private network only (no domain) | `compliance-data` at `/data` | 1 |
| dashboard | `apps/dashboard-ts` (Next.js) | — (Vercel builds it) | — | public, password session | — | Vercel |

**The Stripe gateway is in orchestrator-go — verified.** `services/orchestrator-go/cmd/stripe-gateway/main.go` is a
second binary in the orchestrator-go module (its own `main`, its own README section). It is the only piece Stripe
calls (`POST /webhooks/stripe`); it holds no Stripe secret and forwards the raw body and `Stripe-Signature` to Finance
(`POST /fin/v1/stripe/events`), which verifies the signature and reads the object back from Stripe. It is deployed as
its own Railway service from its own Dockerfile.

**The revenue recovery service is detection-py**, run behind orchestrator-go with ledger-rust as its evidence store
(root README; ADR 0001 decisions 2-3 and "Scope boundary"). A scan's data source is one connected Shopify store's
snapshot, read by detection's read-only store connector (ADR 0020); the fixture pool (`fixtures/*.json`, copied into
the detection image) is refused on Railway (`FIXTURE_SCAN_DISABLED`). Connecting a store is Andre's step:
[Revenue Recovery: connecting a real store](#revenue-recovery-connecting-a-real-store).

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
| finance, seo, detection, legal | compliance | `http://compliance.railway.internal:8380` (ADR 0024) |
| compliance | legal, ledger | `http://legal.railway.internal:8420`, `http://ledger.railway.internal:8090` |
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
uid 10001. So the entrypoint writes a secret file only when it runs as root (`RAILWAY_RUN_UID=0`, which every service
with these secrets sets: ledger, finance and seo also for their volume, detection for its secret files only); started as any other user with a
value variable set, it refuses with `--secret-file <NAME>_FILE: <NAME> is set but this container runs as uid ...,
not root`. The CI smoke starts seo-py as root for this reason and checks that the file is 0600/10001, that the value
is not in the service's environment, that uid 10001 cannot read `/proc/1/environ`, and that the non-root start
refuses.

| Railway variable (the value) | Written to | Read by |
|---|---|---|
| `FIN_STRIPE_SECRET_KEY` | `FIN_STRIPE_SECRET_KEY_FILE` | finance |
| `FIN_STRIPE_WEBHOOK_SECRET` | `FIN_STRIPE_WEBHOOK_SECRET_FILE` | finance |
| `SEO_LOG_HASH_KEY` | `SEO_LOG_HASH_KEY_FILE` | seo |
| `SEO_GSC_SERVICE_ACCOUNT` (optional) | `SEO_GSC_SERVICE_ACCOUNT_FILE` | seo |
| `LEDGER_CALLERS` | `LEDGER_CALLERS_FILE` | ledger |
| `DETECTION_LEDGER_TOKEN` | `DETECTION_LEDGER_TOKEN_FILE` | detection |
| `RR_STORE_CONNECTIONS` | `RR_STORE_CONNECTIONS_FILE` (`/run/zbm-secrets/rr-store-connections-file`) | detection |
| `RR_STORE_SECRET_1` | `RR_STORE_SECRET_1_FILE` (`/run/zbm-secrets/rr-store-secret-1-file`) | detection, through the connections JSON |
| `RR_STORE_SECRET_2` | `RR_STORE_SECRET_2_FILE` (`/run/zbm-secrets/rr-store-secret-2-file`) | detection, through the connections JSON |

The written path is fixed: `/run/zbm-secrets/` plus the `*_FILE` name in lower case with `_` as `-`. detection-py does
not read `RR_STORE_SECRET_1_FILE` / `RR_STORE_SECRET_2_FILE` itself: the connections JSON names those two paths as a
connection's `token_file` (or `client_id_file` + `client_secret_file`), so the JSON holds references and the
credential stays a sealed variable. `RR_STORE_CONNECTIONS` holds no secret; it goes through the same mechanism because
detection-py reads the connections only from a file.

`SEO_OPENAI_API_KEY_FILE`, `SEO_ANTHROPIC_API_KEY_FILE`, `SEO_GOOGLE_API_KEY_FILE`,
`SEO_SEARCH_CONSOLE_CREDENTIALS_FILE` (renamed to `SEO_GSC_SERVICE_ACCOUNT_FILE`) and `SEO_BING_WEBMASTER_KEY_FILE`
belong to a port that is not built (or a retired name): setting one refuses the start. Leave them unset.

A value variable that is unset or empty writes no file and leaves its `*_FILE` setting unset, so the service starts
exactly as if the feature were never configured (`SEO_GSC_SERVICE_ACCOUNT` relies on this: no key, Search Console
`NOT_CONNECTED`). The value is written with `printf '%s'`, so a multi-line value (the Search Console JSON key, its
PEM with `\n` escapes) reaches the file unchanged, except that trailing newlines are dropped (shell command
substitution); seo strips surrounding whitespace from the key file anyway.

One built setting is a `*_FILE` setting this deploy does **not** map (no `--secret-file` in its image), so on
Railway it stays unset and its feature stays off, fail closed:

- `SEO_PERPLEXITY_API_KEY_FILE`: not needed on Railway. seo reads the same key from the plain variable
  `SEO_PERPLEXITY_API_KEY` (ADR 0017 W4; setting both refuses the start).

## Secrets

Generate every token on your own machine and paste it straight into Railway (or Vercel). Never commit one, never put
one in a chat or a ticket. Mark each one **Sealed** in Railway (its value is then hidden after it is set), except
`LEDGER_CALLERS`: it holds hashes only and is set **not** Sealed, so it can be read back (see below). Each must
be a different value: several services refuse to start when two of their tokens are equal.

| Secret | Generate with | Set on (owner) | Also used by (references, not copies) |
|---|---|---|---|
| `ZBM_SERVICE_TOKEN` | `openssl rand -hex 32` | detection | orchestrator `DETECTION_SERVICE_TOKEN` (reference) |
| `ORCHESTRATOR_SERVICE_TOKEN` | `openssl rand -hex 32` | orchestrator | Vercel `ORCHESTRATOR_SERVICE_TOKEN` (paste the same value; Vercel cannot reference Railway) |
| `LEDGER_SERVICE_TOKEN` (orchestrator's) | `openssl rand -hex 32` | orchestrator | its SHA-256 goes in `LEDGER_CALLERS` |
| `LEDGER_SERVICE_TOKEN` (finance's) | `openssl rand -hex 32` | finance | its SHA-256 goes in `LEDGER_CALLERS` |
| `LEDGER_SERVICE_TOKEN` (legal's) | `openssl rand -hex 32` | legal | its SHA-256 goes in `LEDGER_CALLERS` |
| `LEDGER_SERVICE_TOKEN` (seo's) | `openssl rand -hex 32` | seo | its SHA-256 goes in `LEDGER_CALLERS` |
| `DETECTION_LEDGER_TOKEN` | `openssl rand -hex 32` | detection | its SHA-256 goes in `LEDGER_CALLERS` (caller `detection-py`, department `rr_store`); becomes `DETECTION_LEDGER_TOKEN_FILE` |
| `LEDGER_SERVICE_TOKEN` (compliance's) | `openssl rand -hex 32` | compliance | its SHA-256 goes in `LEDGER_CALLERS` (caller `compliance-py`, department `compliance`; ADR 0024) |
| `LEDGER_CALLERS` | `devtools/ledger_callers.py` (below; hashes only, no token; **not Sealed**) | ledger | you: keep the file it writes (`~/zbm/ledger-callers.json`), your record of the value |
| `FIN_SERVICE_TOKEN` | `openssl rand -hex 32` | finance | stripe-gateway `STRIPE_GATEWAY_FINANCE_TOKEN`, seo `SEO_FINANCE_TOKEN` (references) |
| `FIN_ANDRE_APPROVAL_TOKEN` | `openssl rand -hex 32` | finance | you (keep it in your password manager) |
| `STRIPE_GATEWAY_CALLER_TOKEN` | `openssl rand -hex 32` | stripe-gateway | finance `FIN_CALLER_TOKENS` (`rail_gateway`, interpolated) |
| `SEO_FINANCE_CALLER_TOKEN` | `openssl rand -hex 32` | seo | finance `FIN_CALLER_TOKENS` (`seo_02`, interpolated) |
| `FIN_LEGAL_CALLER_TOKEN` | `openssl rand -hex 32` | finance | legal `LEGAL_CALLER_TOKENS` (`finance_31`, interpolated) |
| `FIN_CRON_CALLER_TOKEN` | `openssl rand -hex 32` | fin-cron | finance `FIN_CALLER_TOKENS` (`ops_cron`, interpolated; ADR 0026) |
| `FIN_CRON_CALLER_TOKEN` (on ops-alerts) | `openssl rand -hex 32` (a new value) | ops-alerts | finance `FIN_CALLER_TOKENS` (`ops_alerts`, interpolated; ADR 0027) |
| `RESEND_API_KEY` | Resend → API Keys → Create (permission **Sending access**, domain `zbestmedia.com`) | finance | — (becomes `RESEND_API_KEY_FILE`; client email stays OFF until it is set, ADR 0026) |
| `FIN_ONBOARDING_CALLER_TOKEN` | `openssl rand -hex 32` | finance | finance `FIN_CALLER_TOKENS` (`onboarding`, interpolated); you, as header `X-FIN-Caller-Token` (keep it in your password manager) |
| `LEGAL_SERVICE_TOKEN` | `openssl rand -hex 32` | legal | finance `FIN_LEGAL_TOKEN` (reference) |
| `LEGAL_ANDRE_APPROVAL_TOKEN` | `openssl rand -hex 32` | legal | you |
| `LEGAL_ONBOARDING_CALLER_TOKEN` | `openssl rand -hex 32` (a different value from `FIN_ONBOARDING_CALLER_TOKEN`) | legal | legal `LEGAL_CALLER_TOKENS` (`onboarding`, interpolated); you, as header `X-LEGAL-Caller-Token` |
| `SEO_SERVICE_TOKEN` | `openssl rand -hex 32` | seo | — |
| `SEO_ANDRE_APPROVAL_TOKEN` | `openssl rand -hex 32` | seo | you |
| `SEO_CALLER_TOKENS` | `{"dashboard": "<openssl rand -hex 32>"}` | seo | you (the `dashboard` caller is how your approvals reach seo) |
| `SEO_LOG_HASH_KEY` | `openssl rand -hex 32` | seo | — (becomes `SEO_LOG_HASH_KEY_FILE`) |
| `COMPLIANCE_SERVICE_TOKEN` | `openssl rand -hex 32` | compliance | finance `FIN_COMPLIANCE_TOKEN`, seo `SEO_COMPLIANCE_TOKEN`, detection `DETECTION_COMPLIANCE_TOKEN` (references), legal `LEGAL_COMPLIANCE_TOKEN` (interpolated); ADR 0024 |
| `COMPLIANCE_ANDRE_APPROVAL_TOKEN` | `openssl rand -hex 32` | compliance | you (approving the register, recording OFAC searches, releasing holds) |
| `COMPLIANCE_ONBOARDING_CALLER_TOKEN` | `openssl rand -hex 32` (different from the finance and legal onboarding tokens) | compliance | compliance `COMPLIANCE_CALLER_TOKENS` (`onboarding`, interpolated); you, as header `X-Compliance-Caller-Token` to run a client's activation |
| `COMPLIANCE_LEGAL_CALLER_TOKEN` | `openssl rand -hex 32` | compliance | legal `LEGAL_CALLER_TOKENS` (`compliance_38`, interpolated) |
| `FIN_COMPLIANCE_CALLER_TOKEN` | `openssl rand -hex 32` | finance | compliance `COMPLIANCE_CALLER_TOKENS` (`finance_31`, interpolated) |
| `SEO_COMPLIANCE_CALLER_TOKEN` | `openssl rand -hex 32` | seo | compliance `COMPLIANCE_CALLER_TOKENS` (`seo_02`, interpolated) |
| `DETECTION_COMPLIANCE_CALLER_TOKEN` | `openssl rand -hex 32` | detection | compliance `COMPLIANCE_CALLER_TOKENS` (`revenue_recovery`, interpolated); becomes `DETECTION_COMPLIANCE_CALLER_TOKEN_FILE` |
| `LEGAL_COMPLIANCE_CALLER_TOKEN` | `openssl rand -hex 32` | legal | compliance `COMPLIANCE_CALLER_TOKENS` (`legal_37`, interpolated) |
| `RR_STORE_CONNECTIONS` (only to connect a store) | the JSON in [Revenue Recovery: connecting a real store](#revenue-recovery-connecting-a-real-store) (references, no secret; setting it is your approval) | detection | — (becomes `RR_STORE_CONNECTIONS_FILE`) |
| `RR_STORE_SECRET_1` (only to connect a store) | the store's Dev Dashboard app **Client ID** (or a legacy custom app's Admin API access token) | detection | — (becomes `/run/zbm-secrets/rr-store-secret-1-file`) |
| `RR_STORE_SECRET_2` (only to connect a store) | the store's Dev Dashboard app **Client secret** (leave unset for a legacy token) | detection | — (becomes `/run/zbm-secrets/rr-store-secret-2-file`) |
| `SEO_PERPLEXITY_API_KEY` (optional) | Perplexity → API settings (only when you turn the engine on; set the four budgets too) | seo | — |
| `SEO_GSC_SERVICE_ACCOUNT` (optional) | Google Cloud → IAM & Admin → Service accounts → the account → Keys → Add key → JSON (only when you connect Search Console; steps below) | seo | — (becomes `SEO_GSC_SERVICE_ACCOUNT_FILE`) |
| `FIN_STRIPE_SECRET_KEY` | Stripe dashboard → Developers → API keys → create a **restricted** key (test mode first: `rk_test_...`) with write on Checkout Sessions and read on PaymentIntents, Charges, Disputes, Payouts and Balance | finance | — (becomes `FIN_STRIPE_SECRET_KEY_FILE`) |
| `FIN_STRIPE_WEBHOOK_SECRET` | Stripe, when you register the webhook endpoint (step 7): the endpoint's signing secret, `whsec_...` | finance | — (becomes `FIN_STRIPE_WEBHOOK_SECRET_FILE`) |
| `DASHBOARD_SESSION_SECRET` | `openssl rand -hex 32` | Vercel | — |
| `DASHBOARD_PASSWORD_HASH` | `cd apps/dashboard-ts && npm run hash-password` (prompts for the password; prints `scrypt$...`) | Vercel | — |

`openssl rand -hex 32` gives 64 hex characters (256 bits): every service that checks a length wants at least 32, and
the ledger and the orchestrator cannot rate-limit guesses, so the token's entropy is the defence.

**`LEDGER_CALLERS`** (per-caller ledger tokens with department scopes; services/ledger-rust/README.md). It holds the
SHA-256 of each caller's token, never a token. `devtools/ledger_callers.py` builds it. Run it on your machine from a
checkout (Python 3.9 or newer, standard library only; its tests pass on 3.9 to 3.13). Run it in a real terminal
window (Terminal, iTerm), not piped or in an editor's output pane: Python's `getpass` shows what you type when it is
not attached to a terminal (it warns `GetPassWarning: Can not control echo`; if you see that, stop and close the
window). It asks for each token without echoing it, twice, and refuses:

- an empty token, whitespace in a token, or a token under the ledger's 32 bytes;
- the same token for two callers (the ledger would refuse the start; a hand-made map would silently drop one);
- a caller or department it does not know, so a typo cannot cut a department off;
- anything else the ledger refuses at start.

Its table is the only place that sets what each caller may do:

- orchestrator-go writes `revenue_recovery`;
- finance-py writes `finance`;
- legal-py writes `legal`;
- seo-py writes `seo`;
- detection-py writes `rr_store`, its store-read evidence (ADR 0020 section 7). It neither writes nor reads
  `revenue_recovery` (the orchestrator's findings);
- compliance-py writes `compliance`, its rulings, register versions and log anchors (ADR 0024). Its control C-11
  calls `GET /ledger/verify` (open to every caller) and reads `GET /ledger/entries` scoped to `compliance`.

Each caller reads only its own department: no `read_departments`, no `read_all`. Every command prints one line per
caller: its name, what it writes and what it reads. It never prints a token, and never a hash outside the value it
writes.

First time (step 5), after the six ledger tokens are set on their services:

```bash
mkdir -p ~/zbm && chmod 700 ~/zbm
python3 devtools/ledger_callers.py new --out ~/zbm/ledger-callers.json
```

Check it with `python3 devtools/ledger_callers.py check --current ~/zbm/ledger-callers.json`, then paste the file's
content into `LEDGER_CALLERS` on the ledger (macOS: `pbcopy < ~/zbm/ledger-callers.json`) and **do not mark it
Sealed**. **Keep the file.** It is your record of the value and your rollback. It holds hashes only: with it, adding
or rotating one caller needs only that caller's new token, never the others (which stay Sealed on their services).

Why `LEDGER_CALLERS` is not Sealed (AEGIS ledger-callers N-M1):

- It holds no secret. Each key is the SHA-256 of a 256-bit random token, which cannot be reversed or guessed, and the
  ledger hashes whatever it is given, so a key presented as a token is refused (401). The rest is caller names and
  departments, which this page already lists. People who can see the ledger's variables can also change them.
- Sealing it only made the deployed value unreadable ("Sealed variables cannot be un-sealed"; not shown by the
  dashboard, the API, `railway variables` or `railway run`). A record that no longer equals the deployed value could
  then not be caught: `add` or `rotate` would drop a live caller's hash and cut that department off, and pasting the
  record back would not restore it. Read back, the deployed value is the source of truth: every change starts by
  copying it and checking the record against it.
- Nothing else changes: the image still writes it to `/run/zbm-secrets/ledger-callers-file` (0600) and removes it
  from the ledger's environment, as it does for `RR_STORE_CONNECTIONS`, which holds no secret either.

**Already Sealed?** (Earlier versions of this page said to seal it.) Railway cannot unseal a variable. Recreate it,
not Sealed, with the same value, and only with a value you know is the deployed one:

1. `railway ssh` into the ledger and run `cat /run/zbm-secrets/ledger-callers-file`. UNVERIFIED: whether the
   `railway ssh` user can read that file (mode 0600, owner uid 10001). Save what it prints as
   `~/zbm/ledger-callers.deployed.json` (`chmod 600`). It is hashes only, so it shows no token on your screen.
2. Run `python3 devtools/ledger_callers.py compare --current ~/zbm/ledger-callers.json --deployed
   ~/zbm/ledger-callers.deployed.json`. If it is refused, or you have no record, the deployed file is the value: run
   `check` on it and copy it over the record (`cp ~/zbm/ledger-callers.deployed.json ~/zbm/ledger-callers.json`).
3. Run the baseline per-department check ("Changing `LEDGER_CALLERS`", step 2): every caller 200.
4. On the ledger's Variables tab, delete `LEDGER_CALLERS` and add it again, not Sealed, with exactly the content of
   `~/zbm/ledger-callers.json`, both before you deploy. Then redeploy the ledger and run the log check ("Changing
   `LEDGER_CALLERS`", step 5) and the per-department check for every caller: all `200`. Anything else: stop and bring
   the output back (the value is the one that was deployed, so the cause is elsewhere). If Railway deploys between
   the delete and the add, the ledger refuses to start (no callers file and no shared token): it fails closed. Add
   the variable and redeploy.
5. **Cannot read the file?** Do not recreate it from a record you cannot prove is current. A stale record would cut a
   department off with no way back to the Sealed value. Rotate every caller instead: new tokens for all five, `new`,
   the value set not Sealed, each new token set on its service, then the ledger and every service redeployed. Ledger
   writes fail until each service runs with its new token.

With `LEDGER_CALLERS` set and no `LEDGER_SERVICE_TOKEN` on the ledger, there is no shared token: each service can
write and read only its own department.

#### Changing `LEDGER_CALLERS` (adding a caller, rotating one token)

Every change replaces the whole value and needs a ledger redeploy. A wrong value either stops the ledger (every
department's ledger writes then fail closed) or quietly cuts one department off. The value the ledger runs on is the
source of truth, not your file. So:

1. **Copy the deployed value and compare your record with it.** Copy it now, in this session; never reuse an old copy.
   - Dashboard: ledger → Variables → `LEDGER_CALLERS` → copy the value. Then, on macOS:
     `(umask 077; pbpaste > ~/zbm/ledger-callers.deployed.json)`.
   - Or the CLI, with the project linked to production: `(umask 077; railway variables --service ledger --json |
     python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["LEDGER_CALLERS"])' >
     ~/zbm/ledger-callers.deployed.json)`. UNVERIFIED: that `--json` prints one flat object of name → value. Nothing
     reaches your screen either way.
   - Run `python3 devtools/ledger_callers.py compare --current ~/zbm/ledger-callers.json --deployed
     ~/zbm/ledger-callers.deployed.json`. It checks that both hold the same callers, the same hashes and the same
     grants (order and spacing do not matter to the ledger). It never prints a hash: a difference is named by caller.
   - **Refused** (or no record): the deployed value wins, because it is what the ledger runs on. Run `check` on the
     deployed file and do step 2 against it. If every caller answers 200, make it the record:
     `mv ~/zbm/ledger-callers.json ~/zbm/ledger-callers.stale.json; cp ~/zbm/ledger-callers.deployed.json
     ~/zbm/ledger-callers.json`. Then run `compare` again. Find out how the two drifted (a hand edit in Railway, a
     rotation done another way, an old copy of the file) before you go on.
   - **The value is empty or hidden** (still Sealed): see "Already Sealed?" above first.
   - `add` and `rotate` (step 3) run the same comparison themselves and refuse, before asking for any token, unless
     the record equals the deployed copy.
2. **Baseline: every caller in the deployed value reaches the ledger now.** Run the per-department check (below) on
   each service whose caller the `compare` summary lists (to add detection: the other four). Every one must print
   `200`. Anything else: **stop**. Something is already wrong before you changed anything, and a change now would
   hide it. Bring the output back.
3. **Build the new value from the record.** It is written to a new file; the record stays as it is for rollback.
   - Add detection: `python3 devtools/ledger_callers.py add detection-py --current ~/zbm/ledger-callers.json
     --deployed ~/zbm/ledger-callers.deployed.json --out ~/zbm/ledger-callers.next.json`. It asks only for
     `DETECTION_LEDGER_TOKEN`; the other four entries are copied, not re-typed.
   - Rotate one (generate its new token first): `python3 devtools/ledger_callers.py rotate seo-py --current
     ~/zbm/ledger-callers.json --deployed ~/zbm/ledger-callers.deployed.json --out ~/zbm/ledger-callers.next.json`.
   - Read the summary: every caller listed, each with its one department.
4. **Set the new token on its service** (detection's `DETECTION_LEDGER_TOKEN`, or the rotated service's
   `LEDGER_SERVICE_TOKEN`, Sealed). Do not deploy that service yet. For a rotation, that department's ledger writes
   fail closed from the ledger redeploy (step 5) until its own redeploy (step 8), so do it when nothing of that
   department is running.
5. **Replace `LEDGER_CALLERS`** on the ledger (not Sealed) with the content of `~/zbm/ledger-callers.next.json` and
   redeploy the ledger. Wait for it to be Active. Its log must show:
   - `listening on [::]:8090`;
   - no `REFUSING TO START`;
   - `per-caller tokens from /run/zbm-secrets/ledger-callers-file:` listing the same callers and departments as the
     summary from step 3.
6. **Check the unchanged departments.** Run the per-department check on every service whose token you did *not*
   change. Each must print `200`. Any `401` or `403`: **roll back** (below). The service you are changing is expected
   to fail this check now, and that is not a reason to roll back. A rotated service prints `401`, because it still
   runs with its old token. Detection, being added, prints `rr_store no-token` (or `401`), or is not running yet.
7. **Make the new value the record.** The ledger now runs it, so it is the deployed value:
   `mv ~/zbm/ledger-callers.json ~/zbm/ledger-callers.prev.json && mv ~/zbm/ledger-callers.next.json
   ~/zbm/ledger-callers.json`, then `rm ~/zbm/ledger-callers.deployed.json` (out of date now; step 1 always copies a
   fresh one).
8. **Deploy the changed service and check it.** It must print `200`.
   - `401`: the token on that service is not the one you gave the script. Fix **that service's token**, **not** with a
     ledger rollback: the ledger value is right, and step 6 showed every other department still works. Set on the
     service exactly the token you gave the script, and redeploy it. If you no longer have that token, start again at
     step 1 with `rotate` for that caller (a fresh token).
   - `403`: the token is there with another department. That cannot come from the script's table: roll back (paste
     `~/zbm/ledger-callers.prev.json`) and bring the output back.

What each check must print:

| when | unchanged callers | rotated caller | added caller (detection) |
|---|---|---|---|
| step 2, before any change | `200`, else stop | `200`, else stop | not checked (not in the value yet) |
| step 6, after the ledger redeploy | `200`, else roll back | `401` expected (old token): not a rollback | `rr_store no-token` or `401` expected: not a rollback |
| step 8, after its own redeploy | — | `200`; `401` → fix its token, not the ledger | `200`; `401` → fix its token, not the ledger |

**Per-department check** (read-only; it writes nothing to the append-only ledger). `railway ssh` into each service in
turn and paste this with that service's department as `D`:

| service | `D` |
|---|---|
| orchestrator | `revenue_recovery` |
| finance | `finance` |
| legal | `legal` |
| seo | `seo` |
| detection | `rr_store` |
| compliance | `compliance` |

```sh
D=finance python3 - <<'PY'
import os, urllib.error, urllib.request
d = os.environ["D"]
if d == "rr_store":
    url = os.environ["DETECTION_LEDGER_URL"]
    try:
        tok = os.environ.get("DETECTION_LEDGER_TOKEN") or open("/run/zbm-secrets/detection-ledger-token-file").read().strip()
    except FileNotFoundError:
        raise SystemExit(d + " no-token")
else:
    url, tok = os.environ["LEDGER_SERVICE_URL"], os.environ["LEDGER_SERVICE_TOKEN"]
req = urllib.request.Request(f"{url}/ledger/entries?department={d}&limit=1", headers={"Authorization": "Bearer " + tok})
try:
    print(d, urllib.request.urlopen(req, timeout=15).status)
except urllib.error.HTTPError as e:
    print(d, e.code)
PY
```

It prints the department and an HTTP status, never the token:

- `200`: the service's own token is in `LEDGER_CALLERS` with that department. In the script's table each department
  is readable only by its one write caller, so this is that caller's write grant.
- `401`: its token is not in the value. For a service you did not change: a typo, or the caller was dropped; roll
  back. For the service you are changing: expected until it is redeployed (step 6); after that, fix its token (step 8).
- `403`: the token is there, but with another department. Roll back.
- `rr_store no-token`: detection runs without a ledger token file. Expected while detection is being added (step 6);
  at any other time, set `DETECTION_LEDGER_TOKEN` on detection and redeploy it.
- `KeyError`: the ssh session does not have the service's variables (UNVERIFIED Railway behaviour). Fall back:
  - orchestrator: the smoke check `GET /revenue-recovery/findings` → 200 with `"ledger_verify": {"valid": true}`;
  - finance, legal, seo: no `ledger refused` / `HTTP 401` line in their log at their next recorded operation;
  - detection: a store sync that does not answer `LEDGER_UNAVAILABLE`.

**Rollback** (step 6 only, before step 7). Paste `~/zbm/ledger-callers.json` (the record, unchanged, which step 1
proved equal to the value the ledger ran on) back into `LEDGER_CALLERS` and redeploy the ledger. Then run the
per-department check again: every caller from step 2 must print `200`. A caller you were adding then fails closed on
the ledger until you try again (detection: `LEDGER_UNAVAILABLE`). Railway's Rollback on an earlier ledger deployment
may also work. UNVERIFIED: whether it restores that deployment's variables. Re-pasting the record is the rollback
this page relies on.

**If the new ledger deployment fails its healthcheck.** The ledger has a volume. Railway's healthcheck docs say three
things:

- a new deployment is made active only after its healthcheck passes;
- "we prevent multiple deployments from being active and mounted to the same service";
- "there will be a small amount of downtime when re-deploying a service that has a volume attached".

UNVERIFIED: whether the old ledger deployment is still running when the new one fails, or was already stopped. Assume
the ledger is down until you see it Active. While it is down:

- the other services keep running (nothing restarts them), but every ledger write fails closed;
- orchestrator scans, and the finance, legal and seo operations that are recorded first, are refused;
- detection syncs answer `LEDGER_UNAVAILABLE`;
- nothing is written half-way and nothing on the ledger is lost.

Roll back at once. Do not redeploy finance, legal or seo while the ledger is down: they refuse to start without a
reachable ledger (their local log is checked against it).

**Interpolated maps.** `FIN_CALLER_TOKENS` and `LEGAL_CALLER_TOKENS` are JSON maps built by `.railway/railway.ts`
from other services' variables with Railway's reference syntax, e.g.
`{"rail_gateway": "${{stripe-gateway.STRIPE_GATEWAY_CALLER_TOKEN}}", "seo_02": "${{seo.SEO_FINANCE_CALLER_TOKEN}}", "onboarding": "${{FIN_ONBOARDING_CALLER_TOKEN}}"}`
(`${{VARIABLE}}` with no service name is the same service's variable). You never type those two; you set the tokens
they point at. Finance and Legal refuse to start when a token value still holds `${{` or `{{` (a reference Railway
did not resolve, left as literal text): the error names the setting, never the value (AEGIS night review M-2).

**The `onboarding` caller is you, for now** (AEGIS A1 C1). Drafting an invoice (`POST /fin/v1/invoices`) needs the
`onboarding` (or `scheduler`) caller, and recording a client's acceptance (`POST /legal/v1/acceptances`) needs
`onboarding` (or `hub` / `clipper_network`); none of those services is deployed. Until an onboarding service exists
you hold `FIN_ONBOARDING_CALLER_TOKEN` and `LEGAL_ONBOARDING_CALLER_TOKEN` yourself: generate each on your own
machine with `openssl rand -hex 32` (two different values; each service refuses to start when a caller token is under
32 printable characters or equals another of its tokens), paste it into the variable on its service, mark it
**Sealed**, and keep it in your password manager. Two tokens, not one, so either can be revoked alone and a leaked
Finance token does not record contract acceptances.

> **Warning: set BOTH tokens BEFORE `railway config apply`** (AEGIS night review M-1). `FIN_CALLER_TOKENS` and
> `LEGAL_CALLER_TOKENS` reference `FIN_ONBOARDING_CALLER_TOKEN` and `LEGAL_ONBOARDING_CALLER_TOKEN`. If either is
> missing when the IaC is applied, that map's `onboarding` entry is empty or unresolved and **finance and legal refuse
> to start** on their next deploy. On a project that is already running, that is an outage of both services, not a
> first-deploy hiccup. So: set `FIN_ONBOARDING_CALLER_TOKEN` on finance and `LEGAL_ONBOARDING_CALLER_TOKEN` on legal
> (both Sealed) first; then run `railway config plan` and read it; only then `railway config apply`.

What it means:
- **You become maker and checker.** You draft the invoice with the onboarding token and approve it with
  `FIN_ANDRE_APPROVAL_TOKEN`: the code still needs two different tokens and pins the approval to the draft's
  `content_sha256`, but one person holds both, so it is a self-review step, not an independent check (FIN-19's
  separation of duties is nominal until an onboarding service holds the token). Finance's audit trail records the
  draft as `onboarding`, not as you.
- **An acceptance is a claim that the client clicked.** Legal's acceptance route takes only
  `method: clickwrap_unticked_box` with the exact text shown and an affirmative act. Record one only for an
  acceptance the client actually made, with its evidence; never to unblock an invoice.
- **More than the two routes.** `onboarding` also reaches, at Finance, `POST /fin/v1/payees`, `GET
  .../billing-readiness`, `GET /fin/v1/media-buys/{id}`, the Stripe checkout create/view (which your approval token
  already reaches) and every read open to any caller (treasury, journal, audit export, ...); at Legal,
  `PUT /legal/v1/contracts/{client_id}/terms`, `POST .../obligations/{id}/done`, `POST .../playbooks/{type}/reviews`,
  `POST /legal/v1/requests` and every read open to any caller. Do not use it for those. It is still the narrowest
  caller that can draft or record: `scheduler` reaches payout runs, releases, refunds and sweeps.

## Environment variables

Every variable each deployed service reads, taken from its code (`devtools/test_deploy_config.py` fails when the code
reads one this page does not name; for the Python services it collects every name under the service's prefixes and
every upper-case name read from the environment whatever its prefix, and checks its own scan, AEGIS M-2). "IaC" = set by `.railway/railway.ts`; "you" = you set the value once in Railway;
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
| `LEDGER_CALLERS` | yes | no (hashes only; set **not** Sealed) | (you; `devtools/ledger_callers.py`, see Secrets) | becomes `LEDGER_CALLERS_FILE`; changing it: "Changing `LEDGER_CALLERS`" |
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
| `RAILWAY_RUN_UID` | yes | no | `0` (IaC) | the entrypoint starts as root only to write the secret files below, then runs detection as uid 10001 (no volume) |
| `DETECTION_LEDGER_URL` | for store reads | no | `http://ledger.railway.internal:8090` (IaC) | the Revenue Recovery store connector's evidence ledger (ADR 0020 section 7) |
| `DETECTION_LEDGER_TOKEN` | for store reads | yes | (you; `openssl rand -hex 32`) | detection's own ledger caller token (`rr_store` in `LEDGER_CALLERS`); becomes `DETECTION_LEDGER_TOKEN_FILE`. Unset: every store read fails closed (`LEDGER_UNAVAILABLE`, record-first). Read once when detection starts |
| `DETECTION_LEDGER_TOKEN_FILE` | — | — | (set by the entrypoint) | do not set yourself on Railway (setting both refuses the start) |
| `RR_STORE_CONNECTIONS` | to connect a store | references only | unset (**the store connector is OFF until you set it**) | the approved Shopify connections JSON (ADR 0020 section 3); setting it is your approval. Becomes `RR_STORE_CONNECTIONS_FILE`. Unset: no client is registered, every `/stores/*` call answers `NOT_CONNECTED` and no outbound call is made. Changing it redeploys detection (held snapshots are dropped) |
| `RR_STORE_CONNECTIONS_FILE` | — | — | (set by the entrypoint) | do not set yourself on Railway (setting both refuses the start); off Railway, the path of the connections file |
| `RR_STORE_SECRET_1`, `RR_STORE_SECRET_2` | to connect a store | yes | (you) | the two credential slots, written to `/run/zbm-secrets/rr-store-secret-1-file` and `/run/zbm-secrets/rr-store-secret-2-file`, which the connections JSON names. One store at a time on Railway: one Dev Dashboard app's client ID + secret, or one legacy token in slot 1 |
| `RR_STORE_SECRET_1_FILE`, `RR_STORE_SECRET_2_FILE` | — | — | (set by the entrypoint) | not read by detection-py; the paths are fixed and named in the connections JSON |
| `RR_STORE_READS_DISABLED` | no | no | unset | Revenue Recovery kill switch 1: `1` stops every store sync and snapshot (`STORE_READS_DISABLED`). Read when detection starts, so changing it needs a redeploy of detection. The stop that survives a restart |
| `RR_STORE_KILL_SWITCH_FILE` | no | no | unset; set it to `/tmp/rr-store.KILL` | Revenue Recovery kill switch 2: a path; while a file exists there every sync stops (a running one at its next page) and held snapshots are not served, with no restart. Stop at once: `railway ssh` into detection, `touch /tmp/rr-store.KILL`. detection has no volume, so the file is gone after a restart or redeploy: use kill switch 1 for a stop that must survive one |
| `RR_STORE_MAX_CONCURRENT_SYNCS` | no | no | `2` | store syncs at once across all clients, clamped 1..16 |
| `RR_STORE_SYNC_DEADLINE_SECONDS` | no | no | `120` | hard deadline per sync, clamped 10..600 |
| `DETECTION_COMPLIANCE_URL` | yes for store reads | no | `http://compliance.railway.internal:8380` (IaC) | ADR 0024: every sync and every snapshot handed to the scan path needs Compliance's current ALLOW for the client (`COMPLIANCE_NOT_ACTIVATED` 409 / `COMPLIANCE_UNAVAILABLE` 503, no store call made). Unset or unreadable token files: every client refused |
| `DETECTION_COMPLIANCE_TOKEN` | yes for store reads | yes | ref `compliance.COMPLIANCE_SERVICE_TOKEN` (IaC) | becomes `DETECTION_COMPLIANCE_TOKEN_FILE`; read when detection starts |
| `DETECTION_COMPLIANCE_CALLER_TOKEN` | yes for store reads | yes | (you) | detection's `revenue_recovery` caller token at Compliance; becomes `DETECTION_COMPLIANCE_CALLER_TOKEN_FILE` |
| `DETECTION_COMPLIANCE_TOKEN_FILE`, `DETECTION_COMPLIANCE_CALLER_TOKEN_FILE` | — | — | (set by the entrypoint) | do not set yourself on Railway |

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
| `ORCHESTRATOR_ALLOW_FIXTURE_SCANS` | no | no | unset (**never set it on Railway**) | `1` allows a scan of the fixture tenant (`client_id` omitted or `fixture-pool`); unset or `0` refuses it with 403 `FIXTURE_SCAN_DISABLED` before the ledger or detection is called, so fixture findings cannot reach the production ledger (AEGIS final go-live L-2). Any other value stops the start-up. Dev, tests and live runs only |

### stripe-gateway (orchestrator-go `cmd/stripe-gateway`)

| Variable | Required | Secret | Value / example | Notes |
|---|---|---|---|---|
| `PORT` | yes | no | `8470` (IaC) | copied to `STRIPE_GATEWAY_PORT` |
| `STRIPE_GATEWAY_PORT` | — | no | (from `PORT`) | |
| `STRIPE_GATEWAY_BIND_ADDR` | yes | no | `::` (IaC) | default `127.0.0.1` |
| `STRIPE_GATEWAY_FINANCE_URL` | yes | no | `http://finance.railway.internal:8410` (IaC) | → finance; a URL with a user or password (`user@host`) is refused in every mode (AEGIS L4), and the startup log prints it with any userinfo removed; a `?` or `#` (even empty; R2-2) or a non-ASCII host name (R2-1: `İ`/U+0130 and the Kelvin sign fold to ASCII under lower-casing) is refused too |
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
| `FIN_CALLER_TOKENS` | yes | yes | interpolated map (IaC): `rail_gateway`, `seo_02`, `onboarding`, `ops_cron`, `ops_alerts` | `onboarding` is your operator-held token (Secrets); `ops_cron` is fin-cron's (it reaches only the housekeeping jobs and the reconciliation); `ops_alerts` is ops-alerts' (it reaches only the founder-alerts job, ADR 0027); `scheduler` is never deployed |
| `FIN_ONBOARDING_CALLER_TOKEN` | yes to draft invoices | yes | (you) | the `onboarding` caller token you hold; ≥ 32 printable characters |
| `FIN_LEGAL_URL` | yes | no | `http://legal.railway.internal:8420` (IaC) | → legal. Without Legal no invoice can issue |
| `FIN_LEGAL_TOKEN` | yes | yes | ref `legal.LEGAL_SERVICE_TOKEN` (IaC) | |
| `FIN_LEGAL_CALLER_TOKEN` | yes | yes | (you) | Finance's `finance_31` caller token at Legal |
| `FIN_COMPLIANCE_URL` | yes | no | `http://compliance.railway.internal:8380` (IaC) | ADR 0024: invoice approval, billing readiness and every Stripe page need Compliance's current ALLOW for the client (`COMPLIANCE_NOT_ACTIVATED` / `DEPENDENCY_UNAVAILABLE:compliance_38`, rule FIN-08). Also the payout OFAC status and jurisdiction reads |
| `FIN_COMPLIANCE_TOKEN` | yes | yes | ref `compliance.COMPLIANCE_SERVICE_TOKEN` (IaC) | |
| `FIN_COMPLIANCE_CALLER_TOKEN` | yes | yes | (you) | Finance's `finance_31` caller token at Compliance |
| `FIN_STRIPE_INCOMING` | yes | no | `1` (IaC) | Stripe client payments on; then the four settings below are required |
| `FIN_STRIPE_LIVE` | yes | no | `0` (IaC) → `1` at go-live | `1` only with `sk_live_`/`rk_live_` keys |
| `FIN_STRIPE_SECRET_KEY` | yes | yes | (you; Stripe) | becomes `FIN_STRIPE_SECRET_KEY_FILE` |
| `FIN_STRIPE_WEBHOOK_SECRET` | yes | yes | (you; Stripe) | becomes `FIN_STRIPE_WEBHOOK_SECRET_FILE` |
| `FIN_STRIPE_SECRET_KEY_FILE`, `FIN_STRIPE_WEBHOOK_SECRET_FILE` | — | — | (set by the entrypoint) | do not set yourself |
| `FIN_STRIPE_SUCCESS_URL` | yes | no | `https://zbestmedia.example/payment/thanks` (you) | the https page Stripe returns the client to |
| `FIN_STRIPE_CANCEL_URL` | yes | no | `https://zbestmedia.example/payment/cancelled` (you) | |
| `FIN_CARD_PREPAYMENTS` | yes | no | `1` (IaC; founder Oct 30 2026) | card next to ACH on Revenue Recovery and SEO audit (`seo_audit_services`) invoices up to `FIN_CARD_MAX_INVOICE`; no surcharge; `0` turns cards off (the hourly job then closes pages offering a card) |
| `FIN_BUSINESS_TZ` | yes | no | `America/Los_Angeles` (IaC; the default) | CH-10: invoice due dates count from this zone's date; an unknown zone refuses start |
| `MAIL_FROM` | yes | no | `Z Best Media <billing@zbestmedia.com>` (IaC) | the only sender allowed (any other address refuses start); alone it does not switch mail on |
| `RESEND_API_KEY` | to send client email | yes | (you; Resend) | becomes `RESEND_API_KEY_FILE`. With `MAIL_FROM`, client email is ON (receipts, credit notes, refund confirmations, acknowledgements); unset = OFF, nothing is sent and nothing alerts |
| `RESEND_API_KEY_FILE` | — | — | (set by the entrypoint) | do not set yourself; set without `MAIL_FROM` refuses start |
| `FOUNDER_ALERT_EMAIL` | to get alert email | no (private) | (you: your own inbox) | ADR 0027: the ONE address founder alerts go to. With `FOUNDER_ALERT_FROM` and `RESEND_API_KEY`, alerts are ON; unset = OFF (the job's answer says why). A wrong value leaves alerts off, never stops Finance |
| `FOUNDER_ALERT_FROM` | to get alert email | no | `Z Best Media Alerts <alerts@zbestmedia.com>` (IaC) | only alerts@ or billing@zbestmedia.com (the domain Resend has verified) |

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
| `FIN_VI_URL` / `FIN_VI_TOKEN` / `FIN_VI_CALLER_TOKEN` | unset | thin client to a service not deployed; all three or none (the Compliance three are in the table above) |
| `FIN_SECOND_APPROVER_TOKEN`, `FIN_DUAL_HUMAN_THRESHOLD` | unset, `0.00` | optional second approver for payout batches |
| `FIN_RUN_WEEKDAY`, `FIN_RUN_LOCAL_TIME`, `FIN_RECON_LOCAL_TIME`, `FIN_CLOSE_WORKDAYS` | `FRI`, `10:00`, `07:00`, `5` | schedule |
| `FIN_LIMIT_PAYEE_RUN`, `FIN_LIMIT_PAYEE_30D`, `FIN_LIMIT_FIRST_PAYOUT`, `FIN_LIMIT_BATCH_NET`, `FIN_LIMIT_BATCH_ITEMS` | `2500.00`, `10000.00`, `500.00`, `50000.00`, `500` | payout limits |
| `FIN_MIN_PAYOUT`, `FIN_FINAL_PAYOUT_MIN`, `FIN_RESTRICTED_BUFFER` | `10.00`, `0.01`, `0.00` | |
| `FIN_1099NEC_THRESHOLD_2026` (and optional `FIN_1099NEC_THRESHOLD_2027` … `_2030`) | `2000.00` | |
| `FIN_PAYEE_CHANGE_COOLING_OFF_H`, `FIN_RELEASE_DELAY_H`, `FIN_APPROVAL_TTL_H` | `72`, `12`, `48` | hours |
| `FIN_ACCESS_REVIEW_DAYS`, `FIN_OFAC_MAX_AGE_DAYS`, `FIN_CLAWBACK_WRITEOFF_MIN_DAYS`, `FIN_EVIDENCE_RETENTION_DAYS` | `90`, `1`, `180`, `2557` | days |
| `FIN_MAX_RATE_PER_1000`, `FIN_MAX_CERTIFIED_VIEWS`, `FIN_WITHHOLDING_BASIS` | `1000.00`, 10^12, `gross` | |
| `FIN_MAX_INVOICE_TOTAL` | `250000.00` | the largest invoice Finance drafts (RTM-09) |
| `FIN_MEDIA_DEFAULT_MARKUP_PCT`, `FIN_MEDIA_MAX_MARKUP_PCT`, `FIN_MEDIA_RELEASE_HOLD_BD` | `15.00`, `100.00`, `2` | media buys |
| `FIN_CARD_MAX_INVOICE` | `5000.00` | card cap (founder M10; at most 5000.00). `FIN_CARD_PREPAYMENTS` is in the table above |
| `FIN_SEED_PATH`, `FIN_SEED_SHA256`, `FIN_ALLOW_UNPINNED_SEED` | the image's pinned seed, unset, unset | an unpinned seed is non-production only |
| `FIN_RECONCILE_MODE` | unset | `1` only while reconciling after a refused start (finance README) |
| `FIN_REQUEST_HEAD_TIMEOUT_SECONDS`, `FIN_KEEP_ALIVE_TIMEOUT_SECONDS`, `FIN_LIMIT_CONCURRENCY`, `FIN_SWITCH_INTERVAL_SECONDS`, `FIN_DRAINS_MAX` | `10`, `5`, `128`, `0.001`, `512` | launcher tuning |
| `FIN_ALERT_WINDOW_MIN`, `FIN_ALERT_REMIND_HOURS`, `FIN_ALERT_LEDGER_DOWN_MIN`, `FIN_ALERT_MAX_PER_DAY` | `60`, `24`, `15`, `30` | founder alerts (ADR 0027): one digest per window, a reminder for what is still open, ledger/Compliance silence before it alerts, a daily cap |

### fin-cron (deploy/scheduler; ADR 0026)

A Railway **cron** service: no volume, no domain, no port. Railway starts it at minute 17 of every hour
(`cronSchedule: "17 * * * *"`, UTC); it calls Finance and exits. Restart policy Never (a failed run is not re-run;
the next tick runs anyway, and every call is safe to repeat).

| Variable | Required | Secret | Value / example | Notes |
|---|---|---|---|---|
| `FIN_CRON_FINANCE_URL` | yes | no | `http://finance.railway.internal:8410` (IaC) | only a `*.railway.internal` http URL or an https URL is accepted |
| `FIN_CRON_SERVICE_TOKEN` | yes | yes | ref `finance.FIN_SERVICE_TOKEN` (IaC) | Finance's bearer |
| `FIN_CRON_CALLER_TOKEN` | yes | yes | (you; Secrets) | `ops_cron` at Finance; must differ from every other token |
| `FIN_CRON_RECON_HOUR` | yes | no | `7` (IaC) | the local hour of the daily reconciliation |
| `FIN_CRON_TZ` | yes | no | `America/Los_Angeles` (IaC) | the zone of that hour |

What each tick does: `POST /fin/v1/jobs/stripe-sessions/run` (closes Stripe pages that must not take money and
re-asks Compliance about every payment held `unverified`: the hourly hold re-check), `POST /fin/v1/jobs/client-mail/run`
(client email; retries with backoff), and at 07:xx Los Angeles time `POST /fin/v1/reconciliations/run`. A 503 (Finance
paused for a backup) is retried twice; anything else that is not 2xx prints an `ALERT fin-cron:` line and fails the
run (Railway shows the cron run as failed). Compliance's controls are not called here: they run inside Compliance.
Legal's daily jobs and seo's schedule tick are NOT run by fin-cron (they need their own `scheduler` callers; Open items).

### ops-alerts (deploy/scheduler; ADR 0027)

The same image as fin-cron, a second Railway **cron** service: every 5 minutes (`cronSchedule: "*/5 * * * *"`, the
shortest Railway cron interval) it calls `POST /fin/v1/jobs/founder-alerts/run` and exits. Finance decides what to
email (deduplicated, one digest per `FIN_ALERT_WINDOW_MIN`); this service only ticks. Its caller token reaches nothing
else at Finance.

| Variable | Required | Secret | Value / example | Notes |
|---|---|---|---|---|
| `FIN_CRON_JOBS` | yes | no | `alerts` (IaC) | `housekeeping` (fin-cron's default) or `alerts` |
| `FIN_CRON_FINANCE_URL` | yes | no | `http://finance.railway.internal:8410` (IaC) | as fin-cron |
| `FIN_CRON_SERVICE_TOKEN` | yes | yes | ref `finance.FIN_SERVICE_TOKEN` (IaC) | Finance's bearer |
| `FIN_CRON_CALLER_TOKEN` | yes | yes | (you; Secrets: `openssl rand -hex 32`, a NEW value, not fin-cron's) | `ops_alerts` at Finance |

If Finance itself is down, this service cannot email (Finance holds the alert state and the Resend key): each run
prints `ALERT fin-cron:` and Railway marks it failed. Watch for that with the heartbeat (Compliance step F) until a
second, independent alert path exists (ADR 0027, residual risk).

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
| `LEGAL_CALLER_TOKENS` | yes | yes | interpolated map (IaC): `finance_31`, `onboarding`, `compliance_38` | `onboarding` is your operator-held token (Secrets); `compliance_38` reads a client's acceptances (ADR 0024, HR-15) |
| `LEGAL_ONBOARDING_CALLER_TOKEN` | yes to record acceptances | yes | (you) | the `onboarding` caller token you hold; ≥ 32 printable characters |
| `LEGAL_COMPLIANCE_URL` | yes | no | `http://compliance.railway.internal:8380` (IaC) | ADR 0024: counsel-memo register proposals are delivered to Compliance's inbox |
| `LEGAL_COMPLIANCE_TOKEN` | yes | yes | interpolated `${{compliance.COMPLIANCE_SERVICE_TOKEN}}` (IaC) | an unresolved reference refuses the start |
| `LEGAL_COMPLIANCE_CALLER_TOKEN` | yes | yes | (you) | Legal's `legal_37` caller token at Compliance |
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
| `SEO_COMPLIANCE_URL` | yes | no | `http://compliance.railway.internal:8380` (IaC) | ADR 0024: a client tenant's paid audit, every scheduled slot and every client report need Compliance's current ALLOW for the tenant's client (`finance_client_id`); no override. Unset: every client run refused `COMPLIANCE_NOT_CONFIGURED` |
| `SEO_COMPLIANCE_TOKEN` | yes | yes | ref `compliance.COMPLIANCE_SERVICE_TOKEN` (IaC) | |
| `SEO_COMPLIANCE_CALLER_TOKEN` | yes | yes | (you) | seo's `seo_02` caller token at Compliance |
| `SEO_INVOICE_VERIFICATION` | no | no | `finance` | `trust` skips the invoice check (not for production) |
| `SEO_NON_PRODUCTION` | no | no | `0` | `1` = in-memory, tests only |
| `SEO_KILL_GLOBAL`, `SEO_KILLED_CAPABILITIES`, `SEO_KILLED_PROVIDERS` | no | no | `0`, empty, empty | kill switches at start (sticky) |
| `SEO_FETCH_TIMEOUT_SECONDS`, `SEO_FETCH_MAX_BYTES`, `SEO_FETCH_MAX_REDIRECTS`, `SEO_AUDIT_MAX_PAGES`, `SEO_PROBE_SAMPLES`, `SEO_BOT_INFO_URL` | no | no | `10`, 2 MiB, `5`, `10`, `5`, unset | fetcher and audit limits |
| `SEO_LOG_MAX_BYTES`, `SEO_LOG_RETENTION_DAYS`, `SEO_LOG_MAX_OPEN_INGESTS`, `SEO_LOG_MAX_INGESTS`, `SEO_LOG_TENANT_BYTES` | no | no | 256 MiB, `90`, `2`, `30`, 1 GiB | log ingests |
| `SEO_BOT_VERIFY_DNS`, `SEO_BOT_VERIFY_MAX` | no | no | `0`, `200` | |
| `SEO_SCHEDULE_BUDGET_RUNS`, `SEO_SCHEDULE_PERIOD_DAYS`, `SEO_FINANCE_TIMEOUT_SECONDS` | no | no | `8`, `30`, `5` | |
| `SEO_GSC_SERVICE_ACCOUNT` | no | yes | unset | Search Console: the whole Google service-account JSON key (you). Becomes `SEO_GSC_SERVICE_ACCOUNT_FILE`. Unset = `NOT_CONNECTED`, no call ever made |
| `SEO_GSC_SERVICE_ACCOUNT_FILE` | — | — | (set by the entrypoint) | the key file (owner-only, ≤ 16 KiB); never set it on Railway (setting it and `SEO_GSC_SERVICE_ACCOUNT` refuses the start) |
| `SEO_GSC_WINDOW_DAYS`, `SEO_GSC_PAGE_ROWS`, `SEO_GSC_MAX_PAGES`, `SEO_GSC_MAX_ROWS` | no | no | `28`, `5000`, `10`, `50000` | Search Console read window and pagination caps (seo-py README) |
| `SEO_GSC_MAX_REQUESTS`, `SEO_GSC_TIMEOUT_SECONDS`, `SEO_GSC_DEADLINE_SECONDS` | no | no | `30`, `10`, `60` | requests per read (must cover the page caps, else start is refused), per-attempt timeout, whole-read deadline |
| `SEO_GSC_READS_PER_DAY`, `SEO_GSC_RETENTION_DAYS` | no | no | `4`, `90` | reads per tenant per 24 h; days before breakdowns are served as totals only |
| `SEO_PERPLEXITY_API_KEY` | no | yes | unset | the Perplexity key as a plain variable (the Railway form; ADR 0017 W4). Unset = the engine is `NOT_CONNECTED`. Must differ from every seo token |
| `SEO_PERPLEXITY_API_KEY_FILE` | no | — | unset | the same key as a file; not used on Railway (setting both refuses start) |
| `SEO_PERPLEXITY_PROTOCOL` | no | no | `agent` | `agent` (the Agent API, `POST /v1/agent`) or `sonar` (the legacy request, rollback only; ADR 0017 W4-8) |
| `SEO_PERPLEXITY_MODEL`, `SEO_PERPLEXITY_TIMEOUT_SECONDS`, `SEO_PERPLEXITY_MAX_ATTEMPTS` | no | no | `perplexity/sonar`, `30`, `3` | closed model list per protocol (`perplexity/sonar` on the Agent API; `sonar`, `sonar-pro` on the rollback); hard deadline per question; requests per question |
| `SEO_PERPLEXITY_DAILY_CALLS`, `SEO_PERPLEXITY_DAILY_SPEND_USD`, `SEO_PERPLEXITY_TENANT_DAILY_CALLS`, `SEO_PERPLEXITY_TENANT_DAILY_SPEND_USD` | no | no | `0` each | daily budgets (UTC day). **0 = OFF: nothing is asked**; all four must be above 0 |
| `SEO_PERPLEXITY_RESERVE_USD_PER_CALL` | no | no | `0.05` | reserved per request before it is sent (0.001..5) |
| `SEO_REPORT_BRAND_NAME`, `SEO_REPORT_BRAND_ACCENT` | no | no | `Z Best Media`, `#1f4e79` | white label of the client report (preparer name, `#RRGGBB` accent); a bad value refuses start |
| `SEO_RENDERER`, `SEO_OPENAI_API_KEY_FILE`, `SEO_ANTHROPIC_API_KEY_FILE`, `SEO_GOOGLE_API_KEY_FILE`, `SEO_SEARCH_CONSOLE_CREDENTIALS_FILE`, `SEO_BING_WEBMASTER_KEY_FILE`, `SEO_PROMPT_VOLUME_PROVIDER`, `SEO_ZERO_DAY_URL`, `SEO_ORCA_PUBLISH_URL`, `SEO_CLIENTFIX_URL` | no | — | unset | not built (`SEO_SEARCH_CONSOLE_CREDENTIALS_FILE`: renamed): setting any refuses to start |
| `SEO_REQUEST_HEAD_TIMEOUT_SECONDS`, `SEO_KEEP_ALIVE_TIMEOUT_SECONDS`, `SEO_LIMIT_CONCURRENCY`, `SEO_SWITCH_INTERVAL_SECONDS`, `SEO_DRAINS_MAX` | no | no | `10`, `5`, `128`, `0.001`, `512` | launcher tuning |

### compliance (compliance-py)

ADR 0024. Private: no public domain. Settings not in this table must stay unset (the service refuses to start on
them, services/compliance-py/README.md).

| Variable | Required | Secret | Value / example | Notes |
|---|---|---|---|---|
| `PORT` | yes | no | `8380` (IaC) | copied to `COMPLIANCE_PORT` |
| `COMPLIANCE_PORT` | — | no | (from `PORT`) | |
| `COMPLIANCE_BIND_ADDR` | yes | no | `::` (IaC) | default `127.0.0.1` |
| `COMPLIANCE_DATA_DIR` | yes | no | `/data/compliance` (IaC; image default) | the local log; unset = in memory (nothing in force after a restart) |
| `RAILWAY_RUN_UID` | yes | no | `0` (IaC) | |
| `LEDGER_SERVICE_URL` | yes | no | `http://ledger.railway.internal:8090` (IaC) | → ledger; at start the local log is verified against it |
| `LEDGER_SERVICE_TOKEN` | yes | yes | (you) | compliance's own ledger caller token (`compliance-py` in `LEDGER_CALLERS`) |
| `COMPLIANCE_SERVICE_TOKEN` | yes | yes | (you) | refuses to start without it |
| `COMPLIANCE_ANDRE_APPROVAL_TOKEN` | yes for any approval | yes | (you) | header `X-Andre-Approval-Token` |
| `COMPLIANCE_CALLER_TOKENS` | yes | yes | interpolated map (IaC): `onboarding`, `finance_31`, `legal_37`, `seo_02`, `revenue_recovery` | every referenced token must be set first: an empty or unresolved one refuses the start. No `scheduler` caller exists (the scheduler is in process) |
| `COMPLIANCE_ONBOARDING_CALLER_TOKEN` | yes to activate clients | yes | (you) | the `onboarding` caller token you hold |
| `COMPLIANCE_LEGAL_URL` | yes | no | `http://legal.railway.internal:8420` (IaC) | → legal: a client's documents are current and accepted (HR-15) |
| `COMPLIANCE_LEGAL_TOKEN` | yes | yes | ref `legal.LEGAL_SERVICE_TOKEN` (IaC) | |
| `COMPLIANCE_LEGAL_CALLER_TOKEN` | yes | yes | (you) | Compliance's `compliance_38` caller token at Legal. The three are set together or not at all (unset: every client activation refused) |
| `COMPLIANCE_SCHEDULER_ENABLED` | yes | no | `1` (IaC) | the in-process scheduler: internal controls at start and every interval, the Change Watcher every 24 h |
| `COMPLIANCE_SCHEDULER_INTERVAL_SECONDS` | no | no | `21600` | 300..86400; keep it well under C-11's 24 h SLA |
| `COMPLIANCE_WATCHER_ENABLED` | yes | no | `1` (IaC) | the Change Watcher fetches the allowlisted government and platform pages and drafts proposals for you; it never changes the register |
| `COMPLIANCE_SANCTIONS_FRESHNESS_DAYS`, `COMPLIANCE_DISCLOSURE_MAX_OFFSET_S`, `COMPLIANCE_A11Y_MAX_AGE_DAYS`, `COMPLIANCE_SITE_OWNER_CALLER`, `COMPLIANCE_WATCHER_MAX_PROPOSALS_PER_CYCLE`, `COMPLIANCE_WATCHER_MAX_PROPOSALS_PER_SOURCE` | no | no | `1`, `3`, `30`, `creative_production`, `50`, `20` | services/compliance-py/README.md |
| `COMPLIANCE_SANCTIONS_PROVIDER`, `COMPLIANCE_A11Y_PROVIDER`, `COMPLIANCE_AUTO_REVERIFY_UNCHANGED`, `COMPLIANCE_WAYBACK_CAPTURE` | no | — | unset | not built: setting any refuses to start |
| `COMPLIANCE_SEED_PATH`, `COMPLIANCE_SEED_SHA256`, `COMPLIANCE_ALLOW_UNPINNED_SEED` | no | no | the image's pinned seed, unset, unset | an unpinned seed is non-production (every caller refuses its answers) |
| `COMPLIANCE_RECONCILE_MODE` | no | no | unset | `1` only while reconciling after a refused start (compliance README) |
| `COMPLIANCE_REQUEST_HEAD_TIMEOUT_SECONDS`, `COMPLIANCE_KEEP_ALIVE_TIMEOUT_SECONDS`, `COMPLIANCE_LIMIT_CONCURRENCY`, `COMPLIANCE_SWITCH_INTERVAL_SECONDS`, `COMPLIANCE_DRAINS_MAX` | no | no | `10`, `5`, `128`, `0.001`, `512` | launcher tuning |

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

   > **Warning — on a project that already exists** (finance and legal already deployed), set BOTH
   > `FIN_ONBOARDING_CALLER_TOKEN` (on finance) and `LEGAL_ONBOARDING_CALLER_TOKEN` (on legal) in Railway **before**
   > `railway config apply`: a service whose onboarding token is missing refuses to start on its next deploy (finance
   > without the first, legal without the second; their caller maps reference those variables; see [Secrets](#secrets)).
   > Finance also refuses to start when any reference to another service's token (e.g. `FIN_LEGAL_TOKEN`) is
   > unresolved. Generate every token with `openssl rand -hex 32` (a hand-made token containing `{{` is refused).
   > Always run `railway config plan` first and read it;
   > never `apply` without a plan you have read. The same holds for every later re-apply (step 13 included).

   ```bash
   cd .railway && npm ci && cd ..
   railway config plan      # read it: 8 services, 5 volumes, every variable; secrets show as preserved
   railway config apply     # creates the services and volumes; asks before anything destructive
   ```
   Services whose secrets are not set yet will fail their first deploy — expected, step 5 fixes it. If `plan` refuses
   because a preserved variable has no value yet (behaviour not documented by Railway; see Open items), stop there and
   bring the exact message back.
5. **Secrets.** Generate each value from [Secrets](#secrets) on your machine and set it in the Railway dashboard on the
   service in the "Set on" column (Variables tab → New Variable → mark Sealed). Do the six ledger tokens first, then
   `LEDGER_CALLERS` on the ledger with `devtools/ledger_callers.py new` (keep the file it writes; **not** Sealed: it
   holds hashes only). When Railway offers to deploy the changes, wait until step 8.
6. **Public domains.** orchestrator → Settings → Networking → Generate Domain (port 8080). stripe-gateway → Generate
   Domain (port 8470). No other service gets a public domain.
7. **Stripe (test mode).** Create the restricted key and set `FIN_STRIPE_SECRET_KEY` on finance; set
   `FIN_STRIPE_SUCCESS_URL` / `FIN_STRIPE_CANCEL_URL`. Then Developers → Webhooks → Add endpoint:
   - URL: **`https://<stripe-gateway's Railway domain>/webhooks/stripe`**
   - events, WEBHOOK EVENTS (services/finance-py/src/stripe_webhooks.py, the one list; print it with
     `python3 services/finance-py/devtools/stripe_webhook_events.py`; a test fails if this list differs):
     `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
     `checkout.session.async_payment_failed`, `checkout.session.expired`, `payment_intent.succeeded`,
     `payment_intent.payment_failed`, `charge.failed`, `charge.refunded`, `charge.refund.updated`, `refund.created`,
     `refund.updated`, `refund.failed`, `charge.dispute.created`, `charge.dispute.updated`,
     `charge.dispute.funds_withdrawn`, `charge.dispute.funds_reinstated`, `charge.dispute.closed`, `payout.created`,
     `payout.updated`, `payout.paid`, `payout.failed`, `payout.canceled`.
     `charge.refunded`, `charge.refund.updated` and `refund.*` are what bring a refund made in the Stripe Dashboard
     into the books (F7r) and link it to its credit note; without them a Dashboard refund never reaches Finance.
     An endpoint registered before Oct 31 2026 lacks them: add them (Developers → Webhooks → the endpoint →
     Update details → Select events).
   - copy the endpoint's signing secret (`whsec_...`) into `FIN_STRIPE_WEBHOOK_SECRET` on finance.
8. **Deploy, ledger first.** Deploy `ledger`; wait until it is Active and its log shows
   `ledger-rust: EVENT {"event":"ledger_created",...}` and `listening on [::]:8090`, and does **not** show the
   `LEDGER_CALLERS_FILE is not set` warning. Then deploy `legal`; then `compliance`; then `detection`; then `finance`; then `orchestrator`,
   `stripe-gateway` and `seo`; then `fin-cron` (it needs `FIN_CRON_CALLER_TOKEN` set first, on fin-cron; its first
   tick is at the next minute 17 and its log shows two `fin-cron: {...}` lines with `"ok": true`). A department that
   crashed because the ledger was not up yet: Redeploy it.
9. **Volume backups.** The IaC declares a daily backup schedule for `ledger-data`, `finance-data`, `legal-data`,
   `seo-data` and `compliance-data` (`backupSchedules: ["DAILY"]` on each service's volume attachment, the SDK field; AEGIS L6). Check that
   `railway config plan` shows the schedule and that each volume → Backups shows Daily after the apply; if the plan
   does not carry it (the engine that applies it is in the Railway CLI, not the SDK, and has not been run against
   this file), enable daily backups there by hand. Daily backups of different volumes are taken at different times
   and are not a consistent set: before risky work and weekly, take a **consistent backup set** (Finance quiesced,
   departments first, ledger last), and restore only with docs/runbooks/restore-and-reimport.md (it never voids paid
   invoices; it re-imports what Stripe holds). Restoring a backup makes the ledger refuse to start until the one-shot
   `LEDGER_ALLOW_RESET` flow (services/ledger-rust/README.md).
10. **Restart policy check.** Each service's Settings should show restart policy On Failure, 10 retries, and
    healthcheck path `/health` (from the IaC file).
11. **Dashboard on Vercel.** Project root `apps/dashboard-ts`. Set the five variables in the dashboard table
    (`ORCHESTRATOR_URL`, `ORCHESTRATOR_SERVICE_TOKEN`, `DASHBOARD_PASSWORD_HASH`, `DASHBOARD_SESSION_SECRET`, and
    your decision on `DASHBOARD_TRUSTED_PROXY_HOPS`) for Production, then deploy.
12. **Smoke test** — the checklist below.
13. **Going live with Stripe** (later, after the test-mode smoke passes): live restricted key into
    `FIN_STRIPE_SECRET_KEY`, a new live-mode webhook endpoint (same URL, same events) and its secret into
    `FIN_STRIPE_WEBHOOK_SECRET`, `FIN_STRIPE_LIVE=1` (in `.railway/railway.ts`, then `railway config plan`, read it,
    then `railway config apply`; both onboarding caller tokens must already be set, see step 4), redeploy finance.

Operating the private services (your approval routes on finance, legal and seo) goes through `railway ssh` into that
service and a request to `127.0.0.1:<port>` from inside it; there is deliberately no public route to them.

### Compliance (38): the hard gate (ADR 0024)

From the day this is applied, Compliance decides whether a client may be served. Finance refuses to approve an
invoice, report billing readiness or make a Stripe page, seo refuses a client's paid audit, scheduled slot or client
report, and detection refuses to read a client's store, unless Compliance answers a **current ALLOW** for that client.
ALLOW needs: the client in the service area (the 50 states, DC, Puerto Rico, Guam, the US Virgin Islands, American
Samoa, the Northern Mariana Islands), your recorded OFAC search (no
match, at most 30 days old at activation, 90 days of validity), the client's MSA current and accepted at Legal, every
rule it ran through still verified and unchanged, and control C-11 green. The service area is re-checked on every
read: change HR-05 or HR-07 and a client outside the new area is refused at once. When a client turns REFUSE (an OFAC
hit, an area change, a lapsed search) Finance closes its open Stripe pages (at the next checkout request, and in the
hourly `stripe-sessions` job). Compliance has no public domain.

**A. Tokens.** Generate each on your machine with `openssl rand -hex 32` and set it, Sealed, on the service named
([Secrets](#secrets)). All nine before step C: Compliance refuses to start while any token its caller map references
is missing.

| Variable | Set on |
|---|---|
| `COMPLIANCE_SERVICE_TOKEN` | compliance |
| `COMPLIANCE_ANDRE_APPROVAL_TOKEN` | compliance (keep a copy in your password manager) |
| `COMPLIANCE_ONBOARDING_CALLER_TOKEN` | compliance (keep a copy: you activate clients with it) |
| `COMPLIANCE_LEGAL_CALLER_TOKEN` | compliance |
| `LEDGER_SERVICE_TOKEN` | compliance (compliance's own ledger caller) |
| `FIN_COMPLIANCE_CALLER_TOKEN` | finance |
| `SEO_COMPLIANCE_CALLER_TOKEN` | seo |
| `DETECTION_COMPLIANCE_CALLER_TOKEN` | detection |
| `LEGAL_COMPLIANCE_CALLER_TOKEN` | legal |

The service variables `compliance`, `compliance-data` and these tokens only exist after step C's apply: if Railway has
no `compliance` service yet when you start, do step C's `plan`/`apply` first, then set the five compliance tokens
before its first deploy (it fails to start without them; that is expected and harmless).

**B. Ledger caller.** Add `compliance-py` to `LEDGER_CALLERS` with the procedure in
[Changing `LEDGER_CALLERS`](#changing-ledger_callers-adding-a-caller-rotating-one-token) (copy the deployed value
first; the script asks only for compliance's `LEDGER_SERVICE_TOKEN`):

```bash
python3 devtools/ledger_callers.py compare --current ~/zbm/ledger-callers.json --deployed ~/zbm/ledger-callers.deployed.json
python3 devtools/ledger_callers.py add compliance-py --current ~/zbm/ledger-callers.json --deployed ~/zbm/ledger-callers.deployed.json --out ~/zbm/ledger-callers.next.json
```

**C. Plan, check 0 destroy, apply.**

```bash
git switch main && git pull                      # the merged commit; .railway/railway.ts deploys main
cd .railway && npm ci && cd ..
railway config plan 2>&1 | tee ~/zbm/plan-compliance.txt
grep -inE "destroy|delete|remove|destructive" ~/zbm/plan-compliance.txt
```

Read the plan. It must show exactly: one service created (`compliance`), one volume created (`compliance-data`, region
`us-west2`, 5000 MB, daily backups), variables **added** on finance, seo, detection and legal (`*_COMPLIANCE_*`), and
`LEGAL_CALLER_TOKENS` changed (it gains `compliance_38`). The `grep` must print nothing, or only `0 destructive`. Any
destroy, delete or a change to an existing volume: **stop** and bring the plan back. Then:

```bash
railway config apply
```

**D. Deploy order.** ledger (with the new `LEDGER_CALLERS`), then legal, then compliance, then detection, then seo,
then finance. seo must run this version **before or together with** finance: this finance adds `held` to the invoice
status seo reads, and an older seo refuses any field it does not know (`FINANCE_RESPONSE_INVALID` on every paid run:
safe, but an outage until seo is updated). compliance's log prints its readiness at start (`compliance readiness ...`, `register not in force` until step
E) and `compliance scheduler: controls every 21600 s; Change Watcher every 24 h`.

**E. Approve the register (seed v2).** Read ADR 0024, decision 1 (what changed from v1), then open a tunnel and
approve. Get compliance's **service instance ID** as for seo (Ctrl+K / Cmd+K → Copy Service Instance ID).

```sh
ssh -N -L 127.0.0.1:18380:127.0.0.1:8380 <compliance-service-instance-id>@ssh.railway.com     # terminal 1, leave open
```

```sh
# terminal 2: header files only you can read (printf is a shell builtin: the token is never on a command line)
umask 077 && mkdir -p ~/.zbm-cmp && cd ~/.zbm-cmp
read -rs T && printf 'Authorization: Bearer %s\n' "$T" > auth.hdr && unset T            # paste COMPLIANCE_SERVICE_TOKEN
read -rs T && printf 'X-Compliance-Caller-Token: %s\n' "$T" > caller.hdr && unset T     # paste COMPLIANCE_ONBOARDING_CALLER_TOKEN
read -rs T && printf 'X-Andre-Approval-Token: %s\n' "$T" > andre.hdr && unset T         # paste COMPLIANCE_ANDRE_APPROVAL_TOKEN
C=http://127.0.0.1:18380
curl -sS $C/health
curl -sS -H @auth.hdr -H @caller.hdr $C/compliance/v1/inbox > inbox.json
python3 -c 'import json; p=[x for x in json.load(open("inbox.json")) if x["kind"]=="seed"][0]; print(p["proposal_id"], p["content_sha256"], len(p["proposed_rows"]))'
```

It must print one proposal with `121` rows. Approve it (one decision):

```sh
python3 -c 'import json,uuid; p=[x for x in json.load(open("inbox.json")) if x["kind"]=="seed"][0]; print(json.dumps({"request_id": "seed-"+uuid.uuid4().hex[:12], "decisions": [{"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve"}]}))' > decide.json
curl -sS -H @auth.hdr -H @andre.hdr -H 'Content-Type: application/json' --data @decide.json $C/compliance/v1/register/decisions
curl -sS $C/health                                             # register_version_in_force: 1, seed_pinned: true
```

`/health` also shows `scheduler`: `alive: true` and `last_controls_ok_at` (the start's run).

- If the scheduler thread itself dies, compliance logs `compliance scheduler: scheduler thread stopped unexpectedly`
  and exits with code 1; Railway restarts it by itself (ON_FAILURE, at most 10 times). A run of restarts in the
  deployment's history means look at that log line.
- If the controls keep failing (most often the ledger is down), compliance does **not** restart: `/health` answers
  **503** after 12 h without a successful controls run, and after 24 h C-11 closes every gate. Look at compliance's
  log (`compliance scheduler: controls run failed`, with the error type), fix the cause (usually: get the ledger
  Active again); the next 60 s tick runs the controls. Redeploying compliance does not help while the cause is there.

Railway checks `/health` only while a deployment starts, and compliance has no public domain, so an outside uptime
monitor cannot reach it directly. Set up a **private check with a heartbeat** (once, about 20 minutes):

1. At an uptime service that offers heartbeat (dead-man) monitors (for example Better Stack or Healthchecks.io), make a
   heartbeat monitor with a 15-minute period and a 15-minute grace, alerting your phone/email. Copy its ping URL
   (treat it as a secret).
2. In the Railway project, add a service from the public `curlimages/curl` image (pin its digest), no domain, no volume,
   with a cron schedule `*/10 * * * *` and the start command

   ```sh
   sh -c 'curl -fsS --max-time 10 http://compliance.railway.internal:8380/health >/dev/null && curl -fsS --max-time 10 http://finance.railway.internal:8410/health | grep -Eq "\"compliance_holds_open\":0[,}]" && curl -fsS --max-time 10 "$HEARTBEAT_URL" >/dev/null'
   ```

   and a Sealed variable `HEARTBEAT_URL` (the ping URL). It reaches compliance and finance over the private network
   (`/health` needs no token) and pings the monitor only while compliance's `/health` answers 200 **and** finance has
   no payment held by Compliance (`compliance_holds_open` is 0). A 503, a dead or unreachable service, an open hold, or
   the check itself failing all stop the pings, and the monitor alerts you. An alert therefore means one of two
   things: look at compliance's `/health`, then at finance's `/fin/v1/breaks` (step G, "A payment Compliance holds").
3. This service is not in `.railway/railway.ts`; add it there in a later change (the plan must then show it as already
   existing, not created twice), or leave it as the one hand-made service and note it in your Railway checklist.

Until it exists, and as a fallback when it does: **every day**, through the tunnels, check compliance's `/health` and
finance's `compliance_holds_open` (`curl -sS http://127.0.0.1:18410/health`). A hold grounded in sanctions has a
10-business-day OFAC reporting deadline, so a hold must not wait for a weekly look.

```sh
```

**F. Readiness.** C-11 passed at start (the scheduler's first run); check that nothing closes the gates:

```sh
curl -sS -H @auth.hdr -H @caller.hdr $C/compliance/v1/readiness | python3 -m json.tool
```

`gate_blockers` must be `[]` and `us_web_client_activation.blocking_now` `[]`. A `control_red:C-11` blocker: redeploy
compliance (the scheduler runs the controls at start) and look again. `expired` lists the rules already expired and the
gates each one affects (on Oct 30: the ten platform rows; none blocks a US web client).

**G. Activate a client** (about 10 minutes per client). The client id is the one you use at Finance and Legal.

1. OFAC search: open https://sanctionssearch.ofac.treas.gov/, search the client's exact legal name with the SDN and
   the Consolidated (non-SDN) lists both selected, and save the result page (or its export) as a file. Note the list
   date the page shows. Any hit: stop, do not serve the client, do not look for a way around it. Keep the saved file
   (your evidence folder, named after the client and the date): the record stores only its SHA-256, so the file is what
   proves the search later.
2. Record it (the name is stored only as a hash):

   ```sh
   CLIENT=<client id>
   EVIDENCE_SHA=$(shasum -a 256 <saved result file> | cut -d' ' -f1)
   read -r NAME && NAME_SHA=$(printf '%s' "$NAME" | shasum -a 256 | cut -d' ' -f1) && unset NAME   # type the legal name
   cat > att.json <<JSON
   {"request_id": "att-$CLIENT-$(date +%s)", "subject_id": "$CLIENT", "lane": "client",
    "searched_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)", "list_version": "<list date shown on the page>",
    "lists": ["SDN", "Consolidated"], "tool": "ofac_sanctions_list_search", "result": "no_match",
    "evidence_sha256": "$EVIDENCE_SHA", "searched_name_sha256": "$NAME_SHA"}
   JSON
   curl -sS -H @auth.hdr -H @andre.hdr -H 'Content-Type: application/json' --data @att.json $C/compliance/v1/sanctions/attestations
   ```

   Keep the `attestation_id` it prints.
3. The client's MSA acceptance at Legal (the acceptance you record today with `LEGAL_ONBOARDING_CALLER_TOKEN`): note
   its `acceptance_id`, the `version` and the document's `doc_sha256`. The onboarding token records a standard form's
   acceptance only with an invitation you issued for that client first (`POST /legal/v1/acceptance-invitations` with
   your Legal approval token and `{"request_id", "doc_id", "version", "party_ref": "client:<client id>"}`, valid 14
   days); the acceptance names its `invitation_id`, otherwise Legal answers `ACCEPTANCE_NOT_INVITED` (RTM-13).
4. Run the activation (edit the values in `<...>`; the state is the client's ISO 3166-2 code: `US-CA`, `US-NY`;
   a territory `PR`, `GU`, `VI`, `AS` or `MP`; set a flag `true` only if it is true of the client):

   ```sh
   cat > rule.json <<JSON
   {"request_id": "act-$CLIENT-$(date +%s)", "subject_id": "$CLIENT", "lane": "client", "facts": {
     "jurisdiction": {"declared_country": "US", "declared_region": "<US-CA>", "attested": true,
                      "attestation_ref": "<the MSA acceptance id: the client stated its address there>"},
     "flags": {"political_content": false, "child_directed": false, "health_data_shared": false,
               "biz_opp_client": false, "audience_data_sale": false, "marketplace": false, "pooled_client_funds": false},
     "target_jurisdictions": ["US"], "platforms": ["web"], "client_category": "general",
     "claims": {"claims_present": false, "health_or_earnings_claim": false, "claim_file_id": null,
                "claim_file_approved": false},
     "services_include_review_suppression": false, "outbound_cold_contact_countries": [], "hbnr_clause_signed": false,
     "sanctions_attestation_id": "<attestation_id>",
     "legal_documents": [{"doc_id": "client_msa", "version": "<1.0>", "doc_sha256": "<sha>",
                          "acceptance_id": "<lg-acc-...>"}]}}
   JSON
   curl -sS -H @auth.hdr -H @caller.hdr -H 'Content-Type: application/json' --data @rule.json $C/compliance/v1/rule
   curl -sS -H @auth.hdr -H @andre.hdr $C/compliance/v1/clients/$CLIENT/activation
   ```

   Read the status with your Andre header (`andre.hdr`): it shows the reasons. With the onboarding token a refusal
   reads only "the client is not activated (the reasons go to Andre)" (AEGIS r9 R9-4).

   For a territory use `"declared_country": "PR"` (or `"GU"`, `"VI"`, `"AS"`, `"MP"`), `"declared_region": null` and
   `"target_jurisdictions": ["PR"]` (the same code). `allowed: false` lists every unmet item with the rule it cites; fix it and run
   the rule again with a new `request_id`. The status must say `"decision": "ALLOW"`. Only then: invoice it at
   Finance, run its SEO audit, or connect its store. Re-run the search and the activation every 90 days
   (`valid_until`), after you approve an amendment of a rule the client ran through (the status says `<rule> changed
   since the ruling: re-activate`), and when Legal publishes a new MSA version that must bind existing clients (the
   documents are checked at activation, not on every read). A ZBC brand needs its own search with `"lane":
   "zbc_brand"`: a search recorded for one lane does not count for the other.

   **Which register changes force re-activating clients.** Approving an amendment of HR-05 or HR-07 other than their
   `parameters`, or any amendment of HR-03, HR-08, HR-14, HR-15, US-FTC-465-07 or US-OFAC-04 (parameters included;
   a re-verification is not an amendment), or a new rule on the activation gate, turns **every** client to REFUSE until each is re-activated (a new OFAC search
   first for any client whose last one is over 30 days old); the hourly job then closes their open Stripe pages.
   Changing only the service area (HR-05 `operate`, HR-07 `refuse`) refuses exactly the clients it takes out: adding a
   state or territory refuses nobody. Plan such an amendment for a day you can re-activate every client.
   A checkout request repeated with the same `request_id` after a client is re-activated returns its earlier answer
   (a page now expired); use a new `request_id` for a new page.

   **A payment Compliance holds.** When a client's payment is booked (a Stripe payment, a wire or ACH line from the
   bank feed, your application of an unapplied receipt, a ZBC campaign deposit), Finance asks Compliance first. If the
   answer is REFUSE, or Compliance cannot be reached, Finance still books the payment (the money arrived) but holds it:
   the
   invoice carries `held_compliance` (`kind` `refused` or `unverified`, and `sanctions` true or false), no receipt goes
   to the client, no media vendor is paid for it, the invoice status seo reads says `held: true` (seo refuses the run
   with `INVOICE_HELD`), and a break opens for you (`compliance_hold` or `compliance_unverified`). These breaks do not
   turn FC-02 red (they hold this client's money only); the private heartbeat check above alerts on them. No new
   checkout page is made on an invoice that carries a hold. Make a header file for your Finance approval token, as in
   step E (`read -rs T && printf 'X-Andre-Approval-Token: %s\n' "$T" > fin-andre.hdr && unset T`, paste
   `FIN_ANDRE_APPROVAL_TOKEN`), and read the holds through the finance tunnel of step I with it (other tokens see only
   that a payment is held, never whether it is a sanctions hold, including in the audit export and evidence and in
   the reasons of a refused checkout or billing readiness, which say only "compliance hold"): `curl -sS -H @fin-auth.hdr -H @fin-andre.hdr
   http://127.0.0.1:18410/fin/v1/breaks`. The hourly job asks Compliance again about every held client and marks a hold
   `sanctions: true` as soon as Compliance says the refusal rests on sanctions; a hold never loses that mark (its
   summary counts every change as `holds_changed`). While any of the client's invoices is held, or Compliance does not
   ALLOW the client, Finance also refuses every payment out for that client: a media vendor payment and a campaign
   deposit refund (proposal, your approval and its payment); your approval shows an OFAC warning on a sanctions
   ground. A held ZBC deposit does not fund its campaign until the hold is released on a current ALLOW. If a held
   payment is refunded at Stripe anyway, Finance books the refund as it happened, keeps the hold, and marks the
   hold's break (high severity and an OFAC note on a sanctions ground: report it and contact Stripe at once).

   **Read the hold's live ground** (your token only) at any time, and always immediately before you refund anything
   at Stripe:

   ```sh
   curl -sS -H @fin-auth.hdr -H @fin-andre.hdr http://127.0.0.1:18410/fin/v1/invoices/<invoice id>/compliance-hold
   ```

   It shows the stored `sanctions_ground`, Compliance's answer now (`compliance_now`), the merged `ground_now`, and
   `rejected_return_possible` (true only for a `place` Compliance confirms now). What to do depends on why it is
   held:

   - **`sanctions: true` (an OFAC match or potential match, or a place or campaign target under OFAC comprehensive
     sanctions). Do not refund, return or move the money: it is blocked unless OFAC tells you otherwise in
     writing.** Finance refuses a refund release and an ALLOW release for it; a refund made at Stripe is booked
     truthfully but releases nothing by itself. You run these steps yourself. Primary sources: 31 CFR 501.603
     (https://www.law.cornell.edu/cfr/text/31/501.603) and 501.604; OFAC's FAQs on blocking and rejecting
     (https://ofac.treasury.gov/faqs/topic/1601: FAQs 32, 35, 36, 53) and on reporting
     (https://ofac.treasury.gov/faqs/topic/1606: FAQs 49, 50, 1196).
     - **The default is BLOCKED, for every sanctions hold.** Never refund or return a sanctions hold on your own
       judgement. Finance shows the hold's `sanctions_ground` (Andre's token): `match` (an OFAC list match on the
       client) or `place` (the client's place or a campaign target is comprehensively sanctioned, with no match).
       - A `match` is always blocked. Finance refuses any return for it.
       - A `place` is blocked too unless OFAC tells you in writing that the funds are to be rejected. OFAC's general
         rule (FAQs 35, 36, 53) is that property a blocked person has an interest in is blocked, and a transaction
         that is prohibited with no blocked interest is rejected; but **each program's own regulations decide**. For
         example, under the Cuban Assets Control Regulations, dealings in any property in which Cuba or a Cuban
         national has an interest are prohibited (31 CFR 515.201, https://www.law.cornell.edu/cfr/text/31/515.201)
         and such blocked funds are held in an interest-bearing account at a domestic bank (31 CFR 515.205,
         https://www.law.cornell.edu/cfr/text/31/515.205): Cuba-related money is blocked, not returned. For any other
         program, read its regulations on ofac.treasury.gov and check with OFAC.
       - **Before any money moves, contact OFAC** (https://ofac.treasury.gov/contact-ofac) with the facts and ask in
         writing whether to block or reject. Keep their written answer. Without it, the payment stays blocked.
     - **Blocked.** The money stays frozen. FAQ 32 says blocked funds are held in a separate interest-earning account;
       31 CFR 501.603 lists "a blocked, interest-bearing account" as the usual place, and each program's regulation
       sets the rule (for example 31 CFR 515.205 above, or 31 CFR 510.203 for North Korea:
       https://www.law.cornell.edu/cfr/text/31/510.203). Ask your bank to open such a blocked account and move the
       funds there only as that regulation and OFAC direct. File a report of blocked property through the OFAC
       Reporting System (ORS) **within 10 business days** (501.603(b)(1)), and every year you still hold it on
       **June 30**, the Annual Report of Blocked Property by **September 30** (501.603(b)(2); form TD F 90-22.50
       through ORS, FAQ 50). The hold leaves only with one OFAC document, kept in your evidence folder, released with
       basis `ofac_authorization`, its SHA-256 and its kind:
       - `license`: a specific license or other written authorization from OFAC;
       - `unblocking_report`: the match was a **false positive** (mistaken identity): per FAQ 1196 you may unblock it
         and file an unblocking report within 10 business days (501.603(b)(3)), citing FAQ 1196 as the authorization.
         For error cases only: a payment correctly blocked needs a license;
       - `compliance_release`: OFAC's Compliance Release under 31 CFR 501.806 (mistaken identity or a similar error);
       - `delisting`: OFAC removed the person from the SDN List; no license is needed to unblock (FAQ 1196). Keep
         OFAC's notice of the removal.
     - **Rejected (only a `place` hold, only with OFAC's written guidance).** If OFAC has told you in writing that the
       payment is to be rejected, it is returned to the sender (FAQ 36: "rejected and returned to the originator").
       **Immediately before refunding**, read the hold's live ground (above): refund only if
       `rejected_return_possible` is `true`; if `compliance_now` is unavailable or `ground_now` is `match`, stop: the
       payment is blocked. Refund it to the client in the Stripe Dashboard, file a report of the rejected transaction
       through ORS **within 10 business days** (501.604), keep the ORS confirmation, and release with basis
       `ofac_rejected_return`: the refund's `F7r` journal entry, kind `rejected_transaction_report`, the
       confirmation's SHA-256, and `ofac_guidance_sha256`, the SHA-256 of OFAC's written guidance. Finance refuses it
       for a `match`, without the guidance hash, or unless the refund is this very payment's, booked after it,
       covering it; it asks Compliance again at release and refuses (and marks the hold `match`) if the refusal now
       rests on a match, or if Compliance cannot answer.
     - **Stripe.** Stripe screens too and may block, hold or report the same payment on its side. Contact Stripe
       support, tell them what you found and what you decided, and coordinate so neither side moves the money in a
       way the other did not expect.

     ```sh
     # blocked, leaving with an OFAC document
     curl -sS -H @fin-auth.hdr -H @fin-andre.hdr -H 'Content-Type: application/json' \
       --data "{\"request_id\": \"rel-<invoice>-1\", \"basis\": \"ofac_authorization\", \"ofac_document_kind\": \"<license|unblocking_report|compliance_release|delisting>\", \"ofac_authorization_sha256\": \"$(shasum -a 256 <the OFAC document> | cut -d' ' -f1)\"}" \
       http://127.0.0.1:18410/fin/v1/invoices/<invoice id>/compliance-hold/release
     # rejected (a place hold, with OFAC's written guidance), returned to the sender and reported
     curl -sS -H @fin-auth.hdr -H @fin-andre.hdr -H 'Content-Type: application/json' \
       --data "{\"request_id\": \"rel-<invoice>-1\", \"basis\": \"ofac_rejected_return\", \"entry_id\": \"<the F7r entry id>\", \"ofac_document_kind\": \"rejected_transaction_report\", \"ofac_authorization_sha256\": \"$(shasum -a 256 <the ORS confirmation> | cut -d' ' -f1)\", \"ofac_guidance_sha256\": \"$(shasum -a 256 <OFAC's written guidance> | cut -d' ' -f1)\"}" \
       http://127.0.0.1:18410/fin/v1/invoices/<invoice id>/compliance-hold/release
     ```

   - **`sanctions: false`, `kind: refused`** (out of the service area, re-screen overdue, documents not current...):
     refunding is the normal path. **Immediately before refunding at Stripe**, read the hold's live ground (above) and
     check `compliance_now` is available with `sanctions_ground` null: if it names a ground, follow the sanctions case
     instead; if Compliance is unavailable, wait. Refund the payment in the Stripe Dashboard; Finance books the refund
     from Stripe's event (an `F7r` journal entry). Then release the hold with that entry (Finance asks Compliance again
     and refuses if the refusal now rests on sanctions, or if Compliance cannot answer):

     ```sh
     curl -sS -H @fin-auth.hdr -H @fin-andre.hdr -H 'Content-Type: application/json' \
       --data '{"request_id": "rel-<invoice>-1", "basis": "refund", "entry_id": "<the F7r entry id>"}' \
       http://127.0.0.1:18410/fin/v1/invoices/<invoice id>/compliance-hold/release
     ```

     It is refused unless the entry is a refund of this very payment, booked after it, and the refunds cover the held
     amount. After the refund the invoice is `issued` again (the client owes it on paper): there is no void for an
     issued invoice yet, so note it for your accountant until that exists. If instead the client is re-activated at
     Compliance and you keep the payment, release with `{"basis": "compliance_allow"}`: it is refused unless Compliance
     answers a current ALLOW now, and the held receipt then goes out (none if anything was refunded).
   - **A wire or ACH payment** (the bank path) cannot be refunded through Finance: there is no wire-return flow yet.
     It leaves with `compliance_allow` once the client is re-activated, or with an OFAC ground; for a non-sanctions
     refusal you want to return, make the return at the bank by hand and note it for your accountant.
   - **`kind: unverified`**: the hourly `stripe-sessions` job asks Compliance again for each such payment just before
     acting. A current ALLOW releases the hold and the receipt goes out; a REFUSE turns it into a `refused` hold (with
     `sanctions` set as Compliance says). Nothing to do unless Compliance stays down.
   - `POST /fin/v1/breaks/{break_id}/resolution` refuses these breaks; only the release above clears a hold.
5. Close the tunnel (Ctrl-C) when done; delete `~/.zbm-cmp/*.hdr` if you do not activate another client today.

**H. Re-verify before rules expire (nothing is extended by itself).** The readiness answer lists every expired rule and
every rule a US web client runs through with its expiry date. Re-verify = read the rule's source page today, confirm
the rule still says what the register says, save the page, then:

```sh
cd ~/.zbm-cmp && ID=US-OFAC-04
curl -sS -H @auth.hdr -H @caller.hdr $C/compliance/v1/register/$ID > row.json
python3 <your checkout>/services/compliance-py/devtools/reverify_proposal.py --row row.json --page <saved page> \
  --excerpt "<the sentence that still holds>" > reverify.json
curl -sS -H @auth.hdr -H @andre.hdr -H 'Content-Type: application/json' --data @reverify.json $C/compliance/v1/register/proposals
curl -sS -H @auth.hdr -H @caller.hdr $C/compliance/v1/inbox > inbox.json
python3 -c 'import json,sys; p=[x for x in json.load(open("inbox.json")) if x["kind"]=="reverify" and x["target_id"]==sys.argv[1]][0]; print(json.dumps({"request_id": "rv-"+p["proposal_id"], "decisions": [{"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve"}]}))' $ID > decide.json
curl -sS -H @auth.hdr -H @andre.hdr -H 'Content-Type: application/json' --data @decide.json $C/compliance/v1/register/decisions
```

**Early warning (RTM-06).** Compliance's readiness answer carries `expiry_warnings` (rules expiring within 30, 14 and 7
days, and separately those a client's activation relies on, with the next one), every start and scheduled run prints
a `WARNING` line for them, compliance's `/health` carries `rules_expiring`, and Finance's billing readiness shows you
`compliance_rule_expiry` (your token only). Check it weekly with the heartbeat check; re-verify at the 30-day warning.

The scheduler also drafts these by itself when the Change Watcher fetched the row's page in the last 2 days and the
rule expires within 7 days: they appear in the inbox; approve them the same way. Checklist (dates on the seed approved
Oct 30, 2026):

| By | Rules | Why | Time |
|---|---|---|---|
| now | seed v2 (step E) | nothing is in force before it | 20 min reading ADR 0024 + 1 decision |
| **2026-12-25** | **US-OFAC-04** | every client activation status turns REFUSE that day (it is in every client's rule set): no invoices, audits or store reads | 10 min |
| 2026-12-25, same sitting | US-OFAC-01, US-OFAC-02, US-OFAC-03 | same OFAC pages; the basis of HR-14; ZBC creators and entity payees | 5 min each |
| 2027-03-25 | US-FTC-465-07 | in every client's rule set (review suppression) | 10 min |
| only for TikTok, X, YouTube or Snapchat clients, or Stripe Connect payouts | PLT-TT-01..04, PLT-X-01, PLT-X-02, PLT-YT-01, PLT-YT-02, PLT-SNAP-01, US-STRIPE-KYC (expired 2026-10-26) | they block those platforms only; every 30 days after | 5 min each, about 50 min |
| 2026-12-25, when ZBC payouts, email campaigns or claims are in scope | US-FTC-D101-01, US-FTC-D101-02, US-FTC-FAQ-01, US-FTC-5-01, US-FTC-437-01, US-FTC-CANSPAM, US-FTC-HBNR, US-IRS-BWH, US-IRS-TIN, US-IRS-1099K, US-IRS-FOREIGN, AGE-01, US-CPPA-2025, PRG-ECCP | not in a plain US web client's activation | 5-10 min each, about 2 h |
| when convenient | US-SALESTAX-NEXUS | unverified (vendor table); it only feeds Finance's report, never a gate | 30 min with each state's own page |

**I. Monthly: per-jurisdiction sales.** Through a tunnel to finance (as in step E, finance's instance ID, port 8410
→ 18410), with two header files made as in step E:

```sh
ssh -N -L 127.0.0.1:18410:127.0.0.1:8410 <finance-service-instance-id>@ssh.railway.com      # terminal 1
cd ~/.zbm-cmp
read -rs T && printf 'Authorization: Bearer %s\n' "$T" > fin-auth.hdr && unset T             # paste FIN_SERVICE_TOKEN
read -rs T && printf 'X-FIN-Caller-Token: %s\n' "$T" > fin-onboarding.hdr && unset T        # paste FIN_ONBOARDING_CALLER_TOKEN
curl -sS -H @fin-auth.hdr -H @fin-onboarding.hdr http://127.0.0.1:18410/fin/v1/readiness/sales-tax-nexus | python3 -m json.tool
```

`flagged` lists every state or territory at 75% or more of its economic-nexus threshold (trailing 12 months, the
calendar year or the previous calendar year; Hawaii, Nevada and Puerto Rico also by transactions): register there before the threshold is crossed
(memo MEMO-FIN-CQ-11-2026-10-30). `unknown` holds invoices issued before this change (no recorded jurisdiction).
Guam, the US Virgin Islands, American Samoa and the Northern Mariana Islands have no threshold number: the report lists
them under `thresholds.check_before_first_client` ("local gross receipts/business tax — check before first client")
and flags any of them with a sale. Do that check before the first client there.

### Money operations: void, credit notes, refunds, client email (ADR 0026)

Through the finance tunnel with `fin-auth.hdr` and `fin-andre.hdr` (Compliance step I). `F=http://127.0.0.1:18410`.
Every route below takes Andre's approval token; none takes a caller token.

**SEO audit invoices take card or ACH.** Draft the SEO Technical Audit with line code `seo_audit_services` (not
`strategy_services`) and `"payment_methods": ["ach", "card"]`; the Stripe page then offers both (USD only, no
surcharge, at most $5,000 by card, `FIN_CARD_PREPAYMENTS=1`). Other strategy work stays ACH or wire.

**Billing contact first** (once per client, from the signed SOW): the only address client email ever goes to.
```sh
curl -sS -X PUT -H @fin-auth.hdr -H @fin-andre.hdr -H 'Content-Type: application/json' $F/fin/v1/clients/<client id>/billing-contact \
  -d '{"request_id":"contact-<client id>-1","email":"ap@client.example","name":"Accounts payable"}'
```

**Cancel an unpaid invoice in full** (refund policy 5.3; MSA 4.12(g), within 10 business days after the SOW ends):
read the invoice (`GET $F/fin/v1/invoices/<id>`, keep `content_sha256`), then
`POST $F/fin/v1/invoices/<id>/void` with `{"request_id": "...", "content_sha256": "...", "reason_code": "sow_ended_unpaid"}`.
Finance first closes the invoice's open Stripe pages at Stripe (if Stripe cannot confirm, nothing changes: retry; if
the client already paid on a page, it refuses: wait for the payment, then credit the paid invoice). Reason codes:
`sow_ended_unpaid`, `sow_ended_prepaid`, `cancelled_before_work`, `nonconforming`, `billing_dispute`,
`infringement_refund`, `audit_not_run`, `other`.

**Reduce an invoice, or owe a refund: a credit note.** `POST $F/fin/v1/invoices/<id>/credit-notes` with
`{"request_id", "content_sha256", "amount": "300.00", "reason_code"}`.
- Unpaid invoice: the amount due drops (a new Stripe page asks for the new amount; the old page is closed first).
- Paid invoice: the credit note records a refund ZBM owes (account 2050) and opens a `credit_note_refund_due` break
  with `due_by` = 10 business days (MSA 4.10; weekends and US federal bank holidays skipped, Federal Reserve schedule, ADR 0027).
  **Then refund it in the Stripe Dashboard** (Payments → the payment → Refund, the credit note's amount). The
  `charge.refunded` webhook books it against the credit note (F7r from 2050), marks the credit note `refunded`,
  resolves the break and queues the "refund sent" email. A refund made before the credit note is linked when the
  credit note is issued.
- Refused: a draft, a media prepayment (cancel the media buy), a ZBC deposit, an invoice with a Compliance hold or a
  dispute, more than is left to credit, or a refund larger than the client's money ZBM still holds. A credit note
  that owes a refund asks Compliance first: a client refused on SANCTIONS grounds is never refunded (OFAC steps
  instead), and no answer from Compliance refuses it.
- `GET $F/fin/v1/invoices/<id>/credit-notes` lists them; `GET $F/fin/v1/credit-notes/<id>` reads one.

**Acknowledge a client request within 2 business days** (MSA 4.12(f)): `POST $F/fin/v1/clients/<client id>/acknowledgements`
with `{"request_id", "topic": "refund_request" | "cancellation" | "billing_dispute" | "payment_question",
"received_on": "2026-11-02", "invoice_id": "<optional>"}`. The answer gives `acknowledge_by` and says `late` if that date has passed.

**Client email** (off until configured). One time, in Resend: add and verify the domain `zbestmedia.com` (Resend shows
the DNS records; adding them is your DNS change, not the code's), then in the domain's settings turn **Open tracking
OFF** and **Click tracking OFF**, then create an API key with **Sending access** for that domain only and set it as
`RESEND_API_KEY` on finance (Sealed). `MAIL_FROM` is already in the IaC. After the redeploy, the hourly fin-cron run
sends what is queued: payment receipts, credit notes, invoice cancellations, refund confirmations, acknowledgements.
Plain text plus simple HTML, from billing@zbestmedia.com, to the billing contact only; never a hold reason or anything
about sanctions. Retries back off 15 min, 30, 60, 120, 240, 480; after 7 attempts (or a permanent refusal such as an
unverified domain) the message is `send_failed`, a high-severity `client_mail_failed` break opens and finance logs
`ALERT finance: client_mail_failed`. Fix the cause, then `POST $F/fin/v1/client-mail/<id>/requeue`
(`{"request_id": "..."}`); `GET $F/fin/v1/client-mail/<id>` shows a message's state. A client with no billing contact
gets a `client_mail_no_contact` break; nothing is sent until the contact is recorded.

**Alerts to watch** (until a log alert is wired, search the finance log daily): `ALERT finance:` (a payment on a
Stripe page Finance does not know, CH-4; a client email that failed for good) and `ALERT fin-cron:` (a housekeeping
call that failed). Breaks with `"severity": "high"` in `GET $F/fin/v1/breaks` are the same events, kept.

### Connecting Search Console (optional)

Search Console stays `NOT_CONNECTED` until you do this; seo starts and runs normally without it.

1. Create the Google Cloud project, enable the Google Search Console API and create the service account and its
   **JSON** key: ADR 0017 GSC-10 steps 1-3 (docs/adr/0017-seo-answer-intelligence-department-architecture.md).
2. Railway → `seo` → Variables → New Variable: name **`SEO_GSC_SERVICE_ACCOUNT`**, value: open the downloaded `.json`
   file in a text editor and paste **the whole file** as it is (from the first `{` to the last `}`; do not
   base64-encode it, do not edit the `\n` sequences in `private_key`). Mark it **Sealed**. Do not create
   `SEO_GSC_SERVICE_ACCOUNT_FILE`: the entrypoint sets it (setting both refuses the start). Do not change the start
   command (ADR 0017 GSC-9's start-command form is superseded: it would bypass the entrypoint that runs seo as uid
   10001).
3. Deploy seo. At start the entrypoint writes the key to `/run/zbm-secrets/seo-gsc-service-account-file` (0600, owned
   by uid 10001) and seo loads it; a key it cannot parse refuses the start with
   `SEO_GSC_SERVICE_ACCOUNT_FILE: <reason>` (the key itself is never printed).
4. Check `GET /seo/v1/status` (through `railway ssh`, as in the other approval routes): `search_console.state` is
   `connected` and `search_console.service_account` is the key's `client_email`.
5. Delete the downloaded `.json` from your machine; then ADR 0017 GSC-10 steps 6-7 for each client.

To disconnect: delete the variable (or empty it) and redeploy; seo then reports `NOT_CONNECTED` again. To rotate:
create a new key in Google Cloud, replace the variable's value, redeploy, then delete the old key in Google Cloud.

### Delivering an SEO client report from Railway (AEGIS final go-live M-1)

The client report is produced only by `services/seo-py/devtools/render_report.py fetch`. That command is the
recorded delivery path: the service records the render on the ledger before answering, and the command writes the
file (mode 0600) only when the document's SHA-256 and size match the answer and the service's ledger-backed
`client-reports` list holds the same `report_id` and `document_sha256`. A hand-made request skips both checks, so do
not use one. `--api` must be https or a loopback address, because the request carries three tokens, and seo has
neither https nor a public domain.

**Decision: an SSH tunnel to the container's loopback, with the command run on your own machine.** The script is
**not** added to the seo image. The tunnel keeps the three tokens and the client document off the server: they never
sit in the container's filesystem or shell history, and the file is written 0600 on your machine, where you send it
from. Railway SSH supports local port forwarding to "the container's loopback and your project's private network";
public destinations are refused, and the connection "is dialed from inside the container, so `127.0.0.1` is the
container's own loopback" (https://docs.railway.com/cli/ssh, read Oct 25 2026). seo binds `::` dual-stack
(`SEO_BIND_ADDR`, `serve.py` → `dual_stack`), so the container's `127.0.0.1:8500` reaches it.

You need: an SSH key registered with Railway (`railway ssh keys add`; the CLI also prompts on the first
`railway ssh`), a checkout of the commit seo runs, and Python 3.12 or 3.13 with `services/seo-py/requirements.txt`
installed (the script imports `httpx` and `client_report` from `services/seo-py/src`).

1. Get seo's **service instance ID**: in the dashboard open the `seo` service, press `Ctrl+K` / `Cmd+K`, choose
   **Copy Service Instance ID**. Do not use **Copy Service ID**: it is not a valid SSH target.
2. Open the tunnel in its own terminal and leave it running (`-N` holds the tunnel open without a shell):

   ```sh
   ssh -N -L 127.0.0.1:18500:127.0.0.1:8500 <seo-service-instance-id>@ssh.railway.com
   ```

3. In a second terminal, check that the tunnel reaches seo: `curl -sS http://127.0.0.1:18500/health` should return
   200 with `{"status":"ok"}`.
4. Write the three tokens to files that only you can read. Copy the values from the seo service's Variables tab:
   `SEO_SERVICE_TOKEN`, the `dashboard` value inside `SEO_CALLER_TOKENS`, and `SEO_ANDRE_APPROVAL_TOKEN`. Paste each
   one into its file; do not echo it on a command line.

   ```sh
   umask 077 && mkdir -p ~/.zbm-seo && cd ~/.zbm-seo
   cat > service.token     # paste SEO_SERVICE_TOKEN, Enter, Ctrl-D
   cat > dashboard.token   # paste the "dashboard" token from SEO_CALLER_TOKENS, Enter, Ctrl-D
   cat > andre.token       # paste SEO_ANDRE_APPROVAL_TOKEN, Enter, Ctrl-D
   mkdir -p -m 700 ~/client-reports
   ```

5. From the checkout's `services/seo-py`, render. The tenant is the client's tenant id; the audit id is
   `seo-aud-<40 hex>` from the completed audit. Pick the request id once and keep it in the shell, so that a retry
   reuses it:

   ```sh
   RID="$(python3 -c 'import uuid; print(uuid.uuid4())')"; echo "request id: $RID"
   python3 devtools/render_report.py fetch --api http://127.0.0.1:18500 --tenant <tenant> --audit <audit-id> \
     --format html --out ~/client-reports/<tenant>-<audit-id>.html --request-id "$RID" \
     --service-token-file ~/.zbm-seo/service.token --caller-token-file ~/.zbm-seo/dashboard.token \
     --andre-token-file ~/.zbm-seo/andre.token
   ```

   Exit 0 prints one JSON line with `report_id`, `request_id`, `document_sha256` and `bytes`. Keep that line with the
   delivery record. Exit 2 (`refused: ...`) means nothing was written. The command checks `--out` (directory present
   and writable, not a symlink) **before** it calls seo, so an output problem never records a render. If it fails
   after calling seo, it prints `request id: <RID>`, because seo may already have recorded the render. Fix the cause,
   then re-run the same command with the same `--request-id "$RID"`: seo returns the same document, or
   `REPORT_CHANGED` if the audit's retained view changed since, and does not record a second render.
6. Have a person review the document before it is sent. Then close the tunnel (Ctrl-C) and delete
   `~/.zbm-seo/*.token`.

This procedure has not been run against a real Railway deployment: nothing in this repository has been applied to
Railway yet. CI covers the command against a loopback service (seo-py tests and live run); the tunnel itself is
Railway's, as documented above. If the tunnel is refused, do not fall back to a hand-made request. Stop, and treat
delivery as blocked.

### Revenue Recovery: connecting a real store

AEGIS A2 H-2: before this, detection on Railway could not read any store (no ledger credential, no connections file,
no credential file). The image now maps four `*_FILE` settings from sealed variables (table in "How `*_FILE` secrets
map onto Railway"), with `RAILWAY_RUN_UID=0` so the entrypoint writes them as root and runs detection as uid 10001.
Detection keeps no state, so it has **no volume**: the connections JSON and the credentials are sealed Railway
variables, not files on a backed-up disk. Every one of them unset = the connector stays off (`NOT_CONNECTED`; with
connections but no ledger token, `LEDGER_UNAVAILABLE` before any byte goes to Shopify).

How the pieces fit (ADR 0020): you call detection's `POST /stores/sync` (private: SSH tunnel), which records
`rr_store_read_requested` on the ledger, reads the store's orders read-only and keeps a minimised snapshot **in
memory**; then the orchestrator's `POST /revenue-recovery/scan?client_id=<id>` (public, bearer) takes that snapshot,
runs the agents and records the findings on the ledger; the dashboard's Revenue Recovery view reads them back through
`GET /revenue-recovery/findings`. That route returns **every** client's findings (AEGIS A2 M-1): the dashboard must
stay Andre-only.

**Shopify side (your own store; a client's store follows ADR 0020 section 9).** Read this first: the documented
path below works only if the store is in the same Shopify organization as the app (step 4). Whether a live merchant
store such as PCA can be is UNVERIFIED. If it cannot, use the fallback after step 5 (a custom-distribution app and a
static token). Admin-created ("legacy") custom apps
can no longer be created (shopify.dev, "Generate access tokens for admin-created custom apps": "You can no longer create
new admin-created custom apps"; existing ones keep working). The documented path for your own store is a Dev
Dashboard app with the client credentials grant (shopify.dev, "Create apps using the Dev Dashboard" and "Authenticate
an app for stores in your organization"):

1. Open the Dev Dashboard (dev.shopify.com) and create an app (the "Dev Dashboard" path, not Shopify CLI).
2. On its **Versions** page create a version: Scopes = **`read_orders` only** (no other scope, no `write_` scope:
   detection refuses the credential with `STORE_TOKEN_EXCESS_SCOPE` / `STORE_TOKEN_HAS_WRITE_SCOPE`, before reading
   any order). The App URL can stay the default. Select **Release**. (`read_orders` covers orders from the last 60
   days, which is the default `lookback_days`.)
3. **Install** it: Installs card → Install app → pick your store → Install, approving the scope.
4. Check that the store is in the **same organization** as the app: Dev Dashboard → the store must be listed there.
   Shopify: "Owning a store or having it installed doesn't automatically place it in your org"; if it is not, the token
   exchange fails (`shop_not_permitted`) and detection answers `STORE_AUTH_REFUSED`. UNVERIFIED how a live merchant
   store (not a dev store) is placed in the app's organization; if this check fails, use the fallback below.
5. App settings → copy the **Client ID** and **Client secret** into your password manager. Never into chat or email.

**If step 4 fails (fallback, AEGIS detection-branch M-3).** This is the case when the store is not in the app's
organization: no store listed, or `STORE_AUTH_REFUSED` at step 12. A live store such as PCA may well land here. Do not
try to force the client credentials grant. Shopify's documented route for such a store (shopify.dev, "Authenticate an
app for stores in your organization"): "The client credentials grant can't reach a store outside your organization,
including a client's store", so "Distribute your app to that store with custom distribution so that a merchant
installs it", then "the authorization code grant if it runs outside" the Shopify admin.

- a. **Partner organization.** Custom distribution needs one. shopify.dev, "Select a distribution method": "The
  Distribution card appears only for apps that belong to a Partner organization". UNVERIFIED: whether you already
  have a Partner account, and what creating one involves.
- b. **Install.** In the app: Distribution → **Custom distribution** → enter the store's `*.myshopify.com` domain →
  **Generate link**. Open the link as the store owner and install, approving `read_orders` only.
- c. **Token.** Get an offline access token with the authorization code grant. Leave `expiring` at its default `0`,
  which gives a non-expiring token. shopify.dev, "Offline access tokens": "Non-expiring offline tokens grant permanent
  access until the app is uninstalled or the secret is revoked". The rule that public apps must use expiring tokens
  "doesn't apply to custom apps or apps created by merchants". Detection never refreshes a token, so an expiring
  (1 hour) token would work for one hour only. UNVERIFIED: the exact manual steps of that grant for an app with no
  server of its own (redirect URL, `state`, the HMAC check). Do it with someone who has done it before, and never
  paste the code or the token into a chat.
- d. **Railway.** Put the token in `RR_STORE_SECRET_1` (Sealed) and leave `RR_STORE_SECRET_2` unset. In step 9, use
  the static-token credential below in place of the `client_credentials` one, and skip the Client ID/secret in step 8.
  Keep `RR_STORE_KILL_SWITCH_FILE` in step 8: it is required here too.

The static-token credential (detection's `StaticTokenRef`, services/detection-py/src/store_connector/config.py):

```json
{"kind": "static_token", "token_file": "/run/zbm-secrets/rr-store-secret-1-file"}
```

What detection does with it, as for the client credentials token:

- reads it from that file (owner-only, uid 10001);
- checks its scopes against the store before reading any order: `read_orders` / `read_all_orders` only, no `write_`
  (`STORE_TOKEN_EXCESS_SCOPE` / `STORE_TOKEN_HAS_WRITE_SCOPE`);
- never stores or logs it.

The token does not expire. To revoke it, uninstall the app from the store or rotate the app's client secret.

An existing legacy custom app with **only** `read_orders` also works the same way: its Admin API access token goes in
`RR_STORE_SECRET_1`, `RR_STORE_SECRET_2` stays unset, and the credential is the static-token one above. Do not reuse
an app that has any other scope.

**Railway side.** Merge first: `railway config plan` must show on `detection` the new `RAILWAY_RUN_UID=0`,
`DETECTION_LEDGER_URL` and the preserved `DETECTION_LEDGER_TOKEN`, `RR_STORE_CONNECTIONS`, `RR_STORE_SECRET_1`,
`RR_STORE_SECRET_2`; then `railway config apply`.

6. Generate `DETECTION_LEDGER_TOKEN` (`openssl rand -hex 32`) and set it on **detection** (Sealed).
7. Add detection's caller with "Changing `LEDGER_CALLERS`" (Secrets), its steps 1 to 7: copy the deployed value and
   compare, the baseline check on the other four, then `python3 devtools/ledger_callers.py add detection-py --current
   ~/zbm/ledger-callers.json --deployed ~/zbm/ledger-callers.deployed.json --out ~/zbm/ledger-callers.next.json`
   (asks only for the token from step 6; the four Sealed tokens are not needed). Replace `LEDGER_CALLERS` on the
   **ledger**, redeploy it, wait for Active, run the log check and the four unchanged departments' checks, and make
   the new value the record. Detection's own check (that section's step 8: `rr_store 200`) comes after it is deployed
   in step 9.
8. On **detection** set (Sealed): `RR_STORE_SECRET_1` = Client ID, `RR_STORE_SECRET_2` = Client secret. **Required,
   not optional:** also set `RR_STORE_KILL_SWITCH_FILE` = `/tmp/rr-store.KILL` (not a secret). Without it the
   at-once stop below does nothing (nothing defaults it).
9. On **detection** set `RR_STORE_CONNECTIONS` (this is your approval; `shop` is the store's `*.myshopify.com` handle,
   not its custom domain; `client_id` is the id you will scan under):
   ```json
   {"version": 1, "connections": [{"client_id": "pca", "platform": "shopify",
     "shop": "<handle>.myshopify.com",
     "credential": {"kind": "client_credentials",
                    "client_id_file": "/run/zbm-secrets/rr-store-secret-1-file",
                    "client_secret_file": "/run/zbm-secrets/rr-store-secret-2-file"},
     "approval": {"approved_by": "andre", "approved_on": "<YYYY-MM-DD>", "approval_ref": "pca-rr-test-1"},
     "budget": {"syncs_per_day": 4, "max_orders": 1000, "lookback_days": 60, "snapshot_max_age_hours": 24}}]}
   ```
   Deploy detection. Its log must show it listening, and no `REFUSING TO START` line. Then run the per-department
   check on detection ("Changing `LEDGER_CALLERS`", step 8): `rr_store 200`. A `401` there means detection's token
   is not the one you gave the script: fix detection's `DETECTION_LEDGER_TOKEN`, not the ledger.
10. Open a tunnel to detection (as for seo above) and leave it running:
    `ssh -N -L 127.0.0.1:18000:127.0.0.1:8000 <detection-service-instance-id>@ssh.railway.com`.
    Put `Authorization: Bearer <ZBM_SERVICE_TOKEN>` in a file only you can read (`chmod 600`), e.g. `~/zbm/det.hdr`,
    so the token is never on a command line.
11. Status (no outbound call): `curl -sS -H @$HOME/zbm/det.hdr http://127.0.0.1:18000/stores/pca/status` →
    `"connected": true`, `"kill_switch": false`.
12. Sync (the only call that reaches Shopify): `curl -sS -X POST -H @$HOME/zbm/det.hdr -H 'Content-Type:
    application/json' -d '{"client_id": "pca"}' http://127.0.0.1:18000/stores/sync` → 200 with `snapshot_id`,
    `orders_read`, `orders_analysed`. A reason code instead: `LEDGER_UNAVAILABLE` (step 6/7), `STORE_AUTH_REFUSED`
    (step 4/5; the store is not in the app's organization: the fallback after step 5), `STORE_SCOPE_MISSING` / `STORE_TOKEN_EXCESS_SCOPE` (step 2), `STORE_CONFIG_INVALID` (step 9 JSON),
    `STORE_CREDENTIAL_UNREADABLE` (step 8).
13. Scan, within 24 hours of the sync and with no detection redeploy in between (the snapshot lives in memory):
    with `Authorization: Bearer <ORCHESTRATOR_SERVICE_TOKEN>` in `~/zbm/orch.hdr`,
    `curl -sS -X POST -H @$HOME/zbm/orch.hdr "https://<orchestrator domain>/revenue-recovery/scan?client_id=pca" -o
    ~/zbm/scan-pca.json`.
14. Read it: the dashboard's Revenue Recovery view (findings recorded on the ledger), and the client-style report
    from a checkout of the commit detection runs: `python3 services/detection-py/devtools/rr_report.py
    ~/zbm/scan-pca.json --md ~/zbm/report-pca.md`. Review before anyone else sees it.

What the first run can show: only the discount-misuse check is fed by Shopify orders (seven agents are NOT COVERED by
this source), and orders with automatic discounts or refunds are left out (ADR 0020 Limitations). A store with few or
no orders in the window gives few or no findings; the run still proves the path (ledger records, scopes, snapshot).

**Stop / disconnect.** At once: `railway ssh` into detection, `touch /tmp/rr-store.KILL` (gone after a restart).
Durable: `RR_STORE_READS_DISABLED=1` on detection (redeploys it). Disconnect: delete `RR_STORE_CONNECTIONS`,
`RR_STORE_SECRET_1`, `RR_STORE_SECRET_2` (redeploys detection; snapshots are dropped), then uninstall the app in the
store or rotate its client secret in the Dev Dashboard.

## Smoke-test checklist

Test mode, after step 11. `$ORCH` = the orchestrator's domain, `$GW` = the stripe-gateway's domain.

- [ ] Every service is Active and passed its healthcheck (`/health`).
- [ ] ledger log: `ledger_created` on the first deploy, the callers listed (orchestrator-go, finance-py, legal-py,
      seo-py, detection-py, compliance-py, each with its one department), no shared-token warning.
- [ ] finance, legal, seo logs: no `REFUSING TO START` / `refusing to start`.
- [ ] SEO report delivery path: open the seo tunnel (steps 1-2 of "Delivering an SEO client report from Railway"),
      `curl -sS http://127.0.0.1:18500/health` → 200 `{"status":"ok"}`, then close it.
- [ ] `curl -sS https://$ORCH/health` → 200 `{"status":"ok","service":"orchestrator-go"}`.
- [ ] `curl -sS -o /dev/null -w '%{http_code}\n' https://$ORCH/revenue-recovery/findings` → `401` (no token).
- [ ] `curl -sS -H "Authorization: Bearer <ORCHESTRATOR_SERVICE_TOKEN>" https://$ORCH/revenue-recovery/findings` → 200,
      `"ledger_verify": {"valid": true, ...}`.
- [ ] `curl -sS -X POST -H "Authorization: Bearer <ORCHESTRATOR_SERVICE_TOKEN>" https://$ORCH/revenue-recovery/scan` →
      `403` with `"reason":"FIXTURE_SCAN_DISABLED"` (a fixture scan is refused before anything is written; any other
      answer means `ORCHESTRATOR_ALLOW_FIXTURE_SCANS` is set: remove it and redeploy). Never run a scan with a
      `client_id` against production as a test: the ledger is append-only.
- [ ] `curl -sS https://$GW/health` → 200.
- [ ] `curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://$GW/webhooks/stripe -d '{}'` → `400` (no signature).
- [ ] Stripe webhook, **no money**: resend one existing **`checkout.session.*`** event, and only that type, to this
      endpoint: `stripe events resend evt_… --webhook-endpoint we_…` (the `we_…` id is on the endpoint's page; the
      flag syntax is unverified, so the fallback is the **Resend** button on that event under the endpoint's Event
      deliveries) → the delivery answers `200` `{"result":"received"}`. Finance answers it `unknown_session`: it reads
      the session and its PaymentIntent back with the restricted key and writes only a `stripe_event_applied` evidence
      event, no journal entry and no break. (A `400` means the Finance rules are not approved yet or Finance refused the
      signature: re-check `FIN_STRIPE_WEBHOOK_SECRET`. A `503` means Finance could not read Stripe back or was
      unreachable: check finance's log and the key's permissions.) If the sandbox has no `checkout.session.*` event
      yet, skip this line; the unsigned `400` check above and finance's `/health` below still prove the wiring.
      **Never run `stripe trigger` (or resend a `payment_intent.*` / `charge.*` event) against the sandbox production
      uses.** Finance has one journal per volume and no test/live split: once the Finance rules are approved, a
      triggered `payment_intent.succeeded` has no Finance invoice, so it books unapplied cash into the production
      books for good (Dr 1060 / Cr 2070, the fee, and an open `unapplied_cash` break that blocks payouts), and the
      ledger is append-only. Every enabled endpoint in a sandbox receives that sandbox's events, so a `stripe trigger`
      that should exercise the whole payment path belongs in a staging environment wired to its **own** separate
      Stripe sandbox, with the Stripe CLI logged in to that sandbox.
- [ ] finance `/health` from inside (`railway ssh` → `python3 -c "import urllib.request as u;
      print(u.urlopen('http://127.0.0.1:8410/health').read())"`): `"in_memory": false`,
      `"stripe_incoming": {"wired": true, "livemode": false}`.
- [ ] Dashboard: the Vercel URL shows the login page; the password logs in; the findings page renders (HTTP 200);
      `/healthz` answers 200 once logged in.
- [ ] Redeploy `ledger`: it comes back Active, its log says it loaded the existing entries with the chain verified, and
      the dashboard still loads.
- [ ] Volume backups: each of the four volumes → Backups shows the Daily schedule from the IaC.

## Open items and risks

- **Docker builds: first run in CI on golive-combined.** The build box has no Docker daemon and its egress proxy blocks
  every container registry. CI runs 40147816673 and 40149787508 built all seven images (step "Build every Railway
  image" green); the smoke then failed only the four Python images' IPv4 health checks (next item). What was verified
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
- **IPv6 bind: dual-stack (fixed on golive-combined).** The first `railway-images` run (CI 40147816673 / 40149787508)
  showed all four Python images listening on `[::]` and refusing every IPv4 connection (`ConnectionResetError`):
  uvicorn binds through asyncio's `create_server`, which sets `IPV6_V6ONLY` on the sockets it binds itself, while the
  Go and Rust services bind `[::]` dual-stack and passed. On Railway the same IPv6-only socket would fail any IPv4
  peer: Railway's docs say private DNS in environments created after Oct 16 2025 resolves to IPv4 and IPv6 (legacy
  environments: IPv6 only), and do not state the health check's address family (a Railway community report shows
  uvicorn on `::` failing the health check where `0.0.0.0` passed). The launchers now bind `::` themselves with
  `IPV6_V6ONLY` cleared (`src/dual_stack.py`, the same file in the four Railway Python services; only the IPv6
  wildcard takes the new path) and hand uvicorn that socket, so one socket serves IPv4 and IPv6. This box's kernel has no IPv6; the CI job
  runs the production values. If a service on Railway fails with "Address family not supported", set its bind
  variable to `0.0.0.0`.
- **Railway behaviours taken from the docs or the SDK types, not observed:** `preserve()` on a variable that does not
  exist yet (first apply); `${{service.VARIABLE}}` interpolation inside the two JSON maps, including the hyphen in
  `stripe-gateway`, and the same-service `${{VARIABLE}}` form for the two `onboarding` entries (and the reference
  resolving to a Sealed variable); `requiredMountPath` (an SDK field, not described in the docs); `RAILWAY_RUN_UID=0`;
  `checkSuites: true` (wait for CI); `railway ssh` sessions having the service's variables; the CLI applying
  `backupSchedules` from a volume attachment (the SDK types it and emits it in the graph; checked by evaluating the
  file, not by a plan).
- **stripe-gateway private-network opt-in** (`STRIPE_GATEWAY_ALLOW_RAILWAY_PRIVATE_HTTP`) relaxes AEGIS launch-hardening
  L8 for one named host on the private network: `http://<STRIPE_GATEWAY_FINANCE_PRIVATE_NAME>.railway.internal[:port]`
  (IaC: `finance`; AEGIS L1). It needs AEGIS review before merge.
- **Two public services.** The orchestrator's token has no minimum-length check and no rate limit, so its entropy is
  the only defence (use `openssl rand -hex 32`). Anyone holding it can also start a scan of a connected
  tenant; a fixture scan is refused unless `ORCHESTRATOR_ALLOW_FIXTURE_SCANS=1` (never set here; L-2).
- **fin-cron runs Finance's housekeeping only** (ADR 0026): the hourly Stripe-session sweep and hold re-check, client
  mail, the daily reconciliation. Legal's daily jobs and seo's schedule tick still need a `scheduler` caller (and a
  cron entry) of their own; until then they run only when you call them. Railway's cron behaviour (minute-17 schedule
  in UTC, Restart policy Never for a cron service) is from Railway's documentation, not yet observed on this project.
- **Detection's store connector on Railway: wired, off until Andre connects a store** (AEGIS A2 H-2). The image maps
  four secret files and detection runs with `RAILWAY_RUN_UID=0` (root only in the entrypoint); the IaC sets
  `DETECTION_LEDGER_URL`. Not yet observed on Railway: the first real sync is the first end-to-end check of the
  connector against Shopify (ADR 0020 Limitations), and the detection secret-file start is checked by running the
  entrypoint locally, not by the CI smoke (which starts detection as uid 10001 with no store values). Residual risks:
  `GET /revenue-recovery/findings` returns every client's findings (AEGIS A2 M-1), so the dashboard must stay
  Andre-only; one store at a time (two credential slots); a connections or credential change redeploys detection and
  drops held snapshots; the per-day sync budget resets on restart (in memory, ADR 0020); the approval is the
  `RR_STORE_CONNECTIONS` variable itself, so anyone who can edit detection's variables can register a store.
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
  - the ledger smoke authenticates with the shared `LEDGER_SERVICE_TOKEN`; the production auth mode (`LEDGER_CALLERS`
    only, written to `LEDGER_CALLERS_FILE`, no shared token, per-caller department scopes) is never smoked;
  - no cross-service flow runs in CI (orchestrator → detection → ledger, stripe-gateway → finance); it was run once by
    hand on the build box without Docker;
  - `devtools/test_deploy_config.py` reads `.railway/railway.ts` as text; CI does not evaluate the file with the
    pinned SDK (it was evaluated by hand with `node --experimental-strip-types` and type-checked with `tsc --strict`).
- **Startup order** is enforced only by retries: a department that exhausted its retries before the ledger was up stays
  down until redeployed.
