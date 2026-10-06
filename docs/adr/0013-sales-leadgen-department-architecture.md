# ADR 0013 — Lead Generation & Opportunity Intelligence (26) + Sales (27)

Status: accepted for build, Oct 5 2026 (founder Q&A the same day). Not in force: no email, SMS or voice provider is
chosen, no lead source is connected, the Onboarding, Finance and Legal clients are stand-ins, and no department or
agent calls this service yet.

## Founder decisions (Q&A, Oct 5 2026)

| Question | Answer |
|---|---|
| One department or two | **One service** for Lead Generation (26) and Sales (27), for **both brands**: ZBM (full-service agency: Revenue Recovery, social, billboards/OOH, TV, radio, digital media buys, creative) and ZBC (Z Best Clips, clipping campaigns). Recruiting clippers is NOT here (Clipper Network owns it). |
| Lead sources | **All four:** inbound (site forms, the ZBC campaign inquiry form, free Revenue Recovery scan results), referral / partner, public data, paid provider. |
| Who sends cold email | **Agents, on their own** — only from a separate outreach domain, only Andre-approved templates, CAN-SPAM complete. |
| Texts and calls | **Only with recorded express consent** for that channel, inside 8 am – 9 pm recipient-local. |
| Pipeline | **Built in**, on the ledger; no external CRM. |
| Pricing | Two price books (zbm, zbc); every line exists with **no price** until Andre approves one; media buys are cost + markup (standard 15%, per deal variable, Andre-approved). |
| Who approves a proposal | Agents send at approved list prices **only** when total ≤ $10,000, no media buy, no discount, no custom term; everything else waits for Andre. |
| Models | **No model calls in v1**: deterministic single-task components. |

## Decisions

1. **One service, `services/sales-py`** (Python, FastAPI, pydantic strict; the stack of the other departments). Ledger
   department name `sales`, event ids `sl-<abbr>-<40 hex>`, port 8450, `X-SALES-Caller-Token`.
2. **Callers** (`config.KNOWN_CALLERS`), each with its own token in `SALES_CALLER_TOKENS`: `hub` (site forms, the ZBC
   inquiry form, consent capture, the unsubscribe page), `onboarding`, `detection` (scan results), `dashboard`
   (Andre's console: referrals, partners; with Andre's token, Andre himself), `scheduler`, `sales_agent` (the agent
   runtime that works the pipeline), `provider_events` (bounce, complaint and reply webhooks relayed from the send
   providers), `compliance_38` (audit reads).
