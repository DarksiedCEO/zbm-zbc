# ADR 0014 — Customer Service (30) + Client Success (29): one desk for ZBM and ZBC

Status: accepted for build, Oct 5 2026 (founder Q&A the same day). Not in force: no sending provider (email, SMS,
chat push, voice) or alert channel is chosen, only the Legal (37) handoff has a client, and no department or site
calls this service yet.

## Founder decisions (Q&A, Oct 5 2026)

| Question | Answer |
|---|---|
| One service or two | **One service** for Customer Service (30) and Client Success (29), for **both brands**: ZBM (full-service ad agency) and ZBC (Z Best Clips). |
| Channels | **All four.** Email (a separate support identity per brand); chat (sites and client portal; the widget is the frontend's job, this service is its backend, caller `hub`); SMS **outbound only to clients with recorded express SMS consent**, STOP revokes at once, quiet hours 8am-9pm recipient-local, unknown time zone refuses; phone: build the plumbing now, the voice provider is a port that is not wired. |
| Who answers | **AI answers routine questions right away; money, contracts and complaints go to Andre.** No LLM in v1: deterministic, single-task intelligences. |
| Where answers come from | Only a knowledge base of answers **Andre approved**; nothing generated. |
| Client Success | Health score per account from results trend, payment status, portal logins, tickets and complaints, NPS; at risk -> alert Andre and start a save plan; offers only from a catalogue Andre approved; renewal tracker; NPS surveys. |

## Decisions

1. **One service, `services/service-py`** (Python; FastAPI, pydantic strict; the stack of the other departments).
   Ledger department `service`, event ids `sv-<abbr>-<40 hex>`, port 8460, `X-SVC-Caller-Token`.
2. **Callers.** Eleven names (`config.KNOWN_CALLERS`), each with its own token (`SVC_CALLER_TOKENS`). `dashboard` is
   Andre's console backend and is never Andre by itself: Andre's actions also carry `X-Andre-Approval-Token`
   (`SVC_ANDRE_APPROVAL_TOKEN`, legal-py's FounderGate; a token equal to the service or any caller token counts as
   not configured, so every approval is refused). Moving approvals to Cybersecurity (22) passkeys is unlock item 1.
3. **One conversation model.** A contact (per brand) has tickets; a ticket holds messages from every channel. An
   inbound message joins the ticket it names (it must be that contact's, same brand), else the contact's latest
   ticket in that brand that is not closed, else a new one. A contact is found by its ref (chat), email (email) or
   phone (SMS, calls); a changed address no longer finds the old contact.
4. **Phone plumbing.** Call records (refs to the voicemail and transcript, never audio or text), routing rules per
   brand (business hours and days in a time zone; in hours `ring_andre` or `voicemail`, otherwise voicemail; no rules
   = voicemail), handoff to Andre. A call becomes a phone ticket in Andre's queue; a voicemail or missed call alerts
   him. No bot answers a call. `SVC_VOICE_PROVIDER` refuses start: not built.
5. **Consent registry.** Per contact per channel (`sms`, `email`): source, when it was captured, the SHA-256 of the
   exact consent text (the text is stored once, by hash, never on the ledger), revocation (when, and how: Andre,
   the contact's request, or the STOP keyword). Only express consent is recorded (`express: true`); a capture time in
   the future is refused. Every consent change is a typed ledger event before it takes effect.
6. **SMS rules.** Outbound only with an active SMS consent; between 08:00 and 21:00 in the recipient's own time zone
   (a message outside the window waits, it is never sent early); no or an invalid time zone refuses. A whole message
   that is one opt-out keyword (STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT, REVOKE, OPTOUT) revokes SMS consent at
   once and cancels every queued SMS to that contact. START does not restore consent: a new express consent must be
   recorded. Every outbound SMS ends with "Reply STOP to opt out."
7. **Email and chat rules.** A reply on a ticket the contact opened needs no consent record but stops when they
   revoke email; anything proactive (check-in, survey, offer) needs an active email consent. Chat goes into the
   contact's own thread and needs no consent. Phone is never an outbound channel here.
8. **Ticket status machine:** `open -> pending_customer | escalated | resolved`; `pending_customer -> open |
   escalated | resolved`; `escalated -> open | pending_customer | resolved`; `resolved -> open | closed`; `closed` is
   final (a later message opens a new ticket). A message on a resolved ticket reopens it. Queues: `bot` and `andre`.
9. **Priority and SLA.** p1..p4; triage sets security p1, privacy / contract / money / complaint p2, everything else
   p3; Andre may change it. First-response and resolution targets per priority (defaults 1 h / 8 h, 4 h / 24 h,
   8 h / 72 h, 24 h / 7 days), configurable within bounds (first response 5 min .. 48 h, resolution 1 h .. 14 days;
   never looser for a higher priority; first response never above resolution), counted in wall time from the ticket's
   creation. An approved bot answer counts as a first response.
10. **Breach -> Andre.** The `sla-sweep` job records each breach once per ticket and kind, with an alert to Andre.
11. **Alerts to Andre** carry a code and an id only (never a body, an address or caller text). No channel is chosen:
    the alert port is not wired, so each alert is recorded with delivery `not_wired`; when a channel is wired, alerts
    are sent after the request that raised them, outside the lock, and retried by `handoff-retries`.
12. **Triage (intelligence I1, `src/triage.py`).** Every inbound message is classified, in precedence order:
    privacy (data deletion / access / do-not-sell), security (password, hacked, breach, phishing ...), contract/legal
    (contract, cancel, terminate, lawyer, sue, terms, DMCA, copyright ...), money (refund, charge, invoice, dispute,
    chargeback, price ...), complaint (complaint words, profanity including masked and leet forms, at least 70% capitals
    over 12+ letters, `!!!`, a third message in 24 hours, a ticket reopened twice). One keyword is enough (no negation
    handling): **fail toward a human**. Only a message where nothing fired, at most 800 characters and at most two
    question marks, is a routine candidate; everything else is `other` (Andre's queue). Signals are codes, never text.
13. **Routing.** Every category that fired is routed: money -> Andre + Finance (31) port; contract -> Andre + Legal (37)
    matter intake (legal-py `POST /legal/v1/requests`, kind `litigation_threat`, `ip_claim` or `contract_dispute`);
    security -> Andre + Cybersecurity (22) port; complaint -> Andre; privacy -> Compliance (38) + Legal (kind
    `privacy_request`) + Andre, **never answered by the bot**. One alert per category. A handoff carries refs and
    codes only (ticket id, account id, category, kind), never the message.
14. **Approved answers (intelligence I2, `src/kb.py`).** An article has brands, channels, a title, the answer text
    and match rules (`phrases` any one; or `all` every one plus `any` at least `min_any`; `exclude` none). Saving
    always makes a new, unapproved version; Andre approves one version by its content SHA-256 (what he read is what
    is approved). At match time the hash is recomputed: an edited, retired or tampered article is never used. The
    highest score wins; a tie is ambiguous and goes to Andre. The text sent is the article's, byte for byte.
15. **Answering.** A routine candidate with exactly one usable matching article, on a ticket in the `bot` queue, is
    answered at once: chat inline in the response (status `sent`), email and SMS queued for the provider (SMS only
    with consent). A follow-up on a ticket that is with Andre is never answered by the bot, and an escalated ticket
    is never demoted. A queued answer whose article, template or offer changed before it went out is cancelled and
    the ticket goes back to Andre's queue.
16. **Sending.** Email, SMS and chat push are ports; none is wired, so outbound messages stay `queued`, visibly, and
    `outbound-tick` does not touch them. When one is wired: the channel rules and the approvals a message relies on
    are re-checked; `message_sent` is recorded on the ledger FIRST (ledger down = nothing is sent and the tick stops);
    the provider is called outside the lock with the message id as its idempotency key; the result is committed. A
    send whose result could not be committed is never re-sent by this process (it is recorded at the next tick); a
    restart in that window can send it again (at least once; the provider is expected to de-duplicate on the id).
    After five failed attempts a message is cancelled and its ticket goes back to Andre.
17. **Templates and offers** follow the KB rule (versioned, content-hash bound, Andre approves, editing un-approves).
    Templates have a purpose (`check_in`, `nps_survey`, `offer`), a brand, channels and text with placeholders from a
    closed list (`first_name`, `brand_name`, `offer_title`, `offer_terms`, `offer_price`, `survey_id`). Offers have a
    brand, title, terms and a price as a Decimal string (`"1250.00"`, never a float) in USD.
18. **Health score (intelligence I3, `src/health.py`).** Start at 100, integers only, clamp 0..100: results trend down
    -25 / flat -10 / unknown -5; payment failed -30 / late -15 / unknown -5; portal login never or older than 30 days
    -15, 15..30 days -8; complaints in 30 days -10 each (at most -30); open escalations -5 each (at most -15); latest
    NPS 0..6 -20, 7..8 -5. A signal that cannot be read counts as unknown, never as fine. Every score lists each
    contributing signal with its points and source.
19. **At risk and save plans.** `health-recompute` scores every account; a score below `SVC_AT_RISK_THRESHOLD`
    (default 60) with no active plan alerts Andre and starts a plan of three steps: a check-in message from an
    approved `check_in` template (channel: email if email consent, else SMS if SMS consent, else the portal chat), a
    results-review task for Andre, and an offer. The offer step waits for Andre to pick an offer **by id from the
    approved catalogue** (nothing else is accepted: no price, terms or text); `save-plan-tick` sends it with the
    approved terms and price only if the offer is still approved at the same version and content, else the step goes
    back to awaiting selection. Every step change is recorded. Andre closes a plan (saved, lost, cancelled).
20. **Renewals and NPS.** Contract end dates come from Onboarding (posted with the account) or the contract ports
    (Legal, Onboarding; a port answer wins); a date within `SVC_RENEWAL_WINDOW_DAYS` is listed and alerted once per
    date. NPS surveys use an approved `nps_survey` template and the channel rules; a detractor answer alerts Andre.
21. **Minimum data.** A contact holds a ref, email, phone, time zone, display name and account id; no request may
    carry a date of birth, government id, card or bank number, IP or device field anywhere (422, legal-py's list).
    Message bodies, subjects, consent texts and NPS comments are stored once, content-addressed (`bodies/<sha256>`),
    re-hashed on read; they are never in the log line, the ledger, an alert, a handoff or an error.
22. **Record first.** Every state change is one log line holding a list of effects (an inbound message, its ticket,
    triage, answer or escalation and alerts take effect together or not at all). Before the line: each answer,
    escalation, consent change, approval, alert, SLA breach, save plan and offer selection is recorded as its own
    typed ledger event (ids, codes and hashes only); then the line is fsynced aside (`pending.line`), anchored
    (`log_anchor`), appended and applied. A typed event whose line was then refused stays on the ledger as an intent
    that never took effect; the anchors say which lines exist.
23. **The record log and its integrity** are security-py's (ADR 0012 decisions 6-9) **as fixed in its AEGIS rounds
    1-5**: our own pending line kept in memory and rolled forward (identical anchor re-recorded, then appended); a
    pending line found on disk appended only if the ledger already holds its anchor, else set aside
    (`pending.discarded`) and dropped once the log moves past it (a forged line is inert); a lost ledger answer keeps
    the line pending and stops writes until the next check; an empty line refuses start; appends are exact-size with
    adopt / truncate; every local line must have its anchor and the ledger no anchor this log lacks (a truncated,
    rolled back, deleted or replaced log stops every write: 503 `INTEGRITY_UNVERIFIED`).
24. **Data directory.** Required in production (`SVC_DATA_DIR`, owned by the service user, 0700); one process per
    directory (`flock`); bodies written before their line; bodies no line cites removed at start (never while a
    pending line exists).
25. **Idempotency** by (actor, operation, target, request_id) with the SHA-256 of the normalised body: a replay
    returns the stored answer (after a restart too); the same id with a different body is 409 `REQUEST_ID_REUSED`.
26. **Errors** carry a code from the closed catalogue (`src/reasons.py`) and never echo the request. Every response is
    `Cache-Control: no-store`; /docs and /openapi.json are off; `/health` says `ok` or `degraded` only; the shared
    request limits, hardened launcher and graceful close of the other Python services apply.
27. **Jobs** (caller `scheduler`, idempotent per request id, one at a time): `sla-sweep`, `health-recompute`,
    `save-plan-tick`, `outbound-tick`, `handoff-retries`, `integrity` (always reads the ledger, and `GET
    /ledger/verify`).
28. **Audit export** (`/svc/v1/audit/events`): the log with addresses, names, refs, time zones and request keys
    reduced to SHA-256 and stored answers dropped.

## How the other departments use it (not wired yet)

| Department | Here | Needed there |
|---|---|---|
| Sites / client portal (`hub`) | contacts, consent capture, chat, threads, portal logins, NPS answers | the chat widget and consent forms (frontend thread) |
| Onboarding | contacts, accounts, contract end dates | a thin client to this service |
| Legal (37) | handoff client `src/legal_client.py` -> `POST /legal/v1/requests` | a caller for this department in `LEGAL_CALLER_TOKENS` (legal-py `config.py` / README) |
| Finance (31) | payment-status signal and money handoff ports | a read route for payment status per account and an intake route for money escalations |
| Cybersecurity (22) | incident handoff port | `POST /sec/v1/incidents` is dashboard-only today: a service caller route for reported incidents |
| Compliance (38) | privacy-request handoff port; reads consents and the audit export | a privacy-request (DSAR) intake route |
| Revenue Recovery / detection | results-trend port | a per-account results trend read |

## Not built (unlock list)

1. Andre's approvals by passkey through Cybersecurity (22) instead of `X-Andre-Approval-Token`.
2. Email, SMS and chat-push providers (and their inbound webhooks' signature checks in the gateways).
3. The voice provider (call control, recordings, transcripts).
4. An alert channel to Andre (text, email, push).
5. Clients for Finance (31), Cybersecurity (22), Compliance (38), the results trend and contract end dates; each
   needs the route named in the table above. Legal (37) needs this department added as a caller.
6. Erasure: a privacy request is routed, not executed; deleting a contact's bodies and records waits for the
   Compliance / Legal rule on retention.
7. The console pages (queues, approvals, save plans) and the chat widget.
8. Moving bootstrap caller tokens to Cybersecurity (22) minted credentials.

## Known limits (accepted)

- ledger-rust has no filtered read, so every integrity check reads the whole shared ledger (security-py's L7 rate
  limit is kept: a forced check at most every 10 s; the scheduler's job always reads).
- The ledger's `department` field is self-declared by any holder of the ledger token (per-department ledger tokens
  are a ledger-rust change).
- Sending is at least once across a restart (decision 16).
- A process that stops with an anchor in flight AND commits a new line before that anchor lands leaves two anchors
  for one sequence number; the integrity check reports it and writes stop until an operator reconciles
  (security-py's accepted residual, ADR 0012 round 4).

## Settings

All settings and routes are in `services/service-py/README.md`.

## Amendment — AEGIS round 1 (Oct 5 2026): BLOCKING, every finding fixed

Each fix has a regression test in `services/service-py/tests/test_aegis_r1.py` built from the reviewer's scenario,
and each new guard was mutation-checked (the guard removed on a copy: its test fails).

| Id | Finding | Fix |
|---|---|---|
| V1-C1 (Critical) | SMS consent followed the contact, not the number: after the hub changed a phone, texts went to a number nobody consented for | A consent records the address it was given for; `channels.check` requires an active consent whose address is the contact's CURRENT phone (or email); changing a phone or email revokes the old address's consent (`via: address_changed`) in the same line |
| V1-C2 (Critical) | A STOP processed while the outbound tick was sending was ignored: the next queued SMS still went, and a cancelled message could flip to sent | Under the lock, right before each send intent is recorded, the message is re-checked: still queued, channel rules (consent, quiet hours), the article / template / offer still approved as it was, body present, attempts left; otherwise it waits or is cancelled. A cancelled message never becomes `sent` (a late provider answer is kept as `provider_accepted`) |
| V1-H1 (High) | Triage bypassed by zero-width characters, Cyrillic look-alikes, HTML tags and entities, soft hyphens, full-width letters, Spanish, misspellings and synonyms; the routine message carried a second request that was auto-answered | Text is cleaned in this order: format characters (Cf, soft hyphen) out, HTML entities unescaped (repeatedly), tags stripped, NFKC, Cyrillic / Greek confusables mapped to Latin; accents folded; runs of single letters joined ("r e f u n d"). Any non-ASCII letter that survives goes to a human. Spanish lexicon in every category; many English synonyms; a one-edit fuzzy match on 5+ letter tokens against misspelling-prone stems (refund, chargeback, lawyer, password, complaint ...); a currency sign is money. Only a message of at most 25 words, one sentence, at most one question mark, no comma and no clause joiner (and, also, but, y, pero ...) after a leading greeting is a routine candidate. Fail toward a human: a few everyday words now escalate (e.g. "layer" is one edit from "lawyer"), accepted |
| V1-H2 (High) | The email subject was never triaged | Subject and body are scanned by the same rules; an escalation in either escalates; a routine answer needs a clean, single-intent subject too |
| V1-H3 (High) | Only a bare keyword revoked SMS consent ("Please stop", "S T O P", "leave me alone", "ALTO" did not) | Any opt-out word or phrase anywhere, after the same cleaning (stop, unsubscribe, cancel, end, quit, remove me, leave me alone, no more texts, wrong number, do not text, alto, parar, cancelar, baja ...), and short ambiguous messages on their own (no, nope, bye, enough ...) revoke at once. If the contact had consent for that number, the one permitted confirmation is queued through the SMS port (no consent check, no STOP footer, never cancelled by the revocation); a repeat STOP sends none. An opt-out of more than four words still opens a ticket for Andre (never bot-answered by SMS) |
| V1-H4 (High) | A consent captured before a STOP (a hub replay) brought consent back | A consent whose `captured_at` is at or before the last revocation for that contact, channel and address is refused 409 `CONSENT_PREDATES_REVOCATION` |
| V1-M1 (Medium) | The text of a replaced consent (evidence) was deleted at restart as an orphan | The keep-set is every digest cited anywhere in the log, not what memory still points at |
| V1-M2 (Medium) | The audit export reduced phones and emails to unsalted SHA-256 (reversible by enumeration); stored digests were plain SHA-256 | The export replaces personal fields and request keys with HMAC-SHA-256 under a random key made for that export and returned once with it (`hmac_key`; the personal-field digests of two exports differ — but see V2-L3: contact, ticket and message ids are the same in every export, so two exports CAN be joined on them). Stored bodies and consent texts are addressed by HMAC-SHA-256 under `SVC_HMAC_KEY_FILE` (required in production; field names keep `*_sha256`) |
| V1-L1 (Low) | A dashboard revocation was labelled `via: andre` without Andre's token | `andre` only with his verified token (a wrong token is 403); otherwise `dashboard` |
| V1-L2 (Low) | The dashboard alone could resolve or close an escalated money / legal ticket | Resolving or closing a ticket whose categories include money, contract, complaint, security or privacy needs Andre's FounderGate token |
| V1-L3 (Low) | The "never re-send" guard for a send whose result was not recorded lived in memory only | The send intent is committed (`message_sending`, its typed ledger event first) BEFORE the provider is called, with the message id as the provider's idempotency key. A result not recorded is recorded at the next tick by the same process; after a restart a message still `sending` is held (`held` in the tick result) and never re-sent automatically: Andre resolves it (`POST /svc/v1/outbound/{id}/resolve`: `sent`, `requeue` or `cancel`) |

Decision 16 now reads with `message_sending` (recorded first) in place of `message_sent` (recorded on the result).

## Amendment — AEGIS round 2 (Oct 5 2026): BLOCKING, every finding fixed

Round 2 confirmed every round-1 repro closed, then found that a fresh corpus of 89 paraphrased money, legal, complaint
and security messages (other languages, emoji, two-typo misspellings, line separators, glued to a routine question)
was auto-answered 50 times on chat and email and 48 on SMS: a blocklist does not converge. The design now fails
closed. Regressions: `services/service-py/tests/test_aegis_r2.py` (the reviewer's corpus verbatim, on all three
channels); each new guard mutation-checked.

| Id | Finding | Fix |
|---|---|---|
| V2-H1 (High) | The routine gate was a blocklist; paraphrases and other languages passed it | **Allow-list.** The bot answers only when, after cleaning, EVERY word of the message (and of an email subject) is a fixed stop word (no negations), a word of the matched approved article's rules, a word of that article's new `vocabulary` (approved with it, part of its content hash) or a small base vocabulary (days, today, time, question ...); nothing on the deny list (every lexicon word, negations, profanity, misspelling stems) is ever allowed, whatever an article says, and a vocabulary holding one is refused 422 `VOCABULARY_DENIED`. Also refused: any symbol, emoji, digit or currency, any internal line separator (`\r`, `\n`, U+2028, U+2029, U+0085), a run of single letters, fewer than three words. The lexicons remain as labels that route escalations. The reviewer's 89 messages: zero answers on chat, email and SMS; a positive set of routine questions is still answered |
| V2-H2 (High) | A replay of a consent captured for the old number was accepted for the new one | The consent request carries the address (`address`), which must equal the contact's current phone / email (409 `CONSENT_ADDRESS_MISMATCH`); a `captured_at` before the contact's current address was CHANGED to it is refused (409 `CONSENT_PREDATES_ADDRESS`; the first address a contact has sets no lower bound: the capture usually predates the record) |
| V2-H3 (High) | Opt-outs on chat or email, "stopp", "never text me again", "Remove my number" and the stop-sign emoji did not stop texts | The opt-out check runs on every inbound channel (body and subject); a STOP by chat or email revokes SMS consent (`via: stop_by_chat` / `stop_by_email`; no SMS confirmation then). One typo from stop / unsubscribe / stopall, repeated letters collapsed, digits read as letters, a wider phrase list (English, Spanish, French, Portuguese), stop / no-entry / raised-hand emoji. Fail safe: every inbound SMS the bot does not answer pauses PROACTIVE SMS to that number (409 `SMS_PAUSED`) until Andre clears it (`POST /svc/v1/contacts/{id}/sms-pause/clear`); clearing never restores a revoked consent. Over-matching (e.g. "shop" is one typo from "stop") only stops texts |
| V2-M1 (Medium) | Starting with another `SVC_HMAC_KEY_FILE` made every stored body unreadable, silently | The key's fingerprint (HMAC of a fixed label) is written to the log at the first verified start; a different key refuses start |
| V2-L1 (Low) | Weak keys were accepted | All-zero keys and keys with fewer than 16 distinct bytes are refused |
| V2-L2 (Low) | A dashboard revocation with a bad optional Andre token was refused | It revokes and is recorded `via: dashboard` (a revocation must never be blocked) |
| V2-L3 (Low) | The round-1 amendment said two exports "cannot be joined" | Corrected above: personal-field digests differ per export, but contact, ticket and message ids are stable, so exports can be joined on them |
| V2-L4 (Low) | The live run never exercised `outbound-tick` with a wired sender | A non-production-only file sender (`SVC_SMS_PROVIDER=nonprod_file`, `SVC_NONPROD_OUTBOX_FILE`; refused without `SVC_NON_PRODUCTION=1`) lets the live run restart the service, send an SMS through the port and show `message_sending` then `message_sent` on the real ledger |
| Unverified | A display name holding `{offer_terms}`; offers / templates edited or retired after selection | Rendering is single pass (a merge value is never expanded again) and braces are refused in display names; tests show an edited template is cancelled before sending, a retired offer sends the step back to awaiting selection, a retired offer template blocks the step |

## Amendment — AEGIS round 3 (Oct 5 2026): BLOCKING, every finding fixed

Round 3 showed that word-level allow-lists still let meaning through ("how long will you have me", "can my clips be
taken down", "when you open can you let me be"). The design now removes interpretation from answering entirely.
Regressions: `services/service-py/tests/test_aegis_r3.py` (the reviewer's round-3 corpus verbatim on chat, email and
SMS; the round-2 corpus stays in `test_aegis_r2.py`); each new guard mutation-checked.

| Id | Finding | Fix |
|---|---|---|
| V3-H1 (High) | Allow-listed words still combined into money, legal, privacy, complaint and opt-out meanings, which the bot answered | **Exact match only.** Each KB article carries the EXAMPLE QUESTIONS Andre approves with it (part of the content hash; `rules` and `vocabulary` are gone). The bot answers only a message equal to one of them after `kb.exact_form`: lower case, whitespace trimmed and collapsed, trailing `? . !` removed, one leading greeting (hi, hello, hey) and one trailing thanks / thank you / please removed — nothing else. An email subject must be empty, neutral (question, quick question, hello, hi, hey, inquiry, enquiry, help) or the article's title or one of its questions. Every lexicon remains as labels only. The round-2 and round-3 corpora: zero answers except the four exact approved questions used as positive controls |
| V3-C1 (Critical) | An opt-out by email or chat phrased outside the list did not stop texts; only SMS paused | ANY inbound on ANY channel the bot does not answer pauses proactive SMS to every phone of the resolved contact (and of any contact the message names). A negation within four words of text / sms / message / phone / cell / number is labelled `sms:opt_out_suspected` and alerts Andre (`SMS_OPT_OUT_SUSPECTED`) |
| V3-C2 (Critical) | A revocation belonged to the contact: the same number under a new contact got a replayed pre-STOP capture accepted | Revocations are kept by (brand, channel, HMAC of the address) and checked for every contact: at capture (409 `CONSENT_PREDATES_REVOCATION` for a capture at or before the address's last revocation on any contact) and at send time (a consent captured before its address's last revocation is not live). A capture before the contact existed is refused (409 `CONSENT_PREDATES_CONTACT`) |
| V3-C3 (Critical) | The opt-out confirmation went to the contact's NEW number after the hub changed it | The confirmation is bound to the number that sent the STOP (`bound_to`), sent to that number only, and cancelled (`ADDRESS_CHANGED`) if the contact's phone is no longer that number |
| V3-M1 (Medium) | Inflections ("refunding", "deleted", "lawyers", "hacking", "thieves") escaped the lexicons | Category stems match by prefix (refund*, reimburs*, overcharg*, chargeback*, disput*, invoic*, delet*, eras*, wipe*, hack*, breach*, leak*, expos*, passw*, cancel*, lawyer*, attorney*, litigat*, terminat*, scam*, thie*, complain* ...) plus exact sue / sues / sued / suing and phrases such as "taken down". They label and route (deletion -> Compliance + Legal), and an example question that triggers any category, stem, typo or profanity is refused 422 `QUESTION_DENIED` |
| V3-M2 (Medium) | An opt-out from an address that matches no contact (or another contact) changed nothing for the contact meant | It still revokes for the sending address, pauses proactive SMS for every contact the message names by phone number or email address, always opens a ticket for Andre (never the short opt-out-only path) and alerts him (`OPT_OUT_UNKNOWN_SENDER`) |
| V3-L1 (Low) | A supplied key with 16 distinct bytes but no entropy (`0123456789abcdef` twice) was accepted | The service generates its key on the first start (`os.urandom(32)`, `SVC_DATA_DIR/hmac.key`, 0600, created with `O_EXCL`); a supplied key is refused if all zeros, fewer than 16 distinct bytes, any 8-byte block repeated, or only printable characters |
| V3-L2 (Low) | No rotation procedure; a log from before the fingerprint adopted any key | README: key rotation is not supported yet (unlock list). A log without a fingerprint is accepted only if every stored body it cites verifies under the configured key, then the fingerprint is written |
| V3-L3 (Low) | Pausing and clearing SMS were not on the ledger as typed events | `sms_paused` and `sms_pause_cleared` are typed ledger events (ids only, no number) |
| Info | "shop" is one typo from "stop": a chat or email typo revoked consent permanently | Opt-outs have two levels: `exact` (a listed word or phrase, also with repeated letters collapsed or digits read as letters, a stop-sign emoji, a short ambiguous message) revokes on every channel; `suspected` (a one-typo stop / unsubscribe, a negation near a channel word) revokes on SMS but on chat and email only pauses proactive SMS and alerts Andre to confirm |

Unlock list addition: 9. HMAC key rotation (recompute every stored digest and address revocation under a new key).

## Amendment — AEGIS round 4 (Oct 5 2026): NOT BLOCKING, every finding fixed

Regressions: `services/service-py/tests/test_aegis_r4.py` (the reviewer's 38 questions verbatim); each new guard
mutation-checked.

| Id | Finding | Fix |
|---|---|---|
| V4-M1 (Medium) | Andre could approve example questions that carry money, privacy, account-closure, security or opt-out meaning without a lexicon word ("can i get my funds back", "do you sell my info", "how do i shut down my account") | At approval an example question is refused 422 `QUESTION_DENIED` when it has more than 12 words, a second clause (`triage.single_intent`), any opt-out wording (`channels.opt_out_level`), a negation near a channel word, any category, stem or typo, or an approval-only deny term: funds, back, rebate, deposit, waive, renew, credit, card, wire, sell, share, rid, forget, leave, out, off, offline, dump, safe, problem, broken, shut, close, stuff, return, money back, get rid, have on me, shut down, close my account, close out, out of, take off / down, got into, end things, free month, auto renew, my info / data / information / account. These terms refuse questions only; they do not label inbound messages |
| V4-M2 (Medium) | `bound_to` (the STOP number) appeared in the audit export and in the `answer_queued` ledger payload | `bound_to` is a personal key: hashed (per-export HMAC) in the export, left out of every typed-event payload. A regression scans the whole export and every ledger payload for any E.164 number or email address |
| V4-L1 (Low) | A pause was keyed by contact: moving the number to a new contact dropped it | Pauses are keyed by (brand, HMAC of the number), like revocations, and checked for whichever contact holds the number now |
| V4-L2 (Low) | The generated key file was written in place: a crash could leave it empty, and the error did not say what to do | Written to a 0600 temporary file, fsynced, linked into place (`os.link`, which never replaces an existing file) and the directory fsynced; a leftover temporary file is removed. An empty or invalid key file stops the start with the remedy: delete it only if the log is absent or empty, otherwise restore it from backup; a permissions problem is reported as such |
| V4-L3 (Low) | A changed generated key was reported as `SVC_HMAC_KEY_FILE` | The mismatch error names the actual key source: the generated key file's path, or `SVC_HMAC_KEY_FILE` with its path |

## Amendment — AEGIS round 4b (Oct 6 2026): NOT BLOCKING, every finding fixed

Regressions: `services/service-py/tests/test_aegis_r4b.py`; each new guard mutation-checked.

| Id | Finding | Fix |
|---|---|---|
| V4b-I1 | Concurrent first starts on one data directory raced on the key (a fixed temporary name; the flock was taken only after the key, in `api.build`) | `config.load` takes the exclusive flock on `SVC_DATA_DIR` (security-py's DataDirLock, `service.lock`) FIRST, before the key is generated or read and before the log is opened, and holds it for the life of the process; a second process refuses to start with a message naming the data directory and the remedy (stop the other process). The temporary key file is per process (`hmac.key.<pid>.<random>.tmp`). The reviewer's race (300 trials of 6 simultaneous starts): never two keys, no leftovers, the only error the lock message; the regression holds every winner until all have tried and asserts exactly one start per directory |
| V4b-I2 | A stale temporary key file was removed only when `hmac.key` was absent, and the round-4 amendment said more than the code did | Every `hmac.key.*.tmp` is removed at each start (under the lock), whether or not the key exists. Corrected statement of V4-L2: a crash leaves no key file (the next start generates one) or a complete one, and any temporary file is swept at the next start |
| V4b-L1 | The approval-time deny list is a word list and missed nine more phrasings (phone number / email to others, compensated, done with you, deactivate, discontinue, pull my, come down, downgrade) | Those terms are added (phrases, and the stems compensat*, deactivat*, discontinu*, downgrad*). Saving, approving and listing an article now also returns WARNINGS: for each example question, its watch-list words (change, plan, keep, give, pause, policy, account, number, email, password ...) and any word one typo from a deny term, for Andre to read before approving. **The deny list can never be complete: Andre's approval of each example question, read with its warnings, is the main control**; the deny list and the exact-match rule only narrow what a mistaken approval can expose |
| V5-L1, V5-I1 (round 5 check of f61ae83) | Caching the flock in `config.load` let a second service instance in the same process (a second `api.build`) open the same data directory; a FIFO or directory planted at `service.lock` or a temporary key path was accepted or crashed | The process-wide flock is now CLAIMED once per service instance (`DataDirLock.claim`, taken by the service's construction, given back by `close()` and on a failed start): a second instance is refused like a second process; the test harness closes before it restarts. `service.lock` and stale `hmac.key.*.tmp` paths must be regular files (`lstat`, then `O_NOFOLLOW` and `fstat`): anything else refuses with a clear message naming it. Regressions: `tests/test_aegis_r5.py` |
| V5r-L1, V5r-Info (follow-up check of e0a69a9) | A refused second `api.build` still swept the running instance's `bodies/*.tmp` (the body store was built before the claim); a closed instance could still write | `api.build` (and the test harness) claims the data directory straight after `config.load`, BEFORE the log and the body store are built, gives the claim back if construction fails, and the service adopts it. `close()` marks the instance, its log and its body store closed: every request is refused 503 `SERVICE_CLOSED`, and log and body writes raise. Regressions: `tests/test_aegis_r5b.py` |
| V5c-L1, V5c-L2, V5c-Info (focused check of e0a69a9..4f10b5f) | `lock_token` adoption trusted the caller; `close()` ran outside the service lock and the closed check in `append_prepared` sat outside the log lock; `clear_pending`/`clear_discarded` on a closed instance could delete the live instance's files; `/svc/v1/status` still said ok after close | The service adopts a handed claim only through `DataDirLock.adopt(token)`: true only for the current claim's token (constant-time compare) and only once; otherwise it refuses `DataDirBusy`. `release_claim` is a no-op for a stale or foreign token. `close()` takes the service lock, then each store's lock. Every log write and clear checks `closed` inside the log lock. `/svc/v1/status` reports `status: closed`, `closed: true`, integrity not ok. Regressions: `tests/test_aegis_r5c.py` |
| a5dd261-M1, L2, L3, L4, I5, I6, I7 (AEGIS review of a5dd261) | A closed instance still ran `verify_integrity` (integrity job, audit route) and re-recorded its own pending line on the ledger, leaving the live instance's integrity permanently not ok; the audit route answered a cached ok after close; `claim`/`holds`/`release_claim` were not atomic; a claim token could be adopted by two services; `BodyStore()` swept `bodies/*.tmp` before the claim was verified; a test assertion was vacuous; `/health` answered 200 when closed | `verify_integrity` returns the closed result first, under the service lock, with no ledger I/O. The integrity job and `/svc/v1/audit/integrity` (`audit_integrity`) refuse 503 `SERVICE_CLOSED` when closed. `DataDirLock` guards `claim`/`holds`/`adopt`/`release_claim` with one `threading.Lock`. Adoption is single-use (a second service with the same token is refused `DataDirBusy`). Constructing a `BodyStore` deletes nothing: the service calls `sweep_tmp()` only after adopting the claim. The r5c test asserts both conditions. `/health` returns 503 `{"status": "closed"}` when closed; `degraded` stays 200 as before. Regressions: `tests/test_aegis_r5d.py`, `tests/test_aegis_r5c.py` |
| cc27b69-L1, cc27b69-Info (AEGIS review of cc27b69, not blocking) | The ledger's chain `verify()` (an HTTP call, up to the client timeout) ran inside the service lock, so every request waited on it; the claimer's token could still release a claim a service had adopted | The integrity job and `/svc/v1/audit/integrity` share `_integrity_and_ledger`. Under the lock it refuses if closed and snapshots the integrity result and the log length; it releases the lock and calls `verify()`; then it re-takes the lock and re-checks. If the instance closed meanwhile: 503 `SERVICE_CLOSED`, nothing written. Superseded by db08ff1-M (next row): the retry and the `ledger_valid: null` path are gone. `DataDirLock.adopt` issues a new token that only the adopting service holds: the claimer's token no longer releases the claim. Regressions: `tests/test_aegis_r5e.py` |
| db08ff1-M, db08ff1-Info (AEGIS review of db08ff1) | When the log changed during the ledger check, the `verify()` verdict was dropped and a `False` became `ledger_valid: null`: a broken ledger chain was never reported on a busy service, and any caller able to commit could hide it | `verify()` checks the ledger's own chain, independent of the local log, so its verdict is always reported as returned. The `null` path and the retry are removed. After `verify()` (still outside the lock) the lock is re-taken, closed is re-checked (503 `SERVICE_CLOSED`, nothing written), and `integrity` and `log_length` are read fresh. The integrity job raised no ledger alert or incident for `ledger_valid: false` before cc27b69-L1 (checked at 4f10b5f), and it still raises none: the verdict is reported only. Remaining stall (Info, accepted): `verify_integrity` still reads the ledger's `entries()` under the service lock, so a slow ledger read can still hold up requests. Regressions: `tests/test_aegis_r5e.py` |

## Amendment — AEGIS sweep A (Oct 6 2026, on 5d49ee9): every finding fixed

Regressions: `services/service-py/tests/test_sweep_fixes.py` (each one fails on 5d49ee9).

| Id | Finding | Fix |
|---|---|---|
| Sweep-A email opt-out (High) | An opt-out received by email ("UNSUBSCRIBE") revoked only SMS consent: proactive email (an NPS survey, a check-in, an offer) still went out. "do not email me" and "dont email me" were not opt-outs at all | An `exact` opt-out received by email also revokes the contact's EMAIL consent (`consent_revoked`, via `stop_by_email`, for the contact's address), unless its words name only the phone ("stop texting me", "remove my number"): that stays an SMS opt-out (the existing rule) and the ticket can still be answered by email (`channels.email_opt_out`). SMS is revoked as before. `dont email`, `do not email`, `stop emailing`, `quit emailing`, `never email`, `remove my email` and kin are exact opt-out terms |
| R6-M1 | Typed events were recorded before the anchor and their id included the payload hash, so a retry after a state change (a STOP whose anchor failed, retried once the contact existed) recorded a second `consent_changed` event; nothing told the committed one from the orphan | bizdev-py's round-6 pattern: every payload also carries `rk` (a keyed HMAC of the request key, which can name an address: never the key itself) and `seq`; the id includes that payload's hash; the line names its events (`ledger_evidence`). `GET /svc/v1/audit/evidence` (dashboard, compliance_38) marks each event `committed` or `attempted`: exactly one committed event per logical action. Events recorded before this change carry no rk / seq and read as `attempted` |
| Sweep-A inbound refused | An inbound email from `Owner@Acme.test`, with a tab in the subject (a folded header) or a text over 20,000 characters was refused 422, its opt-out lost | Before the strict checks, inbound email and SMS bodies are read leniently: addresses lowercased (`Name <addr>` read as `addr`), control characters in the subject replaced by spaces (a subject blank after that is dropped), control characters other than newline / tab removed from the text, the text truncated (20,000 email, 1,600 SMS) and an empty text accepted (a subject-only or media-only message) |
| Sweep-A health recompute | `_health_compute` rescanned every ticket twice and every survey for EACH account with the service lock held: O(accounts x tickets) | `_health_aggregates` computes complaints in 30 days, open escalations and the latest NPS per account in one pass over the tickets and one over the surveys. The regression counts `_ticket_account` calls (at most one per ticket); no wall-clock bound |
| Sweep-A closed dispatch | A closed instance still called the alert and handoff ports in `dispatch_side_effects` (only the result commit was refused), so the instance that then owned the data directory sent the same alert again | `_closed` is checked at the snapshot and again, under the lock, right before each port call: a closed instance dispatches nothing |

### Sweep A follow-up — AEGIS review of 17cda6a (REVISE): fixed

Regressions: `services/service-py/tests/test_sweep_fixes.py` (the tests after "AEGIS review of 17cda6a").

| Id | Finding | Fix |
|---|---|---|
| H1 (High) | `email_opt_out` scanned the whole body and subject for phone words: "Unsubscribe" with a signature "Cell: 310-555-1212", "UNSUBSCRIBE / Sent from my phone", "unsubscribe" above a quoted "call our phone line", or "Please unsubscribe me" under the subject "Re: Text us anytime" kept email consent | The scope comes from each matched opt-out phrase itself (`channels.opt_out_scope`), in the person's own words: quoted lines and the quoted thread (`strip_quoted`: `>` lines, "On ... wrote:", "-----Original Message-----") and the signature (`strip_signature`: a `--` delimiter, a sign-off line, "Sent from my ...") are removed first. Email consent is kept only when EVERY phrase found names the phone itself ("stop texting", "remove my number", "unsubscribe from texts": a phone word in the phrase, or right after it past connector words such as "me from your"). An exact opt-out whose phrase is not found in the stripped text revokes email too (err toward honouring). Inbound email is classified on the quote-stripped text, so our own quoted footer no longer reads as an opt-out |
| M1 | A typo of unsubscribe ("unsubcribe", "unsubscibe") by email scored `suspected` and only raised an alert | A typo of stop / unsubscribe received by email revokes EMAIL consent (`consent_revoked` via `suspected_stop_by_email`, recorded as `consent_changed` evidence); SMS keeps the existing rule (pause and SMS_OPT_OUT_SUSPECTED alert) |
| M2 | EmailIn / SmsIn were strict: extra fields, a missing or Message-ID-style request_id, a null text or a non-string subject were refused 422 | sales-py's raw-body approach: the two inbound routes read leniently (`api.inbound_body`, no forbidden-key scan: unknown fields are dropped before validation and never stored), a missing or unusable request_id becomes `h-` + the body's SHA-256 (the gateway's retry of the same body is the same message), a null text is "", a numeric subject is text and any other non-string subject is dropped, an unusable ticket_id is dropped. The byte cap is unchanged. A body without brand or addresses is still 422: it cannot be routed |
| M4 | `/svc/v1/audit/evidence` read the whole ledger on every call | It reads only this department's entries (and one event type when asked), page by page, through `HttpLedgerClient.entries_filtered` with ledger-rust's `?after_seq=&limit=&department=&event_type=` (fix-ledger, sweep F-2). At 5d49ee9 ledger-rust has no such query (it answers 404 to any path with a query, checked against the real binary): the client then reads the whole ledger once and filters it, bounded by `LEDGER_ENTRIES_MAX_BYTES` like `entries()`; a ledger that ignores the query is recognised (more than a page, or a page that does not move past `after_seq`). Outside the service lock as before |
| L2 | The text was cut to the cap before it was classified: an opt-out past 20,000 characters was lost | A text over the cap keeps its head and its last 2,000 characters (`models.cut_head_tail`) |
| L3 | `audit/events` exported `ledger_evidence[].payload.rk` unchanged, so two exports could be joined on it | `rk` is re-keyed with the export's own HMAC key |
| L5 | `dispatch_side_effects` checked closed at the top of each iteration, not right before the port call | The check sits immediately before each port call. Residual window (documented in the docstring, accepted): a `close()` between that check and the call cannot stop that one call; holding the lock across a provider call would stall every request, and the alert and handoff ids are the providers' idempotency keys |

### Sweep A follow-up — AEGIS re-review of 1e709a0 (REVISE): fixed

Regressions: `services/service-py/tests/test_sweep_fixes.py` (the tests after "AEGIS re-review of 1e709a0").

| Id | Finding | Fix |
|---|---|---|
| N1 (High, regression) | `strip_quoted` dropped everything after a reply header, so an opt-out typed below the quote was lost on both channels | `channels.split_reply`: `>` lines and a `<blockquote>` are dropped; a header followed by `>` lines is dropped and the lines after the quoted block stay the person's own. A header followed by unmarked lines (Outlook "Original Message", "On ... wrote:" without `>`) starts the quoted tail, which is read only for strong opt-out wording (`OPT_OUT_STRONG`) or a bare stop / unsubscribe last line; a hit revokes (over-suppressing is the safe side) and raises `OPT_OUT_IN_QUOTED_TEXT` for Andre. The whole message is NOT scanned: our own quoted words ("cancel anytime", "ends soon") would opt every replier out. A future marketing footer must be listed in `OWN_FOOTER_LINES` |
| H1 residual (High) | `_phone_scoped` ignored an email word in the same phrase, crossed line breaks, and the sign-off cut dropped later opt-outs | An email word (`EMAIL_SCOPE_WORDS`) in the phrase's reach makes it an email opt-out; each line and sentence is read alone; a phone word followed by a number or "is" is an address label, not a channel; the scope is read on the person's own words without the signature cut |
| N4 (High, pre-existing) | HTML tags were deleted without a space, merging "Unsubscribe<br>Sent" into one word | `channels.html_as_text` before every opt-out check: block tags end a line, other tags are a space |
| N3 (Medium) | A gateway reusing a request id for another body got 409, losing that message (an opt-out) | On the email and SMS gateway routes the message is re-keyed `<id>.b<body sha16>` and processed; chat (our own hub) still answers 409. Accepted residual: with NO request id, an identical body is the same message (the id is the body hash) |

### Sweep A follow-up — AEGIS re-review of 91f5b8b (REVISE): fixed

| Id | Finding | Fix |
|---|---|---|
| R1 (High, regression) | A greedy `<blockquote>` match ate the person's text between two quotes | Quotes removed innermost first, non-greedy; an unclosed quote starts the quoted tail |
| R2 (High) | Common wording below an unmarked quote ("no more emails", "STOP. Thanks", "stop / Sent from my iPhone") was ignored | Tail: `OPT_OUT_STRONG` (widened) revokes; a last line (signature removed) that is only stop / unsubscribe plus courtesy words revokes; any other opt-out wording raises `OPT_OUT_IN_QUOTED_TEXT` for Andre instead of being dropped |
| R3 (High) | "stop texting me, same for email" stayed SMS-only | An email word anywhere in the person's own words makes the opt-out `all` |
| R4 (Medium) | Third-party wording in a quote ("do not contact the carrier", "opt out of the warranty") revoked | The strong list holds only unsubscribe / email wording; a short last line must be stop / unsubscribe itself ("Cancel anytime." never revokes); `OWN_FOOTER_LINES` matched as re-wrap-tolerant substrings. Before any proactive email with an unsubscribe notice ships, its text must be added there |
| R5 (Medium) | A Gmail header wrapped over two lines, forwarded and localized headers were read as the person's own words | Header matched on a line or a line joined with the next; "Forwarded message", Spanish, French, German, Portuguese forms added |
| R6 (High, pre-existing, SMS) | `<STOP>`, "i <3 u but stop texting me >:(" read no opt-out (angle brackets stripped as tags) | Opt-out checks also read the text with `<` `>` as spaces |
| R7 (Low) | A literal re-keyed id with another body got an uncaught 409 | Re-key is `<id[:80]>.r<sha8(id)>.b<sha16(body)>`; a second conflict falls back to `rk.<sha256(body)>`. Accepted residual: a redelivery differing only in whitespace is a second message |

### Sweep A follow-up — AEGIS re-review of d1477aa (REVISE): fixed

| Id | Finding | Fix |
|---|---|---|
| H-A (High) | "Stop texting me. Email me instead." / "I prefer email" / "my email is ..." revoked email | An email word widens an SMS-only opt-out only with an adding word (too, also, same, as well, and/or, spam, inbox); a preference (instead, prefer, rather, only, use, reach) or an address ("email is", "email me") keeps it SMS-only |
| H-B (High, pre-existing) | Outlook for Mac / new Outlook "From: / Date: / To: / Subject:" quote block read as the person's words | A `From:` line with a `Sent:`/`Date:` line within the next three starts the quoted tail (English, Spanish, French, German forms) |
| M-1 | Common words in our quoted mail ("end of the week", "cancel anytime") alerted Andre | The tail alerts only on multi-word opt-out phrases (plus "cancel my subscription / account / membership", "opt me out", "stop sending", "take me off"); "no more", "who is this", "wrong person" excluded |
| M-2 | A forwarded newsletter's "To unsubscribe click here" revoked | Text below "Forwarded message" / "Begin forwarded message:" is never read for an opt-out |
| R6 by email | `<STOP>` by email was stripped as a tag | Only known HTML tag names (and comments) are tags |
| L-2 | A blank `OWN_FOOTER_LINES` entry would disable tail detection | Blank entries skipped |
| L-1 (accepted) | "On second thought / ... my wife wrote:" can be read as a wrapped header | Accepted: contrived; the message still goes to a human and pauses SMS |

### Sweep A follow-up — AEGIS re-review of 3c89631 (REVISE): fixed

| Id | Finding | Fix |
|---|---|---|
| H-C (High, regression) | "Do not text or email me" revoked SMS only | "email me" after or / and / nor adds email; an explicit negated email phrase ("don't ... email", "text or email me") always widens to `all`, whatever preference word is present (M-3) |
| M-3 | A preference word narrowed an explicit email opt-out to SMS | As H-C |
| M-4 | "Stop!" above a name sign-off below an unmarked quote raised nothing | A short stop / unsubscribe / quit line among the tail's last three lines alerts Andre; "stop contacting", "quit it", "stop messaging" alert |
| M-5 | A customer's own "From: / Date:" lines were read as a quote header | The header block also needs an address on the From: line or a To: / Cc: / Subject: line |
| L-4 | `<style>` / `<script>` content was read as the person's words | Removed before reading |
| L-3, L-5 (accepted) | Text typed below a forward is not read; unrelated quoted phrases ("not interested in ...") may alert | Accepted: rare; the message still reaches a human and pauses SMS; an alert is the safe side |

### Sweep A follow-up — AEGIS re-review of ffe7ede (REVISE): fixed

| Id | Finding | Fix |
|---|---|---|
| H-D (High, regression) | "stop texting me and email me instead", "... or email me if you must" revoked email | One grammatical rule (`channels._email_word_adds`) used by the lookahead and the negation regex: "or / nor" + email carries the negation; "and emailing" (the same verb form as "stop texting") too; "and email me", "email me instead / if / only", "emails are fine", "my email is" are requests or addresses and never widen |
| M-6 | "stop the texts, emails are fine" revoked email | As H-D |
| M-7 | An unclosed `<style>` flood cost ~1.6 s per message under the lock | Style / script removed only when a closing tag exists, with a bounded pattern |
| L-6 | Our quoted "Stop by anytime!" alerted | The short-line tail alert needs a line of only stop / quit / unsubscribe and courtesy words |

### Sweep A follow-up — AEGIS re-review of 37eff26 (REVISE): fixed

| Id | Finding | Fix |
|---|---|---|
| H-E (High, regression) | "Do not text or email me please" / "... if you can help it" / "..., only call" revoked SMS only | "please" is not a preference word; after "or / nor" email is added unless a base-form "email" follows a gerund ("stop texting me or email me if you must" stays a request) |
| M-8 | A preference word in a later sentence narrowed an explicit opt-out | The preference check runs only when no phrase already reached an email word; "...the emails please. I prefer you call." is `all` |

### Sweep A follow-up — AEGIS re-review of b7cc067 (REVISE): decide only the clear cases

The email scope of an SMS opt-out flipped on every round from d1477aa to b7cc067 (H-A, H-C, H-D, H-E, H-F): word-level
guessing cannot settle every phrasing. Decision (`channels.email_opt_out_decision`): the code decides only the clear
cases and asks Andre about the rest, so a wrong guess becomes an alert, never a silent error.

| Outcome | When |
|---|---|
| `revoke` (email and SMS) | A phrase that names no phone ("unsubscribe", "stop", "do not email me"); a phrase whose own clause reach names email ("text or email me please", "texts and emails", "texting and emailing", "call, text or email" — a comma inside a channel list is not a clause end); an explicit negated email clause elsewhere; an adding word with email ("same for email", "email too"); strong wording in the unmarked quoted tail |
| `keep` (SMS only) | Every phrase names only the phone and no email word appears, or the email clause is a request ("email me instead", "you can call or email", "emails are fine", "my email is ...", "call or email me", "or email me if you must") |
| `ask` (SMS only + `EMAIL_OPT_OUT_UNCLEAR` alert) | An SMS-only opt-out with an email word that is neither adding nor a request, or both |

"do not call", "dont call", "never call", "stop calling" added to the opt-out terms (phone scope). The corpus of every
phrase from all rounds is pinned in `test_scope_corpus_all_rounds_at_once`.

### Sweep A follow-up — AEGIS re-review of 7d58d7b (REVISE): fixed

| Id | Finding | Fix |
|---|---|---|
| H-G (High, regression) | "Don't text, email me instead" was joined into "don't text or email me" and revoked email | A comma joins channels only in a real list of three or more ("call, text or email"); two items are two clauses |
| M-9 | "Never call before 9 please" revoked SMS | Call wording is scope-only (`SCOPE_ONLY_TERMS`): it names a channel for the scope but is never an exact SMS opt-out; "Do not call, text or email me" revokes email and pauses SMS with `SMS_OPT_OUT_SUSPECTED` for Andre |

### Sweep A follow-up — AEGIS re-review of f63a9b2 (REVISE): fixed

| Id | Finding | Fix |
|---|---|---|
| H-H (High) | "Do not call or email me" (no SMS wording, so no opt-out level) kept email with no signal | Every inbound email gets an email decision; a scope-only call phrase whose reach names email revokes email (`consent_changed` evidence) and raises `SMS_OPT_OUT_SUSPECTED` |
| M-11 | Oxford comma "call, text, or email" was not a list | ", or / , and" between channels joins the list |

### Sweep A follow-up — AEGIS re-review of 2018cd2 (REVISE): fixed

| Id | Finding | Fix |
|---|---|---|
| H-I (High, regression) | With the email decision on every inbound email, complaints ("why do you never call or email back?") and time limits ("don't call or email before 9am") revoked email under an SMS alert code | With no opt-out level, email is revoked only for a clause that is nothing but a direct command not to contact the sender by two or more channels ("Please do not call or email me again"): `revoke_direct`, evidence via `request_by_email`, alert `EMAIL_OPTED_OUT_BY_REQUEST`. Any other no-level case is `ask` (`EMAIL_OPT_OUT_UNCLEAR`) |

### Sweep A follow-up — AEGIS re-review of b3725e9 (REVISE): fixed

| Id | Finding | Fix |
|---|---|---|
| H-J (High, pre-existing) | "I don't want your emails", "I no longer want to receive your emails", "I do not want calls or emails" were not opt-outs | `_NEG_WANT`: a negated want / wish / need (to receive / get / hear) of emails, texts, messages or newsletters is an exact opt-out; its channels decide the scope (email or messages → `all`, texts only → SMS). Calls alone are not an SMS opt-out |

### Sweep A follow-up — AEGIS re-review of 9c55872 (REVISE): fixed

| Id | Finding | Fix |
|---|---|---|
| H-K (High, regression) | "I don't need the email receipt", "I don't want the mail carrier to ...", "... the text on the banner" revoked consent | `_NEG_WANT` takes only want / wish (to receive / get), read per clause, and the channel must end the clause or be followed by from you / anymore / again / please |
| Pre-existing gaps | "Opt me out", "removed from your email list", "I don't want to hear from you again", "Delete my info", "Enough with the emails", "I'd rather not receive these" were neither honoured nor surfaced | Added to the opt-out terms (no phone word, so scope `all`) |

### Sweep A follow-up — AEGIS re-review of 94b4dde (REVISE): stop growing the revoke list

| Id | Finding | Fix |
|---|---|---|
| H-L (High, regression) | Round-12 terms as bare substrings revoked ordinary mail ("removed from your page", "opt me out of the warranty", "rather not receive a partial shipment") | Decision: the exact-revoke list (`OPT_OUT_TERMS`) is closed to context-dependent wording. Such wording (`OPT_OUT_POSSIBLE_TERMS`: "opt me out", "remove me from your ...", "don't want to hear from you", "delete my info", "cease all communication", "no further contact", ...) changes no consent and raises `OPT_OUT_POSSIBLE` for Andre. New wording found later goes there unless it cannot mean anything else. "marketing / promotional" allowed in the negated-want rule ("I don't want your marketing emails" revokes) |

Operational dependency (Andre): `OPT_OUT_POSSIBLE`, `EMAIL_OPT_OUT_UNCLEAR` and `OPT_OUT_IN_QUOTED_TEXT` alerts must be
worked within days — CAN-SPAM requires an email opt-out honoured within 10 business days.

### Sweep A follow-up — AEGIS re-review of 48eedfd (APPROVE WITH CONDITIONS): condition fixed

| Id | Finding | Fix |
|---|---|---|
| H-M (High-class, pre-existing; the approval's condition) | Bare "remove me" / "take me off" revoked "take me off hold please", "remove me from the order as the contact" | `_REMOVE_ME`: exact only at the end of a clause ("Remove me.", "remove me please") or bound to a list / texts / emails / messages / contacts / database ("take me off your mailing list"); other uses fall to `OPT_OUT_POSSIBLE` |
| H-N (High, regression from the H-M fix) | "Stop texting me. Take me off your email list" kept email: `_REMOVE_ME` was read by the level only, not the scope | The scope reads `_REMOVE_ME` too; its object decides (email / list / messages / contacts → `all`, texts only → SMS); "text and email lists" accepted as an object; "remove me" alerts from a quoted tail |

## Amendment — Bug sweep E fixes (Oct 9 2026)

Regressions: `services/service-py/tests/test_bug_sweep_e_store.py` (each fails before the fix).

| Id | Finding | Fix |
|---|---|---|
| E-M1 (finance-py AEGIS 5a56a3a M1 / c0869c4 M1, backported) | `RecordLog.append_prepared` ignored a short `os.pwrite` (disk full, quota, a signal): a partial line stayed on disk that memory did not hold, and the append "succeeded" | A short write is a failed write: the file is cut back to its previous length, fsynced, and `StoreWriteError` is raised. If the cut-back itself fails the log carries `fault`: integrity reports `LOCAL_LOG_WRITE_FAULT`, `/health` says `degraded`, the authenticated status view carries `log_write_fault: true`, and every later append refuses until the file is inspected |
| E-M1b | `_write_file` (the pending-line files) ignored a short `os.write` too | A short write raises; the temp file is removed and never replaces the target |

## War room fixes (Oct 10 2026; ADR 0018 red-team gate, `devtools/warroom/findings.md`)

Pinned by `services/service-py/tests/test_warroom_fixes.py`; the AEGIS-certified corpus (`tests/test_sweep_fixes.py`,
every H- / N- / R- test) passes unchanged. The replay cases stay in `devtools/warroom/replay/service-py.json` as
regression cases (`fixed`).

| Id | Finding | Fix |
|---|---|---|
| WR-F001 | Opt-outs written with `β ɡ η ζ` (letters the repo's shared lookalike table and sales-py's small table both fold) were neither honoured nor surfaced: `triage.CONFUSABLES` folded 19 of the 23 | Every opt-out rule in `channels.py` reads text through `triage.normalise_opt_out`: `clean(..., opt_out=True)` folds with the shared lookalike fold (`src/lookalikes.py`, byte-identical in four services; ADR 0007 "War room fixes"): `CONFUSABLES` on top (nothing it folded changes), over creative-py's shared hand table and the Unicode confusables.txt 15.1.0 skeleton, case-sensitive, then lower case and the table again. Triage's own reading (categories, `non_ascii_letters`) keeps `CONFUSABLES` alone, so a message in another script still reaches a human as one. A capital that is the upper case of a lower-case lookalike (`сOmmΥnіCATIoΗ`: upsilon for u, eta for n, case flipped) keeps its visual reading (`Υ` is Y) for every revoking rule; the casefold-first reading only surfaces the message (`possible_opt_out` → `OPT_OUT_POSSIBLE`) |
| WR-F006 (AEGIS H1 on the WR-F001 fix) | The shared fold maps Cyrillic `п` to `n`, so a word written wholly in Cyrillic was read as a Latin one: `Пожалуйста, отправьте код по SMS` ("send the code by SMS") revoked SMS consent (`по` read as "no" beside "sms"), and a bare `По` revoked email; the same for Ukrainian, Bulgarian and Serbian | A word (letters, digits, marks) of two or more letters written wholly in ONE non-Latin script is read with `CONFUSABLES` alone, as before the war room, unless the shared layers make it an English opt-out word of four letters or more (`triage.OPT_OUT_DISGUISE_WORDS`: an all-Cherokee `ᏚᎢᎾᏢ`); a word that mixes Latin with lookalikes, or two scripts (`ЅΤΟΡ`), and a single letter (spaced-out `Ꮪ Ꭲ Ꮎ Ꮲ`) are folded in full (`lookalikes.Table.fold(..., single_script=...)`, opt-in, so the other services' folds are unchanged). The casefold-first reading leaves such a word as written. Everything the base (2cedde8) caught is still caught (`ЅТОР`, `ѕтор`, `вуе`, `ΝΟ`); Russian `стоп` was not an opt-out there and is not one now (no language's opt-out wording is guessed from its look). War room scenarios `foreign-script-ordinary-sms` / `-email` (MUST `consent_unchanged`) |
| WR-F004 | `５７０ｐ` (full-width digits as leetspeak) was not read: the digit table ran before NFKC | `_leet` applies NFKC first, everywhere the digit table was applied (`opt_out_level`, `opt_out_scope`, `typo_opt_out`, `quoted_tail_opt_out`) |
| WR-F004 (seed 3) | `Ｐｌeaｓｅ stop tｅxtｉnｇ． Cａｌｌ ｏｒ ｅmail ｉf neｅded`: the full-width full stop did not end the clause, so the email request was read as part of the opt-out and email was revoked (also on the war room's base) | The opt-out rules read a message through `_read`: `html_as_text`, NFKC, then the clause split (replay `service-py/R0016`) |
| SHOULD (dotted) | `N.e.v.e.r call or email me again` was not read (a dot ends a clause) | `_read` joins a word spelled out with `.`, `-` or `_` between three or more single letters before the clause split (`e.g.`, `a.m.`, domains unchanged) |
| SHOULD (quoted tail) | Below an unmarked Outlook quote, "Dont call me or email me anymore" or "I want to be removed from your email list" raised nothing, though R2 says any other opt-out wording raises `OPT_OUT_IN_QUOTED_TEXT` | `quoted_tail_opt_out` also answers `alert` (never `revoke`) for the sentence-shaped opt-outs the own-words rules read: a direct no-contact command, `_REMOVE_ME`, `_NEG_WANT` and the possible-opt-out wording. Single words and loose pairs in our own quoted mail still raise nothing (M-1) |
| SHOULD (quoted tail, seeds 1-3) | Below an unmarked quote, `Stop the texts and the emails please`, a bare `STOP` line after a sign-off (`Thanks,\nSTOP`, cut with the signature) and leet alert wording (`D0n7 text or email m3`) still raised nothing | `quoted_tail_opt_out` alerts (never revokes) on a clause that STARTS with an imperative stop / quit / cease naming a channel or a contacting verb right after it (`_TAIL_STOP_CHANNEL`: a mailer's `Stop by anytime!` or `You can stop these emails at any time` still raises nothing); on a line after the sign-off that is nothing but exact opt-out words; and reads the alert phrases on the leet view too, as the strong-wording check already did |
| SHOULD (long middle), not changed | An opt-out in the middle of an email over the 20,000-character inbound cap is dropped: `models.cut_head_tail` keeps the head and the last 2,000 characters before the service sees the text | Not local: the cut is the gateway model's (AEGIS L2), and keeping a middle clause means running the opt-out rules inside request validation. The documented promise stays an opt-out at the start or the end of a long message; the war room keeps scoring it as SHOULD. For Andre: a fix is to scan the dropped middle with `opt_out_level` in `_lenient_inbound` and carry the matching lines into the kept text |
