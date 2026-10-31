# ADR 0026: Finance operations: invoice void and credit notes, client email, the cron service, restores

Status: proposed (fix wave B, Oct 31 2026; local branch `feat/finance-ops-2026-10-31`, not pushed)
Deciders: Andre (founder). Inputs: production audit red team reports (ops OPS-C4, OPS-H1, OPS-H3, OPS-H9, OPS-M1;
chaos CH-3, CH-4, CH-10; money RTM-12), MSA v1 sections 4.10-4.12, Refund and Cancellation Policy v1, founder decision
of Oct 30 2026 (SEO audit by card, no surcharge).

## Context

The contracts promise things Finance could not do:

- "If you have not paid yet, ... we either cancel the invoice in full or send you a credit note that reduces it"
  (policy 5.3, MSA 4.12(g)); "we send a corrected invoice or a refund within 10 business days" (policy 8.4). Finance
  could void only drafts. After a refund at Stripe the invoice went back to `issued`: the client "owed it again".
- Receipts and acknowledgements (MSA 4.12(f): within 2 business days) depended on Andre typing emails; the client-mail
  port was a stand-in.
- Hourly and daily jobs (Stripe page sweep, the hold re-check the runbook promised, the reconciliation) had no caller.
- The runbook's webhook list had no `charge.refunded`: a Dashboard refund never reached the books.
- A restore of one volume made the reconcile plan void the evidence of paid invoices, and Stripe payments made after
  the backup were acknowledged and then lost (CH-3); a paid page Finance did not know was answered 2xx silently (CH-4).
- Due dates used the UTC date (CH-10).

## Decisions

1. **Void** (`POST /fin/v1/invoices/{id}/void`, Andre only): an ISSUED invoice on which nothing was ever paid. Its open
   Stripe pages are expired at Stripe first; a page Stripe does not confirm closed refuses, a page already paid on
   refuses. F11v reverses what is still owed: Dr the account F11 credited (4110, or 2110 for a retainer) / Cr 1100.
2. **Credit notes** (`POST /fin/v1/invoices/{id}/credit-notes`, Andre only), partial or full, against an issued or paid
   invoice. F11c: Dr 4110/2110 for the amount; Cr 1100 for the part still owed on paper (A/R open on the invoice), Cr
   2050 Refunds payable for the part already paid. One debit, credits summing to it: always balanced. A credit owing a
   refund opens a `credit_note_refund_due` break due 10 business days later (weekends and US federal bank holidays
   skipped, Federal Reserve schedule: ADR 0027 decision 6, fix wave C). Fully credited: `void` when nothing was paid, `credited` when money was.
   Limits: never more than the total left to credit; a refund never above the client's money ZBM still holds.
   Refused: ZBC deposits, media prepayments (their own cancel flow), drafts, Compliance holds, disputes, returns.
   A refund-owing credit note asks Compliance first; SANCTIONS or no answer refuses (31 CFR 501.603: blocked property is
   reported, not refunded).