3. **Fail closed at start.** Missing service token, a production start without `SALES_DATA_DIR`, no
   `SALES_PII_HASH_KEY_FILE` in production or with any `SALES_DATA_DIR`, `SALES_PRIMARY_DOMAINS` naming fewer than two
   registrable domains (both brands'), an outreach domain sharing a registrable domain with one of them (or set without
   them, or without `SALES_POSTAL_ADDRESS`), `SALES_AUTO_APPROVE_MAX` above 10000.00, a warm-up schedule that starts
   above 50 a day, decreases, more than doubles in a day or exceeds 500, a `noreply` From mailbox, or any switch that
   would select an unbuilt provider or client (`SALES_EMAIL_PROVIDER`, `SALES_SMS_PROVIDER`, `SALES_VOICE_PROVIDER`,
   `SALES_PUBLIC_DATA_PROVIDER`, `SALES_PAID_LEAD_PROVIDER`, `SALES_ONBOARDING_URL`, `SALES_FINANCE_URL`,
   `SALES_LEGAL_URL`) refuses to start (security-py's NOT_BUILT pattern).
4. **Record-first, security-py's store as fixed in ADR 0012 rounds 1-5.** Every state change is one log line: prepared,
   fsynced aside (`pending.line`), anchored on the ledger (`log_anchor`), appended, then applied by the same `_apply`
   replay uses. The own pending line is kept in memory and is the one trusted; a pending line found on disk at start
   is appended only when the ledger already holds its anchor, otherwise set aside to `pending.discarded` (inert);
   a blank log line refuses start; the append is exact-size with adopt / truncate; one process per data directory
   (`flock`). At start and before writes: every line has its anchor and the ledger holds none this log lacks; a
   truncated, replaced or edited log stops all writes (503 `INTEGRITY_UNVERIFIED`).
5. **Single-task intelligences** (`src/intelligences/`, deterministic, no model): i01 intake admissibility, i02 contact
   identity (normalise, keyed hashes), i03 scoring, i04 routing, i05 suppression, i06 consent, i07 quiet hours, i08
   template guard (honesty, hash, rendering), i09 send pace, i10 reply classifier, i11 pricing, i12 audit export.
6. **Lead sources.** `inbound` from `hub` (`site_form`, `zbc_campaign_inquiry`), `onboarding` (`site_form`) and
   `detection` (`rr_scan`, with its finding count); `referral` / `partner` only from `dashboard` and only with the
   referrer recorded; `public_data` and `paid_provider` only through their ports (`POST /sales/v1/leads/import`;
   stand-ins answer 503 `SOURCE_NOT_WIRED`), never over the lead API. Every lead keeps its source and every piece
   of evidence.
7. **Identity and minimum data.** Emails are lowercased with `+tags` removed, phones are E.164, company domains
   lowercased (free-mail providers are not company domains). Dedupe, suppression and the export use HMAC-SHA256
   hashes under `SALES_PII_HASH_KEY_FILE`; the key's fingerprint is bound in the log at first start and a later start
   with a different key refuses (a silently changed key would empty the suppression list). A person's record holds a
   name, email, phone, title and time zone; any body naming a date of birth, government id, payment card, bank
   account, IP, device or protected-trait field at any depth is refused 422 (`textguard.py`).
8. **Qualification and routing.** Fit (0-50) + intent (0-50) from named rules (i03 docstring: F1-F5, I1-I6); A ≥ 60,
   B ≥ 40 are `qualified`, C ≥ 20 `nurture`, else `new`. A ZBC campaign inquiry is ZBC; a scan is ZBM Revenue
   Recovery; otherwise the product lines decide; mixed or contradicting brands are refused; none is `unrouted` with a
   routing task. A repeat inquiry from the same email or phone adds evidence to the open lead (I6) instead of a new
   lead.
9. **Andre's approvals** use legal-py's FounderGate: the `dashboard` caller AND `X-Andre-Approval-Token`
   (`SALES_ANDRE_APPROVAL_TOKEN`); a token equal to the service token or any caller token counts as not configured.
   Refusals are recorded on the ledger (`founder_approval_refused`). Andre approves template versions (by content
   hash), prices, price withdrawals and proposals (by content hash).
10. **Cold email (CAN-SPAM).** Only from `SALES_OUTREACH_DOMAIN`, only from an Andre-approved template version whose
    current content hash equals the hash he approved (an edit after approval voids it: `TEMPLATE_HASH_MISMATCH`);
    subjects are checked for deception (fake Re:/Fwd:, alarm, account or billing bait, prize claims, shouting) at
    draft, approval, queue and send; the From line is the brand name at a mailbox that accepts replies. Every message
    gets, from the service and not the template, the brand, `SALES_POSTAL_ADDRESS` and a one-click unsubscribe link
    with `List-Unsubscribe` and `List-Unsubscribe-Post` (RFC 8058). The send pace: a warm-up schedule
    (`SALES_WARMUP_SCHEDULE`, default 20,30,40,60,80,100,150,200) capped by `SALES_DAILY_SEND_CAP`; it advances one
    step a day only through the `warmup-reset` job and holds when yesterday's complaints exceed 0.3% or hard bounces
    5%. Messages over the cap stay queued.
11. **Suppression** is one list across both brands, append-only with no removal path at all. Opt-outs (the one-click
    link, a reply, a STOP), hard bounces and complaints suppress at once, and queued messages to that address are
    cancelled in the same log line.
12. **Texts and calls (TCPA).** Only with express consent recorded for that phone, channel and brand (source, time,
    consent text version and SHA-256); consent to ZBM is not consent to ZBC. 08:00–21:00 in the recipient's IANA time
    zone; no time zone = refused. A revocation (form, call, STOP) revokes every channel for both brands and suppresses
    the number; a later grant cannot undo it. Re-checked at send time; a text outside the window waits in the queue.
13. **Replies** (i10): unsubscribe words and declines → suppressed now, on every channel: every address and number
    tied to the sender is suppressed and any phone's consents revoked; out-of-office → a reschedule task a week out;
    interested → a book-a-call task; anything else → human review. The text is never stored, only its SHA-256.
14. **Price books.** Every line of both books (`i11_pricing.CATALOG`) exists with no price. Andre approves version
    n+1 of a line with a price (service lines) or a markup percentage (media-buy lines); the record binds line id,
    version and price. A withdrawn price makes the line unquotable.
15. **Proposals.** Money is Decimal, two places, canonical strings only (finance-py's `money.py`; floats refused on
    the wire; no float anywhere in src, test G1). Auto-approved only when total ≤ `SALES_AUTO_APPROVE_MAX` (default
    and maximum 10000.00), no media-buy line, no discount, no custom term; otherwise `pending_andre` until Andre
    approves that content hash. Payment methods follow Finance: any media buy → ACH only; card only for Revenue
    Recovery at most $5,000; otherwise ACH. A proposal is valid 30 days; a line price changed after the proposal was
    built blocks approval and sending (`PRICE_CHANGED`). Before `sent`, Legal (37) must confirm a client_msa or order
    form in force (stand-in: 503 `LEGAL_UNAVAILABLE`).
16. **Audit.** Every send (`outreach_send`), consent change, suppression, template approval, price approval or
    withdrawal, proposal approval and proposal send has a typed ledger event recorded BEFORE its log line, and the
    log line is anchored before it takes effect; ledger down = nothing happens (503). `GET /sales/v1/audit/export`
    shows emails and phones as keyed hashes and names and notes as SHA-256.
17. **Ports** with stand-ins: email, SMS and voice senders (`not_wired`: the message stays queued, visibly), the two
    lead sources, the Onboarding and Finance hand-offs (`unavailable`: `pending_delivery`, retried by
    `handoff-retry`; a real adapter must be idempotent on `handoff_id`), Legal contracts.
18. **Pipeline:** accounts, contacts, leads, opportunities (stages new, qualified, meeting, proposal, negotiation,
    closed_won, closed_lost), owners, activities and tasks. `closed_won` comes only from a won proposal.
19. **Jobs** (`scheduler`): `send-queue`, `warmup-reset`, `handoff-retry`, `stale-leads`
    (`SALES_STALE_LEAD_DAYS`, default 30), `integrity`.
20. Idempotency is by (actor, operation, target, request id) with the body hash (409 `REQUEST_ID_REUSED` on another
    body). Error bodies carry a code from the closed catalogue (`reasons.py`) and never echo input. Every response is
    `Cache-Control: no-store`; /docs and /openapi.json are off; the shared request limits, hardened launcher
    (`serve.py`, `SALES_` prefix) and graceful close apply.

## Known limits (accepted for v1)

- A message whose outcome could not be recorded after the provider was called stays `sending` and is never resent
  on its own (a duplicate cold email is worse than a missing one); an operator resolves it.
- Quiet hours are the federal 8 am – 9 pm; some states are stricter (for example Florida and Oklahoma end at 8 pm and
  limit calls per day). A per-state rule table is an unlock item before SMS or voice is wired.
- A contact's details are not updated by a later duplicate inquiry (the evidence is added to the lead; the time zone
  has its own route).
