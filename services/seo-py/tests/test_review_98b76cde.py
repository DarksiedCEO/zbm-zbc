"""The independent review of commit 98b76cde (the fix round for the review of 2b26d980), closed.

The governing rule is differential, against release 2b26d980 (the last one before the host rule):
  P1  a host that release took in a lane is taken now in that lane (the host rule only ever ADDS host names);
  P2  in the domain REGISTRATION lane, a telephone number / SSN with one to three hex letters attached, a card
      number with letters attached and ``first-last-at-domain.tld`` are refused, as that release refused them.

MUST 1  a sub-domain of a mailbox provider (``finance.yahoo.com``) and a host with an ``at`` label are host names in
        every lane: the two e-mail rules are gone from the host rule; the "@"-less e-mail forms stay on the tenant id
        and the Finance client id. A directory holding such domains keeps working; a citation run citing such hosts
        keeps every ``host``.
MUST 2  the client view masks a credential label and its value whatever the case of the verb between them
        (`` Is ``, `` IS ``, `` Was ``, `` WOULD BE ``), and a label in a cased script in upper case.
MUST 3  the platform-hash exemption of a 9..12 digit run needs 4+ hex letters; ``-at-`` in a DOMAIN is refused at
        registration exactly as at 2b26d980.
SHOULD  the legacy-line count is off the start path; a token split over two labels, 32 hex split by a word label and
        ``<label>-is-<value>`` are refused by the rule; the two surviving mutants are killed.

Every credential-shaped or personal-data-shaped value is generated at run time; no assertion message prints one.
"""

from __future__ import annotations

import collections
import json
import random
import re
import string

import pytest

import helpers
import leak_corpus_v2 as lc
import sink_guard as sg
import svc_tenants
import test_client_view as tcv
import test_review_2b26d980 as prev
import test_sink_audits as sa
import utility_corpus_v2 as uc
from agents import callum, secret_guard
from answer_adapter import ProviderAnswer
from helpers import invoice_id, rid
from ports import Ports

SEED = 20261111
N = 2000
HEXL = "abcdef"
LOWER, DIGITS = string.ascii_lowercase, string.digits
ALNUM = LOWER + DIGITS


@pytest.fixture(autouse=True)
def _fresh_guard_caches():
    prev.clear_guard_caches()
    yield
    prev.clear_guard_caches()


def rs(r, chars: str, n: int) -> str:
    return "".join(r.choice(chars) for _ in range(n))


# ================================================================================================ MUST 1: host names

# real, well-known hosts: sub-domains of mailbox providers / portals, and hosts under the Austrian TLD
PORTAL_HOSTS = """finance.yahoo.com news.yahoo.com sports.yahoo.com search.yahoo.com login.yahoo.com help.yahoo.com
weather.yahoo.com shopping.yahoo.com autos.yahoo.com money.yahoo.com tw.news.yahoo.com hk.finance.yahoo.com
uk.finance.yahoo.com news.yahoo.co.jp finance.yahoo.co.jp auctions.yahoo.co.jp page.auctions.yahoo.co.jp
detail.chiebukuro.yahoo.co.jp store.shopping.yahoo.co.jp basketball.fantasysports.yahoo.com r.search.yahoo.com
uk.news.yahoo.com news.yahoo.co.uk money.msn.com news.msn.com sports.msn.com weather.msn.com zone.msn.com
assets.msn.com outlook.live.com login.live.com account.live.com onedrive.live.com signup.live.com
profile.live.com support.outlook.com answers.outlook.com support.hotmail.com mp.weixin.qq.com weixin.qq.com
news.qq.com v.qq.com im.qq.com y.qq.com sports.qq.com pay.weixin.qq.com open.weixin.qq.com
developers.weixin.qq.com work.weixin.qq.com user.qzone.qq.com view.inews.qq.com blog.naver.com cafe.naver.com
news.naver.com search.naver.com map.naver.com shopping.naver.com smartstore.naver.com n.news.naver.com
m.blog.naver.com finance.naver.com market.yandex.ru news.yandex.ru maps.yandex.ru music.yandex.ru
disk.yandex.ru translate.yandex.ru passport.yandex.ru webmaster.yandex.ru translate.yandex.com
metrica.yandex.com news.mail.ru cloud.mail.ru otvet.mail.ru e.mail.ru hi-tech.mail.ru news.163.com
music.163.com sports.163.com money.163.com c.m.163.com 3g.163.com support.icloud.com help.aol.com
search.aol.com news.aol.com hilfe.gmx.net newsroom.gmx.net produkte.web.de hilfe.web.de suche.web.de
email.t-online.de sportnachrichten.t-online.de crm.zoho.com help.zoho.com accounts.zoho.com books.zoho.com
app.fastmail.com account.proton.me drive.proton.me calendar.proton.me support.mail.com xfinity.comcast.net
business.comcast.net currently.att.net start.att.net account.protonmail.com app.tuta.io app.hey.com
www.orf.at help.orf.at news.orf.at www.derstandard.at www.wien.gv.at www.univie.ac.at www.oesterreich.gv.at
look.at.me.com go.at.example.org arrive.at.station.example.net""".split()
SUBS = ("finance", "news", "sports", "money", "weather", "maps", "docs", "photos", "shopping", "games", "video",
        "answers", "groups", "calendar", "blog", "mp", "support", "help", "login", "accounts", "developer", "status")