3. **Refunds link to credit notes.** The pre-existing F7r (a refund at Stripe) is split: the part pending credit notes
   promised is Dr 2050 (oldest credit note first, each recording the refund's entry); only the rest is Dr 1100 with the
   old "refunded outside Finance" break. A credit note issued after a refund absorbs that refund's A/R and records the
   link. A failed refund on a credited invoice is never booked automatically (break). The **amount due** (total less
   credit notes) is what a Stripe page asks for and what a payment must equal to match.
4. **Client email.** A Resend adapter (`mail.ResendClientMail`), sender billing@zbestmedia.com only (config refuses any
   other `MAIL_FROM`), one recipient: the billing contact Andre records (`PUT /fin/v1/clients/{id}/billing-contact`;
   the ledger keeps only its SHA-256; amended by ADR 0027: the address lives only in a deletable contact vault and the log and ledger keep a `contact_ref`, never a hash of it). Plain text plus simple HTML built from a fixed field list per kind: no images,
   links, remote resources or tracking (Resend's domain-level open and click tracking stay off, runbook); a hold, its
   reason or a sanctions marker can never reach a template. Messages are queued in the same commit as the booking and
   sent by the repeatable `client-mail` job outside every lock: mail never blocks or undoes bookkeeping. Idempotency key
   = the outbox id. Backoff 15, 30, 60, 120, 240, 480 minutes; 7 attempts end about 16 hours after the first, inside
   Resend's 24-hour idempotency window. Permanent failure: `send_failed`, a high break, an `ALERT finance:` log line,
   Andre requeues. OFF by default: on only with both `RESEND_API_KEY` (as a file, through the entrypoint) and
   `MAIL_FROM`.
5. **fin-cron**, a Railway cron service (`deploy/scheduler`, stdlib Python, pinned base, non-root, no volume, no domain,
   restart Never, `17 * * * *`), calls the stripe-sessions and client-mail jobs hourly and the reconciliation daily at
   07:xx Los Angeles time. It has its OWN caller, `ops_cron`, which Finance lets reach exactly those three routes
   (middleware allowlist, like `seo_02`); the broad `scheduler` caller stays undeployed. Compliance's controls stay in
   Compliance's own in-process scheduler. Legal's and seo's jobs are out of scope (open item).
6. **One webhook event list** in code (`stripe_webhooks.WEBHOOK_EVENTS`), adding `charge.refunded`,
   `charge.refund.updated` and `refund.*`; the runbook, the README and `devtools/stripe_webhook_events.py` show it and a
   test fails if they differ or if an event is one Finance ignores.
7. **SEO audit by card.** New line code `seo_audit_services`, card-eligible like Revenue Recovery (cap $5,000, no
   surcharge, USD only, Adaptive Pricing off); the IaC sets `FIN_CARD_PREPAYMENTS=1`. `strategy_services` stays ACH or
   wire.
8. **Restores (CH-3).** (a) `POST /fin/v1/backup/quiesce|resume` pause Finance's writes (503) and record a snapshot
   marker; the documented order is departments first, ledger last, so a restored set is reconcilable, never FATAL.
   (b) `POST /fin/v1/stripe/reimport` (Andre; a dry run first, the real run names its `plan_sha256` and is refused if
   anything changed) lists Stripe's payments, refunds and disputes for a period (read only, at most 92 days) and books
   the missing ones through the webhook code paths, re-linking a lost Checkout page from Stripe's own session (same
   invoice id, USD, same mode, complete). (c) The reconcile plan lists `money_at_risk`; reconciling it needs
   `money_events_acknowledged: true` (409 `RESTORE_GUARD` otherwise) and opens a `restore_reimport_required` break
   closed only by a successful re-import. Runbook: docs/runbooks/restore-and-reimport.md.
9. **CH-4:** a paid (or processing) Checkout page Finance does not know is still answered 2xx (Stripe's record stands;
   a 5xx loop would not bring the page back), but opens a `stripe_unknown_session` break and logs an ALERT line.
10. **CH-10:** due dates count from the date in `FIN_BUSINESS_TZ` (default America/Los_Angeles; an unknown zone refuses
    start), the same day the journal uses.

## Consequences

- New ledger event types: `invoice_voided`, `credit_note_issued`, `credit_note_refund_linked`, `client_contact_recorded`,
  `client_mail_*`, `backup_marker_*`, `stripe_reimport_*`, `stripe_checkout_relinked`. New flows F11c, F11v.
- New collections in Finance's log: `credit_notes`, `client_contacts`, `mail_outbox`, `backup_markers`,
  `stripe_imports`. Older logs replay unchanged (the collections start empty).
- The client's email address is stored in Finance's volume (and therefore its backups). Deleting a client's data must
  include it (done in ADR 0027: the contact vault and the offboarding route).
- Existing reconcile callers that void money evidence must now acknowledge it.
- Not done here: sales-tax lines on invoices (OPS-M2); a ZBM "apply an unapplied Stripe receipt" route (a re-imported
  payment whose page cannot be re-linked stays unapplied cash with a break); legal/seo cron entries; a log-based alert
  sink (done in ADR 0027: founder alert email); US bank holidays in the 10-business-day count (done in ADR 0027).

## Verification

Finance tests: test_credit_notes.py, test_client_mail.py, test_seo_audit_card_and_webhooks.py,
test_restore_reimport.py (the red team's restore scenario, end to end with the simulated Stripe),
test_ops_cron_caller.py, test_business_tz.py; devtools/test_deploy_config.py (FinCron). Nothing has run on Railway or
against the real Stripe or Resend APIs.
