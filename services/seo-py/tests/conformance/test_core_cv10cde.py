"""core-spec/2 CV-10c / CV-10d / CV-10e (card C2-13): the frozen split set FS, the K-1 families under the N-1 disguise
set, and the end-to-end citation run with VG values. Zero tolerance; counts and ids only, never a value.

CV-10c  FS = gen_split_v2.accepted(cv_shape.fragment, cv_shape.valueish). shown = code points of the joined value's
        CV-2 core (the first piece without its leading W_L, the last piece without its trailing W_T, every inner
        piece whole) outside cv_shape.value_spans(text). CI lane (G-2) = 20% of FS: the cells of the values with
        vi % 5 == 0 of every seed. Heavy lane (CONFORMANCE_HEAVY=1) = all of FS + R-split size (section 11).
CV-10d  per K-1 family (26) >= 2,000 synthetic values (tests/leak_corpus_v2.values + C2-13 generators for the five
        families the corpus has no key for), each under the N-1 disguise set (leak_corpus_v2.char_disguises +
        PAIRS = 51 disguises) in the corpus carrier. shown = the owner's display (callum._display, the CV-5 path)
        does not alert, or alerts with value_shape spans and an alphanumeric code point of the disguised value lies
        outside every span. Each family uses its first N_K values. CI = every 2nd of them (1,000 per family),
        each under one disguise (round robin: every disguise drawn >= 19 times per family; ~26,000 cells, ~3 ms
        each); heavy = the full cross 26 x 2,000 x 51, shardable by CV10D_SHARD="i/n" (by family index).
CV-10e  v1 P-4 kept (test_core_e2e.e2e_forms = the SP label forms of the value matrix generator) with VG values in
        answer, title and hint (3 values per form): shown to the owning tenant 0 for EVERY form; readable in the
        data directory (ledger included), the logs and any non-2xx body 0."""
import json
import logging
import os
import random
import string

import pytest

import leak_corpus_v2 as lc
from conformance import gen_split_v2 as FS
from conformance import gen_values_v2 as VG

pytestmark = pytest.mark.conformance
HEAVY = os.environ.get("CONFORMANCE_HEAVY") == "1"
CI_MOD = 5                                              # G-2: 20% of FS
N_K = 2_000                                             # CV-10d: values per K-1 family
B62 = string.ascii_letters + string.digits


@pytest.fixture(scope="module")
def cv():
    from agents import cv_shape
    return cv_shape


# ---------------------------------------------------------------- CV-10c
def fs_shown(c: dict, cv) -> int:
    """Code points of the value's CV-2 core outside every masked span (0 = fully masked). CV-7 / CV-5 (C2-13b,
    owner decision 11-15 10:00): a text with a client-view finding other than value_shape (e.g. a K-5 e-mail token, which
    CV-7 hands to K-5 so CV-3 / CV-4 never mask it) is masked WHOLE by callum._display, so nothing of it is shown."""
    from agents import secret_guard as sg
    if set(sg.client_view_findings(c["text"])) - {"value_shape"}:
        return 0
    spans = cv.value_spans(c["text"])
    pos, k = [], c["span"][0]
    pieces, seps = c["pieces"], c["seps"] + [""]
    for i, p in enumerate(pieces):
        a, b = 0, len(p)
        if i == 0:
            while a < b and p[a] in cv.W_L:
                a += 1
        if i == len(pieces) - 1:
            while b > a and p[b - 1] in cv.W_T:
                b -= 1
        pos.extend(range(k + a, k + b))
        k += len(p) + len(seps[i])
    return sum(1 for x in pos if not any(s <= x < e for s, e in spans))


