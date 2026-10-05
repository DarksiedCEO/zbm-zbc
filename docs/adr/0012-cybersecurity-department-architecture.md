# ADR 0012 — Cybersecurity (22): vault, passkey approvals, service identity, freezes, incidents, findings

Status: accepted for build, Oct 5 2026 (founder Q&A the same day). Not in force: no key service for production is
wired (hosting not chosen), no alert provider is chosen, no department calls it yet.

## Founder decisions (Q&A, Oct 5 2026)

| Question | Answer |
|---|---|
| Where the backend runs (decides where the master key lives) | **Not decided yet.** Build the key service as a port; a local key file for tests only. |
| Who approves sensitive actions | **Andre only, with a passkey / security key** (no passwords, no text-message codes). |
| Scope beyond the vault | **All four:** service sign-in and access rules; freeze switch and incident response; audit log and intrusion alerts; vulnerability and dependency scanning. |
| How Andre is alerted | **Text message, email and phone push** (all three). |

## Decisions

1. **One service, `services/security-py`** (Python, the stack of the other departments; FastAPI, pydantic strict,
   `cryptography` 50.0.1 for AES-GCM, Ed25519, ECDSA, RSA). Ledger department name `cybersecurity`, event ids
   `sc-<abbr>-<40 hex>`, port 8440, `X-SEC-Caller-Token`.
2. **Callers.** Fourteen known names (`config.KNOWN_CALLERS`), each with its own bootstrap caller token
   (`SEC_CALLER_TOKENS`). `dashboard` is Andre's console backend: it relays passkey ceremonies and Andre's
   requests, and is never Andre by itself and can never read a secret value.
3. **Envelope encryption.** Each secret version has its own random 256-bit data key; the value is sealed with
   AES-256-GCM; the data key is wrapped by the key service. Both layers bind the context (secret id, version, owner,
   kind) as associated data, so a sealed file moved to another record does not open.
4. **Key service port.** `NotWiredKeyService` (default: 503 `VAULT_UNAVAILABLE`), `LocalFileKeyService` (a 32-byte
   key from a 0600 file, allowed only with `SEC_NON_PRODUCTION=1`). `SEC_KMS=aws|gcp` refuses to start: not built
   until Andre picks the host.
5. **What the vault never does:** return a value to anyone but a listed reader for a listed purpose; put a value in
   the log, the ledger, an error, the audit export or an alert; release a canary; release its own signing keys.
6. **Record-first.** Every state change: prepare the exact log line, fsync it aside (`pending.line`), anchor it on the
   ledger (`log_anchor`), append it, then apply it in memory with the same `_apply` replay uses. Every release
   (`secret_released`) and every credential issued (`credential_issued`) is recorded on the ledger BEFORE it is
   returned; ledger down = nothing released.
7. **Crash window closed.** At start a pending line whose anchor is on the ledger and which is the next line is
   appended; otherwise it is discarded.
8. **Integrity against the ledger.** At start and before every write: every local line has its anchor, and the
   ledger holds no anchor this log lacks and none from another log epoch. A truncated, rolled-back, replaced or
   deleted log stops all writes and releases (503 `INTEGRITY_UNVERIFIED`); a lost in-memory log cannot reopen
   passkey enrolment.
9. **Data directory.** Required in production (`SEC_DATA_DIR`, owned by the service user, mode 0700); one process per
   directory (`flock`); sealed files `sealed/<secret_id>.v<n>` written before their log line, orphans removed at
   start; destroy overwrites and unlinks the file (the wrapped data key exists nowhere else).
10. **Approvals are passkeys (WebAuthn Level 2).** Every Andre action carries an assertion over a single-use
    challenge (5 minutes) bound to the SHA-256 of exactly the action, target and normalised body it approves. A
    changed body, another action, a reused or expired challenge, a failed attempt (the challenge is burnt) are all
    refused.
11. **Assertion checks:** type `webauthn.get`, our challenge, an allowed origin, not cross-origin, RP id hash, user
    present AND user verified, the signature over `authData || SHA-256(clientData)`, and the counter.
12. **Clone detection.** When either counter is non-zero the new one must be greater; otherwise the passkey is
    suspended and a sev1 incident opened. Passkeys that report 0 always (synced passkeys) are accepted.
13. **Registration:** attestation `none` only (enrolment is secured by how it is authorised), UV required, ES256,
    EdDSA or RS256 (2048-bit or more). A strict bounded CBOR decoder (`cbor.py`, stdlib) instead of a new
    dependency.
14. **Enrolment.** The first passkey needs the one-time enroll token (`SEC_ANDRE_ENROLL_TOKEN_FILE`); every later
    passkey needs an approval from an existing one; the last passkey cannot be revoked. `/sec/v1/status` warns until
    two are enrolled.
15. **Recovery (lost every passkey).** The operator restarts with a NEW enroll token file and
    `SEC_PASSKEY_RECOVERY=1`: every passkey is revoked, a sev1 incident is opened (all alert channels), and the new
    token enrols one passkey. Restarting with the same token does not reset again.
