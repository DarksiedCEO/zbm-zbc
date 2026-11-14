"""core-spec/1 P-4: end to end through the real service. One citation run carrying a label form of every SP row with
CV R (one Latin and one script entry where the row has both) in the answer, the cited page's title and its hint,
three run-time values per form: 0 values shown to the owning tenant for the CV-R forms; 0 values readable in anything
written to the data directory, ledger or logs for ALL forms (the CV-O forms ride along and their shown values are
reported). Ids and counts only."""
import json

import pytest

import test_vault_wiring as vw
from conformance import core_matrix as M
from conformance import core_ref as R
from helpers import rid
from ports import ProviderAnswer

pytestmark = pytest.mark.conformance


def e2e_forms() -> list:
    """(sp, eid, form, cv verdict) - per SP row: the first form of the first Latin and the first script entry."""
    out = []
    for sp in range(1, 41):
        if sp == 39:
            continue                                       # (markup forms carry their own separator)
        taken = set()
        for eid, e, kind, _sc, _g in R.entries():
            group = "latin" if kind in M.LATIN_KINDS else "script"
            if group in taken or not M.applies(eid, kind, sp, e):
                continue
            fs = M.forms(eid, sp)
            if not fs:
                continue
            tag, form = fs[0]
            cv = M.sp_verdict(eid, sp, tag, "cv")
            if cv in ("R", "O") and M.sp_verdict(eid, sp, tag, "strict") == "R":
                out.append((sp, eid, form, cv))
                taken.add(group)
    return out


def run(tmp_path) -> dict:
    forms = e2e_forms()
    answers, values = [], []
    for i, (sp, eid, label, cv) in enumerate(forms):
        url = f"https://site.test/guides/{vw.MARK}-note-{i}"
        vals = [M.value(f"e2e|{i}|{k}") for k in range(3)]
        a = ProviderAnswer("ANSWER", f"Z Best Media sellers note {i}. {label}: {vals[0]}", [url], "m", "1",
                           cite_from_text=False, provider="openai", asked_at="2026-10-09T18:00:00Z")
        a.citation_titles = {url: f"{label} = {vals[1]}"}
        a.citation_hints = {url: f"{label} is {vals[2]}"}
        answers.append(a)
        values.append((sp, eid, cv, vals))
    h = vw.harness(tmp_path)
    port = vw.Port(answers)
    h.ports.engines["openai"] = port
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [vw.DOMAIN]}, andre=True))
    psid = h.ok(h.post("/tenants/zbm/prompt-sets", {
        "request_id": rid(), "name": "core", "brand_terms": ["Z Best Media"], "competitor_domains": ["competitor.test"],
        "prompts": [f"who recovers marketplace revenue, question {i}" for i in range(-(-len(forms) // 3))],
        "engines": ["openai"]}),
        201)["prompt_set_id"]
    run = h.ok(h.post("/tenants/zbm/citation-runs", {"request_id": rid(), "domain": vw.DOMAIN, "prompt_set_id": psid}),
               201)
    assert run["status"] == "completed" and port.asked >= len(forms)
    view = h.ok(h.get(f"/tenants/zbm/citation-runs/{run['citation_run_id']}", caller="dashboard"))
    shown_blob = json.dumps(view, ensure_ascii=False) + json.dumps(view)
    disk = vw.written(h)
    out = {"forms": len(forms), "cv_r_forms": sum(1 for _s, _e, cv, _v in values if cv == "R"),
           "shown_cv_r": 0, "shown_cv_o": {}, "on_disk": 0}
    for sp, eid, cv, vals in values:
        n_shown = sum(1 for v in vals if v in shown_blob)
        if cv == "R":
            out["shown_cv_r"] += n_shown
            if n_shown:
                out.setdefault("shown_cv_r_by", []).append(f"SP{sp:02d}|{eid}|{n_shown}")
        elif n_shown:
            out["shown_cv_o"][f"SP{sp:02d}|{eid}"] = n_shown
        out["on_disk"] += sum(1 for v in vals if v in disk)
    return out


def test_core_P_4_end_to_end_no_cv_r_value_shown_and_nothing_on_disk(tmp_path):
    s = run(tmp_path)
    print(f"P-4 end to end: {s}")
    assert s["forms"] >= 40 and s["cv_r_forms"] >= 30
    assert s["shown_cv_r"] == 0, s.get("shown_cv_r_by")
    assert s["on_disk"] == 0
