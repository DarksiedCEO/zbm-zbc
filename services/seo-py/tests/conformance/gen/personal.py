"""INTERIM AC-2 generator (CLARIFICATIONS v1.1 CL-4): priya-spec/1 classes C1-C7 x disguises D01, D06, D08, D09, D10 x
contexts X0-X12, seeded. Every value is SYNTHETIC, assembled at run time (no personal-data literal in the source).
Replaced by Priya's own generator when it merges (then the full D list applies).

``cases(n, seed)`` -> [(case_id, carrier, value)]: ``carrier`` is the text as planted; ``value`` the plain value that
must never be readable outside the owner's vault views."""

from __future__ import annotations

import random

CLASSES = ("C1", "C2", "C3", "C4", "C5", "C6", "C7")
DISGUISES = {"D01": CLASSES, "D06": ("C1", "C2", "C3", "C4", "C5"), "D08": ("C1", "C2", "C3", "C4", "C5", "C7"),
             "D09": ("C1", "C2", "C3", "C4", "C5", "C6", "C7"), "D10": CLASSES}
CONTEXTS = ("X0",) + tuple(f"X{i}" for i in range(1, 13))
_WORDS = {"X1": ("call", "today"), "X2": ("llamar", "hoy"), "X3": ("appeler", "maintenant"), "X4": ("anrufen", "jetzt"),
          "X5": ("ligar", "agora"), "X6": ("chiamare", "oggi"), "X7": ("\u96fb\u8a71", "\u4eca"),
          "X8": ("\u7535\u8bdd", "\u4eca\u5929"), "X9": ("\uc804\ud654", "\uc624\ub298"),
          "X10": ("\u0437\u0432\u043e\u043d\u0438\u0442\u044c", "\u0441\u0435\u0433\u043e\u0434\u043d\u044f"),
          "X11": ("\u0627\u062a\u0635\u0644", "\u0627\u0644\u064a\u0648\u0645"),
          "X12": ("\u0915\u0949\u0932", "\u0906\u091c")}
_SEPS = ("-", ".", " ", "/", "\u2013", "\u00b7", "_")
_DIGIT_BLOCKS = (0x0660, 0x06F0, 0x0966, 0x09E6, 0x0E50, 0xFF10)
_ZW = ("\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\u2060", "\ufeff", "\u00ad")


def _luhn(rng, prefix: str, length: int) -> str:
    body = prefix + "".join(rng.choice("0123456789") for _ in range(length - len(prefix) - 1))
    total = 0
    for i, ch in enumerate(reversed(body)):
        d = int(ch)
        if i % 2 == 0:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return body + str((10 - total % 10) % 10)


def value(rng, cls: str) -> str:
    if cls == "C1":
        return _luhn(rng, rng.choice(("4", "51", "37", "6011")), 16 if rng.random() < 0.8 else 15)
    if cls == "C2":
        return f"{rng.randint(2, 9)}{rng.randint(0, 99):02d}{rng.randint(2, 9)}{rng.randint(0, 99):02d}" \
               f"{rng.randint(0, 9999):04d}"
    if cls == "C3":
        return "+" + str(rng.randint(1, 9)) + "".join(rng.choice("0123456789") for _ in range(rng.randint(8, 13)))
    if cls == "C4":
        return rng.choice(("800", "833", "844", "877", "888")) + "".join(
            rng.choice("ABCDEFGHJKLMNPRSTUVWXY") for _ in range(7))
    if cls == "C5":
        return f"{rng.randint(1, 665):03d}{rng.randint(1, 99):02d}{rng.randint(1, 9999):04d}"
    if cls == "C6":
        local = "".join(rng.choice("abcdefghijklmnop") for _ in range(rng.randint(4, 9)))
        return f"{local}@{rng.choice(('mail', 'inbox', 'post'))}{rng.randint(1, 99)}.{rng.choice(('com', 'org', 'io'))}"
    n = rng.randint(10, 9999)
    street = rng.choice(("Maple", "Cedar", "Harbor", "Sunset", "Mission"))
    return f"{n} {street} {rng.choice(('Street', 'Avenue', 'Blvd', 'Road'))}, Springfield, CA {rng.randint(90001, 96162)}"


def disguise(rng, text: str, d: str) -> str:
    if d == "D01":
        return text
    if d == "D06":
        out = []
        for i, ch in enumerate(text):
            out.append(ch)
            if ch.isdigit() and i % rng.randint(2, 4) == 0:
                out.append("".join(rng.choice(_SEPS) for _ in range(rng.randint(1, 3))))
        return "".join(out)
    if d == "D08":
        base = rng.choice(_DIGIT_BLOCKS)
        return "".join(chr(base + int(ch)) if ch.isdigit() else ch for ch in text)
    if d == "D09":
        return "".join(chr(ord(ch) + 0xFEE0) if "!" <= ch <= "~" else ch for ch in text)
    return "".join(ch + (rng.choice(_ZW) if rng.random() < 0.4 else "") for ch in text)


def cases(n: int, seed: int = 2) -> list:
    rng = random.Random(seed)
    out = []
    combos = [(c, d) for d, cs in DISGUISES.items() for c in cs]
    for i in range(n):
        cls, d = combos[i % len(combos)]
        x = CONTEXTS[(i // len(combos)) % len(CONTEXTS)]
        v = value(rng, cls)
        carrier = disguise(rng, v, d)
        if x != "X0":
            a, b = _WORDS[x]
            carrier = rng.choice((f"{a} {carrier}", f"{carrier} {b}", f"{a}\t{carrier} {b}"))
        out.append((f"{cls}:{d}:{x}:{i}", carrier, v))
    return out
