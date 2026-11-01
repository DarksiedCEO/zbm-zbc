# ADR 0028 — The client acceptance page (2026-10-31)

Status: **proposed, not merged** (local branch `feat/client-acceptance-2026-10-31`, built on the local
`feat/compliance-gate-deploy` at 98aaf6f6 with PR #7 `feat/legal-founder-contract-path` 959a687 merged in). AEGIS
audited it at 55023ba3: CONDITIONAL, no Critical or High; its five Medium and seven Low findings are addressed in
"AEGIS r1 fixes" below, except the items that need Andre's decision ("For Andre's sign-off"). Amends ADR 0010 (Legal: who records a client's acceptance, a new caller, two new resources) and
ADR 0022 (decision 6, "who may record an acceptance", for client documents).

## Context

The production audit of Oct 30 2026 found that a client could not accept a contract (red team OPS-C1, Critical,
blocks the first paid client): no acceptance page and no e-signature provider existed, and the only way to record an
acceptance was Andre posting `clickwrap_unticked_box` with his onboarding token, for a page that never existed. The
money red team's RTM-13 and AEGIS r10/r11 (R10-L3 and r11's residual) found the same root from the other side: whoever
held the onboarding token, with any open invitation, could record a client's acceptance with an arbitrary signer;
there was no signer binding, no IP or user-agent evidence, and invitation ids were not secret.

The founder-approved clickwrap text v1 describes the page: a unique emailed link (Part A), the full text of each
document with its version and SHA-256 fingerprint and a download (Part B; Part C for a new SOW), one unticked box per
document, typed full name and title, the authority and electronic-records statements, **Accept and sign**, a
confirmation email with a copy of each document (Part D), and what must be recorded (Part E). Under ADR 0022 Andre
approves the client contracts himself and his CQ-19 determination makes such a clickwrap evidence-sufficient.

## Decision

1. **A separate, minimal public service, `client-portal`** (`services/client-portal-py`), the only new public surface.
   Stateless (no volume, no ledger), one replica, Railway domain only. It holds three secrets: Legal's service token
   (a Railway reference), its own `client_portal` caller token at Legal, and its evidence key. It reaches Legal only,
   over Railway's private network. Alternatives considered:
   - *A route on the orchestrator or the Stripe gateway* (both public already). Rejected: either would gain HTML,
     form handling and a Legal credential; a defect in the page would sit inside the money path (the gateway) or the
     dashboard's API (the orchestrator), and vice versa. A separate service keeps each blast radius to its purpose.
   - *A route on the Vercel sites.* Rejected: Vercel cannot reach Railway's private network, so Legal would need a
     public route; the token check would live in another repository and runtime.
   - *Legal itself public.* Rejected: Legal is the system of record for every contract and acceptance.
   The page is plain ASGI on uvicorn (no web framework), server-rendered HTML, no script, no cookie, no third-party
   resource; its only dependencies are uvicorn and httpx, both already in the shared hash lock.
2. **The link is Legal's.** `POST /legal/v1/client-acceptance-invitations` (Andre only) binds ONE client
   (`client:*`) to exact document versions by hash: the MSA, one SOW, or both. Every document must be one this client
   can validly accept now (approved and in force, this hash, bound to this client or the current standard form, its
   template in force, a CQ-19 evidence basis in force); otherwise no link is issued. The token is 32 random bytes
   (base64url, 43 characters), in that answer only; Legal stores its SHA-256 (a hash of a 256-bit random value needs
   no salt or key), never the token — not in the record, the ledger, the audit export or the idempotency record (a
   replay of the request answers `token: null`). 1-14 days, default 14. Andre revokes it before use
   (`POST .../{id}/revoke`), reads its status (`GET .../{id}`) and, once used, its evidence (`GET .../{id}/evidence`).