16. **Store.** A department stores secrets it owns; it may name itself as the only reader or no one (write-only,
    Onboarding's pattern). Only Andre grants another department (`/secrets/{ref}/access`) or stores for one.
    `hmac_key` and `canary` values are generated inside, never supplied.
17. **Use.** `POST /sec/v1/secrets/{ref}/use` with a purpose. A stranger gets 404, never a confirmation the secret
    exists. Release rate per caller is capped (`SEC_RELEASE_RATE_PER_MIN`, default 60).
18. **Service identity.** A caller exchanges its bootstrap token for a JWT (EdDSA) naming one audience, at most 15
    minutes (`SEC_TOKEN_TTL_SECONDS`, default 10). Verifiers fetch `/identity/jwks` and `/identity/denylist`.
19. **Signing keys** are generated inside, sealed in the vault under owner `cybersecurity` (never releasable, never
    listed), rotated every 30 days by the scheduler with a 24-hour overlap, then destroyed.
20. **`tokens.verify`** is written to be copied into the other services when they are wired: exact header, known kid,
    signature, iss, aud, sub, exact claim set, integer times, TTL cap, skew 60 s, deny list.
21. The bootstrap tokens stay until each department is moved to minted credentials (unlock list).
22. **Freezes.** Andre freezes a caller, a secret, or everything (`all` = lockdown), each with a passkey, and lifts
    them the same way. A frozen caller can do nothing here and is on the deny list.
23. **Legal holds.** Legal (37) asks us to preserve `email`, `chat` and `drive` for a hold. No such system is
    connected yet, so the answer is `delivered: false` with the systems named — never a false "frozen". A hold on
    `client:<id>` or a subject ref blocks destroying the matching secrets.
24. **Incidents.** sev1..sev4; open, note, close (close needs a passkey and a root-cause code). The same detection
    on the same subject while open is one incident.
25. **Alerts.** sev1: text, email, push; sev2: text, push; sev3: email; sev4: none. An alert carries codes and ids
    only. No provider is chosen: each channel is `not_wired`, and `SEC_ALERT_*` set refuses start. Sending happens
    outside the lock; a provider error is a `failed` send, retried by `alerts-retry`.
26. **Vulnerability findings.** The scheduler (or dashboard) posts each scanner's results per source; findings
    open, stay or close by advisory, package and version. Deadlines: critical 7 days, high and unknown 30, medium
    90, low 180. A critical opens a sev2 incident; overdue findings open incidents. Andre may accept a risk for at
    most 90 days, with a passkey. The scanners themselves run in CI (unlock list), not in this service.
27. **Detections:** D1 20 authentication failures in 5 minutes (sev2); D2 3 refusals of one caller in 10 minutes
    (caller frozen, sev2); D3 a canary touched (caller frozen, sev1); D4 3 failed approvals in 10 minutes (sev2);
    D5 a passkey counter regression (sev1); D6 a sealed file that does not open (sev1); D7 a failed integrity
    check (sev1); release rate exceeded (sev3).
28. **Compliance C-14.** `compliance-report` pushes pass/fail to `POST /compliance/v1/controls/C-14/results` as
    `cybersecurity_22`: pass only if every scan source is at most 8 days old, nothing is overdue, a passkey is
    enrolled and no sev1 is open. Evidence is hashes and `urn:sec22:` refs.
29. Every response is `Cache-Control: no-store`; /docs and /openapi.json are off; the shared request limits,
    hardened launcher and graceful close of the other Python services apply.

## How the other departments use it (not wired yet)

| Department | Expects (its port today) | Here |
|---|---|---|
| Verification & Integrity | `TokenVault.identity_hmac_key`, `with_token`, `exchange_and_store`, `destroy`, `authorization_url` | `hmac_key` secret + `/use`; store / destroy. OAuth exchange inside the vault is not built (unlock list). |
| Finance (31) | `VaultPort.identity_hmac_key`, `contact_ref_valid(vault:…)`, rail and bank credentials | `hmac_key` + `/use`; `GET /secrets/{ref}` answers whether a ref exists for that caller; Stripe keys move from the interim 0600 files into the vault. |
| Legal (37) | `Cybersecurity22Port.freeze(hold_id, systems, subject_refs) -> Delivery` | `POST /sec/v1/holds` answers `delivered`, `reason`, `reference`. |
| Client Delivery (28) | `Vault.secret(ref)` for `vault:` refs | `/use` with its purpose. |
| Onboarding | `SecretsVault.store`, `destroy_all(client_id)`, no read | write-only store; `/clients/{id}/destroy`. |
| Compliance (38) | C-14 results from `cybersecurity_22` | `compliance-report` job. |

## Not built (unlock list)

1. A production key service (AWS KMS or Google Cloud KMS adapter) — after Andre picks the host.
2. Alert providers for text, email and push — after Andre picks them.
3. Each consumer's thin client to this service, and moving each from bootstrap tokens to minted credentials.
4. The OAuth code exchange inside the vault for Verification & Integrity.
5. Preservation adapters for email, chat and drive.
6. Scanner jobs in CI that post to `/sec/v1/scans` (pip-audit, govulncheck, cargo audit, npm audit).
7. The dashboard pages that run the passkey ceremonies.

## Settings

All settings are in `services/security-py/README.md`.
