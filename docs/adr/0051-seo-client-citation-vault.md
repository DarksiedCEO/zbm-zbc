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
   failed or short write cut back to the old size (or every later write refuses). A symlink, a file not owned by the
   service user or open to group/others refuses to open; a torn final line is cut at open (decision 20), a torn final
   line together with any other bad line refuses. Malformed or duplicate lines are counted (`bad_lines`) and never
   served.
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
    - `phone`: E.164 (`+` then 8-15 digits) or NANP `(xxx) xxx-xxxx`, `xxx-xxx-xxxx`, `xxx.xxx.xxxx` (one or two
      separators; and `(xxx)` then seven digits however grouped), also across listed filler words (decision 19);
    - `email`: an address (the strict lane's plain and space-split forms), `name [at] domain [dot] tld` (a dot part
      required), `name at domain dot tld` / `name at domain.tld` and their letter-spaced / split-word forms; the
      PLAIN spelled form `<word> at <domain>` and a price before "at" / "@" are not read as an e-mail (decision 22,
      which replaces decision 18's retailer / prose exemption);
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

## Review 601e328a: views, retailer key, linear excerpt path, vault hardening

16. **Storage write and lock scope (review adacb2ca SHOULD 1, recorded here).** The split (encryption into a pending
    `VaultBatch`, keyed hashes) and the `guard_record` sweep run OUTSIDE the service lock; under it only, in this
    order: the integrity gate, the kill re-check (`ai_probe` for the tenant, then `audit`; a kill here discards the
    batch and nothing is written), ONE `write_batch` (one append, one fsync; a duplicate id inside the batch or one
    already in the index refuses the whole batch, nothing written), then the commit. The owner's view takes a snapshot
    under the lock and decrypts / masks outside it; an idempotent replay of the POST returns that view after leaving
    the lock (review 601e328a SHOULD 1).
17. **Orphaned records (SHOULD 2).** When the commit fails after `write_batch`, the run is closed `RUN_FAILED` and its
    records stay in the vault, encrypted under the tenant's key and referenced by nothing (never served: no recorded
    row holds their refs). They are counted (`CitationVault.orphans`, `health().citation_vault.orphaned_records`,
    counts only, this process). A purge job is NOT built now; it would drop index entries no recorded run refers to.
18. **Retailer-keyed " at " exemption (owner decision).** The spelled " at " e-mail forms are shown as prose only when
    the domain after "at" is in the CLOSED list `secret_guard.CV_RETAILER_HOSTS` (large retailers, marketplaces and
    platforms with their regional top-level domains, about 130 hosts; a subdomain of one counts) AND the word before
    "at" is on `CV_PROSE_BEFORE_AT`, from which every role-mailbox word and pronoun (`CV_ROLE_WORDS`: sales, support,
    info, shop, store, deals, offers, shipping, order, sale, us, me, you, our, ...) was removed. Measured: role
    mailboxes on non-retailer domains 1000 / 1000 masked (50 role words x 10 domains x 2 forms), role words before
    retailer domains 150 / 150 masked, prose words before other domains masked, retailer shop prose 0 / 384 masked.
19. **Client view on the strict lane's views.** The client rules read the same views as the strict lane: the
    markdown-only view (no tag strip) and the markup-stripped view where only real tags without an address are
    removed; every percent round of a decoded base64 / hex run; the filler-word bridge (2-6 digit groups: any word
    for the card rule, which has Luhn; the listed filler words `CV_FILLER_WORDS` for the SSN and phone rules); the
    letter-spaced view for labels (`a p i _ k e y = value`). Recall on the review's corpus (every required class x
    the strict lane's disguise set and this review's disguises, 3380 cases): 100%; unlabelled random tokens remain
    the residual of decision 14.
20. **Torn final line (SHOULD 3).** At open, if ONLY the final line is torn (no trailing newline) and every earlier
    line parses, the torn bytes are cut (`ftruncate` + `fsync`), the cut is counted (`tail_repair`: torn lines,
    bytes removed, records kept; never content) and recorded once as the typed ledger event
    `citation_vault_tail_repaired` (counts only; a ledger that cannot take it leaves `recorded: false` in health).
    A torn line with any other bad line, a symlink, a foreign owner or open permissions still refuse to open; an
    observe-only start (`observe_existing_on_start`) repairs nothing and refuses a torn line as before.
21. **Linear excerpt path.** `callum._excerpt_window` widens to whole words by at most `EXCERPT_WORD_MAX` (1024)
    characters past each margin edge (an answer with no whitespace made the window the whole text). The prompt
    guard's e-mail patterns start only at the start of a local-part run and are possessive, the spelled-words form is
    decided by a linear scan that finds the same matches, and the year-range pattern is written without nested
    whitespace runs: the same accept / reject (fuzz-tested against the previous patterns), 64 KB in under 0.2 s.

## Review c13529af: wrapped variants, dash folding, phone vs EAN, base64 offsets, operator text, plain " at "

22. **The plain spelled " at " form is shown to the owner (owner decision, client view ONLY).** In the client view,
    "<word> at <domain>" (ONE letters-only word, spaces, " at ", a domain written with literal dots and nothing
    spelled, bracketed or defanged after it) is NOT read as an e-mail. Rationale: it is irreducibly ambiguous with
    ordinary commerce prose ("Visit us at brand.com", "Get yours at shop.co.uk", "Our team at brand.com can help",
    "Customers at brand.com rate it 4.8"); the client view is a courtesy on the owner's OWN data behind tenant
    authentication (Option B), and every closed-list exemption tried before (decision 18, review 08eea6e1 SHOULD 5)
    left either utility or recall gaps. The client view STILL masks: a literal "@" (also " @ "), "[at]" / "(at)" /
    "{at}" (also "[a t]"), defanged dots, the spelled "dot" ("x at y dot com"), the hyphen / underscore / glued
    variants, "at sign", the Spanish / French spellings, letter-spaced forms, and any spelled form whose local part
    holds a digit, ".", "_", "+" or "-". S1: a local part of digits and dots only (a price: "$12.99 at", "12.99@") is
    never an e-mail in the client view. The strict lane is unchanged: it withholds every one of these forms from the
    log, the ledger, the data directory and every outbound surface. The retailer / prose / role lists of decision 18
    (`CV_RETAILER_HOSTS`, `CV_PROSE_BEFORE_AT`, `CV_ROLE_WORDS`, `CV_PRONOUNS_BEFORE_RETAILER`, `_cv_unprose_at`) are
    INERT: no rule calls them (tests still read them, so they are kept and documented, not deleted); decision 18 is
    superseded. Residual: a real address written in the plain form ("jsmith at gmail.com", also a role mailbox such as
    "sales at brand.com") and a numeric-only local part with "@" are shown to the owner.
23. **Wrapped and split forms (both lanes).** In the e-mail variant view, spaces (line breaks read as spaces) next to
    "-" / "_" are removed before the variants are read ("name-\nat-\ndomain", "name_\nat_", "at-\nsign"); a number
    word, a listed filler word and the Spanish / French spelled words split by one line break are re-joined when the
    halves form the word; "[a t]" / "(a t)" read as "[at]"; a JSON key wrapped before its closing quote is a label.
