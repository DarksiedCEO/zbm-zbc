# Compliance calendar

Every recurring date ZBM must meet. Put each one in your Google Calendar **now**, with two reminders (14 days and 2
days before), and tick it off in this file's log at the end when done. Nothing in the system reminds you of these
yet: Compliance does not send alerts for rules about to expire or for 90-day re-screens.

If a date falls on a weekend or federal holiday, a tax or government deadline usually moves to the next business day;
check the agency's page each time.

Items marked **(entity)** depend on what legal entity sells ZBM's services, which is still an open fact (F1-F5 in the
business register). Fill those in once it is settled.

---

## 1. Fixed dates: the next 12 months

| Date | What | How long | Runbook / source |
|---|---|---|---|
| **2026-12-01** | Railway stops reading `railway.json` / `railway.toml` (Config as Code). ZBM uses `.railway/railway.ts` (Infrastructure as Code), so nothing should change. Check that no service still depends on a JSON/TOML file. | 10 min | `docs/deploy/railway.md` (intro) |
| **2026-12-11** (do it by **2026-12-18**) | **Re-verify US-OFAC-04**, and US-OFAC-01, -02, -03 in the same sitting. **If US-OFAC-04 is not re-verified, on 2026-12-25 every client's activation turns REFUSE**: no invoices, audits or store reads. | 25 min | `docs/deploy/railway.md` → Compliance step H |
| **2026-12-25** | Hard expiry of US-OFAC-01..04 and the other 2026-12-25 rows (US-FTC-D101-01/02, -FAQ-01, -5-01, -437-01, -CANSPAM, -HBNR, US-IRS-*, AGE-01, US-CPPA-2025, PRG-ECCP) that matter once ZBC payouts, email campaigns or claims are in scope. | ~2 h for the non-OFAC rows | same |
| **2027-01-15** | 4th-quarter 2026 estimated tax: federal (Form 1040-ES) and California (540-ES), if you pay estimates. | 30 min | IRS / FTB |
| **2027-02-01** | Forms 1099-NEC to recipients and the IRS for any unincorporated person ZBM paid at or above the threshold in 2026 (Jan 31 falls on a Sunday). Probably none now. | 30 min | business register B5 |
| **2027-03-15** | S-corporation and LLC/partnership returns (federal 1120-S / 1065; CA 100S / 568 / 565) **(entity)** | — | FTB important dates |
| **2027-03-25** | Re-verify US-FTC-465-07 (in every client's rule set). | 10 min | Compliance step H |
| **2027-04-15** | Individual and C-corporation returns (federal 1040 / 1120; CA 540 / 100) **(entity)**; 1st-quarter 2027 estimates (federal and CA). | — | IRS / FTB |
| **2027-06-15** | 2nd-quarter 2027 estimates (federal and CA); CA LLC estimated fee (Form 3536) if an LLC **(entity)**. | 30 min | IRS / FTB |
| **2027-06-30** | OFAC snapshot date: does ZBM hold any **blocked** property today? Write yes/no in the log. | 5 min | [ofac-owner-screening.md](ofac-owner-screening.md) |
| **2027-09-15** | 3rd-quarter 2027 estimates. | 30 min | IRS / FTB |
| **2027-09-30** | **OFAC Annual Report of Blocked Property** (TD F 90-22.50 through ORS), only if the 2027-06-30 answer was yes. Every year. | 30 min | 31 CFR 501.603(b)(2); OFAC FAQ 50 |
| **2027-12-25** | Next re-verification of the OFAC rows (they run 12 months from the 2026 re-verification; check the actual expiry the readiness answer shows). | 25 min | Compliance step H |

Estimated taxes: whether you owe quarterly estimates depends on the entity and on how much tax you expect to owe. You
have said you expect to pay yearly early on; if more than about $1,000 of federal tax ends up owed for the year, an
underpayment penalty can apply. Decide knowingly each quarter.

## 2. Dates you must look up once, then add

| Item | Where to find the date | Then repeat |
|---|---|---|
| **Statement of Information** (California Secretary of State) **(entity)** | https://bizfile.sos.ca.gov → search the entity → it shows the filing period. A corporation files every year in the month of its original filing and the 5 months before it; an LLC every 2 years. | Yearly (corp) / every 2 years (LLC) |
| **California franchise / annual tax** (the $800 minimum) **(entity)** | FTB, for the entity type | Yearly |
| **Long Beach business license** | The City sends a bill each year; paying it renews the license for the next year. The due date is on the bill. Questions: (562) 570-6211, lbbiz@longbeach.gov. | Yearly, when the bill arrives (put a reminder one month before the license anniversary in case the bill does not arrive) |
| **Fictitious business name** (only if filed) | The filing receipt: it expires 5 years after filing. | Every 5 years |
| **Domain `zbestmedia.com`** (and the ZBC domain) | Namecheap → Domain List → expiry date; confirm **Auto-Renew** is on and the card on file is current. If the domain lapses, email, Resend sending and the website all stop together. | Reminders 60 and 14 days before expiry, every year |
| **TLS certificates** | Vercel and Railway issue and renew them automatically. Check monthly that they did: `echo | openssl s_client -connect <host>:443 -servername <host> 2>/dev/null | openssl x509 -noout -enddate` for each public host (each site's domain once cut over, the stripe-gateway domain, the orchestrator domain). Less than 14 days left: open a support ticket with the host. | Monthly |
| **Insurance renewals** (once bought: E&O, cyber) | The policy's declarations page | Yearly |
| **Vercel plan** | The site must be on Pro, not Hobby, before real-prospect traffic (Hobby is for non-commercial use). | Check at each billing cycle |

## 3. Per-client dates (set when each client is activated)

For each client, on the day you activate it, add these to the calendar:

| When | What | Runbook |
|---|---|---|
| Activation day + **75 days** | Re-screen the client and every owner, re-run the activation (gives 15 days of slack before expiry). | [ofac-owner-screening.md](ofac-owner-screening.md) D |
| Activation day + **90 days** | `valid_until`: the client's gate closes if not re-screened. | same |
| Each time Legal publishes a new MSA version that must bind existing clients | Re-activate the client (documents are checked at activation only). | Compliance step G.4 |
| After approving an amendment to HR-03, HR-05/07 (non-parameter), HR-08, HR-14, HR-15, US-FTC-465-07 or US-OFAC-04 | **Every** client turns REFUSE until re-activated: plan it for a day you can re-activate them all. | Compliance step G.4 |
| Engagement end + **20 days** / **30 days** | Offboarding: deletion done by day 20, confirmed by day 30. | [client-offboarding.md](client-offboarding.md) |

The activation also needs an OFAC search **no more than 30 days old**, so search no earlier than 30 days before you
activate.

## 4. Event-driven clocks (no fixed date; start them when the event happens)

| Event | Deadline | Runbook |
|---|---|---|
| Security incident involving Shopify Merchant Data | 24 hours | [incident-response.md](incident-response.md) |
| Security incident involving a client's data | 72 hours | same |
| Breach of California residents' personal information (ZBM owns the data) | 30 calendar days; AG sample 15 days after if more than 500 residents | same |
| Blocked or rejected transaction | OFAC report within 10 business days | [ofac-owner-screening.md](ofac-owner-screening.md) |
| Card dispute | The "evidence due" date (usually 7-21 days) | [chargebacks-and-disputes.md](chargebacks-and-disputes.md) |
| Refund request | Acknowledge within 2 business days; pay within 10 business days after it is due | same |
| Privacy request (access, deletion, correction) | Acknowledge within 10 business days; respond within 30 days | [founder-unavailable.md](founder-unavailable.md) 2.3b |
| Accessibility complaint | Reply within 2 business days | same, 2.3c |
| Subprocessor change | Notify clients 15 days before (MSA) | — |
| Shopify client uninstalls / access ends | Credentials deleted in 5 business days; data in 30 days | [client-offboarding.md](client-offboarding.md) |

## 5. Recurring operations

| How often | What | Time |
|---|---|---|
| **Daily** | Through the tunnels: compliance `/health` and finance `compliance_holds_open` (until the heartbeat monitor exists, and as its fallback). A hold with `sanctions: true` starts a 10-business-day clock. | 5 min |
| **Daily** | Stripe Dashboard → Disputes: anything new? | 2 min |
| **Weekly** | Each site's `/api/health` is `ok` (the lead purge ran in the last 48 h); Resend → Metrics (bounces, spam). | 5 min |
| **Weekly** (until the scheduler is deployed) | Release `unverified` Compliance holds by hand once Compliance answers ALLOW (`basis: compliance_allow`); compare Stripe's balance with Finance's 1060 by hand. | 15 min |
| **Monthly** | Per-jurisdiction sales report (Compliance step I; sales-tax nexus watch, WA first). | 15 min |
| **Monthly** | Each volume → Backups shows a backup under 24 h old; `df -h /data` on each volume service under 70%. | 10 min |
| **Monthly** | TLS expiry check (section 2). | 5 min |
| **Monthly** | Dependency and security updates: read GitHub's Dependabot / security alerts on both repos; update Next.js, Python locks, Go and Rust toolchains, and pinned base images as the deploy runbook's "Open items" say; each update goes through a PR and CI. | 1-2 h |
| **Every 90 days** | **Access review (FC-05)**: list every account and token (table in section 6), confirm each holder still needs it, remove the rest, then record the attestation (below). Finance treats FC-05 as red after 90 days (2,160 hours) without one. | 45 min |
| **Every 90 days** | Rotate the Andre approval and onboarding tokens (section 6). Do it in the same sitting as FC-05. | 30 min |
| **Every 90 days** | Review [founder-unavailable.md](founder-unavailable.md): delegate still right, kit still sealed and current. | 10 min |
| **Yearly** | Backup restore rehearsal ([backup-restore-and-replay.md](backup-restore-and-replay.md)). | 3 h |
| **Yearly** | Rotate the yearly secrets (section 6). Re-read every runbook in this folder and fix what changed. | 2 h |

**Recording FC-05** (through the finance tunnel, Andre approval token; `services/finance-py/README.md`, "First steps"):

```sh
# evidence_ref: an id you choose for the saved review (for example the file name of your review sheet)
curl -sS -H @fin-auth.hdr -H @fin-andre.hdr -H 'Content-Type: application/json' \
  --data '{"request_id": "fc05-<YYYY-MM-DD>", "result": "pass", "evidence_ref": "access-review-<YYYY-MM-DD>"}' \
  http://127.0.0.1:18410/fin/v1/controls/FC-05/results
```

Record `"result": "fail"` honestly if the review found something you have not fixed yet, and fix it.

## 6. Token and secret rotation cadence

Rotate at least this often, and **at once** on any suspicion ([incident-response.md](incident-response.md) section 4
has the exact steps for each). These periods are ZBM's policy choice.

| Secret | Every | Notes |
|---|---|---|
| Andre approval and onboarding tokens (Finance, Legal, SEO, Compliance) | 90 days | With FC-05 |
| Railway account / workspace / project tokens | 90 days, or delete when the job is done | Prefer short-lived |
| GitHub personal access tokens | Set an expiry of 90 days or less when you create them | GitHub revokes expired tokens by itself |
| Vercel tokens | 90 days, or project-scoped and deleted after use | `vercel tokens ls` / `rm` |
| Service-to-service tokens (ledger callers, `FIN_SERVICE_TOKEN`, gateway caller, etc.) | 12 months | Use the `LEDGER_CALLERS` procedure for ledger tokens |
| Stripe restricted key | 12 months | Rotate with a short grace period (the Dashboard offers up to 7 days) |
| Stripe webhook signing secret | 12 months | Roll with a delay of up to 24 hours |
| Neon role password | 12 months | Update `POSTGRES_URL` on both Vercel projects |
| Resend API key | 12 months | Sending access only |
| Search Console service-account key | 12 months | |
| `CRON_SECRET`, `LEAD_IP_HASH_SECRET`, `DASHBOARD_SESSION_SECRET` | 12 months | |
| Heartbeat ping URL | 12 months | |
| Account passwords (Google, GitHub, Railway, Stripe, Vercel, Neon, Resend, Namecheap) | When compromised; 2FA always on | Password manager generated |

## 7. Done log

Append one line per completed item. No secrets.

```text
<YYYY-MM-DD> <item> <result / evidence file name>
```

## Sources

Checked on 2026-10-30.

- Compliance register expiry dates and re-verification steps: `docs/deploy/railway.md` → Compliance steps G-I (branch
  `feat/compliance-gate-deploy`).
- FC-05 access review every 90 days: `services/finance-py/README.md`; `services/finance-py/src/intelligences/i09_controls.py`
  (2,160 hours).
- OFAC annual report (as of June 30, due Sept 30) and 10-business-day reports: https://ofac.treasury.gov/faqs/topic/1606
- California and federal estimated-tax dates and return due dates (2026 calendar; same pattern for 2027):
  https://taxes.ca.gov/income-tax/important-dates
- Statement of Information period (Corp. Code §1502) and 1099-NEC threshold: `scratchpad/compliance/business-register.md`
  rows A4 and B5 (sources cited there).
- Long Beach business license renewal by annual bill: https://longbeach.gov/finance/business-info/business-licenses/frequently-asked-questions/
- Railway Config as Code end date (2026-12-01): `docs/deploy/railway.md` (intro).
- Stripe key grace period and webhook secret roll delay: https://docs.stripe.com/keys, https://docs.stripe.com/webhooks
