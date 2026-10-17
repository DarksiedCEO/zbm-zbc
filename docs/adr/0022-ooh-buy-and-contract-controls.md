# ADR 0022 — Out-of-Home & DOOH (15): buy and contract controls (OOH-4)

Status: accepted for build, Oct 16 2026. Built on branch `ooh-dept-15-wave2` on top of OOH-3 (ADR 0021). **Not in
force.** No agent is built; nothing here buys media or signs anything; there is no e-signature or vendor port.

**Source caveat.** The brief named OOH-4 only as "buy/contract controls" and said to follow the blueprint, which was
not available as a file. The scope below is this build's reading on top of the section 12 purchase gate of ADR 0019,
bound by the locked decisions: Andre is Buyer and signer; agents prepare and never commit money or contracts;
vendors are paid by ACH or check only, never card; max single purchase $10,000; authorization lasts 7 days.

## Decisions

1. **The vendor contract** (insertion order), `POST …/campaigns/{cid}/contracts`, preparers: drafted from the vendor's
   document — vendor, the exact quotes it covers, its total, currency, the IO document's reference, the
   cancellation terms' reference, payment terms (0..120 days) and the vendor payment method. Optional vendor text is
   untrusted (hash-only on the ledger). Drafted from `QUOTES_VALIDATED` to `PURCHASE_AUTHORIZED`
   (`policy.CONTRACT_WRITABLE`). Ledger event `vendor_contract_drafted`.
2. **The draft must match what was quoted**: the quotes are this campaign's live, unexpired vendor quotes of that
   vendor, each once, no unit in two of them (`QUOTE_NOT_FOUND`, `VENDOR_MISMATCH`, `QUOTE_RETIRED`, `QUOTE_EXPIRED`,
   `UNIT_IN_MULTIPLE_QUOTES`); the total is exactly their sum (`CONTRACT_TOTAL_MISMATCH`); the cancellation terms
   are the commercial model's (`TERMS_CHANGED`).
3. **Vendor payment: ACH or check, never card** (locked). The method is `ach` or `check` by type (card, wire, cash:
   422), and it must be one the vendor's sourced supplier profile accepts (ADR 0020): nothing sourced is
   `VENDOR_PAYMENT_METHOD_UNKNOWN`, an unlisted method `VENDOR_PAYMENT_METHOD_NOT_ACCEPTED`. Checked at draft, at
   approval, and by the gate at authorization and at submission (a vendor that stops taking a method blocks the
   order).
4. **Only Andre approves a contract's terms** (`POST …/contracts/{kid}/approve`, dashboard + Andre's token, bound to
   the campaign version): `DRAFT → TERMS_APPROVED_BY_ANDRE`, after the terms are checked again. Once only
   (`CONTRACT_NOT_DRAFT`). Ledger event `vendor_contract_terms_approved_by_andre`. Agents draft; they never approve.
5. **No purchase without it.** The authorization body names `contract_id`. The purchase gate, after the terms check
   and before the funds check, at authorization and again at submission, requires that contract to be this
   campaign's (`CONTRACT_NOT_FOUND`), approved (`CONTRACT_NOT_APPROVED`), and identical to the authorization in
   vendor, quotes, total, currency and cancellation terms (`CONTRACT_MISMATCH`), with its payment method still
   accepted. (The quote-set and total comparisons are deliberately redundant: either alone catches the reachable
   mismatch, a contract covering fewer quotes than the order.)
6. **The contract follows the order.** A new draft sharing a quote with an open contract supersedes it (`VOID`,
   `SUPERSEDED`), and voids any unsubmitted authorization under it (`CONTRACT_SUPERSEDED`). Submission →
   `SUBMITTED`; the vendor's `ACCEPTED` → `EXECUTED`; `REJECTED` → `VOID` (`VENDOR_REJECTED`): buying again needs a
   new contract. A change request or a cancellation voids every open contract.