24. **Unicode dashes (both lanes).** U+2010-2015, U+2212, U+FE58, U+FE63 and U+FF0D read as "-" in the per-character
    map every view uses (typed dashes and the `&hyphen;` / `&dash;` / `&minus;` entities).
25. **Phone numbers are never a safe shape (both lanes).** The EAN / ISBN / order / date safe shapes are not applied to
    a digit run right after "+" or with a phone word ("tel", "phone", "call", "text", "whatsapp", "mobile", "cell",
    "fax", "sms" ...) within 16 characters before it.
26. **Base64 (both lanes, the same decoded views).** Offsets 0-8 (and the first 8 "-" / "_" boundaries) are all
    decoded (no offset is skipped); 0-3 bytes are always tried trimmed at both ends of each decoding; format
    characters (zero-width, bidi, TAG...) and Hangul fillers are removed BEFORE the readable-ratio test. Each run
    gives its main readings and ONE edge view (at most 8 pieces of 128 characters; at most 16384 edge characters per
    view) holding the trimmed edges and the head of a reading that only repeats an earlier one shifted by whole quanta.
    Linear: the bytes are split at two character starts inside the trim zones and the middle is decoded once.
27. **Operator text is checked before it is written (strict check).** A prompt set is written to `seo_log.jsonl` in
    plaintext and its prompts / brand-term rules are sent to engines: every string field must pass
    `secret_guard.check` (a credential: 422 `PROMPT_SECRET`; personal data: 422 `PROMPT_PERSONAL_DATA`; nothing
    written). The MNC export shows a prompt only when the strict check passes too. Audit / schedule paths
    (`PATH_WITHHELD`), entity values and provenance (`ENTITY_VALUE_WITHHELD`; check (e) waived on the telephone value
    only) and URL-prefix Search Console properties (`GSC_PROPERTY_INVALID`) are refused the same way; a log-ingest
    path-template segment kept by the template rules but failing the strict check (other than a label word alone,
    such as "reset-password") is written as `{token}`.
28. **Client view speed.** Strong-prefix scans are remembered per call (an identical string is scanned once), the
    case-insensitive `db_uri` / `jwt` / `azure` families are skipped when a literal every match holds is absent, a
    field found `cap` is not read further (also inside one digit span), and the per-word label and token reads are
    skipped / memoised where they cannot change the answer (equivalence-tested). Measured worst case at 64 KB on the
    adversarial repeats: see the review report; both lanes are linear.

## Review c0c09bd1: operator text vaulted, identifiers validated, scoped rules

