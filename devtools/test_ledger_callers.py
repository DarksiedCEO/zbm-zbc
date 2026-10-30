"""devtools/ledger_callers.py: the LEDGER_CALLERS generator refuses what would silently revoke a department or stop the
ledger, pins each caller's rights, adds or rotates one caller without the other tokens, and never prints a token or a
hash in its summary (AEGIS detection-branch M-2, M-4). Standard library only; CI runs it in the `hygiene-static` job:

    python3 -B -m unittest devtools/test_ledger_callers.py -v

Every token here is generated at run time (secrets.token_hex); none is real.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger_callers as lc  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "services" / "ledger-rust" / "src" / "bin" / "server.rs"
FOUR = ("orchestrator-go", "finance-py", "legal-py", "seo-py")


def tok() -> str:
    return secrets.token_hex(32)


def sha(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


class Prompt:
    """Answers getpass prompts from a list; each token is given twice (entry + confirmation) unless told otherwise."""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, text: str) -> str:
        self.asked.append(text)
        return self.answers.pop(0)


def twice(*tokens: str) -> list[str]:
    return [x for t in tokens for x in (t, t)]


def value_for(callers_tokens: dict[str, str]) -> str:
    return json.dumps({sha(t): lc.entry(c) for c, t in callers_tokens.items()})


def run(argv, prompt=None):
    out, err = io.StringIO(), io.StringIO()
    code = lc.main(argv, prompt=prompt or Prompt([]), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class TheGrants(unittest.TestCase):
    """M-4: the script is the only artifact that sets the ledger privilege boundary; its table is pinned here."""

    def test_each_caller_writes_exactly_its_one_department(self):
        self.assertEqual({c: d for c, (d, _w) in lc.CALLERS.items()},
                         {"orchestrator-go": "revenue_recovery", "finance-py": "finance", "legal-py": "legal",
                          "seo-py": "seo", "detection-py": "rr_store"})

    def test_no_caller_reads_more_than_it_writes(self):
        for caller in lc.CALLERS:
            with self.subTest(caller=caller):
                self.assertEqual(lc.entry(caller),
                                 {"caller": caller, "departments": [lc.CALLERS[caller][0]], "scope": "write"})

    def test_detection_neither_writes_nor_reads_revenue_recovery(self):
        e = lc.entry("detection-py")
        self.assertEqual(e["departments"], ["rr_store"])
        self.assertNotIn("read_all", e)
        self.assertNotIn("read_departments", e)

    def test_the_generated_value_holds_exactly_the_table(self):
        tokens = {c: tok() for c in lc.CALLERS}
        code, out, _err = run(["new"], Prompt(twice(*tokens.values())))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {sha(t): lc.entry(c) for c, t in tokens.items()})
        for e in json.loads(out).values():
            self.assertEqual(set(e), {"caller", "departments", "scope"})
            self.assertEqual(e["scope"], "write")

    def test_the_ledger_limits_are_the_servers(self):
        rust = SERVER.read_text()
        self.assertIn(f"const MIN_TOKEN_BYTES: usize = {lc.MIN_TOKEN_BYTES};", rust)
        self.assertIn("const MAX_CALLERS_FILE_BYTES: u64 = 1024 * 1024;", rust)
        self.assertEqual(lc.MAX_CALLERS_FILE_BYTES, 1024 * 1024)
        struct = rust.split("struct Caller {", 1)[1].split("\n}", 1)[0]
        self.assertEqual(set(re.findall(r"^\s+(\w+):", struct, re.M)), lc.LEDGER_FIELDS)
        self.assertIn("#[serde(deny_unknown_fields)]\nstruct Caller {", rust)
        self.assertIn("hex_sha256(token.as_bytes())", rust, "the ledger keys a caller by sha256 of the token bytes")


class NewValue(unittest.TestCase):
    def test_refuses_empty_whitespace_short_and_non_printable_tokens(self):
        good = tok()
        for bad, why in (("", "empty"), ("   ", "empty"), ("\t\n", "empty"), (good + " ", "whitespace"),
                         (" " + good, "whitespace"), (good[:20] + "\n" + good[20:], "whitespace"),
                         (good[:31], "under 32"), ("a" * 31, "under 32"), (good[:40] + "é" * 4, "non-printable")):
            with self.subTest(why=why, n=len(bad)):
                code, out, err = run(["new"], Prompt([bad, bad]))
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn("REFUSED: orchestrator-go:", err)
                self.assertIn(why, err)
                if bad.strip():
                    self.assertNotIn(bad.strip(), err)

    def test_accepts_exactly_32_bytes(self):
        tokens = [secrets.token_hex(16) for _ in lc.CALLERS]
        self.assertEqual(len(tokens[0]), 32)
        code, _out, _err = run(["new"], Prompt(twice(*tokens)))
        self.assertEqual(code, 0)

    def test_refuses_the_same_token_for_two_callers(self):
        a, b, c = tok(), tok(), tok()
        code, out, err = run(["new"], Prompt(twice(a, b, a, c, tok())))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("legal-py: this token is already orchestrator-go's", err)
        self.assertNotIn(a, err)
        self.assertNotIn(sha(a), err)

    def test_refuses_a_confirmation_that_differs(self):
        a = tok()
        code, out, err = run(["new"], Prompt([a, a[:-1] + ("0" if a[-1] != "0" else "1")]))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("orchestrator-go: the two entries differ", err)

    def test_asks_for_every_caller_by_where_its_token_lives(self):
        p = Prompt(twice(*(tok() for _ in lc.CALLERS)))
        run(["new"], p)
        self.assertEqual(len(p.asked), 2 * len(lc.CALLERS))
        self.assertIn("DETECTION_LEDGER_TOKEN on detection", p.asked[-2])
        self.assertIn("LEDGER_SERVICE_TOKEN on seo", p.asked[-4])


class AddAndRotate(unittest.TestCase):
    """The add-one-caller path needs the current value (hashes) and the ONE new token, never the Sealed four."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tokens = {c: tok() for c in FOUR}
        self.current = self.dir / "ledger-callers.json"
        self.current.write_text(value_for(self.tokens))

    def tearDown(self):
        self.tmp.cleanup()

    def test_adds_detection_with_only_its_own_token(self):
        det = tok()
        p = Prompt([det, det])
        code, out, err = run(["add", "detection-py", "--current", str(self.current)], p)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(p.asked), 2)
        self.assertTrue(all(q.startswith("detection-py:") for q in p.asked))
        new = json.loads(out)
        old = json.loads(self.current.read_text())
        self.assertEqual(list(new)[:4], list(old), "the existing entries are kept, in order")
        for h, e in old.items():
            self.assertEqual(new[h], e)
        self.assertEqual(new[sha(det)], {"caller": "detection-py", "departments": ["rr_store"], "scope": "write"})
        self.assertEqual(len(new), 5)
        self.assertIn("every caller in devtools/ledger_callers.py is present", err)

    def test_refuses_an_unknown_caller(self):
        for name in ("detection", "Detection-py", "detection-py ", "dashboard", ""):
            with self.subTest(name=name):
                code, out, err = run(["add", name, "--current", str(self.current)], Prompt([tok(), tok()]))
                self.assertEqual((code, out), (1, ""))
                self.assertIn("unknown caller", err)

    def test_refuses_adding_a_caller_already_there(self):
        t = tok()
        code, out, err = run(["add", "seo-py", "--current", str(self.current)], Prompt([t, t]))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("use `rotate seo-py`", err)

    def test_refuses_another_callers_token_and_a_pasted_hash(self):
        code, _out, err = run(["add", "detection-py", "--current", str(self.current)],
                              Prompt(twice(self.tokens["finance-py"])))
        self.assertEqual(code, 1)
        self.assertIn("already finance-py's", err)
        h = sha(self.tokens["legal-py"])
        code, _out, err = run(["add", "detection-py", "--current", str(self.current)], Prompt([h, h]))
        self.assertEqual(code, 1)
        self.assertIn("a hash from the current value, not a token", err)
        self.assertNotIn(h, err)

    def test_rotates_one_caller_and_keeps_the_rest(self):
        new_seo = tok()
        code, out, err = run(["rotate", "seo-py", "--current", str(self.current)], Prompt([new_seo, new_seo]))
        self.assertEqual(code, 0, err)
        new = json.loads(out)
        self.assertNotIn(sha(self.tokens["seo-py"]), new)
        self.assertEqual(new[sha(new_seo)], lc.entry("seo-py"))
        for c in ("orchestrator-go", "finance-py", "legal-py"):
            self.assertEqual(new[sha(self.tokens[c])], lc.entry(c))
        self.assertEqual(len(new), 4)

    def test_rotate_refuses_the_same_token_another_callers_token_and_a_missing_caller(self):
        same = self.tokens["seo-py"]
        code, _o, err = run(["rotate", "seo-py", "--current", str(self.current)], Prompt([same, same]))
        self.assertEqual(code, 1)
        self.assertIn("the token it already has", err)
        other = self.tokens["legal-py"]
        code, _o, err = run(["rotate", "seo-py", "--current", str(self.current)], Prompt([other, other]))
        self.assertEqual(code, 1)
        self.assertIn("already legal-py's", err)
        t = tok()
        code, _o, err = run(["rotate", "detection-py", "--current", str(self.current)], Prompt([t, t]))
        self.assertEqual(code, 1)
        self.assertIn("use `add detection-py`", err)

    def test_out_writes_a_new_owner_only_file_and_never_overwrites(self):
        det = tok()
        out_path = self.dir / "ledger-callers.next.json"
        code, out, err = run(["add", "detection-py", "--current", str(self.current), "--out", str(out_path)],
                             Prompt([det, det]))
        self.assertEqual((code, out), (0, ""), err)
        self.assertEqual(stat.S_IMODE(os.stat(out_path).st_mode), 0o600)
        self.assertIn(sha(det), json.loads(out_path.read_text()))
        before = out_path.read_text()
        code, _o, err = run(["add", "detection-py", "--current", str(self.current), "--out", str(out_path)],
                            Prompt([det, det]))
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual(out_path.read_text(), before)
        code, _o, err = run(["add", "detection-py", "--current", str(self.current), "--out", str(self.current)],
                            Prompt([det, det]))
        self.assertEqual(code, 1)
        self.assertIn("--out is the --current file", err)

    def test_a_missing_current_file_is_refused(self):
        code, _o, err = run(["check", "--current", str(self.dir / "nope.json")])
        self.assertEqual(code, 1)
        self.assertIn("REFUSED: --current", err)


