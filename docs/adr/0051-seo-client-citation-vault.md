# ADR 0051 - SEO (Dept 2): client citation vault (Option B)

Number to be confirmed at merge (0051 is the next unused number in `docs/adr` on this branch; open PRs elsewhere may
already claim it). Status: job 1 (the module) and job 2 (the wiring, below) built Nov 9 2026 on
`claude/seo-callum-engines-20261108`. **Not in force** until merged and a key is set with Andre's approval. Fixtures
and synthetic data only: no real key exists, no client data has been stored. Extends ADR 0046 (Callum's citation
sources) and sits beside ADR 0050 (the redaction core; job 2 changes only its fetch rule, decision 8).

## Context

Callum's citation runs (ADR 0046) see client citation evidence: the URL an AI answer cited, an excerpt and the page
title, for answers about the client's own brand. The owning client is entitled to that evidence in full. ADR 0050
withholds such text from the log and from storage unless it is clearly safe, so without somewhere else to keep it the
evidence is lost. Option B keeps it in full, encrypted per tenant, in a separate store, and the log keeps only an
opaque vault id. (Option A, storing it in the log after redaction, loses the evidence; Option C, a separate database
service, adds a dependency and a network hop the department does not otherwise need.)

## Decision

1. **Module.** `services/seo-py/src/vault.py`: `VaultKey`, `parse_vault_key`, `load_vault_key`, `CitationVault`.
   Dependencies: the standard library and `cryptography` (already pinned at 50.0.1). No new dependency.
2. **Key.** `SEO_VAULT_KEY` is base64url (padding optional) of exactly 32 random bytes, canonical encoding, at least
   16 distinct byte values; `SEO_VAULT_KEY_ID` is a short id (`[a-z0-9][a-z0-9-]{0,31}`). Both or neither; anything
   malformed raises `VaultKeyInvalid` with a message that names the variable and never quotes its value. Two
   sub-keys are derived with HKDF-SHA256 (distinct `info` labels): one for AES-256-GCM, one for the tenant tag; the
   raw key is not kept. The key objects follow `gsc_client.ServiceAccountKey`: `__slots__`, a repr that names only
   the key ids (`key=<redacted>`), pickling (every protocol) and copying refused with `TypeError`.
3. **Records.** `put(tenant_id, record)` takes a JSON object (strict JSON: no NaN, sorted keys), at most
   `MAX_RECORD_BYTES` = 262,144 bytes encoded (UTF-8); larger is refused unread. It is encrypted with AES-256-GCM
   under the current key, a fresh 12-byte nonce from `os.urandom` per record, and associated data
   `"seo-citation-vault/v1" NUL tenant_id NUL vault_id NUL key_id`. The vault id is 128 random bits
   (`secrets.token_bytes(16)`, returned as 32 hex characters).
4. **File.** `SEO_DATA_DIR/citation_vault.jsonl` (the same data directory as `seo_log.jsonl`), mode 0600, append
   only, one JSON line per record: `{"v":1,"id","t","k","n","c"}` = vault id, tenant tag (HMAC-SHA256 of the tenant id
   under the tag sub-key), key id, nonce, ciphertext; every value base64url without padding. No plaintext field: not
   the tenant id, not the URL, not the title. Each append follows `store.RecordLog`: the file must be exactly the
   size this process wrote (else refused), `pwrite` then `fsync`, the directory fsynced when the file is created, a
   failed or short write cut back to the old size (or every later write refuses). A torn tail, a symlink, a file not
   owned by the service user or open to group/others refuses to open. Malformed or duplicate lines are counted
   (`bad_lines`) and never served.
5. **Reads.** `get(tenant_id, vault_id)` returns the record only for the tenant that stored it. The tenant tag is
   compared (constant time) before any decryption, so a wrong tenant never reaches the key; the associated data
   binds tenant, id and key id a second time. A wrong tenant, an unknown or malformed id, a tampered line, an
   unknown key id and a failed decryption all raise the same `VaultNotFound("citation record not found")`, raised
   outside every `except` block (no chained context).
6. **Fail closed.** No key configured: `put` raises `VaultUnavailable` and writes nothing (the file is not even
   created); `get` raises `VaultUnavailable`. There is never a plaintext fallback.
7. **Concurrency.** One re-entrant lock (`threading.RLock`, as `service.Service.lock` and `RecordLog.lock`) around
   the index and the append; one process per data directory is already guaranteed by the data-directory flock.

## Job 2: the wiring (svc_citations.py, agents/callum.py, service.py, config.py)

