# Incident response

Use this the moment you think something *might* be a security incident. You do not need to be sure. Starting this
runbook costs ten minutes; starting it a day late can cost a client, a Shopify app, or a legal deadline.

**A "Security Incident"** (MSA §8.11) is unauthorized access to, or acquisition, disclosure or loss of, Client Data or
Access Credentials held by ZBM. Treat these as incidents too: any leaked ZBM secret (key, token, password), any account
takeover, and any exposure of lead data (names, emails, messages people sent through the site).

Contents: [Detection signals](#1-detection-signals) · [Severity](#2-severity-levels) ·
[First 60 minutes](#3-the-first-60-minutes) · [Containment per asset](#4-containment-per-asset) ·
[Evidence](#5-evidence-preservation) · [Notification clocks](#6-notification-clocks) ·
[Templates](#7-notification-templates) · [After the incident](#8-post-incident-review) ·
[Incident log template](#incident-log-template) · [Sources](#sources)

---

## 1. Detection signals

There is no automated intrusion detection yet (the Cybersecurity department is not deployed, and Legal's push
notifier is not wired). Discovery depends on you noticing one of these. Each one means: **open this runbook now.**

| Signal | Where you see it |
|---|---|
| A Stripe email about a new API key, a key used from an unknown place, an access-policy block, a payout destination change, a refund you did not make | Gmail; Stripe Dashboard → Workbench → request logs |
| A GitHub email: new sign-in, new SSH key, new personal access token, new OAuth app, a push to `main` you did not make | Gmail; GitHub → Settings → Security log |
| A Google "Security alert" / "New sign-in" email, or a sign-in you do not recognize | Gmail; myaccount.google.com → Security |
| A Railway email or dashboard entry: a deploy you did not start, a variable you did not change, a new member, a new token | Railway dashboard → project → Activity / Deployments |
| A Vercel deploy or env change you did not make | Vercel dashboard → project → Deployments / Settings → Environment Variables |
| Resend: emails you did not send, a bounce or spam spike, a "sending paused" notice | Resend → Emails, Logs, Metrics |
| A secret shows up in a repo, a screenshot, a chat, a pasted log or a CI log | Anywhere. Both repos are **public**: a committed secret is exposed the moment it is pushed. |
| The compliance heartbeat monitor alerts, or finance `/health` shows `compliance_holds_open` > 0 that you cannot explain | Your uptime service; the daily tunnel check (`docs/deploy/railway.md`, Compliance step E) |
| A client, Shopify, Stripe, a researcher or a stranger tells you something is wrong | Email, phone |
| Your laptop or phone is lost or stolen | Go to [lost-device.md](lost-device.md) first, then come back here |

**Write down the discovery time now** (date, time, time zone). Every legal clock below runs from it.

---

## 2. Severity levels

Pick the highest one that fits. When unsure, pick the higher one; you can lower it later and write down why.

| Level | Meaning | Examples | Response |
|---|---|---|---|
| **SEV-1** | Client Data, Access Credentials, lead data or money is exposed or being misused **now**, or an attacker may control a production account. | Railway account or token leaked; Stripe secret/restricted key leaked; GitHub account taken over; Neon URL leaked; a client's Shopify credential or report leaked; unexplained refunds or payout changes. | Drop everything. Do sections 3 and 4 now. Notification clocks almost certainly start. |
| **SEV-2** | A secret or account is exposed, but you have strong reason to think no client or lead data was reached, or the exposure is already contained. | `whsec` leaked (Finance reads every event back from Stripe, so a forged webhook books nothing); a Resend key leaked with no unexpected sends; an Andre approval token on a stolen-but-encrypted laptop. | Contain within 1 hour. Assess data access within 24 hours. Decide on notifications in writing. |
| **SEV-3** | No exposure, but a control failed or a near miss happened. | A secret committed to a local branch that was never pushed; a heartbeat alert caused by an outage; a suspicious email you did not click. | Fix within 1 week. Log it. |

---

## 3. The first 60 minutes

Set a timer for each step. Write every action in the [incident log](#incident-log-template).

1. **(2 min) Start the log.** Copy the template at the end of this file into a new local file outside any repo, for
   example `~/zbm/incidents/<YYYY-MM-DD>-<short-name>.md` (`mkdir -p ~/zbm/incidents && chmod 700 ~/zbm/incidents`).
   Write: discovery time, who/what told you, what you saw, your first severity guess.
2. **(3 min) Do not destroy evidence.** Do not delete the leaked commit, the strange deploy, the odd email or the
   unknown key yet. Take a screenshot of each first (section 5). The one exception: a secret visible on a public
   page that you control may be taken down *after* the screenshot, because every minute it stays up is more exposure.
3. **(5 min) Decide what is exposed.** Use the asset list in section 4. For each asset ask: could someone else have
   seen or used it? If yes, it is in scope.
4. **(30 min) Contain.** For each asset in scope, run its containment steps in section 4, **highest blast radius
   first**: Railway → GitHub → Stripe → Google Workspace → Neon → Vercel → Resend → service tokens. A rotated key that
   an attacker can read again from a still-compromised account is wasted, so secure the *accounts* that hold the
   secrets before rotating the secrets themselves.
5. **(5 min) Stop the data flow if needed.** If client store data may be exposed, pull the Revenue Recovery kill
   switches (section 4.11). If lead data may be exposed, you may pause lead intake by removing `POSTGRES_URL` from
   the Vercel project and redeploying: the form then answers "nothing was saved" (it fails closed).
6. **(10 min) Start the clocks.** Fill in section 6's clock table in the log: which clients, whether Shopify data is
   involved, which states' residents. Put each deadline in your calendar with two reminders.
7. **(5 min) Decide who to tell first.** Shopify (24-hour outer limit) and the client (72-hour outer limit) come
   before anyone else. Use the templates in section 7.

**If you cannot finish containment in 60 minutes:** keep going on containment; it beats everything else except the
Shopify 24-hour notice. Send the Shopify and client notices "with the information then available"; the MSA allows
that and requires you to keep them informed afterwards.

---

## 4. Containment per asset

For every rotation: generate the new value, put it in the password manager entry first, then set it in the
provider, then redeploy, then verify, then revoke the old value. Tokens for ZBM's own services are generated with
`openssl rand -hex 32` on your own machine (`docs/deploy/railway.md`, Secrets). Every service token must differ from
every other.

### 4.1 Railway account, account/workspace/project tokens

Blast radius: **total.** Railway holds every service variable, `railway ssh` reaches every container, and a deploy
can replace any service.

1. Sign in at railway.com. Change your password; confirm two-factor authentication is on.
2. Tokens: open https://railway.com/account/tokens. Delete every account and workspace token you do not recognize or
   no longer use. Project tokens live in the project's settings → Tokens: delete them the same way. (Railway's docs
   list these three token types and where they are created; the docs do not describe the delete button, so look for
   the token's menu on those pages.)
3. SSH keys: `railway ssh keys` lists the keys registered for `railway ssh` (the deploy runbook adds one with
   `railway ssh keys add`). Remove any you do not recognize, from the CLI or from your account settings.
   UNVERIFIED: the exact remove subcommand; `railway ssh keys --help` shows it.
4. CLI sessions on other machines: on each machine you still control, run `railway logout`. For a machine you do
   not control, deleting its token in step 2 and changing your password is the available control; if you are not
   sure every session is gone, contact Railway support through https://station.railway.com and ask them to end all
   sessions on your account. UNVERIFIED: whether Railway offers a self-service "sign out everywhere".
5. Members: project → Settings → Members. Remove anyone you did not invite.
6. Look at what changed: project → Activity, and each service → Deployments and Variables. A deploy you did not
   start: roll back to the last known-good deployment from that service's Deployments list.
7. Then rotate **every** service secret in `docs/deploy/railway.md` → Secrets (anyone with the account could read
   them), using sections 4.2-4.10 below and the `LEDGER_CALLERS` procedure in that runbook.

### 4.2 Stripe secret or restricted key (`FIN_STRIPE_SECRET_KEY`)

Blast radius: whatever the key's permissions allow. ZBM's restricted key should have write on Checkout Sessions and
read on PaymentIntents, Charges, Disputes, Payouts and Balance only (`services/finance-py/README.md`). Check that the
key in use really is that restricted `rk_live_` key; a full `sk_live_` key can move money.

1. Stripe Dashboard → https://dashboard.stripe.com/apikeys (live mode).
2. Find the key → overflow menu (⋯) → **View request logs**. Screenshot any request you did not make (time, IP,
   endpoint). This is your evidence of what was done with it.
3. Overflow menu (⋯) → **Rotate key** → in **Expiration** choose **Now** (a suspected compromise: do not keep the
   old key alive) → **Rotate API key** → copy the new value straight into the password manager.
   Stripe: "If you choose **Now**, the old key is deleted." The 7-day grace period exists only if you choose it;
   do not, for a compromise.
4. Railway → `finance` → Variables → `FIN_STRIPE_SECRET_KEY` → replace the value (Sealed) → redeploy `finance`.
5. Verify: finance starts (`/health` 200 through the tunnel), and the Workbench request log shows calls from the new
   key only.
6. If the leaked key was a full `sk_live_` secret key, or the request log shows refunds, payouts, payout destination
   changes or anything you did not do: **also** contact Stripe support at once (Dashboard → Help) and tell them the
   account may be compromised. Check Settings → Team for unknown users and Settings → Payouts for a changed bank
   account.
7. Recommended afterwards: add an access policy (Dashboard → Access policies → **+ Create policy**) so the key only
   works from Railway's network, and apply it to the key (⋯ → **Manage access policy**). Stripe then blocks and
   notifies you of any request from elsewhere.

Card data: ZBM never receives or stores card numbers. Clients pay on Stripe-hosted Checkout pages. A ZBM-side
incident therefore does not expose card numbers. If Stripe tells you *your Stripe account* was used to access
payment data, follow Stripe's instructions and treat it as SEV-1.

### 4.3 Stripe webhook signing secret (`FIN_STRIPE_WEBHOOK_SECRET`, `whsec_`)

Blast radius: low. Finance does not trust the event body: it reads every payment, dispute and payout back from
Stripe with its own key before booking anything, and deduplicates by event id. A forged event therefore books
nothing. Rotate anyway.

1. Stripe Dashboard → Workbench → **Webhooks** → open the ZBM endpoint (the stripe-gateway `/webhooks/stripe` URL).
2. Overflow menu (⋯) → **Roll secret**. Choose to keep the old secret active for a short delay (Stripe allows up to
   24 hours) so no real event is refused while you update Railway. During the delay Stripe signs each event with both
   secrets.
3. Reveal and copy the new `whsec_` value into the password manager.
4. Railway → `finance` → Variables → `FIN_STRIPE_WEBHOOK_SECRET` → replace (Sealed) → redeploy `finance`.
5. Verify: Workbench → Webhooks → the endpoint → **Event deliveries** shows the next event `Delivered` (200).
6. Then expire the old secret early if the Dashboard offers it; otherwise it expires at the delay you chose.

### 4.4 Neon database URL (`POSTGRES_URL` on Vercel)

Blast radius: every lead (names, emails, company names, free-text messages, region). Treat as SEV-1 until you know
whether rows were read.

1. Neon Console → select the project → select the branch that holds the role (usually `production` or `main`) →
   **Roles** → the role's menu → **Reset password** → **Reset** → copy the new password; then **Connect** to copy the
   new connection string into the password manager.
2. The reset applies to new connections at once; connections already open stay open. Restart the compute to close
   them (Neon: "restart the compute").
3. A role exists per branch. If you created other branches (for example a preview branch), reset the role there too.
4. Vercel → `zbm-site-v2` → Settings → Environment Variables → `POSTGRES_URL` → edit → paste → Save. Same on
   `zbc-site-v1` if it uses the same database. Redeploy each project (Deployments → latest → ⋯ → Redeploy).
5. Remove the old URL from anywhere else it lives: local `.env.local` files (the lead-route migration step uses
   `vercel env pull`), CI secrets, scripts.
6. Verify: `GET /api/health` on each site answers `{"ok":true,...}` (behind Vercel SSO until the domain cutover, so
   check from your signed-in browser).
7. Assess access: the database is the only source of truth for which leads exist. Export the list of lead ids and
   created dates that existed at discovery time (they are the people you may need to notify, section 6).

### 4.5 Resend API key (`RESEND_API_KEY` on Vercel)

Blast radius: someone can send email as zbestmedia.com (phishing your clients and leads) and read the last 30 days
of send activity.

1. Resend → **API Keys** → find the key (the "last used" column helps) → options menu → **Remove API key**. Resend:
   deletion revokes the key immediately; treat the old key as compromised.
2. Create a new key with **Sending access** only (not Full access) → copy into the password manager.
3. Vercel → each site project → `RESEND_API_KEY` → replace → redeploy.
4. Review what was sent: Resend → **Logs** (filter by the old key and the exposure window) and **Emails**
   (recipients, subjects). Resend keeps 30 days of activity. Screenshot anything you did not send.
5. If phishing went out in your name: this is SEV-1 for reputation. Email the affected recipients from Gmail (template
   7.5), and watch Resend → Metrics for bounce and spam rates for several weeks.

### 4.6 GitHub account or personal access token

Blast radius: high. Both repos are public, and `zbm-zbc` `main` deploys straight to Railway. Until branch protection
is on, anyone who can push to `main` can change the money path.

1. Change your GitHub password. Confirm two-factor authentication is on (a passkey or hardware key is strongest).
2. GitHub → profile picture → **Settings** → **Sessions** (under "Access") → **Revoke session** on every web session
   you do not recognize, and **Revoke** on any unknown GitHub Mobile device.
3. Settings → **Developer settings** → **Personal access tokens** (both "Fine-grained tokens" and "Tokens
   (classic)") → delete every token you do not need right now. Delete all of them if unsure; recreate later.
4. Settings → **SSH and GPG keys** → delete unknown keys.
5. Settings → **Applications** → **Authorized OAuth Apps** and **Authorized GitHub Apps** → revoke anything you do not
   recognize. Revoking an OAuth app also revokes its tokens.
6. Settings → **Security log** (under "Archives"; it covers the last 90 days) → filter for `personal_access_token`,
   `oauth_authorization`, `oauth_access`, `public_key`, `two_factor_authentication`, `passkey`. Screenshot what you
   did not do.
7. Each repo → **Commits** on `main` (and the open PR branches): look for commits you did not make. In `zbm-zbc`,
   check `.github/workflows/` and `.railway/` changes especially. A malicious commit on `main` has already been
   deployed: roll back each Railway service to the last good deployment (4.1 step 6), then revert the commit through a
   pull request.
8. Rotate every secret that was ever in a GitHub Actions secret for these repos (repo → Settings → Secrets and
   variables → Actions).

### 4.7 Vercel account or token

1. Change your Vercel password / sign-in method; confirm two-factor authentication.
2. Tokens: Vercel dashboard → Account Settings → **Tokens** (https://vercel.com/account/tokens), or from a trusted
   machine: `vercel tokens ls`, then `vercel tokens rm <token id>` for each token you do not need.
3. Each project → Settings → Environment Variables: compare with what you set. Rotate every value (Neon 4.4, Resend
   4.5, `CRON_SECRET`, `LEAD_IP_HASH_SECRET`, `ORCHESTRATOR_SERVICE_TOKEN` 4.10, `DASHBOARD_SESSION_SECRET`,
   `DASHBOARD_PASSWORD_HASH`).
4. Each project → Deployments: a deployment you did not make → promote the last good one ("Instant Rollback").
5. Team → Members: remove anyone you did not invite.
6. Do not touch the `zbestmedia-ui` project unless it is itself involved.

### 4.8 Google Workspace (zbestmedia.com email, Drive, Calendar)

Blast radius: high. Password resets for every other provider arrive here, and client emails, reports and receipts
live here.

1. Change the password. Confirm 2-Step Verification is on and look at the recovery phone and email: an attacker
   often adds their own.
2. Google Admin console → Menu → **Directory** → **Users** → your user → **Security** → **Sign-in cookies** →
   **Reset**. Google notes this does not sign the account out of desktop apps such as Gmail or Drive for desktop;
   sign those out on each machine you control.
3. myaccount.google.com → **Security** → **Your devices**: sign out every device you do not recognize.
   → **Third-party apps with account access**: remove anything unknown.
4. Gmail → Settings → **Forwarding and POP/IMAP** and **Filters and Blocked Addresses**: delete any forwarding
   address or filter you did not create (attackers use them to keep reading your mail).
5. Admin console → **Reporting** → audit and investigation → Gmail log events and user log events: screenshot
   unknown sign-ins.
6. Because resets for Stripe, Railway, GitHub, Vercel, Neon and Resend go through this mailbox, treat all of them as
   possibly exposed and run 4.1-4.7.

### 4.9 Andre approval and onboarding tokens

These are the tokens only you hold. Each one is set on one service and kept in your password manager.

| Token | Set on | Also used as |
|---|---|---|
| `FIN_ANDRE_APPROVAL_TOKEN` | finance | your `X-Andre-Approval-Token` for Finance |
| `FIN_ONBOARDING_CALLER_TOKEN` | finance | your `X-FIN-Caller-Token` (onboarding caller) |
| `LEGAL_ANDRE_APPROVAL_TOKEN` | legal | Legal approvals |
| `LEGAL_ONBOARDING_CALLER_TOKEN` | legal | your `X-LEGAL-Caller-Token` |
| `SEO_ANDRE_APPROVAL_TOKEN` | seo | SEO approvals |
| the `dashboard` entry in `SEO_CALLER_TOKENS` | seo | the caller your approvals reach seo with |
| `COMPLIANCE_ANDRE_APPROVAL_TOKEN` | compliance | Compliance approvals and OFAC attestations |
| `COMPLIANCE_ONBOARDING_CALLER_TOKEN` | compliance | client activation |

For each token in scope:

1. `openssl rand -hex 32` → save as a new password-manager entry (keep the old entry until step 4 passes).
2. Railway → the service → Variables → replace the value (Sealed). For `SEO_CALLER_TOKENS` replace only the
   `dashboard` value inside the JSON, leaving it valid JSON.
3. Redeploy that service.
4. Verify with your new header file through the tunnel: a read that needs the token answers 200, and the same call
   with the old token answers 401.
5. Delete the old password-manager entry and any `~/.zbm-cmp/*.hdr`, `~/.zbm-seo/*.token` or other header files that
   held the old value (`rm -P` on macOS overwrites before deleting; on an encrypted disk plain `rm` is fine).

### 4.10 Service-to-service tokens

These connect the services to each other: `ZBM_SERVICE_TOKEN`, `ORCHESTRATOR_SERVICE_TOKEN` (also pasted in Vercel
for the dashboard), each service's `LEDGER_SERVICE_TOKEN` and `DETECTION_LEDGER_TOKEN`, `FIN_SERVICE_TOKEN`,
`STRIPE_GATEWAY_CALLER_TOKEN`, `SEO_FINANCE_CALLER_TOKEN`, `FIN_LEGAL_CALLER_TOKEN`, `LEGAL_SERVICE_TOKEN`,
`SEO_SERVICE_TOKEN`, `SEO_LOG_HASH_KEY`, the compliance caller tokens.

1. Rotate them in the order and with the exact steps of `docs/deploy/railway.md` → Secrets. Most are referenced
   (`${{service.VAR}}`) by the services that call them, so changing the owner's value and redeploying both sides is
   enough. `ORCHESTRATOR_SERVICE_TOKEN` must also be pasted into Vercel (Vercel cannot reference Railway).
2. **Ledger tokens:** follow "Changing `LEDGER_CALLERS` (adding a caller, rotating one token)" in that runbook
   exactly. Copy the deployed value first; never rebuild it from memory. A wrong value either stops the ledger or
   silently cuts one department off.
3. After all redeploys: run the deploy runbook's smoke-test checklist.

### 4.11 Client store data and Shopify credentials (Revenue Recovery)

1. **Stop all store reads now, without a restart:** `railway ssh` into `detection`, then `touch /tmp/rr-store.KILL`
   (kill switch 2: every sync stops and held snapshots are not served). This file disappears on a redeploy.
2. **Make the stop survive restarts:** Railway → `detection` → Variables → `RR_STORE_READS_DISABLED=1` → redeploy
   (kill switch 1).
3. Delete the affected store's secrets: unset `RR_STORE_SECRET_1` / `RR_STORE_SECRET_2` (and its entry in
   `RR_STORE_CONNECTIONS`) on `detection`, redeploy.
4. Ask the client (template 7.1) to rotate the app's client secret or uninstall the app in their Shopify admin. You
   cannot do this for them; the access is theirs to revoke.
5. Shopify's Dev Dashboard keeps 30 days of Admin API request logs per app (shop, client IP, user agent). If the app
   is in ZBM's own organization, export them now (section 5).

### 4.12 Search Console service account (`SEO_GSC_SERVICE_ACCOUNT`)

1. Google Cloud → IAM & Admin → Service accounts → the account → **Keys** → delete the leaked key; **Add key** → JSON
   for a new one. The deploy runbook's "Connecting Search Console" section has the exact steps for setting it.
2. Railway → `seo` → replace `SEO_GSC_SERVICE_ACCOUNT` (Sealed) → redeploy.
3. Delete the old JSON file from your laptop.

### 4.13 Other credentials

- `SEO_PERPLEXITY_API_KEY`: Perplexity → API settings → revoke, create, replace on `seo`, redeploy.
- `CRON_SECRET`, `LEAD_IP_HASH_SECRET`, `DASHBOARD_SESSION_SECRET`: new random value in Vercel, redeploy. A new
  `DASHBOARD_SESSION_SECRET` signs everyone out of the dashboard, which is what you want.
- `HEARTBEAT_URL` (the compliance heartbeat ping URL): regenerate it at the uptime service, replace it on the
  heartbeat service.
- Namecheap (domain registrar): change the password, confirm 2FA, check that the domain lock is on and DNS records
  are unchanged. A hijacked registrar account can take email, the site and Resend sending all at once.
- Anthropic, OpenAI, Perplexity accounts: see [lost-device.md](lost-device.md), step 9.

---

## 5. Evidence preservation

Collect before you clean up; most provider logs roll off on their own.

| Source | How to save it | Keeps it for (provider) |
|---|---|---|
| Stripe API request logs | Dashboard → API keys → key ⋯ → **View request logs** → screenshot / export | see Stripe Workbench |
| Stripe events | `stripe events list --limit 100 > ~/zbm/incidents/<name>/stripe-events.json` (Stripe CLI, logged in to live mode, read-only) | 30 days for API retrieval |
| GitHub security log | Settings → Security log → export (JSON/CSV) | 90 days |
| Railway service logs | `railway logs --service <name> > ~/zbm/incidents/<name>/<service>.log` for each service, and screenshots of Activity and Deployments | UNVERIFIED (check your plan) |
| Vercel logs | Project → Logs → filter the window → screenshot or export | short on Hobby; UNVERIFIED |
| Resend logs and emails | Logs, Emails pages → screenshot / export | 30 days |
| Google Workspace audit logs | Admin console → Reporting → audit and investigation → export | per Workspace edition |
| Shopify Admin API logs (an app in ZBM's org) | Dev Dashboard → the app → logs | 30 days |
| The leaked artifact itself | Screenshot with the URL and time visible; for a git commit, note the repo, branch and SHA | — |
| Your own actions | The incident log | keep 7 years (with tax records) |

Rules:

1. Save everything under `~/zbm/incidents/<YYYY-MM-DD>-<name>/` (a folder only you can read: `chmod 700`). Never in a
   repo, never in a shared drive.
2. After saving, record a fingerprint of each file in the log: `shasum -a 256 <file> >> ~/zbm/incidents/<name>/SHA256SUMS`.
3. Do not edit evidence files. Write notes in the log, not in the evidence.
4. If you think a crime happened (fraud, extortion, account takeover used for theft), you may report it at
   https://www.ic3.gov (FBI Internet Crime Complaint Center). Keep the evidence folder intact for them.

---

## 6. Notification clocks

**Discovery** = when you first had reason to believe a Security Incident happened, not when you finished
investigating. Write it in the log and compute every deadline from it.

### 6.1 The table to fill in

| Who | Trigger | Deadline | Source | Done (time, how) |
|---|---|---|---|---|
| Each affected **client** | Any Security Incident involving that client's Client Data or Access Credentials | **Immediately**, and in any event **within 72 hours after discovery**, with the information then available; then keep them informed | MSA §8.11 | |
| **Shopify** | Actual **or suspected** breach or compromise of Merchant Data (data obtained through Shopify's API) | **Immediately, and no later than 24 hours** after becoming aware | Shopify API License and Terms of Use §6.2.10 | |
| **California residents** (when ZBM *owns* the data, for example leads) | Breach of unencrypted "personal information" as defined in Civ. Code §1798.82(h) | As fast as possible and **no later than 30 calendar days** after discovery or notification; delay allowed only for law enforcement's legitimate needs or as necessary to determine scope and restore the system | Civ. Code §1798.82(a)(2), as amended by SB 446 (Stats. 2025, ch. 319), in force Jan 1 2026 | |
| **California Attorney General** | You notified **more than 500** California residents for one breach | Submit one sample copy of the notice **within 15 calendar days** of notifying them | Civ. Code §1798.82(f) as amended | |
| **The data's owner** (when ZBM only *maintains* the data for someone else, for example a client's data) | Breach of that personal information | **Immediately following discovery** | Civ. Code §1798.82(b) | |
| **Residents of other states** | That state's own law | Many states use "most expedient time possible and without unreasonable delay"; several set a fixed outer limit (for example 30, 45 or 60 days) and an attorney-general threshold | Each state's statute (section 6.3) | |
| **Stripe** | Your Stripe account or keys were misused | As soon as you know (4.2 step 6) | Stripe | |
| **OFAC** | Not a breach clock. Only if the incident involves a blocked person's property | 10 business days (see [ofac-owner-screening.md](ofac-owner-screening.md)) | 31 CFR 501.603, 501.604 | |

### 6.2 Is it "personal information" under Civ. Code §1798.82?

The statute's definition (subdivision (h)) is narrower than "anything about a person". In summary (UNVERIFIED in
this pass: the list below was not re-read against the current text), it covers a
person's first name or initial and last name **combined with** an unencrypted data element such as a Social Security
number, a driver's license or state ID number, a financial account or card number with the code needed to use it,
medical or health insurance information, biometric data, tax identification or passport numbers, and a few others;
**or** a username or email address together with a password or security question and answer that permits access to
an online account. Read subdivision (h) in the current text before deciding; this summary is not the statute.

What ZBM holds, by design:

| Data | Likely §1798.82 "personal information"? |
|---|---|
| Leads (name, email, company, message) | Usually **no** on its own: no listed data element. It can become yes if a person typed one into the message box. Check the actual rows. |
| Revenue Recovery store data | The connector refuses customer names, emails, phones and addresses (Level-2 fields), so usually **no**. Shopify's 24-hour clock still applies. |
| Search Console data | Aggregate only, no query text: **no**. |
| Client Access Credentials (Shopify app secrets, Search Console access) | Not a consumer's login, so usually **no** under §1798.82, but **yes** under MSA §8.11 (72 h) and, for Shopify, §6.2.10 (24 h). |
| Stripe card data | ZBM never holds it. |

**Even when no statute applies, MSA §8.11 and the Shopify terms do.** And the privacy policy promises notice "as the
law requires". When in doubt, notify.

### 6.3 Other states

All 50 states, DC, Puerto Rico, Guam and the US Virgin Islands have breach-notification laws. Their definitions,
deadlines and attorney-general thresholds differ. When residents of a state other than California are affected:

1. List the affected people by state (from the lead's region field, or the client's address).
2. For each state, open that state attorney general's data-breach page and read the current statute it links to.
   Record in the log: the deadline, whether the AG (or another agency) must be told, the threshold, and the notice's
   required contents.
3. Meet the strictest deadline across all affected states for everyone, unless that is impossible.

The figures commonly reported for some states (for example a 60-day limit and an AG notice for 250+ residents in
Texas, 30-day limits in Washington, Florida and Colorado) were **not verified against the statutes for this
runbook**. Treat them as UNVERIFIED until you have read the statute itself.

---

## 7. Notification templates

Fill in every `[bracket]`. Send from your own zbestmedia.com mailbox, keep a copy in the incident folder, and log the
time sent. Do not include any secret, and do not speculate: say what you know, what you do not know yet, and when you
will update.

### 7.1 To an affected client (MSA §8.11; within 72 hours)

> **Subject: Security incident notice under our Master Services Agreement, Section 8.11**
>
> [Client contact name],
>
> I am writing to tell you about a security incident at Z Best Media that involves [your data / the access you gave
> us to (system)]. Under Section 8.11 of our agreement I am giving you notice with the information I have now, and I
> will keep you updated.
>
> **What happened.** On [date] at [time, time zone] we discovered [plain description: for example, that an access
> key for our hosting account was exposed]. [What we know about when it started.]
>
> **What was involved.** [Exactly which of your data or credentials: for example, the read-only Shopify app credential
> you created for us on (date); the order data we had read for your store (order references, amounts and discount
> codes; no customer names or contact details).] [What was not involved.]
>
> **What we have done.** [For example: we stopped all reads of your store at (time), deleted the credential from our
> systems, rotated every key in our hosting account, and reviewed the access logs.]
>
> **What we ask you to do.** [For example: please rotate the client secret of the app you created for us, or uninstall
> it, in your Shopify admin: (exact steps). Tell us if you need anything from us for notices you must give.]
>
> **What we do not know yet.** [List.] I will update you by [date and time], and sooner if I learn more.
>
> Reply to this email or call me at [phone] with any question.
>
> Andre [last name]
> [ZBM legal entity name], [address]

### 7.2 To Shopify (API Terms §6.2.10; within 24 hours)

Send it through the channel the API Terms name, https://help.shopify.com/questions/partners (Shopify Support for
partners). Shopify's developer security guide also asks developers to email **security@shopify.com** and to tell
Shopify early, "don't wait for certainty". Use both.

> **Subject: Actual or suspected compromise of Merchant Data: [app name], [ZBM legal entity name]**
>
> We are notifying Shopify under Section 6.2.10 of the Shopify API License and Terms of Use.
>
> - App: [app name / client ID if known, never the secret]; organization: [whose Dev Dashboard organization]
> - Affected store(s): [shop domain(s)]
> - Became aware: [date, time, time zone]
> - What happened, as known now: [description]
> - Merchant Data involved: [fields; the app reads orders only and refuses customer names, emails, phones and addresses]
> - Containment: [store reads stopped at (time); credential deleted; merchant asked to rotate/uninstall at (time)]
> - Contact: Andre [last name], [email], [phone]
>
> We will cooperate with Shopify's inquiries and send updates as we learn more.

### 7.3 To an individual (California form; adapt for other states)

California requires a notice titled **"Notice of Data Breach"**, written in plain language, with these headings in
this order: **What Happened**, **What Information Was Involved**, **What We Are Doing**, **What You Can Do**, **For
More Information** (Civ. Code §1798.82(d); UNVERIFIED in this pass: the headings were not re-read against the
current text, so check it, including the type size and any other required content such as the date or estimated date
range of the breach).

> **Notice of Data Breach**
>
> **What Happened.** On [date] we discovered that [plain description]. [The breach happened on / between (dates), as
> far as we can tell.] [This notice was / was not delayed because of a law enforcement investigation.]
>
> **What Information Was Involved.** [Exactly which of your information: for example, the name and email address you
> sent us through the contact form on (date), and the message you wrote.]
>
> **What We Are Doing.** [Containment and fixes, in plain terms.]
>
> **What You Can Do.** [Specific, useful steps: for example, be careful of emails that look like they come from us;
> we will never ask you for a password or payment details by email.]
>
> **For More Information.** Contact us at privacy@zbestmedia.com or [phone]. [ZBM legal entity name], [address].

If more than 500 California residents receive this notice, submit one sample copy to the California Attorney General
within 15 calendar days (the AG's online data-breach submission form, on oag.ca.gov; UNVERIFIED: the exact page).

### 7.4 Update to a client (every 48 hours until closed, or as agreed)

> **Subject: Update [n]: security incident notice of [date]**
>
> Since my last update on [date]: [what we learned]. [What we changed.] [What is still open.] Next update by [date].

### 7.5 To recipients of phishing sent in ZBM's name

> **Subject: Please ignore an email that appeared to come from Z Best Media on [date]**
>
> On [date] someone sent emails that appeared to come from [address]@zbestmedia.com without our permission. If you
> received one with the subject "[subject]", please do not click its links or reply to it. We have stopped it. We
> never ask for passwords or payment details by email. Questions: [your direct email or phone].

---

## 8. Post-incident review

Within 7 days of closing the incident:

1. Finish the incident log: the timeline, the root cause, what was exposed, every notification sent (who, when, how).
2. Answer in writing:
   - How did it start, and how did you find out? How long between start and discovery?
   - Which step in this runbook was missing, wrong or slow? Fix the runbook in a pull request.
   - Which control would have stopped it or caught it earlier? Add it to the backlog with an owner and a date.
3. Confirm every rotated secret is in the password manager, and every old one is gone from it and from the laptop.
4. Re-run the access review (FC-05, [compliance-calendar.md](compliance-calendar.md)) and record it.
5. Keep the incident folder and log for 7 years with the tax records (and 10 years if it involved an OFAC matter).
6. If a client asks for a written report, send the finished log's summary, with no secrets.

---

## Incident log template

```text
INCIDENT: <short name>                    Severity: SEV-1 / SEV-2 / SEV-3 (changes: time, reason)
Discovered: <YYYY-MM-DD HH:MM TZ>         By / how: <who told you, what you saw>
Deadlines:  Shopify 24 h -> <time>   Client 72 h -> <time>   CA 30 days -> <date>   Other: <...>

Assets in scope: [ ] Railway [ ] GitHub [ ] Stripe key [ ] whsec [ ] Google [ ] Neon [ ] Vercel [ ] Resend
                 [ ] Andre tokens [ ] service tokens [ ] client store creds [ ] GSC key [ ] laptop/phone

Timeline (one line per action):
<HH:MM> <what you did> <result>

Evidence saved (file, sha256):
...

Notifications:
<who> <when sent> <channel> <copy saved at>

Root cause:
Fixes and owners:
Closed: <YYYY-MM-DD HH:MM>
```

---

## Sources

Checked on 2026-10-30.

- MSA §8.11 (Security Incidents) and §15.5: `scratchpad/legal/zbm-msa-v1.md` (contract text, not in this repo).
- California Civ. Code §1798.82 as amended by SB 446 (Stats. 2025, ch. 319, approved Oct 3 2025): 30 calendar days
  (subd. (a)(2)(A)); delay for law enforcement or to determine scope (subd. (a)(2)(B)); AG sample within 15 calendar
  days when more than 500 residents (subd. (f)); maintainer notifies owner "immediately following discovery" (subd.
  (b)). Chaptered text read at https://calmatters.digitaldemocracy.org/bills/ca_202520260sb446; status at
  https://legiscan.com/CA/bill/SB446/2172. The official text is on leginfo.legislature.ca.gov.
- Shopify API License and Terms of Use §6.2.10 (24 hours, via https://help.shopify.com/questions/partners), §6.2.3
  (deletion within 30 days), §7.2: https://www.shopify.com/legal/api-terms. Shopify developer guide "Respond to a
  security incident" (security@shopify.com; Dev Dashboard keeps 30 days of Admin API logs):
  https://shopify.dev/docs/apps/build/security/monitoring-and-response/respond-to-a-security-incident.md
- Stripe API keys (rotate, Expiration "Now", 7-day grace, request logs, access policies): https://docs.stripe.com/keys
- Stripe webhooks (Roll secret, up to 24 hours; live-mode retries up to three days; Dashboard resend 15 days; CLI
  resend 30 days; duplicate events): https://docs.stripe.com/webhooks
- Railway token types and https://railway.com/account/tokens: https://docs.railway.com/integrations/api
- Neon password reset and its effect on open connections: https://neon.com/faqs/rotate-database-password-after-leak
- Resend leaked-key guide (Remove API key, Sending access, 30 days of activity):
  https://resend.com/docs/knowledge-base/how-to-handle-a-leaked-api-key.md
- GitHub sessions and security log:
  https://docs.github.com/en/enterprise-cloud@latest/authentication/keeping-your-account-and-data-secure/viewing-and-managing-your-sessions,
  https://docs.github.com/en/enterprise-cloud@latest/authentication/keeping-your-account-and-data-secure/reviewing-your-security-log
- Vercel tokens CLI: https://vercel.com/docs/cli/tokens
- Google Workspace, reset sign-in cookies:
  https://knowledge.workspace.google.com/admin/devices/sign-a-user-out-of-a-managed-google-account