def provider_host(r) -> str:
    """A sub-domain of a mailbox provider's domain (one or two labels before it)."""
    p = r.choice(sorted(secret_guard.MAILBOX_PROVIDERS))
    k = r.random()
    if k < .5:
        return f"{r.choice(SUBS)}.{p}"
    if k < .8:
        return f"{r.choice(prev.NEUTRAL_WORDS)}.{r.choice(SUBS)}.{p}"
    return f"{rs(r, LOWER, r.randint(4, 8))}.{rs(r, LOWER, r.randint(4, 8))}.{p}"


def at_label_host(r) -> str:
    """A host with an ``at`` label before its domain."""
    w = prev.NEUTRAL_WORDS
    return r.choice((f"{r.choice(w)}.at.{r.choice(w)}.{r.choice(prev.TLDS)}",
                     f"{r.choice(w)}.{r.choice(w)}.at.{r.choice(w)}.{r.choice(('com', 'org', 'net'))}",
                     f"{rs(r, LOWER, 5)}.{rs(r, LOWER, 6)}.at.{rs(r, LOWER, 7)}.com"))


def lanes(h: str) -> dict:
    """Whether each lane takes host ``h``."""
    u = secret_guard.inspect_url(f"https://{h}/about?x=1")
    return {"rule": sg.host_refusal(h) is None, "sink": sg.classify(h) == "hostname",
            "check": secret_guard.check(h) is None, "url": bool(u) and u["host_kind"] is None,
            "kept_for_the_log": callum.checked_host(h) == {"host": h, "host_withheld": None},
            "url_host": callum.url_host(f"https://{h}/p?x=1") == {"host": h, "host_withheld": None},
            "registration": not svc_tenants.domain_refused(h),
            "ledger": sg.violations(sg.LEDGER + "tenant_domains_set", {"domains": [h]}) == []}


def test_real_portal_hosts_are_host_names_in_every_lane():
    assert len(set(PORTAL_HOSTS)) >= 110
    bad = {h: sorted(k for k, v in lanes(h).items() if not v) for h in PORTAL_HOSTS}
    bad = {h: v for h, v in bad.items() if v}
    assert bad == {}, bad                                    # real, public host names: printing them is fine
    assert all(svc_tenants.valid_domain(h) for h in PORTAL_HOSTS)


@pytest.mark.parametrize("name,gen", [("mailbox_provider_subdomain", provider_host), ("at_label", at_label_host)])
def test_generated_provider_subdomains_and_at_label_hosts_are_taken_in_every_lane(name, gen):
    r = random.Random(f"{SEED}:{name}")
    hosts = sorted({gen(r) for _ in range(N)})
    refused = collections.Counter()
    for h in hosts:
        for lane, ok in lanes(h).items():
            if not ok:
                refused[lane] += 1
    print(f"host false refusals {name}: generated={len(hosts)} refused_by_lane={dict(refused)}")
    assert len(hosts) >= N * 0.6 and not refused, dict(refused)


def test_no_e_mail_reason_is_left_in_the_host_rule():
    assert "email" not in sg.HOST_REASONS and "email" not in secret_guard._HOST_CHECK
    assert not hasattr(sg, "MAILBOX_PROVIDERS") and not hasattr(sg, "HOST_AT_LABEL")
    r = random.Random(SEED)
    reasons = collections.Counter(sg.host_refusal(g(r)) for g in (provider_host, at_label_host) for _ in range(N))
    assert set(reasons) == {None}, dict(reasons)
    # a host is never withheld as personal data for its shape as a host name (PERSONAL_DATA is for digits only)
    for h in PORTAL_HOSTS:
        assert callum.url_host(f"https://{h}/x")["host_withheld"] is None


def test_the_atless_e_mail_forms_stay_on_the_tenant_id_and_the_finance_client_id(tmp_path):
    """The forms 98b76cde added are still refused where they are identifiers; the same text as a DOMAIN is a host."""
    forms = prev.atless_forms()
    assert [k for k, v in forms.items() if secret_guard.identifier_refusal(v) is None] == []
    assert [k for k, v in forms.items() if not svc_tenants.identifier_refused(v, prose=True)] == []
    domains = [v for v in forms.values() if re.fullmatch(prev.models.DOMAIN, v) and svc_tenants.valid_domain(v)]
    assert len(domains) >= 2 and [d for d in domains if svc_tenants.domain_refused(d)] == []
    assert [d for d in domains if secret_guard.identifier_refusal(d, atless=False) is not None] == []
    h = helpers.Harness(tmp_path)
    h.tenant("acme", "client", ("acme.test",))
    tenant_rx = re.compile(prev.models.TENANT)
    tried = 0
    for v in forms.values():
        if tenant_rx.fullmatch(v):
            tried += 1
            h.refused(h.post("/tenants", {"request_id": rid(), "tenant_id": v, "kind": "client"}, andre=True), 422,
                      "IDENTIFIER_WITHHELD")
        if re.fullmatch(prev.models.FINANCE_CLIENT, v):
            tried += 1
            h.refused(h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": v},
                             andre=True), 422, "IDENTIFIER_WITHHELD")
    assert tried >= 8


# ------------------------------------------------------------------------------------------------ P1: the fall-back

def worded_serial_host(r) -> str:
    """A name of words with a serial inside it: a letters-and-digits label of 24+ with two letter<->digit changes,
    which the host rule reads as a token and release 2b26d980 took as a name."""
    w = prev.NEUTRAL_WORDS
    while True:
        label = r.choice(w) + r.choice(w) + str(r.randint(10 ** 6, 10 ** 8 - 1)) + r.choice(w) + r.choice(w)
        if 24 <= len(label) <= 40:
            return label + "." + r.choice(("com", "net", "org", "io"))


