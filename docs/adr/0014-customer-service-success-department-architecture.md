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