3. **The page's two Legal routes, and nothing else, for the `client_portal` caller.** Legal narrows that caller to
   the routes that name it: the generic reader routes every other caller may use refuse it. A stolen portal token
   reaches only:
   - `POST /legal/v1/portal/invitations/resolve` `{"token"}`: the documents' exact registered text, version and hash,
     the display block (client legal name, notice email, SOW number and title; a blob whose hash is in the record) and
     the client's latest counted MSA acceptance. **Every unusable token is the same `404 {"detail": "not found"}`**:
     unknown, malformed, expired, revoked, used, a document no longer valid to accept. Legal compares the token's
     hash with every stored hash in constant time with no early exit.
   - `POST /legal/v1/portal/acceptances`: the token, each document with `presented_sha256` and its own
     `affirmative_act`, `presentation: inline`, the page text as shown and its SHA-256, the typed signatory (full
     name, title, company), and HMAC-SHA256 values of the client's IP address and user agent with the key's id. One
     operation under the service lock records one acceptance per document (`entry_by: client_portal`,
     `invitation_id`, `portal_evidence`) and marks the link used: exactly once; a retry of the same submission is
     answered from the idempotency record; a refused submission (a tampered hash, a missing tick, another document
     set, a page hash that does not match its text) records nothing and does not use the link up. The signatory and
     the page text are blobs; records, ledger events and the export carry only their hashes.
4. **No other way to record a client's acceptance.** `POST /legal/v1/acceptances` refuses every `client:*` party for
   every caller (`ACCEPTANCE_NOT_INVITED`), the onboarding token included; this closes RTM-13's and r11's residual.
   And an acceptance of a `client:*` party not recorded by the page is never evidence-sufficient (an older record
   stays in the log and never counts). The RTM-13 single-document invitations remain for non-client parties only.
   Because Legal issues no link while evidence would be insufficient, an insufficient client acceptance can no longer
   be produced at all.
5. **Finance is unchanged.** FIN-29 reads Legal's acceptance view, which for a page acceptance says
   `evidence_sufficient: true`, `basis_in_force: true` and `via: client_portal`. The end-to-end contract
   (`services/client-portal-py/tests/contract_e2e_real.py`) runs the real legal-py, the page and the real finance-py:
   invite -> accept -> billing readiness allowed -> the invoice is ISSUED; and the used link, another client's
   invoice, a tampered hash, a link naming another client's fill, a template retired after the invitation and an
   expired link are all refused.
6. **ZBM's entity facts come from configuration.** A template's `{{zbm_*}}` variables are filled from Legal's
   `LEGAL_ZBM_FACTS` at fill time, never from a request (422) and never hard-coded; a missing fact refuses the fill
   (`ZBM_FACT_MISSING`). The MSA therefore is a template too and is filled per client; each fill is approved by Andre
   on its own hash, so the text a client accepts is exactly the text he approved, and the page shows exactly that
   text (`presented_sha256 == doc_sha256`, clickwrap Part E).
7. **The page's defenses.**
   - The token's format is checked first (a malformed token never reaches Legal); the same 404 page for every
     unusable link; pages the service does not serve (an MSA-only link; a SOW-only link for a client with no counted
     MSA acceptance) are that same 404.
   - Rate limits (AEGIS M-3): per client address (30/min) before anything; the token's format; a global ceiling on
     every call to Legal (300/min), charged before the call; per link (40/h) only for links Legal confirmed (a junk
     token never enters that table); a global ceiling on not-found answers (120/min). Keys are HMACs, never an
     address or a token. In memory, one replica: a restart resets them.
   - Request limits: target 2 KiB, head 8 KiB, form body 8 KiB (`application/x-www-form-urlencoded` only), 10 s body
     deadline; uvicorn: 64 connections, 5 s keep-alive, 16 KiB head, no access log, no `Server` header.
   - CSRF: the form carries an HMAC over the token's hash, the page text's hash and the issue time (at most 4 hours
     old, not from the future); the POST's `Origin` must equal `PORTAL_PUBLIC_ORIGIN`; a `Sec-Fetch-Site` other than
     `same-origin` is refused. No cookies, no session.
   - Headers on every answer: CSP `default-src 'none'` with the one stylesheet by hash, `form-action 'self'`,
     `frame-ancestors 'none'`, `base-uri 'none'`; `no-store`; `Referrer-Policy: same-origin` (the token in the path
     never leaves in a Referer; with `no-referrer` browsers would send `Origin: null` on the POST); nosniff; HSTS;
     COOP/CORP; noindex.
   - The page text is re-rendered from Legal at submission; a different hash than the signed one (a document changed
     between opening and submitting) records nothing and asks the client to reload.
   - The logs carry the route name, the status and the time taken; never a token, a name, an email, an address or a
     user agent.
   - Accessibility: labels on every field and box, an alert error summary linking to each field, `aria-invalid` and
     `aria-describedby` per field, keyboard only, visible focus, light and dark schemes at WCAG AA contrast.