def test_a_host_the_rule_refuses_is_judged_as_before_the_rule_in_every_lane(monkeypatch):
    """P1 by construction: every lane answers "the rule takes it, or the checks of release 2b26d980 take it". The
    100,000-host comparison with that release's own code is in the review notes; this pins the mechanism."""
    r = random.Random(SEED + 1)
    hosts = sorted({worded_serial_host(r) for _ in range(600)})
    assert all(sg.host_refusal(h) == "token" for h in hosts)
    legacy = [h for h in hosts if sg._legacy_host_ok(h)]
    print(f"P1 fall-back: hosts={len(hosts)} refused_by_the_rule={len(hosts)} taken_by_the_2b26d980_checks={len(legacy)}")
    assert len(legacy) >= len(hosts) * 0.5
    for h in legacy:
        got = lanes(h)
        assert got == {**got, "rule": False, "sink": True, "check": True, "url": True, "kept_for_the_log": True,
                       "url_host": True, "registration": True, "ledger": True}, sorted(k for k, v in got.items() if not v)
    # every lane is exactly "the rule, or the checks as they were": the strict check ...
    r2 = random.Random(SEED + 2)
    corpus = hosts + [g(r2) for g, _n in prev.HOST_CORPORA.values() for _ in range(150)] + \
        [g() for g in prev.SECRET_HOSTS.values() for _ in range(10)]
    for h in corpus:
        rule = sg.host_refusal(h) is None
        assert (secret_guard.check(h) is None) == (rule or secret_guard._check(h) is None)
        assert (sg.classify(h) == "hostname") == (rule or sg._legacy_host_ok(h))
        old_registration = secret_guard.identifier_refusal(h, atless=False) is None and sg.classify(h) is not None
        assert (not svc_tenants.domain_refused(h)) == ((rule and not secret_guard.email_form(h)) or old_registration)
    # ... and the mutation: without the fall-back the hosts that release took are refused again
    monkeypatch.setattr(sg, "_legacy_host_ok", lambda host: False)
    prev.clear_guard_caches()
    assert sum(1 for h in legacy if sg.classify(h) == "hostname") == 0
    monkeypatch.undo()
    prev.clear_guard_caches()
    assert all(sg.classify(h) == "hostname" for h in legacy)


def test_a_value_ending_in_a_punycode_label_is_judged_as_before_when_the_rule_refuses_it():
    """At 2b26d980 a value whose last label starts ``xn--`` was not a host name: a code, judged by the strict check.
    The rule takes a real IDN TLD; one it does not take is still judged as it was."""
    real = "example." + "xn--" + "\u0440\u0444".encode("punycode").decode("ascii")
    assert sg.host_refusal(real) is None and sg.classify(real) == "hostname"
    odd = "example.xn--zz"
    assert sg.host_refusal(odd) == "tld" and sg._legacy_host_ok(odd) == (sg._check(odd) is None)
    assert (sg.classify(odd) == "hostname") == sg._legacy_host_ok(odd)
    long = "a" * 61 + ".xn--zz"
    assert len(long) > sg.CODE_MAX and sg.classify(long) is None             # too long for a code then, refused now


# ------------------------------------------------------------------------------------------------ the routes

DOMAINS = ["finance.yahoo.com", "acme.at.rugcare.test", "shop.mail.ru", "brand.gmx.net"]
COMPETITORS = ["money.msn.com", "mp.weixin.qq.com", "look.at.example.org", "blog.naver.com", "market.yandex.ru"]
CITED = [f"{s}.{p}" for s in SUBS[:16] for p in ("yahoo.com", "msn.com", "live.com", "qq.com", "naver.com",
                                                 "yandex.ru", "mail.ru")] + \
    [f"{s}.at.example{i}.org" for i, s in enumerate(SUBS[:12])]


class CitingEngine:
    connected = True

    def ask(self, prompt, model=None):
        return ProviderAnswer("ANSWER", "Z Best Media in Long Beach.", [f"https://{h}/page" for h in CITED], "m", "1")


def portal_world(tmp_path, srv) -> helpers.Harness:
    """A durable service whose client tenant's domains and competitor domains are mailbox-provider sub-domains and
    hosts with an ``at`` label."""
    for d in DOMAINS:
        prev.site(srv, d)
    ports = Ports.default()
    ports.fetcher = prev.fetcher_for(srv, DOMAINS)
    ports.engines["openai"] = CitingEngine()
    tmp_path.mkdir(parents=True, exist_ok=True)
    h = helpers.Harness(tmp_path, data_dir=str(tmp_path / "data"), ports=ports, SEO_PROBE_SAMPLES="3")
    h.tenant("acme", "client", tuple(DOMAINS))
    h.ok(h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": "acme-party-1"},
                andre=True))
    return h


def prompt_set(h, name: str = "core") -> str:
    return h.ok(h.post("/tenants/acme/prompt-sets", {
        "request_id": rid(), "name": name, "brand_terms": ["Z Best Media"], "competitor_domains": COMPETITORS,
        "prompts": ["who recovers marketplace revenue"], "engines": ["openai"]}, caller="dashboard"), 201)["prompt_set_id"]


def host_rows(x) -> tuple:
    """(rows whose host is kept, rows with a host withheld, the kept hosts) in a citation run's view."""
    kept, withheld, stack = [], 0, [x]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if "host" in x and "host_withheld" in x:
                if x["host"] is not None:
                    kept.append(x["host"])
                elif x["host_withheld"] is not None:
                    withheld += 1
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return len(kept), withheld, set(kept)


