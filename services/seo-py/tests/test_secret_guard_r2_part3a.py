"""secret_guard core v2 r2 part 3a: exact-host URL exemptions, grounding redirect ids never stored, fail-closed fetch.

Every sample is generated at run time from fragments (seeded); nothing credential- or number-shaped is written in this
file, and every assertion reports indices / counts only (a failure never prints a sample)."""
from __future__ import annotations

import json
import random
import re

import pytest

from agents import callum
from agents import secret_guard as sg

SEED = 202611093
N = 24                                   # samples per generated class
UP, LO, DG = "ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnopqrstuvwxyz", "23456789"
RHOST = sg.REDIRECT_HOST
RWORD = sg.REDIRECT_WORD


# ------------------------------------------------------------------------------------------------ generators

def _mixed(r: random.Random, n: int) -> str:
    """Letters of both cases with a few spread digits (a token shape for check (d), never a long digit run)."""
    out = [r.choice(UP + LO) for _ in range(n)]
    for i in r.sample(range(n), max(2, n // 8)):
        out[i] = r.choice(DG)
    return "".join(out)


def _dtrip(make, n=N, seed=SEED) -> list:
    """Values from ``make(r)`` that fail check (d) alone and pass every check once (d) is waived."""
    r = random.Random(seed)
    out = []
    for _ in range(n * 40):
        v = make(r)
        if sg.check(v) == "d_token" and sg.check(v, {"d_token"}) is None:
            out.append(v)
            if len(out) == n:
                break
    assert len(out) >= n // 2, len(out)
    return out


def _drive_id(r):
    return "1" + _mixed(r, 32)


def _docs_id(r):
    return "1" + _mixed(r, 43)


def _spotify_id(r):
    return _mixed(r, 22)


def _luhn_digits(r: random.Random, n: int = 16) -> str:
    body = "4" + "".join(r.choice("0123456789") for _ in range(n - 2))
    for c in "0123456789":
        if sg._luhn(body + c):
            return body + c
    raise AssertionError("no check digit")


def _plain_digits(r: random.Random, n: int) -> str:
    """n digits, not Luhn-valid, not starting with 0."""
    while True:
        v = r.choice("123456789") + "".join(r.choice("0123456789") for _ in range(n - 1))
        if not sg._luhn(v):
            return v


def _prefix_key(r: random.Random) -> str:
    return "AK" + "IA" + "".join(r.choice(UP + "234567") for _ in range(16))


def _phone() -> str:
    return "call-" + "-".join(("555", "010", "01" + "99"))


def _email() -> str:
    return "jane" + "." + "doe" + "@" + "example" + ".com"


def _ssn() -> str:
    return "-".join(("123", "45", "67" + "89"))


def _drive(i: str, host="drive.google.com", word="d") -> str:
    return f"https://{host}/file/{word}/{i}/view"


def _redirect(i: str, host=RHOST) -> str:
    return f"https://{host}/{RWORD}/{i}"


def _leaks(i: str, obj) -> bool:
    return i in json.dumps(obj)


# ------------------------------------------------------------------------------------------------ decision 1

class TestExactHostExemptions:
    def test_exempt_ids_on_exact_hosts_are_kept_stored_and_fetchable(self):
        cases = ([_drive(i) for i in _dtrip(_drive_id)]
                 + [_drive(i, "www.drive.google.com") for i in _dtrip(_drive_id, seed=SEED + 1)]
                 + [f"https://docs.google.com/document/d/{i}/edit" for i in _dtrip(_docs_id)]
                 + [f"https://open.spotify.com/track/{i}" for i in _dtrip(_spotify_id)])
        bad = [n for n, u in enumerate(cases) if sg.safe_url(u) != u or callum.stored_url(u)["url"] != u
               or callum.stored_url(u)["withheld"] is not None or callum.url_has_secret(u)]
        assert bad == [], (len(bad), len(cases))

    @pytest.mark.parametrize("host,word", [
        ("x.drive.google.com", "d"), ("drive.google.com.evil.test", "d"), ("evil-drive.google.com", "d"),
        ("sheets.google.com", "d"), ("drive.google.com", "u"), ("drive.google.com", "x")])
    def test_same_ids_on_a_subdomain_lookalike_or_other_word_are_withheld(self, host, word):
        ids = _dtrip(_drive_id)
        bad = []
        for n, i in enumerate(ids):
            u = _drive(i, host, word)
            s = callum.stored_url(u)
            if (not callum.url_has_secret(u) or i in sg.safe_url(u) or _leaks(i, s)
                    or s["withheld"] != "SECRET_REDACTED" or sg.guard_record({"url": u})["url"] is not None
                    or sg.check(f"see {u} now") is None):
                bad.append(n)
        assert bad == [], (len(bad), len(ids))

    def test_commit_sha_only_directly_after_commit_on_the_three_code_hosts(self):
        r = random.Random(SEED)
        shas = []
        for n in (7, 9, 12, 40):
            s = "a" + "".join(r.choice("0123456789abcdef") for _ in range(n - 1))
            shas.append(s)
        for host in ("github.com", "gitlab.com", "bitbucket.org"):
            for s in shas:
                assert sg.position(host, ["", "o", "r", "commit", s], 4) == "commit_sha", (host, len(s))
                for word in ("tree", "blob", "commits", "compare", "raw"):
                    assert sg.position(host, ["", "o", "r", word, s], 4) is None, (host, word)
        for host in ("gist.github.com", "api.github.com", "codeberg.org", "huggingface.co", "github.com.evil.test",
                     "evil.gitlab.com"):
            assert sg.position(host, ["", "o", "r", "commit", shas[-1]], 4) is None, host
        for s in ("a" * 6, "a" * 13, "a" * 39, "1234567", "g" * 40):
            assert sg.position("github.com", ["", "o", "r", "commit", s], 4) is None, len(s)

    def test_canva_design_id_only_as_the_design_segment_on_canva(self):
        r = random.Random(SEED)
        did = "DA" + "".join(r.choice(UP + LO + DG) for _ in range(9))
        tok = _mixed(r, 22)
        assert sg.position("canva.com", ["", "design", did, tok, "view"], 2) == "canva_design"
        assert sg.position(sg.canon_host("www.canva.com"), ["", "design", did], 2) == "canva_design"
        assert sg.position("canva.com", ["", "design", did, tok, "view"], 3) is None        # the share token
        assert sg.position("canva.com", ["", "x", "design", did], 3) is None
        assert sg.position("canva.com", ["", "templates", "EA" + did[2:]], 2) is None
        for host in ("foo.canva.com", "canva.com.evil.test", "canva.site"):
            assert sg.position(host, ["", "design", did], 2) is None, host

    def test_only_the_four_decided_positions_exist(self):
        assert {p[0] for p in sg.POSITIONS} == {"google_doc_id", "commit_sha", "canva_design", "spotify_id"}
        assert all(isinstance(h, tuple) and h for _, h, _, _ in sg.POSITIONS)     # no any-host entry
        assert sg.POSITION_WAIVE == frozenset({"d_token"})

    @pytest.mark.parametrize("layout", [
        "https://www.google.com/maps/place/{t}", "https://site.test/_next/static/{t}/x.js",
        "https://www.facebook.com/acme/posts/pfbid{t}", "https://doi.org/{t}", "https://www.figma.com/file/{t}/x",
        "https://gist.github.com/acme/{t}", "https://www.loom.com/share/{t}", "https://images.ctfassets.net/{t}/x",
        "https://www.canva.com/design/DAbbbbbbbbb/{t}/view", "https://www.notion.so/acme/{t}",
        "https://www.linkedin.com/posts/{t}", "https://open.spotify.com/user/{t}"])
    def test_removed_positions_no_longer_waive_anything(self, layout):
        toks = _dtrip(lambda r: _mixed(r, 22))
        bad = [n for n, t in enumerate(toks) if not callum.url_has_secret(layout.format(t=t))
               or t in sg.safe_url(layout.format(t=t)) or _leaks(t, callum.stored_url(layout.format(t=t)))]
        assert bad == [], (len(bad), len(toks))


# ------------------------------------------------------------------------------------------------ decision 2

class TestGroundingRedirect:
    WITHHELD_FORM = f"https://{RHOST}/{RWORD}/[withheld]"

    def test_the_id_is_never_stored_or_emitted_but_the_exact_host_is_fetchable(self):
        ids = _dtrip(lambda r: _mixed(r, 48)) + ["AAAA" + "b" * 8]
        bad = []
        for n, i in enumerate(ids):
            u = _redirect(i)
            s = callum.stored_url(u)
            if (sg.safe_url(u) != self.WITHHELD_FORM or s["url"] != self.WITHHELD_FORM or _leaks(i, s)
                    or s["withheld"] != "REDIRECT_ID" or callum.url_has_secret(u)
                    or sg.safe_url(u + "?x=1#f") != self.WITHHELD_FORM
                    or sg.check(f"see {u} now") is None
                    or sg.guard_record({"url": u}) != {"url": None}
                    or _leaks(i, sg.guard_record({u: 1, "note": f"see {u}"}))
                    or sg.guard_record({"url": s["url"], "w": s["withheld"]}) != {"url": s["url"], "w": "REDIRECT_ID"}):
                bad.append(n)
        assert bad == [], (len(bad), len(ids))

    def test_a_redirect_on_any_other_host_gets_no_waiver(self):
        ids = _dtrip(lambda r: _mixed(r, 48))
        for host in ("x." + RHOST, RHOST + ".evil.test", "vertexaisearch.cloud.google.co"):
            bad = [n for n, i in enumerate(ids) if not callum.url_has_secret(_redirect(i, host))
                   or i in sg.safe_url(_redirect(i, host))]
            assert bad == [], (host, len(bad))
        assert sg.redirect_withheld(_redirect("Abc", "x." + RHOST)) is None

    def test_prefix_and_digit_rules_still_block_a_redirect_fetch(self):
        r = random.Random(SEED)
        for n in range(N):
            for mid, kind in ((_prefix_key(r), sg.CREDENTIAL), (_luhn_digits(r), sg.PERSONAL)):
                u = _redirect("Ab" + mid + "cd")
                # the fetch judgement still reads the id with rules (c) and (e); ADR 0051 job 2: only a credential
                # blocks the fetch (a card-shaped id is fetched to resolve it and is never stored)
                assert sg._worst(sg._inspect(u, True, fetch=True)) == kind, n
                assert callum.url_has_secret(u) == (kind == sg.CREDENTIAL), n
                assert mid not in json.dumps(callum.stored_url(u)), n

    def test_classification_emits_the_resolved_target_or_the_withheld_form(self):
        from test_callum import answer, classify, collected, ok_page, resolver
        ids = _dtrip(lambda r: _mixed(r, 48), n=2)
        g1, g2 = _redirect(ids[0]), _redirect(ids[1])
        res = resolver({g1: ok_page(g1, "https://competitor.test/p", 1)})
        r = classify(collected((0, answer(cites=[g1, g2]))), res)
        dumped = json.dumps(r)
        assert ids[0] not in dumped and ids[1] not in dumped
        assert g1 in res.fetcher.fetched                               # fetched (exact host) to be resolved
        a, b = r["rows"][:2]
        assert a["url"] == "https://competitor.test/p" and a["evidence"]["url"] == a["url"]
        assert a["chain"]["cited"]["url"] == self.WITHHELD_FORM
        assert b["url"] == self.WITHHELD_FORM and b["class"] == "INCONCLUSIVE"


# ------------------------------------------------------------------------------------------------ decision 3

class TestRulesCAndEInExemptPositions:
    def test_a_prefix_key_or_a_card_in_an_exempt_position_is_withheld(self):
        r = random.Random(SEED)
        bad = []
        for n in range(N):
            key, card = _prefix_key(r), _luhn_digits(r)
            cases = [(_drive("1" + _mixed(r, 12) + key), key, "SECRET_REDACTED"),
                     (_drive("1" + _mixed(r, 16) + card), card, "PERSONAL_DATA"),
                     (f"https://open.spotify.com/track/{_mixed(r, 6)}{card}", card, "PERSONAL_DATA"),
                     (f"https://docs.google.com/document/d/1{_mixed(r, 27)}{card}/edit", card, "PERSONAL_DATA")]
            for u, v, why in cases:
                s = callum.stored_url(u)
                if (not _flagged(u) or v in sg.safe_url(u) or _leaks(v, s) or s["withheld"] != why
                        or sg.guard_record({"url": u})["url"] is not None):
                    bad.append(n)
        assert bad == [], (len(bad), N)


# ------------------------------------------------------------------------------------------------ decision 4

COMMERCE = ["acme.myshopify.com", "www.amazon.com", "amazon.co.uk", "amazon.de", "amazon.co.jp", "www.ebay.com",
            "etsy.com", "www.walmart.com", "target.com"]


class TestQueryIds:
    def _nums(self):
        r = random.Random(SEED)
        out = [v for v in (_plain_digits(r, r.choice((10, 11, 13, 14, 17, 20))) for _ in range(N * 3))
               if sg.check(v) == "e_digits"][:N]
        assert len(out) >= N // 2, len(out)
        return out

    def test_numeric_ids_under_the_four_keys_on_commerce_hosts_are_kept(self):
        bad = []
        for n, v in enumerate(self._nums()):
            for host in COMMERCE:
                for key in ("id", "variant", "item", "product"):
                    u = f"https://{host}/p/x?{key}={v}"
                    if callum.url_has_secret(u) or f"{key}={v}" not in sg.safe_url(u):
                        bad.append(n)
        assert bad == [], len(bad)

    def test_other_hosts_keys_cards_ssns_and_long_runs_fail(self):
        r = random.Random(SEED)
        nums = self._nums()
        bad = []
        for n, v in enumerate(nums):
            urls = [f"https://{h}/p?variant={v}" for h in ("site.test", "myshopify.com", "acme.myshopify.com.evil.test",
                                                            "amazon.com.evil.test", "shop.ebay.com", "amazon.test")]
            urls += [f"https://acme.myshopify.com/p?{k}={v}" for k in ("pid", "p", "page", "variant_id", "ID2")]
            urls += [f"https://acme.myshopify.com/p?variant={x}" for x in (_luhn_digits(r), _luhn_digits(r, 13),
                                                                        _luhn_digits(r, 19), _plain_digits(r, 9),
                                                                        _plain_digits(r, 21))]
            urls += [f"https://www.amazon.com/dp/x?id={_ssn()}", f"https://www.amazon.com/dp/x?id={_phone()}"]
            for u in urls:
                if not _flagged(u) or "?" in sg.safe_url(u):
                    bad.append(n)
        assert bad == [], (len(bad), len(nums))


# ------------------------------------------------------------------------------------------------ decision 5

class TestUrlsInText:
    def test_a_url_that_passes_only_by_an_exemption_stays_in_the_text(self):
        ids = _dtrip(_drive_id)
        bad = []
        for n, i in enumerate(ids):
            u = _drive(i)
            text = f"Z Best Media: read {u} today."
            if (sg.check(text) is None or sg.guard_record({"note": text}) != {"note": sg.WITHHELD}
                    or sg.guard_record({"url": u}) != {"url": None} or _leaks(i, sg.guard_record({u: 1}))):
                bad.append(n)
        assert bad == [], (len(bad), len(ids))

    def test_a_url_that_passes_without_any_exemption_is_masked(self):
        r = random.Random(SEED)
        for n in range(N):
            plain = "1" + "".join(r.choice(LO) for _ in range(32))      # an exempt shape that passes on its own
            assert sg.check(plain) is None, n
            for u in (_drive(plain), "https://drive.google.com/drive/my-drive", "https://site.test/a/b"):
                text = f"Z Best Media: read {u} today."
                assert sg.check(text) is None and sg.guard_record({"note": text}) == {"note": text}, n

    def test_a_commerce_query_id_that_passes_only_by_its_waiver_stays_in_the_text(self):
        v = TestQueryIds()._nums()[0]
        assert sg.check(f"Buy at https://acme.myshopify.com/p?variant={v} now") is not None


# ------------------------------------------------------------------------------------------------ decision 6

class TestFailClosedFetch:
    # ADR 0051 job 2 (owner decision, Option B): the r2 part 3a block on fetching personal-data-shaped URLs is reverted.
    # Personal data anywhere in a URL is still FLAGGED by the strict filter (never kept in the log, masked for the
    # client); only a credential finding blocks the fetch.
    def test_personal_data_anywhere_in_a_url_is_flagged_but_does_not_block_the_fetch(self):
        r = random.Random(SEED)
        bad = []
        for n in range(N):
            card = _luhn_digits(r)
            for v in (_phone(), _email(), _ssn(), card):
                for u in (f"https://site.test/c/{v}/x", f"https://site.test/c?q={v}", f"https://site.test/c#{v}"):
                    if (sg._url_verdict(u) != sg.PERSONAL or callum.url_flag(u) != "personal_data:url"
                            or callum.url_has_secret(u) or sg.url_has_secret(u)):
                        bad.append(n)
        assert bad == [], len(bad)

    def test_a_personal_url_is_fetched_through_classification_and_never_stored(self):
        from test_callum import answer, classify, collected, resolver
        phone = _phone()
        u = f"https://site.test/c/{phone}/x"
        res = resolver({})
        r = classify(collected((0, answer(cites=[u]))), res)
        assert len(res.fetcher.fetched) == 1 and r["rows"][0]["chain"]["resolution"] != "NOT_FETCHED_SECRET"
        assert phone not in json.dumps(r) and "".join(c for c in phone if c.isdigit()) not in json.dumps(r)

    def test_a_credential_url_is_never_fetched(self):
        from test_callum import answer, classify, collected, resolver
        r0 = random.Random(SEED)
        u = f"https://site.test/c/{_prefix_key(r0)}/x"
        res = resolver({})
        r = classify(collected((0, answer(cites=[u]))), res)
        assert res.fetcher.fetched == [] and r["rows"][0]["chain"]["resolution"] == "NOT_FETCHED_SECRET"


# ------------------------------------------------------------------------------------------------ mutation bite

def _flagged(u: str) -> bool:
    """The strict filter flags ``u`` (a credential or personal-data shape in any part). Before ADR 0051 job 2 these
    tests read ``url_has_secret``, which then also blocked personal data; the rule under test is the verdict."""
    return sg._url_verdict(u) is not None


def _bite_cases():
    ids = _dtrip(_drive_id, n=4)
    sp = _dtrip(_spotify_id, n=4)
    rid = _dtrip(lambda r: _mixed(r, 48), n=4)
    r = random.Random(SEED)
    card, num = _luhn_digits(r), TestQueryIds()._nums()[0]
    did = "DA" + "b" * 9
    benign = "AAAA" + "b" * 8

    def judge_no_dep(u):
        return (sg._worst(sg._inspect(u, False)), False)
    return {
        "exact_host": ([("_position_host", lambda h, hs: any(h == x or h.endswith("." + x) for x in hs))],
                       lambda: callum.url_has_secret(_drive(ids[0], "x.drive.google.com"))),
        "path_words": ([("_POSITIONS", tuple((a, b, c | {"tree", "u"}, d) for a, b, c, d in sg._POSITIONS))],
                       lambda: sg.position("github.com", ["", "o", "r", "tree", "abc1234"], 4) is None
                       and callum.url_has_secret(_drive(ids[1], word="u"))),
        "removed_entries": ([("_POSITIONS", sg._POSITIONS + (("google_maps", frozenset({"google.com"}),
                                                              frozenset({"place"}), re.compile(r"[^/]{1,2048}")),))],
                            lambda: callum.url_has_secret(f"https://www.google.com/maps/place/{ids[2]}")),
        "canva_index": ([("_canva_index_ok", lambda i: True)],
                        lambda: sg.position("canva.com", ["", "x", "design", did], 3) is None),
        "waive_d_only": ([("POSITION_WAIVE", sg.WAIVABLE)],
                         lambda: _flagged(f"https://open.spotify.com/track/{sp[0][:6]}{card}")),
        "redirect_never_stored": ([("_redirect_host", lambda h: False)],
                                  lambda: benign not in sg.safe_url(_redirect(benign))
                                  and benign not in json.dumps(callum.stored_url(_redirect(benign)))),
        "redirect_exact_host": ([("_redirect_host", lambda h: h.rstrip(".").endswith(sg.REDIRECT_HOST))],
                                lambda: callum.url_has_secret(_redirect(rid[0], "x." + RHOST))),
        "query_commerce_hosts": ([("_commerce_host", lambda h: True)],
                                 lambda: _flagged(f"https://site.test/p?variant={num}")),
        "query_keys": ([("QUERY_ID_KEYS", sg.QUERY_ID_KEYS | {"pid"})],
                       lambda: _flagged(f"https://www.amazon.com/dp/x?pid={num}")),
        "query_card_ssn": ([("_card_or_ssn", lambda v: False)],
                           lambda: _flagged(f"https://acme.myshopify.com/p?variant={card}")),
        "mask_exempt_dependent": ([("_url_judge", judge_no_dep)],
                                  lambda: sg.check(f"Z Best Media: read {_drive(ids[3])} today.") is not None),
        # ADR 0051 job 2: the fetch policy is load-bearing both ways (a credential blocks; personal data alone does not)
        "fetch_credential": ([("FETCH_BLOCKS", frozenset())],
                             lambda: callum.url_has_secret(f"https://site.test/c/{_prefix_key(random.Random(SEED))}/x")),
        "fetch_personal_allowed": ([("FETCH_BLOCKS", frozenset({sg.CREDENTIAL, sg.PERSONAL}))],
                                   lambda: not callum.url_has_secret(f"https://site.test/c/{_phone()}/x")),
    }


@pytest.mark.parametrize("name", sorted(_bite_cases()))
def test_each_new_restriction_is_load_bearing(monkeypatch, name):
    patches, holds = _bite_cases()[name]
    assert holds(), name
    for attr, value in patches:
        monkeypatch.setattr(sg, attr, value)
    assert not holds(), (name, "removing this restriction broke nothing")