8. **The words.** The page uses the clickwrap text v1 Parts B and C character for character with the values filled
   (tests pin every sentence). Three changes of behaviour, none of wording, all to avoid any script:
   - each document's full text is shown inline (not in a scroll panel), directly above its own box, so the client
     passes the whole text before reaching the box; the record says `presentation: inline`;
   - the server, not a disabled button, refuses a submission with a box unticked or a field empty, and each box comes
     back unticked;
   - "Not now" opens a page that says nothing was recorded (a page cannot close itself without script).
   One addition the build brief asked for: a third field, "Company you are signing for", which must match the
   client's legal name on the documents (case, spaces and punctuation ignored).
9. **What is recorded** (clickwrap Part E): the client (party ref; legal name in the display blob), the link's id
   (not the token), each document's id, version and hash, the text shown (its hash equals the document's), the
   signatory exactly as typed, each document's own tick, the presentation, the server time, the page text as shown
   with every value filled and its SHA-256, and the IP address and browser **as HMACs** keyed by the page's evidence
   key (`hmac_key_id` names the key). With the key, anyone holding a claimed IP address or user agent can check it
   against the record (runbook step 8). Andre should know that the page's text says "We will record ... your IP
   address and browser": they are recorded in this keyed form, not in clear.
10. **Mail.** No mail provider is wired to the page. `ConfirmationMailer` is the port (Part D builder included); the
    stand-in sends nothing, the confirmation page then says Z Best Media will email the copy, and Andre sends Part D by
    hand (runbook step 6). Finance's client-mail adapter (ADR 0026, another branch) is the candidate; a real adapter
    uses the invitation id as the provider's idempotency key.

## Consequences and residual risk

- **Identity.** The signer is whoever holds the emailed link and types a name; the page does not verify identity
  beyond possession of the link sent to the client's recorded contact (the clickwrap text's own model). A forwarded
  link can be used by the person it was forwarded to; Part A asks the recipient not to forward it.
- **UNVERIFIED: Railway's `X-Forwarded-For`.** With `PORTAL_TRUSTED_PROXY_HOPS=1` the client address is the last
  entry. If Railway's edge sets none, every request keys on the proxy (one shared per-address limit; the recorded IP
  HMAC is the proxy's). Runbook step 8 checks it once.
- **UNVERIFIED on Railway:** the public domain's TLS and HSTS behaviour, and any edge protection against volumetric
  attacks; the page's own limits protect Legal, not the edge. Nothing here was deployed or run against Railway.
- **Idempotency window.** Legal answers a retry from its idempotency record for 15 minutes; a later identical retry
  of a used link gets the 404 page (the acceptance stands; nothing is recorded twice).
- **Part C** names the client's latest counted MSA acceptance; it does not read which MSA version the SOW's text
  references (`{{msa_version}}`). Andre fills `msa_version` with the version the client accepted.
- **One replica, in-memory limits;** a redeploy resets them.
- **Mutation evidence.** The page's tests were written with the code; 13 targeted mutants of its defenses (CSRF,
  form signature, uniform 404, rate limit, escaping, IP HMAC, token format, Referrer-Policy, page-hash recheck,
  company match, forwarded address, text-hash check, logging) were all killed. Legal's tests were written first.

## AEGIS r1 fixes (2026-10-31, report `aegis-client-acceptance.md` at 55023ba3)

