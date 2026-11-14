"""The attribution oracle's corpus (farrah-spec/1 O-FA-1..3; SUITE_PLAN 2.2 / 2.4): generated tenant-months whose
TRUE channel revenue is known. Each transaction gets its true channel first; then the generator hides part of it as
first-party data does - channel fields lost (``(not set)`` / no utm and an unmapped lead source), aggregated into the
documented ``(other)`` row, withheld by thresholding with a documented share (rounded UP, as an upper bound needs),
dropped at a row cap with the dropped revenue reported - and keeps the truth.

Out of the claim by the spec's own residuals: AI visits that send no referrer at all (R-FA-2: the band cannot see
them; such a visit is generated as an unknown-channel row, i.e. its referrer is lost, never re-labelled direct), and
refunds on HIDDEN rows (open question QB-FA-6: the spec's U = F + R_unknown is not an upper bound when the unknown
revenue holds a refund; refunds here sit on visible rows, and the pinned xfail case shows the other one)."""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Context, Decimal, ROUND_CEILING

from conformance.gen.attribution import arow, crow, dataset, money, month_days, rc4

ORGANIC = [("google", "organic", "Organic Search"), ("bing", "organic", "Organic Search"),
           ("duckduckgo", "organic", "Organic Search")]
AI = [("chatgpt.com", "referral", "Referral"), ("perplexity.ai", "referral", "Referral"),
      ("claude.ai", "referral", "Referral"), ("gemini.google.com", "referral", "Referral")]
OTHER = [("facebook.com", "cpc", "Paid Social"), ("newsletter", "email", "Email"),
         ("partner.example", "referral", "Referral"), ("(direct)", "(none)", "Direct")]
UNKNOWN = [("(not set)", "(not set)", "Unassigned"), ("(direct)", "(none)", "Direct")]
LEAD = {"organic_search": ["Google Search", "Organic"], "ai_referral": ["ChatGPT", "AI assistant"],
        "other_known": ["Trade show", "Referral partner", "Paid ads"]}
UNMAPPED = ["Walk in", "Other", "Unknown source"]
CHANNEL_MAP = {c: list(v) for c, v in LEAD.items()}
UP = Context(prec=40, rounding=ROUND_CEILING)


def _channel(rng) -> str:
    x = rng.random()
    return "organic_search" if x < 0.45 else "ai_referral" if x < 0.55 else "other_known"


