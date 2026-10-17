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

## What OOH-4 does NOT do

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