def fs_pass(cv, ci: bool) -> dict:
    out = {"candidates": 0, "accepted": 0, "rejected": 0, "rejected_structural": 0, "shown_cells": 0,
           "shown_cp": 0, "by": {}}
    for c in FS.candidates():
        if ci and c["vi"] % CI_MOD:
            continue
        out["candidates"] += 1
        ok, st = FS.keep(c, cv.fragment, cv.valueish)
        out["rejected_structural"] += not st
        if not ok:
            out["rejected"] += 1
            continue
        out["accepted"] += 1
        n = fs_shown(c, cv)
        if n:
            out["shown_cells"] += 1
            out["shown_cp"] += n
            key = f"k{c['k']}|sep{c['sep']}|{c['mode']}|{c['placement']}"
            out["by"][key] = out["by"].get(key, 0) + 1
    return out


def test_core_CV_10c_fs_ci_twenty_percent_shown_zero(cv):
    r = fs_pass(cv, ci=True)
    print(f"CV-10c CI (vi % {CI_MOD} == 0): " + json.dumps({k: v for k, v in r.items() if k != "by"})
          + f" by={sorted(r['by'].items())[:20]}")
    assert r["candidates"] == FS.N_CANDIDATES // CI_MOD
    assert r["accepted"] > 0 and r["accepted"] + r["rejected"] == r["candidates"]
    assert r["shown_cells"] == 0, sorted(r["by"].items())[:20]


@pytest.mark.skipif(not HEAVY, reason="heavy lane (CONFORMANCE_HEAVY=1): full FS")
def test_core_CV_10c_fs_full_shown_zero_and_r_split_sized(cv):
    r = fs_pass(cv, ci=False)
    sel = FS.select(cv.fragment, cv.valueish)
    print("CV-10c FULL: " + json.dumps({k: v for k, v in r.items() if k != "by"})
          + f" sha256_accepted={sel['sha256_accepted']} R-split={sel['rejected']} by={sorted(r['by'].items())[:20]}")
    assert r["candidates"] == FS.N_CANDIDATES == sel["candidates"]
    assert (sel["accepted"], sel["rejected"]) == (r["accepted"], r["rejected"])
    assert r["shown_cells"] == 0, sorted(r["by"].items())[:20]


# ---------------------------------------------------------------- CV-10d
# leak_corpus_v2 key -> K-1 family (by the family's own regex; asserted per value)
KEY_FAMILY = {
    "aws_access_key_id": "aws_access_key_id", "aws_session_id": "aws_access_key_id", "openai_proj": "openai_anthropic",
    "anthropic": "openai_anthropic", "github_classic": "github", "github_fine": "github", "stripe_live": "stripe",
    "stripe_whsec": "stripe", "slack_bot": "slack", "slack_refresh": "slack", "google_api": "google",
    "google_oauth": "google", "google_client_secret": "google", "shopify_access": "shopify",
    "shopify_shared": "shopify", "amazon_lwa": "amazon_lwa", "azure_account_key": "azure", "azure_sas": "azure",
    "square": "square", "vault": "vault", "discord_bot": "discord", "mapbox": "mapbox", "jwt": "jwt",
    "sendgrid": "sendgrid", "npm": "npm_pypi", "huggingface": "huggingface", "perplexity": "perplexity",
    "gitlab": "gitlab", "twilio": "twilio", "pem": "pem_pgp", "db_postgres": "db_uri", "db_mongo": "db_uri",
    "db_redis": "db_uri"}
MISC_PREFIXES = ("lin_api_", "ntn_", "xai-", "gsk_", "r8_", "glc_", "sntrys_", "figd_", "rnd_", "tskey-",
                 "pscale_tkn_", "dp.pt.", "fo1_", "phc_", "phx_")


def _mixed(r: random.Random, n: int, alpha: str = B62) -> str:
    while True:
        s = "".join(r.choice(alpha) for _ in range(n))
        if any(c.isdigit() for c in s) and any(c.isupper() for c in s) and any(c.islower() for c in s):
            return s


def _own(fam: str, r: random.Random) -> str:
    """C2-13 generators for the K-1 families leak_corpus_v2 has no key for (synthetic, built from the family's own
    regex in secret_guard.PROVIDERS)."""
    if fam == "sk_generic":
        return "sk-" + "z" + _mixed(r, r.randint(22, 40), B62 + "_-")
    if fam == "mailgun":
        return "key-" + _mixed(r, 32)
    if fam == "digitalocean":
        return r.choice(("dop", "doo", "dor")) + "_v1_" + "".join(r.choice("0123456789abcdef") for _ in range(64))
    if fam == "misc_saas":
        return r.choice(MISC_PREFIXES) + _mixed(r, r.randint(20, 40), B62 + "_")
    raise KeyError(fam)