def truth_case(seed: int, kind: str, tid: str, unknown_share: float, flags: dict, svc=None, store=None) -> dict:
    """One tenant-month. ``kind``: ``ecommerce`` (analytics basis) or ``leadgen`` (crm basis). ``flags``: subset of
    other_row, thresholded, dropped, sampled, reconcile. Returns {doc, truth, flags, ...}."""
    rng = random.Random(seed)
    first, last = month_days()
    basis = "analytics" if kind == "ecommerce" else "crm"
    n = rng.randint(60, 400) if kind == "ecommerce" else rng.randint(30, 160)
    txns = []
    for i in range(n):
        ch = _channel(rng)
        rev = Decimal(money(rng, 5, 900) if kind == "ecommerce" else money(rng, 500, 40_000))
        day = first + timedelta(days=rng.randint(0, (last - first).days))
        txns.append({"i": i, "ch": ch, "rev": rev, "day": day, "hidden": rng.random() < unknown_share})
    truth = {c: Decimal(0) for c in ("organic_search", "ai_referral", "other_known")}
    for t in txns:
        truth[t["ch"]] += t["rev"]
    # refunds: visible, channel-labelled rows (counted in their channel, A-FA-01)
    refunds = []
    for t in rng.sample(txns, k=min(3, len(txns))):
        if not t["hidden"] and rng.random() < 0.5:
            r = -(t["rev"] / 2).quantize(Decimal("0.01"))
            refunds.append({**t, "rev": r, "i": n + len(refunds)})
            truth[t["ch"]] += r
    pool = list(txns)
    other_row, withheld, dropped = [], [], []
    if basis == "analytics" and flags.get("other_row"):
        other_row = [pool.pop(rng.randrange(len(pool))) for _ in range(max(1, len(pool) // 30))]
    elif basis == "analytics" and flags.get("thresholded"):
        withheld = [pool.pop(rng.randrange(len(pool))) for _ in range(max(1, len(pool) // 25))]
    if flags.get("dropped"):
        dropped = [pool.pop(rng.randrange(len(pool))) for _ in range(max(1, len(pool) // 40))]
    rows = []
    for t in pool + refunds:
        if basis == "analytics":
            src, med, grp = rng.choice(UNKNOWN) if t["hidden"] else rng.choice(
                {"organic_search": ORGANIC, "ai_referral": AI, "other_known": OTHER}[t["ch"]])
            landing = "(not set)" if t["hidden"] and src == "(direct)" else f"/p/{t['i']}"
            rows.append(arow(t["day"], str(t["rev"]), src, med, grp, landing))
        else:
            if t["hidden"]:
                rows.append(crow(t["day"], str(t["rev"]), lead_source=rng.choice(UNMAPPED), ref=f"d{t['i']}"))
            elif rng.random() < 0.3 and t["ch"] != "other_known":
                med, src = ("organic", "google") if t["ch"] == "organic_search" else ("referral", "chatgpt.com")
                rows.append(crow(t["day"], str(t["rev"]), utm_medium=med, utm_source=src, ref=f"d{t['i']}"))
            else:
                rows.append(crow(t["day"], str(t["rev"]), lead_source=rng.choice(LEAD[t["ch"]]), ref=f"d{t['i']}"))
    if basis == "crm":
        for j in range(rng.randint(0, 20)):
            rows.append(crow(first, money(rng, 500, 9000), stage=rng.choice(("open", "lost")), ref=f"o{j}"))
    if other_row:
        rows.append(arow(first, str(sum((t["rev"] for t in other_row), Decimal(0))), "(other)", "(other)",
                         "(other)", "(other)"))
    total = sum((t["rev"] for t in txns + refunds), Decimal(0))
    kw: dict = {}
    if withheld:
        w = sum((t["rev"] for t in withheld), Decimal(0))
        kw = {"thresholded": True, "other_row_share": str(UP.divide(w, total)) if total else "0",
              "totals": {"purchaseRevenue": str(total)}}
    if dropped:
        kw["rows_dropped_cap"] = {"rows": len(dropped), "revenue": str(sum((t["rev"] for t in dropped), Decimal(0)))}
    if flags.get("sampled") and basis == "analytics":
        kw["sampled"] = True
    visible = {c: Decimal(0) for c in ("organic_search", "ai_referral", "other_known", "unknown")}
    for t in pool + refunds:
        visible["unknown" if t["hidden"] else t["ch"]] += t["rev"]
    other_sum = sum((t["rev"] for t in other_row), Decimal(0))
    visible["other_known"] += other_sum
    rng.shuffle(rows)
    doc = dataset(tid, basis, rows, rng=rng, **kw)
    if other_row:
        doc["labels"] = sorted(set(doc["labels"]) | {"incomplete:other_row_share"})
    out = {"doc": doc, "truth": truth, "basis": basis, "flags": dict(flags), "seed": seed, "kind": kind,
           "unknown_share": unknown_share, "channel_map": CHANNEL_MAP if basis == "crm" else None, "other": None,
           "visible": visible, "other_row_sum": other_sum, "total": total,
           "dropped_sum": sum((t["rev"] for t in dropped), Decimal(0)), "rc4": None}
    if flags.get("reconcile"):
        other_src = "crm" if basis == "analytics" else "analytics"
        other = dataset(tid, other_src, [], rng=rng)
        out["other"] = other
        out["rc4"] = rc4(doc, other, f"{rng.randint(-5000, 5000)}.{rng.randint(0, 99):02d}")
    return out


def corpus(n: int, seed: int = 1) -> list:
    """``n`` tenant-month specs: e-commerce and lead-generation, unknown shares 0-60%, flags at realistic rates."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        kind = "ecommerce" if i % 2 == 0 else "leadgen"
        u = rng.choice((0.0, 0.01, 0.03, 0.05, 0.1, 0.2, 0.35, 0.6))
        flags = {"other_row": rng.random() < 0.1, "thresholded": rng.random() < 0.1, "dropped": rng.random() < 0.05,
                 "sampled": rng.random() < 0.05, "reconcile": rng.random() < 0.3}
        out.append({"seed": seed * 100_000 + i, "kind": kind, "unknown_share": u, "flags": flags})
    return out