7. **The locked limits stay where ADR 0019 put them**: $10,000 per authorization and 7 days are ceilings
   (`MAX_PURCHASE_CEILING`, `AUTHORITY_DAYS_CEILING`); one open order per face per overlapping flight across all
   tenants; a confirmed hold (Andre's) for every unit; principal/agent confirmed by a CPA or counsel; 100% prepaid.
8. The hub never reads a contract (it carries vendor cost). Everything replays identically.
9. **Finance (31) must verify funds cleared** (locked; AEGIS OOH-3/4 plan fidelity PF-1). Built literally, fail
   closed: a new caller `finance_31` posts `POST …/campaigns/{cid}/finance-verifications` — the current client
   approval's id, the verified amount and currency, Finance's own reference, `cleared: true` — recorded
   `FINANCIALLY_VERIFIED`, source `FINANCE_31` (ledger event `client_funds_verified_by_finance_31`). Not even Andre
   can post it. The purchase gate, at authorization and at submission, after the funds checks, requires it for the
   current approval, in the same currency, covering the approved price and every open commitment plus this order
   (`FUNDS_NOT_FINANCE_VERIFIED`). A change request clears it. Andre's gate still records what he observed (and
   moves the campaign to `FUNDS_VERIFIED`), but it is no longer enough to buy. Until Finance holds a `finance_31`
   token and calls this route, no purchase can be authorized.

## AEGIS review of OOH-2..4 (Oct 16 2026): fixes

- **Security F1 (HIGH), an id collision.** Object ids derived from a request key left out the caller, and
  idempotency is per caller, so a preparer reusing Andre's request id got the same price id and overwrote his record
  (a buy limit raised from 1000.00 to 9999.00; a quote under an approved contract rewritten). The pattern dated
  from waves 0-1. Every created id now includes the caller (campaigns, units, prices, approvals, authorizations,
  contracts, sources, avails, estimates, markup evidence, hold evidence), and every create refuses an id that already
  exists (`REQUEST_ID_REUSED`), so no record is ever overwritten.
- **Security F2, plan-guard ordering.** `QUOTES_VALIDATED` now re-runs the plan guards and needs at least one line
  (`PLAN_EMPTY`), so units added after an (empty) plan approval are checked; a quote naming several units counts once,
  in full, while any of them is live (withdrawing one no longer hides cost).
- **Security F3 / wave 2 F-2, personal data.** `src/pii.py`: NFKC-normalised and case-folded text is checked for
  e-mail addresses (also `[at]` / `(at)` / ` at ` spellings) and phone numbers (as rewritten in the second and final
  rounds below). Free-text attribute values carrying them are refused; vendor text is stored with them redacted
  (`personal_data_redacted`). References cannot hold `@`; a reference made of digits cannot be told from an id.
- **Red team F2 (wave 2), card and cash.** A client funds reference naming a card, cash or a payment app (`card`,
  `visa`, `mastercard`, `amex`, `debit`, `credit`, `cash`, `zelle`, `venmo`, `paypal`, `cashapp`) is refused under
  ACH (`FUNDS_METHOD_CONFLICT`).
- **Red team F3 (wave 2), leading zeros.** A face reference's digit runs lose their leading zeros, so `LA-123` and
  `LA-0123` are one face (and one open order across tenants).
- **Red team F4 (wave 2), avails at the gate.** The purchase gate, at authorization and at submission, refuses a
  unit the vendor's avails block (`AVAILS_UNAVAILABLE`).
- **Log format 4**: face keys and object ids changed; a format-3 log refuses start (no migration: unreleased).
- Tests: `tests/test_aegis_ooh_w234.py` (with the red team's main mutation gaps: the registry kill switch on every
  registry write, every principal/agent field required, caller checks on the registry routes, the face filters,
  registry retries).

### Second round (re-verification at 70e6962 and the red team at 138dcc8)

- **Personal data (N1, N3, F4, F10).** `src/pii.py` rewritten: control and format characters read as spaces (as
  they are once stored), zero-width characters dropped, `%40` read as `@`; e-mail with `@` / `[at]` (spaces allowed
  around it and its dots), or ` at ` with a tight or `dot`-spelled domain; phones by North American shape (optional
  +1, 3-3-4 with any separators incl. dashes) or ten-plus digits. Dates, hours, ZIP+4 and grouped counts now pass.
  Campaign names and reference attributes are screened too; vendor-text redaction keeps the original case.
- **Instruments (N2).** More non-ACH words (money order, crypto, wallets, gift cards, `cc`, `mc`) and any 13-19-digit
  run with a card-network prefix (Visa 4, Mastercard 51-55, Amex 34/37, Discover 6011 — none of which an
  ABA routing prefix can start an ACH trace number with) passing the Luhn check refuse a client funds reference. Short words (`cc`, `mc`, `mo`, `btc`,
  `eth`) count only as whole tokens, never as a prefix before a digit (that misread `CC4491`-style bank references and
  made the suite flaky).
