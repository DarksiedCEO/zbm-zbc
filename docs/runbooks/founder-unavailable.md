# Founder unavailable

Today every approval, token and account belongs to Andre alone. If Andre is unreachable (illness, travel without
signal, an emergency), some things keep running and some stop. This runbook says which, what a trusted delegate may
and may not do, and what must be set up **in advance** so the delegate can act at all.

Two parts:

- **Part 1** is for Andre, now: set up the delegate and the sealed emergency kit (about 2 hours, once; review every 90
  days with the access review).
- **Part 2** is for the delegate, when Andre has been unreachable for 48 hours (or Andre tells them to start).

---

## What keeps running and what stops

| Keeps running without Andre | Stops or lapses without Andre |
|---|---|
| The websites and the lead form (leads are stored and emailed to Andre's inbox) | Replies to leads, clients, refund requests, privacy requests |
| The daily lead purge cron | New client activations, invoices, Checkout links, audits, reports (all need Andre's tokens) |
| Railway services, webhooks, Finance booking Stripe payments, disputes and payouts | Responses to card disputes (lost by default after the deadline, usually 7-21 days) |
| Compliance controls (every 6 hours) and the heartbeat monitor's alerts | Releasing any held payment |
| Stripe payouts to the bank on its schedule | OFAC 10-business-day reports, if a sanctions hold opens |
| Daily Railway volume backups (kept 6 days) | The 72-hour client and 24-hour Shopify breach notices |
| | 90-day client re-screens: a client's gate closes when its screen expires |
| | US-OFAC-04 re-verification: **on 2026-12-25 every client turns REFUSE** if not re-verified |
| | Domain, license and tax deadlines in [compliance-calendar.md](compliance-calendar.md) |

---

## Part 1: Andre sets this up in advance

### 1.1 Choose one delegate

A person you trust with money and client confidentiality, who can use email and a browser. Write their name in your
password manager entry "ZBM emergency kit" (not in this repo).

### 1.2 What the delegate may and may not do

**May:**

1. Read and reply to the role mailboxes (billing@, privacy@, legalnotice@) using the templates below, through Gmail
   **mail delegation** (Gmail → Settings → Accounts → "Grant access to your account"). The delegate signs in with
   their own Google account; they never get your password.
2. Acknowledge refund, privacy and accessibility requests within their promised times (templates 2.3), saying when a
   full answer will follow.
3. Tell clients that work is paused and when Andre is expected back.
4. Pause lead intake on the website, if Vercel access was granted to them as a team member (2.4).
5. Watch the heartbeat monitor and Stripe dispute emails and, for a card dispute, **accept or counter it** with the
   evidence pack only if you granted them a Stripe team role that allows it (Stripe → Settings → Team → invite with a
   limited role). Otherwise they forward the dispute email to Andre and note the deadline.
6. Contact Stripe, Railway or Google support to report a suspected compromise of Andre's accounts.
7. Open the sealed emergency kit under the conditions in 1.4.

**May not:**

1. Use Andre's own passwords, tokens or two-factor devices, or sign in as Andre. (Shared logins break most providers'
   terms and destroy the audit trail.)
2. Approve anything that needs an Andre approval token: contracts, rules, invoices, payouts, OFAC attestations,
   client activations, hold releases.
3. Refund, move or return money held for sanctions reasons, or touch any OFAC matter beyond telling Stripe support.
4. Change Railway, GitHub, DNS or the domain registrar.
5. Sign anything on behalf of ZBM unless Andre has given them written authority to.
6. Delete any data.

### 1.3 Accounts to set up for the delegate (each with their own login)

| Account | Role to grant | Why |
|---|---|---|
| Google Workspace | Mail delegation on Andre's mailbox, or membership of a billing@/privacy@ group | Answer time-bound requests |
| Stripe | Team member with a limited role (for example "Support specialist" or "View only"; UNVERIFIED: role names on your account) | See disputes; respond if the role allows |
| Vercel | Team member (only if you want them able to pause intake) | Pause the lead form |
| Uptime / heartbeat service | Alert recipient | Know when Compliance or a hold needs attention |
| Railway, GitHub, Neon, Resend | **None** | Engineering access stays with Andre |

### 1.4 The sealed emergency kit

The kit is for a situation where Andre may be gone for good or for a long time, and ZBM must be wound down or handed
over. **No secret is ever written in this repo or in this runbook.**

- **What:** a printed sheet with the password manager's emergency-access instructions (for example, the password
  manager's built-in emergency access feature, which grants access after a waiting period that Andre can refuse),
  plus the list of providers and account emails. No passwords on paper unless the password manager has no emergency
  access feature; in that case, the master password in a sealed envelope.
- **Where:** a location Andre chooses and tells the delegate in person (for example a home safe or a bank safe
  deposit box). Record only the location's *name* in the "ZBM emergency kit" entry.
- **When the delegate may open it:** Andre has been unreachable for 14 days, **or** Andre or his family tells them to.
  The delegate writes down the date and reason before opening it.
- **Review:** every 90 days with the access review; re-seal after any change.

### 1.5 Before any planned absence of more than 2 days

1. Run the re-screen and re-verification dates in [compliance-calendar.md](compliance-calendar.md) that fall inside
   the absence **before** you leave.
2. Set a Gmail vacation responder on the role mailboxes (template 2.2).
3. Respond to any open dispute; release or refund any open hold.
4. Tell active clients the dates.

---

## Part 2: the delegate's steps

Start when Andre has not answered calls, texts and email for **48 hours**, or when Andre or his family asks you to.

1. **(15 min) Write down** the date, time and how you tried to reach Andre. Keep a simple log (date, time, what you
   did) for everything below.
2. **(15 min) Turn on the auto-reply** on the role mailboxes (template 2.2) if it is not on.
3. **(30 min, daily) Read the inboxes.** For each message:
   - **Refund request:** acknowledge within 2 business days (template 2.3a). Do not refund.
   - **Privacy request** (access, deletion, correction): acknowledge within 10 business days (template 2.3b).
   - **Accessibility issue:** acknowledge within 2 business days (template 2.3c).
   - **Legal notice** (legalnotice@): reply only "Received on [date]; Z Best Media will respond." Keep it unread-marked
     for Andre.
   - **Stripe dispute email:** write the evidence-due date on the log's first line. If you have a Stripe role that can
     respond, follow [chargebacks-and-disputes.md](chargebacks-and-disputes.md); if not, accept the risk and keep it
     for Andre.
   - **Anything that sounds like a security problem** (a client or Shopify reporting a leak, a "new sign-in" alert):
     contact Stripe / Google / Railway support from their public help pages and report a possibly compromised account
     owned by an unreachable owner. Tell the affected client what you know (incident-response template 7.1, with
     "Andre is unreachable; I am [name], helping in his absence").
4. **(10 min) Pause intake if the absence will be long** (over a week) and you have Vercel access: Vercel →
   `zbm-site-v2` → Settings → Environment Variables → remove `POSTGRES_URL` (copy its *name* only, never the value,
   into the log) → Redeploy. The form then says nothing was saved. Andre restores it on return.
5. **(Day 14) The sealed kit.** If Andre is still unreachable, you may open the kit (Part 1.4). Its instructions say
   what to do next.

### 2.2 Auto-reply

> Thank you for your message. Z Best Media is temporarily operating with limited staff. We have received your email
> and will reply in full by [date]. For refund, privacy or accessibility requests, this message confirms receipt.

### 2.3 Acknowledgements

**a. Refund request (within 2 business days):**
> We received your refund request of [date] for [invoice/service]. We will reply with a decision by [date]. — [Name],
> for Z Best Media

**b. Privacy request (within 10 business days):**
> We received your privacy request of [date] ([type: access / deletion / correction]). We will respond within 30 days
> of receipt, by [date]. We may ask you to confirm your identity. — [Name], for Z Best Media

**c. Accessibility issue (within 2 business days):**
> Thank you for telling us about [the problem] on [page]. We have recorded it and will reply by [date]. — [Name], for
> Z Best Media

**d. Client: work paused:**
> Andre is unavailable at the moment. Work on [project] is paused, and nothing is lost. We expect to resume by [date],
> and we will contact you then. Nothing is due from you in the meantime unless you have already received an invoice.
> — [Name], for Z Best Media

### 2.4 When Andre returns

Andre reviews the delegate's log, restores anything paused (for example `POSTGRES_URL`), checks every hold and
dispute, and rotates any credential the delegate was given for the emergency.
