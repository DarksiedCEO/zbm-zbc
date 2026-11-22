"""G-6 / P-6: the mutants of core-spec/1 over the changed code of round 8 (and the six P-6 mutants and three other
survivors of review rv-sink8). Each mutant is ONE source edit of ``src/agents/secret_guard.py``; the runner copies the
service into a temporary directory, applies it, and runs the conformance CI lane and the label review files (stopping
at the first failure). A mutant is KILLED when a test fails. Counts and names only.

    python tests/conformance/tools/mutants.py [name,...]
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SG = "src/agents/secret_guard.py"
# (the targeted modules first: a killed mutant stops at its first failure)
FIRST = ["test_core_protection_pins.py", "test_core_labels_closed.py", "test_core_pinned_collisions.py",
         "test_core_e2e.py", "test_core_matrix.py", "test_core_matrix_url.py"]
REVIEWS = ["tests/test_review_0334eeae.py", "tests/test_review_cbb6d7b7.py", "tests/test_review_cbdf5e78.py",
           "tests/test_review_601e328a.py", "tests/test_review_cd0a6c7d.py", "tests/test_review_ffd28ca8.py"]
TESTS = ([f"tests/conformance/{f}" for f in FIRST] + REVIEWS
         + sorted(f"tests/conformance/{p.name}" for p in (ROOT / "tests" / "conformance").glob("test_*.py")
                  if p.name not in FIRST))

MUTANTS = {
    # the six P-6 mutants (rv-sink8 survivors that were real gaps)
    "strict_gap_no_digit": ('_STRICT_GAP = "[\\\\W\\\\d_]"', '_STRICT_GAP = "[\\\\W_]"'),
    "strict_no_proclitic": ('    pre = "" if prefix is None else f"[{re.escape(prefix[0])}]{{0,{prefix[1]}}}"\n'
                            '    ending = _ending_alt(endings, last, None)',
                            '    pre = ""\n    ending = _ending_alt(endings, last, None)'),
    "strict_join_1to4": ('_STRICT_JOIN = "[\\\\W\\\\d_]{1,3}"', '_STRICT_JOIN = "[\\\\W\\\\d_]{1,4}"'),
    "inword_gap_val_1": ('(?P<val>[^\\\\s\\"\'`<>|,;&)\\\\]]{4,})")\n_PIECE_RX',
                         '(?P<val>[^\\\\s\\"\'`<>|,;&)\\\\]]{1,})")\n_PIECE_RX'),
    "strict_whole_lt2": ("    whole = len(units) < 3\n", "    whole = len(units) < 2\n"),
    "strict_whole_no_end": ('    end = "(?![^\\\\W\\\\d_])" if whole else', '    end = "" if whole else'),
    # the other three rv-sink8 survivors
    "spelled_whole_no_lookahead": ('(?![^\\\\W\\\\d_])(?!{e}[^\\\\W\\\\d_])"', '(?![^\\\\W\\\\d_])"'),
    "strict_marked_joins": ("    out = [f\"{lead}{_strict_spelled(units)}{end}\"]\n    if unspaced or marked:",
                            "    out = [f\"{lead}{_strict_spelled(units)}{end}\"]\n    if unspaced:"),
    "written_accents_any_script": ('out.append(_GREEK_ACCENTED.get(base, re.escape(c)) if _first_name(base) == "GREEK" '
                                   'else re.escape(c))', 'out.append(_GREEK_ACCENTED.get(base, re.escape(c)))'),
    # round 8: SP-24, SP-34, SP-35
    "sp24_off": ("        out.append(f\"(?<!{first}){spelled_words}{_ending_alt(endings, last, '_')}(?!{last})\")\n",
                 ""),
    "sp24_words_apart_any": ('spelled_words = " ".join(', 'spelled_words = "[ _]?".join('),
    "sp34_off": ('            out.append(f"(?<!{first}){apart}(?!{last})")\n', ""),
    "sp34_hyphen_too": ('apart = "[ _]".join(', 'apart = "[ _\\\\-]".join('),
    "sp34_open_end": ('            out.append(f"(?<!{first}){apart}(?!{last})")', '            out.append(f"(?<!{first}){apart}")'),
    "sp35_off": ('            out.append(f"(?<!{first}){joined_w}(?!{last})")\n', ""),
    # round 8: SP-29 (in-word joins and spelled)
    "inword_join_generic": ("    between = _INWORD_JOIN if inword", "    between = _STRICT_JOIN if inword"),
    "inword_join_no_spaced_dash": ('_INWORD_JOIN = "(?: - |', '_INWORD_JOIN = "(?:'),
    "inword_gap_spacers": ('_INWORD_GAP = "(?: ?[\\\\/,:|+;~*=', '_INWORD_GAP = "(?: ?[ _\\\\-.\\\\/,:|+;~*='),
    "inword_spelled_off": ('            seq.append(f"{_inword_spelled(units)}{close}")\n', "            pass\n"),
    "inword_spelled_two_units": ("        if len(units) >= 3:\n            seq.append(f\"{_inword_spelled",
                                 "        if len(units) >= 2:\n            seq.append(f\"{_inword_spelled"),
    # round 8: SP-25 (split once)
    "split_off": ('    if g["_SPLIT_ANY"]["strict"].search(_split_view(v)) is not None:\n        return True',
                  '    if False:\n        return True'),
    "split_many": ("            if gs.count(None) == len(gs) - 1 and self.chars", "            if gs.count(None) < len(gs) and self.chars"),
    "split_cv_all_chars": ('"cv": _SplitRx(rx, "[ _]")}', '"cv": _SplitRx(rx, _SPLIT_CHARS)}'),
    "split_capital_sigma": ("        r = _SPLIT_SIGMA if o == 0x3A3 else super().__missing__(o)",
                            "        r = super().__missing__(o)"),
    "split_thai_boundary": ("gap, \"\", \"\", (_THAI_WORDS_AT,))]", "gap, \"\", \"\", ())]"),
    "split_hindi_boundary": ("    never = (_HINDI_HALVES_AT,) if marked else ()", "    never = ()"),
    "split_no_guard": ('    guard = "(?!)"\n', '    guard = ""\n'),
    "split_before_mark_tail": ('            no_split = f"(?({junction})(?!)|)" if unicodedata', '            no_split = "" if unicodedata'),
    # round 8: Latin
    "value_after_no_gap": ("def _value_after(v: str, end: int, mixed: bool = False, gap: bool = True)",
                           "def _value_after(v: str, end: int, mixed: bool = False, gap: bool = False)"),
    "value_after_mixed_off": ("    return not mixed or (any(c.isalpha() for c in val) and not val.isalpha())",
                              "    return True"),
    "pair_value_off": ("                                         or _value_after(v, runs[i + 1].end(), mixed=True)):",
                       "                                         ):"),
    "camel_tail_off": ("        if cased and 2 < len(w) <= 32 and w != w.lower() and _camel_tail_label(v, w, runs[i].end()):",
                       "        if False:"),
    "camel_tail_no_boundary": ("    while k > 0 and not (w[k - 1].islower() and w[k].isupper()):",
                               "    while k > 0 and not w[k].isupper():"),
    "spelled_piece_off": ("    if singles.strip() and _SPELLED_PAIR.search(v) is not None and _spelled_piece(v, runs):\n        return True", "    if False:\n        return True"),
    "spelled_piece_no_drop": ("            cands.append(group[:-1])", "            pass"),
    "spelled_piece_mixed_gaps": ("or (gap is not None and g != gap):", ":"),
    "spelled_piece_no_prev_guard": ("        if i > 0 and len(runs[i - 1].group(0)) == 1:", "        if False:"),
    "cv_lone_dash_off": ("    sv = _singles_view(v, _CV_LONE_DASHES)", "    sv = _singles_view(v)"),
    "cv_overlap_off": ("    if matches and isinstance(rx, re.Pattern):", "    if False:"),
    "overlap_all_scripts": ("        inner = [x for m in matches if _UNSPACED_CHAR.match(wv, m.start())",
                            "        inner = [x for m in matches if True"),
    "spelled_pair_latin_only": ('_SPELL_LETTER = "[A-Za-z', '_SPELL_LETTER = "[B-Zb-z'),
    "camel_cased_gate_inverted": ("    cased = v != v.lower()", "    cased = v == v.lower()"),
    "cv_sep_hint_narrow": ('_CV_SEP_HINT = re.compile("[:=>|', '_CV_SEP_HINT = re.compile("[:|'),
    "translated_memo_wrong_table": ("    if r is None or r[0] is not table:", "    if r is None:"),
    "cv_lone_dash_always": ("        ends = t in keep and not (k + 1 < len(toks)", "        ends = t in keep and (k + 1 < 0"),
}


def run(name: str, edit) -> dict:
    old, new = edit
    tmp = Path(tempfile.mkdtemp(prefix=f"mut-{name}-"))
    try:
        dst = tmp / "seo-py"
        shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        p = dst / SG
        src = p.read_text(encoding="ascii")
        if src.count(old) != 1:
            return {"name": name, "result": "NOT_APPLIED", "count": src.count(old)}
        p.write_text(src.replace(old, new), encoding="ascii")
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-m",
                            "not conformance_heavy", *TESTS], cwd=dst, capture_output=True, text=True, timeout=3600)
        failed = [line for line in r.stdout.splitlines() if line.startswith(("FAILED", "ERROR"))]
        return {"name": name, "result": "KILLED" if r.returncode != 0 else "SURVIVED",
                "by": failed[:1], "tail": r.stdout.strip().splitlines()[-1:]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    names = sys.argv[1].split(",") if len(sys.argv) > 1 else list(MUTANTS)
    out = []
    for n in names:
        out.append(run(n, MUTANTS[n]))
        print(json.dumps(out[-1]), flush=True)
    print(json.dumps({"run": len(out), "killed": sum(o["result"] == "KILLED" for o in out),
                      "survived": [o["name"] for o in out if o["result"] == "SURVIVED"],
                      "not_applied": [o["name"] for o in out if o["result"] == "NOT_APPLIED"]}))
