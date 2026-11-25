"""Independent re-derivation of CI-1's answers: parse each page's semantic content (plans, prices, features,
changelog entries, terms) out of the HTML, ignore everything else, and diff consecutive versions."""

from __future__ import annotations

import re
from html import unescape

PLAN = re.compile(r'<section class="plan" data-plan="([^"]+)"><h3>.*?</h3><span class="price">\$(-?\d+)/mo</span>'
                  r'<ul class="features">(.*?)</ul></section>')
LI = re.compile(r"<li>(.*?)</li>")
ENTRY = re.compile(r'<article class="entry" data-type="(ship|remove)"><time>[^<]*</time><p>[^:]*: (.*?)</p></article>')
TERMS = re.compile(r'<p class="terms">(.*?)</p>')


def parse(html: str) -> dict:
    plans = {unescape(n): (int(p), {unescape(f) for f in LI.findall(fs)}) for n, p, fs in PLAN.findall(html)}
    entries = [(t, unescape(f)) for t, f in ENTRY.findall(html)]
    terms = TERMS.findall(html)
    return {"plans": plans, "entries": entries, "terms": unescape(terms[0]) if len(terms) == 1 else None}


def diff(old: dict, new: dict) -> list[dict]:
    out = []
    for name in new["plans"]:
        if name not in old["plans"]:
            out.append({"type": "new_plan", "plan": name})
            continue
        (p0, f0), (p1, f1) = old["plans"][name], new["plans"][name]
        if p0 != p1:
            out.append({"type": "price_change", "plan": name, "from": p0, "to": p1})
        out += [{"type": "new_feature", "plan": name, "feature": f} for f in sorted(f1 - f0)]
        out += [{"type": "removed_feature", "plan": name, "feature": f} for f in sorted(f0 - f1)]
    out += [{"type": "removed_plan", "plan": n} for n in old["plans"] if n not in new["plans"]]
    if new["entries"][: len(old["entries"])] == old["entries"]:
        for t, f in new["entries"][len(old["entries"]):]:
            out.append({"type": "new_feature" if t == "ship" else "removed_feature", "feature": f})
    else:
        out.append({"type": "changelog_rewritten"})
    if old["terms"] != new["terms"]:
        out.append({"type": "terms_change", "from": old["terms"], "to": new["terms"]})
    return out


def check(snapshots: list[dict], key: list[dict]) -> list[str]:
    errors = []
    by = {(k["page_id"], k["version"]): k for k in key}
    if len(by) != len(key) or sorted(by) != sorted((s["page_id"], s["version"]) for s in snapshots):
        errors.append("answer rows do not match the snapshots one to one")
    last: dict[str, dict] = {}
    for snap in sorted(snapshots, key=lambda x: (x["page_id"], x["version"])):
        cur = parse(snap["html"])
        want = diff(last[snap["page_id"]], cur) if snap["page_id"] in last else []
        last[snap["page_id"]] = cur
        k = by.get((snap["page_id"], snap["version"]))
        if k != {"page_id": snap["page_id"], "version": snap["version"], "meaningful_changes": want}:
            errors.append(f"{snap['page_id']} v{snap['version']}: key {k and k['meaningful_changes']} vs page {want}")
    return errors