class TheCurrentValue(unittest.TestCase):
    """What the ledger would refuse at start, or any grant that differs from the table, is refused before a paste."""

    def good(self) -> dict:
        return {sha(tok()): lc.entry(c) for c in lc.CALLERS}

    def refused(self, text: str, fragment: str):
        with self.assertRaises(lc.Refused) as cm:
            lc.parse_current(text)
        self.assertIn(fragment, str(cm.exception))
        self.assertIsNone(re.search(r"[0-9a-f]{64}", str(cm.exception)), "a refusal names no hash")

    def test_accepts_the_generated_shape(self):
        v = self.good()
        self.assertEqual([e for _h, e in lc.parse_current(json.dumps(v))], list(v.values()))

    def test_refuses_what_the_ledger_refuses(self):
        h = sha(tok())
        self.refused("", "not JSON")
        self.refused("{", "not JSON")
        self.refused("{}", "names no callers")
        self.refused("[]", "names no callers")
        self.refused('["x"]', "not a JSON object")
        self.refused(json.dumps({h.upper(): lc.entry("seo-py")}), "lowercase hex SHA-256")
        self.refused(json.dumps({h[:63]: lc.entry("seo-py")}), "lowercase hex SHA-256")
        self.refused('{"%s": %s, "%s": %s}' % (h, json.dumps(lc.entry("seo-py")), h, json.dumps(lc.entry("legal-py"))),
                     "same key is listed twice")
        self.refused(json.dumps({h: {**lc.entry("seo-py"), "note": "x"}}), "unknown field")
        self.refused('{"%s": {"caller": "seo-py", "caller": "seo-py", "departments": ["seo"], "scope": "write"}}' % h,
                     "same key twice")
        self.refused(json.dumps({h: {**lc.entry("seo-py"), "departments": ["SEO"]}}), "not [a-z0-9_]")
        self.refused(json.dumps({h: {**lc.entry("seo-py"), "departments": "seo"}}), "not a list")
        self.refused(json.dumps({h: "seo-py"}), "not an object")
        self.refused(json.dumps({h: {"departments": ["seo"], "scope": "write"}}), "no caller name")

    def test_refuses_any_grant_but_the_tables(self):
        h = sha(tok())
        cases = [({**lc.entry("detection-py"), "departments": ["revenue_recovery"]}, "grant is not"),
                 ({**lc.entry("detection-py"), "departments": ["rr_store", "revenue_recovery"]}, "grant is not"),
                 ({**lc.entry("seo-py"), "read_all": True}, "grant is not"),
                 ({**lc.entry("seo-py"), "read_all": False}, "grant is not"),
                 ({**lc.entry("seo-py"), "read_departments": ["finance"]}, "grant is not"),
                 ({**lc.entry("seo-py"), "scope": "read"}, "grant is not"),
                 ({**lc.entry("seo-py"), "departments": ["onboarding"]}, "unknown department"),
                 ({**lc.entry("seo-py"), "caller": "seo"}, "unknown caller"),
                 ({**lc.entry("seo-py"), "caller": "dashboard"}, "unknown caller")]
        for e, fragment in cases:
            with self.subTest(entry=e):
                self.refused(json.dumps({h: e}), fragment)

    def test_refuses_the_same_caller_twice(self):
        v = {sha(tok()): lc.entry("seo-py"), sha(tok()): lc.entry("seo-py")}
        self.refused(json.dumps(v), "seo-py is listed twice")

    def test_check_prints_a_summary_with_no_secret(self):
        tokens = {c: tok() for c in FOUR}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "v.json"
            p.write_text(value_for(tokens))
            code, out, err = run(["check", "--current", str(p)])
        self.assertEqual((code, out), (0, ""))
        for c in FOUR:
            self.assertRegex(err, rf"{c}\s+write {lc.CALLERS[c][0]}; reads {lc.CALLERS[c][0]} only")
        self.assertIn("not in this value: detection-py", err)
        for t in tokens.values():
            self.assertNotIn(t, err)
            self.assertNotIn(sha(t), err)
        self.assertIsNone(re.search(r"[0-9a-f]{32}", err), "no hash or token fragment in the summary")


class Secrecy(unittest.TestCase):
    def test_no_command_prints_a_token_and_only_the_value_holds_hashes(self):
        tokens = {c: tok() for c in lc.CALLERS}
        code, out, err = run(["new"], Prompt(twice(*tokens.values())))
        self.assertEqual(code, 0)
        for t in tokens.values():
            self.assertNotIn(t, out + err)
        self.assertIsNone(re.search(r"[0-9a-f]{32}", err), "the summary holds no hash")
        for c in lc.CALLERS:
            self.assertIn(c, err)
        self.assertEqual(set(json.loads(out)), {sha(t) for t in tokens.values()})

    def test_refusals_print_no_token(self):
        a = tok()
        for answers in ([a, a, a, a], [a + " ", a + " "], [a, a + "x"]):
            code, out, err = run(["new"], Prompt(answers + twice(tok(), tok(), tok(), tok())))
            self.assertEqual((code, out), (1, ""))
            self.assertNotIn(a, err)
            self.assertNotIn(sha(a), err)


if __name__ == "__main__":
    unittest.main()
