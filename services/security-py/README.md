# security-py — Cybersecurity department (22)

Holds ZBM/ZBC's secrets (the vault), issues short-lived service credentials, lets Andre freeze a caller, a secret
or everything, answers Legal's preservation holds, runs incidents and alerts, and tracks vulnerability findings to
their deadlines. Andre approves every sensitive action with a passkey or security key; nothing else is Andre.

Architecture, founder decisions and the unlock list: `docs/adr/0012-cybersecurity-department-architecture.md`.

**Status:** built and tested; not in force. No production key service is wired (hosting not chosen), no alert
provider is chosen, and no department calls this service yet.

## Run

```bash
cd services/security-py && python3 -m pytest -q          # no network; counts: docs/test-counts.md
export SEC_SERVICE_TOKEN=<secret>                        # required: refuses to start without it
export SEC_CALLER_TOKENS='{"finance_31": "<>=32 printable chars>", "legal_37": "...", "dashboard": "...", "scheduler": "..."}'
export SEC_DATA_DIR=/var/lib/zbm/security               # required unless SEC_NON_PRODUCTION=1; owned by the service user, 0700
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret>
export SEC_WEBAUTHN_RP_ID=zbestmedia.com SEC_WEBAUTHN_ORIGINS=https://console.zbestmedia.com
export SEC_ANDRE_ENROLL_TOKEN_FILE=/etc/zbm/security/enroll.token   # 0600; enrols Andre's first passkey once
cd src && python3 -m api                                 # SEC_BIND_ADDR (127.0.0.1), SEC_PORT (8440)
```

Headers: `Authorization: Bearer <service token>` on every route but `/health`; `X-SEC-Caller-Token` everywhere
else. Andre's actions go through the `dashboard` caller and carry an `approval` (a passkey assertion) in the body.

| Setting | Default | Notes |
|---|---|---|
| `SEC_SERVICE_TOKEN` | — | required, 32..512 printable characters |
| `SEC_CALLER_TOKENS` | `{}` | JSON `{caller: token}`; callers listed in `src/config.py` `KNOWN_CALLERS`; all distinct |
| `SEC_NON_PRODUCTION` | 0 | 1 allows the in-memory store, `SEC_KMS=local_file` and an `http://localhost` origin. Never in production |
| `SEC_DATA_DIR` | — | required in production |
| `SEC_KMS` | `none` | `none` (vault answers 503) or `local_file` (non-production only); `aws` / `gcp` refuse to start: not built |
| `SEC_LOCAL_MASTER_KEY_FILE` | — | with `SEC_KMS=local_file`: 32 random bytes, base64, file mode 0600, not a symlink |
| `SEC_WEBAUTHN_RP_ID`, `SEC_WEBAUTHN_ORIGINS` | — | a pair; unset = nothing can be approved |
| `SEC_ANDRE_ENROLL_TOKEN_FILE` | — | one-time token for the first passkey (or for recovery) |
| `SEC_PASSKEY_RECOVERY` | 0 | 1 with a NEW enroll token: revokes every passkey once (sev1 alert) |
| `SEC_COMPLIANCE_URL` / `_TOKEN` / `_CALLER_TOKEN` | — | all three or none; none = C-14 results stay `unavailable` |
| `SEC_RELEASE_RATE_PER_MIN` | 60 | releases per caller per minute (1..600) |
| `SEC_TOKEN_TTL_SECONDS` | 600 | service credential lifetime (60..900) |
| `SEC_ALERT_SMS`, `SEC_ALERT_EMAIL`, `SEC_ALERT_PUSH` | unset | not built: setting one refuses start |
| `SEC_BIND_ADDR`, `SEC_PORT` | 127.0.0.1, 8440 | |
| `SEC_REQUEST_HEAD_TIMEOUT_SECONDS`, `SEC_KEEP_ALIVE_TIMEOUT_SECONDS`, `SEC_LIMIT_CONCURRENCY`, `SEC_SWITCH_INTERVAL_SECONDS`, `SEC_DRAINS_MAX` | 10, 5, 128, 0.001, 512 | launcher tuning (`src/serve.py`, shared with the other Python services) |

## Routes

| Route | Who | Purpose |
|---|---|---|
| `GET /health` | open | status, vault available, in memory, integrity |
| `GET /sec/v1/status` | dashboard | full health: passkeys, lockdown, channels, key service |
| `POST /sec/v1/approvals/challenges` | dashboard | a challenge bound to one action, target and body |
| `POST /sec/v1/passkeys/enroll/options`, `/enroll`; `GET /passkeys`; `POST /passkeys/{id}/revoke` | dashboard | passkeys |
| `POST /sec/v1/secrets` | a department | store its own secret (itself or no one as reader) |
| `POST /sec/v1/secrets/andre`; `POST /secrets/{ref}/access` | dashboard + passkey | store for a department; grant readers |
| `GET /sec/v1/secrets`, `/secrets/{ref}` | dashboard; owner or reader | metadata only |
| `POST /sec/v1/secrets/{ref}/use` | a listed reader | the value, for a listed purpose (recorded first) |
| `POST /sec/v1/secrets/{ref}/rotate`, `/destroy` | owner; or dashboard + passkey | |
| `POST /sec/v1/clients/{client_id}/destroy` | a department | its secrets for that client (holds respected) |
| `POST /sec/v1/identity/tokens`; `GET /identity/jwks`, `/identity/denylist` | any caller | service credentials |
| `POST /sec/v1/freezes`, `/freezes/{id}/lift`; `GET /freezes` | dashboard + passkey | freeze switch |
| `POST /sec/v1/holds`, `/holds/{id}/release` | legal_37 | preservation holds |
| `POST /sec/v1/incidents`, `/{id}/notes`, `/{id}/close`; `GET /incidents` | dashboard (close: passkey) | incidents |
| `POST /sec/v1/scans`; `GET /findings`; `POST /findings/{id}/accept` | scheduler or dashboard; dashboard, compliance_38; passkey | findings |
| `POST /sec/v1/jobs/{rotate-signing-key,compliance-report,rotation-due,findings-due,alerts-retry,integrity}/run` | scheduler | |
| `GET /sec/v1/audit/integrity`, `/audit/events`, `/audit/access` | dashboard (integrity: compliance_38 too) | audit |

## First passkey

1. Generate a token: `python3 -c "import secrets; print(secrets.token_urlsafe(48))" > enroll.token && chmod 600 enroll.token`.
2. Start the service with `SEC_ANDRE_ENROLL_TOKEN_FILE` pointing at it.
3. In the console, enrol with the token. Then enrol a second key (the console asks the first to approve it) and
   keep it somewhere safe. The token is spent once a passkey exists.

Lost every key: see ADR 0012 decision 15 (recovery).
