# Chargebacks, disputes, ACH returns and refund requests

Use this when Stripe tells you a payment is disputed, when an ACH debit fails after it looked paid, or when a client
asks for money back.

**Know which payment method it was first.** ZBM takes ACH (US bank debit) for every invoice, and cards only on Revenue
Recovery invoices when `FIN_CARD_PREPAYMENTS=1` (off by default). The two behave very differently:

| | Card dispute (chargeback) | ACH dispute / return |
|---|---|---|
| Can you contest it? | **Yes**, once, with evidence, before the deadline | **No.** A dispute filed within the time limit is final and cannot be contested through the ACH network. Only a *late return* inquiry (`warning_needs_response`) takes evidence. |
| Customer's window | Set by the card network | Up to **60 calendar days** after the debit for a personal account; up to **2 business days** for a business account |
| Your deadline | "Usually 7 to 21 days, depending on the card network"; the exact date is on the dispute | Only for an inquiry: the date on the inquiry |
| Fee | A dispute fee, plus a "countered" fee if you contest (returned if you win) | Dispute fee, not refunded |
| Outcome | The issuer decides; "can take up to 3 months" | Final at once |

Sources: Stripe, "Respond to disputes" and "ACH Direct Debit" (links at the end). Re-read them if a dispute looks
unusual; Stripe changes fees and flows.

**What Finance does by itself.** Finance books disputes from Stripe's `charge.dispute.*` events: F7 when the funds are
withdrawn, F7a when they come back, F7l when a dispute is lost, F13x for an ACH payment that failed after it looked
successful. It opens a break for you. It does **not** respond to Stripe for you.

---

## A. Card dispute (chargeback)

**Time limit: respond at least 2 days before the "evidence due by" date. Never let it lapse: no response = you lose.**

1. **(5 min) Open the dispute.** Stripe email → link, or https://dashboard.stripe.com/disputes → the dispute. Write in
   your calendar: the **evidence due** date minus 2 days. Note the reason (fraudulent, product not received, product
   unacceptable, subscription canceled, credit not processed, duplicate, general...).
2. **(5 min) Is it an inquiry or a dispute?** An inquiry is a pre-dispute question: answer it with evidence (do not
   click Accept; accepting an inquiry does not resolve it). A formal dispute: the money has already been taken back
   from your Stripe balance.