8. **Vault in the service.** `SeoService.vault`: `CitationVault(SEO_DATA_DIR, key)` with a data directory;
   `MemoryCitationVault` (same lines, same encryption, no file) without one (in-memory, non-production), under the
   configured key or, if none, a random key held in memory only. The key also comes from `SEO_VAULT_KEY_FILE` /
   `SEO_VAULT_KEY_PREVIOUS_FILE` (the service's *_FILE rules; a file and its variable both set refuses start).
9. **Fail closed.** A data directory without a key: a citation run is closed `RUN_FAILED` with
   `failure.code = VAULT_UNAVAILABLE` right after its request is recorded, before any engine is asked; nothing else
   is written (no vault file).
10. **Storage split.** `callum.classify(..., sink=)` hands each kept row's full evidence (`cited_url` (a grounding
    redirect's id is never kept, not even here), `final_url`, `canonical_url`, `excerpt`, `title`, `hint`) and the
    strict filter's verdict on each field to the service, which `put`s it into the vault under the run's tenant.
    `callum.split_evidence` then reduces every row to what is logged (`seo_log.jsonl`, the ledger, events): the vault
    reference (`seo-vlt-` + id + 8 hex check, `vault.ref`), a SHA-256 per field, the verdict flag per field, the host
    only (itself checked by the strict filter; a failing host is withheld), chain hosts, counts and codes. The MNC list
    is reduced the same way. `guard_record` still sweeps the whole envelope before `_commit`. Split, sweep and commit
    run in one guarded block: any exception closes the run `RUN_FAILED` (never left running) and nothing raw is
    written; records already put stay encrypted and unreferenced.
11. **Owner's view.** `GET .../citation-runs/{cid}` (and the MNC export) for the owning tenant rebuilds rows from the
    vault (`callum.client_view`): a field the strict filter flags (recorded flag, or the same check on the value now)
    is returned masked (`[hidden: looks like a credential]` / `[hidden: looks like personal data]`, `alert: true`,
    `reason` `LOOKS_LIKE_CREDENTIAL` / `LOOKS_LIKE_PERSONAL_DATA`); every other field in full. Another tenant: the same
    404 as a missing run. A record that cannot be read (no key, rotated away) shows `available: false` with a code,
    never an error. The client report renders audit envelopes only and has no citation-run path.
12. **Fetch rule (ADR 0050 r2 part 3a decision 6 reverted).** Only a credential finding blocks a fetch; a
    personal-data-shaped URL is fetched (it is the client's evidence) and never logged. The exact-host exemptions of
    r2 part 3a stay (the log no longer keeps paths, so they cost nothing there).
13. **Utility (client view, measured on the disclosed corpora).** Excerpts masked: 22 / 312 (7.05%), bar 5%, MISSED
    (all by check (a), a credential label word). URLs masked: 73 / 306 (23.9%) on `utility_corpus_v2`, 650 / 1180
    (55.1%) on `url_corpus`, bar 2.5%, MISSED (random-looking and numeric ids, label words in slugs, key-like tracking
    values). The display uses the strict filter as is; a looser display rule is an owner decision.

## Threat model

| Threat | What stops it | What does not |
|---|---|---|
| Disk read (backup, volume snapshot, a stolen copy of the data dir) | AES-256-GCM; the key is not on disk; tenant tags are keyed, so tenant names cannot be confirmed by hashing guesses | Record count, sizes and write times are visible |
| Log read | The vault writes no log line; the logged row keeps the vault reference, SHA-256 hashes, host-only URLs and verdict flags | Plain SHA-256 of a short, guessable field (an e-mail or phone alone in a hint) can be confirmed by hashing guesses |
| Cross-tenant read | Keyed tenant tag checked before decryption; tenant id in the associated data | A caller that passes the wrong tenant id on a client's behalf (the API's tenant authentication is the boundary) |
| Tamper (ciphertext, nonce, tag, key id, record moved to another tenant or id) | GCM tag over ciphertext and associated data; every failure is `VaultNotFound` | Deletion, truncation or rollback of whole lines by someone with write access to the data dir (a missing record is not found, not detected) |
| Missing key | `put` refuses and writes nothing; malformed settings refuse | - |
| Key leak via logs, errors, reprs, pickles | Closed error messages that never quote a value; redacted repr; pickling and copying refused; tests scan every file under the data dir and every message | The key is an environment variable: readable from `/proc/<pid>/environ` by the same user and visible in the hosting platform's variable store; Python cannot zero memory |

## Rotation procedure

1. Generate a new key (`python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`)
   and pick a new id (for example `k-2026-12`).
2. Set `SEO_VAULT_KEY_PREVIOUS` / `SEO_VAULT_KEY_PREVIOUS_ID` to the current key and id, then `SEO_VAULT_KEY` /
   `SEO_VAULT_KEY_ID` to the new ones; restart. New writes use the new key; old records still decrypt.
3. Re-encryption is not built. The previous key stays set until every record written under it has aged out of the
   retention window job 2 defines (or a re-encrypt helper is built and run). Removing the previous key before then
   makes those records permanently `VaultNotFound`.
4. Unset the previous pair and restart. The previous key must differ from the current one in bytes and in id
   (start refuses otherwise).

If a key leaks: rotate at once (steps 1-2), then treat every record under the leaked key as disclosed to whoever
holds it; re-encryption does not undo a disclosure.

## Deployment

A real key is set on the live service only at merge time, with Andre's approval. Until then the variables stay unset
and the vault stores nothing. No key literal is committed anywhere: the tests generate keys with `os.urandom` at run
time.

## Consequences

- Client citation evidence can be kept in full without appearing in plaintext on disk or in a log (once job 2 wires
  it).
- Losing the key loses the vault's contents; the key needs to be kept (in the platform's secret store and an offline
  copy Andre controls).
- `get` reads one line by offset from an in-memory index built at open, so open time is linear in the file size.