def k_values(sg) -> dict:
    """family -> >= N_K values (deterministic; values only in memory)."""
    fams = {n: [] for n, _rx in sg.PROVIDERS}
    rx = dict(sg._PREFIX_EACH)
    for i in range(N_K):
        for key, (v, _kind) in lc.values(random.Random(f"core-spec/2:CV-10d:{i}")).items():
            if key in KEY_FAMILY:
                fams[KEY_FAMILY[key]].append(v)
        r = random.Random(f"core-spec/2:CV-10d:own:{i}")
        for fam in ("sk_generic", "mailgun", "digitalocean", "misc_saas"):
            fams[fam].append(_own(fam, r))
    bad = sorted({f for f, vs in fams.items() for v in vs if not rx[f].search(v)})
    assert not bad, bad                                  # every value belongs to its family
    return fams


def disguises(v: str) -> list:
    d = lc.char_disguises(v)
    out = [(n, x, "Z Best Media note: {} end.") for n, x in d.items()]
    out += [(f"{a}+{b}", lc.char_disguises(d[a])[b], "Z Best Media: {} ok") for a, b in lc.PAIRS]
    return out


def k_shown(display, carrier_t: str, dv: str) -> bool:
    text = carrier_t.format(dv)
    out = display("answer", text)
    if not out["alert"]:
        return True
    if out.get("check") != "value_shape":
        return False                                     # masked whole
    a = carrier_t.index("{}")
    spans = [(s["start"], s["end"]) for s in out["spans"]]
    return any(ch.isalnum() and not any(s <= a + i < e for s, e in spans) for i, ch in enumerate(dv))


def k_pass(full: bool) -> dict:
    from agents import callum
    from agents import secret_guard as sg
    fams = k_values(sg)
    out = {"families": 0, "values_min": min(len(v) for v in fams.values()), "cells": 0, "shown": 0, "by": {},
           "disguises_per_family_min": 10 ** 9}
    si, sn = (int(x) for x in os.environ.get("CV10D_SHARD", "0/1").split("/")) if full else (0, 1)
    for fi, (fam, vs) in enumerate(fams.items()):
        if fi % sn != si:
            continue
        out["families"] += 1
        seen = set()
        for i, v in enumerate(vs[:N_K] if full else vs[:N_K:2]):
            ds = disguises(v)
            pick = ds if full else [ds[i % len(ds)]]
            for name, dv, t in pick:
                seen.add(name)
                out["cells"] += 1
                if k_shown(callum._display, t, dv):
                    out["shown"] += 1
                    out["by"][f"{fam}|{name}"] = out["by"].get(f"{fam}|{name}", 0) + 1
        out["disguises_per_family_min"] = min(out["disguises_per_family_min"], len(seen))
    out["n_disguises"] = len(disguises("Ab1-xyz9"))
    return out


def test_core_CV_10d_k_families_ci_shown_zero():
    r = k_pass(full=False)
    print("CV-10d CI: " + json.dumps({k: v for k, v in r.items() if k != "by"}) + f" by={sorted(r['by'].items())[:20]}")
    assert r["families"] == 26 and r["values_min"] >= N_K
    assert r["disguises_per_family_min"] == r["n_disguises"] == 51
    assert r["shown"] == 0, sorted(r["by"].items())[:20]


@pytest.mark.skipif(not HEAVY, reason="heavy lane (CONFORMANCE_HEAVY=1): full K x N-1 cross")
def test_core_CV_10d_k_families_full_cross_shown_zero():
    r = k_pass(full=True)
    print("CV-10d FULL: " + json.dumps({k: v for k, v in r.items() if k != "by"}) + f" by={sorted(r['by'].items())[:20]}")
    si, sn = (int(x) for x in os.environ.get("CV10D_SHARD", "0/1").split("/"))
    assert r["families"] == len(range(si, 26, sn)) and r["values_min"] >= N_K
    assert r["cells"] == r["families"] * N_K * 51
    assert r["shown"] == 0, sorted(r["by"].items())[:20]


