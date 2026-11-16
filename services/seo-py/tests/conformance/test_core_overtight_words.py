"""core-spec/1 O-1..O-3 and O-4 W (with the sizes of residuals a, g, h).

CI lane: the derived boundary joins of the embedded 220-word samples (``corpora.wsample``: every ordered pair whose
letters hold a script label across the word boundary, in the 4 join forms and the 10 extra joins), six lanes, against
the frozen ffd28ca8 / cbdf5e78 verdicts. A read is allowed only when (i) ffd28ca8 reads the text in that lane, or (ii)
cbdf5e78 reads it and it holds a label form, or (iii) it holds the label's own words (the O-2 oracle), or (iv) it is
residual (a) (Thai: no segmenter). Anything else is NEW and fails.
Heavy lane: the full W corpus (wordfreq 3.1.1 top 30,000 per language, Thai pythainlp 5.4.0), ``wcorpus.py``, against
ffd28ca8 and cbdf5e78 exported from git history and run in this process."""
import collections
import os

import pytest

from conformance import baseline as B
from conformance import conftest as CF
from conformance import corpora as C
from conformance import evidence as E
from conformance.oracle.core_own_words import own_words

pytestmark = pytest.mark.conformance
# FCL-CVR re-pin (core-spec/2 section 6 "the STRICT / CV columns of O-4 W / N / F / PIN: reported"; O-1 / O-2 / O-4 stay
# PASS rows for the URL, HOST and SINK lanes; lead ruling 20:40 PT "overtight_words -> strict + URL asserted, CV columns
# reported", G-7 never weaken): NEW reads in the strict and URL lanes still fail; NEW masks in the CV lanes (CV-2 / CV-3
# as written: words glued by | , : / + = -- are one valueish token) are REPORTED to the evidence file. The CV lane is
# judged by CV-8 (test_core_cv8; full pass lead 21:15 PT: word classes 0.0%, cap 2.0%).
# ST-2 / ST-3 re-pin (core-spec/2 ST-3 "strict-lane over-tightening (O-4 W / N / F / PIN STRICT columns) is reported,
# not judged"; C2-11b; G-7: re-pinned to "reported" with the item named, never weakened): the STRICT column moves from
# JUDGED to REPORTED (counts to the evidence file). The URL lanes still cost availability: they stay JUDGED (section 6).
JUDGED_LANES = ("url_raw", "url_enc")
STRICT_REPORTED = ("strict",)                                   # ST-3: reported, not judged
REPORTED_LANES = ("cv_bare", "cv_ready", "cv_value") + STRICT_REPORTED
assert set(JUDGED_LANES) | set(REPORTED_LANES) == set(C.W_LANES)
assert not set(JUDGED_LANES) & set(REPORTED_LANES)


@pytest.fixture(scope="module")
def sg():
    from agents import secret_guard
    return secret_guard


def _allowed(lang, own, ffd, cbd) -> str | None:
    if ffd:
        return "ffd"
    if own:
        return "own_words"
    if lang == "th":
        return "residual_a"
    return None


def test_core_O_1_O_4_W_derived_joins_no_new_read(sg):
    items = C.wsample()
    stream = C.stream_hash(items)
    ffd, cbd = B.read("wsample", "ffd28ca8", stream), B.read("wsample", "cbdf5e78", stream)
    new = collections.Counter()
    cv_new = collections.Counter()
    allowed = collections.Counter()
    for (lang, join, t), bf, bc in zip(items, ffd, cbd):
        got = C.lanes_w(sg, t)
        own = None
        for lane, g, f, c in zip(C.W_LANES, got, bf.split("|"), bc.split("|")):
            if not g:
                continue
            if own is None:
                own = own_words(t)
            # (a URL path's segments are also read joined, U-1: there the join's text without its "/" is the text read)
            o = own or (lane.startswith("url") and own_words(t.replace("/", "")))
            why = _allowed(lang, o, f == "1", c == "1")
            if why is None:
                (cv_new if lane in REPORTED_LANES else new)[f"{lang}|{lane}|{join}"] += 1
            else:
                allowed[f"{lang}|{why}"] += 1
    print(f"O-4 W (CI derived joins): texts={len(items)} NEW={sum(new.values())} allowed by reason="
          f"{dict(sorted(allowed.items()))}")
    by_lane = collections.Counter()
    for k, v in cv_new.items():
        by_lane[k.split("|")[1]] += v
    cv_lanes = {k: v for k, v in by_lane.items() if k not in STRICT_REPORTED}
    E.report("O-4 W CI cv NEW (reported)", {"texts": len(items), **cv_lanes})
    E.report("O-4 W CI STRICT NEW (reported, ST-3)",
             {"texts": len(items), "strict_new": sum(by_lane[x] for x in STRICT_REPORTED),
              **{f"{k.split('|')[0]}|{k.split('|')[2]}": v for k, v in cv_new.items() if k.split("|")[1] in STRICT_REPORTED}})
    assert not new, dict(sorted(new.items())[:30])             # URL lanes only: judged (section 6 O-4 U)


def test_core_O_3_side_b_is_reported(sg):
    """O-3 (lead ruling): texts read now that ffd28ca8 / cbdf5e78 read are reported by language, not judged."""
    items = C.wsample()
    stream = C.stream_hash(items)
    ffd = B.read("wsample", "ffd28ca8", stream)
    now = collections.Counter()
    for (lang, _j, t), bf in zip(items, ffd):
        got = C.lanes_w(sg, t)
        if got[0] and bf.split("|")[0] == "1":
            now[lang] += 1
    print(f"O-3 side B (strict, read by ffd28ca8 and now): {dict(now)}")


@pytest.mark.conformance_heavy
def test_core_O_4_W_full_corpus(sg):
    from conformance import wcorpus
    if not wcorpus.available():
        pytest.skip("O-4 W needs wordfreq==3.1.1 and pythainlp==5.4.0 in the gate's venv (core-spec/1 Q-5)")
    i, n = CF.shard()
    res = wcorpus.run(sg, shard=(i, n), out=os.environ.get("CONFORMANCE_OUT"))
    print(f"O-4 W heavy shard {i}/{n}: {res['summary']}")
    judged = {k: v for k, v in res["new"].items() if k.split("|")[1] not in REPORTED_LANES}  # unknown lane = judged
    E.report(f"O-4 W heavy shard {i}/{n} cv NEW (reported)",
             {k: v for k, v in res["new"].items()
              if k.split("|")[1] in REPORTED_LANES and k.split("|")[1] not in STRICT_REPORTED})
    E.report(f"O-4 W heavy shard {i}/{n} STRICT NEW (reported, ST-3)",
             {k: v for k, v in res["new"].items() if k.split("|")[1] in STRICT_REPORTED})
    assert not judged, dict(sorted(judged.items())[:30])
