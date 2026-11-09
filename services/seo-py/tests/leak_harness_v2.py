"""Entry-point harness for the generated leak corpus (ADR 0050 v2): runs every case through every entry point and
returns the leaks per entry point. Used by test_secret_guard_core.py and by the measurement in the ADR."""

from __future__ import annotations

import html
import json
import os
import re
from urllib.parse import unquote

import leak_corpus_v2 as lc

from agents import callum
from agents import secret_guard as sg

PERSONAL_FAMILIES = frozenset({"card_visa", "card_amex", "ssn", "phone_us", "phone_intl", "email"})
TEXT_ENTRY_POINTS = ("check", "redact_text", "guard_value", "guard_key", "guard_list", "guard_set", "guard_bytes",
                     "guard_object", "callum_excerpt", "callum_hint")
URL_ENTRY_POINTS = ("safe_url", "stored_url", "url_has_secret", "guard_url")


class _Obj:
    def __init__(self, s):
        self.s = s

    def __str__(self):
        return self.s


def _forms(blob: str) -> list:
    return [blob, sg.normalise(blob), unquote(blob), html.unescape(blob), unquote(unquote(blob))]


def leaked(out, case, _pre=None) -> str | None:
    """The probe of ``case`` found in ``out`` (as written, normalised or decoded; a 9+ digit value also as digits
    only), or None. ``_pre``: (forms, digits) already computed for a large blob checked against many cases."""
    if _pre is None:
        blob = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
        forms, digits = _forms(blob), re.sub(r"\D", "", blob)
    else:
        forms, digits = _pre
    for p in case.probes:
        if p.isdigit():
            rx = re.compile(r"(?<!\d)" + p + r"(?!\d)")         # a short all-digit probe (a PIN) as a whole number
            if any(rx.search(f) for f in forms):
                return p
        elif any(p in f for f in forms):
            return p
    vd = re.sub(r"\D", "", case.value)
    if len(vd) >= 9 and case.family in PERSONAL_FAMILIES and vd in digits:
        return vd
    return None


def run_text(cases) -> dict:
    out = {k: [] for k in TEXT_ENTRY_POINTS}
    for c in cases:
        t = c.carrier
        if sg.check(t) is None:
            out["check"].append(c.name)
        if leaked(sg.redact_text(t)[0], c):
            out["redact_text"].append(c.name)
        if leaked(sg.guard_record({"field": t}), c):
            out["guard_value"].append(c.name)
        if leaked(sg.guard_record({t: 1}), c):
            out["guard_key"].append(c.name)
        if leaked(sg.guard_record([("x", [t])]), c):
            out["guard_list"].append(c.name)
        if leaked(sg.guard_record({"s": {t}}), c):
            out["guard_set"].append(c.name)
        if leaked(sg.guard_record({"b": t.encode("utf-8")}), c):
            out["guard_bytes"].append(c.name)
        if leaked(sg.guard_record({"o": _Obj(t)}), c):
            out["guard_object"].append(c.name)
        if leaked(callum.excerpt(t, ["Z Best Media"]), c):
            out["callum_excerpt"].append(c.name)
        if leaked(callum._safe_hint(t), c):
            out["callum_hint"].append(c.name)
    return out


def run_urls(cases) -> dict:
    out = {k: [] for k in URL_ENTRY_POINTS}
    for c in cases:
        u = c.carrier
        if leaked(sg.safe_url(u), c):
            out["safe_url"].append(c.name)
        if leaked(callum.stored_url(u), c):
            out["stored_url"].append(c.name)
        if c.family not in PERSONAL_FAMILIES and not sg.url_has_secret(u):
            out["url_has_secret"].append(c.name)                 # a credential URL must never be fetched
        if leaked(sg.guard_record({"url": u}), c):
            out["guard_url"].append(c.name)
    return out