- **Finance references (N4).** One Finance (31) reference verifies one campaign (`FUNDS_EVIDENCE_REUSED`).
- **Rate cards (red team F6).** An estimate needs amount and period from ONE rate card source; an estimate from a
  card since replaced no longer counts in the plan (re-estimate).
- **Avails (red team F8).** Andre's hold confirmation is refused `AVAILS_UNAVAILABLE` when the vendor said no after
  the request.
- **Untested checks (red team F5a/F5b).** Tests for the gate's cancellation-terms comparison, contract reads through
  another campaign or tenant, kill switches on every new write, cancellation voiding contracts, supersession scope,
  quote eligibility in drafts and the plan, the snapshot holding contracts, and caller scopes on the new routes:
  `tests/test_aegis_ooh_rt34.py`. War room scenario `someone-else-approves-a-contract` (all MUST).
- Not changed: after a change request, Finance replaying its OLD request gets the idempotent answer (200, the campaign
  view, which shows no verification) — the standard retry rule; the gate refuses (N5).

### Final round (red team at 0a1729e)

- **ACH references (NF-1).** `credit` and `debit` alone are description words, not rails (an ACH credit or debit is
  ACH); instruments named in words (`money order`, `Apple Pay`, `Western Union`, `credit card` …) match on adjacent
  whole tokens, never inside a longer word (`APPLE-PAYMENT-0001` passes). The Luhn rule needs a card-network prefix,
  so no ACH trace number is refused by chance.
- **Non-ACH instruments (NF-2).** A card number is read across any separator (`4242.4242…`, `4242_4242…`,
  `4242:4242…`). More words are refused: Western Union / `wu`, MoneyGram, Stripe, Square, prepaid, `ltc`, `doge`.
  Accepted limit: `cc` and `mc` count only as whole tokens, so `cc4242` is recorded under ACH; refusing it would also
  refuse bank references such as `CC4491` (and random hex). The method itself is structurally ACH only, and a purchase
  also needs Finance (31)'s verification.
- **Personal data (NF-3, NF-4).** `src/pii.py`: HTML entities and bracket spellings of `@` and `.` (`&#64;`, `<at>`,
  `[.]`, `(.)`, ideographic dots) are read as the character; phone separators add the minus sign, colons, bars,
  middle dots, apostrophes and ampersands; seven-digit local numbers with a dash (`555-1234`) and international
  numbers (`+`, country code, eight or more digits) are refused. After a spaced `@` a dot takes spaces on both sides
  or none, so `Digital @ Sunset. Faces west` passes; GPS coordinates pass.
