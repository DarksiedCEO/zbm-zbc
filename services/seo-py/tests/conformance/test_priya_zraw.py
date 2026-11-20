"""priya-spec/2 section 4 Z-RAW (no residual) through the real service path, Wave-1 D-05 scanned forms, and the Wave-1
D-08 canary on every Priya route.

Service path: fake Search Console AND Bing rows -> POST /tenants/acme/keyword-ledger -> the POST run response (ledger
rows, frames, derived variants, hand-off, withheld view), the run views, the audit export, every file under the data
directory, every log record at every level and the ledger events (Z-1). For every WITHHELD case (function level,
priya_common.readable, T empty) its raw text, its key and, for PERSONAL corpora, its planted value are searched at
every output in raw, lower, upper, hex, base64 + base64url at 3 alignments, percent-encoded, JSON-escaped and
digits-only form. Bar: 0 hits.

Scope: CI lane = every case of the CI-lane draw of every PERSONAL corpus and every case of every OQ corpus (one node
per corpus); heavy lane = a seeded sample of >= 12,000 cases over the heavy PERSONAL corpora (PRIYA_ZRAW_SEED).
Readings (interpretations I-30..I-32, lead confirms): I-30 the generators do not export the planted value; for a
PERSONAL case it is the case text itself (raw + key) plus its digits-only run and every key token of >= 6 characters.
I-31 store codes ``{c:kind:...}`` are vault output (ciphertext-derived) and are cut from the scanned text before the
search (no short probe is matched against ciphertext); digits-only probes need >= 7 digits and a non-digit boundary.
I-32 run-time collision check: a form that is a substring of a READABLE row text of the same run, of a reason code or
of the service's own constant output (a baseline run) is not a leak and is counted as "collided", never as a hit.
R-Z1 (lead ruling GATE-2Z, 2026-11-15): spec 4 l.175 covers deterministic text only. A real leak is deterministic in the
input, so a batch with a hit is run once more through a fresh service instance and a fresh data directory: a form found
again is a HIT; a form not found again whose every original occurrence lies wholly inside a hex token of >= 12 chars
that is absent from the rerun (a run-random service id / digest) is "collided_random" (counted apart, case ids only);
else a HIT. The detectors and forms are unchanged; the bar stays hits == 0.
R-Z1a (lead ruling GATE-2Z3, 2026-11-16): a canonical UUID (8-4-4-4-12 hex with dashes) is ONE random token for that
test (hex length 32); a form found again on the rerun is still a HIT. Every HIT / collided_random record names its
reason (reappeared-on-rerun | outside-random-token | inside-random-token), the output, the form kind and the form
length, never the form.
Counts are printed, never query text."""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
from urllib.parse import quote, quote_plus

import pytest
from bing_fakes import FakeBing
from bing_fakes import row as bing_row
from corpora import CI_STRIDE, HEAVY, ORDINARY, PERSONAL, ci_rows, rows
from gsc_fakes import FakeGoogle
from helpers import rid
from priya_common import readable
from test_gsc import PROP, bind, gsc_harness
from test_priya import VAULT_ENV

from agents import priya

pytestmark = pytest.mark.conformance

BATCH = 900  # distinct keys per run: below priya.ROWS_CAP (dropped_past_cap checked 0)
HEAVY_N = 12000
SEED = int(os.environ.get("PRIYA_ZRAW_SEED", "20261114"))
_CODE = re.compile(r"\{c:[a-z_]+:[^}]*\}")
MIN_TEXT, MIN_DIGITS, G = 6, 7, 6
_HEX = re.compile(r"[0-9a-f]{12,}")  # R-Z1: a run-random service id / digest token
_UUID = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])")  # R-Z1a
EVIDENCE = os.environ.get("PRIYA_ZRAW_EVIDENCE")  # optional dir: one json per node (counts + case ids, no values)


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    # P2-5 / V-2: synthetic vault key (F-7: the service refuses 503 VAULT_UNAVAILABLE without one)
    for k, v in VAULT_ENV.items():
        monkeypatch.setenv(k, v)


