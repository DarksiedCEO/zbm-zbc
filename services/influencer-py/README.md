# influencer-py — Influencer & Partnership Marketing (11)

One service for both brands — ZBM (Z Best Media) and ZBC (Z Best Clips). It finds creators (their own applications,
profiles a person researched, and later a paid influencer database and public-profile data through ports that are not
built), reaches them inside the law (outreach email from a separate domain under CAN-SPAM; platform DMs only as Andre
approved each one), holds every further outreach when anyone replies, runs campaigns (influencer campaigns and
campaign-level co-marketing with brands and agencies), issues sponsored-content briefs that always carry an FTC
disclosure, makes deals (anything over $5,000 in total goes to Andre), sends contracts through Legal (37), checks
content for the disclosure before Andre approves it by hash, keeps material-connection records, and asks Finance (31)
to pay verified creators. Deterministic single-task components; no model calls; nothing generated is ever sent unless
it is an exact approved template or a DM Andre approved by hash. Referral and alliance partner commissions are
department 12's, not here.

Architecture, founder decisions and the unlock list: `docs/adr/0015-influencer-partnership-department-architecture.md`.

**Status:** built and tested; not in force. No send provider, DM provider, discovery source or department client is
wired: email and approved DMs stay queued, imports answer `SOURCE_NOT_WIRED`, contracts `LEGAL_UNAVAILABLE`, payee
verification `FINANCE_UNAVAILABLE`, and nothing is ever paid. Not wired into CI (`ci.yml`, `PY_SERVICES`) or
`docs/test-counts.md` yet (left to the integration lead).

## Run

```bash
cd services/influencer-py && python3 -m pytest -q -p no:cacheprovider     # no network
export INF_SERVICE_TOKEN=<secret>                           # required: refuses to start without it
export INF_CALLER_TOKENS='{"hub": "<>=32 printable chars>", "dashboard": "...", "influencer_agent": "...", "scheduler": "...", "provider_events": "...", "compliance_38": "..."}'
export INF_DATA_DIR=/var/lib/zbm/influencer                 # required unless INF_NON_PRODUCTION=1; owned by the service user, 0700
export INF_PII_HASH_KEY_FILE=/etc/zbm/influencer/pii.key    # openssl rand -hex 32 > pii.key; 0600; never change it
export INF_ANDRE_APPROVAL_TOKEN=<Andre's token>             # unset = nothing can be approved
export INF_OUTREACH_DOMAIN=zb-creators.example INF_ZBM_DOMAIN=zbestmedia.com INF_ZBC_DOMAIN=zbestclips.com
export INF_POSTAL_ADDRESS="<street address, city, state, zip>"
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret>
cd src && python3 -m api                                    # INF_BIND_ADDR (127.0.0.1), INF_PORT (8480)
```

Live run against the real ledger binary: `LEDGER_BIN=<ledger-rust>/target/release/server python3 devtools/live_run.py`
(no check depends on the hour or the day).

Headers: `Authorization: Bearer <service token>` on every route but `/health`; `X-INF-Caller-Token` everywhere
else; Andre's routes also `X-Andre-Approval-Token` (through the `dashboard` caller only; every refusal is recorded on the
ledger as `founder_approval_refused`).

