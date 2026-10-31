# Client offboarding and data deletion

Use this when an engagement ends (the last SOW ends or is terminated), when a client asks you to delete their data,
or when a Shopify client uninstalls the app or tells you ZBM's access has ended.

**Deadlines.**

| Promise | Deadline | Source |
|---|---|---|
| Delete Client Data, including Client Personal Information, from ZBM's systems and its subprocessors', and confirm in writing on request | **30 days** after the last SOW ends, or earlier at the client's written request | MSA §15.5 |
| Revenue Recovery: delete the Store's credentials and disconnect the Store | **5 business days** after the last read | RR SOW §6.4 |
| Revenue Recovery: delete (or make unidentifiable) all data obtained from the Store, including order references and amounts in finding records and any backup copies | **30 days** after the earliest of: uninstall / access ended, a written deletion request, or the SOW ending | RR SOW §6.3(d); Shopify API Terms §6.2.3 |
| Revenue Recovery: if a finding record cannot be deleted in time, tell the client and **notify Shopify** before the 30 days end | Before day 30 | RR SOW §6.3(e); Shopify API Terms §6.2.3 ("promptly notify Shopify") |
| SEO audit: remove the Search Console link, ask the client to remove ZBM's service account, delete working copies | **30 days** after the SOW ends | SEO SOW §7.3 |

Count from the right day and write the deadline in the calendar **today**, with a reminder at day 20.

**Time: about 1.5 hours per client.**

---

## A. What is deleted and what is kept

