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

## Amendment — AEGIS round 1 (Oct 5 2026): BLOCKING, every finding fixed

Round 1 found no path to a secret value for anyone not entitled and no approval bypass. It blocked on one High.

| Id | Finding | Fix |
|---|---|---|
| H1 | A lost ledger answer (the ledger may have recorded the anchor) dropped the pending line: the ledger then held an anchor the log never would, and every later check failed for ever | The same anchor is retried once (the ledger is idempotent on identical content); if the answer is still unknown, the pending line is KEPT and writes stop until the next integrity check appends it (anchor present) or discards it (absent). In memory too. |
| M1 | A retried clean exit skipped a secret (sub-requests were keyed by list position) | Keyed by the secret id; the answer counts every secret the request destroyed |
| M2 | A department could see and destroy its own canary | A department's status, rotate or destroy on a canary is D3 (caller frozen, sev1), answered 404 |
| M3 | Holds, scans and jobs ignored freezes | Every non-dashboard caller is checked; the `integrity` job stays available in a lockdown |
| M4 | 64 open challenges blocked every approval | FREEZE and LIFT_FREEZE have their own pool; a full pool is refused `APPROVAL_CHALLENGES_EXHAUSTED` and opens a sev2 |
| M5 | A chosen recovery token could be guessed from its exported hash | Tokens must be generated (43+ characters, 20+ different); token hashes never appear in the audit export |
| M6 | Rotating a held secret destroyed the preserved version | Refused `PRESERVATION_HOLD` |
| L1, L2 | Request ids were shared across targets and actions | Request keys are operation + target + request id |
| L3 | A mismatched approval did not burn its challenge | Any presented challenge is burnt |
| L4 | Alerts could be sent with the lock held | Detections only queue; a middleware sends after every request, outside the lock |
| L5 | A pending line over 1 MiB could not be read back | Read cap 16 MiB |
| L6 | Any caller could mint any scope; verify ignored a lockdown | Scopes refused until a registry exists; `verify(..., lockdown=True)` refuses every token |
| L7 | Forced integrity checks could be triggered without limit | At most one full ledger read per 10 s |
| L8 | `/health` disclosed internals unauthenticated | `/health` answers `status` only; detail is `/sec/v1/status` (dashboard) |
| L9 | A failed route lost the passkey counter it had verified | The counter is kept in memory at once |
| T1 | No test presented a spent enroll token | Added |

Known and accepted: ledger-rust has no filtered read, so every integrity check reads the whole shared ledger (the
256 MiB read cap is shared with the other services; a filtered read is a ledger-rust change). A WebAuthn assertion
does not show Andre what he signs: the dashboard must display the action before the ceremony, and a compromised
dashboard can still ask him to sign the wrong thing (design limit, ADR unlock item 7).

## Amendment — AEGIS round 2 (Oct 5 2026): BLOCKING, every finding fixed

Round 2 confirmed M1–M3, M5, M6 and L1–L9 closed, and found that the round-1 H1 fix itself introduced a High.

| Id | Finding | Fix |
|---|---|---|
| N2 (High, introduced by the H1 fix) | On an unknown outcome the caller deleted the new sealed file, then the roll-forward recorded a secret whose value no longer existed (a rotation could destroy a live credential) | An unknown outcome is marked `maybe`; the sealed file is kept, and removed only if the line is discarded. Orphan removal never runs while a pending line exists. |
| N1 (High) | An anchor still in flight could land after the pending line was discarded and its seq reused | The pending line is never discarded when it is the exact next line: its identical anchor is re-recorded (the ledger answers 200 whether or not it already holds it) and the line appended. Only a stale or unparsable line is discarded. |
| N3 (Medium; M4 not closed) | 8 FREEZE challenges blocked the freeze switch | The emergency pool (FREEZE, LIFT_FREEZE) never refuses: 256 slots, the oldest evicted, a sev2 opened |
| N4 (Low) | The integrity job could report a stale OK inside the rate-limit window | The scheduler's job always reads the ledger. An integrity failure cannot be logged as an incident (the log is what failed), so Andre is alerted directly, once until the log is healthy, with a best-effort ledger record |
| N5 (Low) | A replayed clean exit destroyed a secret stored after it | The first execution records the plan (which secrets); a replay finishes only those |
| N6 (Low) | An unparsable pending line; the alert middleware skipped on an unhandled error | Discarded through the guarded path; the middleware flushes in `finally` |
| T1 (round 2) | The consumed-token guard inside `enroll()` had no test | Added |

## Amendment — AEGIS round 3 (Oct 5 2026): BLOCKING, every finding fixed

Round 3 confirmed N1, N2 (maybe path), N4–N6 and T1 closed.

