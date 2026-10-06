# ADR 0015 — Influencer & Partnership Marketing (11)

Status: accepted for build, Oct 6 2026 (founder Q&A the same day). Not in force: no email provider is chosen, no
platform DM provider exists, no discovery source is connected, the Legal (37) and Finance (31) clients are stand-ins,
nothing is ever paid, and no department or agent calls this service yet. Not wired into CI or `docs/test-counts.md`.

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
   No date of birth, age or birth year is accepted anywhere (`FORBIDDEN_FIELD`). *AEGIS round 1 (M1/M2):* an
   application changes no identity field of any record — no attestation, no handle — until the creator confirms the
   address: a one-time token (HMAC under the PII key, never stored, single use, 7 days) is mailed through the email port
   as a fixed service text to the address on the record, and comes back through `POST /inf/v1/confirmations` (`hub`).
   A new address gets a bare record (no handle, unattested, unconfirmed); no confirmation mail goes to a suppressed
   address or a blocked record (a reply hold does not stop it: it answers the creator's own request). A researched or
   imported prospect is
   never attested: it may be contacted under the outreach rules but gets no brief-based deal, contract, content,
   tax reference or payout until the creator applies with the same address. When a declared-minor application names
   the address of a record we hold, that record is **frozen at once** (`blocked: MINOR_DECLARED`: no outreach, deal,
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
    form, the country and a provider reference — `stripe:acct_` + 16..64 letters and digits, or `vault:` + a lowercase
    UUID — (its SHA-256 on the ledger). Every tax-reference change goes through the email confirmation (decision 8);
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
5. The Finance (31) payee and payout client (needs an `influencer` payee kind and payout intake in finance-py).
6. Moving Andre's approvals from the shared approval token to Cybersecurity (22) passkeys, as the other departments
   move.
7. An un-block path for a confirmed minor who later turns 18 (today: never contracted again).
8. A CCPA erasure design for the local log that keeps the hash chain and the suppression hashes.
9. Wiring into CI (`ci.yml`, the hygiene checker's `PY_SERVICES`) and `docs/test-counts.md` (left to the integration
   lead).

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
