"""secret_guard core v2 r2 part 2: padded digit groups, Unicode digits, confusable folding on every rule.

Every sample is generated at run time from fragments (seeded); nothing credential- or number-shaped is written in this
file, and every assertion reports indices / counts only (a failure never prints a sample)."""
from __future__ import annotations

import random
import re

import pytest

from agents import callum
from agents import secret_guard as sg

SEED = 20261109
N = 48                                   # samples per generated class

# ------------------------------------------------------------------------------------------------ fragments

# filler characters (code points): ASCII punctuation / symbols and non-ASCII dots, dashes, bars, shapes, spaces
FILLER_CPS = (0x5F, 0x2C, 0x7C, 0x7E, 0x23, 0x2A, 0x3B, 0x21, 0x27, 0x3D, 0x2B, 0x5E, 0x5C, 0x25, 0x26, 0x24, 0x3F,
              0x2E, 0x2D, 0x2F, 0x3A, 0x22, 0x60, 0xB7, 0x2022, 0x2014, 0xA7, 0xB6, 0x2026, 0x3000, 0xFF0C, 0x2500,
              0x25A0, 0x2605, 0x00A6, 0x2016, 0x30FB)
FILLER_WORDS = ("and", "or", "the", "to", "x", "n", "a", "of", "by", "via")
# decimal digit zeros of other scripts: Devanagari, Arabic-Indic, Extended Arabic-Indic, Bengali, Thai, fullwidth,
# mathematical bold / double-struck / monospace
DIGIT_ZEROS = {"devanagari": 0x966, "arabic_indic": 0x660, "ext_arabic_indic": 0x6F0, "bengali": 0x9E6, "thai": 0xE50,
               "fullwidth": 0xFF10, "math_bold": 0x1D7CE, "math_double": 0x1D7D8, "math_mono": 0x1D7F6}
# confusable lookalikes (code point -> the ASCII letter it reads as): Cyrillic, Greek, Latin small capitals
LOOKALIKES = {
    0x430: "a", 0x441: "c", 0x435: "e", 0x456: "i", 0x458: "j", 0x43E: "o", 0x440: "p", 0x455: "s", 0x445: "x",
    0x443: "y", 0x4BB: "h", 0x501: "d", 0x51B: "q", 0x51D: "w", 0x3BF: "o", 0x3B1: "a",
    0x410: "A", 0x412: "B", 0x421: "C", 0x415: "E", 0x41D: "H", 0x406: "I", 0x408: "J", 0x41A: "K", 0x41C: "M",
    0x41E: "O", 0x420: "P", 0x405: "S", 0x422: "T", 0x425: "X", 0x4AE: "Y", 0x391: "A", 0x392: "B", 0x395: "E",
    0x396: "Z", 0x397: "H", 0x399: "I", 0x39A: "K", 0x39C: "M", 0x39D: "N", 0x39F: "O", 0x3A1: "P", 0x3A4: "T",
    0x3A5: "Y", 0x3A7: "X",
}
# letters that NFKC already reads as ASCII (fullwidth, mathematical bold), used alongside the table above
NFKC_UPPER = (0xFF21, 0x1D400)
NFKC_LOWER = (0xFF41, 0x1D41A)

UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LOWER = UPPER.lower()
DIGITS = "0123456789"


def _by_letter() -> dict:
    out: dict = {}
    for cp, ch in LOOKALIKES.items():
        out.setdefault(ch, []).append(chr(cp))
    for i, ch in enumerate(UPPER):
        out.setdefault(ch, []).extend(chr(b + i) for b in NFKC_UPPER)
        out.setdefault(ch.lower(), []).extend(chr(b + i) for b in NFKC_LOWER)
    return out


SUBS = _by_letter()


def _disguise(rng: random.Random, s: str, rate: float, table: dict | None = None) -> str:
    table = SUBS if table is None else table
    return "".join(rng.choice(table[c]) if c in table and rng.random() < rate else c for c in s)


def _confusables_only() -> dict:
    """The Cyrillic / Greek lookalikes alone (no NFKC letters): only the new folded view reads them."""
    out: dict = {}
    for cp, ch in LOOKALIKES.items():
        out.setdefault(ch, []).append(chr(cp))
    return out


