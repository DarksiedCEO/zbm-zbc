# Media billing spec (Finance 31): founder decisions, Oct 5 2026

Source: founder voice session, Oct 5 2026, confirmed in text the same day. **Status: SPEC.** Nothing is built.
This adds media spend to ZBM billing in `finance-py`. It does not change ZBC billing.

## Decisions (founder)
| # | Decision |
|---|---|
| M1 | **ZBM is principal on media.** The client pays ZBM, and ZBM pays the vendor (station, network, OOH operator). The full media amount books as revenue and the vendor payment books as cost. |
| M2 | **All media is prepaid.** No media buy is placed or paid until the client's payment has cleared. |
| M3 | **Collect before pay, enforced by the system.** A vendor payment cannot be recorded or queued against a buy until that buy's client prepayment is marked cleared. |
| M4 | **The markup is a fee on top of cost.** The default is 15%, and it can be overridden per buy (higher on some OOH deals). The true media cost and the fee are **always stored separately**, whatever the client sees. |
| M5 | **Invoice display is chosen per invoice:** either a breakout (media cost + fee) or one blended price. Only what the client sees changes; the ledger is the same either way. |
| M6 | **Stripe is the only processor for incoming money, for every product,** both card and ACH. Checks are recorded by hand. |
| M7 | **Vendors are paid by Andre outside the system.** Finance records the payment (date, amount, method, reference); it never initiates it. |
| M8 | **Tax treatment is gated on a founder confirmation.** There is no CPA. Andre confirms the booking and sales-tax treatment before the first real media invoice. The gate is recorded, not skipped. |

## Conflicts with the current code (must be resolved before the build)
| Where | What the code does today | What the spec needs |
|---|---|---|
| `finance-py/src/models.py:349-350` | No media line code | Two codes: `media_spend` (at cost) and `media_fee` (the markup). A blended invoice renders them as one line but stores both. |
| `svc_books.py:323` | ZBM may not issue a deposit-style (prepaid) invoice | ZBM media prepayment invoices allowed |
| **D11**: `config.py:190`, `svc_books.py:320` | **Card payment is refused.** It waits on counsel question FIN-CQ-09 (Cal. Civ. Code §1748.1 surcharge rules; §1671 late fees) | M6 says card is accepted. **Founder decision needed** (see below). |
| `config.py:27,178` | The only rails built are `stripe` and `trolley`, and they are wired for *outgoing* clipper payouts (Connect). There is no incoming Stripe adapter. | An incoming Stripe adapter (Checkout/PaymentIntents + webhooks) for ZBM invoices |

## Risks the build has to handle (inference; verify current Stripe terms)
- **"Cleared" needs a definition per method.**
  - *Card:* a cardholder can dispute a charge for months. If ZBM pays the station and the client then charges back, M3 is defeated after the fact.
  - *ACH debit:* a payment can still be returned after it settles.
  - *Proposal:* the vendor payment unlocks at ACH settlement plus the return window; card and check each get their own rules.
- **Card fees on large buys.** Stripe's standard US pricing has been about 2.9% + 30¢ for cards and 0.8% capped at $5 for ACH Direct Debit. On a $50,000 buy that is roughly $1,450 vs $5. **Check current pricing before relying on these figures.**
- **ZBC is a separate company** (own EIN, own bank account), so it needs its **own Stripe account**. Its money must never pass through ZBM's account.
