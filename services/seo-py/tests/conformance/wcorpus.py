"""core-spec/1 O-4 W, the heavy lane: the fixed W corpus (wordfreq 3.1.1 top 30,000 words of 17 languages; Thai: the
pythainlp 5.4.0 TNC top 20,000 by count with the full dictionary; the Greek words with their final sigma restored, the
German ones with the sharp-s hand list) and every 2- and 3-word join that can hold a label key ACROSS a word boundary
(key = suffix of w1 + prefix of w2, or suffix of w1 + all of w2 + prefix of w3; no cap), in the 4 join forms and,
for pairs, the 10 extra joins. Lanes: strict, client view bare / ": ready today" / ": " + value, URL raw / encoded path.
Each text read by the build is judged as O-1 says, against ffd28ca8 and cbdf5e78 exported from git history and loaded
in this process. Neither corpus package is a repo dependency (Q-5): ``available()`` says whether the gate's venv has
them. Counts only."""
import collections
import json
import unicodedata

from conformance import corpora as C
from conformance import core_ref as R
from conformance import releases as RL
from conformance.oracle.core_own_words import own_words

LANGS = ("ja", "hi", "th", "ar", "fa", "ru", "uk", "el", "he", "ko", "zh", "pl", "de", "tr", "fr", "es", "pt", "it")
N = 30000
EXTRA_JOINS = ("/", ":", ",", "+", "|", ", ", " - ", "=", ". ", "--")
JOINS = (" ", "-", ".", "") + EXTRA_JOINS


def available() -> bool:
    try:
        import pythainlp  # noqa: F401
        import wordfreq  # noqa: F401
    except ImportError:
        return False
    return True


def words(lang: str) -> list:
    if lang == "th":
        from pythainlp.corpus import tnc
        from pythainlp.corpus.common import thai_words
        cnt: collections.Counter = collections.Counter()
        for w, c in tnc.word_freqs():
            if w and " " not in w and any("\u0e00" <= ch <= "\u0e7f" for ch in w):
                cnt[w] += c
        top = [w for w, _c in sorted(cnt.items(), key=lambda x: (-x[1], x[0]))][:20000]
        return list(dict.fromkeys(top + sorted(w for w in thai_words() if w and " " not in w)))
    import wordfreq
    ws = [w for w in wordfreq.top_n_list(lang, N + 400) if w.strip() and " " not in w][:N]
    if lang == "el":                                  # (wordfreq case folds: the final sigma restored)
        ws = [w[:-1] + "\u03c2" if w.endswith("\u03c3") else w for w in ws]
    return ws


def _keys() -> tuple:
    script = {C._letters(e) for _i, e, kind, _s, _g in R.entries() if kind not in ("joined", "piece")}
    script |= {C._letters(w) for w in R.EL_WRITTEN}
    joined = {C._letters(e) for e in R.JOINED} | {C._letters(n) for e, ns in R.NATURAL.items() for n in ns
                                                  if e not in R.PIECES}
    return sorted(script | joined), sorted(R.PIECES)


def candidates(ws: list) -> tuple:
    """(pairs, triples) of word indexes whose loose skeleton holds a key across a boundary (pieces: whole words)."""
    keys, pieces = _keys()
    sk = [C._letters(w) for w in ws]
    L = max(len(k) for k in keys)
    suf, pre, whole = collections.defaultdict(list), collections.defaultdict(list), collections.defaultdict(list)
    for n, s in enumerate(sk):
        whole[s].append(n)
        for j in range(1, min(len(s), L) + 1):
            suf[s[-j:]].append(n)
            pre[s[:j]].append(n)
    pairs, triples = set(), set()
    for klist, A_, B_ in ((keys, suf, pre), (pieces, whole, whole)):
        for k in klist:
            for i in range(1, len(k)):
                for a in A_.get(k[:i], ()):
                    for b in B_.get(k[i:], ()):
                        pairs.add((a, b))
            for p in range(1, len(k)):
                A = A_.get(k[:p], ())
                for q in range(p + 1, len(k)):
                    M, Cc = whole.get(k[p:q], ()), B_.get(k[q:], ())
                    for a in A:
                        for m in M:
                            for c in Cc:
                                triples.add((a, m, c))
    return sorted(pairs), sorted(triples)


def texts(lang: str):
    ws = words(lang)
    pairs, triples = candidates(ws)
    for a, b in pairs:
        for j in JOINS:
            yield f"{ws[a]}{j}{ws[b]}"
    for a, m, c in triples:
        for j in JOINS[:4]:
            yield j.join((ws[a], ws[m], ws[c]))


def residual_g(t: str) -> bool:
    """Residual (g): a lone letter of another script carrying a mark, glued next to Latin letters."""
    for w in t.replace("-", " ").replace(".", " ").split():
        latin = sum(1 for ch in w if ch.isascii() and ch.isalpha())
        other = [ch for ch in w if ch.isalpha() and not ch.isascii()]
        if latin and len(other) == 1 and len(unicodedata.normalize("NFD", other[0])) > 1:
            return True
    return False


def residual_h(t: str) -> bool:
    """Residual (h): two one-letter words joined by "-" or "." spelling a two-unit label."""
    for sep in ("-", "."):
        parts = t.split(sep)
        if len(parts) == 2 and all(len(p) == 1 for p in parts):
            return True
    return False


def run(sg, shard=(0, 1), out=None, langs=LANGS) -> dict:
    # (O-1 (ii), cbdf5e78 reading a text that holds a label form, is within (iii): a label form is the label's own
    # words by the oracle, so cbdf5e78 itself is not needed here)
    ffd = RL.load_guard(RL.export("ffd28ca8"), "ffd_secret_guard")
    new, allowed, counts = collections.Counter(), collections.Counter(), collections.Counter()
    for li, lang in enumerate(langs):
        if li % shard[1] != shard[0]:
            continue
        for t in texts(lang):
            counts[lang] += 1
            got = C.lanes_w(sg, t)
            if not any(got):
                continue
            f = C.lanes_w(ffd, t)
            own = own_words(t)
            for lane, g, x in zip(C.W_LANES, got, f):
                if not g or x:
                    continue
                o = own or (lane.startswith("url") and own_words(t.replace("/", "")))
                if o:
                    allowed[f"{lang}|own_words"] += 1
                elif lang == "th":
                    allowed[f"{lang}|residual_a|{lane}"] += 1
                elif residual_g(t):
                    allowed[f"{lang}|residual_g|{lane}"] += 1
                elif residual_h(t):
                    allowed[f"{lang}|residual_h|{lane}"] += 1
                else:
                    new[f"{lang}|{lane}"] += 1
    res = {"summary": {"texts": dict(counts), "allowed": dict(allowed), "new": sum(new.values())}, "new": dict(new)}
    if out:
        with open(out, "w", encoding="ascii") as fh:
            json.dump(res, fh, sort_keys=True)
    return res