- **M-1, the first deploy.** Legal skips an empty or unresolved `client_portal` entry in its caller map
  (`OPTIONAL_CALLERS` in legal-py `config.py`), logs a warning and keeps running; the portal routes answer 503 until
  the token is set and Legal is redeployed. Every other caller entry, and a set but malformed portal token, still
  refuses the start. `devtools/test_deploy_config.py` fails if Legal's map references a service this change adds
  without the caller being optional, or if the runbook asks for an impossible order. The runbook order is: apply,
  set the portal's secrets and domain, redeploy Legal, then the portal.
- **M-2, a retry after success.** `POST /legal/v1/portal/receipts` answers the receipt (documents, display,
  acceptances) only to the token AND the request id of the submission that used the link (the request id is an HMAC
  of the token, the signed page hash and the typed values, so only the same form reproduces it). On a POST whose link
  is used, or whose accept Legal answers 404 (a double submit racing the first), the page asks for the receipt and
  shows the confirmation; anything else is the 404. It works beyond Legal's 15-minute idempotency window, for as long
  as the form's signature is valid (4 hours).
- **M-3, Legal's load and the client address.** The global ceiling on Legal calls is charged before each call; the
  per-link table holds only confirmed links. The client address is `X-Real-IP` (Railway's edge sets it: a moderator
  states it is overwritten at the edge, which is not a documented contract), else the right-most `X-Forwarded-For`
  entry, never the left-most. **Attacker cost:** the tokens are 256-bit, so guessing is pointless; the attack left is
  availability. If a client cannot choose its address, one address gets 30 requests a minute and a single attacker
  needs many real addresses to matter. If it can (the edge passing client values through, which runbook step 8
  checks), the per-address limit costs the attacker nothing; then Legal still sees at most 300 calls a minute
  (5 a second: each a hash comparison over the open links under Legal's lock, well inside Legal's capacity), and an
  attacker who keeps the global ceilings saturated makes the page answer 429 for everyone for as long as it keeps
  sending: a denial of the page, not of Legal, Finance or Compliance, and no acceptance can be forged. Edge
  protection against floods is Railway's (UNVERIFIED).
- **M-4.** Runbook step 8 sends spoofed `X-Real-IP` and `X-Forwarded-For` values, reads append or replace versus pass-
  through from the 429s, and says what to set if they pass through.
