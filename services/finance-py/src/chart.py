"""
Chart of accounts per entity (Finance spec §B.1). Two entities, two charts; the only way an account exists is this
table (changes would go through an Andre-approved rule proposal; not built — see ADR 0009).

Each account names the sub-ledger kind its lines must carry (``campaign`` / ``payee`` / ``item`` / ``client`` /
``buy``) or None. Titles are checked against the R5 banned words at import (test G8).
"""

from __future__ import annotations

from textguard import banned_words_in

ENTITIES = ("zbc", "zbm")
DEBIT_NORMAL = ("asset", "asset_restricted", "expense")
CREDIT_NORMAL = ("contra_asset", "liability", "equity", "revenue")

# code -> (title, type, subledger kind)
ZBC = {
    "1010": ("Cash - Operating", "asset", None),
    "1020": ("Cash - Restricted: ZBC Client Campaign Deposits", "asset_restricted", None),
    "1040": ("Rail funds - Stripe (segregated creator money)", "asset_restricted", None),
    "1041": ("Rail funds - Trolley (segregated creator money)", "asset_restricted", None),
    "1100": ("Accounts receivable", "asset", "client"),
    "1200": ("Clawback receivable", "asset", "payee"),
    "1210": ("Clawback receivable - allowance", "contra_asset", "payee"),
    "1300": ("Disputed funds", "asset", None),
    "2010": ("Client deposits (contract liability)", "liability", "campaign"),
    "2020": ("Creator payouts payable", "liability", "payee"),
    "2030": ("Payouts in transit", "liability", "item"),
    "2040": ("Backup withholding payable", "liability", None),
    "2050": ("Refunds payable", "liability", "client"),
    "2070": ("Unapplied client cash", "liability", None),
    "3000": ("Owner equity", "equity", None),
    "4010": ("Campaign revenue - verified views (gross)", "revenue", None),
    "5010": ("Creator cost (cost of revenue)", "expense", None),
    "5020": ("Rail and processing fees", "expense", None),
    "5030": ("Dispute fees and losses", "expense", None),
    "5040": ("Uncollectible clawbacks", "expense", None),
}
ZBM = {
    "1010": ("Cash - Operating", "asset", None),
    # Stripe incoming (ADR 0009 amendment, Oct 5 2026): client payments held by Stripe until paid out to 1010
    "1060": ("Stripe balance (clearing)", "asset", None),
    "1100": ("Accounts receivable", "asset", "client"),
    # Media billing (ADR 0009 amendment, Oct 5 2026; founder decisions M1-M8): ZBM is principal on media, every buy
    # is prepaid, and the vendor is paid only from collected money. Sub-ledger ``buy`` = one media buy.
    "1150": ("Prepaid media (vendor paid, not yet run)", "asset", "buy"),
    "1300": ("Disputed funds", "asset", None),
    "2050": ("Refunds payable", "liability", "client"),
    "2070": ("Unapplied client cash", "liability", None),
    "2110": ("Deferred revenue", "liability", "client"),
    "2120": ("Media prepayments (billed, not yet run)", "liability", "buy"),
    "3000": ("Owner equity", "equity", None),
    "4110": ("Service revenue", "revenue", None),
    "4120": ("Media revenue (gross, principal)", "revenue", None),
    "5020": ("Processing fees", "expense", None),
    "5030": ("Dispute fees and losses", "expense", None),
    "5110": ("Media cost (cost of revenue)", "expense", None),
}
MEDIA_ACCOUNTS = ("1150", "2120", "4120", "5110")                       # ZBM only, F12* flows only
CHARTS = {"zbc": ZBC, "zbm": ZBM}

RESTRICTED_POOL = ("1020", "1040", "1041")                               # ZBC only
LIABILITIES = ("2010", "2020", "2030", "2040", "2050", "2070")          # creator-and-client liabilities (ZBC)
RAIL_ACCOUNT = {"stripe": "1040", "trolley": "1041"}
DEPOSITS_ACCOUNT_TITLE = ZBC["1020"][0]

for _e, _chart in CHARTS.items():
    for _code, (_title, _t, _k) in _chart.items():
        if banned_words_in(_title):
            raise RuntimeError(f"chart {_e}/{_code}: account title uses a banned custody word (spec R5)")


def account(entity: str, code: str):
    return CHARTS.get(entity, {}).get(code)


def normal_side(entity: str, code: str) -> str:
    a = account(entity, code)
    return "debit" if a and a[1] in DEBIT_NORMAL else "credit"
