"""Independent reviewer's generator and oracle for Farrah (farrah-spec/1 B-FA-11..12, B-FA-20..25, O-FA-1).

Written from the spec text only (not from agents/farrah.py). Every value is synthetic and seeded. The oracle uses
fractions.Fraction for exact arithmetic and rounds the one B-FA-24 division to 40 significant digits half-even
(CLARIFICATIONS GF-4). ``literal=True`` follows the spec text word for word (U += a negative widening term too);
``literal=False`` is the reading "a widening never narrows" (negative widening terms count 0).
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace
from zoneinfo import ZoneInfo

AI_HOSTS = ("chatgpt.com", "chat.openai.com", "perplexity.ai", "gemini.google.com", "copilot.microsoft.com",
            "claude.ai")
ZONES = ("UTC", "America/Los_Angeles", "America/New_York", "Europe/London", "Asia/Kolkata", "Pacific/Kiritimati",
         "Pacific/Pago_Pago", "Australia/Lord_Howe")
CHANNEL_MAP = {"organic_search": ["Google Search", "SEO blog"], "ai_referral": ["ChatGPT", "AI answer"],
               "other_known": ["Trade show", "Partner referral", "Cold call"]}


# ------------------------------------------------------------------ oracle: mapping (B-FA-11, B-FA-12)

def norm(v):
    return None if v is None else v.strip().lower()


def o_analytics_channel(r: dict) -> str:
    med, src, grp, land = (norm(r.get(k)) for k in ("sessionMedium", "sessionSource", "sessionDefaultChannelGroup",
                                                       "landingPage"))
    if med == "organic" or grp == "organic search":
        return "organic_search"
    if src in AI_HOSTS:
        return "ai_referral"
    if "(not set)" in (med, src, grp) or grp == "unassigned" or (med, src, grp) == (None, None, None):
        return "unknown"
    if (src == "(direct)" or med == "(none)" or grp == "direct") and land in (None, "", "(not set)"):
        return "unknown"
    return "other_known"


def o_crm_channel(r: dict, cmap: dict) -> str:
    med, src = norm(r.get("utm_medium")), norm(r.get("utm_source"))
    if med or src:
        if med == "organic":
            return "organic_search"
        if src in AI_HOSTS:
            return "ai_referral"
        return "other_known"
    lead = norm(r.get("lead_source"))
    if not lead:
        return "unknown"
    hits = {c for c, vals in cmap.items() for v in vals if norm(v) == lead}
    return hits.pop() if len(hits) == 1 else "unknown"


# ------------------------------------------------------------------ oracle: days, months

def o_local_day(v: str, tz: str) -> date:
    if "T" not in v:
        if len(v) == 8:
            return date(int(v[:4]), int(v[4:6]), int(v[6:]))
        return date.fromisoformat(v)
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return dt.date() if dt.tzinfo is None else dt.astimezone(ZoneInfo(tz)).date()


def month_range(month: str):
    y, m = int(month[:4]), int(month[5:])
    first = date(y, m, 1)
    return first, date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)


def round40(q: Fraction) -> Fraction:
    """q to 40 significant digits, half-even (exact)."""
    if q == 0:
        return Fraction(0)
    sign = -1 if q < 0 else 1
    a = abs(q)
    e = 0
    while a >= 10 ** 40:
        a /= 10
        e += 1
    while a < 10 ** 39:
        a *= 10
        e -= 1
    n, rem = divmod(a.numerator, a.denominator)
    r = Fraction(rem, a.denominator)
    if r > Fraction(1, 2) or (r == Fraction(1, 2) and n % 2 == 1):
        n += 1
    return sign * Fraction(n) * Fraction(10) ** e


def F(s) -> Fraction:
    return Fraction(Decimal(s))


# ------------------------------------------------------------------ oracle: figure, band, label (B-FA-20..25)

def oracle(case, literal: bool = False) -> dict:
    """Expected {channel: {figure, lower, upper, label}} (Fractions or None) for one case (see ``make_case``)."""
    out = {}
    doc = case.basis_doc
    if doc is None or not case.covers:
        return {c: {"figure": None, "lower": None, "upper": None, "label": "INSUFFICIENT_EVIDENCE"}
                for c in ("organic_search", "ai_referral")}
    insufficient_state = case.state in ("STALE", "FAILED_LAST", "NOT_CONNECTED")
    tz = doc["window"]["timezone"]
    sums = {c: Fraction(0) for c in ("organic_search", "ai_referral", "other_known", "unknown")}
    nrows = dict.fromkeys(sums, 0)
    field, dayf = ("purchaseRevenue", "date") if case.basis == "analytics" else ("amount", "closed_date")
    other_rev = Fraction(0)
    has_other = False
    for r in doc["rows"]:
        if case.basis == "crm" and r["stage"] != "won":
            continue
        if r.get(field) is None or r.get(dayf) is None:
            continue
        d = o_local_day(r[dayf], tz)
        if f"{d.year:04d}-{d.month:02d}" != case.month:
            continue
        c = o_analytics_channel(r) if case.basis == "analytics" else o_crm_channel(r, case.cmap)
        v = F(r[field])
        sums[c] += v
        if v != 0:
            nrows[c] += 1
        if case.basis == "analytics" and any(r.get(k) == "(other)" for k in
                                             ("sessionSource", "sessionMedium", "sessionDefaultChannelGroup",
                                              "landingPage")):
            has_other = True
            other_rev += v
    total = sum(sums.values(), Fraction(0))
    comp = doc["completeness"]
    widen = Fraction(0)
    flagged = bool(doc["labels"])
    force_low = False

    def add(x):
        return x if literal else max(x, Fraction(0))
    if has_other:
        widen += add(other_rev)
        flagged = True
    elif comp["other_row_share"] is not None and F(comp["other_row_share"]) > 0:
        tot = doc["totals"].get(field)
        widen += add(F(comp["other_row_share"]) * (F(tot) if tot is not None else total))
        flagged = True
    if comp["thresholded"]:
        flagged = True
    if comp["sampled"]:
        flagged, force_low = True, True
    dr = comp["rows_dropped_cap"]
    ndr = dr["rows"] if isinstance(dr, dict) else dr
    if ndr:
        flagged = True
        rev = dr.get("revenue") if isinstance(dr, dict) else None
        if rev is None:
            force_low = True
        else:
            widen += add(F(rev))
    for c in ("organic_search", "ai_referral"):
        lo = sums[c]
        up = lo + sums["unknown"] + widen
        if case.recon_d is not None and total != 0:
            term = round40(abs(F(case.recon_d)) * lo / total)
            up += add(term)
        if insufficient_state or up == 0:
            out[c] = {"figure": None, "lower": None, "upper": None, "label": "INSUFFICIENT_EVIDENCE"}
            continue
        width = up - lo
        if not force_low and not flagged and case.state == "FRESH" and nrows[c] >= 30 and width <= up / 10:
            lab = "HIGH"
        elif not force_low and nrows[c] >= 10 and width <= up * Fraction(35, 100):
            lab = "MEDIUM"
        else:
            lab = "LOW"
        out[c] = {"figure": lo, "lower": lo, "upper": up, "label": lab, "rows": nrows[c]}
    return out


def ns(**kw):
    return SimpleNamespace(**kw)


# ------------------------------------------------------------------ generator

def _hex(rng, n=16):
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def _money(rng, fam):
    """A positive amount string (and its exact value) of the family's shape."""
    if fam == "money" and rng.random() < 0.5:
        k = rng.choice(("big", "tiny", "exp", "digits40", "half"))
        if k == "big":
            s = f"{rng.randint(10 ** 14, 10 ** 15)}.{rng.randint(0, 99):02d}"
        elif k == "tiny":
            s = "0.005"
        elif k == "exp":
            s = f"{rng.randint(1, 999)}.{rng.randint(0, 9)}E{rng.choice(('+2', '-1', '1', '+0', '-3'))}"
        elif k == "digits40":
            n = "".join(rng.choice("0123456789") for _ in range(40)).lstrip("0") or "1"
            p = rng.randint(0, len(n))
            s = n[:p] + ("." + n[p:] if p < len(n) else "") if p else "0." + n
        else:
            s = f"{rng.randint(0, 999)}.{rng.choice(('005', '015', '025', '995'))}"
        return s
    return f"{rng.randint(1, 2000)}.{rng.randint(0, 99):02d}"


