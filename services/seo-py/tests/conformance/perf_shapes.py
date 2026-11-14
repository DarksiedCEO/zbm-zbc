"""core-spec/1 PF-3: adversarial input shapes (each a function of the target length), and the timing helpers of PF-1 /
PF-3. No value is a credential; label words are \\u escapes."""
import time

RU = "\u043f\u0430\u0440\u043e\u043b"
KO = "\ube44\ubc00\ubc88\ud638"
AR1, AR2 = "\u0643\u0644\u0645\u0629", "\u0627\u0644\u0645\u0631\u0648\u0631"
EL = "\u03ba\u03c9\u03b4\u03b9\u03ba\u03cc\u03c2"
HE = "\u05e1\u05d9\u05e1\u05de\u05d4"
HI = "\u092a\u093e\u0938\u0935\u0930\u094d\u0921"
TH = "\u0e23\u0e2b\u0e31\u0e2a\u0e1c\u0e48\u0e32\u0e19"


def _rep(unit: str, n: int) -> str:
    return (unit * (n // len(unit) + 1))[:n]


SHAPES = {
    "mark_run": lambda n: "a" + "\u0301" * (n - 1),
    "one_script_cyrillic": lambda n: _rep("\u0434\u043e\u043c \u0440\u0435\u043a\u0430 ", n),
    "glued_hindi": lambda n: _rep(HI[:3] + "\u0915\u093e", n),
    "glued_thai": lambda n: _rep(TH[:4] + "\u0e01\u0e32", n),
    "many_separators": lambda n: _rep(" :=-|/., ", n),
    "arabic_proclitics_glued": lambda n: _rep("\u0648\u0628" + AR1, n),
    "greek_code_glued": lambda n: _rep(EL, n),
    "korean_compound_hyphen": lambda n: _rep(KO[:2] + "-", n),
    "tatweel_in_label": lambda n: AR1[:2] + "\u0640" * (n - 4) + AR1[2:],
    "hebrew_prefix_run": lambda n: _rep("\u05d5\u05d4\u05d1", n) + HE,
    "spelled_near_miss": lambda n: _rep(" ".join(RU[:-1]) + " ", n),
    "korean_particles": lambda n: _rep(KO + "\ub97c\uc740", n),
    "tag_back_refs": lambda n: _rep("<b>x</b><i>", n),
    # one aimed at each new pattern of round 8
    "split_once_near_miss": lambda n: _rep(RU[:2] + " " + RU[2:4] + " ", n),
    "split_korean_particles": lambda n: _rep(KO[:2] + " " + KO[2:] + "\ub97c ", n),
    "spelled_piece_runs": lambda n: _rep("a u t h - x ", n),
    "camel_short_tails": lambda n: _rep("userAut ", n),
    "inword_f2_joins": lambda n: _rep("\ub4e4" + KO[:2] + ".:", n),
    "inword_f3_spelled": lambda n: _rep("\u0434\u043e" + " / ".join(RU[:-1]) + " ", n),
}


def best(fn, text: str, k: int = 3) -> float:
    out = []
    for _ in range(k):
        t = time.perf_counter()
        fn(text)
        out.append(time.perf_counter() - t)
    return min(out)


def linearity(sg, sizes, k: int = 3) -> dict:
    """shape -> lane -> [best seconds per size]."""
    out: dict = {}
    for name, make in SHAPES.items():
        for lane, fn in (("strict", sg.check), ("cv", sg.client_view_findings)):
            out.setdefault(name, {})[lane] = [best(fn, make(n), k) for n in sizes]
    return out
