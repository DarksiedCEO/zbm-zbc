"""core-spec/2 CV-9: the frozen value generator VG (FROZEN; a change is a spec change).

  values(seed, n)  valueish values: length uniform 8..24; exactly 1 symbol drawn uniformly from ALL 32 ASCII
                   punctuation characters (``string.punctuation``); 1..max(1, len // 4) ASCII digits (count uniform);
                   the rest ASCII letters, each letter's case drawn independently (mixed case); shuffled.
  prose(seed, n)   prose values (BL): ordinary lower-case words of 4+ letters drawn uniformly from the v1
                   220-word-per-script samples (``data/words.json``, the scripts with >= 220 words).
Seeds {20261114, 51, 7}. A pure function of (seed, n): the first m of values(seed, n) are values(seed, m).
Values exist only at run time: never written to disk, never printed, never committed. ``manifest()`` gives (N,
sha256) of the frozen sets for HANDOFF / certification evidence; ``MANIFEST`` pins them."""
import random
import string
import unicodedata

from conformance.corpora import WORDS, stream_hash

SEEDS = (20261114, 51, 7)
SYMBOLS = string.punctuation
DIGITS = string.digits
LETTERS = string.ascii_letters
LEN_MIN, LEN_MAX = 8, 24
PROSE_LANGS = tuple(sorted(lang for lang, ws in WORDS.items() if len(ws) >= 220))
N_VALUES_MANIFEST = 10_000
N_PROSE_MANIFEST = 2_000


def _rng(seed: int, stream: str) -> random.Random:
    return random.Random(f"core-spec/2:VG:{stream}:{seed}")


def _one(r: random.Random) -> str:
    n = r.randint(LEN_MIN, LEN_MAX)
    d = r.randint(1, max(1, n // 4))
    chars = [r.choice(SYMBOLS)] + [r.choice(DIGITS) for _ in range(d)]
    chars += [r.choice(LETTERS) for _ in range(n - 1 - d)]
    r.shuffle(chars)
    return "".join(chars)


def values(seed: int, n: int) -> list:
    """The first ``n`` VG values of ``seed``."""
    r = _rng(seed, "value")
    return [_one(r) for _ in range(n)]


def _is_prose_word(w: str) -> bool:
    return (w == w.lower() and sum(c.isalpha() for c in w) >= 4
            and all(c.isalpha() or unicodedata.category(c)[0] == "M" for c in w))


def prose_pool() -> list:
    """The prose-value pool: every lower-case 4+ letter word (letters and combining marks only) of the >= 220-word
    script samples, de-duplicated, in a fixed order."""
    out = []
    for lang in PROSE_LANGS:
        out.extend(w for w in WORDS[lang] if _is_prose_word(w))
    return list(dict.fromkeys(out))


def prose(seed: int, n: int) -> list:
    """The first ``n`` prose values of ``seed`` (uniform over ``prose_pool()``)."""
    r = _rng(seed, "prose")
    pool = prose_pool()
    return [r.choice(pool) for _ in range(n)]


def manifest() -> dict:
    """{set name: {"n": N, "sha256": stream hash}} of the frozen VG sets (values never leave the process)."""
    out = {}
    for s in SEEDS:
        out[f"values:{s}"] = {"n": N_VALUES_MANIFEST, "sha256": stream_hash(values(s, N_VALUES_MANIFEST))}
        out[f"prose:{s}"] = {"n": N_PROSE_MANIFEST, "sha256": stream_hash(prose(s, N_PROSE_MANIFEST))}
    out["prose_pool"] = {"n": len(prose_pool()), "sha256": stream_hash(prose_pool())}
    return out


MANIFEST = {
    "values:20261114": {"n": 10000, "sha256": "92d8bb9af12cd6ce35f5bde00c8c29588dd471c06f11e2bf25613fb0cded78cc"},
    "prose:20261114": {"n": 2000, "sha256": "a207321a336209d5d9fd834145ad7707d05244acd9d8088f66dc36374511c6aa"},
    "values:51": {"n": 10000, "sha256": "50f032d518ad0de574638fd77a33315ba2d5ff6a917f49885e1e6621b4ecf6d1"},
    "prose:51": {"n": 2000, "sha256": "bcfc68f87c7f3cc628872832d3e6bc062aa92fb26439792450146fe36fc98093"},
    "values:7": {"n": 10000, "sha256": "1fc0168632d8e8dbdb28aca31160a856706fc483d9c6c1ae4b992bc3eaf94e0b"},
    "prose:7": {"n": 2000, "sha256": "a1dcfda61339848ad52081455e8ea4744643510d06164d6a60ae29bb301702f9"},
    "prose_pool": {"n": 954, "sha256": "3093edbcef940ff52ae45cc54f058b8aaff6d6e6ec6791ce1f39d53b8fe2058c"},
}
