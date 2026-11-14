"""SUITE_PLAN section 3: the Priya mutants (priya-spec/1 G-6). Each mutant is one exact text replacement in the
classifier or Priya (the old text must occur exactly once); a mutant is KILLED when the test selection fails on it.

    python devtools/conformance/mutants.py [--ids a,b] [--jobs 2] [--out report.json] [--check] [--python PY]

Every mutant runs in its own copy of ``services/seo-py`` and ``docs`` (``tar --exclude=.git``; nothing is written
in the source tree), against the test selection ``TESTS`` in order with ``-x`` (the first failure kills it).
``PINNED`` mutants must be killed (G-6 list of the spec); every other behavioural mutant must be killed too, or shown
equivalent in the report of the round. The report holds ids, verdicts and counts only."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SP = "services/seo-py/src/agents/search_performance.py"
PR = "services/seo-py/src/agents/priya.py"
TESTS = ("tests/test_query_privacy_r20.py", "tests/test_query_privacy_r19.py", "tests/test_query_privacy_r18.py",
         "tests/test_query_privacy_r13.py", "tests/test_query_privacy_r14.py", "tests/test_query_privacy_r16.py",
         "tests/conformance/test_priya_mutants_pinned.py", "tests/conformance/test_priya_boundary.py",
         "tests/conformance/test_priya_owner_items.py", "tests/conformance/test_priya_ordinary.py",
         "tests/conformance/test_priya_reviewer_corpora.py", "tests/conformance/test_priya_matrix.py",
         "tests/conformance/test_priya_composed.py", "tests/conformance/test_priya_lost_protection.py")

# (id, pinned, file, old, new)
MUTANTS = [
    # ---- priya-spec/1 G-6, pinned (must be killed)
    ("m09_e164_min9", 1, SP, "or (plus and 8 <= n <= 15):", "or (plus and 9 <= n <= 15):"),
    ("m10_plus_adjacent", 1, SP, "lo = max(gs[a - 1][1] if a else 0, start - STREAM_GAP - 1)",
     "lo = max(gs[a - 1][1] if a else 0, start - 1)"),
    ("p_ctx_window_off", 1, SP, "    if any(_window_hit(w) for w in _suspicious_windows(q)):",
     "    if False and any(_window_hit(w) for w in _suspicious_windows(q)):"),
    ("p_vanity_two_letters", 1, SP,
     "    return any(any(c.isalpha() for c in m.group(1)) for m in _VANITY_ANY.finditer(t))",
     "    return any(sum(c.isalpha() for c in m.group(1)) >= 2 for m in _VANITY_ANY.finditer(t))"),
    ("p_vanity_codegap2", 1, SP, '_VG = r"[^0-9]{0,3}"', '_VG = r"[^0-9]{0,2}"'),
    ("p_addr_ctx2", 1, SP, '_UNIT + r"?(?=\\s|$)"),', '_UNIT + r"?(?:\\s+[a-z0-9]+){0,2}\\s*$"),'),
    ("p_card_min14", 1, SP, "    return 13 <= len(d) <= 19 and (card_iin(d)", "    return 14 <= len(d) <= 19 and (card_iin(d)"),
    ("p_uatp_off", 1, SP, '(card_iin(d) or (len(d) == 15 and d[0] == "1")) and _luhn(d)', "card_iin(d) and _luhn(d)"),
    ("p_diners36_off", 1, SP, "2720|3[46789]|30[0-5]", "2720|3[4789]|30[0-5]"),
    # ---- review rv-priya19's behavioural survivors (round 20 item 7: must be killed)
    ("e_no_reversed_windows", 1, SP,
     "    for s in _enc_windows([t[::-1] for t in reversed(toks)], ok[::-1], plain[::-1]):", "    for s in []:"),
    ("g_gtin_any", 1, SP, "ok = _gtin_valid(d) and (word", "ok = (word"),
    # ---- review rv-priya19's other mutants, where the code still holds their text
    ("e_tld_max3", 0, SP, '_TLD_ANY = r"(?:[a-z]\\|?){1,23}[a-z]"', '_TLD_ANY = r"(?:[a-z]\\|?){1,2}[a-z]"'),
    ("e_no_sep_spelling", 0, SP, '    if "-" not in folded and "_" not in folded:\n        return False',
     '    if True:\n        return False'),
    ("e_marker_no_script", 0, SP,
     ' \\\n        or (not c.isascii() and (c.isalpha() or unicodedata.category(c)[0] == "M"))', ""),
    ("e_no_pct_chunk", 0, SP, "            or _percent_chunked_email(undecoded) \\\n", ""),
    ("e_printable12", 0, SP, "    if k >= 8 or (k >= 5 and k >= 0.9 * len(raw))", "    if k >= 12 or (k >= 5 and k >= 0.9 * len(raw))"),
    ("s_two_words", 0, SP, "    if not found or (len(found) < 3 and not any(c.isdigit() for c in folded)):",
     "    if len(found) < 2:"),
    ("s_no_vanity_whole", 0, SP, "digit_stream_hit(joined) or _vanity_any(whole) \\", "digit_stream_hit(joined) or False \\"),
    ("v_code_sep_only", 0, SP, '_VG = r"[^0-9]{0,3}"', '_VG = r"[^0-9a-z]{0,3}"'),
    ("lp_glued_any", 0, SP, "len(groups[-1]) <= 2 and _GLUED_AFTER.match(s, m.end()):",
     "len(groups[-1]) <= 2 and s[m.end():m.end() + 1].isalpha():"),
    ("a_core_no_split", 0, SP,
     '    folded = "".join(" " if not c.isascii() and unicodedata.category(c)[0] in "PSZ" else c for c in folded)\n', ""),
    ("a_ordinary_end_only", 0, SP, '_UNIT + r"?(?=\\s|$)"),', '_UNIT + r"?\\s*$"),'),
    ("g_local7_letter", 0, SP, '_LOCAL7 = re.compile(r"(?<![\\da-z.,])', '_LOCAL7 = re.compile(r"(?<![\\d.,])'),
    ("g_no_tire", 0, SP, '    t = _TIRE.sub(" # ", text)', "    t = text"),
    ("g_dotted12", 0, SP, "sum(c.isdigit() for c in m.group()) <= 8 else", "sum(c.isdigit() for c in m.group()) <= 12 else"),
    ("w_no_raw", 0, SP, '    raw = folded if "%" not in w else _LOOKALIKE.fold_cased(strip_marks(w))', "    raw = folded"),
    ("p_key_never", 0, PR, "        if cls != PII_CLASS and norm != raw:", "        if False:"),
    # ---- round 20's changed code (C-1 lists, C-2 glue, C-4 in-word reading, the per-character script view)
    ("n01_no_point", 0, SP, '"dot", "punto", "point", "ponto"', '"dot", "punto", "ponto"'),
    ("n02_no_kukac", 0, SP, '"malpa", "kukac", "snabel-a"', '"malpa", "snabel-a"'),
    ("n03_no_titik", 0, SP, '"nokta", "titik")', '"nokta")'),
    ("n04_no_hyphen_forms", 0, SP, '    forms = set(words) | {w.replace("-", "") for w in words}', "    forms = set(words)"),
    ("n05_tld_no_trailing_dot", 0, SP, '+ r"(?=\\.?(?:\\||$))")', '+ r"(?=\\||$)")'),
    ("n06_despell_tld_at", 0, SP, "        if x.start() == after_dot + 1:\n            continue\n", ""),
    ("n07_addr_glue_off", 0, SP, "    folded = _ADDR_GLUE.sub(\" \", folded)\n", ""),
    ("n08_addr_glue_no_dot", 0, SP, '|\\.(?=[a-z])")', '")'),
    ("n09_script_view_off", 0, SP, '    xs = script_view(q if "%" not in q else unquote(unquote(unquote(q)))) if q else ""',
     '    xs = ""'),
    ("n10_xs_no_stream", 0, SP, "    if xs and (digit_stream_hit(xs) or _vanity_any(xs) or", "    if xs and (False or _vanity_any(xs) or"),
    ("n11_xs_no_vanity", 0, SP, "digit_stream_hit(xs) or _vanity_any(xs) or _spelled_hit(xs)",
     "digit_stream_hit(xs) or _spelled_hit(xs)"),
    ("n12_xs_no_spelled", 0, SP, "_vanity_any(xs) or _spelled_hit(xs) or email_views_hit(xs)",
     "_vanity_any(xs) or email_views_hit(xs)"),
    ("n13_xs_no_email", 0, SP, "_spelled_hit(xs) or email_views_hit(xs)\n", "_spelled_hit(xs)\n"),
    ("n14_xs_no_address", 0, SP, '               or looks_like_address(_address_core(xs.replace("#", " ")))):',
     "               ):"),
    ("n15_script_view_letters_kept", 0, SP, '            elif c.isalpha():\n                out.append("#")',
     '            elif c.isalpha():\n                out.append(c)'),
    ("n16_script_view_no_nfkc", 0, SP, "            if len(n) == 1 and n.isascii():\n                out.append(n.lower())\n"
     "                continue\n", ""),
    ("n17_script_view_spaces_end_words", 0, SP, '        elif cat[0] == "Z" or c.isspace():\n            out.append(c)',
     '        elif cat[0] == "Z" or c.isspace():\n            out.append(" ")'),
    ("n18_word_end_any_space", 0, SP, '            if not any(c in _WORD_END for c in before):',
     '            if not any(c in _WORD_END or c.isspace() for c in before):'),
    ("n19_trail_blind", 0, SP, "                trail = STREAM_GAP - len(after) - off", "                trail = 0"),
    ("n20_lead_blind", 0, SP, "                lead = STREAM_GAP - len(before) - prev", "                lead = 0"),
    ("n21_c4_off_start", 0, SP, "        if unit_at[k] is not None and (tok[k].isdigit() or k <= lead):",
     "        if unit_at[k] is not None:"),
    ("n22_c4_off_end", 0, SP, "        if rest[i] is None and i >= tail and i and (tok[i - 1].isdigit() or n - i <= trail):",
     "        if rest[i] is None and i >= tail and i:"),
    ("n23_digit_token_no_tail", 0, SP, "    return tok, (len(tok) - last - 1 if last >= 0 else None)", "    return tok, None"),
    ("n24_enc_split_spaces_only", 0, SP, "    toks = _ENC_TOKEN.findall(cased)\n",
     '    toks = [t.strip(".,:;") for t in cased.split()]\n'),
    ("n25_pct_no_reversed", 0, SP, "    for v in (folded[::-1], \" \".join(w[::-1] for w in folded.split())):",
     "    for v in ():"),
    ("n26_seg_gap2", 0, SP, "        for g in range(STREAM_GAP + 1):\n            k = i + g\n            if k >= n or (g and tok[k - 1].isdigit()):",
     "        for g in range(STREAM_GAP):\n            k = i + g\n            if k >= n or (g and tok[k - 1].isdigit()):"),
    ("n27_unit_offset_zero", 0, SP, "    for k in range(min(STREAM_GAP, len(tok) - 1) + 1):", "    for k in range(1):"),
    ("n29_short_email_prefix_off", 0, SP, " or (k >= 6 and 64 in raw[1:k - 3]):", ":"),
    ("n30_oracle_free_seg_dp_leftmost", 0, SP, "            for w in _IN_WORD_UNITS:\n                if tok.startswith(w, i) and rest[i + len(w)] is not None:",
     "            for w in _IN_WORD_UNITS:\n                if tok.startswith(w, i):"),
    ("n28_ideographic_space_no_end", 0, SP, '_WORD_END = frozenset(" \\t\\n\\r\\f\\v\\u3000")',
     '_WORD_END = frozenset(" \\t\\n\\r\\f\\v")'),
]


def _copy(dest: Path) -> None:
    """services/seo-py (src, tests) and docs of the working tree into ``dest`` (no .git, no caches, no data)."""
    def keep(ti):
        parts = Path(ti.name).parts
        return None if "__pycache__" in parts or ".git" in parts or ".pytest_cache" in parts else ti
    tmp = dest / "copy.tar"
    with tarfile.open(tmp, "w") as tf:
        for sub in ("services/seo-py/src", "services/seo-py/tests", "docs/adr"):
            tf.add(REPO / sub, arcname=sub, filter=keep)
        for f in ("services/seo-py/pyproject.toml", "services/seo-py/conftest.py", "services/seo-py/pytest.ini"):
            if (REPO / f).exists():
                tf.add(REPO / f, arcname=f)
    with tarfile.open(tmp) as tf:
        tf.extractall(dest, filter="data")
    tmp.unlink()


def check() -> list:
    bad = []
    for mid, _pin, f, old, _new in MUTANTS:
        n = (REPO / f).read_text(encoding="utf-8").count(old)
        if n != 1:
            bad.append((mid, n))
    return bad


def run_one(m, python: str, timeout: int) -> dict:
    mid, pinned, f, old, new = m
    with tempfile.TemporaryDirectory(prefix="mut_") as tmp:
        d = Path(tmp)
        _copy(d)
        p = d / f
        s = p.read_text(encoding="utf-8")
        assert s.count(old) == 1, mid
        p.write_text(s.replace(old, new), encoding="utf-8")
        t0 = time.time()
        try:
            r = subprocess.run([python, "-m", "pytest", "-q", "-x", "-rf", "-p", "no:cacheprovider", *TESTS],
                               cwd=d / "services" / "seo-py", capture_output=True, text=True, timeout=timeout,
                               env={**os.environ, "CONFORMANCE_HEAVY": "0"})
            failed = r.returncode != 0
            last = (r.stdout.strip().splitlines() or [""])[-1]
        except subprocess.TimeoutExpired:
            failed, last = True, "timeout"
        first = next((ln.split("::")[0].split("/")[-1] + "::" + ln.split("::")[1].split(" ")[0]
                      for ln in (r.stdout.splitlines() if not last == "timeout" else []) if ln.startswith("FAILED")), "")
        return {"id": mid, "pinned": bool(pinned), "killed": failed, "by": first, "summary": last[:120],
                "seconds": round(time.time() - t0)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--out", default="")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--timeout", type=int, default=3600)
    a = ap.parse_args()
    bad = check()
    if a.check or bad:
        print(json.dumps({"mutants": len(MUTANTS), "not_applicable": bad}))
        return 1 if bad else 0
    ids = set(a.ids.split(",")) - {""}
    todo = [m for m in MUTANTS if not ids or m[0] in ids]
    results = []
    with ThreadPoolExecutor(a.jobs) as pool:
        for res in pool.map(lambda m: run_one(m, a.python, a.timeout), todo):
            results.append(res)
            print(json.dumps(res), flush=True)
    survived = [r["id"] for r in results if not r["killed"]]
    report = {"mutants": len(results), "killed": len(results) - len(survived), "survived": survived,
              "pinned_survived": [r["id"] for r in results if r["pinned"] and not r["killed"]], "results": results}
    if a.out:
        Path(a.out).write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}))
    return 0 if not report["pinned_survived"] else 1


if __name__ == "__main__":
    sys.exit(main())