| Id | Finding | Fix |
|---|---|---|
| R3-1 (High, from the round-2 roll-forward) | A failure reported as certain (pending file placed but its write unconfirmed; or the anchor refused but the pending file not removable) let the caller delete the new sealed file, and the roll-forward then pointed the secret at it | A failure is "certain" only when the pending file is certainly gone; otherwise it is "maybe" and the sealed file is kept. An anchored line is always rolled forward (discarding it would leave the ledger ahead of the log); if its sealed file is missing anyway (removed from outside), a sev1 `SEALED_SECRET_TAMPERED` is opened once the log is verified and the secret's other versions are kept for a manual recovery |
| R3-3 (High, present since round 1) | An fsync error after the write left the line in the file but not in memory; the roll-forward wrote it twice and the next start refused | The log file must equal memory before a write (else refused); a failed write is cut back; a line already on disk is adopted, never written twice |
| R3-2 (Medium; N3 not closed) | A flooder evicted Andre's FREEZE challenge in about a second | FREEZE and LIFT_FREEZE challenges hold no server state: nonce, expiry and an HMAC (per-process key) over the action hash. Nothing to fill or evict; single use is kept by remembering only challenges that approved something, until they expire |
| R3-4 (Low) | A failed integrity alert was never sent again while the log was unhealthy | Deduplicated only after a channel reports `delivered` |

## Amendment — AEGIS round 4 (Oct 5 2026): BLOCKING, every finding fixed

Round 4 confirmed R3-1 (a and b), R3-2, R3-3 and R3-4 closed.

| Id | Finding | Fix |
|---|---|---|
| R4-1 (High, since round 2) | A forged `pending.line` (write access to the data directory only) was anchored by the service itself and applied: e.g. an attacker's passkey enrolled | Only a line THIS process wrote is anchored by it, and the trusted copy is the one kept in memory, never the file. A line found on disk at start is appended only if the ledger ALREADY holds its anchor (decision 7 restored); otherwise it is set aside (`pending.discarded`), inert, and appended later only if its anchor appears (it was in flight when the process stopped); it is dropped once the log has moved past it |
| R4-2 (Medium, from R3-3) | A blank line in the log stopped every write, restart included | A blank line refuses start with a message naming it; `verify()` reports it |
| R4-3 (Low) | A failed FREEZE attempt did not use its challenge up | A MAC-valid attempt uses it up, whatever happens next |
| Info | A failed delete of the old version after a recorded rotation answered 500 | Guarded: the rotation stands; the orphan is removed at the next start |

Residual (accepted, fail-closed): if the process stops while an anchor is in flight AND a new line is committed
before that anchor lands, the ledger then holds two anchors for one sequence number; the integrity check reports it
(sev1, writes stop) and an operator reconciles. The ledger's `department` field is self-declared by any holder of the
ledger token, so a service holding that token could forge a `cybersecurity` anchor; per-department ledger tokens are
a ledger-rust change (unlock list).

## Amendment — AEGIS round 5 (Oct 5 2026): NOT BLOCKING

Round 5 confirmed R4-1, R4-2 and R4-3 closed and found no Critical or High. One Medium, fixed before merge:

| Id | Finding | Fix |
|---|---|---|
| R5-1 (Medium, from R4-1) | A failed unlink after a successful commit left the in-memory own line set, and every later check failed on it until a restart | The own line is cleared as soon as it is in the log; an own line that is no longer the next line falls through to the file path (set aside as stale) |

Accepted trade-off (Info): anyone who sees a freeze challenge id on the request path can use it up with a bad
signature; Andre asks for a new one. Ids are HMAC-protected and cannot be guessed.

## Amendment — AEGIS sweep A (Oct 6 2026, on 5d49ee9): every finding fixed

Regressions: `services/security-py/tests/test_sweep_fixes.py` (each one fails on 5d49ee9).

| Id | Finding | Fix |
|---|---|---|
| Sweep-A release order | `release_hold` released every preserved system externally FIRST, under the lock, then committed: a ledger failure answered 503 with the data no longer preserved and the hold still `active` | The release is committed first (`hold_released` carries `release_pending`, the preserved systems). Then each is released externally, outside the lock, retried until the adapter confirms: 3 tries in the request, then the new `hold-release-retry` job; each confirmation is its own line (`hold_release_confirmed`). Pending releases survive a restart. The answer of a released hold names `release_pending` |
| Sweep-A adapter exceptions | An exception from a preservation adapter in `preserve` was a 500 | It is a `failed` preservation: the hold is recorded, the answer says `delivered: false` with the failed systems in `reason`, and the PRESERVATION_NOT_CONNECTED incident is opened as for an unconnected system. An adapter release that raises is unconfirmed and retried |
| Sweep-A clock | Emergency (freeze) challenges used `time.time()`, and the test harness ran on the system clock unless a test passed one | Emergency challenges (issue, check, used-set expiry) use `self.clock`. The test harness defaults to a `FixedClock` (2026-10-06 12:00 UTC); the tests that read the wall clock read the harness clock, and the expired-freeze-challenge test advances it instead of monkeypatching `time.time` |