def writes(h, psid: str, n: int) -> None:
    """An audit, a citation run, a schedule with its slot, a log ingest: every one succeeds."""
    a = h.ok(h.post("/tenants/acme/audits", {
        "request_id": rid(), "domain": DOMAINS[0], "scheme": "http", "paths": ["/", "/about"],
        "invoice_id": invoice_id(n), "prompt_set_id": psid}, caller="dashboard", andre=True), 201)
    assert a["status"] == "completed"
    run = h.ok(h.post("/tenants/acme/citation-runs", {"request_id": rid(), "domain": DOMAINS[1], "prompt_set_id": psid,
                                                      "engines": ["openai"]}, caller="dashboard", andre=True), 201)
    assert run["status"] == "completed"
    view = h.ok(h.get(f"/tenants/acme/citation-runs/{run['citation_run_id']}", caller="dashboard"))
    kept, withheld, hosts = host_rows(view)
    assert len(CITED) >= 100 and hosts >= set(CITED) and kept >= len(CITED) and withheld == 0, (kept, withheld)
    h.ok(h.post("/tenants/acme/schedules", {
        "request_id": rid(), "domain": DOMAINS[1 + n % 3], "scheme": "http", "every_days": 7,
        "invoice_id": invoice_id(n + 1), "paths": ["/", "/about"], "prompt_set_id": psid},
        caller="dashboard", andre=True), 201)
    t = sa.tick(h)
    assert t["ran"] >= 1 and t["failed"] == 0 and t["refused"] == 0, {k: v for k, v in t.items() if isinstance(v, int)}
    fin = prev.tl.ingest(h, tid="acme", caller="dashboard", domain=DOMAINS[3])
    iid = h.ok(fin, 201)["ingest_id"]
    line = '66.249.66.1 - - [09/Oct/2026:17:00:01 +0000] "GET /about HTTP/1.1" 200 5 "-" "Mozilla/5.0 (compatible; ' \
           'Googlebot/2.1)"\n'
    import base64
    h.ok(prev.tl.send(h, iid, 1, base64.b64encode(line.encode()).decode(), tid="acme", caller="dashboard"))
    assert h.ok(h.post(f"/tenants/acme/log-ingests/{iid}/finish", {"request_id": rid()},
                       caller="dashboard"))["status"] == "finished"


@pytest.mark.local_http
def test_a_directory_with_portal_and_at_label_domains_keeps_working_across_a_restart(tmp_path, srv, caplog):
    """MUST 1: the tenant of 2b26d980 whose domain is a mailbox provider's sub-domain (or has an ``at`` label) was
    refused every write by 98b76cde (503 at the sink). Audits, citation runs (100+ cited hosts, each kept), schedule
    slots and log ingests succeed, before and after a restart; the same domains are registered again without a 422."""
    h = portal_world(tmp_path, srv)
    psid = prompt_set(h)
    writes(h, psid, 1)
    lines = prev.log_records(h)
    assert all(sg.violations(r["kind"], r["data"]) == [] for r in lines)
    assert all(sg.violations(sg.LEDGER + e["event_type"], e["_payload"]) == [] for e in h.ledger.events)
    logged = set(prev.all_strings([r["data"] for r in lines]))
    assert set(DOMAINS) <= logged                            # the domains are in the log as host names
    h2 = h.restart()
    health = h2.svc.health()
    assert h2.svc.integrity["ok"] and health["status"] == "ok" and health["legacy_prose_lines"] == 0
    h2.clock.advance(days=7)
    writes(h2, psid, 11)
    h2.ok(h2.post("/tenants/acme/domains", {"request_id": rid(), "domains": DOMAINS}, andre=True))
    h2.ok(h2.post("/tenants/acme/domains", {"request_id": rid(), "domains": DOMAINS + ["news.yahoo.com"]}, andre=True))
    prompt_set(h2, "again")
    assert h2.svc.health()["legacy_prose_lines"] == 0
    assert not [r for r in caplog.records if "sink guard" in str(r.msg)]


@pytest.mark.local_http
def test_mutation_with_the_e_mail_host_rules_back_the_same_tenant_is_refused_at_the_sink(tmp_path, srv, monkeypatch):
    """The bug, reproduced by putting the two rules back: the audit of a mailbox provider's sub-domain answers 503."""
    h = portal_world(tmp_path, srv)
    psid = prompt_set(h)
    real = sg._host_judge

    def with_e_mail_rules(s, labels):
        n = len(labels)
        if any(labels[i] == "at" for i in range(1, n - 2)) or any(
                n > k and ".".join(labels[-k:]) in secret_guard.MAILBOX_PROVIDERS for k in (2, 3)):
            return "digits"
        return real(s, labels)
    monkeypatch.setattr(sg, "_host_judge", with_e_mail_rules)
    monkeypatch.setattr(sg, "_legacy_host_ok", lambda host: False)
    prev.clear_guard_caches()
    r = h.post("/tenants/acme/audits", {"request_id": rid(), "domain": DOMAINS[0], "scheme": "http", "paths": ["/"],
                                        "invoice_id": invoice_id(1), "prompt_set_id": psid},
               caller="dashboard", andre=True)
    assert r.status_code == 503
    monkeypatch.undo()
    prev.clear_guard_caches()
    assert h.ok(h.post("/tenants/acme/audits", {
        "request_id": rid(), "domain": DOMAINS[0], "scheme": "http", "paths": ["/"], "invoice_id": invoice_id(1),
        "prompt_set_id": psid}, caller="dashboard", andre=True), 201)["status"] == "completed"


# ================================================================================================ MUST 2: client view