def _filler(rng: random.Random) -> str:
    kind = rng.randrange(5)
    k = rng.randint(4, 8)
    if kind == 0:
        return chr(rng.choice(FILLER_CPS)) * k
    if kind == 1:
        return "".join(chr(rng.choice(FILLER_CPS)) for _ in range(k))
    if kind == 2:
        return " " + chr(rng.choice(FILLER_CPS)) * (k - 2) + " "
    pad = chr(rng.choice(FILLER_CPS)) * rng.randint(0, 3)
    w = rng.choice(FILLER_WORDS)
    return (" " + w + " ") if kind == 3 else (pad + w + pad if pad else "  " + w + "  ")


def _num(rng: random.Random, n: int) -> str:
    return str(rng.randint(1, 9)) + "".join(rng.choice(DIGITS) for _ in range(n - 1))


PII_SHAPES = {"card": (4, 4, 4, 4), "ssn": (3, 2, 4), "phone": (3, 3, 4), "phone_intl": (2, 3, 3, 4),
              "iban_len": (4, 4, 4, 4, 4, 2)}


def _padded(rng: random.Random, sizes: tuple, script: str | None = None) -> str:
    ds = _num(rng, sum(sizes))
    groups, at = [], 0
    for k in sizes:
        groups.append(ds[at:at + k])
        at += k
    out = groups[0]
    for g in groups[1:]:
        out += _filler(rng) + g
    if script == "mixed":
        zs = list(DIGIT_ZEROS.values())
        return "".join(chr(rng.choice(zs) + int(c)) if c.isdigit() else c for c in out)
    if script is not None:
        z = DIGIT_ZEROS[script]
        return "".join(chr(z + int(c)) if c.isdigit() else c for c in out)
    return out


