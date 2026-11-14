"""core-spec/1 O-4 U (and U-1): ordinary-word URLs of the CI draw (``corpora.urls``, raw and percent-encoded): 0
newly ``url_has_secret`` vs ffd28ca8 outside the label's own words (the O-2 oracle). Counts only."""
import pytest

from conformance import baseline as B
from conformance import corpora as C
from conformance.oracle.core_own_words import own_words

pytestmark = pytest.mark.conformance


@pytest.fixture(scope="module")
def mods():
    from agents import callum
    from agents import secret_guard

    import sink_guard
    import svc_audits
    import svc_tenants
    return {"sg": secret_guard, "sink": sink_guard, "tenants": svc_tenants, "audits": svc_audits, "callum": callum}


def test_core_O_4_U_ordinary_urls_none_newly_unfetched(mods):
    items = C.urls()
    base = B.read("urls", "ffd28ca8", C.stream_hash(items))
    new: dict = {}
    own = 0
    for (lang, place, u), b in zip(items, base):
        if mods["sg"].url_has_secret(u) and b == "0":
            if own_words(u):
                own += 1
                continue
            new[f"{lang}|{place}"] = new.get(f"{lang}|{place}", 0) + 1
    print(f"O-4 U: urls={len(items)} newly unfetched: label own words={own} other={sum(new.values())} {new}")
    assert not new