def _visible_analytics(rng, ch, day_s, fam):
    """An analytics row encoding TRUE channel ``ch`` visibly (so B-FA-11 maps it to ``ch``)."""
    sp = (lambda s: s) if fam != "gaming" else (lambda s: rng.choice(("", " ", "  ", "\t")) + rng.choice(
        (s, s.upper(), s.title())) + rng.choice(("", " ", "\t")))
    if ch == "organic_search":
        if rng.random() < 0.8:
            return {"date": day_s, "sessionSource": rng.choice(("google", "bing", "duckduckgo")),
                    "sessionMedium": sp("organic"), "sessionDefaultChannelGroup": "Organic Search",
                    "landingPage": "/p"}
        return {"date": day_s, "sessionSource": "google", "sessionMedium": "(none)",
                "sessionDefaultChannelGroup": sp("Organic Search"), "landingPage": "/p"}
    if ch == "ai_referral":
        return {"date": day_s, "sessionSource": sp(rng.choice(AI_HOSTS)), "sessionMedium": "referral",
                "sessionDefaultChannelGroup": "Referral", "landingPage": "/a"}
    look = ["newsletter", "facebook.com", "partner.example"]
    if fam == "gaming":
        look += ["chatgpt.com.evil.test", "perplexity.ai.example", "xchatgpt.com", "claude.ai.co",
                 "chatgpt\u2024com", "cl\u0430ude.ai"]
    med = rng.choice(("email", "referral", "cpc", "social"))
    if fam == "gaming" and rng.random() < 0.3:
        med = rng.choice(("\u043erganic", "0rganic", "organic-ish", "orga nic", "organ\u0131c"))
    return {"date": day_s, "sessionSource": rng.choice(look), "sessionMedium": med,
            "sessionDefaultChannelGroup": "Referral", "landingPage": "/x"}