CASED = {"is": ("Is", "IS", "iS"), "was": ("Was", "WAS", "wAs", "waS", "WAs"), "are": ("Are", "ARE", "aRe", "arE"),
         "would be": ("Would be", "WOULD BE", "Would Be", "would Be", "wOuLd bE"),
         "will be": ("Will be", "WILL BE", "Will Be", "wIlL Be"), "remains": ("Remains", "REMAINS", "reMains")}
LATIN_LABELS = [x for x in lc.LABELS if x.isascii()]


def test_a_label_and_its_value_are_masked_whatever_the_case_of_the_verb():
    r = random.Random(SEED + 5)
    missed, n = [], 0
    for verb, forms in CASED.items():
        for form in (verb,) + forms:
            assert form.lower() == verb
            for label in LATIN_LABELS:
                value = rs(r, LOWER, 5) + str(r.randint(10, 99)) + r.choice("!#%")
                n += 1
                if "label_value" not in secret_guard.client_view_findings(f"for the admin the {label} {form} {value} today"):
                    missed.append((form, label))
    print(f"client view verb case: texts={n} missed={len(missed)}")
    assert n >= 800 and missed == [], missed[:10]
    # every case mix of the two-letter verb
    mixes = {"".join(p) for p in ((a, b) for a in "iI" for b in "sS")}
    assert len(mixes) == 4 and all("label_value" in secret_guard.client_view_findings(
        "the pass" + f"word {m} " + rs(r, LOWER, 6) + "7!") for m in mixes)


def test_the_corpus_carries_the_cased_verb_forms_and_the_client_view_masks_them():
    forms = lc.cased_verb_forms("PIN", "0000")
    assert len(forms) == 10 and not set(forms) & set(lc.label_forms("PIN", "0000"))
    cased = [c for c in lc.corpus() if c.disguise in forms]
    assert len(cased) == len(forms) * len(lc.LABELS) >= 400
    assert lc.corpus()[-len(cased):] == cased                 # appended last: no earlier case moved
    assert [c.name for c in cased if not secret_guard.client_view_findings(c.carrier)] == []
    assert [c.name for c in cased if secret_guard.check(c.carrier) is None] == []          # the strict lane too


def test_a_label_in_a_cased_script_is_masked_in_upper_case():
    r = random.Random(SEED + 6)
    cased = [s for s in secret_guard._SCRIPT if s != s.upper() and ("\u0430" <= s[0] <= "\u044f")]
    assert len(cased) >= 2
    for s in cased:
        value = rs(r, LOWER, 6) + "7!"
        for label in (s, s.upper(), s.capitalize()):
            assert "label_value" in secret_guard.client_view_findings(f"{label}: {value}"), len(label)
            assert secret_guard.check(f"{label}: {value}") is not None


def test_prose_stays_shown_and_false_masking_on_neutral_prose_did_not_rise():
    """The cost of MUST 2, measured: neutral prose as written and in Title Case is not masked for a label and a
    value; prose after a capitalised verb is read as prose after the lower-case verb always was."""
    kept = list(tcv.PROSE_KEPT) + ["The Password Would Be Reset After 90 Days.", "Tokens Are Explained Below.",
                                   "The Cookie Policy Was Updated In March.", "Our Secret Remains Quality Above All Else."]
    assert [t for t in kept if secret_guard.client_view_findings(t)] == []
    units = list(uc.ANSWERS) + [x.strip() for a in uc.ANSWERS for x in re.split(r"(?<=[.!?]) ", a) if len(x.strip()) > 8]
    units += [callum._raw_excerpt(uc.ANSWERS[i], [b] if b else []) for i, b, _e in uc.excerpts()]
    units = [u for u in units if isinstance(u, str) and u]
    rates = {}
    for name, f in (("as_written", str), ("title_case", str.title), ("upper_case", str.upper)):
        rates[name] = sum(1 for u in units if "label_value" in secret_guard.client_view_findings(f(u)))
    print(f"client view false masking (label and value) on neutral prose: units={len(units)} masked={rates}")
    assert len(units) >= 600 and rates == {"as_written": 0, "title_case": 0, "upper_case": 0}
    # the same judgement after a verb in any case: `` is `` takes any value of 4+, `` was `` a value-like one
    for verb in ("is", "Is", "IS"):
        assert "label_value" in secret_guard.client_view_findings("the pass" + f"word {verb} required")
    for verb in ("was", "Was"):
        assert secret_guard.client_view_findings("the pass" + f"word {verb} changed") == []


# ================================================================================================ MUST 3 / P2

def phone(r) -> str:
    return r.choice("23456789") + rs(r, DIGITS, 2) + r.choice("23456789") + rs(r, DIGITS, 6)


def ssn(r) -> str:
    return f"{r.randint(1, 665):03d}{r.randint(1, 99):02d}{r.randint(1, 9999):04d}"


def card(r) -> str:
    body = "4" + rs(r, DIGITS, 14)
    return next(body + c for c in DIGITS if secret_guard._luhn(body + c))


def around(r, number: str) -> str:
    k = r.randint(2, 3)
    a = r.randint(1, k - 1)
    return rs(r, HEXL, a) + number + rs(r, HEXL, k - a)


def tail(r) -> str:
    return r.choice(("com", "net", "io", "ngrok.io", "vercel.app", "pages.dev", "example.com", "example.org"))