def forms(s: str) -> tuple[set, set]:
    """(text forms, digits-only forms) of one value: the Wave-1 D-05 scanned forms."""
    out = {s, s.lower(), s.upper(), json.dumps(s)[1:-1], json.dumps(s, ensure_ascii=False)[1:-1],
           quote(s, safe=""), quote_plus(s)}
    b = s.encode("utf-8", "surrogatepass")
    out |= {b.hex(), b.hex().upper()}
    for o in range(3):  # 3 alignments: the part of the encoding fed only by this value, wherever it sits
        c = b[o:]
        c = c[: len(c) // 3 * 3]
        if c:
            e = base64.b64encode(c).decode()
            out |= {e, base64.urlsafe_b64encode(c).decode()}
    digits = "".join(ch for ch in s if "0" <= ch <= "9")
    return {f for f in out if len(f) >= MIN_TEXT}, ({digits} if len(digits) >= MIN_DIGITS else set())


def _kinds(s: str) -> dict:
    """form -> kind labels of one value (mirrors forms(); labels only, for the R-Z1a records)."""
    b = s.encode("utf-8", "surrogatepass")
    lab = [("raw", s), ("lower", s.lower()), ("upper", s.upper()), ("json", json.dumps(s)[1:-1]),
           ("json_u", json.dumps(s, ensure_ascii=False)[1:-1]), ("pct", quote(s, safe="")),
           ("pct_plus", quote_plus(s)), ("hex", b.hex()), ("hex_upper", b.hex().upper())]
    for o in range(3):
        c = b[o:]
        c = c[: len(c) // 3 * 3]
        if c:
            lab += [(f"b64_a{o}", base64.b64encode(c).decode()), (f"b64url_a{o}", base64.urlsafe_b64encode(c).decode())]
    out: dict = {}
    for k, f in lab:
        out.setdefault(f, set()).add(k)
    return out


def form_kind(key: str, q: str, personal: bool, f: str, dig: bool) -> str:
    """R-Z1a: the kind of a scanned form (value source : encoding), never the form itself."""
    if dig:
        return "digits"
    vals = [("q", q), ("key", key)] + ([("key_token", t) for t in key.split() if len(t) >= MIN_TEXT] if personal else [])
    return "|".join(sorted({f"{src}:{k}" for src, v in vals for k in _kinds(v).get(f, ())})) or "unknown"


def needles(key: str, q: str, personal: bool) -> tuple[set, set]:
    vals = {q, key}
    if personal:
        vals |= {t for t in key.split() if len(t) >= MIN_TEXT}
    t, d = set(), set()
    for v in vals:
        a, b = forms(v)
        t |= a
        d |= b
    if personal:
        dd = "".join(ch for ch in q if "0" <= ch <= "9")
        if len(dd) >= MIN_DIGITS:
            d.add(dd)
    return t, d


class Scan:
    """A scanned output text with store codes cut (I-31) and a 6-gram prefilter."""

    def __init__(self, text: str):
        self.text = _CODE.sub("\x00", text)
        self.grams = {self.text[i : i + G] for i in range(len(self.text) - G + 1)}

    def has(self, f: str) -> bool:
        return f[:G] in self.grams and f[-G:] in self.grams and f in self.text

    def has_digits(self, d: str) -> bool:
        return self.has(d) and re.search(r"(?<![0-9])" + d + r"(?![0-9])", self.text) is not None


def _found(s: Scan, f: str, dig: bool) -> bool:
    return s.has_digits(f) if dig else s.has(f)


def _random_tok(text: str, f: str, dig: bool, rerun: str) -> str | None:
    """R-Z1 / R-Z1a: "hex12" or "uuid" when every occurrence of f lies wholly inside a hex token (>= 12) or a canonical
    UUID that the rerun does not hold ("uuid" when some occurrence needed a UUID span); else None."""
    pat = re.compile("(?<![0-9])" + re.escape(f) + "(?![0-9])" if dig else "(?=" + re.escape(f) + ")")
    toks = [(m.start(), m.end(), m.group(), "hex12") for m in _HEX.finditer(text)]
    toks += [(m.start(), m.end(), m.group(), "uuid") for m in _UUID.finditer(text)]
    pos = [m.start() for m in pat.finditer(text)]
    used = set()
    for p in pos:
        t = next((t for t in toks if t[0] <= p and p + len(f) <= t[1] and t[2] not in rerun), None)
        if t is None:
            return None
        used.add(t[3])
    return ("uuid" if "uuid" in used else "hex12") if pos else None


def service_run(tmp_path, caplog, items: list) -> tuple:
    """One service run over (key, raw) items. Returns (ledger, {output: text}) over every Z-1 output."""
    fake = FakeGoogle()
    fake.queries = [(q, 1 + i % 7, 1000 + i, 3.0) for i, (_k, q) in enumerate(items)]
    fake.dates_clicks = (10, 150)
    h = gsc_harness(tmp_path, fake, data_dir=str(tmp_path / "data"), SEO_GSC_MAX_ROWS="250000",
                    SEO_GSC_PAGE_ROWS="25000", SEO_GSC_MAX_PAGES="12")
    bing = FakeBing()
    bing.rows = [bing_row(q, 1, 20, i % 2) for i, (_k, q) in enumerate(items)]
    h.ports.first_party["bing_webmaster"] = bing.client()
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    out = h.ok(h.post("/tenants/acme/keyword-ledger", {"request_id": rid(), "sources": ["gsc", "bing"],
                                                       "bing_site_domain": "acme.test"}, caller="seo_agent"), 201)
    views = [out, h.ok(h.get("/tenants/acme/keyword-ledger-runs", caller="seo_agent")),
             h.ok(h.get("/tenants/acme/keyword-ledger-runs")), h.ok(h.get("/audit/export", params={"limit": 1000}))]
    errors = [h.get("/tenants/acme/keyword-ledger-runs", caller="compliance_38").text,
              h.post("/tenants/acme/keyword-ledger", {"request_id": rid(), "sources": ["gsc"]}, caller="dashboard").text,
              h.post("/tenants/acme/keyword-ledger", {"request_id": rid(), "sources": ["nope"]}, caller="seo_agent").text,
              h.get("/tenants/acme/audits").text, h.get("/audit/integrity").text]
    blob = {"api": json.dumps(views, ensure_ascii=False), "errors": "\n".join(errors),
            "logs": "\n".join(r.getMessage() + " " + json.dumps(r.__dict__.get("payload"), default=str)
                              for r in caplog.records) + caplog.text,
            "events": json.dumps([e.get("_payload") for e in h.ledger.events], ensure_ascii=False, default=str),
            "disk": "\n".join(p.read_bytes().decode("utf-8", "replace") for p in (tmp_path / "data").rglob("*")
                              if p.is_file())}
    return out["ledger"], blob


def zraw(tmp_path, caplog, name: str, cases, personal: bool) -> dict:
    """Run every case through the service in batches; count scanned cases / outputs, hits and collisions."""
    seen: dict = {}
    for key, q in cases:
        k = priya.normalize(q)
        if k and k not in seen:
            seen[k] = q
    items = list(seen.items())
    st = {"cases": len(items), "withheld": 0, "runs": 0, "outputs": 0, "forms": 0, "collided": 0, "hits": 0,
          "svc_readable_withheld": 0, "dropped_past_cap": 0, "not_ok": 0, "reruns": 0, "collided_random": 0}
    bad: dict = {}
    rnd_cases: set = set()
    recs: list = []  # R-Z1a: one record per (case, form, output): verdict, reason, output, kind, len - never the form
    consts = "\n".join(priya.REASON_CODES)
    for j in range(0, len(items), BATCH):
        part = items[j : j + BATCH]
        d = tmp_path / f"b{j}"
        d.mkdir()
        led, blob = service_run(d, caplog, part)
        st["runs"] += 1
        st["not_ok"] += led["status"] not in ("OK", "EMPTY_MEASURED")  # all-WITHHELD batch: EMPTY_MEASURED
        st["dropped_past_cap"] += led["counts"]["rows_dropped_past_cap"]
        shown = {r["query"]["text"] for r in led["rows"]}
        withheld = [(k, q) for k, q in part if not readable(q)]
        st["withheld"] += len(withheld)
        st["svc_readable_withheld"] += len(shown & {k for k, _q in withheld})
        benign = Scan("\n".join(sorted(shown)) + "\n" + consts + "\n" + _BASE.get("text", ""))
        scans = {o: Scan(t) for o, t in blob.items()}
        st["outputs"] += len(scans)
        pend = []  # (case id, form, digits?, outputs holding it)
        for i, (k, q) in enumerate(part):
            if readable(q):
                continue
            tf, df = needles(k, q, personal)
            for f, dig in [(f, False) for f in tf] + [(f, True) for f in df]:
                st["forms"] += 1
                if _found(benign, f, dig):
                    st["collided"] += 1
                    continue
                outs = [o for o, s in scans.items() if _found(s, f, dig)]
                if outs:
                    pend.append((j + i, f, dig, outs, k, q))
        if not pend:
            continue
        d2 = tmp_path / f"b{j}_rerun"  # R-Z1: fresh service instance + fresh data dir, same batch
        d2.mkdir()
        _led2, blob2 = service_run(d2, caplog, part)
        st["reruns"] += 1
        scans2 = [Scan(t) for t in blob2.values()]
        rerun = "\n".join(s.text for s in scans2)
        for cid, f, dig, outs, k, q in pend:
            again = any(_found(s, f, dig) for s in scans2)
            tok = {o: None if again else _random_tok(scans[o].text, f, dig, rerun) for o in outs}
            rec = {"case": cid, "kind": form_kind(k, q, personal, f, dig), "len": len(f)}
            if not again and all(tok.values()):
                st["collided_random"] += len(outs)
                rnd_cases.add(cid)
                recs += [dict(rec, verdict="collided_random", reason="inside-random-token:" + tok[o], output=o)
                         for o in outs]
                continue
            st["hits"] += len(outs)
            for o in outs:
                bad[o] = bad.get(o, 0) + 1
                why = ("reappeared-on-rerun" if again else "outside-random-token" if tok[o] is None
                       else "outside-random-token:other-output")
                recs.append(dict(rec, verdict="HIT", reason=why, output=o))
    hit_recs = [r for r in recs if r["verdict"] == "HIT"]
    rnd_recs = [r for r in recs if r["verdict"] != "HIT"]
    print(f"Z-RAW {name}: " + " ".join(f"{a}={b}" for a, b in st.items()) + f" hits_by_output={bad}"
          f" collided_random_cases={sorted(rnd_cases)} hit_records={hit_recs}"
          f" collided_random_records={rnd_recs[:20]}" + (f" (+{len(rnd_recs) - 20} more)" if len(rnd_recs) > 20 else ""))
    if EVIDENCE:
        with open(os.path.join(EVIDENCE, f"zraw_{name}.json"), "w", encoding="utf-8") as fh:
            json.dump({"name": name, "counts": st, "hits_by_output": bad, "records": recs,
                       "collided_random_cases": sorted(rnd_cases)}, fh, sort_keys=True)
    return st


_BASE: dict = {}


@pytest.fixture(autouse=True)
def _baseline(tmp_path_factory, caplog, vault_key):
    if "text" not in _BASE:  # once per process: the service constants (one READABLE + one WITHHELD synthetic row,
        # so the withheld-only field names, e.g. the withheld view's "family", are constants too)
        w = "zq0b7e@k9f1.example"
        _led, blob = service_run(tmp_path_factory.mktemp("zraw_base"), caplog,
                                 [("running shoes", "running shoes"), (priya.normalize(w), w)])
        _BASE["text"] = "\n".join(blob.values())
    yield


def check(st: dict) -> None:
    assert st["runs"] and st["not_ok"] == 0 and st["dropped_past_cap"] == 0, st
    assert st["svc_readable_withheld"] == 0, st
    assert st["hits"] == 0, st


@pytest.mark.parametrize("name", sorted(n for n in PERSONAL if CI_STRIDE.get(n, 1)))
def test_ZRAW_personal_ci_draw(name, tmp_path, caplog):
    cases = [(k, q) for _i, k, q in ci_rows(name)]
    st = zraw(tmp_path, caplog, name, cases, personal=True)
    check(st)


@pytest.mark.parametrize("name", sorted(ORDINARY))
def test_ZRAW_every_OQ_case(name, tmp_path, caplog):
    st = zraw(tmp_path, caplog, name, rows(name), personal=False)
    assert st["withheld"] > 0, st  # the scan bites: every OQ corpus holds WITHHELD cases
    check(st)


@pytest.mark.conformance_heavy
def test_ZRAW_heavy_seeded_personal_sample(tmp_path, caplog):
    rng, per, cases = random.Random(SEED), -(-HEAVY_N * 21 // 20 // len(HEAVY)), []  # 5% over: keys repeat
    for name in HEAVY:  # reservoir sample per heavy corpus, same seed -> same cases
        res: list = []
        for i, (k, q) in enumerate(rows(name)):
            if len(res) < per:
                res.append((k, q))
            elif (j := rng.randrange(i + 1)) < per:
                res[j] = (k, q)
        cases += res
    print(f"Z-RAW heavy seed={SEED} sampled={len(cases)}")
    st = zraw(tmp_path, caplog, "heavy_sample", cases, personal=True)
    assert st["cases"] >= HEAVY_N, st  # distinct keys scanned
    check(st)


def test_D08_canary_on_all_priya_routes(tmp_path, caplog):
    """Wave-1 D-08 for Priya: a synthetic canary (personal-shaped, WITHHELD) planted in the Search Console AND Bing
    data reaches no Priya route body (POST run, run views by every caller, audits, audit export, integrity, error
    bodies), no log record, no ledger event and no data file, in any D-05 form. Positive control: the scanner finds
    every form of the canary when it is planted into a body."""
    canary = "zq" + os.urandom(5).hex() + "w@k" + os.urandom(3).hex() + ".example"
    assert not readable(canary)
    items = [(priya.normalize(canary), canary), ("running shoes", "running shoes")]
    led, blob = service_run(tmp_path, caplog, items)
    assert led["status"] == "OK" and any(r["query"]["text"] == "running shoes" for r in led["rows"])
    assert len(led["withheld_rows"]) == 1  # the canary row is there, coded
    tf, df = needles(priya.normalize(canary), canary, personal=True)
    for f in tf:  # positive control: every scanned form bites
        assert Scan(json.dumps({"v": f})).has(f)
    h_out = []
    for d in range(2):  # a second run of the same tenant / data dir: the routes now hold two runs
        (tmp_path / f"r{d}").mkdir()
        _led2, b2 = service_run(tmp_path / f"r{d}", caplog, items)
        h_out.append(b2)
    texts = dict(blob)
    for i, b2 in enumerate(h_out):
        texts.update({f"{o}#{i}": t for o, t in b2.items()})
    hits = {o: sum(Scan(t).has(f) for f in tf) + sum(Scan(t).has_digits(f) for f in df) for o, t in texts.items()}
    print(f"D-08 canary: forms={len(tf) + len(df)} outputs={len(texts)} hits={sum(hits.values())}")
    assert sum(hits.values()) == 0, {o: n for o, n in hits.items() if n}