def _unknown_analytics(rng, day_s):
    k = rng.randrange(4)
    if k == 0:
        return {"date": day_s, "sessionSource": "(not set)", "sessionMedium": "(not set)",
                "sessionDefaultChannelGroup": "Unassigned", "landingPage": "(not set)"}
    if k == 1:
        return {"date": day_s, "sessionSource": "(direct)", "sessionMedium": "(none)",
                "sessionDefaultChannelGroup": "Direct", "landingPage": "(not set)"}
    if k == 2:
        return {"date": day_s, "sessionSource": "x", "sessionMedium": "y", "sessionDefaultChannelGroup": "Unassigned",
                "landingPage": "/u"}
    return {"date": day_s}


def _visible_crm(rng, ch, fam):
    sp = (lambda s: s) if fam != "gaming" else (lambda s: rng.choice(("", " ")) + rng.choice((s, s.upper())) +
                                                rng.choice(("", " ")))
    k = rng.random()
    if ch == "organic_search":
        if k < 0.5:
            return {"utm_medium": sp("organic"), "utm_source": "google", "lead_source": None}
        return {"utm_medium": None, "utm_source": None, "lead_source": sp(rng.choice(CHANNEL_MAP["organic_search"]))}
    if ch == "ai_referral":
        if k < 0.5:
            return {"utm_medium": "referral", "utm_source": sp(rng.choice(AI_HOSTS)), "lead_source": None}
        return {"utm_medium": None, "utm_source": None, "lead_source": sp(rng.choice(CHANNEL_MAP["ai_referral"]))}
    if k < 0.5:
        src = rng.choice(("newsletter", "linkedin", "chatgpt.com.evil.test" if fam == "gaming" else "fb"))
        return {"utm_medium": rng.choice(("email", "cpc", "\u043erganic" if fam == "gaming" else "social")),
                "utm_source": src, "lead_source": None}
    return {"utm_medium": None, "utm_source": None, "lead_source": rng.choice(CHANNEL_MAP["other_known"])}


def _unknown_crm(rng):
    return {"utm_medium": None, "utm_source": None,
            "lead_source": rng.choice((None, "Walk in", "", "  ", "Google Searchx"))}


def _crm_day(rng, d: date, tz: str, fam: str) -> str:
    """A closed_date whose LOCAL day (dataset timezone) is ``d``: a date, or a date-time with an offset at the edge
    of the local day (A-FA-02)."""
    if fam != "month" or rng.random() < 0.3:
        return d.isoformat()
    local = datetime(d.year, d.month, d.day, *rng.choice(((0, 0, 0), (23, 59, 59), (12, 0, 0), (0, 0, 1))),
                     tzinfo=ZoneInfo(tz))
    off = rng.choice((0, -11, -7, 5, 14))
    from datetime import timezone as _tz
    dt = local.astimezone(_tz(timedelta(hours=off)))
    s = dt.strftime("%Y-%m-%dT%H:%M:%S") + ("Z" if off == 0 and rng.random() < 0.5 else dt.strftime("%z")[:3] + ":" +
                                            dt.strftime("%z")[3:])
    return s