- **Delta review of the above (988725f).**
  - DF-1: every Luhn-valid 13-19-digit run is a card; only a 15-digit run (an ACH trace number's length) also needs
    a card-network prefix no routing prefix starts with (4, 34-38, 50-58, 6011), so JCB, Diners, Maestro,
    Mastercard 2-series and Discover 64/65 are refused and no trace number is. Digit groups of three or more are
    joined in every contiguous part (a card followed by an expiry is seen; a date's `-10-17` is never joined).
  - DF-2: a tight `@` (no space on either side) takes any domain and dots with spaces (`jane@163.com`,
    `jane@example .com`), wherever it starts in a token (`IG @lamar/jane@example.com`, a long URL); a comma before
    a number is no longer a reason to skip it (`48,310 555 1234`).
  - DF-3 (revised by the second delta review, E3): **the personal-data screen errs toward refusing.** A false
    positive is refused and never stored; a miss keeps personal data. A 3-3-4 number with any mix of separators,
    `, ` included, is a phone (every US layout of 324 tried is caught), so `360, 720, 1440` and a coordinate with
    seven decimals are refused, as documented false positives.
  - DF-4 (revised, E4): a Finance reference's key drops separators (`FIN31-ABC` = `FIN31ABC`), so two references
    that differ only in separators (`F31-1-10`, `F31-11-0`) are one: that refuses (fails closed) and never lets one
    reference verify two campaigns. A reference with no letter or digit is refused.
  - DF-5: plurals (`money-orders`, `giftcards`, `credit-cards`) are refused; `mo`, `btc`, `eth`, `wu`, `ltc` count
    before digits (`mo5`, `btc1`).
  - DF-6: an address's local part is bounded (64), so a 4,000-character adversarial text screens in about 50 ms.
  - DF-7: redaction repeats until nothing detectable is left.
  - E2 / F1 / F2 (second and third delta reviews): a digit run is read as a card only with a card-network prefix
    (Visa, Mastercard 51-55 and 2221-2720, Maestro, Mir 2200-2204, Amex, JCB, Diners, Discover / UnionPay / RuPay 6,
    RuPay 81/82, Uzcard 8600, Troy 9792, Humo 9860), and groups of three or more digits are joined, never with a nine-digit
    ABA routing number (one that passes the routing checksum). So a bank reference with a date or
    a routing number is not refused by chance, and every listed card network is.
  - Known false positives (refused, never stored): ten-digit references, `<word> at <word>.<word>`, a three- and a
    four-digit group joined by a dash (`LA-101-2025`), lists of 3-3-4 digit groups (`360, 720, 1440`,
    `405 | 110 | 1200`), coordinates with seven decimals, and `place@place. Word` (`Sunset@Vine. Faces east`).
  - Known false refusals (fail closed; fourth delta review H1): a reference made of an account number and a
    sequence number that together look like a card (a valid network prefix and Luhn check, about 2-5% of such
    shapes) is refused `FUNDS_METHOD_CONFLICT` (open question 4). A card split 10-6 and an account plus a sequence number
    cannot be told apart, and a card number must never be stored.
  - Known limits (accepted): a card number written in one- or two-digit groups (`42-42-42-…`), or split around a
    nine-digit group that passes the routing checksum, is not seen (only a deliberate spelling does that); `cc4242` (see NF-2); and
    card networks outside the listed prefixes are not screened (for example BORICA 2205, GPN, and WEX / Voyager fleet
    cards). These are acceptable only while the method stays structurally ACH, only Andre posts client funds, and a
    purchase needs Finance (31)'s verification (AEGIS Ω ruling 1).
- **Rate cards (NF-5).** An estimate counts only while the face's current rate card has the same sources, amount,
  period and date it was made from: a card re-recorded with another value under the same source makes it stale.
- **Finance references (NF-6).** One Finance (31) reference, in any case or separator spelling, verifies one campaign.
- **Tests (NF-7).** `tests/test_aegis_ooh_final.py`: Luhn bounds and doubling, every refused instrument, the PII
  inputs both ways, Finance retry, bearer and body checks, Andre's REJECT under UNAVAILABLE avails, both ends of the
  AVAILABLE cover, the all-zero face ref, and supersession leaving another contract's authorization. 30 targeted
  mutants (the red team's surviving ones among them, W34-172/173/375 included) are killed; the delta review's DF-8
  gaps (entity spellings, the eight-digit bound, redaction, credit/debit, litecoin/dogecoin) by 42 more.

## What OOH-4 does NOT do

- **No taxes, fees or permits** (final red team PF-2): no sales tax, production, installation, permit or municipal
  fee is computed or checked; a vendor quote's total is taken as the vendor states it.
- **No competitive separation**: nothing stops two competing advertisers on adjacent faces.
- **No contract amendments**: a change is a new draft that supersedes the open one (Andre approves it again).

- **No signing.** Andre still signs and sends the order outside the system (channel `MANUAL_BY_ANDRE`); there is no
  e-signature port (`OOH_ESIGN_URL` refuses start).
- **No vendor payment.** Paying the vendor is Finance's (31); this records only the method the contract states.
- **No Finance (31) client inside this service**: Finance calls in (decision 9); nothing here reaches a bank.
- No makegoods, cancellations in flight, or credit terms.

## Open questions for the founder

1. **Finance (31) must verify funds cleared** (locked). Built literally (decision 9): purchases need Finance's own
   verification, so none can happen until Finance (31) is wired to call the route. Confirm this reading, or rule
   that Andre's gate is an interim (that would be one constant, `policy.FINANCE_VERIFICATION_REQUIRED`).
2. Should approving a contract's terms be separate from authorizing the purchase (built: two steps, both Andre's), or
   one step?
3. Payment terms bound (built: 0..120 days).
4. About 2-5% of bank references shaped as an account number plus a sequence number are refused as possible card
   numbers (fail closed). How should Andre record such a reference: a separate structured field for the bank's
   transfer id, or another rule?
