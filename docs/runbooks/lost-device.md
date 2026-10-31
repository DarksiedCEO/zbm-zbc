# Lost or stolen laptop or phone

Use this when a laptop or phone that has ever been signed in to a ZBM account is lost, stolen, seized, or was left
unlocked where someone else could use it.

**Why it matters.** Following `docs/deploy/railway.md`, your laptop can hold: header and token files
(`~/.zbm-cmp/*.hdr`, `~/.zbm-seo/*.token`), `~/zbm/ledger-callers*.json`, OFAC evidence files, client reports
(`~/client-reports`), a Railway CLI session and a registered SSH key (which gives `railway ssh` into finance, where
every variable is readable), plus browser sessions for Stripe, GitHub, Google, Vercel, Neon and Resend. Your phone
holds your email, two-factor codes and possibly the Stripe and GitHub apps.

**Time limit for the whole runbook: 2 hours.** Steps 1-6 are the urgent part: do them within the first 30 minutes.

Open an incident log first ([incident-response.md](incident-response.md#incident-log-template)). Write: what was lost,
where, when you last had it, whether it was locked, whether the disk is encrypted.

---

## Decision point: how exposed is it?

| Situation | Treat as |
|---|---|
| Powered off or locked, full-disk encryption on (FileVault on a Mac), strong password, and you are sure | SEV-2. Do every step, but the token rotation in step 10 can wait until tomorrow. |
| Unlocked, asleep without a password, or encryption off / unknown | SEV-1. Do every step now, including step 10. Start the notification clocks in [incident-response.md](incident-response.md#6-notification-clocks) if client reports, store credentials or lead exports were on it. |
| Taken in a targeted theft, or someone is already using your accounts | SEV-1, and go to [incident-response.md](incident-response.md) section 4 straight after step 6. |

You cannot check a lost Mac's FileVault state remotely. If your own records (the recovery key saved in your password
manager when you turned it on) do not show it was on, assume it was **off**.

---

## Steps

1. **(5 min) Lock and locate the device.** From another device, sign in to the device's "find my" service:
   - Apple: https://www.icloud.com/find → select the device → **Mark As Lost** (locks it and shows a message).
   - Android: https://www.google.com/android/find → **Secure device**.
   Do **not** erase yet if you hope to recover it; erasing ends location tracking. Decide in step 7.

2. **(5 min) Google Workspace (your email is the reset channel for everything else).**
   1. From a trusted device, change your Google password.
   2. Admin console → Menu → **Directory** → **Users** → your user → **Security** → **Sign-in cookies** → **Reset**.
   3. myaccount.google.com → **Security** → **Your devices** → the lost device → **Sign out**.
   4. If the phone was a 2-Step Verification device (Google prompt or authenticator), remove it there and add a new
      method. Make sure you can still sign in (backup codes or a hardware key).
   5. If the device is enrolled in Google endpoint management, you can remotely wipe Workspace data from it (Admin
      console → **Devices**). Google notes a device wipe does not by itself cut the user's access from other devices,
      which is why steps 2.1-2.3 come first.

3. **(5 min) GitHub.** Settings → **Sessions**: revoke the lost device's web session and its **GitHub Mobile**
   session (revoking the mobile session also removes it as a second factor). Settings → **SSH and GPG keys**: delete
   the key that lived on the lost laptop. Settings → **Developer settings** → **Personal access tokens**: delete any
   token stored on it (the `gh` CLI keeps one; delete it). Change your password if the device was unlocked.

4. **(5 min) Railway.** Change your password. https://railway.com/account/tokens: delete any token that was on the
   device. Remove the laptop's SSH key from your Railway SSH keys (`railway ssh keys` from a trusted machine lists
   them; UNVERIFIED: the exact remove subcommand, see `railway ssh keys --help`). If you cannot confirm the CLI
   session on the lost laptop is dead, ask Railway support (https://station.railway.com) to end all sessions.

5. **(5 min) Stripe.** Change your password from a trusted device; review Settings → Team (no unknown members). If
   the phone had the Stripe app, sign out of it from the account's security settings if offered, or by changing the
   password. UNVERIFIED: whether Stripe lists active sessions for self-service sign-out. If a live API key was ever
   saved in a file on the device, rotate it now (incident-response 4.2).

6. **(5 min) Vercel, Neon, Resend, Namecheap.** Change each password (or the Google/GitHub sign-in they use, already
   done). Vercel: delete any token the CLI stored (`vercel tokens ls`, `vercel tokens rm <id>` from a trusted machine).
   Resend: delete any API key that was saved on the device.

7. **(10 min) Decide on remote erase.**
   - Recovery is unlikely (stolen, left on transit, more than 24 hours gone), or it was unlocked: **erase now**
     (Apple: Find → **Erase This Device**; Android: **Erase device**).
   - Police have a lead or you can see it at a known place: keep it locked and tracked for up to 24 hours, then erase.
   Write the decision and time in the log.

8. **(10 min) Apple ID / Google account for the phone.** If the phone held your Apple ID, change its password and
   remove the device from the account after erasing. Remove the phone as a trusted device for any app's 2FA.

9. **(10 min) AI providers.** For each account that was signed in on the device:
   - Anthropic (claude.ai, Claude Code, console.anthropic.com): sign out of other sessions from account settings
     (UNVERIFIED: exact menu name) and delete any API key that was stored on the device (console → API keys).
   - OpenAI (chatgpt.com, platform.openai.com): sign out of all devices from settings (UNVERIFIED: exact menu name)
     and delete stored API keys (platform → API keys).
   - Perplexity: change the password; revoke and recreate `SEO_PERPLEXITY_API_KEY` if it was stored on the device.
   Client data may sit in these accounts' chat histories. If any did, note it for the client assessment in step 12.

10. **(45 min) Rotate the tokens the laptop held.** Anything in a file on the laptop is in the thief's hands if the
    disk was readable.
    - **Always:** every token you held as a header file or in your shell: the Andre approval and onboarding tokens
      (incident-response 4.9). If `~/.zbm-cmp`, `~/.zbm-seo` or other token files might have existed on the laptop
      at the time, rotate all of them; you cannot prove they were deleted.
    - `~/zbm/ledger-callers*.json` holds **hashes only**, not tokens: no rotation needed for that file alone.
    - A `.env.local` from `vercel env pull`: rotate everything in it (`POSTGRES_URL`, `RESEND_API_KEY`,
      `CRON_SECRET`, `LEAD_IP_HASH_SECRET`; incident-response 4.4, 4.5, 4.13).
    - Search Console service-account JSON files, Shopify credentials, Stripe keys: rotate if they were ever saved
      on the device.
    - With a working Railway SSH session the thief could read every service variable: if the laptop was unlocked
      or unencrypted, rotate **every** secret (incident-response 4.1 step 7).

11. **(10 min) Check audit logs for use after the loss time.**
    - Google: Admin console → Reporting → audit and investigation (sign-ins); myaccount.google.com → Security →
      recent security activity.
    - GitHub: Settings → Security log (90 days).
    - Stripe: API keys → ⋯ → **View request logs**; Dashboard → recent activity.
    - Railway: project → Activity and each service's Deployments.
    - Vercel: project → Deployments; Settings → Environment Variables.
    - Resend: Logs and Emails (30 days).
    Anything you did not do after the loss time: go to [incident-response.md](incident-response.md) and treat it as
    SEV-1.

12. **(10 min) Assess client data on the device.** List what was on it: client reports, OFAC evidence files, exported
    leads, Gmail offline copies. If the disk may have been readable, this is a possible Security Incident for each
    client whose data was there: the MSA's 72-hour clock runs from now (incident-response section 6).

13. **(10 min) Report and record.** Report a theft to the police and keep the report number (insurers and some
    providers ask for it). Note the device's serial number in the log. Close the incident when steps 1-12 are done.

---

## Before it happens (do this once, 30 minutes)

- Turn on FileVault (Mac: System Settings → Privacy & Security → FileVault) or the equivalent, and record the
  recovery key in your password manager.
- Turn on "Find My" / "Find My Device" on every device.
- Use a strong device password and a short auto-lock.
- Do not keep token files on disk longer than a working session: the deploy runbook already says to delete
  `~/.zbm-cmp/*.hdr` and `~/.zbm-seo/*.token` when you are done for the day.
- Keep client reports out of `~/Downloads`; keep them in one folder you can list and delete
  ([client-offboarding.md](client-offboarding.md)).
- Make sure you can sign in to Google, GitHub, Stripe and Railway **without** the phone: backup codes or a hardware
  key, stored apart from the phone.