P2_SHAPES = {
    "phone_hex_before": lambda r: rs(r, HEXL, r.randint(1, 3)) + phone(r) + "." + tail(r),
    "phone_hex_after": lambda r: phone(r) + rs(r, HEXL, r.randint(1, 3)) + "." + tail(r),
    "phone_hex_around": lambda r: around(r, phone(r)) + "." + tail(r),
    "ssn_hex_before": lambda r: rs(r, HEXL, r.randint(1, 3)) + ssn(r) + "." + tail(r),
    "ssn_hex_after": lambda r: ssn(r) + rs(r, HEXL, r.randint(1, 3)) + "." + tail(r),
    "ssn_hex_around": lambda r: around(r, ssn(r)) + "." + tail(r),
    "card_letters_before": lambda r: rs(r, LOWER, r.randint(1, 3)) + card(r) + "." + tail(r),
    "card_letters_after": lambda r: card(r) + rs(r, LOWER, r.randint(1, 3)) + "." + tail(r),
    "first_last_at_domain": lambda r: f"{rs(r, LOWER, 5)}-{rs(r, LOWER, 6)}-at-{rs(r, LOWER, 7)}." + r.choice(("com", "org")),
    "first_dot_last_at_domain": lambda r: f"{rs(r, LOWER, 5)}.{rs(r, LOWER, 6)}-at-{rs(r, LOWER, 7)}." + r.choice(("com", "net")),
}


@pytest.mark.parametrize("name", sorted(P2_SHAPES))
def test_p2_shapes_are_refused_in_the_registration_lane(name):
    r = random.Random(f"{SEED}:p2:{name}")
    hosts = [P2_SHAPES[name](r) for _ in range(N)]
    valid = [h for h in hosts if svc_tenants.valid_domain(h)]
    accepted = sum(1 for h in valid if not svc_tenants.domain_refused(h))
    print(f"P2 {name}: generated={len(hosts)} valid_domains={len(valid)} accepted_at_registration={accepted}")
    assert len(valid) >= N * 0.9 and accepted == 0
    if "at_domain" not in name:                              # a number: refused in the sink lane too, by the rule
        assert sum(1 for h in hosts if sg.host_refusal(h) is None) == 0
        assert sum(1 for h in hosts if sg.classify(h) is not None) == 0
        assert all(sg.host_refusal(h) == "digits" for h in hosts)
    else:                                                    # a host met in a URL or a citation is a host name
        assert all(sg.classify(h) == "hostname" and secret_guard.email_form(h) for h in hosts[:200])


def test_the_hash_exemption_needs_four_hex_letters_and_its_cost_is_measured(monkeypatch):
    assert sg.HOST_HASH_LETTERS == 4 and sg.HOST_HASH_LABEL == (12, 31) and sg.HOST_HASH_RUN_MAX == 12
    nine = "123456789"
    assert sg.host_refusal("ab" + nine + "c.ngrok.io") == "digits"            # 3 letters around a 9-digit run
    assert sg.host_refusal("abc" + nine + ".ngrok.io") == "digits"
    assert sg.host_refusal("ab" + nine + "cd.ngrok.io") is None               # 4 letters: a platform hash label
    assert sg.host_refusal("abcd" + nine + "0123.ngrok.io") == "digits"       # a run of 13: never part of a hash
    r = random.Random(SEED + 7)
    labels = [rs(r, prev.HEX, n) for n in range(6, 32) for _ in range(400)]
    reasons = collections.Counter(sg.host_refusal(x + ".ngrok.io") for x in labels)
    short = [x for x in labels if len(x) < sg.HOST_TOKEN_MIN]
    digits_short = sum(1 for x in short if sg.host_refusal(x + ".ngrok.io") == "digits")
    print(f"platform hash labels (6..31 hex): generated={len(labels)} refused={len(labels) - reasons[None]} "
          f"by_reason={ {k: v for k, v in reasons.items() if k} } | labels of 6..23: generated={len(short)} "
          f"refused_for_digits={digits_short} rate={digits_short / len(short):.4%}")
    assert reasons["digits"] / len(labels) <= 0.02            # measured 1.3-1.5%: over the 1% of the other corpora
    # the mutation: with two letters (98b76cde) the telephone-number shapes pass the rule
    hosts = [P2_SHAPES["phone_hex_around"](r) for _ in range(300)]
    assert all(sg.host_refusal(h) == "digits" for h in hosts)
    monkeypatch.setattr(sg, "HOST_HASH_LETTERS", 2)
    prev.clear_guard_caches()
    assert sum(1 for h in hosts if sg.host_refusal(h) is None) == len(hosts)


def test_a_hyphenated_at_in_a_domain_is_refused_at_registration_only(tmp_path):
    h = helpers.Harness(tmp_path)
    h.tenant("acme", "client", ("acme.test",))
    n = len(h.svc.log)
    for d in ("great-at-work.test", "look-at-me.com", f"{rs(random.Random(1), LOWER, 5)}-smith-at-example.org"):
        assert svc_tenants.valid_domain(d) and svc_tenants.domain_refused(d)
        h.refused(h.post("/tenants/acme/domains", {"request_id": rid(), "domains": [d]}, andre=True), 422,
                  "IDENTIFIER_WITHHELD")
        h.refused(h.post("/tenants/acme/prompt-sets", {
            "request_id": rid(), "name": "n" + rs(random.Random(2), LOWER, 5), "brand_terms": ["Acme"],
            "prompts": ["who cleans"], "competitor_domains": [d], "engines": ["openai"]}, caller="dashboard"), 422,
            "IDENTIFIER_WITHHELD")
        assert sg.classify(d) == "hostname" and callum.checked_host(d)["host"] == d
    assert len(h.svc.log) == n
    # an ``at`` LABEL and a word holding "at" are not that form
    for d in ("look.at.example.org", "atlas-at.example.org", "great-atlas.com", "at-home.example.org"):
        assert not svc_tenants.domain_refused(d), d


# ================================================================================================ SHOULD 1: start

