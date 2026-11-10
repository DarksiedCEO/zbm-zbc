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
   outside every `except` block (no chained context). Correction (review adacb2ca): that makes them
   indistinguishable by the exception and its message only. At the vault level the paths are NOT timing-equal: an
   unknown id does no file read (its tenant HMAC is now computed too, so the keyed work matches, but the read does
   not). The indistinguishability boundary is the API: a wrong tenant and an unknown run answer the same 404 body.
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
    reference (`seo-vlt-` + id + 8 hex check, `vault.ref`), a keyed hash per field (job 3, decision 15; job 2 wrote
    a plain SHA-256), the verdict flag per field, the host
    only (itself checked by the strict filter; a failing host is withheld), chain hosts, counts and codes. The MNC list
    is reduced the same way. `guard_record` still sweeps the whole envelope before `_commit`. Split, sweep and commit
    run in one guarded block: any exception closes the run `RUN_FAILED` (never left running) and nothing raw is
    written; records already put stay encrypted and unreferenced.
11. **Owner's view.** `GET .../citation-runs/{cid}` (and the MNC export) for the owning tenant rebuilds rows from the
    vault (`callum.client_view`): a field with a client-view finding (job 3, decision 14; job 2 used the strict
    filter here) is returned masked (`[hidden: looks like a credential]` / `[hidden: looks like personal data]`,
    `alert: true`, `reason` `LOOKS_LIKE_CREDENTIAL` / `LOOKS_LIKE_PERSONAL_DATA`); every other field in full. Another
    tenant: the same 404 as a missing run. A record that cannot be read (no key, rotated away) shows `available: false` with a code,
    never an error. The client report renders audit envelopes only and has no citation-run path.
12. **Fetch rule (ADR 0050 r2 part 3a decision 6 reverted).** Only a credential finding blocks a fetch; a
    personal-data-shaped URL is fetched (it is the client's evidence) and never logged. The exact-host exemptions of
    r2 part 3a stay (the log no longer keeps paths, so they cost nothing there).
13. **Utility (client view, measured on the disclosed corpora).** Job 2 (strict filter as the display rule): excerpts
    masked 22 / 312 (7.05%), URLs 73 / 306 (23.9%) on `utility_corpus_v2` and 650 / 1180 (55.1%) on `url_corpus`,
    both bars MISSED. Job 3 (decision 14): excerpts 0 / 312 (0%, span and margins), URLs 0 / 306, 0 / 1180 and 0 / 42
    (`KEEP_URLS`); bars (5% excerpts, 2.5% URLs) MET. The strict lane's own numbers are unchanged (22 / 312 excerpts
    withheld from the log, 73 / 306, 650 / 1180 and 23 / 42 URLs flagged in the log).

## Job 3: precise client-view masking, keyed log hashes