3. **(10 min) Read the claim.** Dispute page → **Review the claim details** (the bank's documents, if provided).
4. **(15 min) Contact the client.** Reply by email (from your zbestmedia.com mailbox; keep a copy). Many card disputes
   are mistakes ("I didn't recognize the charge"). If the client agrees it was a mistake, ask them to withdraw the
   dispute with their bank **in writing** and keep that email as evidence. Template D.1.
5. **(Decision) Accept or counter?**
   - **Accept** if the client is right (the work was not delivered, or you would refund anyway). Dispute page →
     **Accept dispute**. Then go to step 9.
   - **Counter** if the work was delivered as agreed and paid for. Continue to step 6.
   - If the client is now refused at Compliance with `sanctions: true`, **stop**: do not move or return money; follow
     [ofac-owner-screening.md](ofac-owner-screening.md) and contact Stripe support first.
6. **(60 min) Build the evidence pack** from ZBM's records (section C below). Combine items of the same evidence type
   into one PDF (one file per evidence type; 4.5 MB total; Mastercard 19 pages total). No links, no audio, no video,
   no "call me" (banks will not follow them).
7. **(20 min) Submit.** Dispute page → **Counter dispute** → page 1: why the dispute is wrong and the product type
   ("service", delivered digitally) → page 2: upload each file under the evidence type it proves, fill in the
   customer, product and refund-policy details → tick the box → submit. **You get one submission; you cannot add
   anything after.** Check every file before you submit.
8. **(Wait) Outcome.** Status becomes `under review`. Stripe emails the result and sends `charge.dispute.closed`
   (Finance books F7a if won, F7l if lost). This can take up to 3 months.
9. **(15 min) Afterwards.**
   - **Won:** nothing else; check that Finance's break closed.
   - **Lost or accepted:** the client still owes the invoice under the MSA unless the work was really defective. Decide:
     write it off, or send an overdue notice under the MSA's payment terms (template D.3) and pause further work for
     that client until it is settled. Record the decision in the client's file.

## B. ACH return or ACH dispute

1. **(5 min) Identify it.** Stripe sends `charge.dispute.created`. For an ACH payment that failed after it showed
   `succeeded`, Stripe creates a dispute with reason `insufficient_funds`, `incorrect_account_details` or
   `bank_cannot_process` and charges a failure fee. Finance books it (F13x / F7).
2. **(Decision) Is there an inquiry?**
   - Status `warning_needs_response` (a late-return proof-of-authorization inquiry): respond before its deadline with
     the mandate authorization (Stripe Checkout records it: date, IP, user agent), the invoice and the delivery
     record. Payments page → the payment → **Respond to the inquiry** → upload one PDF. Only once.
   - Any other ACH dispute: **final.** You cannot contest it. Go to step 3.
3. **(15 min) Contact the client** (template D.2). The client's mandate is now invalid: a new payment needs a new
   Stripe Checkout page (a new mandate). Make one only when the client confirms it will be paid.
4. **(Decision) Delivered work?** If the report or analysis was already delivered: title to the deliverables stays
   with ZBM until paid (MSA). Send the overdue notice (template D.3) and pause further work. If not yet delivered: hold
   delivery until a new payment shows `succeeded`.
5. **Prevention.** For a new client paying by ACH, consider releasing the report only after the payment has been
   `succeeded` for 2 business days (the business-account dispute window). Tell the client the timing up front.

## C. The evidence pack (from ZBM's records)

Gather what applies. Fill in the checklist in your notes and put the files in a folder for the dispute
(`~/zbm/disputes/<YYYY-MM-DD>-<client>/`, `chmod 700`). Strip anything about other clients.

| Evidence | Where it comes from | Proves |
|---|---|---|
| The client's acceptance of the MSA and SOW: version, `doc_sha256`, acceptance id, time, method, the signer's name and email | Legal (`/legal/v1/acceptances` record) and the email trail | They agreed to the service, price and refund terms |
| The signed SOW / order: service, price, deliverables | Legal document store; your email | What was promised |
| The invoice: number, amount, line items, issue date, approval | Finance (`/fin/v1/invoices/{id}`); Stripe invoice/Checkout page | What was charged and why |
| Payment record: Stripe payment id, date, method, the ACH mandate (date, IP, user agent) | Stripe Dashboard → the payment | Who paid, and that they authorized it |
| Delivery record: the email that delivered the report (date, recipient, subject, attachment name), the report's SHA-256 (`shasum -a 256 <report>`), the `render_report` JSON line, the ledger event id for the audit or scan | Gmail "Sent"; the report file; the seo / orchestrator ledger records | The work was done and delivered |
| Client's acknowledgement: replies such as "thanks, got it", walkthrough call invite, questions about the findings | Gmail; calendar | They received and used it |
| Refund policy as accepted, and the published page | MSA refund section / refund policy; the site's refunds page (screenshot with date) | Terms the client agreed to |
| All communication about the dispute | Gmail | Your attempt to resolve it |

**Honesty rule.** Submit only true records. If an acceptance was recorded by a method that did not really happen,
do not use it as evidence; use the email trail instead and say what it is.

## D. Templates

### D.1 Card dispute: first contact

> **Subject: Your payment to Z Best Media on [date]**
>
> [Name], our payment processor tells us a dispute was filed with your bank for the payment of $[amount] on [date]
> for [service] under our agreement of [date]. If this was a mistake (for example, you did not recognize the charge
> as ours), please ask your bank to withdraw the dispute and reply to let me know. If something about the work is not
> right, please tell me what it is: our agreement gives you a remedy (re-performance or refund of the affected part),
> and I would rather fix it with you directly. Andre

### D.2 ACH return

> **Subject: Your bank returned the payment for invoice [number]**
>
> [Name], your bank returned the ACH payment of $[amount] for invoice [number] (reason given: [reason]). The invoice
> is still open. When you are ready, reply and I will send a new secure payment page. [If work is on hold: We have
> paused (work) until the invoice is paid.] Andre

### D.3 Overdue notice

> **Subject: Overdue: invoice [number]**
>
> [Name], invoice [number] for $[amount] (issued [date], due [date]) is unpaid after [the dispute / the returned
> payment]. Under Section [payment terms section] of our Master Services Agreement, please pay by [date]. Until then
> we have paused [work]. If you believe the invoice is wrong, reply with the reason and we will look at it. Andre

### D.4 Refund request acknowledgement (send within 2 business days)

> **Subject: We received your refund request**
>
> [Name], we received your request of [date] for a refund of [invoice / service]. We will reply with our decision by
> [date, within the refund policy's timeline]. [If approved later: the refund of $[amount] will be sent to the
> original payment method by [date]. ACH refunds take up to 3 business days to arrive and show as a credit
> referencing our statement descriptor.] Andre

## E. Refund requests (no dispute yet)

1. **(Within 2 business days)** Acknowledge in writing (template D.4). The MSA and refund policy promise this.
2. Decide under the refund policy and MSA (§4 refunds, §11.3 re-performance or refund). Record the decision.
3. Before refunding: read the client's activation status at Compliance. If `sanctions_grounded` is `true`, **do not
   refund**: follow [ofac-owner-screening.md](ofac-owner-screening.md).
4. **Check that Finance will see the refund.** The Stripe webhook endpoint must be subscribed to `charge.refunded`
   (and `charge.refund.updated`). At the time of writing the deploy runbook's event list does **not** include them,
   so a Dashboard refund does not reach Finance and the books drift. Until the subscription and the matching code
   change exist: before refunding, add those events in Stripe → Workbench → Webhooks → the endpoint → edit events,
   **or** record the refund by hand in your manual register (date, invoice, amount, Stripe refund id) and reconcile
   it later.
5. Refund in the Stripe Dashboard → Payments → the payment → **Refund**. ACH: full refunds only, within 180 days of the
   payment, and they take up to 3 business days and cannot be canceled. Tell the client the money is coming, so they
   do not also open a dispute (a refund does not stop a dispute, and they could be credited twice).
6. After a refund an issued invoice reverts to `issued` in Finance (there is no void for an issued invoice yet). Note
   it in the manual register so the receivable is not chased.
7. Pay it within the policy's timeline (10 business days after it becomes due) and send the confirmation.

## Sources

Checked on 2026-10-30.

- Stripe, "Respond to disputes": https://docs.stripe.com/disputes/responding (window "usually 7 to 21 days", one
  submission only, Accept/Counter, countered fee, up to 3 months, evidence file limits).
- Stripe, "ACH Direct Debit": https://docs.stripe.com/payments/ach-direct-debit (60 calendar days personal / 2
  business days business; disputes final; late-return inquiries; failures after `succeeded` become disputes; refunds
  full only, 180 days, 3 business days; mandates recorded by Checkout).
- Finance booking codes: `services/finance-py/README.md` (Stripe section), `services/finance-py/src/svc_stripe.py`.