FAMILIES = ("plain", "money", "month", "flags", "recon", "gaming", "edge_label", "state", "coverage")


def make_case(seed: int, family: str | None = None, tid: str = "acme"):
    """One tenant-month with KNOWN truth. Returns a namespace: basis, month, tz, basis_doc, other_doc, recon_d,
    state, cmap, truth {channel: Fraction}, hidden_negative (a hidden part holds a refund), covers, family."""
    rng = random.Random(seed)
    fam = family or FAMILIES[seed % len(FAMILIES)]
    basis = rng.choice(("analytics", "crm"))
    y = rng.choice((2024, 2025, 2026, 2027, 2028))
    mo = rng.choice((1, 2, 3, 10, 11, 12)) if fam == "month" else rng.randint(1, 12)
    month = f"{y:04d}-{mo:02d}"
    tz = rng.choice(ZONES)
    first, last = month_range(month)
    unk_share = rng.choice((0, 0, 0.01, 0.03, 0.05, 0.1, 0.3, 0.6, 1.0))
    refund_p = 0.0 if fam in ("plain", "edge_label") else rng.choice((0, 0.02, 0.1))
    n = rng.choice((0, 1, 5, 9, 10, 11, 29, 30, 31, 60, 200)) if fam in ("edge_label", "plain") else rng.randint(0, 120)
    truth = dict.fromkeys(("organic_search", "ai_referral", "other_known"), Fraction(0))
    rows, hidden_neg = [], False
    pad_lo = first - timedelta(days=rng.choice((0, 0, 3)))
    pad_hi = last + timedelta(days=rng.choice((0, 0, 2)))
    for i in range(n):
        ch = rng.choices(("organic_search", "ai_referral", "other_known"), (5, 2, 3))[0]
        s = _money(rng, fam)
        if rng.random() < refund_p:
            s = "-" + s
        v = F(s)
        if fam == "money" and rng.random() < 0.03:
            s, v = "-0", Fraction(0)
        d = first + timedelta(days=rng.randrange((last - first).days + 1))
        if rng.random() < 0.1:
            d = rng.choice((pad_lo, pad_hi))                 # inside the window, maybe outside the month
        in_month = first <= d <= last
        hide = rng.random() < unk_share
        if basis == "analytics":
            r = _unknown_analytics(rng, d.strftime("%Y%m%d")) if hide else \
                _visible_analytics(rng, ch, d.strftime("%Y%m%d"), fam)
            r["purchaseRevenue"] = s
            r["sessions"] = rng.randint(1, 5)
        else:
            enc = _unknown_crm(rng) if hide else _visible_crm(rng, ch, fam)
            stage = rng.choices(("won", "lost", "open"), (8, 1, 1))[0]
            r = {"deal_ref": "d" + _hex(rng, 8), "created_date": None,
                 "closed_date": _crm_day(rng, d, tz, fam) if stage in ("won", "lost") or rng.random() < 0.3 else None,
                 "stage": stage,
                 "amount": s, "currency": "USD", "utm_campaign": None, "landing_path": None, **enc}
            if stage != "won":
                in_month = False
        rows.append(r)
        if in_month:
            truth[ch] += v
            if hide and v < 0:
                hidden_neg = True
    comp = {"sampled": False, "thresholded": False, "other_row_share": None, "rows_dropped_cap": 0}
    totals = {}
    field = "purchaseRevenue" if basis == "analytics" else "amount"
    if fam == "flags" or (fam == "plain" and rng.random() < 0.1):
        k = rng.choice(("other_row", "share", "share_nototal", "dropped_rev", "dropped_norev", "dropped_int",
                        "thresholded", "sampled") if basis == "analytics" else ("dropped_rev", "dropped_norev",
                                                                                "dropped_int"))
        hidden = {c: Fraction(0) for c in truth}
        for _ in range(rng.randint(1, 6)):
            hc = rng.choice(list(truth))
            hv = F(_money(rng, "plain"))
            hidden[hc] += hv
        hsum = sum(hidden.values(), Fraction(0))
        hs = str(Decimal(hsum.numerator) / Decimal(hsum.denominator))
        if k == "other_row":
            rows.append({"date": (first + timedelta(days=3)).strftime("%Y%m%d"), "sessionSource": "(other)",
                         "sessionMedium": "(other)", "sessionDefaultChannelGroup": "(other)",
                         "landingPage": "(other)", "purchaseRevenue": hs, "sessions": 1})
            # the (other) row is mapped other_known; its true composition is ``hidden``
            for c in truth:
                truth[c] += hidden[c]
        elif k in ("share", "share_nototal"):
            visible_total = sum(truth.values(), Fraction(0))   # approx: truth so far = rows (incl. unknown)
            true_total = visible_total + hsum
            if true_total > 0 and 0 <= hsum <= true_total:
                share = hsum / true_total
                from decimal import Context, ROUND_CEILING
                comp["other_row_share"] = str(Context(prec=38, rounding=ROUND_CEILING).divide(
                    Decimal(share.numerator), Decimal(share.denominator)))
                if k == "share":
                    totals[field] = str(Decimal(true_total.numerator) / Decimal(true_total.denominator))
                for c in truth:
                    truth[c] += hidden[c]
        elif k.startswith("dropped"):
            comp["rows_dropped_cap"] = {"rows": rng.randint(1, 50), "revenue": hs if k == "dropped_rev" else None} \
                if k != "dropped_int" else rng.randint(1, 50)
            if k == "dropped_rev":
                for c in truth:
                    truth[c] += hidden[c]
        elif k == "thresholded":
            comp["thresholded"] = True
        else:
            comp["sampled"] = True
    labels = [f"incomplete:{f}" for f in ("sampled", "thresholded") if comp[f]]
    if comp["other_row_share"] is not None:
        labels.append("incomplete:other_row_share")
    if comp["rows_dropped_cap"]:
        labels.append("incomplete:rows_dropped_cap")
    covers = True
    ws, we = pad_lo, pad_hi
    if fam == "coverage" and rng.random() < 0.6:
        covers = False
        if rng.random() < 0.5:
            ws = first + timedelta(days=1)
        else:
            we = last - timedelta(days=1)
        rows = [r for r in rows if ws <= o_local_day(r.get("date") or r.get("closed_date") or
                                                     first.isoformat(), tz) <= we]
    did = "ds-" + _hex(rng)
    prop_tz = tz
    if fam == "month" and rng.random() < 0.1:
        prop_tz = rng.choice([z for z in ZONES if z != tz])
        covers = False
    doc = {"contract": "osei.dataset/1", "tenant_id": tid, "dataset_id": did, "source": basis,
           "read_id": "read-" + did[3:], "window": {"start": ws.isoformat(), "end": we.isoformat(), "timezone": tz},
           "grain": "day", "surface": {"fingerprint": did[3:], "parts": {"property_settings": {"timezone": prop_tz,
                                                                                                "currency": "USD"}}},
           "validation": {"state": "VALID", "reason": None, "counts": {"rows": len(rows)}},
           "completeness": comp, "totals": totals, "rows": rows, "produced_at": "2026-10-05T12:00:00Z",
           "refresh_tier": "first_party_daily",
           "evidence_ref": {"run_id": "read-" + did[3:], "contract": "osei.dataset/1", "item_key": "rows"},
           "data_class": "measured", "labels": labels}
    state = "FRESH"
    if fam == "state" or rng.random() < 0.05:
        state = rng.choice(("FRESH", "DUE", "NEVER", "STALE", "FAILED_LAST", "NOT_CONNECTED", None))
    recon_d, other_doc = None, None
    if fam == "recon" or rng.random() < 0.1:
        recon_d = rng.choice(("0", "12.34", "-250.00", "1000", "0.01", "-99999.99"))
        oid = "ds-" + _hex(rng)
        other = "crm" if basis == "analytics" else "analytics"
        other_doc = {**doc, "dataset_id": oid, "source": other, "read_id": "read-" + oid[3:], "rows": [],
                     "completeness": {"sampled": False, "thresholded": False, "other_row_share": None,
                                      "rows_dropped_cap": 0}, "labels": [], "totals": {},
                     "window": {"start": first.isoformat(), "end": last.isoformat(), "timezone": tz},
                     "surface": {"fingerprint": oid[3:], "parts": {"property_settings": {"timezone": tz}}},
                     "evidence_ref": {"run_id": "read-" + oid[3:], "contract": "osei.dataset/1", "item_key": "rows"}}
        if not covers:
            recon_d = None
    return ns(seed=seed, family=fam, basis=basis, month=month, tz=tz, basis_doc=doc, other_doc=other_doc,
              recon_d=recon_d, state=state, cmap=CHANNEL_MAP if basis == "crm" else {}, truth=truth,
              hidden_negative=hidden_neg, covers=covers, comp=comp, has_share_total=bool(totals))
