# ZBM operations runbooks

These are the runbooks Andre follows when something goes wrong, or when a recurring task is due. Each one is written
to be followed at 3 a.m., under stress, by one person: numbered steps, exact places to click, exact commands, a time
limit on each step, and a template for every message that has to go out.

| Runbook | Use it when |
|---|---|
| [incident-response.md](incident-response.md) | Anything that might be a security incident: a leaked key, a strange login, a strange payment, client data where it should not be. |
| [lost-device.md](lost-device.md) | Your laptop or phone is lost, stolen, seized or left unlocked somewhere. |
| [chargebacks-and-disputes.md](chargebacks-and-disputes.md) | Stripe tells you a payment is disputed, an ACH debit is returned, or a client asks for a refund. |
| [backup-restore-and-replay.md](backup-restore-and-replay.md) | A Railway volume is lost, corrupted or full, or the money path was down long enough that Stripe stopped retrying. |
| [founder-unavailable.md](founder-unavailable.md) | You will be (or already are) unreachable for more than 2 days. Set it up **before** that happens. |
| [ofac-owner-screening.md](ofac-owner-screening.md) | Before activating any client, every 90 days after, and whenever a search shows a hit. |
| [client-offboarding.md](client-offboarding.md) | An engagement ends, or a client asks you to delete their data. |
| [compliance-calendar.md](compliance-calendar.md) | Every recurring date: OFAC rule re-verification, re-screens, taxes, licenses, renewals, token rotation, access reviews. |

## Rules that apply to every runbook

1. **No secret goes in a runbook, a ticket, a chat, an email or a commit.** Every secret lives in your password
   manager and in the provider's own secret store (Railway Sealed variables, Vercel env vars). Where a runbook says
   "paste the token", it means paste it from the password manager straight into the field it names.
2. **Write down what you do as you do it.** Open the incident log (template in
   [incident-response.md](incident-response.md#incident-log-template)) before step 1 and add a timestamped line for
   every action. The log is your evidence later, and it is how you avoid doing a step twice.
3. **Time limits are there to stop you getting stuck.** When a step's time limit runs out, take the "if it does not
   work" branch, write down why, and move on.
4. **Times are your local time (Pacific) unless a step says UTC.** Legal clocks run from the moment of *discovery*:
   write that moment down first.
5. **You decide.** Every decision point in these runbooks is yours. You may ask any professional you choose for
   advice; no step waits for one.

## Where things are (no secrets here)

| Thing | Where |
|---|---|
| Backend code and the Railway deploy runbook | repo `DarksiedCEO/zbm-zbc`, `docs/deploy/railway.md` |
| Railway project | `zbm-zbc` → environment `production` (7 services: ledger, detection, legal, finance, orchestrator, stripe-gateway, seo; plus compliance once the compliance gate is merged) |
| Websites | repo `DarksiedCEO/zbest-sites`; Vercel projects `zbm-site-v2` and `zbc-site-v1` |
| Lead database | Neon Postgres (`POSTGRES_URL` on each Vercel project) |
| Lead email | Resend (`RESEND_API_KEY` on each Vercel project) |
| Payments | Stripe (ZBM account only; ZBC never uses Stripe) |
| Email and calendar | Google Workspace for zbestmedia.com |
| Secret values | Your password manager, and nowhere else |
