# ADR 0015 — Influencer & Partnership Marketing (11)

Status: accepted for build, Oct 6 2026 (founder Q&A the same day). Not in force: no email provider is chosen, no
platform DM provider exists, no discovery source is connected, the Legal (37) and Finance (31) clients are stand-ins,
nothing is ever paid, and no department or agent calls this service yet. Wired into CI and `docs/test-counts.md` on
`wire-influencer-bizdev-11-12` at 241c54d (unlock item 9).

## Founder decisions (Andre, Q&A Oct 6 2026 — binding)

| Question | Answer |
|---|---|
| Brands | **Both**: ZBM (Z Best Media) and ZBC (Z Best Clips), one service. |
| Paying influencers | **Stripe payouts, the same as ZBC clippers.** Intake their tax info, verify them, pay out only once verified. Payouts go through a **Finance (31) port**; this service never calls Stripe. **Never store a raw TIN, SSN or EIN**: a reference or token only; raw values refused 422. Nothing is ever actually paid (fixtures only). |
| Outreach email | May go out **automatically**: Andre-approved templates matched by content hash, CAN-SPAM (postal address, one-click unsubscribe), a suppression list shared across both brands, a separate outreach domain (sales-py's design). |
| Platform DMs (Instagram, TikTok, X, YouTube) | **Always need Andre**: the AI drafts, Andre approves each one by hash. No DM provider yet: approved DMs stay queued. |
| Replies | **Any reply on any channel holds further automatic outreach** to that influencer until Andre decides (sales-py's fail-closed reply design). |
| Deal approval | **Any influencer deal over $5,000 total goes to Andre**, aggregated across the influencer's open deals and per campaign so a split deal cannot get under the limit (sales-py S1-H2). Money is Decimal strings; floats refused. |
| Discovery | Public profile data plus a paid influencer-database provider later: both **NOT_BUILT ports** refusing `SOURCE_NOT_WIRED`; **no scraping code**. Inbound applications accepted. |
| FTC Endorsement Guides (16 CFR Part 255) | **Fail closed**: every sponsored-content brief carries a required clear disclosure; content submitted without it is refused; Andre approves final content by hash before the campaign counts it live; material-connection records are kept. |
| Minors | The influencer **attests to being 18 or older**; under 18 or no attestation is refused; only the attestation flag is stored, **never a date of birth**. |
| Contracts | Through a **Legal (37) client handoff** (service-py's `legal_client` pattern); while Legal is a stand-in, sending a contract is refused `LEGAL_UNAVAILABLE`. |
| Partnership marketing | Co-marketing with brands and agencies **at campaign level** belongs here. Referral and alliance partner commissions are **department 12's** (built in parallel), not here. |
| Models | No model calls: deterministic single-task intelligences (house rule). |

## Decisions

1. **One service, `services/influencer-py`** (Python, FastAPI, pydantic strict; the stack of the other departments).
   Ledger department `influencer`, event ids `if-<abbr>-<40 hex>`, port 8480, `X-INF-Caller-Token`, env prefix `INF_`,
   routes under `/inf/v1`.
2. **Callers** (`config.KNOWN_CALLERS`), each with its own token in `INF_CALLER_TOKENS`: `hub` (the creator application
   form, the creator portal: tax reference and content; the unsubscribe page), `dashboard` (Andre's console; with
   `X-Andre-Approval-Token`, Andre himself), `influencer_agent` (the agent runtime), `scheduler`, `provider_events`
   (bounce, complaint and reply webhooks from the email provider and the platforms), `compliance_38` (audit reads).
3. **Fail closed at start.** Missing service token; a production start without `INF_DATA_DIR`; no
   `INF_PII_HASH_KEY_FILE` in production or with any `INF_DATA_DIR`; an outreach domain without both brand domains,
   sharing a registrable domain with either, or without `INF_POSTAL_ADDRESS`; a `noreply` From mailbox;
   `INF_AUTO_APPROVE_MAX` above 5000.00; or any switch that would select an unbuilt provider or client
   (`INF_EMAIL_PROVIDER`, `INF_DM_PROVIDER`, `INF_PUBLIC_PROFILE_PROVIDER`, `INF_PAID_DATABASE_PROVIDER`,
   `INF_FINANCE_URL`, `INF_LEGAL_URL`) refuses to start (security-py's NOT_BUILT pattern).
4. **Record-first, anchored on ledger-rust** (security-py's store as fixed in ADR 0012 rounds 1-5, with service-py's
   closed-instance and claim rules from ADR 0014 rounds 5-5e). Every change is prepare → pending line (fsynced aside)
   → typed evidence events → anchor (`log_anchor`) → exact-size append (adopt / truncate) → apply (the same `_apply`
   replay uses). The service keeps its own pending line in memory; a pending line found on disk at start is appended
   only if the ledger already holds its anchor, otherwise set aside to `pending.discarded` (inert). A blank log line
   refuses start. Every local line must have its anchor and the ledger no anchor this log lacks (truncated, replaced or
   edited log: 503 `INTEGRITY_UNVERIFIED`). Idempotency keys are `op|target|request_id` per caller, with the body's hash
   (`REQUEST_ID_REUSED` on another body). One process per data directory (`flock`) and one service instance per
   process (a single-use claim token adopted under a mutex). `close()` makes an instance inert: no log line, pending
   file, ledger event or port call; its integrity, job and audit routes answer 503 `SERVICE_CLOSED` and `/health` 503.
   The ledger's own `verify()` runs outside the service lock and `ledger_valid` is always its real verdict.
5. **Single-task intelligences** (`src/intelligences/`, deterministic, no model): i01 intake admissibility and the 18+
   rule, i02 identity (canonical email and handle, keyed hashes), i03 discovery fit, i04 suppression, i05 email
   template guard (sales-py's i08), i06 send pace, i07 reply classifier (sales-py's i10), i08 FTC disclosure, i09 deal
   approval, i10 DM guard, i11 audit export.
6. **Discovery.** `inbound_application` only from `hub`; `manual_research` (a profile a person found) only from
   `dashboard` with an evidence reference; `public_profile` and `paid_database` only through their ports
   (`POST /inf/v1/discovery/import`; stand-ins 503 `SOURCE_NOT_WIRED`). A wired source's records pass the same gates
   as a request body (no tax id, no date of birth or age, the strict prospect model) and are never attested. There is
   no scraping code.
7. **Identity and minimum data.** Emails are sent to the address as given (lowercased); dedupe, suppression and holds
   match the canonical form: `+tags` removed for every domain, and for gmail.com / googlemail.com every dot of the
   local part removed and the domain read as gmail.com (AEGIS R1-L2); handles NFKC, lowercased, one `@` dropped,
   ASCII `[a-z0-9._-]{1,60}` only (anything else refused, never altered), one handle per platform. Dedupe,
   suppression, holds, ledger events and the export use HMAC-SHA256 under `INF_PII_HASH_KEY_FILE` (`email:<hex>`,
   `handle:<hex>` over platform + handle); the key's fingerprint is bound in the log at first start and another key
   refuses start. The local log is the system of record (it holds the email and handles); the ledger and the export
   never do.
8. **Minors and confirmation.** An application must carry `adult_18_plus: true` exactly (a string, number or missing value is not an
   attestation) with the attestation text's version and SHA-256; only the creator's own form (`hub`) attests. `false`
   is refused 422 `MINOR_REFUSED` and nothing about the applicant is kept; missing is 422 `AGE_ATTESTATION_REQUIRED`.
   No date of birth, age or birth year is accepted anywhere (`FORBIDDEN_FIELD`). *AEGIS round 4 (M1′; replaces the
   round 1-3 payload-bound confirmations):* the mailbox is verified FIRST and the application is taken after. The public
   form (`POST /inf/v1/applications`) takes an address only; each canonical address has one open address link (HMAC
   under the PII key, never stored, single use, 7 days), reused by every repeat request and mailed at most once a day
   (from send time) as a fixed service text to the address on the record. The click (`POST /inf/v1/confirmations`,
   `hub`) opens a creator session (`INF_CREATOR_SESSION_MINUTES`, default 60) bound to the record, and the attestation,
   handles and details (`POST /inf/v1/sessions/application`) apply at once inside it; a record is created only then.
   No link mail goes to a suppressed address (until Andre approves that mail by hash) or a blocked record (a reply hold
   does not stop it: it answers the creator's own request). A researched or
   imported prospect is
   never attested: it may be contacted under the outreach rules but gets no brief-based deal, contract, content,
   tax reference or payout until the creator applies with the same address. When a declared-minor application is
   submitted in a session for the address of a record we hold, that record is **frozen at once** (`blocked: MINOR_DECLARED`: no outreach, deal,
   contract, content or payout; queued messages cancelled) and waits for Andre's minor review: `confirm_minor` keeps it
   blocked for good and suppresses every address and handle; `not_a_minor` releases it with the attestation the
   creator had made before the declaration, if any (AEGIS R1-L4; a record that never attested stays unattested).
9. **Andre's approvals** use legal-py's FounderGate: the `dashboard` caller AND `X-Andre-Approval-Token`
   (`INF_ANDRE_APPROVAL_TOKEN`); a token equal to the service token or any caller token counts as not configured.
   Refusals are recorded on the ledger (`founder_approval_refused`). Andre approves template versions, DM drafts,
   briefs, deals over the limit and final content — each by its content SHA-256 — and decides holds and minor reviews.
10. **Outreach email (CAN-SPAM)** — sales-py's design. Only from `INF_OUTREACH_DOMAIN` (separate registrable domain
    from both brands), only from an Andre-approved template version whose current content hash equals the approved
    hash (templates are never edited in place: a change is a new version); deceptive subjects refused at draft,
    approval and send; the only merge field `{{first_name}}` renders only a name a person verified at the console; the
    service appends the brand, `INF_POSTAL_ADDRESS` and a one-click unsubscribe link with `List-Unsubscribe` and
    `List-Unsubscribe-Post` (RFC 8058). At most `INF_DAILY_SEND_CAP` per outreach domain per UTC day. Rules are
    re-checked at send time; `outreach_send` is on the ledger before the provider is called.
10b. **Platform DMs** need Andre every time: the agent drafts (`POST /inf/v1/dm-drafts`), Andre approves the exact
    text by its hash (influencer, platform, handle hash, brand, text), which queues it. There is no DM provider: it
    stays queued. At send time the draft, its hash and the handle are re-checked; any change cancels it.
11. **Suppression** is one list across both brands, append-only, with no removal route. The one-click link, an opt-out
    reply, a complaint, Andre's `opt_out` hold decision and a confirmed minor suppress EVERY address and handle of the
    influencer, so they stop email AND DMs for both brands; queued messages are cancelled in the same log line.
12. **Replies** (sales-py's S3-C1 design, no interpretation; never refused for a sender field — an unknown message
    id, an unreadable address or handle, a handle on an email reply are ignored, `Name <addr>` is read, and a reply that
    resolves to nothing is still recorded as an active, unresolved hold for Andre, AEGIS R1-M4): ANY reply on ANY channel (email or a platform DM) holds
    every further outreach to that influencer — email and DMs, both brands — "yes" and "interested" included, until
    Andre decides (`continue` lifts the hold and nothing else; `opt_out` suppresses). The one exception is an email
    whose raw body is exactly a fixed machine auto-reply. A hold covers the influencer it resolved to AND every address
    and handle it names, so an unresolved reply holds whoever the sender turns out to be. Opt-out wording also
    suppresses at once (a DM gets the lower SMS-style bar). The text is never stored, only its SHA-256; the reply route
    is never refused for its text, sender address or handle content (an opt-out must land).
13. **No raw tax ids.** Before any body is parsed (textguard.py): a key naming a tax id in any spelling
    (`ssn`, `taxId`, `tax-id`, `ein`, `itin`, `tin_last4`, ...) or a value shaped like one (exactly nine digits of any
    script standing alone, optionally split by up to three spaces, dots, underscores or dashes; in free text a letter
    may touch it) is refused 422 `TAX_ID_REFUSED`. *AEGIS round 1 (M3):* after NFKC (any script's digits as ASCII),
    up to 8 characters that are not letters or digits — spaces, punctuation, symbols, combining marks, invisible format
    characters — between two digits do not end a number; free text also refuses the classic 3-2-4 / 2-7 shapes inside
    longer runs; `tax_ref` refuses ANY number of nine or more digits. Money fields are not scanned. A tax profile holds
    only the form kind (W-9 for a US person, W-8BEN for a foreign individual, W-8BEN-E for a foreign entity), the legal
    form, the country and a provider reference — `stripe:acct_` + 16..64 letters and digits, or `vault:` + exactly 26
    lowercase letters (the vault's reference alphabet has NO digits, so a reference's shape can never hide a tax id;
    AEGIS R2-L-b) — (its SHA-256 on the ledger). Whether a reference EXISTS is Finance's to confirm (unlock item 5). Every tax-reference change is made inside a
    creator session (decision 8, AEGIS round 4), for that session's record, once per session, with no per-address cap;
    for a creator whose payee is already verified it then also waits for Andre's approval of its hash
    (`POST /inf/v1/confirmations/{id}/approve`).
14. **FTC Endorsement Guides, fail closed** (i08). A brief's disclosure comes from a closed list (`#ad`, `#sponsored`,
    or the brand's own `Paid partnership with ...` / `Sponsored by ...`; the other brand's phrase is refused); the
    service appends a fixed FTC section (the material connection, the exact disclosure and where it goes, the platform
    label, say it in videos, honest opinions and actual use only) that the brief cannot leave out, and Andre's approval
    binds the hash of the brief as issued. Content is refused unless the exact disclosure (ASCII, case-insensitive,
    token-bounded so `#adventure` is not `#ad`) is on the caption's first line, ends within the first 100 characters,
    comes before any other hashtag and has no combining mark touching it (AEGIS R1-L1); any invisible character (bidi controls, zero-width space, word joiner, BOM, tag or private-use characters;
    a zero-width joiner only between two emoji) is refused; on Instagram, TikTok and YouTube the platform's
    paid-partnership label must be on. Andre approves the final content by its hash; the campaign counts content live
    only when it is approved (the hash named again) and the deal's contract is in force.
15. **Deals** (i09). A deal's total is its cash fee plus the value of any product. It is approved without Andre only if
    D1 the deal, D2 the deal plus EVERY other deal of the same person that was not rejected or cancelled — lifetime,
    both brands, every campaign, completed deals included; the person is the record and every record sharing its tax
    reference (applied or awaiting confirmation) or Finance payee — and D3 the same within the campaign all stay within
    `INF_AUTO_APPROVE_MAX` (default and ceiling 5000.00); otherwise `pending_andre` naming each rule that failed
    (AEGIS R1-H1, R1-M5). The rule is applied again at payout time keyed on the payee (decision 18). Money is canonical Decimal strings (floats, ints,
    exponents refused 422). A deal needs an attested, unblocked influencer, an active campaign and an approved brief of
    that campaign; it is immutable (a change is a new deal); it can be cancelled only before its contract is in force.
    A deal whose cash fee is fully submitted to Finance is `completed`.
16. **Contracts** go through the Legal (37) port: send the agreement for THIS deal (its hash, its brief's hash and
    disclosure) and later confirm it is in force. Legal is never called with the lock held. Stand-in: 503
    `LEGAL_UNAVAILABLE`, nothing recorded.
17. **Ports** with stand-ins: email sender and DM sender (`not_wired`: messages stay queued), the two discovery sources
    (`SOURCE_NOT_WIRED`), Legal contracts (`LEGAL_UNAVAILABLE`), Finance payees and payouts (`FINANCE_UNAVAILABLE`).
18. **Paying influencers.** `POST /inf/v1/payees/{id}/verify` registers the payee at Finance from the tax reference —
    with the creator's confirmed identity (record id and SHA-256 of the confirmed address) for Finance to match the
    KYC against (AEGIS R1-M2) — and reads its verification; only Finance's `verified` counts (KYC and TIN match happen at Finance and Stripe). A new tax
    reference resets verification. A payout request needs a contracted deal, live content Andre approved (each content
    paid once), an amount within the deal's remaining cash fee, the stored verification AND Finance's verification read
    again at request time; it is recorded (`payout_requested`) before Finance is called with the payout id, and
    `payout-retry` re-sends what Finance could not take (the adapter must be idempotent on the payout id). One
    hand-over per payout at a time, and Finance's verification is read again before each (AEGIS R1-L3). When the
    PERSON's lifetime deal total (decision 15, keyed on the payee and tax reference) is over the limit and the deal was
    not approved by Andre himself, the payout waits `pending_andre` for his approval of its hash
    (`POST /inf/v1/payouts/{id}/approve`; AEGIS R1-M5). A waiting
    payout whose payee changed or whose influencer was blocked is cancelled, never handed over. Finance's refusal
    releases the amount. This service never calls Stripe and never pays.
19. **Material connections**: recorded when a deal is approved (by the limit or by Andre) — influencer, brand,
    campaign, kinds (payment, free product), amounts and the disclosure — as a typed ledger event and readable by
    Compliance (38).
20. **Audit and errors.** Typed ledger events carry ids, codes, hashes and amounts only, never an email, handle, name,
    text or tax reference. `GET /inf/v1/audit/export` drops emails and handles (their keyed hashes stay) and shows
    names, texts and caller references as SHA-256. Error bodies carry a code from the closed catalogue (`reasons.py`)
    and never echo input. Every response is `Cache-Control: no-store`; /docs and /openapi.json are off; the shared
    request limits, hardened launcher (`serve.py`, `INF_` prefix) and graceful close apply.
21. **Jobs** (`scheduler`, one at a time): `send-queue`, `payout-retry`, `integrity` (always reads the ledger).

## Defaulted decisions (not asked in the Q&A; the reviewer and Andre should confirm)

| Decision | Default chosen | Why |
|---|---|---|
| What "split" aggregates over | *Corrected in AEGIS round 1 (R1-H1, R1-M5):* per PERSON (the record plus every record sharing its tax reference or Finance payee), LIFETIME: every deal not rejected or cancelled, across both brands, all campaigns, completed deals included; and again at payout time keyed on the payee. (Round 0 counted only OPEN deals across campaigns, so deals run one after another were each approved alone.) | "Any influencer deal over $5,000" is per person; a time window would be a new rule, so it is NOT_BUILT (`INF_DEAL_AGGREGATE_WINDOW_DAYS` refuses start) |
| Pending deals in the person total | Counted | Fail closed: a $1 deal beside a pending $6,000 deal still reaches Andre |
| Product value | Counted in the deal total | A product given is a material connection and part of the deal's value |
| Contacting unattested prospects | Allowed (email under the outreach rules; DMs only with Andre's approval); no deal, contract, content, tax reference or payout until the creator attests | Recruiting needs a first contact; the 18+ rule applies where we contract |
| A declared minor naming a record we hold | Frozen at once, Andre reviews (not suppressed automatically) | Anyone can type an address into a public form: a stranger may freeze a record but never permanently opt a creator out (sales-py S5-L2) |
| Unblocking a confirmed minor | Not possible here | Unlock list item 7 |
| Daily email cap | 50 per outreach domain, no warm-up schedule | Influencer outreach is low volume |
| Disclosure placement | Ends within the first 100 characters and before any other hashtag | The FTC asks for disclosures that are hard to miss, not after "more" or in a hashtag block |
| Content before contract | Submittable and approvable when the deal is approved; counted live only when contracted | Andre can review drafts while Legal works; nothing counts or pays before a contract |
| Payouts | Requested by worker callers for live, approved content; the deal was already approved under the $5,000 rule | Finance applies its own approvals before any money moves |
| Who may record a tax reference | `hub` (creator portal) and `dashboard` | The agent never touches tax information |
| Amounts on the ledger | Deal totals, fees and payout amounts are on typed events | Amounts are audit evidence, not personal data |
| Port | 8480 | 8470 is the Stripe gateway's |

## How the other departments use it (not wired yet)

| Department | Here | Needed there |
|---|---|---|
| Sites / creator portal (`hub`) | applications with the 18+ attestation, tax reference, content drafts, unsubscribe | the forms and the `https://<outreach domain>/u/<token>` page |
| Legal (37) | `ports.LegalContracts`: send the influencer agreement for a deal hash, read in force | an influencer-agreement document type and a caller for this department |
| Finance (31) | `ports.FinancePayees`: register payee from a reference, verification status, payout request | a payee kind `influencer` (today only `clipper`) and a payout intake for it, idempotent on our ids |
| Compliance (38) | reads suppressions, material connections, the audit export and integrity | a reader |
| Referral & alliances (12) | nothing: partner commissions are there | — |

## Known limits (accepted for v1)

- A message whose outcome could not be recorded after the provider was called stays `sending` and is never resent on
  its own (sales-py's rule).
- The 18+ attestation is the creator's own statement; age is verified only as far as Finance's / Stripe's KYC does
  before any payout. A stranger who knows a prospect's address can attest on the prospect's behalf; the contract
  (Legal, signed by the creator) and payee verification (Finance) are the further gates.
- The hub is trusted to submit content and tax references only for the creator who is signed in to the portal.
- The disclosure check reads the caption; spoken and on-screen disclosures in a video are covered by the brief and by
  Andre's review of the final content, not checked by the service.
- A `stripe:` / `vault:` reference that happens to hold nine digits standing alone is refused (fail closed).
- ledger-rust has no filtered read, so every integrity check reads the whole ledger (as ADR 0012).
- The local log holds emails and handles (system of record); an erasure design (CCPA) is an unlock item.

## Not built (unlock list)

1. The email provider adapter and its webhooks (`/inf/v1/events/email`, `/inf/v1/replies`), DNS (SPF, DKIM, DMARC)
   for the outreach domain, and the hub page serving the one-click unsubscribe.
2. Platform DM providers (Instagram, TikTok, X, YouTube) and their reply webhooks.
3. The paid influencer-database adapter and public-profile data source (through the ports only; no scraping).
4. The Legal (37) contract client (needs an influencer-agreement document type in legal-py).
5. The Finance (31) payee and payout client (needs an `influencer` payee kind and payout intake in finance-py). When it
   is wired it must (a) confirm that a `stripe:` / `vault:` tax reference EXISTS and belongs to the creator before it
   registers the payee (AEGIS R2-L-b: the shape is no longer guessed here), and (b) return its per-person key — an
   opaque keyed hash of the matched TIN — on `register_payee` and `payee_status` (AEGIS R2-N2). The vault must issue
   references as `vault:` + 26 lowercase letters. **Nothing ships until Finance confirms a vault reference EXISTS**
   (AEGIS R3-L4): an all-letter reference can still encode digits (`a`..`j` for `0`..`9`), so its shape alone
   proves nothing; the reference must resolve at Finance / the vault to a record of that creator before any payee is
   registered or paid.
6. Moving Andre's approvals from the shared approval token to Cybersecurity (22) passkeys, as the other departments
   move.
7. An un-block path for a confirmed minor who later turns 18 (today: never contracted again).
8. A CCPA erasure design for the local log that keeps the hash chain and the suppression hashes.
9. ~~Wiring into CI (`ci.yml`, the hygiene checker's `PY_SERVICES`) and `docs/test-counts.md` (left to the integration
   lead).~~ **Done** on branch `wire-influencer-bizdev-11-12`, commit 241c54d.
10. **A creator who lost their mailbox** (AEGIS R5 Info). There is NO route today to change a record's email or move
    its payee: the address on the record is the only way into a creator session, and the dedupe index never moves an
    address. Until it is built the operator procedure is: Andre verifies the person out of band (Finance's KYC on the
    existing payee, or the platform accounts on the record), then a NEW application from the new address creates a new
    record (handles the old record holds are not moved), the old record is blocked or suppressed by Andre, and the new
    record goes through its own tax reference, Finance verification and the per-person aggregation (Finance's person
    key links the two for the $5,000 rule). To build: an Andre-only route `POST /inf/v1/influencers/{id}/email` that
    binds the change to the SHA-256 of {record id, old address hash, new address hash, payee reference hash}, resets
    the payee to unverified, mails a notice to BOTH addresses, and is recorded on the ledger.

## Hub requirements (the creator portal / public forms)

- **Per-IP rate limiting and a CAPTCHA (or equivalent) on the creator application form and the tax-reference form**
  (AEGIS R2-N1). This service bounds what it can see — one open address link per address, mailed at most once a day,
  its own confirmation queue and daily cap, apart from outreach — and never refuses a repeat request for the same
  address (round 4: any per-address limit before the click is a lockout of the real creator); it never sees the
  caller's IP, so stopping a flood of distinct junk addresses is the hub's job.
- The one-click unsubscribe page (`/u/<token>`) and the link page (`/c/<token>`) on the outreach domain relay the token
  to this service; the link page receives the creator session token and keeps it for the session's forms only (never
  in a URL, a log or analytics), and sends it with the application and tax-reference forms.
- The hub submits content only for the creator signed in to the portal.
- *(AEGIS round 5.)* Per-IP AND per-session rate limits on the public form, the link page and the session forms, plus a
  CAPTCHA (or equivalent) on the public form (R5-M2). The link page `/c/<token>` confirms only on an explicit button
  press, never on a GET, so mail scanners and prefetchers cannot use up the single-use link; the hub derives the
  `request_id` of the click from the token so repeat clicks and retries return the same session (R5-L1). Both are in
  `services/influencer-py/README.md`, "Contract with the hub".
- *(AEGIS rounds 6-7.)* Every public application carries `requester_key`: the hub's HMAC-SHA-256 (under a secret only
  the hub holds) of the requester's IP or portal session, 64 lowercase hex, computed by the hub itself — never a value
  the client controls passed through. Users behind one shared IP (NAT) share a bucket when the key is IP-based. The service validates its shape, keeps only
  its own keyed hash of it, and shares the new-address link-mail queue fairly between requesters (R6-L1). Without it a
  request falls in the keyless bucket, which never displaces keyed mail.

## Settings

All settings are in `services/influencer-py/README.md`.

## Amendment — AEGIS round 1 (Oct 6 2026, on a58e633): BLOCKING, every finding fixed

Regressions: `services/influencer-py/tests/test_aegis_r1.py` (each of the reviewer's probes; each fails on a58e633 and
passes after the fix).

| Id | Finding | Fix |
|---|---|---|
| R1-H1 (High) | Deals run one after another (each paid, so `completed`, before the next) were each approved alone: $15,000 to one creator, Andre never asked | The per-person total counts EVERY deal not rejected or cancelled, lifetime (D2); a window is NOT_BUILT (`INF_DEAL_AGGREGATE_WINDOW_DAYS`) |
| R1-M1 (Medium) | An application attached the applicant's handles to the record holding the typed address; a reply from that handle then suppressed the real creator | Applications change no identity field until a token mailed to the address on record comes back (decision 8) |
| R1-M2 (Medium) | Anyone could attest for a prospect; the hub could replace a tax reference at any time | Attestation, handles and every tax-reference change wait for the email confirmation; a change for a verified payee also needs Andre's approval by hash; Finance gets the confirmed identity to match |
| R1-M3 (Medium) | Raw tax ids got through: zero-width characters, `/ , : * ⁃`, 4+ spaces as separators, `vault:ssn123456789` as a reference | Gap-tolerant digit grouping after NFKC (decision 13); provider formats for `tax_ref`; no run of 9+ digits in it |
| R1-M4 (Medium) | Opt-out replies were lost on an unknown message id (404), `Name <addr>` (422) or a handle on an email reply (422) | A reply is never refused for its sender fields; what does not resolve is ignored; nothing resolving is still a hold for Andre |
| R1-M5 (Medium) | Totals were per record, not per person | Records sharing a tax reference or payee are one person for D2/D3; the rule is re-checked at payout keyed on the payee (`pending_andre` payouts) |
| R1-L1 (Low) | A disclosure below a fold of blank lines, or struck through with combining marks, passed | First line only; a combining mark touching it is `DISCLOSURE_OBSCURED` |
| R1-L2 (Low) | Gmail dot and `+` variants were different identities | Canonical form for dedupe and suppression (decision 7) |
| R1-L3 (Low) | A payout could be handed to Finance twice (request vs retry job, both unlocked) | Per-payout in-flight guard; verification re-read from Finance before each hand-over |
| R1-L4 (Low) | A false minor declaration left a real creator unattested after Andre's release | `not_a_minor` restores the attestation made before the declaration |

Residuals (accepted): a `vault:` UUID that happens to hold nine or more digits in a row once separators are dropped
(about one in seven random UUIDs) is refused: the vault adapter issues another. A free-text number of exactly nine
digits that is not a tax id (for example `10/06/2026 9am`, or `$1,000,000.00` written with cents) is refused
(fail closed). The confirmation proves control of the mailbox, not identity; identity is Finance's KYC, matched
against the confirmed address.

## Amendment — AEGIS round 2 (Oct 6 2026, on 50312db): NOT BLOCKING, every follow-up fixed

Regressions: `services/influencer-py/tests/test_aegis_r2.py` (each `test_vuln_*` probe; each fails on 50312db and passes
after the fix).

| Id | Finding | Fix |
|---|---|---|
| R2-N1 | Junk applications flooded confirmation mail: one address could be mailed twenty times, and fifty junk addresses used up the outreach cap so a real creator's confirmation did not go | One OPEN confirmation per record and kind: the same request reuses it, a different one supersedes it (the old token dies) *(superseding replaced in round 3, R3-M1)*; at most one confirmation mail per address per 24 hours *(from send time since round 3)*; five applications per address per 24 hours (429 `APPLICATION_RATE_LIMITED`) *(removed in round 4)*; confirmation mails have their own queue (`INF_CONFIRMATION_QUEUE_MAX`, default 2000) and daily cap (`INF_CONFIRMATION_DAILY_CAP`, default 200), apart from outreach, and are sent before outreach with records we already hold first. Per-IP limits and a CAPTCHA are a hub requirement (above) |
| R2-N2 | Two tax references of one person were two people | Finance's per-person key (an opaque keyed hash of the matched TIN, returned by `register_payee` / `payee_status`; the stand-in returns none) ties records together for D2/D3, and is read again at payout. A payout with NO person key yet, on a deal Andre did not approve himself, waits for Andre (`PERSON_KEY_MISSING`) — read as "fail closed while the person cannot be proven under the limit" |
| R2-N3 | A long reply text (422), a message id shaped like a tax id (422), or any other provider field could refuse a reply | The reply route reads every field leniently: any JSON type, unknown fields ignored and never stored, no tax-id scan (nothing raw is stored), text cut to 20,000 characters before it is classified and hashed, an unknown channel is `other` (the lower opt-out bar), a missing request id replaced by the body's SHA-256, a body that is not an object read as empty; the route accepts up to 512 KiB (the relay truncates beyond that) |
| R2-L-a | The confirmation for an existing record went to the address typed into the form (`owner+evil@...`) | It goes to the address ON THE RECORD |
| R2-L-b | Vault references were judged by shape (UUIDs, a 14% false refusal rate) | `vault:` + 26 lowercase letters (no digits); Finance confirms existence when wired (unlock item 5) |
| R2-L-c | A tax-id 422 did not say which field | `{"detail": "TAX_ID_REFUSED", "field": "handles[].handle"}`; a key that could itself carry the number is shown as `*`, and validation errors mask such keys the same way |
| R2-L-d | A suppressed creator could never apply again | The confirmation waits `awaiting_andre`; Andre approves THAT confirmation by its hash (`POST /inf/v1/confirmations/{id}/approve`), which mails it; the suppression stays in place for outreach |

**Log compatibility (Info).** Logs written by a58e633 (before round 1) do not replay on this version: their record
lines carry the attestation inline and their tax lines a different kind. No log of this service exists outside tests
and live runs (it is not in force), so this is accepted before launch; from launch on, log kinds only grow.

## Amendment — AEGIS round 3 (Oct 6 2026, on fa0c091): NOT BLOCKING, every item fixed

Regressions: `services/influencer-py/tests/test_aegis_r3.py` (each fails on fa0c091 and passes after the fix).

| Id | Finding | Fix |
|---|---|---|
| R3-M1 | A stranger could supersede a creator's open confirmation again and again (and the 24-hour rule counted from queue time, so a cancelled mail delayed the next) | No request invalidates another's open confirmation: each stays valid until it expires and applies only its own payload. At most three open confirmations per address (429 `CONFIRMATIONS_OPEN_LIMIT`, the open ones untouched). The one-mail-a-day rule counts from SEND time; a cancelled mail never delays the next. *(Replaces round 2's supersede rule; itself replaced in round 4, R4-M1′: an address-only link, then a session.)* |
| R3-L1 | `/replies` was idempotent on the request id alone: the same id with another body was a 409 that dropped an opt-out | The key is the request id AND the body's hash; the README states the remaining transport limits (413/415/400/422/408) as the contract with the relay |
| R3-L2 | Self-suppressed junk addresses could flood Andre's review queue | `INF_ANDRE_REVIEW_DAILY_CAP` (default 20) new review items a day; past it the item is kept as `awaiting_andre_digest` (approvable the same way, never dropped). Andre bulk-rejects with `POST /inf/v1/confirmations/bulk-reject`, bound to the SHA-256 of the exact id list, all or nothing |
| R3-L3 | Known creators could use the whole confirmation cap | `INF_CONFIRMATION_NEW_ADDRESS_PERCENT` (default 25%, rounded up) of the daily confirmation cap is reserved for new addresses |
| R3-L4 | A letter-only vault reference can still encode digits | Unlock item 5: Finance must confirm a vault reference exists before anything ships (no code change) |
| R3-L5 | In-memory rate state and target-less holds grew for ever | Rate state older than 24 hours is pruned (memory only, rebuilt from the log); the `hold-expiry` job closes an unresolved hold (no influencer, no address or handle) after `INF_UNRESOLVED_HOLD_DAYS` (default 30) on the service clock, recorded on the ledger (`holds_expired`) and anchored like any other change. A hold with a target is never expired: only Andre lifts it |

Accepted (until round 4, which removed payload-bound confirmations): with several open confirmations a creator could
click a stranger's link by mistake; the mail listed exactly the accounts that confirmation would attach.

## Amendment — AEGIS round 4 (Oct 6 2026, on 9170c01): NOT BLOCKING, redesigned

Regressions: `services/influencer-py/tests/test_aegis_r4.py` (each fails on 9170c01 and passes after the change).
Tests of rounds 1-3 that exercised the payload-bound confirmation were amended to the new flow (their intent kept:
nothing a stranger types binds to a record; the mail goes to the address on the record; a verified payee's tax
change waits for Andre).

| Id | Finding | Change |
|---|---|---|
| R4-M1′ | Per-address caps still let a stranger lock the creator out: before the click the service cannot tell the creator from a stranger, so any limit on payload-bound confirmations (three open, five a day) was a lockout, and it also blocked the creator's own tax change | **Verify the address first, then take the application.** The public form takes `{email, brand?}` only. One open address link per canonical address, reused by every repeat (never refused for being a repeat) and mailed at most once per 24 hours from send time; it carries no handle, attestation or payload, so a flood yields at most one harmless email a day. The 429 `CONFIRMATIONS_OPEN_LIMIT` and `APPLICATION_RATE_LIMITED` are removed. The click opens a creator session (`INF_CREATOR_SESSION_MINUTES`, default 60, service clock) bound to the record; inside it the 18+ attestation, handles and details (`POST /inf/v1/sessions/application`) and a tax-reference change (`POST /inf/v1/tax-profiles` with `session_token`) apply at once, with no per-address cap. A verified payee's tax change still waits for Andre's approval by hash; a suppressed address still waits for Andre (daily cap and digest); the mail always goes to the address on the record |
| R4-L1′ | A reply with the same body under a new request id opened a second hold, which kept holding after Andre lifted the first | A reply with the same target (influencer, address and handle hashes) and the same text hash as an ACTIVE hold attaches to it (`reply_ids`, ledger `outreach_hold_reply_attached`); one decision lifts it |
| R4-L5′ | `hold-expiry` closed an unresolved opt-out silently | An unresolved hold classified `unsubscribe` or `review` goes into Andre's digest (`GET /holds?status=digest`, ledger `holds_digested`, anchored) when it reaches `INF_UNRESOLVED_HOLD_DAYS`, and is closed only 7 days later if he has not decided it; other unresolved holds expire as in round 3 |

**The session token.** `<session id>.<HMAC-SHA-256 under the PII key of the session id>` — 256 bits, the house pattern
of the confirmation and unsubscribe tokens: never stored, re-derived to check, so it is in neither the log (which holds
only the session id) nor the ledger (which holds ids only). Deriving rather than drawing it at random lets a retried
click (same request id) answer the same session after a lost response or a restart, without the token ever being
written down. Its strength rests on the PII key (a 32-byte generated secret, mode 0600), which already guards every
confirmation and unsubscribe token.

**Single use per action (the choice).** A session may submit ONE application and make ONE tax-reference change; a
second of the same kind is 409 `SESSION_ACTION_USED` and needs a new click (a new link mail at most once a day, or the
open link if unused). Why: a session token can leak within its hour (a shared device, a browser extension, a proxy
log at the hub); single use per action bounds what such a leak can do to one change of each kind, each recorded on the
ledger (`creator_session_used`) and visible to the creator, and for a verified payee the tax change still waits for
Andre. A creator who mistypes a reference opens a new session; that cost is small next to a token that could rewrite
the payout destination repeatedly for an hour.

Accepted: a global flood of distinct junk addresses can fill the confirmation queue (429 `QUEUE_FULL`; since round 5,
R5-M2, the oldest new-address mail is evicted instead and mail for a record we hold is never refused) or use the
day's new-address share; that delays, never binds, and per-IP limits are the hub's (above). The link proves control of
the mailbox, not identity; identity remains Finance's KYC, matched against the confirmed address.

## Amendment — AEGIS round 5 (Oct 6 2026, on bb073dc): NOT BLOCKING, every item fixed

Regressions: `services/influencer-py/tests/test_aegis_r5.py` (each fails on bb073dc and passes after the fix).

| Id | Finding | Fix |
|---|---|---|
| R5-M1 | `_cancel_queued` compared `msg["influencer_id"] == influencer_id` with both `None`: an unrelated opt-out cancelled every queued link mail for a new address | The id matches only when there is one (`influencer_id is not None and ...`). Audit of every comparison of an optional id: `_held` hardened the same way (the record's id is never None today); the reply-attach rule compares `None == None` ON PURPOSE (two unresolved replies with the same text, class and hashes are one review entry — the hashes still decide) and now also requires the same class; the link checks in `svc_confirm` were already guarded (`c.get("influencer_id") and ...`); person keys, payee references and tax-reference hashes are only compared when present (`_person_keys` adds none that are empty); the query filters are `x is None or ...`; every other `==` compares ids that are always set (campaign, deal, brief, draft, message ids) |
| R5-M2 | The global link queue could be filled with junk so a creator's link was refused 429 | A link mail is never refused. `INF_CONFIRMATION_QUEUE_MAX` bounds NEW-address link mails only; mail for an address we hold a record for has its own share (bounded by the records: one open link and one queued mail per address). When the new-address share is full the OLDEST queued new-address mail is evicted (`QUEUE_EVICTED`; ledger `confirmation_mail_evicted`, anchored with the line that queues the new mail); its link stays valid and a repeat request mails it again. Per-IP and per-session limits and a CAPTCHA are the hub's (above) |
| R5-L1 | A mail scanner or prefetcher could use up the single-use link | Hub contract (above and in the README): the link page confirms only on a button press; the click's `request_id` is derived from the token, so repeat clicks get the same session |
| R5-L2 / L3 | A record created for the address between the click and the submit (a prospect, an import) got a DUPLICATE record from the session's application | The application looks the address up again under the lock and BINDS to the record it finds (the tax route resolves the session's record the same way). Chosen over refusing `STATE_CHANGED` because the click proved control of that very address, which is exactly what binds an existing record when it exists at click time; the application only adds the attestation and the handles no other record holds and never overwrites the record's identity fields (address, name, source, existing handles); refusing would force a new click (and a day's wait for the mail) for a race the creator cannot see. A declared minor in such a session freezes the record found |
| Info | No procedure for a creator who lost their mailbox | Unlock item 10: the operator procedure today, and the Andre-only, hash-bound route to build (not built) |

## Amendment — AEGIS round 6 (Oct 6 2026, on cd64e6b): NOT BLOCKING, the one Low fixed

Regressions: `services/influencer-py/tests/test_aegis_r6.py` (each fails on cd64e6b and passes after the fix).

| Id | Finding | Fix |
|---|---|---|
| R6-L1 | A sustained flood could keep one new creator's link mail evicted for ever: eviction took the oldest mail and sending also went oldest first | **Per-requester fairness** (the reviewer's preferred option). The hub sends an opaque `requester_key` (its keyed hash of the IP or session; 64 lowercase hex, validated; kept only as our own keyed hash of it). A requester has at most `INF_CONFIRMATION_PER_REQUESTER` (default 3) new-address mails queued — past it its OWN oldest is evicted; a full pool evicts the oldest mail of the requester with the MOST queued; requests without a key form their own bucket, which may evict only keyless mail (with none queued, one keyless mail may stand over the pool). Fallback for keyless traffic: a re-ask after an eviction is marked `reasked`, is evicted last within its bucket, and is sent ahead of first-time mail. *(Round 7 corrected this: the bucket itself was still chosen by ALL its mail, so re-asked mail was not protected across buckets — see R7-L1.)* Within a priority the send order is the queue order (a stable sort; no tie broken by message id). Every eviction stays recorded and anchored (`confirmation_mail_evicted`) and the evicted link stays valid |

Accepted: a flooder who rotates many requester keys (many IPs or sessions) still competes for the pool; bounding that
is the hub's per-IP / per-session limits and CAPTCHA, and the re-ask fallback keeps a creator who asks again ahead of
first-time mail.

## Amendment — AEGIS round 7 (Oct 6 2026, on 27bcd16): NOT BLOCKING, cleared for wiring; the three Lows closed

Regressions: `services/influencer-py/tests/test_aegis_r7.py` (each fails on 27bcd16 and passes after the fix).

| Id | Finding | Fix |
|---|---|---|
| R7-L1 | The bucket to evict from was chosen by ALL its mail, so a flooder rotating keys made one-mail buckets and a re-asked creator's mail was evicted anyway; round 6's wording claimed more than the code did | The bucket is chosen by FIRST-TIME mail only (the requester with the most first-time mail gives up its oldest first-time mail). Re-asked mail is neither counted nor evicted while any first-time mail is queued in the buckets the request may evict from; only when all of it is re-asked does a re-asked mail go. The per-requester cap evicts the requester's own oldest first-time mail first. README and this ADR now say exactly that |
| R7-L2 | Re-asking could be farmed (every evicted junk address re-asked over and over), and re-asked mail was evicted oldest first | A link earns ONE re-ask priority per eviction cycle: after its re-asked mail is queued it earns none again until one of its mails is actually SENT (or, after expiry, as a new link). Among re-asked mail, the link FIRST evicted most recently goes first: the oldest-evicted is kept longest (an order counter, rebuilt from the log, not the clock). Hub contract: the key is the hub's own HMAC under a hub-only secret, never a client-controlled value passed through; shared-IP (NAT) users share a bucket when the key is IP-based |
| R7-L3 | A re-send of the same link at the same instant reused the message id (derived from the link and the clock), overwriting the evicted mail | The id also carries the link's own mail counter: every mail of a link has a distinct id whatever the clock says |

## Amendment — Bug sweep E fixes (Oct 9 2026)

Regressions: `services/influencer-py/tests/test_bug_sweep_e_store.py` (each fails before the fix).

| Id | Finding | Fix |
|---|---|---|
| E-M1 (finance-py AEGIS 5a56a3a M1 / c0869c4 M1, backported) | `RecordLog.append_prepared` ignored a short `os.pwrite` (disk full, quota, a signal): a partial line stayed on disk that memory did not hold, and the append "succeeded" | A short write is a failed write: the file is cut back to its previous length, fsynced, and `StoreWriteError` is raised. If the cut-back itself fails the log carries `fault`: integrity reports `LOCAL_LOG_WRITE_FAULT`, `/health` says `degraded`, the authenticated status view carries `log_write_fault: true`, and every later append refuses until the file is inspected |
| E-M1b | `_write_file` (the pending-line files) ignored a short `os.write` too | A short write raises; the temp file is removed and never replaces the target |