- The local log holds contact details (it is the system of record); only the export is minimised. Erasure requests
  (CCPA) need a redaction design that keeps the hash chain and the suppression hash: unlock list.
- ledger-rust has no filtered read, so every integrity check reads the whole ledger (as ADR 0012).

## Not built (unlock list)

1. Email, SMS and voice provider adapters (after Andre picks them), including the provider webhooks that feed
   `/sales/v1/events/email` and `/sales/v1/replies`, and DNS (SPF, DKIM, DMARC) for the outreach domain.
2. The hub page at `https://<outreach domain>/u/<token>` that serves the one-click unsubscribe and relays it to
   `/sales/v1/unsubscribe` (the link in every email depends on it).
3. Public-data and paid-provider lead source adapters.
4. The Onboarding (create client), Finance (31) (invoice draft) and Legal (37) (contract in force) clients.
5. Moving Andre's approvals from the shared approval token to Cybersecurity (22) passkeys (ADR 0012), as the other
   departments move.
6. Rendering and delivering the proposal document itself (the service records the approved content and its release).
7. A per-state quiet-hours and call-frequency table; a CCPA erasure design for the local log.
8. Wiring into CI, the hygiene checker's service list and docs/test-counts.md (left to the integration lead).

## Settings

All settings are in `services/sales-py/README.md`.

## Amendment — AEGIS round 1 (Oct 5 2026): BLOCKING, every finding fixed