| Data | Deleted | Kept (MSA §15.5) | Keep for |
|---|---|---|---|
| Client reports (PDF/HTML) on your laptop, in Gmail attachments, in Drive | Yes | — | — |
| Exports, notes, screenshots with Client Data | Yes | — | — |
| Shopify credentials (`RR_STORE_SECRET_*`, the store's `RR_STORE_CONNECTIONS` entry) | Yes (within 5 business days) | — | — |
| Store snapshots and working data in detection / orchestrator | Yes | — | — |
| Search Console link and service-account access | Yes | — | — |
| SEO audit records' page extracts | Yes, or wait out the retention (below) | — | — |
| Lead row for this client in Neon | Yes, unless still needed for an open matter | — | — |
| Email threads that discuss the work | Delete attachments and Client Data; keep the thread only if it is a tax/accounting record or the acceptance record | (a), (b) | — |
| **Invoices, payment records, receipts, journal entries, Stripe records** | — | Yes, §15.5(a) | **7 years** (ZBM's tax-record policy; the IRS's periods are 3 to 7 years depending on the case, so 7 covers them) |
| **The client's acceptance of the MSA and each SOW** | — | Yes, §15.5(b) | 7 years after the engagement ends |
| **Ledger entries** (identifiers, codes, counts, amounts, timestamps, hashes; no page contents, names or contact details) | Shopify order references and amounts: deleted or made unidentifiable under RR SOW §6.3(d) | Everything else, §15.5(c) | Life of the ledger |
| **OFAC screening log and evidence PDFs** | — | Yes, §15.5(d) (required by law) | **10 years** (31 CFR 501.601) |
| Incident logs that mention the client | — | Yes, §15.5(d) where required | 7 years |

Everything kept stays confidential under MSA §§7-8 for as long as it is kept.

**Backups.** Railway's daily volume backups are kept 6 days and then roll off by themselves; Neon keeps its own
history for a period that depends on the plan (UNVERIFIED); Resend keeps 30 days of email activity. Data deleted from
the live system therefore disappears from these copies on their own schedules, all well inside the 30-day promise
**if you delete from the live systems by day 20.** Check the Neon history window once and write it here: `____ days`.

---

## B. Steps

### B.1 Day 0: start (15 min)

1. Write the trigger and date in the client's file (which SOW ended, or the request email). Compute the deadlines in
   the table above and put them in the calendar.
2. If the client still owes money, keep the invoice open; offboarding does not cancel it.
3. If any Compliance hold is open on this client, **stop** and resolve it first ([ofac-owner-screening.md](ofac-owner-screening.md),
   [chargebacks-and-disputes.md](chargebacks-and-disputes.md)). Never delete records connected to a sanctions hold.

### B.2 Revenue Recovery clients (Shopify): within 5 business days of the last read (20 min)

1. Stop reads for this store: Railway → `detection` → Variables → remove this store's entry from
   `RR_STORE_CONNECTIONS` and unset `RR_STORE_SECRET_1` / `RR_STORE_SECRET_2` for it → redeploy `detection`. (If it was
   the only store, you can also set `RR_STORE_READS_DISABLED=1`.)
2. Ask the client to uninstall the app or revoke the token (template E.1). The access is theirs to revoke; ZBM never
   holds their Shopify login.
3. Delete any copy of the credential from your laptop and password manager.

### B.3 Revenue Recovery: Store data within 30 days (30 min)

1. Delete working copies of store data and the report from your laptop and email (B.5).
2. **Finding records in the ledger** hold the Shopify order reference and amount of each finding. The ledger is
   append-only and has no per-client erase route yet. If you cannot delete or make them unidentifiable by day 30:
   - before day 30, tell the client in writing which finding records you still hold and why (template E.3);
   - before day 30, notify Shopify through Shopify Support for partners (https://help.shopify.com/questions/partners):
     say which shop, which records, why they cannot yet be deleted, and when they will be (template E.4);
   - keep them only in the audit log and its backups, use them for nothing, and make them unidentifiable as soon as
     the system can; then confirm to the client in writing.
   UNVERIFIED: whether any tool exists yet to make ledger finding records unidentifiable. Until one does, the
   notification path above is the honest one. **Hold new Revenue Recovery work for Shopify clients** until it exists.

### B.4 SEO audit clients: within 30 days (15 min)

1. Remove the link between the client's Search Console property and ZBM's system on `seo` (UNVERIFIED: the tenant
   route that removes it; check `services/seo-py/src/api.py` for the tenant's Search Console link and use the Andre
   approval token). If no such route exists, remove the property from the service account's configuration and record
   that in the client file.
2. Ask the client to remove ZBM's service account from the property (template E.2): Search Console → Settings →
   **Users and permissions** → the service account → **Remove access**.
3. SEO audit records are kept by `seo` with a time-based retention (after 90 days the Search Console part is reduced
   to window totals). Page contents and answer text are never stored. Nothing else to delete there.

### B.5 Every client: working copies (20 min)

1. Laptop: delete the client's folder under `~/client-reports/` (and `~/Downloads`, `~/Desktop` copies). Search:

   ```sh
   mdfind -name "<client name>"     # macOS Spotlight; also search the client id and domain
   ```

   Delete what you find, except invoices and the acceptance record. Empty the Trash.
2. Token files left from this client's work: `rm -f ~/.zbm-seo/*.token ~/.zbm-cmp/*.hdr` (they are recreated per
   session).
3. Gmail: search `from:<client domain> OR to:<client domain> has:attachment`. For each message with a report or data
   attachment: delete it (keep invoices, receipts and the acceptance email). Empty Gmail's Trash
   (Trash → **Empty Trash now**).
4. Google Drive: search the client name and domain; delete working files; empty Drive's trash.
5. Resend: nothing to do unless client data was sent through it (it is used for lead notifications only); its
   activity rolls off after 30 days.
6. AI tools: if Client Data was pasted into any AI chat, delete those conversations in that tool.
7. Neon: if the client came in through the lead form and the lead is no longer needed, delete the row:

   ```sql
   -- in the Neon SQL Editor, production branch; look first, then delete by id
   SELECT id, created_at, app, form FROM leads WHERE payload->>'email' ILIKE '<client email>';
   DELETE FROM leads WHERE id = '<id from above>';
   ```

   The form fields are stored in the `payload` JSON column (`packages/lead/sql/001_leads.sql` in `zbest-sites`).
   UNVERIFIED: that the key is exactly `email`; run `SELECT payload FROM leads ORDER BY created_at DESC LIMIT 1;` once
   to see the keys.

### B.6 Confirm (10 min)

1. Fill in the checklist (section C) and save it in the client's file.
2. Send the written confirmation (template E.5). The MSA requires it on request; send it anyway.
3. Put a reminder at day 30 to check nothing came back (for example a restored backup).

---

## C. Offboarding checklist (copy into the client file)

```text
Client: <id / legal name>            Trigger: <SOW end / request / uninstall>   Date: <YYYY-MM-DD>
Deadlines: RR creds 5 bd -> <date>   Data 30 d -> <date>   Reminder day 20 -> <date>
[ ] No open hold, dispute or unpaid invoice that blocks deletion (or noted: <...>)
[ ] RR: store removed from RR_STORE_CONNECTIONS, secrets unset, detection redeployed (<date>)
[ ] RR: client asked to uninstall / revoke (<date>)
[ ] RR: finding records deleted / unidentifiable, OR client + Shopify notified under SOW 6.3(e) (<date>)
[ ] SEO: Search Console link removed; client asked to remove service account (<date>)
[ ] Laptop copies deleted (reports, exports, token files)
[ ] Gmail attachments deleted, trash emptied; Drive cleaned, trash emptied
[ ] AI-tool conversations with Client Data deleted
[ ] Neon lead row deleted (or kept because: <...>)
[ ] Kept: invoices/payments (7 y), acceptance (7 y), ledger entries (no PI), OFAC records (10 y)
[ ] Confirmation sent (<date>)
```

## D. When a client asks for deletion during the engagement

The MSA lets a client ask earlier. Deleting the data usually means the work cannot continue: tell the client that,
in writing, before you delete (template E.6). If they confirm, run B.2-B.6 and end the affected SOW.

## E. Templates

### E.1 Ask the client to revoke Shopify access

> **Subject: Please remove Z Best Media's access to [shop domain]**
>
> [Name], our work under the Revenue Recovery SOW is complete. We have deleted the credentials you gave us and
> stopped all reads of your store. Please also remove our access on your side: in your Shopify admin, [uninstall the
> app "(name)" / revoke the Admin API access token of the custom app "(name)"]. Andre

### E.2 Ask the client to remove the Search Console service account

> **Subject: Please remove Z Best Media's Search Console access**
>
> [Name], our SEO audit work is complete and we have disconnected your Search Console property from our system.
> Please remove our service account ([service account email]) in Search Console → Settings → Users and permissions →
> the account → Remove access. Andre

### E.3 Records not yet deletable (RR SOW §6.3(e)(i))

> **Subject: Data deletion under our Revenue Recovery SOW**
>
> [Name], we have deleted the data we read from [shop domain] except [n] finding records in our tamper-evident audit
> log, each holding a Shopify order reference and an amount. Our system cannot yet delete or anonymize those entries.
> We keep them only in that log and its backups, we use them for nothing, and we will make them unidentifiable as
> soon as our system can, and confirm to you in writing. We have also told Shopify, as its terms require. Andre

### E.4 Shopify notice (cannot delete in time)

> **Subject: Merchant Data deletion: [app name], [shop domain]**
>
> Under Section 6.2.3 of the Shopify API License and Terms of Use: [n] records derived from Merchant Data of
> [shop domain] (order references and amounts in an append-only audit log; no customer data) cannot be deleted within
> 30 days of [trigger, date]. They are retained only in the audit log and its backups, not used for any purpose, and
> will be rendered unidentifiable by [date]. Contact: Andre [last name], [email].

### E.5 Deletion confirmation

> **Subject: Confirmation: your data has been deleted**
>
> [Name], as of [date] Z Best Media has deleted the Client Data it held for [client], under Section 15.5 of our Master
> Services Agreement, including working copies of [reports / store data / Search Console data] and the access
> credentials you gave us. As that section allows, we keep only: invoices and payment records needed for tax and
> accounting; the record of your acceptance of our agreements; entries in our tamper-evident audit records, which hold
> no page contents, customer names or contact details; and records the law requires us to keep, such as sanctions-
> screening records. These remain confidential. [If E.3 applies: The finding records described in our message of
> (date) will be made unidentifiable by (date).] Andre

### E.6 Deletion requested mid-engagement

> **Subject: Your deletion request**
>
> [Name], we received your request of [date] to delete your data. Doing so means we cannot finish [the work under the
> SOW of (date)]. If you confirm, we will delete it within 30 days and end that SOW, and [refund / invoice] [amount]
> under the agreement. Please reply "confirm" to proceed. Andre

## Sources

Checked on 2026-10-30.

- MSA §15.5 (deletion within 30 days; what may be kept), RR SOW §§6.3-6.4, SEO SOW §7.3: `scratchpad/legal/`
  (contract text, not in this repo).
- Shopify API License and Terms of Use §6.2.3 (delete Merchant Data within 30 days of uninstall, no longer needed, or
  an enforceable request; promptly notify Shopify if you cannot): https://www.shopify.com/legal/api-terms
- Railway backup retention (daily 6 days): https://docs.railway.com/volumes/backups.md
- Resend activity retention (30 days): https://resend.com/docs/knowledge-base/how-to-handle-a-leaked-api-key.md
- OFAC recordkeeping 10 years (31 CFR 501.601): https://www.govinfo.gov/content/pkg/FR-2025-03-21/pdf/2025-04864.pdf
- IRS record periods (3 years generally, up to 7 in some cases, longer for some): https://www.irs.gov/businesses/small-businesses-self-employed/how-long-should-i-keep-records
  (UNVERIFIED in this pass: page not fetched; 7 years is ZBM's chosen period).