@pytest.mark.local_http
def test_the_legacy_line_count_is_not_on_the_start_path_and_is_remembered(tmp_path, srv):
    h, _marks, _aid, _report = prev._old_format_directory(tmp_path, srv)
    h2 = h.restart()
    assert h2.svc._legacy_prose is None and h2.svc._legacy_scan_len == len(h2.svc.log)   # start did not count
    assert h2.svc.health()["legacy_prose_lines"] == 3 and h2.svc._legacy_prose == 3
    # a line written after the start has passed the guard; one that has not (a raw write) is not looked at again
    sa.raw_commit(h2.svc, "job_ran", {"job": "integrity", "note": "two words"}, "scheduler")
    assert h2.svc.legacy_prose_lines == 3 and h2.ok(h2.get("/status"))["legacy_prose_lines"] == 3
    h3 = h2.restart()
    assert h3.svc._legacy_prose is None and h3.svc.legacy_prose_lines == 4               # the next start sees it
    # a fresh directory: nothing to count
    (tmp_path / "fresh").mkdir()
    h4 = helpers.Harness(tmp_path / "fresh", data_dir=str(tmp_path / "fresh" / "data"))
    assert h4.svc.legacy_prose_lines == 0


# ================================================================================================ SHOULD 2: capacity

def mixed(r, n: int) -> str:
    """A random letters-and-digits token: a letter beyond ``f``, 4..10 digits, 4+ changes, no run of 9 digits."""
    while True:
        t = rs(r, ALNUM, n)
        if any(c in "ghijklmnopqrstuvwxyz" for c in t) and 4 <= sum(c in DIGITS for c in t) <= 10 \
                and sg._changes(t) >= 4 and not any(t[i:i + 9].isdigit() for i in range(n - 8)):
            return t


def two_label_token(r) -> str:
    while True:
        t = mixed(r, r.choice((24, 28, 32)))
        k = r.choice((10, 12, 14))
        a, b = t[:k], t[k:]
        if a.strip(LOWER) and b.strip(LOWER) and a.strip(DIGITS) and b.strip(DIGITS):   # each half holds both kinds
            return f"{a}.{b}.example.com"


CAPACITY = {
    "token_over_two_labels": two_label_token,
    "hex_32_split_by_a_word_label": lambda r: f"{rs(r, prev.HEX, 16)}.{r.choice(prev.NEUTRAL_WORDS)}.{rs(r, prev.HEX, 16)}.example.com",
    "value_after_label_is": lambda r: "pass" + "word-" + r.choice(sorted(sg.HOST_VALUE_LINKS)) + "-" + rs(r, LOWER, 4)
                                      + rs(r, DIGITS, 3) + ".example.com",
}
CAPACITY_MUTATIONS = {
    "token_over_two_labels": lambda m: m.setattr(sg, "HOST_TOKEN_MIN", 10 ** 6),
    "hex_32_split_by_a_word_label": lambda m: m.setattr(sg, "HOST_HEX_MIN", 10 ** 6),
    "value_after_label_is": lambda m: m.setattr(sg, "HOST_VALUE_LINKS", frozenset()),
}


@pytest.mark.parametrize("name", sorted(CAPACITY))
def test_split_tokens_and_a_value_after_label_is_are_refused(name, monkeypatch):
    r = random.Random(f"{SEED}:s2:{name}")
    hosts = [CAPACITY[name](r) for _ in range(N)]
    rule = sum(1 for h in hosts if sg.host_refusal(h) is None)
    lanes_ = sum(1 for h in hosts if sg.classify(h) is not None or not svc_tenants.domain_refused(h)
                 or secret_guard.inspect_url(f"https://{h}/")["host_kind"] is None)
    print(f"host capacity {name}: generated={len(hosts)} accepted_by_the_rule={rule} accepted_in_a_lane={lanes_}")
    assert rule == 0 and lanes_ <= N * 0.005
    CAPACITY_MUTATIONS[name](monkeypatch)
    prev.clear_guard_caches()
    assert sum(1 for h in hosts[:200] if sg.host_refusal(h) is None) > 0, f"the {name} rule changed nothing"


def test_the_split_rules_do_not_refuse_real_world_host_names():
    r = random.Random(SEED + 9)
    for name, (gen, _n) in sorted(prev.HOST_CORPORA.items()):
        reasons = collections.Counter(sg.host_refusal(gen(r)) for _ in range(3000))
        print(f"host false refusals after the split rules {name}: generated=3000 by_reason={ {k: v for k, v in reasons.items() if k} }")
        assert (3000 - reasons[None]) / 3000 <= (0.015 if name == "platform_hash" else 0.01), (name, dict(reasons))
    ok = ["store.1a2b3c4d5e6f.example.com", "v2.api-7.example.com", "eu-west-1.s3.example.com", "key-is-you.example.com",
          "cafe.bad.dead.beef.example.com", "pass" + "word-is-here.example.com", "a1.b2.c3.example.com"]
    assert [h for h in ok if sg.host_refusal(h) is not None] == []