| Setting | Default | Notes |
|---|---|---|
| `INF_SERVICE_TOKEN` | — | required, 32..512 printable characters |
| `INF_CALLER_TOKENS` | `{}` | JSON `{caller: token}`; callers in `src/config.py` `KNOWN_CALLERS`; all distinct, none equal to the service token |
| `INF_NON_PRODUCTION` | 0 | 1 allows the in-memory store and a fixed test PII key (never with `INF_DATA_DIR`). Never in production |
| `INF_DATA_DIR` | — | required in production; a directory owned by the service user, mode 0700; one process and one service instance per directory |
| `INF_PII_HASH_KEY_FILE` | — | required in production and whenever `INF_DATA_DIR` is set: a generated key of at least 32 bytes as hex or base64, file mode 0600, not a symlink. Its fingerprint is bound in the log; a different key refuses start |
| `INF_ANDRE_APPROVAL_TOKEN` | — | Andre's approvals; equal to the service or a caller token = not configured |
| `INF_OUTREACH_DOMAIN` | — | the only domain outreach email is sent from; unset = no email (`OUTREACH_NOT_CONFIGURED`) |
| `INF_ZBM_DOMAIN`, `INF_ZBC_DOMAIN` | — | the two brands' own domains (different registrable domains); both required with an outreach domain, which may share a registrable domain with neither |
| `INF_PRIMARY_DOMAINS` | — | optional comma list of further brand-owned domains the outreach domain must also be separate from |
| `INF_POSTAL_ADDRESS` | — | required with an outreach domain; printed in every email (CAN-SPAM) |
| `INF_OUTREACH_FROM_LOCAL` | `creators` | the From mailbox (must accept replies; `noreply` refused) |
| `INF_DAILY_SEND_CAP` | 50 | outreach emails per outreach domain per UTC day (1..200); over it they wait |
| `INF_QUEUE_MAX_PER_CALLER` | 500 | queued outreach messages one caller may have waiting (1..5000); past it `429 QUEUE_FULL` |
| `INF_CONFIRMATION_DAILY_CAP` | 200 | confirmation mails per outreach domain per UTC day, apart from outreach (1..1000) |
| `INF_CONFIRMATION_QUEUE_MAX` | 2000 | link mails for NEW addresses waiting at once (1..20000); past it the oldest is evicted (`QUEUE_EVICTED`, on the ledger as `confirmation_mail_evicted`; its link stays valid), never a refusal. Mail for an address we hold a record for has its own share and is never refused |
| `INF_CONFIRMATION_NEW_ADDRESS_PERCENT` | 25 | share of the daily confirmation cap reserved for NEW addresses (0..90); records we hold use the rest |
| `INF_ANDRE_REVIEW_DAILY_CAP` | 20 | new items a day in Andre's review queue (1..1000); past it they wait in his digest (`awaiting_andre_digest`), never dropped |
| `INF_UNRESOLVED_HOLD_DAYS` | 30 | days an unresolved reply hold (one that names nobody) lasts before `hold-expiry` closes it (1..365); one classified `unsubscribe` or `review` goes into Andre's digest at that age instead and is closed 7 days later if he has not decided it |
| `INF_CONFIRMATION_PER_REQUESTER` | 3 | new-address link mails one requester (the hub's `requester_key`) may have queued at once (1..1000); past it that requester's own oldest mail is evicted. A full pool evicts from the requester with the most queued; requests without a key share one bucket that never evicts keyed mail |
| `INF_CREATOR_SESSION_MINUTES` | 60 | how long a creator session opened by the address link lasts (5..1440), on the service clock |
| `INF_AUTO_APPROVE_MAX` | `5000.00` | largest deal total (and influencer and campaign aggregate) approved without Andre; at most 5000.00 |
| `INF_EMAIL_PROVIDER`, `INF_DM_PROVIDER`, `INF_PUBLIC_PROFILE_PROVIDER`, `INF_PAID_DATABASE_PROVIDER`, `INF_FINANCE_URL`, `INF_LEGAL_URL`, `INF_DEAL_AGGREGATE_WINDOW_DAYS` | unset | not built: setting one (other than `none`/`0`) refuses start (the $5,000 per-person total is lifetime) |
| `LEDGER_SERVICE_URL`, `LEDGER_SERVICE_TOKEN` | — | unset = every write refused (fail closed) |
| `INF_BIND_ADDR`, `INF_PORT` | 127.0.0.1, 8480 | |
| `INF_REQUEST_HEAD_TIMEOUT_SECONDS`, `INF_KEEP_ALIVE_TIMEOUT_SECONDS`, `INF_LIMIT_CONCURRENCY`, `INF_SWITCH_INTERVAL_SECONDS`, `INF_DRAINS_MAX` | 10, 5, 128, 0.001, 512 | launcher tuning (`src/serve.py`, shared with the other Python services) |

## Routes

All under `/inf/v1` except `/health`. "worker" = `dashboard` or `influencer_agent`; "Andre" = `dashboard` +
`X-Andre-Approval-Token`; "auditor" = `dashboard` or `compliance_38`.

| Route | Who | Purpose |
|---|---|---|
| `GET /health` | open | `ok`, `degraded` or (503) `closed`, nothing else |
| `GET /status` | dashboard | integrity, ports wired, send pace, queue, holds, pending approvals |
| `GET /intelligences` | worker | the eleven single-task components |
| `POST /applications` | hub | the public apply step: `{request_id, email, brand?, requester_key?}` only (AEGIS R4, R6). Each address has ONE open address link, reused by every repeat request (never refused for being a repeat); mailed at most once a day from send time to the address on the record; it carries no handle, attestation or payload |
| `POST /confirmations` | hub | the link came back (the mailbox is proven): opens a creator session for `INF_CREATOR_SESSION_MINUTES` (default 60), bound to the record; answers `session_token` (256 bits, re-derived, never logged or on the ledger). The link is single use |
| `POST /sessions/application` | hub | inside a session: the application (`adult_18_plus` exactly `true` — false: 422 `MINOR_REFUSED`, nothing kept and a record we hold is frozen for Andre's review; missing: 422 `AGE_ATTESTATION_REQUIRED`), handles and details, applied at once; once per session (409 `SESSION_ACTION_USED`); an expired session 403 `SESSION_EXPIRED`, a wrong token 403 `SESSION_INVALID` |
| `GET /confirmations`; `POST /confirmations/{id}/approve`, `/reject` | dashboard; Andre | by hash: a tax-reference change for an already verified payee, or mailing the address link to a suppressed address |
| `POST /confirmations/bulk-reject` | Andre | rejects every listed item, bound to `ids_sha256` = SHA-256 of `"\n".join(conf_ids)` in his order; all or nothing |
| `POST /influencers` | dashboard | a prospect a person researched (never attested: no deal until the creator applies) |
| `POST /discovery/import` | influencer_agent, scheduler | public-profile / paid-database discovery through the ports (stand-ins: 503 `SOURCE_NOT_WIRED`) |
| `GET /influencers`, `/influencers/{id}` | worker | records, with suppressed / held / payee state |
| `POST /influencers/{id}/first-name` | dashboard | the only value `{{first_name}}` renders (verified by a person) |
| `POST /influencers/{id}/minor-review` | Andre | a record frozen by a declared minor: `confirm_minor` (blocked for good, suppressed) or `not_a_minor` (released without an attestation) |
| `POST /suppressions`; `GET /suppressions` | hub, dashboard, provider_events, influencer_agent; auditor | one list for both brands, append-only, no removal route |
| `POST /unsubscribe` | hub | the one-click link's token: suppresses every address and handle of the influencer |
| `POST /templates`, `/templates/{id}/versions`; `GET /templates`, `/templates/{id}` | worker | email drafts (never edited in place) |
| `POST /templates/{id}/versions/{v}/approve` | Andre | binds the content hash |
| `POST /outreach/email` | influencer_agent | queue (rules checked now and again at send) |
| `GET /outreach/messages`; `POST /outreach/messages/{id}/cancel` | worker | the queue (email and approved DMs) |
| `POST /dm-drafts`; `GET /dm-drafts` | influencer_agent; worker | the agent drafts a platform DM |
| `POST /dm-drafts/{id}/approve`, `/reject` | Andre | approve binds the hash and queues the DM (no DM provider: it stays queued) |
| `POST /events/email`; `POST /replies` | provider_events | bounces and complaints; replies on any channel (any reply holds; never refused for anything a provider sends: lenient fields, text truncated, up to 512 KiB) |
| `GET /holds`; `POST /holds/{id}/decision` | worker; Andre | `continue` lifts the hold; `opt_out` suppresses. A reply with the same target and text as an active hold attaches to it (one decision). `?status=digest`: Andre's digest of unresolved opt-outs and reviews |
| `POST /campaigns`, `/campaigns/{id}/close`; `GET /campaigns`, `/campaigns/{id}` | worker | influencer or `co_marketing` (with its partner); live-content count |
| `POST /briefs`; `GET /briefs`, `/briefs/{id}` | worker (`GET /briefs/{id}` also hub) | a brief with its required disclosure and the fixed FTC section |
| `POST /briefs/{id}/approve` | Andre | binds the hash of the brief as issued |
| `POST /deals`; `GET /deals`, `/deals/{id}`; `POST /deals/{id}/cancel` | worker | approved here at or under the limit in all three aggregates, else `pending_andre` |
| `POST /deals/{id}/approve`, `/reject` | Andre | by the deal's hash; records the material connection |
| `POST /deals/{id}/contract`; `POST /deals/{id}/contract/confirm` | worker; worker, scheduler | through Legal (37) (stand-in: 503 `LEGAL_UNAVAILABLE`) |
| `POST /contents`; `GET /contents`, `/contents/{id}` | influencer_agent, hub; worker | content refused without the brief's disclosure up front |
| `POST /contents/{id}/approve`, `/reject` | Andre | the final content, by hash |
| `POST /contents/{id}/live` | worker | counts live only when approved (hash named again) and contracted |
| `GET /material-connections` | auditor | FTC material-connection records |
| `POST /tax-profiles` | hub | inside a creator session (`session_token`), for the session's own record (403 `SESSION_RECORD_MISMATCH`), once per session: a tax REFERENCE (`stripe:acct_...` or `vault:` + 26 lowercase letters), never a number; applied at once, or for a verified payee approved by Andre by hash. No per-address cap |
| `POST /payees/{influencer_id}/verify` | dashboard, influencer_agent, scheduler | register at Finance and read the verification (stand-in: 503 `FINANCE_UNAVAILABLE`) |
| `POST /payouts`; `GET /payouts`, `/payouts/{id}` | worker | a payout request handed to Finance (31) for a verified payee (`pending_andre` when the person's lifetime deals exceed the limit and Andre did not approve the deal) |
| `POST /payouts/{id}/approve`, `/reject` | Andre | a payout held by the per-person rule, by hash |
| `POST /jobs/{send-queue,payout-retry,hold-expiry,integrity}/run` | scheduler | jobs (one at a time) |
| `GET /audit/integrity`, `/audit/export` | auditor | integrity with the ledger's own verdict; export with emails and handles dropped (keyed hashes stay), names and texts as SHA-256 |

Every body is refused 422 before it is parsed when it carries a raw tax id (`TAX_ID_REFUSED` with the `field` that
tripped it, never the value: a key naming one, or a value shaped like one) or a date of birth, age, government id, payment, bank, IP, device or protected-trait key
(`FORBIDDEN_FIELD`). Money is a canonical two-decimal string; a JSON number is refused.

## Contract with the hub (creator portal and the link page)

- **The link page needs a button press.** `/c/<token>` on the outreach domain shows a page with a "Continue" button and
  calls `POST /inf/v1/confirmations` only when the person presses it — never on a GET — so a mail scanner or link
  prefetcher that opens the link cannot use it up (the link is single use; a new mail waits a day from the last send).
- **Repeat clicks are one request.** The hub derives the `request_id` of that call from the token (for example
  `c-` + the first 40 hex of SHA-256 of the token): a double click or a retry after a lost answer gets the SAME session
  back instead of `409 CONFIRMATION_USED`.
- **The session token** stays in the page's memory for the session's forms only (never in a URL, a cookie readable by
  scripts of other origins, a log or analytics) and is sent as `session_token` with the application and tax forms.
- **`requester_key` on every public application** (AEGIS R6-L1): the hub's keyed hash (HMAC-SHA-256 under a key only
  the hub holds) of the requester's IP or portal session, as 64 lowercase hex. It is opaque here: validated, then kept
  only as this service's own keyed hash of it, and used for fairness in the link-mail queue (a requester has at most
  `INF_CONFIRMATION_PER_REQUESTER` mails queued; a full pool evicts from the requester with the most). Requests
  without it share one bucket, which can never push out mail of requesters that sent a key — so the hub should always
  send it.
- **Rate limits and CAPTCHA are the hub's.** Per IP and per session on the public form, the link page and the session
  forms, plus a CAPTCHA (or equivalent) on the public form: this service never sees the caller's IP and, by design,
  never refuses a repeat request for an address (any per-address limit before the click locks the real creator out).

## Contract with the reply relay (`provider_events`)

`POST /inf/v1/replies` never refuses a reply for its CONTENT: any JSON value in any field, unknown fields, a text of
any length up to the body limit (cut to 20,000 characters before it is classified and hashed), a body that is not an
object. The same `request_id` with a different body is a different reply; the same body again (with or without a
`request_id`) is the same reply. What remains is the transport layer, shared by every route, which the relay must
respect — it answers before the service reads the body, and the relay must not drop the reply when it sees one of
these; it fixes the transport and sends again:

| Answer | When | What the relay does |
|---|---|---|
| 413 | the body is over 512 KiB (`Content-Length` or bytes received) | truncate the text and send again |
| 415 | the body is not `application/json` (or `+json`) | send it as JSON |
| 400 | an unparseable `Content-Length`, or JSON the parser cannot read (for example a number of thousands of digits) | send valid JSON |
| 422 | JSON nested deeper than 32 levels or with more than 20,000 members | flatten or drop the extra structure |
| 408 | the body did not arrive within 30 seconds | send again |
| 401 / 403 | a wrong service or caller token | fix the credentials |
| 503 | the ledger or the store could not record it | retry with backoff (sales-py's S1-L2 rule: never drop an opt-out) |
