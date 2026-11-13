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
    2b26d980 (the strict check read the whole host, the URL inspection each label too). What holds: the guard
    takes a host exactly when both strict lanes keep it, EXCEPT a value whose last label is an `xn--` label
    that is not an IDN label: the guard judges it as a code (the strict check of the whole value, as at
    2b26d980) and takes it, while the URL inspection reads that label as a token and withholds the host
    (corrected by review ffd28ca8; measured on 8,100 generated hosts, 27 families of 300: the guard takes and a
    strict lane withholds 285, every one in the two families ending in such a label, 94 and 191 of 300; both
    strict lanes keep and the guard refuses: 0). A host kept for the log (`callum.checked_host`) is one the
    guard takes.
    Measured against the code of 2b26d980 loaded from git, offline (lanes: guard class, tenant-domain
    registration, competitor-domain registration, URL inspection, strict check, `callum.checked_host`):

    | Corpus | Hosts | Accepted at 2b26d980 and refused now (each of the 6 lanes) | Accepted at 2b26d980 / now (guard class) |
    |---|---|---|---|
    | generated (11 families: neutral, portal sub-domains, `at` forms, words with digits, hash labels, random labels, IDN, credential-word brands, infrastructure names, numeric, long chains) | 103,448 | 0 | 63,135 / 87,384 |
    | real well-known hosts (portal and mailbox-provider sub-domains, news, shops, SaaS, government) | 648 | 0 | 641 / 648 |

    At 98b76cde the same comparison gave 13,097 (generated) and 254 (real) hosts accepted before and refused.

    Corrected by review ffd28ca8: the table compares the lane FUNCTIONS. At ROUTE level the competitor-domain
    lane is not differential. The prompt-set route of 2b26d980 judged a competitor domain by the strict check
    and `secret_guard.identifier_refusal` only (the set is one vaulted blob; the guard never saw the domain);
    the route now judges it by `svc_tenants.domain_refused`, which also asks the guard. Measured through both
    routes on the same 8,100 generated hosts: 1,164 answered 201 at 2b26d980 and answer 422
    `IDENTIFIER_WITHHELD` now, in seven families: a random label of 24 characters (279 of 300), of 28 (257), of
    32 (3), an all-hex label of 24 (19), a strong-key-prefix shape (26), an AWS key-id shape (299), a token
    split over two labels (281). Mostly key and token shapes; a random label of 24+ letters and digits is also
    what some platforms issue as a deploy id (decision 70). The other 20 families (neutral names, platform
    deploy ids of 6..23 characters, preview numbers, hex labels of 12..20, digit runs): 0. The tenant-domain
    route: 0 of 8,100.
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
    Added by review ffd28ca8 (measured on the final code):
    - A directory written by 98b76cde (never deployed) may hold a domain this release's guard refuses: a
      platform hash label with 2 or 3 hex letters and a digit run of 9..12 (generated labels of 11..14
      characters: the rule, the guard and the registration lane of 98b76cde took 1,501 of 2,000 with 2 letters
      and 2,000 of 2,000 with 3; this release takes 0 of either, and all 2,000 with 4 letters). On such a
      directory this release starts (the lines naming the domain are counted in `legacy_prose_lines`) and a
      write of that tenant that names the domain answers 503 `SINK_REFUSED` (measured: a citation run on each
      of two such domains). A citation run on another domain of the same tenant succeeds; the same domain list
      submitted again is a 422; the list without those domains is accepted (200), which is the way out.
    - The false-refusal cost is not even over label lengths. All-hex labels refused for digits, 2,000 labels
      per length: none at 6..8; 35, 38, 58 at 9..11; 57, 70, 73, 76, 65, 63, 58, 33, 41 at 12..20 (536 of
      18,000, 2.98%); 43, 22, 21 at 21..23; 24..31 are refused as tokens. Over 6..31: 753 of 52,000 (1.448%;
      98b76cde: 339, 0.652%). The cost at 12..20 is more than twice the average the table above reports.
      Corrected by review cd0a6c7d (re-measured on that review's fix): the 52,000 of that percentage include
      the 16,000 labels of 24..31 characters, every one of which is refused as a token whatever its digits
      (16,000 of 16,000), so they cannot be refused for digits. Over the 36,000 labels of 6..23 characters,
      the ones the digit rule decides, the same 753 are 2.092% (98b76cde: 339, 0.942%).
65. **Client view: the verb in any case (MUST 2).** `_CV_AFTER` matched ` is `, ` was `, ` are `, ` would be `,
    ` will be `, ` remains ` in lower case only: `the password IS <value>` was shown to the owner. All six are
    matched in any case mix. The other alternations of section 5 were checked for the same gap: `_CV_SCRIPT`
    (the labels in scripts without word spacing) holds three labels in cased scripts (Cyrillic, Greek) and
    matched them in lower case only, while the strict lane folds case: fixed (`re.IGNORECASE`). Checked and
    case-insensitive already: `_CV_BEARER`, the SSN label, the spelled `at` / `dot` / `sign` forms, the number
    words; `_CV_PEM` (a PEM header is upper case by format).
    Corrected by review ffd28ca8: this decision said there was no other case gap. There was one: `_CV_LABEL`
    is built per letter in both cases, but the view it reads was folded by a table that turned the upper-case
    stroked L into a capital I, so the Polish label in upper case was shown; and the Hindi and Thai labels
    matched in no case at all. What holds now is decision 71: one normalisation for a label entry and for the
    text, and a test over every entry in every spelling (as listed, lower, UPPER, Title, natural diacritics or
    marks present and absent, NFC and NFD, before a colon, an equals sign and every case mix of every verb): 0
    misses in the client view and in the strict lane. The Greek label in its natural nominative form is matched
    now; what remains is listed in decision 71 (another inflected form of the Russian and Greek labels).
    Corrected by review cd0a6c7d (decision 72): "marks absent" no longer holds for the Japanese and the Hindi
    label, whose marks spell the word; demanding it made ordinary words read as those labels.
    False masking for a label and a value on neutral prose (636 units of the
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
    Added by review ffd28ca8: further residuals of the same owner-accepted known limit for identifier fields
    (decision 49), each measured on 2,000 generated hosts on the final code (accepted by the rule, by the
    guard and by the registration lane alike; the guard and the registration lane of 2b26d980 took none):
    - a token whose halves are separated by a WORD label (`<12 letters and digits>.shop.<12>`: 1,999;
      `<16>.shop.<16>`: 2,000). Only 32 hex characters split by a word label are refused;
    - a credential word, a link word that is not `is` / `was` / `are` / `equals`, and a value
      (`password-for-<value>`, `-of-`, `-to-`, `-equal-`, `-set-`, `-eq-`: 2,000 each; with `-is-`: 0);
    - a credential word and 4 to 8 digits (`pin-<4 digits>`, `password-<8 digits>`: 2,000 each at 4, 6 and 8;
      at 9 digits: 0);
    - a telephone number or an SSN with FOUR or more hex letters attached on either side (2,000 each), or
      split by a word (`<3 digits>-call-<3>-<4>`, `<6>-ext-<4>`, `<3>-<2>-and-<4>`: 2,000 each).
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
    Added by review ffd28ca8 (measured: a service restarted with a new key under a new id): two more steps
    follow a key loss. SCHEDULES must be created again: a schedule's paths are in the old records, its slot is
    refused at every tick (`refused` in the tick summary, `vault_unavailable: true` in its view), and a new
    schedule or audit naming the old prompt set answers 503 until the set is entered again. A NEW AUDIT of a
    domain must complete before a log ingest of that domain: an ingest reads the sitemap sample of the
    domain's latest audit, and while that report is in the old records the ingest answers 503
    `VAULT_UNAVAILABLE` (a domain never audited is not affected: 201).
70. **Known limits, restated.**
    - A tenant id or a Finance client reference that holds a credential word followed by more
      (`token-coffee`, `secret-santa-2`) answers 422 `IDENTIFIER_WITHHELD`: the identifier check has no
      "a word is a name" rule (the host rule has one, for hosts).
    - Infrastructure-shaped hosts are refused by design in every lane (as a tenant domain: 422; in a citation:
      the host is withheld): an ELB name with its 9+ digit serial, a Cloud Run URL with a project number, an
      EC2 public DNS name or `nip.io` name embedding an IPv4 address of 9+ digits, a deploy-hash label of 24+.

## Review ffd28ca8: a label entry and the text share one normalisation

The review found the client view showing a labelled credential (a label, a separator, a value) to the owning
tenant for three label forms, and the strict lane missing two of them. The owner asked for the cause to be
fixed, not the three forms.

71. **One normalisation for both sides of every credential-label list (MUST 1).** The cause: a list entry was
    compared as written with a text that had been folded. An entry holding a combining mark the inspection
    views remove (the Hindi and Thai labels: the views drop the virama, the vowel signs and the tone marks)
    could never equal the text, in either lane; and the client view folded its text with a table of its own
    that read the upper-case stroked L as a capital I, so the Polish label in upper case read as another word
    (lower case, Title Case and plain-ASCII upper case were masked; the strict lane, which folds through
    `lookalikes`, flagged all of them).
    - `secret_guard._label_norm(text, latin)` is THE normal form, applied to every list entry and to the
      text it is matched against (in the strict lane, and for the script labels in the client view; the
      client view's text view for the Latin labels is tied to it, below): invisible characters out and NFKC,
      case folded (the dotted capital I, the final sigma and the sharp s included), NFKD with every combining
      mark dropped (accents, the Greek tonos, Arabic and Hebrew points, the Devanagari virama and vowel signs,
      Thai vowel and tone marks, the kana voicing marks) and the Arabic tatweel with them, NFC again (a Hangul
      syllable stays whole). Two alphabets: for the Latin lists (`_JOINED`, `_PIECES`) lookalike letters are
      read as the ASCII letters they resemble, before and after the marks are dropped; for the script list
      (`_SCRIPT`) the script is kept. The letters NFKD does not decompose: the stroked l, o and d and the dotless i are
      folded by `lookalikes`, the sharp s reads `ss` by case folding; the ae / oe ligatures are not folded
      (no entry and no natural spelling of one holds a ligature or a sharp s; the test asserts it, and the
      client view's table leaves the sharp s as written, so an entry holding one would stop the import).
      **Corrected by review cd0a6c7d (decision 72). Two statements of this bullet were wrong.** (1) "Every
      combining mark dropped" was true of the ENTRIES and not of the text: the inspection views drop the
      non-spacing marks only, and compose the kana. For the script list the form was therefore stronger
      than what a text is read through, and ordinary Japanese and Hindi words read as labels. It is no longer
      what the script list uses: decision 72. (For the Latin lists it stands: their keys are ASCII letters
      and the Latin lanes read letters only.) (2) "No natural spelling of an entry holds a sharp s" was
      false: the German entry is written with one in the pre-1996 spelling (lower case, Title Case, and with
      the capital sharp S), the client view left the letter as written and showed the label and its value.
      The import-time check only ran each entry as listed and in upper case, which never produces the
      letter, so it could not see it. Both are fixed in decision 72.
    - The matching tables are DERIVED from the lists through that function at import (`_label_keys` ->
      `_JOINED_KEYS`, `_PIECE_KEYS`, `_SCRIPT_KEYS`, `_CV_SCRIPT_KEYS`, and from them `_JOINED_RX`,
      `_PIECES_IN`, `_SCRIPT_RX`, `_CV_LABEL`, `_CV_SCRIPT`). An entry no text could match after it stops
      the import (a Latin entry whose key is not ASCII letters, a script entry without a non-ASCII letter, a
      piece label of two words, a key of one letter): an entry cannot be added in an unmatchable form, and
      one written with its diacritics, capitals or spaces is listed as its key.
    - The strict lane reads the letters of the normal form (`_letters_view`, `_fold_piece`, `label_only`,
      `_script_letters`).
    - The client view's rules read case (a camelCase boundary before a label, a capital inside a value after
      ` was `), so its text views keep it. For the SCRIPT labels the view is the normal form one character at
      a time in the character's case (`_cv_script_view`); the label is looked for there and what follows it
      (the separator and the value) is read on the view as written (`_cv_script_ends`), exactly as before.
      For the LATIN labels the view (`_cv_label_view`) is the client view's own character table, kept as it
      was (NFKD, the marks dropped, a lookalike letter as the ASCII letter it resembles in its own case) with
      the one wrong letter corrected: an upper-case LATIN letter that folds to `l` is an L (a lookalike of a
      capital I from another script, the Greek iota or the Cyrillic palochka, still reads I). It was not
      replaced by the normal form: a first version that did so showed, in a differential test against
      ffd28ca8, forms the client view masked before (a label in Greek or Cyrillic capitals whose upper-case
      lookalike differs from the lower-case one, a value glued to a word with a spacing mark). Instead the
      two are tied at import: `_cv_latin_keys` runs every Latin entry through the client view's table, as
      listed and in upper case, and stops the import unless the result is the entry's key; the test checks
      the same for every character of every natural spelling.
    - Two `_SCRIPT` entries are stems (Russian, Greek): the strict lane finds a stem inside the word, the
      client view needs the label to end before the separator. The nominative form of each is listed for
      the client view (`_CV_SCRIPT_FULL`; the Russian one always was, as a literal; the Greek one is new).

    **What the test guarantees** (`tests/test_review_ffd28ca8.py`, the property for every entry, not three
    cases): 96 entries (`_JOINED` 44, `_PIECES` 27, `_SCRIPT` 23, `_CV_SCRIPT_FULL` 2) in 398 spellings (as
    listed, lower, UPPER, Title, capitalised, the natural spelling with its diacritics, spaces or marks, the
    marks absent, the Turkish dotted capital, the Arabic elongation; each in NFC and NFD), each before 344
    separators (`: `, `:`, `=`, ` = `, and every case mix of ` is `, ` was `, ` are `, ` would be `,
    ` will be `, ` remains `) with a value generated at run time: 136,912 cells, each one masked in the client
    view AND flagged as a label by the strict lane.
    Corrected by review cd0a6c7d: the "marks absent" spelling is no longer made for the Japanese and the
    Hindi entry (decision 72: without its voicing marks or its vowel signs the word is another word). The
    same test now holds 396 spellings and 136,224 cells, 0 missed in either lane; the sharp-s spellings,
    which it never held, are in the test of decision 72.

    | The same matrix | Client view: cells shown (entry spellings, entries) | Strict lane: cells not flagged as a label (entry spellings, entries) |
    |---|---|---|
    | ffd28ca8 | 8,256 (24, 8) | 7,912 (23, 7) |
    | this release | 0 | 0 |

    At ffd28ca8 the misses were: the Polish label in upper case with the stroked L (client view only); the
    Hindi and the Thai label in every spelling; the Greek label in its natural form (the tonos and the final
    sigma) in every spelling; the three Arabic and Persian labels with a tatweel; the Japanese label with its
    voicing marks absent. With the tables of ffd28ca8 put back, the test reports the reported entries and a
    citation run shows its owner the values.
    End to end: a citation run whose answer text, cited title and hint carry the three reported forms and 20
    other entries in upper case (69 planted values, 72 evidence fields read by the owning tenant): 72 masked
    with an alert, 0 values shown.

    **Residuals (asserted in the test, not hidden).**
    - An inflected form of the two stem entries other than the listed nominative, before a separator and a
      value: the client view shows it. Russian (genitive, instrumental, dative): 3 of 3 shown, 3 of 3 flagged
      by the strict lane. Greek with the FIRST word inflected (genitive, accusative): 2 of 2 shown and 2 of 2
      not flagged, in either lane, as before (the stem is the nominative of the first word).
    - The word `key` alone is not a list entry: the strict lane has its own rule for it (`key`, a separator,
      a value); the client view masks it only inside a listed label (`api key`, `secret key`). Unchanged.
    - Not a label finding and not changed: an all-capitals sentence can read as a strong-key shape (the AWS
      key-id rule on the text with its spaces removed). 39 of 320 generated Polish sentences in UPPER CASE
      are masked for it, and 39 at ffd28ca8.

    **What the normal form widens (on purpose):** in both lanes the Japanese label without its voicing
    marks and an Arabic or Persian label with a tatweel; in the strict lane a lookalike letter that carried
    a mark (a Greek letter with a tonos standing for a Latin one), which the client view already read.
    Corrected by review cd0a6c7d: this list was incomplete and its first item was a mistake. Dropping the
    voicing marks did not only read the Japanese label without them: it read every word that differs from
    it by voicing marks alone, an ordinary loanword among them; and dropping the Devanagari vowel signs
    read the Hindi label's consonants with ANY vowel signs, across word boundaries. Decision 72 withdraws
    both and states what is widened now.
    Differential test against ffd28ca8, 120,000 generated texts (labels in every script of the lists with
    case, NFD, lookalike, mark and separator mutations, values in several scripts, neutral words around
    them): the strict lane flags 14,846 texts it let through and none fewer (92 more change their reason to
    a label); the client view masks 9,073 more for a label and a value and none fewer; no other client-view
    finding changes.

    **No regression on the other side** (the same corpora at ffd28ca8 and at this release; identical in
    every cell):

    | Corpus (units) | Client view masked: as written / Title Case / UPPER CASE | Of them for a label and a value |
    |---|---|---|
    | generated English e-commerce prose, some with a credential word in prose (3,200) | 0 / 0 / 0 | 0 / 0 / 0 |
    | the utility corpus of the earlier rounds, answers and their sentences (324) | 0 / 0 / 2 (strong-key shape) | 0 / 0 / 0 |
    | generated Polish (320) | 0 / 0 / 39 (strong-key shape, above) | 0 / 0 / 0 |
    | generated Hindi, Thai, Japanese, Russian, Greek (320 each) | 0 / 0 / 0 | 0 / 0 / 0 |

    Mixed-case rise: 0.0 points. The strict lane on the same corpora is unchanged too (English: 935 of 3,200
    withheld in each casing, 870 for a credential word in prose, as it always did; the utility corpus: 20 / 20
    / 22; the other languages: 0, and 39 Polish in UPPER CASE). The entries whose key differs from the entry
    as listed (Hindi, Thai, Japanese): the key is found in 0 of 960 generated units of its language each, and
    in 0 of 2,207 (Hindi), 2,412 (Thai) and 2,440 (Japanese) real-language strings (the CLDR names shipped
    with Babel) and 0 of 200,000 random joins of three of them; no script key in any of the Arabic, Persian,
    Hebrew, Greek, Russian, Korean and Chinese ones (2,205..2,472 strings each). The generated corpora have
    small vocabularies (about 45 words a language) and no larger real-text corpus was available offline: a
    rare ordinary word that holds a stripped key is not excluded by these figures.
    Corrected by review cd0a6c7d: with real frequency lists such words are found at once (decision 72: one
    Japanese word, 182 Hindi word pairs, 60 Thai word pairs). The figures above said nothing about them. The client view asks for a
    separator and a value after the label, as before; the strict lane withholds on the label alone, as for
    every other entry.
    Cost, client view, microseconds of CPU per unit, the best of three interleaved runs of each release on
    one box (a single run of the same code can differ by 20% there): English 64.9 -> 64.2, the utility
    corpus 138.2 -> 138.8, Polish 103.8 -> 103.5, Hindi 51.1 -> 50.8, Thai 25.4 -> 25.1, Japanese 22.8 ->
    23.8 (+4.4%, the largest), Russian 102.4 -> 100.7, Greek 115.2 -> 113.5.
    `tests/test_secret_guard_perf.py` passes.

## Review cd0a6c7d: the key of a label is its entry read by the text's own view

A scoped review of cd0a6c7d (decision 71) found that the fix had made the script labels' keys LOOSER than the
text they are looked for in (MUST A), and that the German password label written with the sharp s was shown
in the client view (MUST B, older than cd0a6c7d). The owner's two standing requirements: fix the cause, not
the cases; and do not over-tighten (ordinary text, real sites and real URLs are not flagged, masked,
withheld or refused).

72. **A key is made by the function that reads the text, never by a stronger one (MUST A).** Decision 71 put
    every entry and every text through one normal form, but that form did more than the inspection views
    do to a text: it dropped EVERY combining mark (the views drop the non-spacing ones) and decomposed the
    kana (the views compose them). Measured at cd0a6c7d against ffd28ca8:
    - Japanese: the ordinary katakana loanword for "buzzword" differs from the password label by voicing
      marks only and read as the label, as did 425 more two-word joins of the frequency list than at ffd28ca8;
    - Hindi: the label's consonants matched with any vowel signs and across a word boundary: 182 two-word
      joins of ordinary words (the word for "near" before the loanword "ward" is the commonest);
    - a text holding one was flagged by the strict lane, a title holding one was masked for its owner, a URL
      holding one was not fetched (`url_has_secret`), a log path template lost the segment, a Unicode-form
      host holding one was refused in the URL, check and `checked_host` lanes.
    The demand behind it ("every entry is matched with its marks absent", decision 71) was wrong for scripts
    whose marks distinguish words and is **withdrawn**.

    **What the code does now** (`secret_guard.py`, section 2a and section 5):
    - The key of a script entry is the entry passed through the functions a text passes through
      (`_label_keys`): its inspection view (`normalise`: NFKC, the non-spacing marks and the invisible
      characters removed) and then the letters-only script view (`_script_letters`). The client view's
      key is the letters of its own script view of the entry (`_cv_script_view`). The import fails unless
      the two are equal, unless reading a key again gives the key, and unless both lanes read the entry as
      listed, in UPPER CASE and in Title Case as that key (`_cv_script_keys`, `_check_label_lists`, run at
      import).
    - The script view of one character is `_script_fold`: case folded (the final sigma is a sigma), an
      ACCENT dropped from a precomposed letter, the tatweel dropped. An accent is a mark of canonical
      combining class 200 or more (`ACCENT_CLASS_MIN`: above, below, attached, the iota subscript). The
      fixed-position classes below it are part of the letter and are kept: the kana voicing marks (class
      8), the nukta (7). Nothing is decomposed that the inspection view composed. Over every letter of the
      first two planes the test checks that the fold keeps the base letter and removes accent-class marks
      only; it changes Greek, Cyrillic and Arabic letters among the label scripts and no kana, Devanagari,
      Thai, Hangul, Han or Hebrew letter.
    - An entry whose inspection view holds a SPACING mark (the Hindi one: its long-a vowel sign) cannot be
      read by its letters, since the letters-only view would read it without part of its spelling. It is a
      **word label**: its key is the inspection view of the entry and it is looked for in the inspection
      view of the text itself, as one unbroken written word (`_SCRIPT_WORD_RX`, `_CV_SCRIPT_WORD`). A
      space, a hyphen, an underscore or any other character between two of its characters is in the view,
      so two ordinary words side by side are never the label; a vowel sign more, less or other than the
      entry's is another word. Only a tatweel, which spells nothing in any script label, may stand inside.
      An entry that could not be such a word (two written words, a digit, a letter the fold would change)
      stops the import.
    - The Latin lists are read as before (`_label_norm`: lookalikes, lower case, marks dropped: their keys
      are ASCII letters).

    **What stays closed, and why** (each asserted one by one in the test; none relies on a step applied
    to the entry only):

    | Closed at cd0a6c7d | Still read as the label because |
    |---|---|
    | Arabic and Persian label with a tatweel | `_script_fold` drops the tatweel from the text |
    | Arabic label with vowel points, Hebrew label with points | `normalise` drops non-spacing marks from the text |
    | Thai label as listed, NFC or NFD | `normalise` drops its vowel and tone marks (non-spacing) from the text; so does the key |
    | Hindi label, NFC or NFD, with a joiner or non-joiner in its conjunct | `normalise` drops the virama (non-spacing) and the joiners; the long-a sign stays in both |
    | Greek label with the tonos and the final sigma, in capitals with or without the tonos | `_script_fold`: accent class, case folding |
    | Japanese label in NFD or in half-width katakana | `normalise` (NFKC) composes it; the key is the composed entry |
    | Polish label in capitals with the stroked L, Turkish dotted capital | the Latin views (decision 71), unchanged |

    **The Greek stem.** The Greek entry is a stem ending in the first letters of its second word. Once the
    tonos is folded (decision 71), the stem as it was read "code" followed by any word beginning with the
    same four letters ("attack", "offends", "insulting": 13 ordinary pairs in the frequency list) as the
    label. It is two letters longer now and ends inside the stem of "access"; every form of the label
    (nominative, the second word in either genitive, capitals, unaccented) is still read.

    **The sharp s (MUST B).** The client view's Latin table reads U+00DF as `ss` and U+1E9E as `SS`
    (`_CV_SHARP_S`), as the strict lane's form always did. The import-time check now runs every Latin
    entry through its natural spellings (`_natural_spellings`: as listed, UPPER, Title, and for an entry
    holding `ss` the three sharp-s spellings) in BOTH lanes, not only as listed and in upper case; these
    two code points are the only ones whose strict-lane form is several ASCII letters and which the client
    view left unfolded (scan of the first three planes).

    **M1, the label matrix** (every entry x natural spellings x 344 separators, a run-time value; 96
    entries, 498 spellings now that the sharp-s spellings of the 16 entries holding `ss` are in it,
    171,312 cells):

    | | Client view: cells shown (entry spellings, entries) | Strict lane: cells not flagged as a label |
    |---|---|---|
    | ffd28ca8 | 39,904 (116, 24) | 7,912 (23, 7) |
    | cd0a6c7d | 31,648 (92, 16): every sharp-s spelling of every entry holding `ss` | 0 |
    | this release | 688 (2, 2) | 688 (2, 2) |

    The 688 are the two withdrawn spellings in both lanes: the Japanese entry with its voicing marks
    absent and the Hindi entry with its vowel signs absent (344 separators each). The three forms of the
    review of ffd28ca8 (Polish capitals with the stroked L, Hindi, Thai) and the sharp-s spellings: 0.

    **M2, collisions with real word lists.** Every word of a frequency list (wordfreq 3.1.1 for Japanese,
    Hindi, Arabic, Persian, Russian, Greek, Polish, German; pythainlp 5.4.0 for Thai) and every two-word
    join that can hold a label key (found by key substring across the boundary under a skeleton looser
    than any release's matching, not by sampling), through the strict lane and, before `: ` and ordinary
    text, the client view. Counted as the review counted them: flagged or masked, and the text does not
    hold a listed entry (so the label words themselves are out); "in script" means both words are written
    in the language's own script. Rates are the sum of the two unigram frequencies' products.

    | Script (words; candidate joins) | Counted | ffd28ca8 | cd0a6c7d | this release |
    |---|---|---|---|---|
    | Japanese (214,960; 38,779) | words, strict / client view | 0 / 0 | 1 / 1 (the loanword) | 0 / 0 |
    | | joins in script, strict (rate) / client view | 3,487 (2.7e-10) / 0 | 3,912 (2.4e-9) / 357 | 0 / 0 |
    | Hindi (26,653; 187) | words | 0 / 0 | 0 / 0 | 0 / 0 |
    | | joins | 0 / 0 | 182 (6.4e-8) / 131 | 0 / 0 |
    | Thai (88,569; 180) | words | 0 / 0 | 0 / 0 | 0 / 0 |
    | | joins | 0 / 0 | 60 (2.0e-10) / 28 | 60 (2.0e-10) / 28 |
    | Arabic (620,701; 78,285) | words; joins | 0 / 0; 0 / 0 | the same | the same |
    | Persian (31,389; 10) | words; joins | 0 / 0; 0 / 0 | the same | the same |
    | Russian (713,447; 287,999) | words | 3 / 3 | 3 / 3 | 3 / 3 |
    | | joins | 0 / 0 | 0 / 0 | 0 / 0 |
    | Greek (46,916; 81) | words | 2 / 22 | 2 / 22 | 2 / 22 |
    | | joins | 0 / 0 | 22 (1.3e-9) / 1 | 9 (3.7e-10) / 1 |
    | Polish (453,320; 8,696,289) | words flagged, strict / client view (label words included) | 30 / 30 | 30 / 30 | 30 / 30 |
    | | joins flagged (space join; label words included) | 1,318,780 / 16,017 | 1,318,780 / 16,017 | 1,318,780 / 16,017 |
    | German (634,502; 12,964,625) | words flagged (label words included) | 226 / 49 | 226 / 49 | 226 / 49 |
    | | joins flagged (space join; label words included) | 2,464,717 / 5,200 | 2,464,717 / 5,200 | 2,464,717 / 5,200 |

    Notes on the table. The Japanese joins at ffd28ca8 are voiced neighbours of the unvoiced "token" entry,
    which that release also looked for in a mark-stripping view; they are gone with the voicing marks kept.
    The Russian and Greek words are older than this round and the same at all three releases: three
    Russian abbreviations and two Greek ones whose letters are lookalikes of a short Latin label, and 20
    Greek words ending in rho, omega after an accented letter, masked by the client view before a
    separator (below). The nine Greek joins now are three spellings of the label itself with one word
    unaccented and the six of the residual below. For Polish and German the joins are not classified (the
    Latin lane is not changed): the flagged sets are identical, index for index, at ffd28ca8 and at this
    release (and for German at cd0a6c7d; the Polish run of cd0a6c7d was made with an earlier filter of
    the harness and agrees in every count). The German list holds no word written with a sharp s (the package spells it `ss`), so the
    sharp s is measured on a hand list instead: of 96 ordinary German words written with it, 95 are read
    as before in both lanes and in a URL; the 96th is the pre-1996 spelling of the listed short label
    ("Pass"), flagged by the strict lane as before and now masked before a separator too. A two-ASCII-word
    pair in a non-Latin list (English words in it) is skipped; four splits of the candidate search were
    capped (Japanese 2, Arabic 1, Russian 1), and 15 / 23 for Polish / German (the most frequent words
    kept on the larger side).

    **Corrected by review cbdf5e78 (decision 73): the statement below ("two named residuals remain") was
    untrue.** The search behind it read two-word joins only, of nine languages, and counted only joins that
    did not hold a listed entry. Measured in decision 73 (18 languages, 20,000 words each, two- and three-word
    joins, spaced, hyphenated and glued), cbdf5e78 flagged in the strict lane, against ffd28ca8: by its script
    rule, Greek 5,049 joins (1,681 triples in each of the three forms, "code" + "to" + a word in bas- among
    them, and 6 pairs), Hindi 24,609 (4 glued pairs, 24,604 glued triples, 1 word: the label inside a longer
    written word), Thai 32,208 (every Thai candidate whose letters, marks ignored, hold the label); and by its
    LATIN rules, through the second lookalike pass of cd0a6c7d reading the accented letters of ordinary
    words as Latin letters, Greek 427,844 (triples of ordinary Greek words spelling "apikey", "katasandi",
    "haslo"...), Persian 5,547, Ukrainian 5,757, Arabic 492. None of these is a residual: decision 73 closes
    them, except the classes it names.

    Japanese 0, Hindi 0. Two named residuals remain, each below 1e-9 per adjacent pair and each asserted in
    the test:
    - **Thai, 60 joins (2.0e-10).** The inspection views drop the Thai vowel and tone marks (they are
      non-spacing), from the text and therefore from the key; Thai is written without spaces. Sixty joins
      of two words of the list ("code" before "loincloth", for one) read as the label
      through that view; no single word does. Taking the marks back into the inspection view would change
      what every other rule reads; not done here. (120 more joins are the label's own two words.)
    - **Greek, 6 joins (1.9e-10).** "Code" before a derivative of the label's second word ("accessible",
      "accessibility") holds the stem and is flagged by the strict lane; the client view masks none.
    Found by the same search and NOT changed, because they are older than this round and identical at
    ffd28ca8 (reported to the owner):
    - joins that hold a listed entry character for character across a word boundary: Japanese 3,927 (the
      two-character Chinese entry "mouth, order" inside "population" before an era name, among others; and
      two ordinary Japanese words, "gag order" in two spellings, hold it whole), Arabic 6,482 (5,423 of
      them the entry "secret word" before a word that begins with the letters of its second word), Persian
      10, Russian 194,420 (the five-letter stem across a space);
    - a Latin joined label spelled across a space by two ordinary words (Polish 1,318,780 candidate joins
      flagged by the strict lane, the same number at all three releases; in German, three common words
      ending in "-sen" before a form of "haben" spell the Portuguese entry: 21 of the 48,400 ordered pairs
      of the 220 commonest words);
    - a Greek word ending in rho, omega after an accented letter ("I know", "around", "I bring") is masked
      by the client view before a separator: the lookalike fold reads the two letters as a short Latin
      label and takes a piece boundary after the accented letter (20 words of the list, 1 of the 220
      commonest);
    - a non-Latin word of about eight letters or more, PERCENT-ENCODED in a URL path, is read as a random
      token by check (d) and the URL is not fetched: of 21,000 generated URLs per script, 7,851 (Japanese)
      to 16,908 (Thai) in the encoded form against 0 to 17 as written, the same at ffd28ca8.

    **M3, over-tightening differential.** 211,782 generated hosts (105,900 in Unicode form and 105,882 in
    punycode, from up to 2,400 common words of each of 29 languages, single, hyphenated, as sub-domains, glued)
    in the sink, registration, competitor, URL, check and `checked_host` lanes: 0 accepted at ffd28ca8 and
    refused now, 0 the other way (the six lanes are identical host by host). URL paths and queries built
    from ordinary words, 21,000 per script for nine scripts, as written and percent-encoded: 0 newly
    `url_has_secret`, 0 no longer. Neutral sentences from the same lists (1,200 per script, 3,400 English,
    each as written, in Title Case and in UPPER CASE): strict flags and client-view masks identical to
    ffd28ca8 in every cell. Against cd0a6c7d: the same three comparisons are identical too (this corpus is
    random and holds none of the collision words; the next one is made of them).
    Hosts built from the M2 collision words (25,068: every cd0a6c7d collision join of Japanese, Hindi,
    Thai and Greek, glued, hyphenated and as two labels, in both forms): 2,069 were accepted at ffd28ca8
    and refused at cd0a6c7d; 208 are accepted at ffd28ca8 and refused now, all in Unicode form in the URL,
    check and `checked_host` lanes: the Thai residual joins (180: 60 joins in three shapes), the Greek
    joins that still read as the label (27: nine joins in three shapes, three of the nine being the label
    itself with one word unaccented), and one Hindi pair glued into one word with no separator ("response"
    before "word": the non-spacing nasal dot of the first is dropped by the inspection view and the rest
    is the label; with a space or a hyphen between them it is not read). None outside the named residuals
    and the label itself. (A Unicode-form host that holds a script label was
    always refused in those three lanes; ffd28ca8 took these only because it did not recognise the Thai,
    Hindi and accented Greek labels at all.)

    **M4, end to end through the real service** (`tests/test_review_cd0a6c7d.py`; the same two runs made
    against the source of each release):

    | | ffd28ca8 | cd0a6c7d | this release |
    |---|---|---|---|
    | (a) 27 forms (the three of the review of ffd28ca8, four sharp-s spellings, 20 other entries in upper case), 81 planted values: values shown to the owner | 21 | 12 | 0 |
    | (a) evidence fields masked with an alert, of 81 | 60 | 69 | 81 |
    | (a) values readable in anything the service wrote | 0 | 0 | 0 |
    | (b) URL resolution on, four own-site URLs cited (the Japanese loanword in one path, the Hindi phrase as a slug in another): fetched | 4 | 2 | 4 |
    | (b) rows classified | 12 as a resolved own URL | 6 so, 6 inconclusive (cited URL not fetched) | 12 as a resolved own URL |
    | (b) title, hint and excerpt fields masked, of 24 | 0 | 9 | 0 |

    **M5, differential fuzz.** 130,000 generated texts (35 labels in every script of the lists under 18
    mutations: case, NFD, lookalikes, accents stripped, all marks stripped, inserted characters, voicing
    variants, vowel-sign variants, a space or a hyphen inside the label, the sharp s; values in several
    scripts; neutral words around them, the collision words among them).

    | | ffd28ca8 -> this release | cd0a6c7d -> this release |
    |---|---|---|
    | strict lane: texts flagged | 90,540 -> 101,259 | 105,548 -> 101,259 |
    | newly flagged | 10,893 | 0 |
    | no longer flagged | 174 | 4,289 |
    | flagged for another reason | 41 (40 to a label, 1 from it) | 30 (from a label to a token or a number) |
    | client view: masked for a label and a value | 53,089 -> 60,910 | 60,818 -> 60,910 |
    | newly masked | 7,821 | 1,558 |
    | no longer masked | 0 | 1,466 |
    | any other client-view finding changed | 0 | 0 |

    Every class, by a classifier that re-reads each changed text (no text is left unclassified):
    - newly flagged against ffd28ca8 (what decision 71 closed, kept): the Greek label with its tonos, final
      sigma or capitals 3,144; the Thai label 3,064; the Hindi label 2,420; a Latin label holding a
      lookalike letter that carried a mark 1,764; a tatweel inside a label of any list 491; an accented
      letter inside a script label 9; a text holding two of these 1. Newly masked: the same classes and,
      in the client view only, the capital stroked L, the sharp s, and a spacing mark typed inside a
      script label (the client view now drops it there, as the strict lane's letters view always did);
    - no longer flagged against ffd28ca8, 174: the unvoiced Japanese entry ("token") with voicing marks
      ADDED to its letters. ffd28ca8 read those through a mark-stripping view it also searched; the text
      views do not strip them, so it is another word (one more such text is now a token finding);
    - no longer flagged against cd0a6c7d, 4,289, exactly the withdrawn over-matches: the Japanese password
      label with other voicing marks or none 3,257; the Japanese token entry with voicing marks added 154;
      the Hindi label's consonants across a word boundary (a space, a hyphen, an underscore, a letter of
      another script between them) 429; the Hindi label with vowel signs added, removed or changed 360;
      texts holding two of these 89. The 30 reason changes and the 1,466 no longer masked are the same
      classes (and 11 texts where a vowel sign follows the Hindi label: a longer word, flagged, not masked);
    - newly masked against cd0a6c7d, 1,558: every one holds a sharp s and every one is masked by cd0a6c7d
      itself once the letter is spelled `ss` (checked text by text).

    **M6, cost.** `tests/test_secret_guard_perf.py` passes. Both lanes, CPU microseconds per unit on 1,000
    real-word sentences per script, ffd28ca8 against this release. The two modules are loaded in one process
    and timed alternately, the best of three runs of 21 alternations each (separate processes differ by 20%
    on this box, which is why the previous round's Greek figure was inconclusive):

    | Script | Client view | Strict lane |
    |---|---|---|
    | Japanese | 29.4 -> 30.1 (+2.4%) | 60.9 -> 58.8 (-3.3%) |
    | Hindi | 60.8 -> 57.9 (-4.7%) | 125.1 -> 121.7 (-2.7%) |
    | Thai | 34.4 -> 35.0 (+1.7%) | 56.5 -> 51.0 (-9.6%) |
    | Arabic | 109.0 -> 113.3 (+4.0%) | 178.4 -> 173.7 (-2.6%) |
    | Persian | 103.8 -> 104.5 (+0.7%) | 214.5 -> 204.4 (-4.7%) |
    | Russian | 138.7 -> 135.2 (-2.5%) | 237.6 -> 243.2 (+2.3%) |
    | Greek | 138.9 -> 140.4 (+1.1%) | 254.7 -> 254.5 (-0.1%) |
    | Polish | 164.7 -> 166.3 (+1.0%) | 259.4 -> 251.3 (-3.1%) |
    | German | 178.0 -> 176.9 (-0.6%) | 270.9 -> 245.0 (-9.6%) |
    | English | 83.8 -> 83.3 (-0.6%) | 126.4 -> 122.6 (-3.0%) |

    The largest rise is 4.0% (Arabic, client view). The same measurement, made before the last change of
    this round, showed a cost that cd0a6c7d had added: `_label_norm` ran the whole lookalike fold a second
    time on every text that had lost an accent, and the strict lane cost 25.5% more than at ffd28ca8 on
    Greek text (Arabic 13.7%, Persian 6.6%, Hindi 6.2%). The second pass is now a one-character table
    (`_refold`): the text it reads is already folded, NFKC and without marks, and there the fold works one
    character at a time. Same result: 0 differences over 4,448,128 code-point contexts and over 669,158
    texts in three forms; the 130,000 fuzz verdicts are byte-identical before and after it.
    Cold start, 36 fresh processes of each release, interleaved: importing the service's modules and
    constructing the service takes a median 745.4 ms at ffd28ca8 and 746.7 ms now (1.002x; lower quartile
    1.012x). The changed module's own body, imported after its dependencies (15 fresh processes each): best
    35.0 ms at ffd28ca8, 36.2 ms at cd0a6c7d, 38.4 ms now (the import-time check reads every entry in
    every natural spelling in both lanes).

    **The two lanes (SHOULD S2), pinned.** The strict lane reads a letter through case folding and the
    `lookalikes` table; the client view reads it through its own cased table. The test puts every code
    point whose strict-lane Latin form is ASCII letters (2,711) in place of those letters in a listed
    label, before a separator and a run-time value, and records where the strict lane flags a label and
    the client view shows the text: 117 code points at cd0a6c7d, 115 now (the sharp s and the capital
    sharp S are gone). The 115 are homoglyphs, documented here and NOT closed in this round; a code point
    outside these classes fails the test:

    | Class | Code points | What each lane reads |
    |---|---|---|
    | a capital whose own lookalike is another letter | 50 (Greek 28, mathematical Greek 20, Warang Citi 2) | the strict lane folds the case first and reads the small letter's lookalike (Greek capital eta: `n`); the client view reads the capital's (`H`) |
    | a capital lookalike of both `I` and `l` | 12 (mathematical 5, Coptic 2, Greek, Cyrillic, Cherokee, Deseret, Warang Citi 1 each) | strict `l`, client view `I` |
    | the Greek eta with an iota subscript | 21 | strict two letters (`ni`), client view one |
    | a capital read as two other letters | 1 (Cyrillic) | strict `io`, client view `LO` |
    | a small letter read only by case folding | 31 (Cherokee 24, Cyrillic 7) | the strict lane folds it to a letter the table knows; the client view's table does not |

    **Residuals (declared, each asserted in the test).**
    - The Japanese label without its voicing marks and the Hindi label without its vowel signs (or with
      others) are other words: not matched in either lane. Withdrawn requirement, above.
    - The Hindi label's two halves written apart (a space, a hyphen, an underscore between them) are two
      ordinary words ("near", "word"): not matched. (The brief for this round asked for "one
      whitespace-delimited word"; a hyphen is excluded too, because a URL slug joins words with hyphens.)
    - The Hindi label inside a longer written word (a plural, a compound) is flagged by the strict lane;
      the client view masks it only when the label ends before the separator, as for every label.
    - A Devanagari mark that is NON-spacing (a vowel sign such as the short u, the nasal dot) is dropped
      by the inspection views from every text, so the label with such a mark added still reads as the
      label. No word of the frequency list does; one of the 182 pairs does when its two words are glued
      with nothing between them (a host name), not with a space or a hyphen.
    - The Thai and the Greek joins of M2, and the homoglyph classes above.
    - Unchanged from decision 71: an inflected form of the Russian and Greek stems in the client view,
      the Greek label with its FIRST word inflected in both lanes, the bare word `key`.

    **Tests.** `tests/test_review_cd0a6c7d.py`: the key-from-view property for every entry of every list
    in both lanes; the fold over every letter; each closure of cd0a6c7d with the function that makes it;
    the loanword, the Hindi joins (all 182, and every first word beside every second) and vowel-sign
    variants; a permanent collision check on 220 common words of each of nine scripts (embedded as
    escapes; the tests import no corpus package): alone, in a sentence, as every ordered pair, before
    five separators in the client view, in a URL path, query and Unicode-form host; the sharp-s
    spellings; the import-time check, arm by arm, and a copy of the module that does not import with an
    entry the lanes read differently; the lane divergence by class; a lookalike carrying a mark,
    `label_only` on non-ASCII segments, the mid-character guard of `_cv_script_ends`; the two end-to-end
    runs; and each residual above. With the keys of cd0a6c7d put back the loanword and all 182 Hindi
    joins are flagged again and two of the four cited URLs are not fetched. In
    `tests/test_review_ffd28ca8.py` the assertions that encoded the withdrawn requirement or the old
    internals were changed (the "marks absent" spelling of the two entries; the key derivation; the sharp
    s; the Hindi examples of the cursor tests, now Thai); nothing else.

## Review cbdf5e78: a script label is a sequence of whole words

A scoped review of cbdf5e78 (decision 72) found that its fixes held but the joins did not. MUST 1: ordinary Greek
three-word text, "code" + "to" + any word beginning with bas- ("code to database"), read as the Greek label (the
key was matched on a letters-only view with the spaces dropped, so it was assembled from the letters of three
words): flagged, a cited URL holding it not fetched, a Unicode host built from it refused. MUST 2: Thai joins whose
letters equal the label's once the vowel and tone marks are ignored ("quilt" + "code") read as the label (the key
was read through the inspection view, which drops those marks). S-1 glued Hindi joins (the label inside a longer
written word), S-2 Arabic phrases masked through the hamza / madda fold, S-3 the Hindi entry spelled one letter at
a time no longer read, S-4 decision 72 untrue (corrected above), N-1 two untested arms of the import check. THE BAR
for this round: against ffd28ca8, nothing ordinary newly flagged, masked, withheld or refused in single words and
two- and three-word joins (spaced, hyphenated, glued) of 18 languages, but the residuals named below.

73. **A script label is matched as a sequence of whole words, by ONE pattern both lanes read; a label spelled
    with a non-spacing mark is read with its marks.**

    **What the code does now** (`secret_guard.py`, sections 1, 2a and 5):
    - **The boundary rule.** In a script that writes words apart (all but Han, kana, Thai, Lao, Khmer, Myanmar:
      `_UNSPACED_SCRIPTS`) a label is a sequence of whole words. Each word of the entry is read by the WORD VIEW
      (`_script_word_view`: per character, a letter through `_script_fold`, ASCII in lower case, the tatweel as
      nothing, a mark kept, anything else kept), as the text is. The match starts at a word start and ends at
      a word end: the character before and after is not a letter or mark of the word's own script
      (`_word_class`, built at import from `_WORD_SCRIPTS`), so a space, a separator, a digit or a letter of
      another script ends a word (a value glued after a separator or a change of script is still read). Two
      words of a label stand apart by one space, "_" or "-", never by another word; only the entries declared
      `glued` (`_SCRIPT_FORMS`: the Persian label, whose non-joiner the inspection view removes, and the
      Russian "API key", whose words change script) may also be written together. A declared STEM takes, in
      its own word, one of its declared endings and nothing else (the Russian and Ukrainian cases of "parol'",
      the Greek forms of "access", the Hindi oblique plural and "-s", a closed list of Korean particles); a
      declared PROCLITIC letter may be written onto the first word (up to three of the Hebrew "and, the, in,
      to, from, that, as"; up to two of the Arabic "and, so, with, to, as"). The multi-word entries are listed
      with their words apart (the two Arabic labels, the Persian label, the Greek label).
    - **One pattern, both lanes.** `_SCRIPT_SEQ` holds the rule for every entry; the strict lane searches it on
      the word view of each view (`_script_hit`), the client view finds every match and reads the separator
      and the value after it on the view as written (`_cv_script_value`, `_cv_script_ends`). A stem's match
      reaches the end of its word, so the declared case forms are masked before a separator too (the Russian
      inflected forms, a residual of decision 71, are closed). The letters-only view is kept as a necessary
      PREFILTER (`_SCRIPT_RX`, `_MARKED_RX`: every match of the rule leaves the entry's letters in a row
      there). The Han and kana labels are read as before: strict, their letters anywhere in the letters-only
      view; client view, the letters with a space or "_" between two, and (review of ced056c5) the WHOLE
      entry spelled one letter at a time with "-" or "." as a word of its own. The client view never reads a script
      label on an ASCII-folded view (a letter folded to a Latin lookalike there stood as a change of script
      before the label: S-2).
    - **The marked entries.** An entry whose inspection view would drop a non-spacing mark it is spelled with
      (the Hindi virama, the Thai vowel and tone marks: `_marked_entry`) is read, entry and text alike, on the
      MARKED views (`_marked_views`: every view of `_tagged_views` made with `_clean_marked`, which keeps the
      non-spacing marks of the Devanagari, Thai, Lao, Khmer and Myanmar blocks and removes every other mark,
      joiner and invisible character as `_clean` does). They are read only when a view without marks holds
      the entry's letters in a row (`_marked_candidate`), once per text (`_marked_label` in `_check` and
      `_abc` before the view's checks (a)-(c); `_cv_marked` in `_cv_findings`). The Thai label, in a script
      without word spaces, is read only as written (its marks on its letters) or spelled one unit at a time.
    - **One space put inside a label word** (the no-break space or line break of a disguised label, which the
      views read as a space; the leak corpus holds it for the Russian and Korean labels): read by a second
      pattern of both lanes, `_SCRIPT_SPLIT`, only letter for letter AS WRITTEN (the entry, its declared endings
      and proclitics as listed, lower case, nothing folded, one space or "_" between two letters of one word,
      the boundaries above). Both lanes of ffd28ca8 read every script label that way (its letters in a row, a
      space or "_" between two), so this reads nothing ffd28ca8 did not; it is not made for the Greek label
      (the word view reads its final sigma otherwise than as written) nor for the marked ones.
    - **A label spelled one unit at a time** with ONE separator (a space, "_", "-" or ".", once or twice where
      the view dropped a mark between two units; a mark may also stay on its letter) is read in both lanes
      (S-3). A two-letter label with "-" or "." between its letters (and a Han or kana label with "-" or ".")
      is read only as the WHOLE entry so spelled, a word of its own: no letter right before or after it and no
      letter joined to it by the same separator (`_spelled_whole`; review of ced056c5, brief item 4: residual
      (e) and the 768 Han / kana cells of M1 closed, residual (h) below).
    - **The Latin lookalikes** (found by this round's search, not by the review). The second lookalike pass of
      cd0a6c7d (decision 72) read every accented letter as the Latin letter it resembles, so ordinary Greek
      words side by side spelled a Latin label across them ("na rik egine" read "apikey"; 427,844 Greek joins
      flagged against ffd28ca8). It now reads only a LONE letter of another script standing in a word with
      letters of other scripts (`p` + alpha with tonos + `ssword`); a run of two letters or more of one script,
      or a whole word, is read in its own script (`_one_script_spans`, `_refold_mixed`). A label split over
      words or spelled letter by letter is read from those words each on its own (`_fold_words`), so a
      one-letter word of another script is not refolded with its neighbours.
    - The import check (`_label_keys`, `_script_words`, `_cv_script_keys`, `_check_label_lists`) stops the
      import unless every entry and every listed full form is taken WHOLE by the one pattern and flagged by
      the strict lane in every natural spelling (`_natural_spellings`: as listed, UPPER, Title), its prefilter
      key is its letters, and its words and declared forms fit it.

    **M1, the label matrix** (`h/m1.py`: every entry of each release's own lists x natural spellings, 19
    classes in NFC and NFD = 38: as written, lower, upper, title, capitalised, Turkish dotted capital, marks
    absent (not for the Japanese, Hindi and Thai labels, whose marks spell the word), elongated, four sharp-s
    forms, words joined by "_", "-" or a no-break space, spelled letter by letter with " ", "_", "-", "." x
    16 separators (`: ` `:` `=` ` = ` ` is ` ` IS ` ` Is ` ` was ` ` are ` ` would be ` ` will be ` ` remains `
    ` -> ` ` - ` `=>` ` | `), bare and inside a sentence, a run-time value):

    | | entries; spellings; cells | client view: cells shown | strict lane: cells not flagged |
    |---|---|---|---|
    | ffd28ca8 | 95; 1,041; 33,312 | 7,648 | 770 |
    | cbdf5e78 | 96; 1,137; 36,384 | 6,120 | 226 |
    | ced056c5 (the first commit of this round, not pushed) | 96; 1,063; 34,016 | 1,088 | 482 |
    | this release | 96; 1,063; 34,016 | 0 | 98 |

    At ced056c5 the 1,088 were the Han and kana labels spelled with "-" or "." (768; ffd28ca8 as there) and the
    three two-letter Korean labels with "-" or "." between their letters (384), the 482 the piece "pw" spelled
    letter by letter (98; ffd28ca8 as now) and the same Korean forms (384). The review of ced056c5 required
    them read (brief item 4): now 0 and 98 (the "pw" cells, unchanged since ffd28ca8). Value leaks to the
    owner end to end: 0 (M4).

    **M2, the collision search.** 18 languages: the 20,000 most frequent words of wordfreq 3.1.1 for Japanese,
    Hindi, Arabic, Persian, Russian, Ukrainian, Greek, Hebrew, Korean, Chinese, Polish, German, Turkish,
    French, Spanish, Portuguese, Italian; of pythainlp 5.4.0 (TNC, by count) for Thai. Candidates
    (`h/m2_cand.py`, no cap): every two-word join whose skeleton holds a label key across the boundary (a
    suffix of the first word + a prefix of the second) and every three-word join holding one across both
    boundaries (a suffix of the first + all of the second + a prefix of the third), under four skeletons
    (letters case folded with every mark and the tatweel dropped; the Latin loose form; the Latin letters view
    of ffd28ca8 and of this release, lookalikes folded) and the union of the three releases' keys; a piece
    label only as whole words: 111,503 pairs and 12,095,642 triples. Each word alone and each candidate
    spaced, hyphenated and glued: 36,981,435 texts. Stage 1 (every text, each release): the label rules of both lanes on every view (exactly the code
    that differs between the releases; the client view on the text followed by ": ready today"). For the
    seven Latin-script languages a join is read only if it holds a character the releases' Latin rules read
    differently (59 characters of the corpora, alone and inside a Latin word: `h/chars.py`) or a letter of
    another script (19,740 of their 29,353,085 join texts; every word is read); a seeded sample of 30,000
    skipped joins per language through all three releases gave identical answers (210,000 texts). Stage 2: every text whose stage-1 answer
    differs between this release and ffd28ca8 or cbdf5e78 where this release says yes, and 2,000 where it says
    no (per language), through the real lanes: `check`, `client_view_findings`, `url_has_secret` on a raw and
    an encoded path, the six host lanes on the Unicode and punycode host.

    | Language (pairs; triples) | strict / client view, ffd28ca8 | cbdf5e78 | this release | new against ffd28ca8, strict / client view | new against cbdf5e78 |
    |---|---|---|---|---|---|
    | Japanese (88; 8,175) | 21,692 / 2,607 | 20,078 / 2,607 | 20,078 / 2,607 | 1 / 0 | 0 / 0 |
    | Hindi (161; 791,261) | 42,828 / 1,372 | 67,437 / 2,105 | 42,837 / 1,381 | 9 / 9 | 0 / 0 |
    | Thai (12; 10,724) | 0 / 0 | 32,208 / 5,086 | 3,158 / 634 | 3,158 / 634 | 0 / 0 |
    | Arabic (72; 3,477) | 9,870 / 268 | 10,362 / 328 | 482 / 153 | 3 / 9 | 0 / 9 |
    | Persian (68; 6,332) | 12,249 / 528 | 17,796 / 528 | 10,195 / 336 | 20 / 1 | 0 / 2 |
    | Russian (162; 98,851) | 295,590 / 848 | 295,590 / 848 | 259,616 / 647 | 0 / 21 | 0 / 21 |
    | Ukrainian (185; 899,733) | 2,691,969 / 941 | 2,697,726 / 941 | 2,642,697 / 714 | 0 / 20 | 0 / 20 |
    | Greek (169; 334,280) | 565,369 / 3,321 | 998,262 / 3,344 | 568,590 / 3,327 | 3,221 / 6 | 0 / 4 |
    | Hebrew (633; 209,026) | 628,886 / 861 | 628,886 / 861 | 439 / 228 | 0 / 0 | 0 / 0 |
    | Korean (1,997; 95,554) | 291,539 / 1,954 | 291,539 / 1,954 | 22,603 / 643 | 0 / 41 | 0 / 41 |
    | Chinese (192; 8,298) | 21,886 / 1,655 | 21,886 / 1,655 | 21,886 / 1,655 | 0 / 0 | 0 / 0 |
    | Polish (1,601; 510,304) (filtered) | 3 / 3 | 3 / 3 | 3 / 3 | 0 / 0 | 0 / 0 |
    | German (71,854; 4,514,288) (filtered) | 9 / 7 | 9 / 7 | 9 / 7 | 0 / 0 | 0 / 0 |
    | Turkish (13,483; 705,486) (filtered) | 2 / 2 | 2 / 2 | 2 / 2 | 0 / 0 | 0 / 0 |
    | French (7,501; 1,135,249) (filtered) | 8 / 10 | 8 / 10 | 8 / 10 | 0 / 0 | 0 / 0 |
    | Spanish (4,399; 1,607,532) (filtered) | 2,971 / 91 | 2,971 / 91 | 2,971 / 91 | 0 / 0 | 0 / 0 |
    | Portuguese (6,072; 882,442) (filtered) | 16,469 / 162 | 16,469 / 162 | 16,469 / 162 | 0 / 0 | 0 / 0 |
    | Italian (2,854; 274,630) (filtered: words + joins read; see text; ffd28ca8 and cbdf5e78 also read every join: 830,877 / 876 each) | 8 / 8 | 8 / 8 | 8 / 8 | 0 / 0 | 0 / 0 |

    Stage-1 counts are texts the label rule of a lane reads (a word, or a candidate in one of its forms); for the
    filtered languages only the words and the joins read. Stage 2, against ffd28ca8, over the 26,119 texts read
    (every new one and the 2,000-text samples of the others): newly flagged 6,412, newly masked 741, a URL holding
    the text newly not fetched 6,410 (raw path; 165 percent-encoded), a host built from it newly refused 0;
    against cbdf5e78: newly flagged 0, newly masked 96 (all the label's own words), newly refused 0.

    Against ffd28ca8, every text this release flags or masks and ffd28ca8 did not, by class (stage 2 confirms
    each in the real lanes; a host built from any of them: 0 newly refused):
    - **the label itself** (its words as whole words, with a declared ending, proclitic or separator): strict
      3,089 (Greek 3,080: "code access" + another word, the joins of the label's own two words; Hindi 9: the
      label's halves glued, alone or with an invisible character or a stray accent between them), client view
      107 (Korean 41 with a particle, Russian 21 and Ukrainian 20 in another case, Arabic 9 hyphenated or
      with a proclitic, Greek 6, Hindi 9, Persian 1 hyphenated). ffd28ca8 did not read these spellings;
    - **residual (a), Thai**: 3,158 glued joins hold the label AS WRITTEN, marks on its letters (strict;
      client view 634 before ": ready today");
    - **the glued lone lookalike**: 165 glued joins (Greek 141, Persian 20, Arabic 3, Japanese 1) in which a
      one-letter word of another script carrying a mark is glued between Latin letters ("p" + omega with
      tonos; "c" + alef with hamza + "e"; "has" + alef with madda + "of") and the whole reads as a Latin label
      through the second lookalike pass of decision 72. It is the very shape that pass exists for (a Latin
      label with one accented Greek letter in it, asserted by `tests/test_review_cd0a6c7d.py`) and cannot be
      told apart from it: declared residual (g), below;
    - anything else: 0.

    **M2 again for Korean, Chinese and Japanese** (review of ced056c5: the two-letter and Han / kana labels
    spelled with "-" or "."), every candidate in FOUR forms (spaced, hyphenated, glued and dotted): 410,204,
    53,960 and 53,052 texts, stage 1 on ffd28ca8, ced056c5 and this release. Strict lane against ffd28ca8:
    0 newly flagged (ko 388,694 -> 30,115, zh 29,103 -> 29,103, ja 28,880 -> 26,728 texts read). Client
    view against ffd28ca8: newly masked 82 that ced056c5 did not mask (ko 30, zh 12, ja 40), and the 41 Korean
    label words with a particle of the table above. Stage 2 (the real lanes) on the 82: masked by the client
    view 0 at ffd28ca8 -> 82; `check`, the raw and encoded URL and the host lanes as at ffd28ca8: residual (h).

    **M3, over-tightening differential.** 948,803 hosts (474,446 in Unicode form, 474,357 in punycode: the
    211,782 of decision 72, every word of the 18 lists of M2 as a label and 700 hyphenated pairs per language,
    each under its country's TLD) in the six lanes (sink classify, tenant registration, audit and competitor
    registration, `inspect_url`, `check`, `checked_host`): against ffd28ca8, accepted then refused 3 lane cells,
    all of ONE host, the Hindi label itself as a Unicode-form label (a Unicode host holding a script label is
    refused in the URL, check and `checked_host` lanes, as for every script label; ffd28ca8 did not read the
    Hindi label); refused then accepted 0. Against cbdf5e78: 0 and 0. The hosts of every M2 candidate this
    release reads differently from ffd28ca8 or cbdf5e78 went through the same lanes in stage 2: 0 newly refused.
    URLs from ordinary words, 21,000 per language for the 18 languages (path, slug, query, fragment, sub-domain;
    378,000), as written and percent-encoded: newly `url_has_secret` 0 and no longer 0, against both releases.
    Neutral sentences (14,200, in three casings): 0 rows differ. 120,000 machine values (UUIDs, SHA-1 / SHA-256,
    ISO times, amounts, invoice and ruling ids, request ids, ASINs, URLs with ids) through `check`, the client view
    and `sink_guard.classify`: 0 differences against either release. The real schedules route took 20,000
    schedules, each with a distinct invoice id and a distinct ruling id answered by Compliance: 20,000 of 20,000
    answered 201.

    **M4, end to end through the real service** (`h/m4.py`: the same runs against the source of each release):

    | | ffd28ca8 | cbdf5e78 | this release |
    |---|---|---|---|
    | (a) the 27 forms of decision 72, 81 planted values: shown to the owner / fields masked of 81 / on disk | 21 / 60 / 0 | 0 / 81 / 0 | 0 / 81 / 0 |
    | (a) 10 forms of this round (spelled Hindi and Thai, Russian and Korean case forms, Hebrew and Arabic proclitics, the Hindi plural, the Greek label with the second word in another case), 30 values: shown / fields masked of 90 / on disk | 24 / 18 / 0 | 18 / 36 / 0 | 0 / 90 / 0 |
    | (b) URL resolution on, 5 own-site URLs cited (raw paths holding "code to" + a word in bas-, a Thai join, the Japanese loanword, a Hindi glued join, an Arabic phrase): fetched | 4 | 1 | 5 |
    | (b) rows | 12 resolved own URL, 3 inconclusive (cited URL not fetched) | 3 resolved, 12 inconclusive | 15 resolved |
    | (b) evidence fields masked, of 45 | 0 | 18 | 0 |

    The one URL ffd28ca8 did not fetch holds the Arabic phrase: its letters-only view read the label across the
    phrase's three words; it is fetched now (the one difference from ffd28ca8, the other way round).

    **M5, differential fuzz** (`corp/m5.json`, the 130,000 texts of decision 72: 35 labels in every script under
    18 mutations, values and neutral words around them):

    | | ffd28ca8 -> this release | cbdf5e78 -> this release |
    |---|---|---|
    | strict lane: texts flagged | 90,540 -> 99,002 | 101,259 -> 99,002 |
    | newly flagged | 9,531 | 0 |
    | no longer flagged | 1,069 | 2,257 |
    | client view: masked for a label and a value | 53,089 -> 60,274 | 60,910 -> 60,274 |
    | newly masked | 7,222 | 5 |
    | no longer masked | 37 | 641 |
    | flagged for another reason | 41 | 10 |

    Every changed text holds a label of the lists (by the generator's own record) but 14, and those 14 are the
    Hindi label itself, made by gluing its two halves (neutral words of the generator). Newly flagged against
    ffd28ca8: the Greek, Thai and Hindi labels (3,084; 2,329; 2,129: what decisions 71 and 72 closed) and Latin
    labels with lookalikes, marks or a tatweel (the same classes as decision 72). No longer flagged against
    ffd28ca8: a script label with a space, a hyphen, an underscore or a letter of another script put inside one of
    its words (the boundary rule), or a value glued to it in its own script ("API key" + letters), and the
    Japanese token with voicing marks added (decision 72). Against cbdf5e78: no longer flagged, the same
    classes and the over-readings this round closes (the Thai and Hindi labels without their marks or across
    words; Latin labels spelled with two or more accented lookalikes of one script, read so only since
    cd0a6c7d); newly masked 5, the Thai and Hindi labels read on their marked views.

    **M6, cost** (the code of this commit). `tests/test_secret_guard_perf.py`: 16 passed. Per script, CPU
    microseconds per sentence (1,000 sentences of each of 10 languages, `corp/m3.json`), ffd28ca8 / this
    release, best of 5 rounds, each round one fresh process per release (the order alternating), each process
    best of 5 passes (`h/q_m6b.sh`):

    | script | client view | strict lane |
    |---|---|---|
    | ja | 29.1 / 25.2 (-13.5%) | 68.2 / 72.9 (+7.0%) |
    | hi | 66.7 / 60.4 (-9.5%) | 136.0 / 130.0 (-4.4%) |
    | th | 36.8 / 33.4 (-9.4%) | 55.4 / 54.3 (-2.0%) |
    | ar | 123.4 / 99.2 (-19.6%) | 180.5 / 156.5 (-13.3%) |
    | fa | 102.7 / 92.9 (-9.6%) | 216.3 / 174.3 (-19.4%) |
    | ru | 137.5 / 124.2 (-9.7%) | 231.3 / 186.8 (-19.2%) |
    | el | 133.3 / 120.0 (-10.0%) | 277.6 / 268.8 (-3.2%) |
    | pl | 195.9 / 161.4 (-17.6%) | 287.9 / 252.7 (-12.2%) |
    | de | 173.3 / 174.0 (+0.4%) | 251.8 / 240.0 (-4.7%) |
    | en | 82.7 / 83.7 (+1.2%) | 125.3 / 125.7 (+0.3%) |

    The largest rise is Japanese strict +7.0% (its five rounds +9.8%, +1.2%, +8.6%, +2.5%, +27.5%; Greek
    strict -0.6%, +17.3%, -6.1%, +7.9%, +15.0%: the box is noisy, the best of each release is the figure).
    The first measurement of this round found the strict lane on Greek text +54% to +69% against ffd28ca8:
    the lone-letter rule of the second lookalike pass (above) folded every Greek text part by part. Three
    changes, each giving the same answer as the code M1 to M5 measured (checked on every inspection view of
    the M2 word lists and joins, of the M3 and M5 texts and of the test sets, 16,553,454 views, 0 differ, and
    on 2.0 million random texts of five scripts, Latin letters, marks and punctuation, 0 differ; asserted in
    the tests on 20,000 random texts and on every code point): a text of ASCII, plain punctuation and ONE
    other script (Greek, Cyrillic, Hebrew, Arabic, Devanagari) whose words touch no letter is answered in one
    pass; elsewhere the part-by-part fold runs only where a part outside the one-script words holds another
    character or a casing context (the final sigma) crosses a word edge; the marked step is skipped for a
    view without a character of the marked blocks. Both releases loaded in ONE process and timed alternately
    (`h/old/m6_ab.py`, best of 5, two runs in both orders): Greek strict +6.9% and +14.4%, Japanese strict
    +7.1% and +7.8%, every other cell at most +6%; the run-to-run spread on the same code is of that size.
    Cold start (24 fresh processes per release, interleaved, medians): import of the guard, the service and
    the first two calls 715.7 ms at ffd28ca8, 745.9 ms now (1.04x); the guard module alone 41.9 ms and
    74.8 ms (1.79x, +33 ms: the patterns of the boundary rule, `_SCRIPT_SEQ` alone 11.8 ms to compile, and
    the import check, 1.3 ms). Not reduced: the import-time list check (`_check_label_lists`) matches every
    entry against `_SCRIPT_SEQ`, so a lazily compiled pattern would be compiled at import anyway; the check is
    kept at import on purpose (a list the lanes read differently must stop the import). Worst-case inputs
    (nine shapes, 2,000 to 32,000 characters, both lanes, `h/m6_lin.py`): 32,000 characters cost 12.1x to
    20.0x what 2,000 do (ffd28ca8: 14.1x to 16.7x; the 20.0x, separators between units in the client view,
    measured again best of 5: 15.3x and 15.5x, and 64,000 characters 1.84x and 2.35x of 32,000): linear.

    **Residuals (declared, each asserted in a test; exact counts from M1 / M2).**
    - (a) Thai: a glued join that holds the label as written, marks on its letters: 3,158 of the Thai
      candidates (strict; 634 in the client view before a separator). Thai is written without spaces and no
      segmenter is used.
    - (b) The Japanese label without its voicing marks, the Hindi label without its vowel signs or virama,
      and now the Thai label without its vowel and tone marks: other words, not read (the Thai one is read
      with its marks only, as the review required).
    - (c) The Hindi label written as two words (ffd28ca8 did not read it either).
    - (d) Older, the same at ffd28ca8, cbdf5e78 and now: the bare word `key` in the client view; ALL-CAPS
      prose; short labels in alternating case in the strict lane; Persian non-joiner URLs; the piece "pw" spelled letter by letter
      in the strict lane (98 cells); a long non-Latin word percent-encoded in a URL path read as a token by
      check (d).
    - (e) CLOSED (review of ced056c5): the two-letter Korean labels with "-" or "." between their letters, and
      the Han and kana labels spelled with "-" or ".", are read (M1: 0 cells; they were 384 per lane and 768).
    - (f) Against cbdf5e78 only, as at ffd28ca8: a Latin label with two accented lookalikes of one script side
      by side is not flagged by the strict lane (the client view masks it with its value); below.
    - (g) ACCEPTED by the owner (review of ced056c5): the 165 glued lone-lookalike joins of M2 (Greek 141,
      Persian 20, Arabic 3, Japanese 1; stage 1 and 2 of M2, classed by re-reading each with the second pass
      off: flagged only through it). **Reworded by review 0334eeae (SHOULD 3; decision 74 has the counts at
      20,000 and 30,000 words):** the mechanism is a LONE letter of another script (one letter, not a run of two
      or more of one script, nor a whole word) carrying a mark, standing in a glued join next to Latin letters,
      which the second lookalike pass reads as the Latin letter it resembles; it stands between two Latin
      letters, at the edge of the join (the text's start or end, Latin letters on its other side), or beside a
      word of another script (a run that pass leaves alone, such as a two-letter Cyrillic word). That is
      exactly the homoglyph-evasion shape (a Latin label with one accented Greek lookalike, asserted flagged by
      `tests/test_review_cd0a6c7d.py`) and cannot be told from it; not flagging it would be a security
      regression. A sample of each language is pinned as flagged.
    - (h) ACCEPTED by the owner (review of ced056c5): 82 joins of the M2 lists, Korean 30, Chinese 12, Japanese
      40 (M2 again, above), are two one-letter words joined by "-" or "." that spell a label exactly (the
      triples: a list "word" that is only a mark or a variation selector between them, which the views drop):
      "rain" + "number", "dark" + "lake", "Saturday" + "big"; "mouth" + "order", "order" + "tablet", "secret" +
      "code". They are the label in the spelled form the brief requires to be read, the class of "the label's
      own words"; the client view masks a value after them (ffd28ca8 did not); `check`, the URL and the host
      lanes read them as ffd28ca8 did. A sample of each language is pinned.
    No longer read, against ffd28ca8 (fewer flags, not more; listed so the owner can weigh them): a script label
    inside a longer written word or across words (the Korean compound with a noun before it, the Russian
    "password-protected", a Hebrew or Arabic word with a prefix that is not a declared proclitic).
    **Corrected by review 0334eeae (decision 74):** this list also named the unaccented Greek label written
    glued, the Korean label written as two words and the Arabic labels written glued; at 0334eeae they were
    not read, which the review rejected (MUST A). They are read again (decision 74: the Arabic and Greek labels
    glued, as written; the Korean compound's two words with a space, "_" or "-" between them): M1 of decision 74
    finds no label form ffd28ca8 or cbdf5e78 read that this release does not. Against cbdf5e78 only (ffd28ca8 did not flag them either): a Latin label with TWO accented lookalikes of
    one script side by side ("t" + omicron with tonos + kappa + "en") is not flagged by the strict lane; the
    client view masks it with its value, as at both releases. Two letters of one other script in a row are read
    as a word of that script: read as lookalikes instead (the mutation of this round), 36 ordinary glued joins
    of the Arabic and Persian lists (a Latin-letter token of the list + the interjection "ah") were flagged.

    **Tests.** `tests/test_review_cbdf5e78.py`: the Greek "code to bas-" phrases (100, in five forms) and their
    mutation; the Greek label in its forms and nothing glued to it; the Thai joins (12 pairs in three forms,
    80 triples) not read, the six that hold the label as written read (residual a), the label with its marks,
    without them (residual b), and the mutation without the marked views; the Hindi glued joins and the label
    with its marks and endings; the 60 Arabic phrases cbdf5e78 newly masked; the Arabic and Hebrew proclitics;
    the spelled forms (every entry, one separator) and the two-letter residual; 40 Greek triples cbdf5e78
    read as Latin labels, a Latin label with one accented lookalike still read, and the mutation; words read
    each on their own, and two letters of one other script in a row read as that script; the Korean particles;
    the marked view keeping the marks of its own blocks only; no script label read on an ASCII-folded view;
    the import-check arms (N-1) and the full forms; one pattern for both lanes; the shortcuts of the Latin
    form (M6) giving the same answer as the spans and the part-by-part fold, on random texts of five scripts
    with the final-sigma case, and no code point outside the marked blocks spelling a marked key; residuals (g)
    and (h) pinned, a sample of each language; the two-letter and Han / kana labels spelled with "-" or "."
    read, and only as a whole word; two citation runs end to end. In `tests/test_review_ffd28ca8.py`, `tests/test_review_cd0a6c7d.py` and
    `tests/test_review_c13529af.py` the assertions on the removed internals were moved to the new ones, and
    the residuals this round closes are asserted closed; each change is listed in the round's report.

## Review 0334eeae: a glued script label is read as written

A scoped review of 0334eeae (decision 73) found the boundary rule sound but one class of label forms lost. MUST A:
the Arabic "password" and "secret word" labels written with their two words glued (also with a tatweel inside,
and with an invisible joiner between letters) were no longer flagged or masked, and the unaccented Greek label
written glued neither; ffd28ca8 and cbdf5e78 read them, and the glued Arabic natural spellings had been removed
from `tests/test_review_ffd28ca8.py` (a weakened assertion). The cause: the multi-word entries were not declared
glued, so the separator between their words was mandatory; declaring them glued on the word view would have read
the ordinary Arabic phrases of review cbdf5e78 S-2 again (the word view folds the hamza and the madda away).
SHOULD 1 seven behavioural mutants survived the review tests; SHOULD 2 the collision tests were snapshots only;
SHOULD 3 the wording of residual (g); NIT 1 a Latin-form shortcut not output-identical; NIT 2 test-only code in
the production module. THE BAR for this round is two-sided: against ffd28ca8 nothing ordinary newly flagged,
masked, withheld or refused (and against 0334eeae nothing ordinary at all); and every label form ffd28ca8 or
cbdf5e78 read still read.

74. **A multi-word script label written glued is read AS WRITTEN, by one pattern both lanes read.**

    **What the code does now** (`secret_guard.py`, section 2a):
    - **The glued rule.** `_glued_alternatives` makes, for every multi-word script entry that is not declared
      `glued` (the two Arabic labels and the Greek label), the entry with its words written together, matched on
      the view AS WRITTEN (`_WRITTEN`: each character in lower case when that is one character, the tatweel as
      nothing; no mark, accent, hamza or madda folded or dropped; the joiners, the soft hyphen, the word joiner
      and every other invisible character are already gone from every inspection view), with the entry's
      declared proclitics on the first word and declared endings on the last, as listed, and the boundaries of
      the spaced form (a word start before it, a word end after it: the boundary rule of decision 73). The Greek
      final sigma of an entry word may be written as the medial sigma (a glued word is so written, and a capital
      sigma lowers to it), never the other way round (`_written`). The entry's declared `written` spellings
      (`_SCRIPT_FORMS`) are read the same way: the Greek label with the accents Greek writes it with (five case
      forms), and the stem ffd28ca8 listed. `_SCRIPT_GLUED` is searched by the strict lane (`_script_hit`) and
      by the client view, with the separator and the value read after it on the view as written (`_cv_label`,
      `_cv_script_value` with `_WRITTEN`). The word view is not used for it: no fold is needed to read what
      ffd28ca8 and cbdf5e78 read (M1 below), so none is made.
    - **Two declared spellings ffd28ca8 read that the boundary rule did not** (found by M1): the Korean compound
      "secret" + "number" written as its two words apart with a hyphen (`apart`: one space, "_" or "-" between the
      words, whole words, the particles on the last), and the stem ffd28ca8 listed for the Greek label (its own
      prefilter key, `_WRITTEN_RX`, as the stem does not hold the entry's key). `_declared_alternatives` adds
      both to `_SCRIPT_SEQ`.
    - **The import check** (`_check_glued`) stops the import unless every entry that has glued alternatives, as
      listed, UPPER and Title, and each declared written spelling, is taken whole by `_SCRIPT_GLUED` on its view
      as written and flagged by the strict lane.
    - **NIT 1.** The shortcut in `_label_norm` that kept the whole text's first pass when a second pass changed
      nothing was not output-identical to the part-by-part fold (`h/nit1.py`: 659 of 300,000 random texts of the
      test's five-script pool gave another internal form at 0334eeae; the same verdicts). Folding a text part by
      part and whole can differ only through a character whose lower case depends on its context, the capital
      sigma (the final-sigma rule; every other step reads one character at a time, and no composition crosses a
      part edge: a part ends where a run of letters and their marks ends): the shortcut is now taken only when
      the text holds no character that gives the capital sigma (`_SIGMA_RX`, checked against every code point in
      `tests/test_review_0334eeae.py`). Now 0 of 600,000 random texts differ from the definition, which
      `tests/test_review_cbdf5e78.py` writes out with no shortcut. (Removing the shortcut altogether was measured
      first: the part-by-part fold then ran on 701 of 1,000 Japanese sentences, strict lane +24.9% against
      ffd28ca8; see M6.) **NIT 2.** `_TAG_RUN` and `_one_script_spans_loop` moved to that test.

    **M1, the label matrix, BAR 2** (`h/m1x_gen.py`, `h/m1x_eval.py`, `h/m1x_diff.py`: ONE fixed cell set for every
    release, so the cells compare one by one): the union of the four releases' lists (101 entries: each
    release's own `_JOINED`, `_PIECES`, `_SCRIPT`, `_CV_SCRIPT_FULL`, so the glued entries ffd28ca8 and cbdf5e78
    listed and the stem ffd28ca8 listed for the Greek label are entries too) x natural spellings in 37 classes x
    NFC and NFD (as written, lower, upper, title, capitalised, Turkish dotted capital, marks absent and its upper
    case, elongated, four sharp-s forms, each of five invisible joiners inside a word (zero-width joiner and
    non-joiner, zero-width space, soft hyphen, word joiner), words joined by "_", "-" or a no-break space, GLUED,
    glued upper, glued title, glued with the medial sigma, glued with a tatweel at the join or inside, glued
    with each of the five joiners at the join, glued with the marks absent and its upper case, spelled letter
    by letter with " ", "_", "-", ".") x 16 separators x bare / inside a sentence: 2,513 spellings, 80,416
    cells, a run-time value in each. Read = flagged by the strict lane / masked by the client view.

    | | strict lane: cells read | client view: cells read |
    |---|---|---|
    | ffd28ca8 | 68,316 | 58,496 |
    | cbdf5e78 | 79,806 | 68,376 |
    | 0334eeae | 71,998 | 71,136 |
    | this release | 80,318 | 79,456 |
    | read at ffd28ca8 or cbdf5e78, not at 0334eeae | 8,320 | 6,656 |
    | read at ffd28ca8 or cbdf5e78, not at this release (BAR 2) | **0** | **0** |
    | this release against 0334eeae: newly read / no longer read | 8,320 / 0 | 8,320 / 0 |

    The 8,320 strict cells 0334eeae lost, by class: glued 672, glued upper 288, glued title 320, glued with the
    medial sigma 320, glued with a tatweel at the join 768 / inside 704, glued with a joiner at the join 768 each
    of the five, glued with the marks absent 128 and upper 128, a joiner inside a word 128 each of the five, as
    written 128, upper 64, title 64, elongated 64, the Korean words with a hyphen 64, spelled 32 each of the four
    separators: the two Arabic labels and the Greek label glued (and the glued entries the older lists held), the
    stem ffd28ca8 listed, the Korean compound with a hyphen. This release reads every one of them, and nothing
    else against 0334eeae: its 8,320 newly read client-view cells are the same cells; 1,664 of them ffd28ca8 and
    cbdf5e78 read in the strict lane only (the glued Greek forms, the Korean compound with a hyphen, the stem
    spelled with "-" or "."), so the client view now masks the label forms the strict lane flags. Still not read
    at this release: strict 98 cells (the piece "pw" spelled letter by letter: residual (d), the same at all four
    releases); client view 960 cells (a Latin label with an ARABIC TATWEEL put inside or between its words: not
    read by the client view of any of the four releases, a class of no natural spelling, recorded only).

    **M2, the collision search, 30,000 words** (`h/m2_words30.py`, `h/m2_cand.py`, `h/m2_eval.py`, `h/m2_diff6.py`,
    `h/m2_s2list6.py`, `h/m2_s2table.py`): wordfreq 3.1.1, the 30,000 most frequent words of 17 languages (Hindi
    26,604 and Korean 29,978: all wordfreq holds); Thai the TNC top 20,000 by count with the full pythainlp 5.4.0
    dictionary (61,495 words), 65,087 words. Candidates as in decision 73 (every two-word join whose skeleton
    holds a key across the boundary, every three-word join across both, four skeletons and the keys of the four
    releases, no cap), each word alone and each candidate in FOUR forms (spaced, hyphenated, glued, dotted):
    174,790,165 texts, 26,011,061 read and 148,779,104 skipped by the Latin filter of decision 73 for the seven
    Latin-script languages (recomputed on these lists; it skips a text only when it holds no letter of another
    script and no character the releases' Latin rules read differently). By the review's amendment the PRIMARY
    proof is the differential against 0334eeae (measured independently in everything but MUST A) over all 18
    languages; the languages of MUST A (Arabic, Greek, Persian, Hebrew) are also measured from scratch against
    ffd28ca8 and cbdf5e78 (and Japanese, Hindi, Thai, Russian, Korean, Chinese against ffd28ca8).

    Stage 1 against 0334eeae (texts read by the label rule of each lane; new / no longer = texts this release
    reads that 0334eeae did not / the other way):

    | Language (words; pairs; triples) | texts read | 0334eeae strict / client view | this release | new | no longer |
    |---|---|---|---|---|---|
    | Japanese (30,000; 246; 37,645) | 181,564 | 137,903 / 6,398 | 137,903 / 6,398 | 0 / 0 | 0 / 0 |
    | Hindi (26,604; 292; 2,063,598) | 8,282,164 | 146,089 / 2,435 | 146,089 / 2,435 | 0 / 0 | 0 / 0 |
    | Thai (65,087; 144; 146,564) | 651,919 | 48,497 / 2,257 | 48,497 / 2,257 | 0 / 0 | 0 / 0 |
    | Arabic (30,000; 190; 11,071) | 75,044 | 7,244 / 281 | 7,276 / 313 | 32 / 32 | 0 / 0 |
    | Persian (30,000; 121; 17,834) | 101,820 | 41,744 / 1,064 | 41,744 / 1,064 | 0 / 0 | 0 / 0 |
    | Russian (30,000; 361; 271,134) | 1,115,980 | 977,859 / 1,364 | 977,859 / 1,364 | 0 / 0 | 0 / 0 |
    | Ukrainian (30,000; 379; 2,127,907) | 8,543,144 | 8,341,247 / 1,804 | 8,341,247 / 1,804 | 0 / 0 | 0 / 0 |
    | Greek (30,000; 275; 786,925) | 3,178,800 | 1,890,127 / 7,258 | 1,890,150 / 7,270 | 23 / 12 | 0 / 0 |
    | Hebrew (30,000; 1,090; 483,111) | 1,966,804 | 1,499 / 400 | 1,499 / 400 | 0 / 0 | 0 / 0 |
    | Korean (29,978; 5,617; 351,089) | 1,456,802 | 99,986 / 2,519 | 99,987 / 2,520 | 1 / 1 | 0 / 0 |
    | Chinese (30,000; 542; 28,336) | 145,512 | 102,682 / 6,700 | 102,682 / 6,700 | 0 / 0 | 0 / 0 |
    | Polish (30,000; 3,209; 1,531,577) (filtered) | 30,000 | 5 / 5 | 5 / 5 | 0 / 0 | 0 / 0 |
    | German (30,000; 151,560; 15,397,542) (filtered) | 30,076 | 33 / 44 | 33 / 44 | 0 / 0 | 0 / 0 |
    | Turkish (30,000; 28,891; 8,127,142) (filtered) | 47,600 | 17,586 / 75 | 17,586 / 75 | 0 / 0 | 0 / 0 |
    | French (30,000; 16,349; 3,405,113) (filtered) | 30,000 | 10 / 13 | 10 / 13 | 0 / 0 | 0 / 0 |
    | Spanish (30,000; 9,428; 4,472,890) (filtered) | 46,740 | 16,424 / 341 | 16,424 / 341 | 0 / 0 | 0 / 0 |
    | Portuguese (30,000; 15,100; 3,199,534) (filtered) | 87,628 | 56,617 / 452 | 56,617 / 452 | 0 / 0 | 0 / 0 |
    | Italian (30,000; 5,715; 856,103) (filtered) | 39,464 | 9,412 / 36 | 9,412 / 36 | 0 / 0 | 0 / 0 |

    Every one of the 56 strict and 45 client-view texts is the label itself as written: the two Arabic labels'
    words glued (with a declared proclitic, or with a list "word" that is only a joiner or a split of the label
    between them: "word" + "the" + "passing" glued is the label's letters in a row), the Greek label's words
    glued in its written spellings (and the label glued with a declared ending, an elision or a full stop after
    it), and the Korean compound "secret" + "number" with a hyphen. Stage 2 (the real lanes on the 68,158 texts
    of the lists: every new text against any reference and 2,000 of the others per language): against 0334eeae,
    newly flagged 56, newly masked 45, a URL holding the text newly withheld 46 (raw path; 1 percent-encoded),
    a host built from it newly refused 56, exactly those texts; nothing the other way. Against ffd28ca8 and
    cbdf5e78, the four languages of MUST A:

    | Language | ffd28ca8 strict / client view | cbdf5e78 | this release | new against ffd28ca8, strict / client view (by class) | new against cbdf5e78 |
    |---|---|---|---|---|---|
    | Arabic | 35,766 / 445 | 44,610 / 529 | 7,276 / 313 | 54 / 12 (cv:label_words 11, cv:lone_lookalike 1, s:lone_lookalike 54) | 0 / 12 (cv:label_words 11, cv:lone_lookalike 1) |
    | Greek | 1,884,880 / 7,250 | 3,134,802 / 7,279 | 1,890,150 / 7,270 | 5270 / 20 (cv:label_words 20, s:label_words 4659, s:lone_lookalike 611) | 0 / 12 (cv:label_words 12) |
    | Persian | 46,361 / 1,323 | 66,503 / 1,323 | 41,744 / 1,064 | 31 / 3 (cv:label_words 1, cv:lone_lookalike 2, s:lone_lookalike 31) | 0 / 3 (cv:label_words 1, cv:lone_lookalike 2) |
    | Hebrew | 1,936,609 / 1,412 | 1,936,609 / 1,412 | 1,499 / 400 | 0 / 0 () | 0 / 0 () |

    (stage 1; the classes by this release's rules: the label's own words; (g) the lone lookalike; nothing else.
    The client-view "lone lookalike" of Arabic 1 and Persian 2 is a stage-1 artifact, the hyphenated "Hormuz" +
    "crossing" read on an ASCII-folded view that the client view does not read: in stage 2 it is not masked.)
    Against ffd28ca8 also: Japanese 2 / 60 (strict: (g); client view: 56 label words, 4 residual (h)), Hindi
    9 / 9 (the label's halves glued), Thai 48,497 / 2,257 (residual (a): the label as written inside a glued
    join), Russian 0 / 21 and Korean 2 / 105 (cases and particles, 6 residual (h); strict 2 (g)), Chinese 0 / 60
    (50 label words, 10 residual (h)); "other" 0 in every language. Stage 2 against ffd28ca8: a host newly
    refused only for these texts (the label, residual (a), (g)); against cbdf5e78: newly flagged 0, newly
    masked 24 (label words), newly refused 0.

    **Residual (g) by mechanism** (SHOULD 3; `h/g_class.py`: every strict-lane text newly read against
    ffd28ca8 only through the second lookalike pass, by where the lone letter stands): at 20,000 words 165
    (Greek 141: 118 between Latin letters, 23 at the edge of the join; Persian 20, Arabic 3: between Latin
    letters; Japanese 1: at the edge), the count of decision 73; at 30,000 words 700 (Greek 611: 552 between
    Latin letters, 29 at the edge of the join, 30 beside a word of another script; Persian 31 and Arabic 54
    between Latin letters; Japanese 2 and Korean 2 at the edge). The same texts at 0334eeae (0 differences).

    **M3, over-tightening** (`h/m3_eval.py`, `h/m3_machine.py`, `h/m3c_gen.py`): 1,320,550 hosts (696,028 in
    Unicode form, 624,522 in punycode: the 948,803 of decision 73 and 371,747 more built from this round's lists,
    words 20,001-30,000 and 2,500 seeded candidate joins per language) in the six lanes; 378,000 URLs from
    ordinary words, raw and percent-encoded; 14,200 neutral sentences in three casings; 120,000 machine values
    through `check`, the client view and `sink_guard.classify`. Against 0334eeae: accepted then refused 0,
    refused then accepted 0; URLs 0 and 0; sentences 0 rows; machine values 0. Against ffd28ca8: accepted then refused 36 lane cells, of 12 hosts: the Hindi label itself (as in decision 73) and 11 hosts built from the Greek label hyphenated with another word (a Unicode host holding a script label is refused in the URL, check and checked-host lanes), the label itself; refused then accepted 20,661; URLs 0 and 0; sentences 0; machine values 0. The real schedules
    route: 20,000 schedules, each with a distinct invoice id and a distinct ruling id: 20,000 of 20,000 answered 201.

    **M4, end to end through the real service** (`h/m4_6.py`, the source of each release with this release's test
    helpers):

    | | ffd28ca8 | 0334eeae | this release |
    |---|---|---|---|
    | the 27 forms of decision 72, 81 values: shown to the owner / fields masked of 81 / on disk | 21 / 60 / 0 | 0 / 81 / 0 | 0 / 81 / 0 |
    | the 10 forms of decision 73, 30 values: shown / fields masked of 90 / on disk | 24 / 18 / 0 | 0 / 90 / 0 | 0 / 90 / 0 |
    | the 190 glued Arabic and Greek forms of `tests/test_review_0334eeae.py`, 570 values (4 runs): shown / fields masked of 1,710 / on disk | 402 / 504 / 0 | 500 / 210 / 0 | 0 / 1,710 / 0 |
    | URL resolution, 5 own-site URLs of decision 73: fetched | 4 | 5 | 5 |
    | URL resolution, 80 own-site URLs holding ordinary phrases (10 Greek "code to" + bas-, 6 Thai joins, the Japanese loanword, 3 Hindi glued joins, the 60 Arabic S-2 phrases; 4 runs of 20): fetched / fields masked of 720 | 20 / 0 | 80 / 0 | 80 / 0 |

    **M5, differential fuzz** (`corp/m5.json`, the 130,000 texts of decision 72): against 0334eeae, strict lane
    newly flagged 56, client view newly masked 30, every one the Korean label with a hyphen put in it at its
    compound point (the generator's "split with a hyphen", the label's own two words), no longer flagged 0, no
    longer masked 0, no other finding changed. Against ffd28ca8: 90,540 -> 99,058 flagged (newly 9,531, no
    longer 1,013), 53,089 -> 60,304 masked (newly 7,252, no longer 37); the classes of decision 73.

    **M6, cost** (the code of this commit). `tests/test_secret_guard_perf.py`: 16 passed. Per script, CPU
    microseconds per sentence (1,000 sentences of each of 10 languages, `corp/m3.json`), ffd28ca8 / this release,
    best of 5 rounds, each round one fresh process per release, the order alternating, each process best of 5
    passes:

    | script | client view | strict lane (the five rounds) |
    |---|---|---|
    | ja | 27.7 / 26.6 (-4.2%) | 57.6 / 61.1 (+6.1%; +12.8, +3.8, +1.8, +10.4, +8.4) |
    | hi | 58.0 / 58.1 (+0.1%) | 125.7 / 134.1 (+6.7%) |
    | th | 34.6 / 34.1 (-1.4%) | 56.4 / 54.1 (-4.0%) |
    | ar | 114.3 / 101.4 (-11.2%) | 189.5 / 165.7 (-12.6%) |
    | fa | 110.8 / 99.8 (-9.9%) | 221.8 / 160.1 (-27.8%) |
    | ru | 148.7 / 132.4 (-11.0%) | 248.4 / 200.0 (-19.5%) |
    | el | 142.6 / 128.1 (-10.2%) | 266.0 / 285.2 (+7.2%) |
    | pl | 176.5 / 171.6 (-2.8%) | 270.7 / 266.6 (-1.5%) |
    | de | 186.5 / 184.2 (-1.2%) | 269.8 / 263.5 (-2.3%) |
    | en | 88.3 / 86.5 (-2.0%) | 133.8 / 123.8 (-7.5%) |

    The largest rise is Greek strict +7.2%. A first measurement of this round, with the NIT-1 shortcut removed
    altogether, had Japanese strict +24.9% (rounds +17.6% to +27.5%): the part-by-part fold ran on 701 of 1,000
    Japanese sentences; the exact sigma test (above) restores the shortcut where it is output-identical (checked:
    the Latin form of this commit and of the code M1 to M5 measured are equal on every inspection view of the M1
    cells, the M5 texts, the M3 sentences, every M2 word, 15,000 seeded M2 candidates per language in four forms
    and 400,000 random texts: 2,289,729 texts, 4,577,043 views, 0 differ). Cold start (24 fresh processes per
    release, interleaved, medians): the guard, the service and the first two calls 848.8 ms at ffd28ca8, 910.5 ms
    now (1.07x); the guard module alone, with its bytecode written 37.0 ms and 78.3 ms (2.12x), without bytecode
    (`-B`, caches removed) 58.3 ms and 114.9 ms (1.97x): the patterns of the boundary and glued rules and the import
    checks. Worst-case inputs (13 shapes, four of them new: Arabic proclitics + the first word glued and repeated,
    the Greek "code" glued and repeated, the Korean compound's first word + hyphen repeated, a tatweel run inside
    the label; 2,000 to 32,000 characters, both lanes, `h/m6_lin.py`): every doubling 1.9x to 2.1x but one noisy
    reading (the Greek glued shape in the client view 3.39x at 32,000; measured again: 1.96, 1.86, 1.93, 2.09):
    linear.

    **Residuals after this round** (decision 73's list, with this round's counts): (a) Thai 48,497 strict / 2,257 client-view texts at 30,000 words
    (3,158 / 634 at 20,000); (b), (c), (d) unchanged (M1: the "pw" cells 98); (e) closed; (f) unchanged; (g) as
    above, 700 at 30,000 words (165 at 20,000); (h) inside the client-view counts against ffd28ca8 above
    (Japanese 60, Korean 105, Chinese 60 texts at 30,000 words, label words and (h) together; the classifier
    names as (h) only the two-unit shapes, 4, 6 and 10; at 20,000 words decision 73 counted 82); the same
    texts at 0334eeae. No new residual: against 0334eeae nothing is read but the label forms of MUST A.

    **Tests.** `tests/test_review_0334eeae.py`: every glued form of the Arabic and Greek labels (190 forms: the
    words glued, with proclitics, a tatweel at the join or inside, each of the five invisible joiners at the join
    or inside, the Greek endings, final and medial sigma, UPPER, Title, the accented spellings, NFC and NFD)
    flagged and masked after ": " and " is ", bare and in a sentence; the 60 S-2 phrases and their forms with a
    tatweel, a joiner or NFD: 0 masked, 0 flagged; the hamza and madda read as written; the known trap (the
    Arabic entries glued on the word view mask the S-2 phrases) as a mutation; the glued pattern off; the final
    sigma rule; the Korean compound apart; the stem ffd28ca8 listed; the import check arm. SHOULD 1, one minimal
    positive and negative pin per rule: label words apart by one space, "_" or "-" only; the closed Korean
    particle list; up to three Hebrew proclitics; a two-letter label spelled with "-" or "." as a word of its own
    (no letter joined before or after); one or two separators between spelled units; a spelled mark on its
    letter or apart; every letter and mark of both Greek blocks continuing a Greek word, before and after the
    label. SHOULD 2: the boundary joins DERIVED from the label entries (and their written spellings) against a
    bundled sample of ordinary words of nine scripts (30 to 38 words each, from this round's M2 lists), spaced,
    hyphenated, glued and dotted, none read (a join whose words glued ARE a label form is read glued and with one
    space, as decision 73 and ffd28ca8 read it, never with "-" or "."); with the letters-only matching of
    cbdf5e78 put back, the derived joins are read (the test is not a snapshot); NIT 1, the sigma list against every
    code point and the shortcut without it read as another form; the snapshots of
    `tests/test_review_cbdf5e78.py` stay. In `tests/test_review_ffd28ca8.py` the glued Arabic natural spellings
    are restored as at cbdf5e78, with the vowelled ones glued too; in `tests/test_review_cbdf5e78.py` the two
    assertions that pinned the glued Arabic and Greek forms as not read now pin them read.

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
