"""CI watch corpus for CI-1 (product-change watcher): 30 fictional competitor pages x 10 versions (test-set spec v0).

Each version is the page's HTML as fetched. Between versions there is always NOISE (footer date, tracking parameters,
an A/B headline, a cookie banner, a per-request nonce, testimonial order) and sometimes ONE meaningful change: a price
change, a new feature, a removed feature, a new plan, or a terms change. The watcher must report exactly the meaningful
changes and stay silent on noise-only versions.

Shapes: 20 pricing pages (plans, prices, features, terms) and 10 changelog pages (dated entries; a new entry that
ships or removes a feature, or a policy entry). App-store and docs shapes are not built yet.
"""

from __future__ import annotations

from html import escape

from . import common
from .common import Stream

POOL_ID = "ci-watch"
PAGES, VERSIONS = 30, 10
CHANGES = ("price_change", "new_feature", "removed_feature", "new_plan", "terms_change")
FEATURES = ("Missed-call text-back", "Review requests", "Two-way texting", "Call tracking", "AI receptionist",
            "Listing sync", "Web chat", "Payments by text", "Rank tracking", "Spanish support", "Booking widget",
            "Reporting dashboard", "Zapier integration", "Team inbox", "Auto-replies")
HEADLINES = (("Grow faster with every call", "Never miss a customer again"),)
TERMS = ("Month to month. Cancel anytime.", "12-month agreement required.", "Annual plan billed upfront.",
         "30-day money-back guarantee.", "Setup fee of $299 applies.")


def _model(s: Stream, kind: str) -> dict:
    if kind == "pricing":
        plans = []
        feats = s.shuffled(FEATURES)
        for j, name in enumerate(("Starter", "Pro", "Elite")[: s.between(2, 3)]):
            plans.append({"name": name, "price": 49 + 50 * j + s.below(30),
                          "features": sorted(feats[: 3 + 3 * j])})
        return {"kind": kind, "plans": plans, "terms": s.pick(TERMS)}
    entries = [{"date": f"2026-{m:02d}-{s.between(1, 28):02d}", "type": "ship", "feature": f}
               for m, f in zip(range(1, 6), s.shuffled(FEATURES)[:5])]
    return {"kind": kind, "entries": entries, "terms": s.pick(TERMS)}


def _mutate(model: dict, s: Stream, change: str) -> tuple[dict, dict]:
    m = {k: (list(v) if isinstance(v, list) else v) for k, v in model.items()}
    if m["kind"] == "pricing":
        m["plans"] = [dict(p, features=list(p["features"])) for p in model["plans"]]
        p = s.pick(m["plans"])
        if change == "price_change":
            old = p["price"]
            p["price"] = old + s.pick([-20, -10, 10, 20, 50])
            return m, {"type": change, "plan": p["name"], "from": old, "to": p["price"]}
        if change == "new_feature":
            new = s.pick([f for f in FEATURES if f not in p["features"]])
            p["features"] = sorted(p["features"] + [new])
            return m, {"type": change, "plan": p["name"], "feature": new}
        if change == "removed_feature":
            gone = s.pick(p["features"])
            p["features"] = [f for f in p["features"] if f != gone]
            return m, {"type": change, "plan": p["name"], "feature": gone}
        if change == "new_plan":
            name = f"Plan {len(m['plans']) + 1}"
            m["plans"].append({"name": name, "price": 400 + s.below(200), "features": sorted(s.shuffled(FEATURES)[:6])})
            return m, {"type": change, "plan": name}
    else:
        if change in ("new_feature", "removed_feature", "new_plan", "price_change"):
            change = s.pick(["new_feature", "removed_feature"])
            feat = s.pick(FEATURES)
            m["entries"] = m["entries"] + [{"date": f"2026-{6 + len(m['entries']) % 6:02d}-15",
                                           "type": "ship" if change == "new_feature" else "remove", "feature": feat}]
            return m, {"type": change, "feature": feat}
    old = m["terms"]
    m["terms"] = s.pick([t for t in TERMS if t != old])
    return m, {"type": "terms_change", "from": old, "to": m["terms"]}


def _render(model: dict, page: dict, s: Stream, version: int) -> str:
    """HTML with noise baked in: the watcher must see through it."""
    utm = f"?utm_source=nav&utm_campaign=v{s.below(1000)}"
    head = HEADLINES[0][s.below(2)]
    parts = [f'<html><head><meta name="csrf-token" content="{s.below(10**9):09d}"></head><body>',
             f'<h1 class="ab-headline">{escape(head)}</h1>',
             f'<nav><a href="https://{page["host"]}/signup{utm}">Sign up</a></nav>']
    if s.chance(1, 2):
        parts.append('<div class="cookie-banner">We use cookies. <button>Accept</button></div>')
    if model["kind"] == "pricing":
        for p in model["plans"]:
            feats = "".join(f"<li>{escape(f)}</li>" for f in p["features"])
            parts.append(f'<section class="plan" data-plan="{escape(p["name"])}"><h3>{escape(p["name"])}</h3>'
                         f'<span class="price">${p["price"]}/mo</span><ul class="features">{feats}</ul></section>')
    else:
        for e in model["entries"]:
            parts.append(f'<article class="entry" data-type="{e["type"]}"><time>{e["date"]}</time>'
                         f'<p>{"Shipped" if e["type"] == "ship" else "Removed"}: {escape(e["feature"])}</p></article>')
    quotes = s.shuffled(["Great tool!", "Saved us hours.", "Five stars."])
    parts.append('<aside class="testimonials">' + "".join(f"<q>{q}</q>" for q in quotes) + "</aside>")
    parts.append(f'<p class="terms">{escape(model["terms"])}</p>')
    parts.append(f'<footer>Updated 2026-11-{(version % 28) + 1:02d}. Session {s.below(10**6)}</footer></body></html>')
    return "".join(parts)


def generate(seed: str) -> tuple[list[dict], list[dict]]:
    root = Stream(f"{POOL_ID}|{seed}")
    snaps, key = [], []
    for i in range(PAGES):
        s = root.child(f"p{i}")
        kind = "pricing" if i % 3 else "changelog"
        page = {"page_id": common.opaque_id("page", seed, i), "host": f"rival-{common.opaque_id('h', seed, i)[-6:]}.test",
                "kind": kind}
        model = _model(s.child("model"), kind)
        for v in range(VERSIONS):
            vs = s.child(f"v{v}")
            change = None
            if v > 0 and vs.chance(3, 10):
                model, change = _mutate(model, vs.child("mut"), vs.pick(CHANGES))
            snaps.append({"fixture": common.FIXTURE_LABEL, "pool": POOL_ID, "page_id": page["page_id"],
                          "url": f"https://{page['host']}/{'pricing' if kind == 'pricing' else 'changelog'}",
                          "version": v, "html": _render(model, page, vs.child("render"), v)})
            key.append({"page_id": page["page_id"], "version": v, "meaningful_changes": [change] if change else []})
    return snaps, key