def test_a_punycode_label_round_trips_and_the_random_token_residual_is_measured():
    """A punycode label must decode to a label holding a non-ASCII letter and encode back to itself (so the label is
    the one canonical spelling of a name). That does NOT make a random token after ``xn--`` impossible: about one in
    six decodes to a valid label of CJK letters. Documented in ADR 0051 as a residual; measured here."""
    good = "xn--" + "m\u00fcnchen".encode("punycode").decode("ascii")
    assert sg._idna_label_ok(good) and sg.host_refusal(good + ".de") is None
    assert not sg._idna_label_ok("xn--" + "munchen-")          # decodes to ASCII only
    assert not sg._idna_label_ok(good + "a") or (good + "a")[4:].encode().decode("punycode").encode("punycode").decode() == (good + "a")[4:]
    r = random.Random(SEED + 10)
    labels = ["xn--" + rs(r, ALNUM, 31) for _ in range(N)]
    passing = [x for x in labels if sg.host_refusal(x + ".example.com") is None]
    for x in passing:                                         # each one that passes IS a canonical non-ASCII label
        text = x[4:].encode("ascii").decode("punycode")
        assert not text.isascii() and text.encode("punycode").decode("ascii") == x[4:]
    print(f"host capacity punycode_token_31: generated={len(labels)} accepted_by_the_rule={len(passing)} "
          f"rate={len(passing) / len(labels):.2%}")
    assert len(passing) / len(labels) <= 0.25
    r = random.Random(SEED + 11)
    assert sum(1 for _ in range(3000) if sg.host_refusal(prev.idn_host(r)) is not None) == 0


# ================================================================================================ SHOULD 3: mutants

@pytest.mark.local_http
def test_the_export_never_serves_a_kind_or_a_time_that_is_not_a_machine_value(tmp_path, srv, monkeypatch):
    """Mutant: ``audit_export`` serving a stored line's ``kind`` (or its time) as it is. The store itself refuses a
    line of an unknown kind, so the line is handed to the export directly: a kind and a time that are not machine
    values are served as the code ``WITHHELD``."""
    h = sa.harness(tmp_path, srv)
    r = random.Random(SEED + 12)
    words, when = f"zq{rs(r, LOWER, 8)} wv{rs(r, LOWER, 8)}", f"zq{rs(r, LOWER, 6)} evening"
    assert sg.classify(words) is None and sg.classify(when) is None
    stored = list(h.svc.log.iter_records())
    foreign = {**stored[-1], "seq": stored[-1]["seq"] + 1, "kind": words, "at": when}
    monkeypatch.setattr(h.svc.log, "iter_records", lambda start_seq=1: iter((stored + [foreign])[max(0, start_seq - 1):]))
    page = h.svc.audit_export(1, 1000)
    text = json.dumps(page)
    assert words not in text and words.split()[0] not in text and when not in text and when.split()[0] not in text
    assert len(page["events"]) == len(stored) + 1
    assert [(e["kind"], e["at"]) for e in page["events"][:-1]] == [(x["kind"], x["at"]) for x in stored]
    assert (page["events"][-1]["kind"], page["events"][-1]["at"]) == (sg.WITHHELD, sg.WITHHELD)
    assert all(sg.classify(e["kind"]) is not None and sg.classify(e["at"]) is not None for e in page["events"])
    # the mutation: the kind and the time served as stored
    real = sg.classify
    monkeypatch.setattr(sg, "classify", lambda v: "code" if v in (words, when) else real(v))
    leaked = json.dumps(h.svc.audit_export(1, 1000))
    assert words in leaked and when in leaked


@pytest.mark.local_http
def test_the_citation_run_line_carries_the_keyed_hash_of_a_ruling_id_of_another_shape(tmp_path, srv):
    """Mutant: the ``citation_run_requested`` line dropping ``ruling_id_hmac``. A ruling id of a shape nobody issues
    is written as the code ``RULING_ID_HASHED`` WITH its keyed hash, in the citation run's line as in the audit's."""
    import compliance_client
    other = "RUL." + prev.digits(12) + ":" + prev.letters(6) + "-" + prev.digits(4)
    assert compliance_client.RULING_ID.fullmatch(other) and sg.classify(other) is None
    h = prev._client_world(tmp_path, srv, other)
    psid = h.ok(h.post("/tenants/acme/prompt-sets", {
        "request_id": rid(), "name": "core", "brand_terms": ["Z Best Media"], "prompts": ["who recovers revenue"],
        "engines": ["openai"]}, caller="dashboard"), 201)["prompt_set_id"]
    run = h.ok(h.post("/tenants/acme/citation-runs", {"request_id": rid(), "domain": sa.DOMAIN, "prompt_set_id": psid,
                                                      "engines": ["openai"]}, caller="dashboard", andre=True), 201)
    assert run["status"] == "completed"
    mac = h.svc.vault.field_hash("acme", "compliance_ruling_id", other)[1]
    (line,) = sa.records(h, "citation_run_requested")
    assert line["data"]["compliance"] == {"ruling_id": sg.RULING_ID_HASHED, "verdict": "ALLOW", "ruling_id_hmac": mac}
    blob = sa.written(h)
    assert other not in blob and other.lower() not in blob.lower()
    # an issued ruling id is written as it is, with no hash beside it
    h2 = prev._client_world(tmp_path / "issued", srv, helpers.ruling_id("acme-party-1"))
    psid = h2.ok(h2.post("/tenants/acme/prompt-sets", {
        "request_id": rid(), "name": "core", "brand_terms": ["Z Best Media"], "prompts": ["who recovers revenue"],
        "engines": ["openai"]}, caller="dashboard"), 201)["prompt_set_id"]
    h2.ok(h2.post("/tenants/acme/citation-runs", {"request_id": rid(), "domain": sa.DOMAIN, "prompt_set_id": psid,
                                                  "engines": ["openai"]}, caller="dashboard", andre=True), 201)
    (line,) = sa.records(h2, "citation_run_requested")
    assert line["data"]["compliance"] == {"ruling_id": helpers.ruling_id("acme-party-1"), "verdict": "ALLOW"}
    assert json.dumps(line["data"]).count("ruling_id_hmac") == 0