def run_classify(text_cases, url_cases, answer, collected, classify) -> list:
    """Every case through ``callum.classify`` (in memory, resolution off): the text as the answer and the engine's
    hint, the URL as the citation. Returns the names that leak anywhere in the result."""
    bad = []
    for c in text_cases:
        a = answer(text=c.carrier, cites=["https://competitor.test/a"],
                   citation_hints={"https://competitor.test/a": c.carrier})
        if leaked(classify(collected((0, a))), c):
            bad.append(c.name)
    for c in url_cases:
        a = answer(text="Z Best Media is named here.", cites=[c.carrier])
        if leaked(classify(collected((0, a))), c):
            bad.append(c.name)
    return bad


# the service's OWN random values (ids, request ids, hashes and the log chain): removed before probes are searched, so a
# 4-digit PIN probe cannot match inside a random hex id by chance. Only these exact shapes / keys are removed.
_SERVICE_RANDOM = re.compile(
    r"seo-[a-z]{2,8}-[0-9a-f]{40}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|(?<=\")(?:[a-z_]*sha256|[a-z_]*sha|hash|mac|prev|chain|key_id)\":\s?\"[0-9a-f]{16,128}(?=\")")


def _scrub(blob: str) -> str:
    return _SERVICE_RANDOM.sub("~", blob)


def openai_body(te, text: str, url: str, title: str) -> dict:
    b = te.fixture("openai")
    b["output"][1]["content"][0]["text"] = text
    b["output"][1]["content"][0]["annotations"] = [{"type": "url_citation", "start_index": 0, "end_index": 10,
                                                    "url": url, "title": title}]
    b["output"][0]["action"]["sources"] = [{"type": "url", "url": url}]
    return b


def run_service(tmp_path, cases_with_carriers, te, base, service, prompt_set, run, batch: int = 96) -> dict:
    """The REAL service path: each batch is one citation run on a fresh service with a data directory; every case is
    one engine answer (text + cited URL + title). Everything the service wrote (data/seo_log.jsonl and every file in
    the data directory, the ledger payloads, the API response) is searched for each case's probes."""
    leaks: list = []
    runs = 0
    for start in range(0, len(cases_with_carriers), batch):
        chunk = cases_with_carriers[start:start + batch]
        while len(chunk) % 3:
            chunk = chunk + [chunk[-1]]
        d = tmp_path / f"svc{start}"
        d.mkdir(parents=True, exist_ok=True)
        script = [base.ok(openai_body(te, text, url, title)) for _c, text, url, title in chunk]
        h, fakes = service(d, data_dir=str(d / "data"), scripts={"openai": script})
        ps = prompt_set(h, engines=("openai",), prompts=tuple(f"prompt {i} for the corpus" for i in range(len(chunk) // 3)))
        v = h.ok(run(h, ps), 201)
        runs += 1
        assert v["status"] == "completed" and len(fakes["openai"].requests) == len(chunk), (
            v.get("status"), len(fakes["openai"].requests), len(chunk), v["result"]["facts"]["engines"])
        blob = json.dumps(v, ensure_ascii=False) + json.dumps([e["_payload"] for e in h.ledger.events],
                                                             ensure_ascii=False, default=str)
        seen_log = False
        for root, _d, files in os.walk(d / "data"):
            for f in files:
                seen_log = seen_log or f == "seo_log.jsonl"
                with open(os.path.join(root, f), "rb") as fh:
                    raw = fh.read().decode("utf-8", "replace")
                blob += raw + json.dumps([json.loads(x) for x in raw.splitlines() if x.startswith("{")],
                                         ensure_ascii=False)
        assert seen_log, "the run wrote no data/seo_log.jsonl"
        blob = _scrub(blob)
        pre = (_forms(blob), re.sub(r"\D", "", blob))
        for c, _t, _u, _ti in chunk:
            if leaked(blob, c, pre):
                leaks.append(c.name)
    return {"runs": runs, "cases": len(cases_with_carriers), "leaks": sorted(set(leaks))}


def service_cases():
    """(case, answer text, cited URL, title) for every text case and every URL case."""
    out = []
    for c in lc.corpus():
        out.append((c, c.carrier, "https://competitor.test/a", c.carrier))
    for c in lc.url_cases():
        out.append((c, "Z Best Media is named here.", c.carrier, "Source"))
    return out