14. **Client view rule** (`secret_guard.client_view_findings(text)`, `secret_guard.client_view_url(url)`; used ONLY by
    `callum._display`, i.e. the owner's decrypted view and its MNC export; never for logs, fetches, outbound data or
    any cross-tenant surface, which keep the strict filter unchanged). Read on the strict lane's inspection views
    (NFKC, invisibles removed, Unicode digits and confusables folded, bounded percent / entity / escape / base64 / hex
    decoding). A text field is masked whole on a HIGH-PRECISION finding only:
    - `strong_key`: the rule (c) prefix table (PEM / PGP included), also on the view with spaces removed, on each
      token with its separators removed (and removed after its first / second separator), a PEM header with
      separators put in, a Discord token with dashes or dots put in;
    - `userinfo`: `scheme://user:password@` (a password present);
    - `label_value`: a rule (a) label word (split by up to two separator characters, at a piece boundary) FOLLOWED by
      `:` `=` `=>` `->` an arrow `|` `>` (JSON / XML / table forms), ` is ` or ` - `, and a value of 4+ non-space
      characters (or two short pieces); after `was` / `are` / `would be` / `will be` / `remains` the value must look
      like one (a digit, a symbol or an inner capital). The bare word in prose is NOT masked; `the password is
      required` IS (the ` is ` separator is on the list);
    - `card`: Luhn-valid, 13-19 digits read across any separators / padding at digit-group boundaries, AND a card
      network's prefix and length (Visa 4: 13/16/19; Mastercard 51-55, 2221-2720: 16; Amex 34/37: 15; Discover,
      Diners, JCB, UnionPay ranges: 14-19). Stricter than "any Luhn number" on purpose: 13-19 digit social / order
      ids starting 1, 7, 8, 9 are never cards;
    - `ssn`: 3-2-4 groups (separators `-` `.` space) with a valid area (not 000, 666, 900-999), group and serial;
    - `phone`: E.164 (`+` then 8-15 digits) or NANP `(xxx) xxx-xxxx`, `xxx-xxx-xxxx`, `xxx.xxx.xxxx` (and `(xxx)` then
      seven digits however grouped);
    - `email`: an address (the strict lane's plain and space-split forms), `name [at] domain [dot] tld` (a dot part
      required) or `name at domain dot tld`;
    - fail closed: over `MAX_INPUT`, a non-string, more than `CV_GROUPS_MAX` digit groups, an internal error.

    An excerpt is judged on its span AND the strict lane's margins (a value cut by the span edge is found whole); the
    margin findings are kept in the vault record (`excerpt_view`), never logged. A URL keeps every part but the
    offending ones, each replaced by `{hidden}` (`alert: true`, `check: url_part`; the row's top-level `url` stays
    null when masked): a path segment with a finding; every segment when the path read joined by `/` or glued has a
    finding no single segment has (a glued e-mail only with a common top-level domain); a query or `k=v` fragment value
    under a credential-like key (`token`, `key`, `api_key`, `secret`, `sig`, `signature`, `password`, `pwd`, `auth`,
    `access_token`, `client_secret`, `code`, `session`, `sid` and names ending in token / secret / password / apikey /
    signature ...), with a finding of its own, or high-entropy (32+ characters mixing upper, lower and digits, or 40+
    hex) under any key except analytics / tracking ones (`TRACKING_PARAMS`, `utm_*` and the listed analytics keys); a
    key with a finding hides its whole pair; user info with a password. A host label with a finding (or a key prefix,
    case-insensitive) masks the whole URL, as does a URL that cannot be read or is over the caps. Product ids, listing
    ids, commit SHAs, Drive / Docs ids, random-looking slugs, years, prices and order numbers are shown.

    Recall (client view, measured): leak_corpus_v2 text cases strong keys 1734 / 1734, labelled credentials
    1250 / 1250, cards 102 / 102, SSNs 51 / 51, phones 102 / 102, e-mails 51 / 51; URL cases strong keys 305 / 305,
    cards 20 / 20, SSNs 10 / 10, phones 18 / 18, e-mails 9 / 9; secret_corpus strong keys 23 / 23 and labelled
    24 / 24; the real service path (owner's API response): no non-residual family shown.

    **Residual (by design).** Bare unlabelled random tokens with no known prefix (an AWS secret access key alone, 32 /
    64 hex, 40-character base64) and bare unlabelled short values are NOT masked in the client view (measured: 196 /
    204 leak_corpus_v2 text cases and 29 / 38 URL cases of those families are shown to the owner). They are the
    owner's own evidence behind tenant authentication; the strict lane still keeps every one of them out of the log,
    the data directory, the ledger and every fetch.
15. **Keyed log hashes.** Every per-field hash written to the log is HMAC-SHA256 (`hmac_sha256`) under a sub-key of
    the vault key derived with HKDF-SHA256 and its own info label (`seo-citation-vault/v1/log-field-hmac`, distinct
    from the AES-GCM and tenant-tag labels), over a tag, the tenant id, the field name and the value (so equal values
    of two tenants or two fields do not compare equal). The row's evidence carries `hash_key_id` (the current key's
    id; a rotation changes the hashes). `CitationVault.field_hash` refuses without a key (`VaultUnavailable`), and
    `callum.split_evidence` without a hasher refuses a run with evidence: no plain per-field SHA-256 is written any
    more. Content-integrity hashes that are existing contracts stay plain SHA-256 (`result_sha256`, `prompt_set_sha256`,
    `domain_sha256`, the log chain).

## Threat model

| Threat | What stops it | What does not |
|---|---|---|
| Disk read (backup, volume snapshot, a stolen copy of the data dir) | AES-256-GCM; the key is not on disk; tenant tags are keyed, so tenant names cannot be confirmed by hashing guesses | Record count, sizes and write times are visible |
| Log read | The vault writes no log line; the logged row keeps the vault reference, keyed HMAC-SHA256 field hashes (tenant- and field-bound), host-only URLs and verdict flags | Equality of one field's value across rows of the same tenant under the same key is visible (same HMAC) |
| Owner's view (tenant-authenticated) | High-precision client-view masking (decision 14) | Unlabelled random tokens and bare short values are shown to the owner (residual, decision 14) |
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
