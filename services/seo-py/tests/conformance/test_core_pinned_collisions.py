"""core-spec/1 O-4 PIN: the pinned collisions, every set in full, all lanes. Bare: read in NO lane (strict, client
view bare and before ": ready today", a URL raw / percent-encoded path and query, a Unicode-form host). With a value:
the client view never masks them; the strict lane never flags them, except the Arabic S-2 phrases (STRICT O: ffd28ca8
flagged them). The sets are the review snapshots of tests/test_review_cbdf5e78.py and test_review_cd0a6c7d.py.
Counts only."""
import random
import unicodedata
from urllib.parse import quote

import pytest

import test_review_cbdf5e78 as cbd
import test_review_cd0a6c7d as cd
from conformance import core_matrix as M
from conformance import evidence as E

pytestmark = pytest.mark.conformance
JOINERS = ("\u200d", "\u200c", "\u200b", "\u00ad", "\u2060")
HORMUZ = ("\u0647\u0631\u0645\u0632 \u0639\u0628\u0648\u0631", "\u0647\u0631\u0645\u0632-\u0639\u0628\u0648\u0631",
          "\u0647\u0631\u0645\u0632\u0639\u0628\u0648\u0631", "\u0645\u062d\u0627\u0631\u0645 \u0632 \u0639\u0628\u0648\u0631",
          "\u0627\u0644\u0647\u0631\u0645 \u0632 \u0639\u0628\u0648\u0631")


@pytest.fixture(scope="module")
def sg():
    from agents import secret_guard
    return secret_guard


def pinned_sets() -> dict:
    greek = [f for p in cbd.greek_phrases() for f in (p, p.upper(), p.title(), p.replace(" ", "-"),
                                                      unicodedata.normalize("NFD", p))]
    thai = [t for t, lit in cbd.thai_texts() if not lit]
    hindi = [f"{a}{sep}{b}" for a, b in cd.hindi_joins() for sep in (" ", "-", "_", "\u00a0", " \u2013 ", "\u0964 ")]
    hindi += [a + b for a, b in cd.hindi_joins()]
    hindi += ["".join(p.split("+")) for p in cbd.HINDI_GLUED_PAIRS if "".join(p.split("+")) != cbd.HINDI]
    arabic = []
    for p in cbd.ARABIC_PHRASES:
        arabic += [p, unicodedata.normalize("NFD", p), p[:2] + "\u0640" + p[2:]] + [p[:2] + j + p[2:] for j in JOINERS]
    return {"greek_code_to_bas": greek, "thai_joins": thai, "hindi_joins": hindi, "arabic_s2": arabic,
            "loanword": [cd.LOANWORD], "greek_derivatives": list(cd.GREEK_DERIVATIVES), "hormuz_bare": list(HORMUZ)}


def bare_reads(sg, t: str, lanes=("strict", "cv", "cv_prose", "url", "host")) -> list:
    out = []
    if sg.check(t) is not None:
        out.append("strict")
    if sg.client_view_findings(t):
        out.append("cv")
    if "cv_prose" in lanes and "label_value" in sg.client_view_findings(f"{t}: ready today"):
        out.append("cv_prose")
    if any(unicodedata.category(c) == "Cf" for c in t):
        return out                     # (an invisible character is withheld from a URL / host whatever it spells: U-1)
    slug = t.replace(" ", "-")
    for u in (f"https://site.test/blog/{slug}/", f"https://site.test/s?q={t}&page=2"):
        if sg.url_has_secret(u):
            out.append("url")
    if sg.url_has_secret("https://site.test/blog/" + quote(slug) + "/") and sg.check(quote(slug)) != "d_token":
        out.append("url_encoded")
    if "host" in lanes and not cbd.host_accepted(t):
        out.append("host")
    return out


@pytest.mark.parametrize("name", ["greek_code_to_bas", "thai_joins", "hindi_joins", "arabic_s2", "loanword",
                                  "greek_derivatives", "hormuz_bare"])
def test_core_O_4_PIN_pinned_collisions(sg, name):
    texts = pinned_sets()[name]
    r = random.Random(f"{M.SEED}:pin:{name}")
    bare: dict = {}
    valued: dict = {}
    for i, t in enumerate(texts):
        # ("Hormuz crossing": pinned bare in the strict lane, the client view and URLs, as review cbdf5e78 pinned it;
        # decision 75 reads it before a value, and its glued host holds the Persian label: refused at every release)
        got = bare_reads(sg, t, ("strict", "cv", "url") if name == "hormuz_bare" else ("strict", "cv", "cv_prose", "url",
                                                                                         "host"))
        if got:
            bare[i] = got
        if name == "hormuz_bare":
            continue                                       # (pinned bare only: decision 75 reads it before a value)
        v = M.value(f"pin|{name}|{i}|{r.random()}")
        for sep in (": ", " = ", " is "):
            if "label_value" in sg.client_view_findings(f"{t}{sep}{v}"):
                valued.setdefault(i, []).append("cv")
            if name != "arabic_s2" and sg.check(f"{t}{sep}{v}") == "a_label":
                valued.setdefault(i, []).append("strict")
    print(f"O-4 PIN {name}: texts={len(texts)} read bare={len(bare)} read with a value={len(valued)}")
    # ST-2 / ST-3 re-pin (core-spec/2 ST-3: the O-4 PIN STRICT column is reported, not judged; section 6: O-4 PIN in the
    # URL / HOST lanes stays a PASS row "0 read bare"; C2-11b; G-7: re-pinned to "reported" with the item named): the
    # strict reads (bare and with a value) go to the evidence file; every other lane keeps its assertion.
    E.report(f"O-4 PIN {name} STRICT (reported, ST-3)",
             {"texts": len(texts), "strict_bare": sum("strict" in v for v in bare.values()),
              "strict_valued": sum("strict" in v for v in valued.values())})
    bare_j = {i: [x for x in v if x != "strict"] for i, v in bare.items()}
    valued_j = {i: [x for x in v if x != "strict"] for i, v in valued.items()}
    bare_j = {i: v for i, v in bare_j.items() if v}
    valued_j = {i: v for i, v in valued_j.items() if v}
    assert not bare_j, list(bare_j.items())[:5]
    assert not valued_j, list(valued_j.items())[:5]