def _rand(rng: random.Random, alphabet: str, n: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


def _mixed(rng: random.Random, n: int) -> str:
    while True:
        s = _rand(rng, UPPER + LOWER + DIGITS, n)
        if any(c in UPPER for c in s) and any(c in LOWER for c in s) and any(c in DIGITS for c in s):
            return s


def _prefix_key(rng: random.Random, i: int) -> str:
    alnum = UPPER + LOWER + DIGITS
    forms = (
        lambda: "AK" + "IA" + _rand(rng, UPPER + DIGITS, 16),
        lambda: "gh" + "p_" + _mixed(rng, 36),
        lambda: "sk-" + "proj-" + _mixed(rng, 40),
        lambda: "sk" + "_live_" + _mixed(rng, 24),
        lambda: "xox" + "b-" + _rand(rng, alnum, 30),
        lambda: "AI" + "za" + _mixed(rng, 35),
        lambda: "gl" + "pat-" + _mixed(rng, 20),
        lambda: "hf" + "_" + _mixed(rng, 34),
        lambda: "pp" + "lx-" + _mixed(rng, 40),
    )
    return forms[i % len(forms)]()


def _email(rng: random.Random) -> str:
    local = _rand(rng, LOWER, rng.randint(3, 8)) + rng.choice((".", "_", "")) + _rand(rng, LOWER, rng.randint(2, 6))
    dom = _rand(rng, LOWER, rng.randint(4, 10)) + "." + rng.choice(("com", "net", "org", "io", "co"))
    return local, dom


def classes() -> dict:
    """class name -> list of generated samples (each must be withheld)."""
    rng = random.Random(SEED)
    out: dict = {}
    for name, sizes in PII_SHAPES.items():
        out["padded_" + name] = ["see " + _padded(rng, sizes) + " ok" for _ in range(N)]
    for script in list(DIGIT_ZEROS) + ["mixed"]:
        out["script_" + script] = ["see " + _padded(rng, PII_SHAPES[rng.choice(list(PII_SHAPES))], script) + " ok"
                                   for _ in range(N)]
    conf = _confusables_only()
    keys = []
    for i in range(N * 2):
        k = _prefix_key(rng, i)
        cut = k.index("_") if "_" in k[:8] else k.index("-") if "-" in k[:8] else 4
        keys.append("use " + _disguise(rng, k[:cut], 1.0, conf) + _disguise(rng, k[cut:], 0.3) + " end")
    out["prefix_lookalike"] = keys
    out["mixed_token_lookalike"] = ["value " + _disguise(rng, _mixed(rng, rng.randint(24, 48)), 0.4) + " end"
                                    for _ in range(N)]
    mails = []
    for _ in range(N):
        local, dom = _email(rng)
        at = rng.choice(("@", chr(0xFF20), chr(0xFE6B)))
        d = _disguise(rng, dom, 0.5, conf)
        if d == dom:
            d = conf["o"][0] + dom
        mails.append("write to " + _disguise(rng, local, 0.3) + at + d + " please")
    out["email_lookalike"] = mails
    return out


def utility() -> dict:
    """class name -> list of ordinary texts (each must be kept)."""
    rng = random.Random(SEED + 1)
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    out = {
        "prices": [f"Item costs ${rng.randint(1, 999)}.{rng.randint(0, 99):02d} today" for _ in range(N)]
        + [f"Total ${rng.randint(1, 9)},{rng.randint(100, 999)}.{rng.randint(0, 99):02d} incl. tax" for _ in range(N)],
        "dates": [f"Published {rng.randint(2000, 2030)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
                  for _ in range(N)]
        + [f"on {rng.randint(1, 12)}/{rng.randint(1, 28)}/{rng.randint(2000, 2030)} we met" for _ in range(N)]
        + [f"{rng.choice(months)} {rng.randint(1, 28)}, {rng.randint(2000, 2030)} launch" for _ in range(N)],
        "years": [f"from {rng.randint(1990, 2020)} to {rng.randint(2021, 2030)} sales grew" for _ in range(N)],
        "zip": [f"Los Angeles, CA {rng.randint(90001, 96162)} store" for _ in range(N)]
        + [f"ship to ZIP {rng.randint(10001, 99950)} please" for _ in range(N)],
        "quantities": [f"order quantity {_num(rng, rng.randint(3, 8))} units" for _ in range(N)],
        "padded_short": [f"{rng.randint(0, 99)} {chr(rng.choice(FILLER_CPS)) * rng.randint(4, 8)} "
                         f"{rng.randint(0, 99)} rows" for _ in range(N)],
    }
    return out


# ------------------------------------------------------------------------------------------------ entry points

def _withheld_everywhere(p: str) -> tuple:
    """(check, redact_text, guard_record value, guard_record key, callum excerpt) each True when withheld."""
    rec_v = sg.guard_record({"v": p})
    rec_k = sg.guard_record({p: "x"})
    ex = callum.excerpt("Answer: " + p + " end.", [])
    return (sg.check(p) is not None,
            sg.redact_text(p) == (sg.WITHHELD, 1),
            isinstance(rec_v, dict) and rec_v.get("v") == sg.WITHHELD,
            isinstance(rec_k, dict) and p not in rec_k and all(str(k).startswith(sg.WITHHELD) for k in rec_k),
            ex.get("withheld") is not None and p not in repr(ex))


ENTRIES = ("check", "redact_text", "guard_record_value", "guard_record_key", "callum_excerpt")


def test_lookalike_table_folds_to_the_letter_it_imitates():
    bad = [cp for cp, ch in LOOKALIKES.items() if sg._ascii_fold(chr(cp)) != ch]
    assert not bad, f"{len(bad)} lookalikes do not fold"
    bad_d = [z for z in DIGIT_ZEROS.values() for d in range(10) if sg._ascii_fold(sg.normalise(chr(z + d))) != str(d)]
    assert not bad_d, f"{len(bad_d)} digits do not fold"


@pytest.mark.parametrize("cls", sorted(classes()))
def test_generated_class_is_withheld_by_every_entry_point(cls):
    samples = classes()[cls]
    assert len(samples) >= N
    misses = {e: [] for e in ENTRIES}
    for i, p in enumerate(samples):
        for e, ok in zip(ENTRIES, _withheld_everywhere(p)):
            if not ok:
                misses[e].append(i)
    counts = {e: len(v) for e, v in misses.items() if v}
    assert not counts, f"{cls}: came through (count per entry point, of {len(samples)}): {counts}"


@pytest.mark.parametrize("cls", sorted(utility()))
def test_ordinary_numbers_are_kept(cls):
    samples = utility()[cls]
    kept = [sg.check(p) is None and sg.redact_text(p) == (p, 0) for p in samples]
    bad = [i for i, k in enumerate(kept) if not k]
    assert not bad, f"{cls}: {len(bad)} of {len(samples)} withheld"


def test_the_safe_shape_mark_is_not_typeable():
    """The safe-shape mark is a private-use character every view drops, so a filler written with the old mark's
    character (U+00A7) cannot act as a barrier."""
    assert all(ord(c) >= 0xE000 for c in sg._MASK.strip())
    rng = random.Random(SEED + 3)
    ds = _num(rng, 16)
    q = "see " + (chr(0xA7) * 5).join(ds[i:i + 4] for i in range(0, 16, 4)) + " ok"
    assert sg.check(q) is not None


# ------------------------------------------------------------------------------------------------ mutation bite

def _bite_probes() -> dict:
    rng = random.Random(SEED + 4)
    conf = _confusables_only()
    akia = "AK" + "IA" + _rand(rng, UPPER + DIGITS, 16)
    akia_disguised = "".join(conf[c][0] if c in "AK" and i < 4 else c for i, c in enumerate(akia))
    local, dom = _email(rng)
    mail = "write to " + local + "@" + conf["o"][0] + dom.replace("o", conf["o"][0]) + " please"
    ds = _num(rng, 16)
    groups = [ds[i:i + 4] for i in range(0, 16, 4)]
    zero = DIGIT_ZEROS["devanagari"]
    deva = "see " + ",,,,".join("".join(chr(zero + int(c)) for c in g) for g in groups) + " ok"
    commas = "see " + ",,,,".join(groups) + " ok"
    # two-digit groups: the word bridge (review adacb2ca MUST 2, 3-4 digit groups only) does not cover them, so this
    # probe still bites the letter bridge alone
    words = "see " + " and ".join(ds[i:i + 2] for i in range(0, 16, 2)) + " ok"
    tok = _mixed(rng, 40)
    while sum(c.isdigit() for c in tok) > 4 or sum(c.isdigit() for c in tok) < 2:
        tok = _mixed(rng, 40)
    barred = "value " + "||||".join(tok[i:i + 5] for i in range(0, 40, 5)) + " end"
    pieces = " ,,,, ".join(_rand(rng, LOWER, 1) + _rand(rng, UPPER, 1) + _rand(rng, LOWER, 1) + _rand(rng, UPPER, 1)
                           + _rand(rng, DIGITS, 1) for _ in range(8))
    spaced = "value " + pieces + " end"
    old_compact = re.compile(r"[\s.\-/()]+")
    return {
        "fold_digits": ([("_fold_digits", lambda v: v)], [deva]),
        "fold_confusables": ([("_fold_confusables", lambda v: v)], [akia_disguised, mail]),
        "digit_filler_removed": ([("_DIGIT_FILLER", old_compact)], [commas]),
        "digit_letter_bridge": ([("_DIGIT_RUN", re.compile(r"[0-9]+"))], [words]),
        "token_filler_rejoin": ([("_TOK_FILLER", re.compile(r"(?!x)x"))], [barred]),
        "filler_only_token_skip": ([("_ALNUM", re.compile(r""))], [spaced]),
    }


@pytest.mark.parametrize("name", sorted(_bite_probes()))
def test_each_new_view_and_step_is_load_bearing(monkeypatch, name):
    patches, probes = _bite_probes()[name]
    before = [sg._kind(sg.check(p)) for p in probes]
    assert all(b is not None for b in before), (name, before)
    for attr, value in patches:
        monkeypatch.setattr(sg, attr, value)
    after = [sg._kind(sg.check(p)) for p in probes]
    assert any(a != b for a, b in zip(after, before)), (name, "removing this step broke nothing")