29. **Operator text is VAULTED, not refused (supersedes the refusals of decision 27; design change, Option B).**
    Refusing operator text that failed the strict check refused 20-30 % of legitimate input. Prompt-set prompts and
    brand-term context / negative terms, entity values and provenance, audit / schedule paths (with their queries) and
    URL-prefix Search Console properties are stored in the tenant's vault (`svc_optext.py`: one AES-GCM record per
    request, the citation evidence's mechanism); the log record keeps per text only a stub (vault id, index, key id,
    keyed HMAC bound to tenant + field, the strict check's flag). Content hashes, request hashes (idempotency) and
    ledger evidence are computed over the HMACs. The apply handler opens the stubs (live and on replay); a text that
    cannot be opened makes its prompt set / schedule / audit unusable (`VAULT_UNAVAILABLE`). No key: the request is
    refused 503 `VAULT_UNAVAILABLE` (never kept in plaintext). Refused before anything is written or sent: a strong-key
    prefix, a PEM / PGP block, URL user info with a password (`secret_guard.operator_refusal`) and, in a fetched /
    queried URL, a value under a credential query key by exact name (`operator_url_refusal`). Prompts still go to the
    engines as typed (W4-3's personal-data rule for prompts is unchanged). A report keeps a flagged path / its URL only
    in the strict lane's kept form (`safe_url`); the fetch and Search Console reads use the full text from memory.
    Owner reads (prompt set, entity, audit / schedule config, tenant) decrypt for the owning tenant and apply the
    client-view masker (a business telephone value is shown). httpx / httpcore loggers are held at WARNING (a fetched
    URL is never a log line).
30. **Identifiers are validated, not vaulted.** Tenant id, tenant domains, Finance client id, prompt-set name,
    competitor domains and an entity's source keep their formats and are refused (`IDENTIFIER_WITHHELD`) when they hold a
    strong key / PEM / user info, a run of 9+ digits, an e-mail form, or fail the strict check.
31. **Scoped rules (both lanes unless noted).** A log-ingest segment with a label finding is kept only when it is the
    label word alone after decoding (`secret_guard.label_only`); the segment after such a segment is withheld (strict
    `safe_url` / `stored_url`, never the fetch decision) and masked (client view) unless it is an action word. The S1
    price exemption (client view) covers only a real price (currency sign and / or two decimals, thousands commas)
    before the PLAIN spelled " at "; decision 22 is for prose only (never inside a URL); a code / model number
    (2-12 upper-case letters and digits, both present) before the plain " at " is prose in the client view. The
    variant view joins spaces around "-" / "_" only next to an at / dot variant word. "at symbol", "at at" and the
    e-mail emoji read as "@"; decoded base64 runs are trimmed up to 6 bytes at each end. The client view reads number
    words as SSNs / phones and labelled unformatted phones (the strict lane's phone words within 16 characters).

## Sink jobs A-C: the log and the ledger take machine values only

32. **Sink guard (job A, `src/sink_guard.py`).** Every `SeoService._commit` record and every ledger payload passes
    `sink_guard.enforce` before anything is written; a string (key or value, any depth) that is not a machine value
    refuses the write (`SINK_REFUSED`, nothing written). Closed classes: `empty`, `service_id` (`seo-<prefix>-<40 hex>`,
    vault and blob refs included), `scoped_id`, `invoice_id`, `request_id`, `request_key`, `hash` (16 / 32 / 40 / 64
    lower-case hex with a letter), `timestamp`, `date`, `number`, `version`, `boolean`, `actor`, `hostname` (strict
    host check), `code` (1-64 of `[A-Za-z0-9_.:-]` with a letter, strict check) and, from job C, `model_id`. No class
    admits whitespace or `@`; `/` only inside a request key, a `name/N` version or a declared model id. Violations
    report normalised paths and reason names, never a value. `LEGACY_PROSE_PATHS` lists the (sink, path) pairs still
    holding prose; a test fails when an entry is unused or a new path appears. 141 entries at job A, 53 after job B,
    23 after job C (left for job D: `entity_created` 9, `log_chunk` 1, `log_ingest_created` 2, `log_ingest_finished`
    7, `prompt_set_version` 4).
33. **Blob API (job A, `vault.py`).** `put_blob(tenant, obj)` stores a JSON object or array of any size as chunk
    lines (192 KiB each, at most 4096) plus a manifest line in the same vault file, each AES-256-GCM under the tenant
    tag with associated data binding tenant, blob id, chunk index, chunk count and key id; one append, one fsync, all
    or nothing; inside `collecting(batch)` the lines join the batch for a later `write_batch`. It returns a ref
    (`seo-blb-` + 32 hex + 8 hex check). `get_blob(tenant, ref)` answers only the storing tenant; every failure is
    the same `VaultNotFound`; no key is `VaultUnavailable`. `field_hash_matches` (job B) checks a keyed log-field
    HMAC under the current or the retiring key in constant time.
34. **Vaulted documents (job B, `svc_vaulted.py`).** A client-owned document is normalised, HMAC-ed (the log-field
    sub-key of decision 15, bound to tenant and field name, over its canonical JSON) and encrypted OUTSIDE the service
    lock; under the lock: the kill-switch re-check, one vault append, the commit. A commit that fails after the
    append leaves an orphan (counted, never served). Readers load the blob for the owning tenant outside the lock and
    check the HMAC; a lost, changed, foreign or unreadable blob is `vault_unavailable: true` in the view and
    `VAULT_UNAVAILABLE` (503) for every dependant. No key: refused up front, nothing written.
35. **What each migrated kind keeps in the log.**
    - `audit_completed` (job B): `audit_id`, `report_ref`, `report_hmac`, `facts` (report version, outcome,
      findings count, start / finish times, per agent: agent, task, outcome, findings count). Ledger
      `audit_report_recorded`: `report_hmac`, outcome, findings. Field `audit_report`.
    - `drift_recorded` (job B): `schedule_id`, `older`, `newer`, `drift_ref`, `drift_hmac`, `facts` (counts).
      Field `drift_report`. Search Console section evidence: keyed hash, field `gsc_section`.
    - `audit_interrupted` / `citation_run_interrupted` (jobs B / C, log and ledger): `failure.where` is
      `svc_audits.where_code` (`module:function:line` without angle brackets, else `module:line`, else `unknown`);
      `error_type` is a code or `Exception`.
    - `citation_run_recorded` (job C): `citation_run_id`, `result_ref`, `result_hmac`, `facts` (`outcome`, `rows`,
      `findings`, `usable_answers`, `mentioned_not_cited`, `strength_answers`: one code and counts). Ledger: the
      same hash, outcome and three counts. Field `citation_result`. No host, URL, model name, note or prompt.
    - `engine_calls_reserved` / `engine_run_settled` (job C, log and ledger): unchanged except `model`: a declared
      model id as it is, any other string as `UNKNOWN_MODEL` with `model_hmac` (keyed, field `engine_model`, bound to
      the tenant; null without a vault key).
36. **Citation results are one blob; the per-row vault records and per-field stubs are retired (job C; supersedes
    the storage side of decisions 10 and 15 for new runs).** The blob is `{"v": 1, "result": the split and swept
    envelope, "evidence": {evidence id: record}}`: `callum.split_evidence` now only separates the envelope (each row
    keeps `evidence_id`, the basis and the strict verdict flags; hosts only) from the evidence records, and
    `guard_record` still sweeps the envelope. Chosen over "per-row records plus an envelope blob" because it has one
    vault object per run instead of rows + 1, one keyed HMAC instead of up to six per row, one read per owner GET
    instead of one per row, and nothing per-row in the log or under the lock. Consequences: a damaged blob makes the
    whole result unavailable (it was per row); the owner's row shows `evidence.evidence_id` where it showed
    `evidence.vault_id`. The owner's view (decisions 11 and 14) is otherwise unchanged; the MNC export of a result
    that cannot be read answers 503 `VAULT_UNAVAILABLE` instead of rows marked unavailable.
37. **`model_id` (job C).** Accepted only when the value is EXACTLY one of `sink_guard.MODEL_IDS`: the union of the
    adapters' declared allowlists (`perplexity.MODELS` / `SONAR_MODELS`, `openai_engine.MODELS`,
    `anthropic_engine.MODELS`, `google_engine.MODELS`); `config.py` can only configure one of them. Nothing is
    admitted by shape; `@` and a free `/` stay outside the `code` class.
38. **Renamed hash fields (plain SHA-256 of client content -> keyed HMAC).** `report_sha256` -> `report_hmac`
    (`audit_completed`, ledger `audit_report_recorded`, the audit view), `drift_sha256` -> `drift_hmac`,
    `result_sha256` -> `result_hmac` (`citation_run_recorded` log and ledger, the citation run view), the per-row
    `evidence.fields.*.hmac_sha256` and `hash_key_id` are gone. The old names stay in memory and in the views as
    `null` for new records and carry the recorded value for lines written before the change. Decision 15's list of
    plain content hashes is now `prompt_set_sha256` (computed over HMAC stubs), `domain_sha256` (a registered
    domain, itself logged as a host name) and the log chain.
39. **Replay.** Lines written before each job still replay: an `audit_completed` with `report`, a `drift_recorded`
    with detail, a `citation_run_recorded` with `result` / `result_sha256` (its rows are served from their per-row
    vault records), interruption records with a free-form `where`, engine records with a raw `model`. The sink guard
    is applied at write time only.
40. **A `[withheld]` marker is never logged.** Where a logged fact would have carried the sweep's marker, the code
    `WITHHELD` is written (`svc_citations.run_facts`); the marker itself is refused by the guard.

## Sink job D: no free text in any log (the legacy list is gone)

41. **No legacy list.** `sink_guard.LEGACY_PROSE_PATHS` (23 entries after job C), `LEGACY_ROWS`, `LEGACY_COUNTS`
    and the bypass (`_legacy`) are deleted. `refused` is `violations`: ANY non-machine value refuses its write, for
    every kind and every ledger event. The suite's exactness test is now: a refusal anywhere but in the tests that
    provoke one on purpose (a closed list of test names) fails the run; a full run also shows that each of the 27
    kinds was committed and passed.
42. **What each kind migrated in job D keeps in the log** (everything else is ONE blob per record, HMAC-ed with the
    log-field sub-key over its canonical JSON, encrypted outside the service lock, appended under it with the commit;
    the `_apply` handler takes the document from the commit in memory and reads the blob on a replay):
    - `entity_created`: `entity_id`, `tenant_id`, `fields` (the field NAMES, from the closed schema), `flags` (per
      field, the strict verdict names of its value and provenance, or null), `content_ref`, `content_hmac`. The blob
      (`entity_content`, operator text vault) is `{v, entity_id, version, fields: {name: {value, source, provenance,
      authority}}}`. Ledger: `entity_id`, `tenant_id`, `content_hmac`. The founder's own record is a constant of the
      code base: its line is `{entity_id, tenant_id, seed: "zbm_nap_v1"}` (no vault, so a deployment without a vault
      key still starts) and its ledger payload names the seed.
    - `entity_field_set`: `entity_id`, `field`, `version`, `flags`, `content_ref`, `content_hmac`; the blob holds
      that one field of that version. Ledger: `entity_id`, `field`, `version`, `content_hmac`.
    - `prompt_set_version`: `prompt_set_id`, `tenant_id`, `version`, `content_ref`, `content_hmac`, `facts` (counts
      of prompts, brand terms, rules and competitor domains; the engine codes). The blob (`prompt_set_content`,
      operator text vault) is the whole content: name, brand terms, rules (term, context terms, negative terms,
      flags), competitor domains, prompts, engines. Ledger: ids, version, `content_hmac`. The version's fingerprint
      (`sha256` in memory, in the views and wherever a later record names the prompt set) IS that keyed HMAC. The
      known gap (an e-mail-shaped context term in the line) is closed: no brand term, rule term or context term is
      in the line.
    - `log_ingest_created`: ids, `domain`, `scheme`, `format`, `key_fp`, `robots_status` (a code),
      `robots_present`, `sitemap_sample_count`, `inputs_ref`, `inputs_hmac`. The blob (`log_ingest_inputs`) is
      `{v, robots_text, sitemap_sample}`. Ledger: ids, format, `domain_sha256`, `robots_status`, `inputs_hmac`.
    - `log_chunk`: `ingest_id`, `seq`, `last`, `bytes`, `delta_ref`, `delta_hmac`, `counts` (lines, non-bot, no
      User-Agent, quarantine reason codes, per bot family: requests / verified / spoofed / claimed). The blob
      (`log_chunk_delta`) is the chunk's aggregate delta (path templates, status mix, sketch, sample indices).
      Uploaded log LINES are in neither: a chunk is parsed in memory and dropped, and the request's idempotency hash
      is over the keyed HMAC of its base64 text. Ledger: `ingest_id`, `seq`, `lines`, `delta_hmac`.
    - `log_ingest_finished`: `ingest_id`, `report_ref`, `report_hmac`, `facts` (outcome code, findings count,
      lines). The blob (`log_report`) is the report (methodology, limitations, findings, templates). Ledger:
      `ingest_id`, `report_hmac`, `outcome`.
    - `tenant_gsc_property_set`: a Domain property is logged as `property_kind: "domain"` + `property_host` (a host
      name of any legal length is a machine value; `sc-domain:` + a long host was not); a URL-prefix property as its
      stub (vault id, keyed HMAC) + `property_host`, as before. Ledger: `property_hmac` (URL prefix) or
      `property_sha256` (Domain: over a logged host). `gsc_read_started`: `property_kind` + `property_host`, or
      `property_host` + `property_hmac` (it carried the kept URL text, which the guard refused).
    Memory holds: entity values and prompt-set content (opened when the line is applied); for a log ingest the
    counts only, with the inputs and the full totals read from the blobs on demand, outside the lock.
43. **A blob that cannot be read fails closed.** Entity: the field is `null`, the view says `vault_unavailable`,
    the audit's entity check is `BLOCKED` / `VAULT_UNAVAILABLE` (never a comparison with `null`) and the Search
    Console read is `BLOCKED`; setting the fields again repairs it. Prompt set: the version is empty and
    `vault_unavailable`; audits and citation runs naming it refuse `VAULT_UNAVAILABLE`. Log ingest: lost inputs
    refuse the next chunk and the finish, a lost delta refuses the finish, a lost report shows `report: null` with
    `vault_unavailable`. Search Console binding: a URL-prefix property whose vault record is lost leaves the
    binding IN PLACE (`gsc_property_host` in memory, from the logged host), marked `gsc_vault_unavailable`;
    `GSC_PROPERTY_TAKEN` is decided on the logged host, so the host is never free for another tenant, the status
    still counts the tenant as bound, and reads for it are `BLOCKED`.
44. **Third sink: Python logging (`src/log_sink.py`).** Every log record is scrubbed before any handler emits it:
    the message template is kept only when it is a string literal of the calling code (found among the constants
    of the caller's code object; an f-string is not), else the message is `WITHHELD`; each argument is kept only
    when `sink_guard` classes it as a machine value (ints and floats under nine digits), else `WITHHELD`; exception
    text, tracebacks and stack text are dropped and replaced by `[<ExceptionType>@<module:function:line>]`
    (`svc_audits.where_code`). Installed as the log-record factory, as a `logging.Filter` on the root logger and
    on `seo`, `uvicorn`, `uvicorn.error`, `uvicorn.access`, `httpx`, `httpcore`, `asyncio`, `py.warnings` and
    their handlers, and in `logging.Handler.handle` (handlers added later, records made by `makeLogRecord`).
    `SeoService.__init__` and `api.create_app` install it. The guard's own refusal line logs the sink, the count
    of paths and the first path as a code.
45. **Error bodies carry reason codes and machine values only.** A validation error keeps `loc` elements that are
    an index or a name from a closed set (the request models' fields, the routes' parameter names, the
    forbidden-key catalogue); any other element is `"*"`. `msg` is kept only for the error types whose text comes
    from the schema (a pattern, a bound, the allowed literals), else `"invalid value"`; `input` and `ctx` are never
    returned; `type` is a code. A forbidden key is named by its normalised catalogue name, not by the path of
    caller keys that led to it. Finance (31)'s view of an audit carries `paths_count`, not the audited paths.
46. **Idempotency hash.** `request_sha` is the SHA-256 of the request body with every non-machine string replaced
    by its keyed HMAC (`request_text`, `SeoService._machine_body`); a body of machine values only hashes as before
    (lines written earlier stay idempotent). No vault key and a non-machine string: `VAULT_UNAVAILABLE`.
    Review 2b26d980: a request a release up to 8bd9e29e recorded carries the LEGACY hash (the plain SHA-256 of the
    body, free text included). A retry of it after the deploy is compared with the legacy computation as well
    (`SeoService._legacy_request_sha`: compared only, never written), so it answers what the first call did
    instead of 409; a different body is still `REQUEST_ID_REUSED`.
47. **Hash fields: every one and its decision (supersedes the last sentence of decision 38).**

    | Field (log / ledger) | Input | Decision |
    |---|---|---|
    | `content_hmac` (entity, prompt set) | the version's content | keyed HMAC (new; was `fields_sha256` / `value_sha256` over HMACs and identifiers, and a plain `sha256` over name, brand terms, competitor domains and prompt HMACs) |
    | `inputs_hmac`, `delta_hmac`, `report_hmac` (log ingest) | robots.txt + sitemap sample; a chunk delta; the report | keyed HMAC (new; were plain `robots_sha256`, `sitemap_sample_sha256`, `delta_sha256`, `report_sha256`) |
    | `facts_hmac` (`invoice_verified`, the audit's verification) | Finance's facts about the invoice | keyed HMAC (new; was plain `facts_sha256`) |
    | `property_hmac` (URL-prefix property) | the property URL | keyed HMAC (new on the ledger and in `gsc_read_started`; was a plain SHA-256 of the HMAC / of the kept URL) |
    | `report_hmac`, `drift_hmac`, `result_hmac`, `section_hmac`, `audit_report_hmac`, `model_hmac`, stub `hmac` | client documents, a model string, operator texts | keyed HMAC (jobs B, C, review c0c09bd1) |
    | `ruling_id_hmac` | a Compliance ruling id of a shape Compliance does not issue | keyed HMAC, bound to the tenant (review 2b26d980, decision 52); absent when the id has the issued shape |
    | `request_sha` | the request body | plain SHA-256 over machine values and keyed HMACs (decision 46) |
    | `sha256` (prompt-set reference in `audit_requested`), `prompt_set_sha256` | the prompt-set fingerprint | that fingerprint as recorded: the keyed `content_hmac` for a version created since job D; for an older version the plain SHA-256 it was created with (name kept so old lines and readers still agree) |
    | `domain_sha256` | a registered domain | plain: the domain is logged as a host name |
    | `paths_sha256` | the audited paths | plain, over their keyed HMACs |
    | `property_sha256` (Domain property) | `sc-domain:<host>` | plain: the host is logged |
    | `scorecard_sha256` | an agent's scorecard | plain: counts and outcome codes derived from logged facts |
    | `document_sha256` | the rendered client report | plain, kept on purpose: the owner verifies the delivered file with it; its input is a whole document, not a short guessable text (residual: a near-empty document is derivable from logged facts) |
    | `audit_report_sha256` | the audit report | null since job B (a value only for audits recorded before it) |
    | `branding_sha256` | the deployment's report branding | plain: operator configuration, not client text |
    | `key_fp` | the log hash key | plain, 64 bits of a deployment secret's SHA-256 (binds an ingest to its key) |
    | `line_sha256`, `prev_line_sha256`, `record_sha256`, `payload_sha256`, `log_head_sha256` | log lines and ledger payloads | plain: the chain, over what is logged |
    | derived ids (`seo-<prefix>-<40 hex>`) | tenant id, actor, request key, prompt-set name, event type, payload hash | plain: identifiers and machine values (the known limit below) |

    `tests/test_sink_job_d.py::test_every_hash_field_has_a_decision` fails when a hash-like key that is not in
    this table appears in a log line or a ledger payload.
48. **The universal canary test (`tests/test_canary_all_routes.py`).** Every route is discovered from
    `app.routes` (41; none exempt; a new route without a builder or an exemption fails), every string field from
    the request models' schemas, the path and query parameters and the four headers the service reads (279 fields).
    Each gets unique run-time canaries in six families (prose, e-mail, key prefix, 16-digit Luhn, SSN shape, short
    token) and three disguises (raw, base64, percent-encoded), plus a syntax-fitted form where a field has one,
    and (review 2b26d980) four more families planted raw and syntax-fitted: an e-mail address without "@",
    separator-formatted digits, 32 hex characters, a JSON number (6,474 canaries, 6,413 requests in all), in an
    otherwise valid call as the owning caller. The persisting flows run
    with canaries in operator text and in engine- and crawl-originated text (answers, citation titles, hints and
    URLs, page titles and text, canonical and redirect URLs, robots.txt, sitemap URLs, llms.txt, uploaded log
    lines, Search Console pages and queries), then a restart. The scan covers every data-directory file (the log,
    both vault files, a real log file written by a logging handler), the ledger payloads, the logs captured at
    DEBUG, every error body, another tenant's hub responses, the Finance views, the operator aggregates, the
    evidence and export views, /health and /status, in raw, lower / upper case, hex, base64 and base64url at three
    alignments, percent-encoded, JSON-escaped and digits-only forms. Result: 0 hits outside the known limit.
    Mutations that make it fail: the guard disabled; any one of the six migrated kinds written inline again
    (refused by the guard, and found by the scan with the guard off; with the guard off and nothing inlined the
    scan is clean); the logging sink disabled; the validation `input` echo restored.
49. **Known limit (asserted, not hidden; wording corrected by review 2b26d980).** What an IDENTIFIER field
    still accepts, and therefore logs as a machine value, is exactly this:
    - `POST /tenants` `tenant_id` and `POST /tenants/{tid}/finance-client` `finance_client_id`: a token of the
      field's own pattern (40 / 64 characters at most) that holds no strong-key prefix, no 9+ digit run, no
      credential label with a value, no random token of 16+ mixed characters or 32+ hex, no e-mail form (with
      "@", spelled, or WITHOUT "@": `mailto...`, `email:...`, an `at` piece before a domain ending in a known
      TLD, a name before one of 40 mailbox providers' domains, each with `.`, `-` or `_` as the separator) and
      is not prose folded with `-` / `.` / `_` (6+ segments of which 4+ are words). So a single made-up word
      (`zqkdbwhfmxpt`), up to five joined words (`acme-co-uk-retail-west`) and a name without a provider
      (`john-smith`) are accepted.
    - `POST /tenants/{tid}/domains` `domains[]` (and a prompt set's `competitor_domains[]`, which is in the blob
      and reaches no log): a syntactically valid host name that `sink_guard.host_refusal` passes (decision 55).
      So a host whose labels are words is accepted whatever the words say (`call.me.tomorrow.example`), as is a
      credential word followed by letters only (`password-correcthorse.example`) and a random label of letters
      only. (Review 98b76cde, decision 64: `great-at-work.example`, an `at` joined with hyphens inside ONE
      label, is refused at registration again, as at 2b26d980; it had been listed here as accepted.)
    The canary test asserts the exact (route, field) pairs where a planted short token is found afterwards (these
    three) and that it is found nowhere else; its e-mail-without-"@", separator-formatted-digits, 32-hex and
    JSON-number families are found nowhere at all. A prompt-set name and an entity source are identifiers too
    (same rules, without the folded-prose rule), but they are inside the blobs and reach no log.
    (Review 98b76cde, decision 62: the canary's e-mail-without-"@" value is `first.last.at.domain.com`, which
    is a host name with an `at` label. The guard takes it; the identifier fields still refuse it; it is found
    afterwards in exactly one pair, `POST /tenants/{tid}/domains` `domains[]`, and the test asserts that.)
50. **Guard cost per commit (re-measured).** Measured on the final code: `sink_guard.enforce` on the largest real
    record of each kind (0.4-2.9 kB each now that the content is in blobs); cold = the first call in a fresh
    process (median of 3 processes), warm = the median of 300 further calls. Log kinds (26 of 27;
    `schedule_slot_skipped` was not produced by the measurement scenario): cold 1.6-7.2 ms (median 2.6 ms; the
    largest are `audit_completed` 7.2, `audit_requested` 5.9, `schedule_created` 5.9), warm 7-538 microseconds
    (median 58). Ledger payloads (30 event types): cold 0.3-2.8 ms (median 1.1 ms), warm 2-308 microseconds. Job A
    had measured 70-80 ms of guard time on the first commit of a process for a large inline record.
51. **Replay.** Lines written before job D still replay: an `entity_created` with plain values (the seed) or
    per-text stubs, an `entity_field_set` with stubs, a `prompt_set_version` with `content` and a plain `sha256`
    (still recognised as the same content), a `log_ingest_created` with `robots_text` / `sitemap_sample`, a
    `log_chunk` with `delta`, a `log_ingest_finished` with `report` / `report_sha256`, a
    `tenant_gsc_property_set` with a `sc-domain:` string. An ingest opened before job D keeps its full totals in
    memory and takes later chunks as blobs.

## Review 2b26d980: legitimate values are no longer refused (availability)

The review passed the structural leak bar, the vault, the client view and the cost, and rejected the commit
because the guard refused LEGITIMATE values (503 `SINK_REFUSED`, skipped paid schedule slots). The guard is not
weakened for prose: each fix is a narrow closed class, a precise rule, or a keyed hash.

52. **Compliance ruling ids (MUST 1).** Compliance (38) issues `cmp-rul-` + the first 26 lower-case base32
    characters of a SHA-256 (compliance-py `service._b32`); about 1.3% of them hold 9+ digits and the strict check
    read them as a number (measured: 128 of 10,000). The guard has a closed `ruling_id` class for exactly that
    shape. The client (`compliance_client.RULING_ID`) still ACCEPTS any `[A-Za-z0-9._:-]{1,128}`; an id of any
    other shape is never written: the record carries the code `RULING_ID_HASHED` and `ruling_id_hmac` (the vault's
    log-field sub-key, field `compliance_ruling_id`, bound to the tenant; null without a vault key). The service
    never compares ruling ids (it records them), so there is no comparison to move to the HMAC.
53. **Every externally issued identifier, audited.** For each value that reaches the log or a ledger payload and
    is not derived by this service from its own machine values, the issuing format was read from the issuer and
    10,000 values of that format are generated at run time by `tests/test_review_2b26d980.py` and judged by the
    guard as a log value and as a ledger payload value: 0 refused, each row.

    | Identifier | Issuer and format | Generated | Refused |
    |---|---|---|---|
    | ruling id | compliance-py `"cmp-rul-" + _b32(...)`: 26 base32 | 10,000 | 0 (128 before) |
    | jurisdiction | compliance-py activation: `US-[A-Z]{2}`, `PR`, `GU`, `VI`, `AS`, `MP` | 681 (all) | 0 (`US-PW` before: read as a credential label; closed class `jurisdiction`) |
    | invoice id | finance-py `service.rid("inv", ...)`: `fin-inv-` + 26 Crockford base32 | 10,000 | 0 |
    | Finance record ids | finance-py `service.rid`, 28 prefixes: `fin-<prefix>-` + 26 Crockford base32 | 10,000 | 0 (closed class `finance_id`; only `fin-inv-` was a class) |
    | Finance event ids | finance-py `ledger.derived_id`: `fin-<prefix>-` + 40 hex | 10,000 | 0 (`finance_id`) |
    | Finance client reference | NOT issued by Finance: a reference it accepts, `[A-Za-z0-9._-]{1,100}`; in use `<slug>-<serial>` | 10,000 | 0 (decision 56) |
    | ledger event id / payload hash | seo-py `derived_id("evd" / "anc", ...)`; SHA-256 hex | 10,000 each | 0 |
    | service ids, vault refs, request ids, request keys | seo-py `derived_id` (11 prefixes), `vault.ref` / `blob_ref`, a UUID, `op|target|uuid` | 10,000 each | 0 |
    | log epoch, key fingerprint, vault record id, repair id | 16 / 16 / 32 / 32 hex, digits-only values included | 10,000 each | 0 (`key_fp` added to the digits-only exemption: one log hash key in about 1,800 could never create an ingest) |
    | keyed HMACs, timestamps, Search Console window dates and property hosts | 64 hex; `clock.iso`; `YYYY-MM-DD`; a host name | 10,000 each | 0 |
    | engine model id | the adapters' allowlists | 8 (all) | 0 |
    | engine spend | seo-py `str(Decimal)` to a millionth, up to 999,999.999999 | 10,000 | 0 (a run that spent or reserved 100.000000 or more was refused as a 9-digit number; closed class `amount`: `N{1,6}.NNNNNN`) |

    Engine request ids and Search Console identifiers other than the property host are not written to the log or
    the ledger (the results are in the vault). `tests/test_review_2b26d980.py::
    test_the_issuing_formats_are_the_other_services_own` reads the formats from compliance-py and finance-py and
    fails when one changes.
54. **Vault key ids (MUST 2).** A key id is written to the log in every operator-text stub. A date-stamped id
    (`2026-11`, `k-20261110-01`) started the service and then refused every audit and schedule. The guard has a
    `key_id` class: a value is accepted ONLY as the value of a `key_id` field and ONLY when it equals a key id this
    process has configured (`sink_guard.register_key_ids`: the current and the previous id; a closed set, as the
    model ids). Start validates every configured id (`sink_guard.key_id_refusal`, in `config._vault` and again in
    `SeoService.__init__`): an id that is key material (16+ hex with a hex letter), a random token, holds a
    strong-key prefix or a credential label, or more than 12 digits, refuses START with a closed configuration
    error naming the variable and the reason, never the id. A line read back (replay, export) may carry a retired
    key's id of a valid shape.
55. **Host names (SHOULD 1).** One rule, `sink_guard.host_refusal`, in every lane (the guard's `hostname` class,
    the domain fields, and the strict lane's host judgement: `secret_guard.host_verdict`; before, a tenant whose
    own domain held a credential word or a hash label had every URL of its site withheld, and its audit refused at
    the sink). A host is accepted when it is syntactically valid (labels of 1..63, 253 in all, a letters-only TLD
    of 2..24 or a punycode TLD, every `xn--` label decoding to a lower-case label of letters, marks and digits
    with a non-ASCII letter, and encoding back to itself) and:
    - no label has a strong-key prefix shape (the prefix table on the label as written, upper-cased and with its
      hyphens removed);
    - no label holds 32+ hex characters in a row with a digit among them, and adjacent all-hex labels do not add
      up to 32+;
    - the letters-and-digits pieces of a label, joined, are not a token of 24+ characters with 2+ letter-digit
      changes (a punycode label is exempt: it must decode instead);
    - no run of 9+ digits (all-digit pieces joined across `-` and `.`; a run inside one piece), and under 12
      digits in all-digit pieces in the whole host. One exception, measured: a piece that is all hex, 12..31
      characters with 2+ hex letters, keeps a digit run of at most 12 (a platform hash);
    - (REMOVED by review 98b76cde, decision 62: no e-mail form: an `at` label after the first with two or more
      labels after it, or a mailbox provider's domain after a name. These refused real hosts.)
    - no value after a credential word: a letters-and-digits piece of 6+ right after the word in the same label
      (`password-hunter2`), or as the next label when that is not the registrable name.
    A credential WORD in a label is a name (`1password.com`, `pin.it`, `secret-sale.shop`, `apikey.dev`).
    Measured false refusals (generated at run time, the test asserts at most 1% each): neutral domains 0 of
    4,000; IDN / punycode 0 of 600; platform-hash hosts (a label of 6..20 hex or letters-and-digits characters,
    alone or after words) 7 of 1,200 (0.58%: a 9+ digit run in a piece shorter than 12, or with fewer than two hex
    letters); label-word hosts 0 of 400. Still refused, 0 accepted of 2,000 each: strong-key prefixes as labels
    (6 families), 32 / 40 / 63-hex labels and 64 hex over two labels, 32- and 40-character tokens, card / SSN /
    phone digit labels (plain, dashed, dotted, after a word), `first.last.at.domain.tld`,
    `user.name.<mailbox provider>`, the value after a credential word. A uniformly random 24-character label is
    refused except when it holds almost no digits: about 0.07% accepted (13 of 20,000 in one run; the test
    bounds it at 0.5%). A random label of 21..23 letters and digits is NOT refused any more (the strict lane used
    to refuse 16+ with 4+ changes): it cannot be told from a platform hash. Not accepted, on purpose: a host
    that embeds an IPv4 address of 9+ digits (`ec2-203-0-113-125...`), a 24+ character deploy hash label.
56. **Finance client id (SHOULD 2).** Finance (31) does not issue client ids: it accepts a reference of
    `[A-Za-z0-9._-]{1,100}` (finance-py `models.CLIENT_ID_RE`). The request model takes what the guard can write
    (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`) and `svc_tenants.identifier_refused` refuses the rest. Measured on 10,000
    ids of that pattern: 7,884 accepted, 2,116 refused with 422, 0 accepted and then refused at the sink (503).
57. **The operator vault's torn tail (SHOULD 4).** Both vault files are treated alike at start: the repair is
    recorded (`operator_vault_tail_repaired`, subject `operator_vault`, the marker's counts only), the marker is
    cleared once recorded, and `/seo/v1/status` shows it (`operator_vault.tail_repair`, as
    `citation_vault.tail_repair`; `/health` is unauthenticated and answers the status only).
58. **Lines written before sink job D (SHOULD 5).** `GET /audit/export` is the one route that serves log lines:
    every served line passes `sink_guard.withhold` first, which replaces each string (a dict key too), non-finite
    float or other object that is not a machine value by the code `WITHHELD` and counts them (`withheld` in the
    answer). A line written since job D comes back unchanged. No rewrite tool is provided: every log line is
    chained by its SHA-256 and anchored on the ledger, so a line cannot be rewritten without breaking the
    anchoring. A data directory written by a release up to 8bd9e29e therefore still holds its prose ON DISK (and
    in backups): it must be retired, or the service started on a fresh directory, before the log file is handed
    to anyone. Start counts such lines and `/seo/v1/status` shows `legacy_prose_lines` (0 on a directory written
    since job D).
59. **A schedule naming a prompt set whose blob is lost (SHOULD 6)** answers 503 `VAULT_UNAVAILABLE`, as an audit
    and a citation run do; it used to be created (201) and skip every slot.
60. **Identifiers (SHOULD 9).** `secret_guard.atless_email` (in `identifier_refusal`: tenant id, Finance client
    id, prompt-set name, entity source; the host rule covers the domain fields) and `secret_guard.folded_prose`
    (tenant id and Finance client id): 422 `IDENTIFIER_WITHHELD`. Decision 49 says exactly what remains accepted.
61. **Small fixes.** The guard refuses `nan` / `inf` (JSON cannot carry them); `do[opr]_v1_` + any body of 8+ is
    a strong-key prefix in both lanes (it needed 32 hex before); the logging sink scrubs the attributes a caller
    attaches with `extra=`; the canary test pages `/audit/export` and `/audit/evidence` with the routes' real
    keys to the last line and plants four more families; `EXPECTED_SINK_REFUSALS` names each test's sinks and
    paths exactly, and a test that replaces a guard entry point (`OBSERVER`, `enforce`, `refused`, `violations`)
    without being listed fails the suite.

## Review 98b76cde: the host rule keeps every host accepted before

The review of the fix round rejected it: rules added beyond the brief refused real hosts. The governing rule of
this round is differential, against release 2b26d980 (the last one before the host rule):

- **P1 (availability).** A host that 2b26d980 took in a lane is taken now in that lane.
- **P2 (no loosening for personal-data shapes).** In the domain REGISTRATION lane (tenant domains, competitor
  domains) a telephone number or an SSN with one to three hex letters attached, 16 card digits with letters
  attached, `first-last-at-domain.tld` and `first.last-at-domain.tld` are refused as often as at 2b26d980.

62. **No e-mail form in the host rule (MUST 1).** The `at`-label rule and the mailbox-provider rule are removed
    from `sink_guard.host_refusal` (every lane). They refused `finance.yahoo.com`, `mp.weixin.qq.com`,
    `blog.naver.com`, `outlook.live.com`, `look.at.example.org`, and with them every
    write of a tenant whose domain was one (503 at the sink) and every such host in a citation (withheld as
    `PERSONAL_DATA`). Measured: on a directory built by 2b26d980 with four such tenant domains, 98b76cde
    answered the first audit with 503 `SINK_REFUSED`, and in a citation run of its own it withheld 730 of 730
    rows citing portal hosts; this release starts on that directory (`legacy_prose_lines` 0), every audit,
    citation run, schedule slot and log ingest succeeds, the same domains are registered again with 200, and
    730 of 730 rows keep their host. The
    "@"-less e-mail forms (`secret_guard.atless_email`) stay where they are identifiers: tenant id, Finance
    client id, prompt-set name, entity source. A DOMAIN is never judged by them
    (`identifier_refusal(..., atless=False)`).
63. **P1 by construction: the rule only adds.** `host_refusal` is the rule; no lane refuses on it alone. Each
    lane answers "the rule takes it, or the checks of 2b26d980 take it":
    - the guard's `hostname` class: `host_refusal(h) is None or _legacy_host_ok(h)` (the strict URL inspection
      of `https://<h>/` and the strict check of the whole host, as `_host_ok` was; a value whose last label is
      an `xn--` label was a code there and is judged as one);
    - `secret_guard.check` and the host part of `secret_guard._inspect`: a host the rule takes is kept; any
      other is judged by `_check` (whole, and in a URL label by label), exactly the code path of 2b26d980;
    - the registration lane (`svc_tenants.domain_refused`): `(rule takes it and it holds no e-mail form of
      2b26d980) or (identifier_refusal without the "@"-less forms passes and the guard takes it)`.
    Consequence, stated plainly: for a host the rule REFUSES the lanes are again as different as they were at
    2b26d980 (the strict check read the whole host, the URL inspection each label too). What holds for every
    host: the guard takes it exactly when both strict lanes keep it; a host kept for the log
    (`callum.checked_host`) is one the guard takes.
    Measured against the code of 2b26d980 loaded from git, offline (lanes: guard class, tenant-domain
    registration, competitor-domain registration, URL inspection, strict check, `callum.checked_host`):

    | Corpus | Hosts | Accepted at 2b26d980 and refused now (each of the 6 lanes) | Accepted at 2b26d980 / now (guard class) |
    |---|---|---|---|
    | generated (11 families: neutral, portal sub-domains, `at` forms, words with digits, hash labels, random labels, IDN, credential-word brands, infrastructure names, numeric, long chains) | 103,448 | 0 | 63,135 / 87,384 |
    | real well-known hosts (portal and mailbox-provider sub-domains, news, shops, SaaS, government) | 648 | 0 | 641 / 648 |

    At 98b76cde the same comparison gave 13,097 (generated) and 254 (real) hosts accepted before and refused.
64. **The hash exemption needs 4+ hex letters; `-at-` at registration as before (MUST 3 / P2).** A piece that is
    all hex, 12..31 characters, keeps a digit run of 9..12 only when it holds 4+ hex letters (it was 2+).
    Chosen by measurement between three variants on 26,000 hex labels of 6..31 characters and on the P2 shapes:

    | Variant | P2 shapes accepted by the rule (of 2,000 each) | Hash labels refused for digits |
    |---|---|---|
    | 2+ letters (98b76cde) | phone 1,343 / 1,354 / 2,000 (before / after / around), SSN 696 / 705 / 953 | 155 (0.596%) |
    | 4+ letters (chosen) | 0 in all six | 340 (1.308%) |
    | 4+ letters and a letter on each side of the run | 0 in all six | 479 (1.842%) |

    The stricter third variant was not taken: it breaks the 1% bound the existing test asserts on its
    platform-host corpus (14 of 1,200) and P2 does not need it. The cost of the chosen one is over 1% on a pure
    hex-label corpus and is not hidden: 1.308% there (0.75% on the existing test's 1,200 platform hosts, 1.10-1.12% on
    60,000 of them; 0.47-0.50% before). Every hex label of 24..31 characters was and is refused as a token (7,999 of
    the 26,000). Residual: a number with FOUR or more hex letters attached on one side
    (`<10 digits>` + `cafe`) passes the rule, as at 98b76cde; 2b26d980 refused it.
    `first-last-at-domain.tld` in a DOMAIN field is refused again (422), exactly as 2b26d980 did (its strict
    e-mail check reads the hyphens as spaces): 0 of 2,000 accepted for each of the two forms (2,000 of 2,000 at
    98b76cde). Only at registration: the same host met in a URL, a citation or crawl data is a host name (the
    guard takes it). A directory written by 98b76cde that holds such a domain keeps working (measured: audits,
    citation runs, slots, ingests succeed); submitting the domain list again with that domain in it is a 422.
65. **Client view: the verb in any case (MUST 2).** `_CV_AFTER` matched ` is `, ` was `, ` are `, ` would be `,
    ` will be `, ` remains ` in lower case only: `the password IS <value>` was shown to the owner. All six are
    matched in any case mix. The other alternations of section 5 were checked for the same gap: `_CV_SCRIPT`
    (the labels in scripts without word spacing) holds three labels in cased scripts (Cyrillic, Greek) and
    matched them in lower case only, while the strict lane folds case: fixed (`re.IGNORECASE`). Not gaps:
    `_CV_LABEL` (built per letter in both cases), `_CV_BEARER`, the SSN label, the spelled `at` / `dot` / `sign`
    forms, the number words (all case-insensitive already), `_CV_PEM` (a PEM header is upper case by format).
    Found and NOT fixed (not a case gap): a Greek label followed by its inflection ending before the separator
    is not matched in either case. False masking for a label and a value on neutral prose (636 units of the
    utility corpus): 0 before and after, as written, in Title Case and in UPPER CASE. On 20 hand-written
    sentences that put a credential word before the verb (`The token was revoked`): 5 masked before and after
    as written; in Title Case 0 before, 5 after (the same 5); in UPPER CASE 0 before, 14 after (an all-capitals
    word after ` WAS ` / ` ARE ` counts as a value, as `HunterX` does after ` was `).
66. **Start (SHOULD 1).** The legacy-line count ran the guard over every log line at start. It is computed on the
    first status read (`legacy_prose_lines`, outside the service lock), over the lines the log held at start,
    and remembered. `SeoService(...)` on a 1,200-line log (1.78 MB), 5 cold starts each, same box, seconds:
    2b26d980 0.137-0.179 (median 0.167); 98b76cde 0.216-0.253 (median 0.237, 1.42x); this release 0.125-0.184
    (median 0.153, 0.92x).
67. **Host token capacity (SHOULD 2).** In the rule (so in every lane, since 2b26d980 refused these too):
    - a token of 24+ characters split over two ADJACENT labels: the non-word pieces of two adjacent labels are
      read together (0 of 2,000 accepted by the rule when each half holds letters and digits; 98b76cde: all);
    - 32 hex characters split by a WORD label (`<16 hex>.shop.<16 hex>`): a letters-only label between all-hex
      labels no longer ends the run (0 of 2,000; 98b76cde: 1,972);
    - a value after `password-is-`: `is`, `was`, `are`, `equals` may stand between the credential word and its
      value (0 of 2,000; 98b76cde: 2,000).
    False refusals added by these three on the real-world corpora: 0 (neutral 8,980, IDN 8,212, credential-word
    brands 8,900, portal sub-domains 9,620, the 648 real hosts). On synthetic corpora they refuse what they
    are meant to: two adjacent hash labels adding up to 24+ (`<hash>.<hash>.<platform>`).
    Residual of the known limit (decision 49), not fixed:
    - a punycode label. The label must decode to a lower-case label with a non-ASCII letter and encode back to
      itself (it did already at 98b76cde). A random 31-character token after `xn--` still passes for about one
      token in six (313 of 2,000; 3 at 2b26d980): it decodes to a valid label of CJK letters, which cannot be
      told from a long Chinese name (270 of the 313 are single-script). A real IDN corpus: 0 of 8,212 refused;
    - a token split over two labels when one half is letters only (a word): 56 of 2,000 (5 at 2b26d980);
    - what P1 keeps: a letters-heavy 32-character label with its few digits together (2 of 2,000 random
      32-character tokens; 2b26d980 took the same 2), and a uniformly random 24-character label (about 1.5% at
      2b26d980, so now).
68. **Tests (SHOULD 3 / 4).** `tests/test_review_98b76cde.py`. The two surviving mutants are killed: the export
    serving a line's `kind` / time that is not a machine value; the citation run's line dropping
    `ruling_id_hmac`. The random-label refusal is asserted at the rule's own threshold, 2,000 generated labels
    on each side: 23 characters accepted, 24 refused (the tests asserted it at 28).
69. **Vault key ids that start now refuses (SHOULD 5, migration).** A record names its key id, the key is looked
    up by that id, and the id is part of the record's associated data (`vault._aad`, `VaultKey._material`). So:
    - an id cannot be renamed. The same key bytes under another id do not open the records written under the
      old id (verified: the service starts, new writes succeed, every older record is `vault_unavailable`);
    - a refused id cannot be kept as the PREVIOUS key either: start validates both ids (`config._vault`,
      `sink_guard.register_key_ids`) and refuses either one that is key-shaped (16+ hex with a hex letter, a
      random token, a strong-key prefix, a credential label, more than 12 digits).
    A deployment whose key id is of such a shape therefore cannot read its existing vault records with this
    release, and no re-encryption helper exists. What the operator must do: BEFORE upgrading, with the release
    that wrote them (2b26d980 or earlier) and the old id, read out whatever must be kept (reports, citation
    evidence, prompt sets, entity values); then start this release with a new accepted id (for example
    `k-2026-12`) and a new key, and enter the prompt sets and entity values again (those whose text is in the
    old records answer 503 `VAULT_UNAVAILABLE`). No live deployment holds a vault key yet (Deployment below).
70. **Known limits, restated.**
    - A tenant id or a Finance client reference that holds a credential word followed by more
      (`token-coffee`, `secret-santa-2`) answers 422 `IDENTIFIER_WITHHELD`: the identifier check has no
      "a word is a name" rule (the host rule has one, for hosts).
    - Infrastructure-shaped hosts are refused by design in every lane (as a tenant domain: 422; in a citation:
      the host is withheld): an ELB name with its 9+ digit serial, a Cloud Run URL with a project number, an
      EC2 public DNS name or `nip.io` name embedding an IPv4 address of 9+ digits, a deploy-hash label of 24+.

## Threat model

| Threat | What stops it | What does not |
|---|---|---|
| Disk read (backup, volume snapshot, a stolen copy of the data dir) | AES-256-GCM; the key is not on disk; tenant tags are keyed, so tenant names cannot be confirmed by hashing guesses | Record count, sizes and write times are visible |
| Log read | The vault writes no log line; a citation run's line keeps a blob ref, ONE keyed HMAC-SHA256 of the whole result (tenant- and field-bound), an outcome code and counts (decision 36) | Equality of two whole results of the same tenant under the same key is visible (same HMAC); record counts and sizes |
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