# ---------------------------------------------------------------- CV-10e
def test_core_CV_10e_end_to_end_vg_values_shown_zero_and_nothing_readable(tmp_path, caplog):
    import test_vault_wiring as vw
    from conformance.test_core_e2e import e2e_forms
    from helpers import rid
    from ports import ProviderAnswer
    caplog.set_level(logging.DEBUG)
    forms = e2e_forms()
    pool = [v for s in VG.SEEDS for v in VG.values(s, len(forms))]
    answers, values = [], []
    for i, (sp, eid, label, _cv) in enumerate(forms):
        url = f"https://site.test/guides/{vw.MARK}-vg-{i}"
        vals = [pool[(3 * i + k) % len(pool)] for k in range(3)]
        a = ProviderAnswer("ANSWER", f"Z Best Media sellers note {i}. {label}: {vals[0]}", [url], "m", "1",
                           cite_from_text=False, provider="openai", asked_at="2026-10-09T18:00:00Z")
        a.citation_titles = {url: f"{label} = {vals[1]}"}
        a.citation_hints = {url: f"{label} is {vals[2]}"}
        answers.append(a)
        values.append((sp, eid, vals))
    h = vw.harness(tmp_path)
    port = vw.Port(answers)
    h.ports.engines["openai"] = port
    bodies = []

    def ok(resp, code=200):
        if resp.status_code >= 300:
            bodies.append(resp.text)
        assert resp.status_code == code, resp.status_code      # (no body in the message: it could carry a value)
        return resp.json()
    ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [vw.DOMAIN]}, andre=True))
    psid = ok(h.post("/tenants/zbm/prompt-sets", {
        "request_id": rid(), "name": "core", "brand_terms": ["Z Best Media"], "competitor_domains": ["competitor.test"],
        "prompts": [f"who recovers marketplace revenue, question {i}" for i in range(-(-len(forms) // 3))],
        "engines": ["openai"]}), 201)["prompt_set_id"]
    run = ok(h.post("/tenants/zbm/citation-runs", {"request_id": rid(), "domain": vw.DOMAIN, "prompt_set_id": psid}),
             201)
    assert run["status"] == "completed" and port.asked >= len(forms)
    # Re-pin (G-7, lead ruling 10:05 PT F10b-1 / V-4 D-02h, C2-10d F10d-1): the decrypted run view is read by the
    # owner caller with tenant authentication (hub + tenant token); dashboard now gets the code-only view.
    view = ok(h.get(f"/tenants/zbm/citation-runs/{run['citation_run_id']}", caller="hub", tenant="zbm"))
    shown_blob = json.dumps(view, ensure_ascii=False) + json.dumps(view)
    disk = vw.written(h, caplog)
    err = "\n".join(bodies)
    out = {"forms": len(forms), "values": 3 * len(forms), "shown": 0, "on_disk_or_logs": 0, "error_bodies": len(bodies),
           "in_error_bodies": 0, "by": []}
    for sp, eid, vals in values:
        n = sum(1 for v in vals if v in shown_blob)
        out["shown"] += n
        if n:
            out["by"].append(f"SP{sp:02d}|{eid}|{n}")
        out["on_disk_or_logs"] += sum(1 for v in vals if v in disk)
        out["in_error_bodies"] += sum(1 for v in vals if v in err)
    print("CV-10e: " + json.dumps({k: v for k, v in out.items() if k != "by"}) + f" by={out['by'][:20]}")
    assert out["forms"] >= 40 and len({v for _s, _e, vs in values for v in vs}) == out["values"]
    assert out["shown"] == 0, out["by"][:20]
    assert out["on_disk_or_logs"] == 0 and out["in_error_bodies"] == 0
