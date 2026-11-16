"""core-spec/1 O-4 N: the neutral sentences of the CI draw (``corpora.neutral``): 0 differences vs ffd28ca8
(strict, client view), against the frozen baseline. Counts only."""
import pytest

from conformance import baseline as B
from conformance import corpora as C
from conformance import evidence as E

pytestmark = pytest.mark.conformance


@pytest.fixture(scope="module")
def mods():
    from agents import callum
    from agents import secret_guard

    import sink_guard
    import svc_audits
    import svc_tenants
    return {"sg": secret_guard, "sink": sink_guard, "tenants": svc_tenants, "audits": svc_audits, "callum": callum}


def test_core_O_4_N_neutral_sentences_no_new_read_vs_ffd28ca8(mods):
    """O-4 N: no sentence read (strict / client view) that ffd28ca8 did not read, outside residual (a) (Thai, as
    written, no segmenter). The sentences ffd28ca8 read and the build does not (its letters-anywhere reads across
    words, closed by the boundary rule of decision 73) are reported: on this draw 186471eb differs from ffd28ca8 in
    that direction too (NOTES Q-N: "0 differences" cannot hold for any build after decision 73)."""
    items = C.neutral()
    base = B.read("neutral", "ffd28ca8", C.stream_hash(items))
    new: dict = {}
    lost: dict = {}
    thai = 0
    for (lang, casing, t), b in zip(items, base):
        got = C.lanes_text(mods["sg"], t)
        for lane, g, x in zip(("strict", "cv"), got, b.split("|")):
            k = f"{lang}|{lane}"
            if g and x == "0":
                if lang == "th":
                    thai += 1
                else:
                    new[k] = new.get(k, 0) + 1
            elif x == "1" and not g:
                lost[k] = lost.get(k, 0) + 1
    print(f"O-4 N: sentences={len(items)} new vs ffd28ca8={new} residual (a) Thai={thai} read by ffd28ca8 only={lost}")
    # ST-2 / ST-3 re-pin (core-spec/2 ST-3: the O-4 N STRICT column is reported, not judged; C2-11b; G-7: re-pinned to
    # "reported" with the item named): strict NEW / LOST counts go to the evidence file. The CV column keeps its v1
    # assertion here (not touched by this card; the CV lane's bar is CV-8).
    strict_new = {k: v for k, v in new.items() if k.endswith("|strict")}
    E.report("O-4 N STRICT (reported, ST-3)",
             {"sentences": len(items), "strict_new": sum(strict_new.values()), **{f"new|{k}": v for k, v in strict_new.items()},
              "strict_lost": sum(v for k, v in lost.items() if k.endswith("|strict"))})
    judged = {k: v for k, v in new.items() if not k.endswith("|strict")}
    assert not judged