- **M-5, screen evidence Legal checks itself.** Legal renders the page (`legal-py/src/clickwrap_page.py`: the
  clickwrap text v1 Parts B and C as blocks with every value filled and each document's exact text). The portal lays
  the blocks out and adds no words of its own to the page text. At submission Legal re-renders the page for the
  link's current state and refuses any other page text (`PRESENTED_TEXT_MISMATCH`), and refuses a typed company that
  is not the client's legal name on the link after NFKC, case, spaces and punctuation are ignored
  (`COMPANY_MISMATCH`). A stolen portal token plus an intercepted link can no longer record fabricated screen
  evidence or another company. Consequence: a link Legal renders no page for (an MSA alone; a SOW alone for a client
  with no counted MSA acceptance) cannot be accepted at all.
- **Lows.** L-1: the typed fields refuse Unicode Cc/Cf/Cs/Zl/Zp/Co/Cn characters after NFKC (bidi controls, zero-width
  characters, soft hyphens), at the page and at Legal. L-2(e): a SOW's `msa_version` must be the MSA of the same link
  or, for a SOW alone, the MSA version the client accepted (`MSA_VERSION_MISMATCH`, checked at link creation and
  whenever the link is used). L-3: approving a newer fill of a client document revokes that client's open links to
  other versions of it. L-4: no RTM-13 invitation is issued for a client. L-5: the legal test helper no longer
  reroutes a client acceptance silently; tests call `client_page_accept` explicitly (it pairs the document under
  test with a standard companion so the link is a page Legal renders). L-7: an explicit `:443` in
  `PORTAL_PUBLIC_ORIGIN` is normalized; the test client in runbook step 8 is named `client:zz-test-andre`.

## AEGIS r2 fixes (2026-10-31, report `aegis-client-acceptance-r2.md` at 591161b0)

- **N-M1, junk refused without Legal.** A link token is self-authenticating: 32 random bytes followed by the first 16
  bytes of HMAC-SHA256(key, `zbm-link-v1` 0x1F random), base64url (64 characters). Legal mints it with
  `LEGAL_LINK_TAG_KEY`; the portal holds the same value (`PORTAL_LINK_TAG_KEY`, a Railway reference to Legal's) and
  the same 30-line function (`src/link_tag.py`, byte-identical in both services, a test checks it). The portal
  checks the tag first, in constant time: a token without a valid tag gets the same 404 page and spends **nothing**:
  no per-address, Legal-call or not-found budget, no Legal call. Only tag-valid tokens are counted toward the
  Legal-call ceiling. Legal also refuses a bad tag before reading any record, and issues no link while its key is
  unset. IPv6 clients are counted per /64; IPv4-mapped addresses as IPv4.
  **Attacker cost after the change.** Without the key, no request the attacker can make costs the page anything but
  one HMAC; the per-address bucket of a client (even one shared edge address) cannot be filled with junk. Forging a
  tag is a 128-bit search; the key itself is held only by Legal and the portal. The attacks left need a real link (a
  client's, or a leaked one): its holder can use that link's own 40-an-hour allowance and the address limit, and by
  sending a used or expired link (still tag-valid) it can spend the not-found ceiling (120 a minute, then 429 on
  not-found answers only; a working link is unaffected) and up to the Legal-call ceiling (300 a minute, then 429 for
  every client until the minute ends). That last case needs a real link and is visible to Andre (revoke it). Edge
  flood protection remains Railway's (UNVERIFIED).
- **N-M2.** Runbook step 8 is mandatory before the first real link. Check B (the recorded HMAC against the phone's
  real address) decides the setting, with a table: the defaults; else the forwarded list at 1, 2 or 3 hops from the
  end; else, honestly, the page records the edge's address, which proves nothing about who submitted and does not meet
  the electronic-records statement, and Andre is told so. `hops=0` is never recommended. Checks A (spoofed headers)
  and A2 (two networks, independent buckets) back it up.
- **N-L1.** The client's legal name must be its own NFKC form (Legal refuses the link otherwise: ™, full-width,
  superscripts, NBSP), and the company match is one function on both sides (NFKC, case, spaces, punctuation).
- **N-L2.** A link names only the client's latest approved fill of each document; an older fill is refused at
  creation and at every use (`VERSION_SUPERSEDED`).
- **N-L3.** Unicode Default_Ignorable_Code_Point characters (U+3164, U+115F, U+1160, U+FFA0 among them) and U+2800 are
  refused in the typed fields, by the same table in both services (a test compares them over U+0000-U+2FFF and the
  supplementary cases).
- **N-L4.** Legal records the hash of the form signature the accepted submission carried; the receipt needs it with
  the request id. After a link is used no new form can be issued for it, so guessed names, titles and companies
  cannot confirm anything.
- **N-L5.** Legal's views carry `time_zone` and `time_zone_label`; the confirmation uses them ("Eastern Time" for
  `America/New_York`), and the zone's own name otherwise.
- **N-L6.** Tests now cover the portal refusing a laid-out page that differs from Legal's (503) and a forged
  document block whose page hash was recomputed.
- **N-L7.** Finance refuses a billing profile, an invoice draft and an invoice approval for any `zz-test-` client.
- **N-L8.** A SOW whose stored fill values are missing or unreadable fails the MSA-version rule closed.

## AEGIS r3 fixes (2026-10-31, report at 5f34539c)

- **N3-M1, a replayed dead link no longer spends the shared Legal ceiling.** The r2 text above overstated the remedy:
  revoking a used link changes nothing (it is already dead at Legal), and its holder could still spend the global
  Legal-call ceiling (300 a minute) and lock every client out. Now, in the portal, in this order: the tag check; the
  per-address limit; the **per-link limit (40 an hour), charged before any Legal call** (only tag-valid tokens reach
  it, so the table holds only links Legal minted; bounded at 5,000 entries); a **dead-link memory**: a link Legal
  answered 404 for (used, expired, revoked, documents changed) is not asked about again for 15 minutes (bounded at
  20,000, keyed by an HMAC of the token, cleared by a restart). Then the Legal-call ceiling and the call.
  **Attacker cost after the change.** Holding one real link that is dead, an attacker gets Legal called at most once
  per 15 minutes; holding one that is open, at most 40 times an hour (the link's own limit, which also locks out
  that link's own client until the hour ends: the remedy is to revoke it and send a new link). Spending the global
  ceiling now needs about 8 open links (300 a minute against 40 an hour each) or about 300 used, expired or revoked
  ones (one Legal call each per 15 minutes), all minted by Legal for real clients, for an outage of up to one minute
  each time (corrected in r4: before N4-L1 each token had 16 spellings, so 6 open or about 19 dead links sufficed). AEGIS's probe4 flood (one used link from 10 addresses, 300 requests): Legal
  saw 1 call (was one per request), a real client's link answered 200. The remaining remedies are: revoke the link
  the log names, or, if many links leaked, change the key and resend (runbook step 9).
  **Which link.** A per-link 429 and a dead link log the link's log fingerprint (8 hex characters of the token's
  SHA-256: not reversible, never the token), once a minute per link. Andre finds it with
  `GET /legal/v1/client-acceptance-invitations/by-fingerprint/{fp}` (Andre's token only); every link view carries
  `log_fingerprint`.
  **Residual (closed in r4, N4-L2).** A transient 404 at Legal (a document blob briefly unreadable) kept a live link
  refused for up to 15 minutes. A key mismatch between the two services is not cached (the tag check fails
  first, nothing is stored).
- **N3-L1.** Legal and the portal refuse to start if the link tag key equals any other secret they hold (Legal: its
  service, Andre, ledger, Compliance and caller tokens; the portal: its service and caller tokens and evidence key).
- **N3-L2.** Tests cover the IPv4-mapped IPv6 branch (counted as the IPv4 address) and the portal's distinct-secrets
  check.
- **N3-L3.** Typed names are refused if they hold a character whose Unicode name says it prints as nothing
  (`FILLER`, `BLANK`, `NULL NOTEHEAD`: U+1D159 and the Hangul fillers among them) or the musical-symbol format
  characters U+1D173-U+1D17A (Default_Ignorable), and a name must hold at least one letter or digit (one made only of
  combining marks, format characters or punctuation is refused). Both services apply the same rule; a test compares
  them over U+0000-U+31FF and U+1D100-U+1D1FF.
- **N3-L4.** The token now starts with a format version byte: base64url(version (1) || 32 random bytes || the first 16
  bytes of HMAC-SHA256(key, `zbm-link` 0x1F version 0x1F random)), 66 characters. Both `/health` answers carry
  `link_tags` (`version`, `key_id` = 8 hex characters of the key's SHA-256); the portal's adds
  `foreign_versions_seen` and a reason when tokens of another version arrive (two different builds). A malformed or
  unset `LEGAL_LINK_TAG_KEY` no longer stops Legal: it starts, `/health` says `link_tags.status: "off: ..."`, link
  creation and the portal routes answer 503 with the reason, and the finance gates stay up. Key rotation order:
  runbook step 9.
- **N3-L5.** Runbook check A2 uses two links (one per device) and tells the per-link limit from a shared address by
  the log line; step 8 opens with the rule that no link is sent before check B.
- **N3-L6.** Finance's test-client rule ignores letter case (`ZZ-TEST-andre` is a test client).

## AEGIS r4 fixes (2026-10-31, report at 05832146)

- **N4-L1, one spelling per token.** A 66-character base64url token carries 4 unused bits in its last character, so
  16 strings decoded to the same bytes, and each got its own per-link counter, dead-link entry and log fingerprint
  (one the fingerprint lookup cannot find). Both copies of `link_tag.py` now accept a token only if it re-encodes to
  exactly itself; the other 15 spellings are the plain 404 at the tag check, before anything is counted. The
  attacker-cost figures in the r3 section above are corrected accordingly.
- **N4-L2, only final 404s are remembered.** On the portal's private resolve and accept calls, Legal's 404 carries
  `"final": true` only when the link is unknown (or its tag fails), used, expired or revoked. A link whose documents
  cannot be served now (rules not loaded, a document retired or superseded, a blob missing) is a 404 without it, and
  the portal asks again next time (within the per-link limit). A blob that exists but cannot be read (EIO, EMFILE)
  is now a 503 (`StoreReadError`), never a 404; a missing blob file is still "not there". The client's page is the
  same 404 in every case.
- **N4-L3, nothing about the key on the public /health.** The portal's `/health` is `{"status":"ok"}` again. It logs
  `link tags: format version 1, key id <8 hex>` at startup, for Andre to compare with Legal's private `/health`
  `link_tags.key_id`. A token in another format version is a log line on the portal (anyone can send one) and a
  counter, `link_tags.foreign_versions_seen`, on Legal's private `/health` (counted when such a token reaches Legal).
  Runbook step 9 updated.
- **N4-L5.** A test pins that a Legal 503 (or timeout) is never remembered as dead, on GET and on POST.

## Rebased on ADR 0024 (2026-10-31, feat/compliance-gate-deploy b5730e4a)

- `OPTIONAL_CALLERS` is `("compliance_38", "client_portal")`: both entries are skipped while unset. The compliance
  branch generalised the behaviour: a route only an unset optional caller may call answers 503, and Legal's
  `/health` says `"status": "warning"` with `optional_caller_unset:<name>` for each. The portal's own dependency
  (503 while `client_portal` is unset, whatever token is sent) is kept.
- Consequence, and AEGIS RB-L1: the cron heartbeat (docs/deploy/railway.md step E.2) is split. Its **alive and
  holds** ping (the only push alarm for a payment Compliance holds) treats a Legal `warning` whose only warnings are
  `optional_caller_unset:*` as alive, so a caller token not set yet never silences the hold alarm; its
  **configuration** ping (`HEARTBEAT_CONFIG_URL`) needs Legal's `"status": "ok"` and alerts until both caller tokens
  are set and Legal redeployed. A deploy-config test runs the runbook's command against a fake `curl`.
- RB-L2, RB-L3, RB-I1: tests pin that no non-portal caller records any client's acceptance (a party-bound filled SOW
  included), that a `degraded` Legal `/health` is never softened to `warning`, and that the portal routes answer
  503 to another configured caller's token while `client_portal` is unset.

## For Andre's sign-off (the approved clickwrap text is the contract record; its wording is unchanged)

1. **The "Company you are signing for" field.** Not in clickwrap text v1; the build brief asked for it. It must equal
   the client's legal name on the link.
2. **The full text inline.** Each document is shown in full on the page above its own box, instead of in a scroll
   panel whose box stays disabled until scrolled; Part E's "Presentation" is recorded as `inline`.
3. **The IP address and browser as keyed hashes.** The page says "We will record ... your IP address and browser",
   and Part E calls for them; they are recorded only as HMACs with the page's evidence key. ZBM can confirm a claimed
   address or browser, but cannot produce one, and losing the key loses even that.
4. **The checkbox rule on the server.** The server, not a disabled button, refuses a submission with a box unticked
   or a field empty (no script on the page).
5. "Not now" opens a page saying nothing was recorded, instead of closing the page.

## Open items

- **Finance does not tie an invoice to a SOW acceptance** (AEGIS L-6, pre-existing FIN-29 behaviour): an invoice's
  `legal_ref` may be the MSA's acceptance, so a new-SOW invoice can issue with only the MSA accepted, and Finance
  relies on Legal's `evidence_sufficient` without itself requiring `via: client_portal`. Not changed here (Finance's
  contract check is shared with other invoice kinds); the fix is to require a SOW `legal_ref` for `kind: service` and
  `via: client_portal` for client acceptances.
- **The same first-deploy hazard exists for `compliance_38`** in Legal's caller map (ADR 0024: Compliance is new and its
  `COMPLIANCE_LEGAL_CALLER_TOKEN` is `preserve()`), on the compliance branch: Legal refuses to start while it is
  empty. Its runbook must set that token before the apply, or the entry must become optional like `client_portal`.
- The confirmation page and Part D say "(Pacific Time)" while `PORTAL_DISPLAY_TZ` is configurable (the text's own
  wording; keep the default).