Regressions: `services/sales-py/tests/test_aegis_r1.py` (each the reviewer's scenario). Each new guard was
mutation-checked on a copy: removing it fails at least one test.

| Id | Finding | Fix |
|---|---|---|
| S1-H1 (High) | Merge fields (a company name typed into a public form) injected unapproved or deceptive copy and links into Andre-approved emails | A merge value the template uses must be ASCII letters, digits, space and `.'&-`, at most 40 characters, with no `://`, `www.` or `@` (else `MERGE_FIELD_REFUSED`); the RENDERED subject is checked for deception (`SUBJECT_DECEPTIVE`) and the rendered text may carry no URL or domain the approved template does not (`URL_NOT_APPROVED`) — at queue time and again at send time |
| S1-H2 (High) | A deal split into proposals of $10,000 or less was auto-approved and sent piece by piece | A proposal is auto-approved only if it AND the opportunity's approved, sent and won proposals together stay within `SALES_AUTO_APPROVE_MAX`; otherwise `pending_andre` with `OPPORTUNITY_TOTAL_OVER_MAX` (lost proposals do not count) |
| S1-H3 (High) | Plain-language SMS/voice revocations ("please cancel", "wrong number", fullwidth or dotted STOP, Spanish) went to review and texts continued | The classifier normalises (NFKC, accents removed, punctuation between letters removed); on SMS and voice any cancel / end / quit / stop / unsubscribe / opt out / revoke anywhere, "wrong number", "lose my number", "leave me alone", "remove", "no more texts/messages/calls", and alto / parar / cancelar / baja / "no más mensajes" are opt-outs; when in doubt, suppress; an opt-out suppresses every channel and both brands |
| S1-M1 (Medium) | The agent could move a contact's time zone to a daytime zone and text at night | Only hub, onboarding and dashboard set a time zone, each change is a typed ledger event (`contact_time_zone_set`) before it applies, and a +1 (NANP) number must have an `America/*` or US Pacific zone (`TIME_ZONE_PHONE_MISMATCH`, at intake too) |
| S1-M2 (Medium) | Warm-up was global: a brand-new outreach domain inherited the old domain's full pace | Warm-up step and daily counts are kept per outreach domain; a new domain starts at day 1 |
| S1-M3 (Medium) | The outreach-domain check was a suffix test (`go.zbestmedia.com` beside `www.zbestmedia.com` passed; one primary listed let the sister brand's domain be the outreach domain) | Registrable domains are compared (last two labels, three under a listed two-level suffix such as `co.uk`, `com.au`); `SALES_PRIMARY_DOMAINS` must name at least two different registrable domains (both brands), and none may share one with the outreach domain |
| S1-M4 (Medium) | The one-click unsubscribe suppressed the email only; texts continued under the old consent | It suppresses every hash of the contact and revokes all consents, as an opt-out reply does; a spam complaint does the same |
| S1-M5 (Medium) | The key rule (16 different characters) refused about a third of `openssl rand -hex 32` keys, and the committed live run used a 24-byte hex key and failed intermittently | The key file holds hex or base64 of at least 32 decoded bytes; the decoded bytes are the key (at least 8 different values); the live run uses a 32-byte key and passes as committed |
| S1-L1 (Low) | Any worker could mark a proposal won, creating a client and an invoice draft | Won needs Andre (dashboard + his token) or the client's acceptance of THIS proposal (Legal acceptance or e-sign envelope) confirmed through the Legal port (stand-in: `LEGAL_UNAVAILABLE`); `proposal_won` is a typed ledger event |
| S1-L2 (Low) | A STOP or reply refused with 503 (ledger down) is lost unless the relay retries | Stated adapter requirement (below) |
| S1-L3 (Low) | `SALES_NON_PRODUCTION=1` with a data directory wrote a durable log under the fixed test key | A real key is required whenever `SALES_DATA_DIR` is set |
| S1-L4 (Low) | Accepted as a residual by the reviewer | Recorded below |

**Adapter requirement (S1-L2).** The provider-events relay (bounces, complaints, replies) and the hub (unsubscribe,
consent revocation) MUST retry a 503 with the same `request_id` until it is answered 2xx or 4xx, with backoff and an
alert after 15 minutes. A STOP must never be dropped because the ledger was briefly down; the service refuses rather
than suppress unrecorded, so the retry is what makes the opt-out land.

**Residuals (accepted).** S1-L4 was accepted by the round-1 review as a residual; its text did not reach this fix
pass, and the integration lead records its description here. Splitting a deal across several opportunities of one
account (each needs its own qualified lead) is not summed (S1-H2 is per opportunity, as the review asked). The
merge-value rule is ASCII-only: a contact whose first name or company carries other letters (José, Zoë) gets no
template that uses that field (refused, never altered).
