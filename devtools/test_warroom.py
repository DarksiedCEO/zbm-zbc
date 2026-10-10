"""Self-test of the war room engine (devtools/warroom, ADR 0018). Standard library only; run as
``python -B -m unittest devtools/test_warroom.py -v`` (CI: the warroom job). Nothing here starts a service: the
engine's parts are tested on their own, the worker against a stand-in driver."""

from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "warroom"))
sys.dont_write_bytecode = True

import chaos  # noqa: E402
import corpus  # noqa: E402
import engine  # noqa: E402
import invariants  # noqa: E402
import personas  # noqa: E402


def _obs(before=None, after=None, alerts=(), steps=(), other=None, foreign=()):
    state = {"consents_before": before or {}, "consents_after": after or {}, "alert_codes": list(alerts),
             "foreign_identifiers": list(foreign)}
    if other is not None:
        state["other_before"], state["other_after"] = other
    return {"steps": list(steps), "state": state, "error": None}


class Chaos(unittest.TestCase):
    TEXT = "Please unsubscribe me from your emails<br>Thanks"

    def test_every_transform_is_deterministic_for_its_rng(self):
        for name, fn in sorted(chaos.TRANSFORMS.items()):
            value = "https://www.tiktok.com/@c/video/d1" if name.startswith("url_") else \
                "kid.name@gmail.com" if name in ("plus_tag", "dash_tag", "gmail_dots", "googlemail_swap",
                                                 "email_homoglyph", "email_homoglyph_wide", "email_case") \
                else self.TEXT
            a, b = fn(value, random.Random("x")), fn(value, random.Random("x"))
            self.assertIsInstance(a, str, name)
            self.assertEqual(a, b, name)

    def test_character_transforms_leave_markup_alone(self):
        for name in ("zero_width", "soft_hyphen", "fullwidth", "homoglyph", "homoglyph_wide", "leetspeak"):
            for seed in range(20):
                out = chaos.TRANSFORMS[name](self.TEXT, random.Random(seed))
                self.assertIn("<br>", out, name)

    def test_homoglyph_tables_come_from_the_repo(self):
        core, wide = corpus.homoglyph_table(core=True), corpus.homoglyph_table(core=False)
        self.assertTrue(core and wide)
        for latin, looks in core.items():
            self.assertTrue(set(looks) <= set(wide[latin]), latin)
        table = corpus.confusables()
        for latin, looks in wide.items():
            for c in looks:
                self.assertEqual(table[c], latin)

    def test_a_chain_puts_wrappers_last_and_includes_required(self):
        for seed in range(50):
            chain = chaos.pick_chain(random.Random(seed), ["homoglyph", "injection", "html_wrap", "case_flip"], 3,
                                     ["injection"])
            self.assertIn("injection", chain)
            kinds = [t in chaos.WRAPPERS for t in chain]
            self.assertEqual(kinds, sorted(kinds), chain)

    def test_long_body_keeps_the_words_at_the_end(self):
        out = chaos.long_body("STOP", random.Random(1))
        self.assertTrue(out.endswith("STOP") and len(out) > 20_000)
        mid = chaos.long_body_middle("STOP", random.Random(1))
        self.assertIn("STOP", mid)
        self.assertFalse(mid.rstrip().endswith("STOP"))


class Corpus(unittest.TestCase):
    def test_every_library_builds_its_seeds_from_the_repo(self):
        for dept in engine.departments():
            lib = engine.load_library(dept)
            for sc in lib["scenarios"]:
                if sc.get("seeds"):
                    seeds = engine.scenario_seeds(lib, sc)
                    self.assertTrue(seeds, f"{dept}/{sc['id']}")
                    self.assertEqual(len(seeds), len(set(seeds)))

    def test_a_reference_that_no_longer_resolves_is_an_error_not_an_empty_scenario(self):
        svc = engine.REPO / "services" / "service-py"
        with self.assertRaises(corpus.CorpusError):
            corpus.extract({"file": "tests/test_sweep_fixes.py", "test": "test_does_not_exist", "loop": 0}, svc)
        with self.assertRaises(corpus.CorpusError):
            corpus.extract({"file": "tests/nope.py", "module": "X"}, svc)
        with self.assertRaises(corpus.CorpusError):
            corpus.extract({"file": "tests/test_sweep_fixes.py", "test": "test_scope_corpus_all_rounds_at_once",
                            "name": "no_such_name"}, svc)

    def test_extraction_reads_the_pinned_phrases(self):
        svc = engine.REPO / "services" / "service-py"
        revoke = corpus.extract({"file": "tests/test_sweep_fixes.py", "test": "test_scope_corpus_all_rounds_at_once",
                                 "name": "revoke"}, svc)
        self.assertIn("Do not text or email me", revoke)


class Generation(unittest.TestCase):
    def test_same_seed_same_cases_other_seed_other_variants(self):
        lib = engine.load_library("service-py")
        a, b, c = engine.generate(lib, 1, 2), engine.generate(lib, 1, 2), engine.generate(lib, 2, 2)
        self.assertEqual(a, b)
        self.assertNotEqual([x["input"] for x in a], [x["input"] for x in c])

        def base(cs):
            return [x["input"] for x in cs if x["case_id"].split("#")[1].split("@")[0].endswith(".0")]
        self.assertEqual(base(a), base(c))                  # the unmutated seeds do not depend on the chaos seed

    def test_case_ids_round_trip(self):
        self.assertEqual(engine.parse_case_id("service-py/email-clear-opt-out#012.3@7"),
                         ("service-py", "email-clear-opt-out", 12, 3, 7))

    def test_a_downgraded_transform_lowers_only_its_invariants(self):
        sc = {"invariants": [{"id": "a", "severity": "MUST"}, {"id": "b", "severity": "MUST"}],
              "downgrade": {"homoglyph_wide": ["a"]}}
        out = {i["id"]: i["severity"] for i in engine._case_invariants(sc, ["homoglyph_wide"])}
        self.assertEqual(out, {"a": "SHOULD", "b": "MUST"})
        out = {i["id"]: i["severity"] for i in engine._case_invariants(sc, ["case_flip"])}
        self.assertEqual(out, {"a": "MUST", "b": "MUST"})

    def test_libraries_are_valid(self):
        for dept in engine.departments():
            lib = engine.load_library(dept)
            self.assertTrue((engine.DRIVERS / lib["driver"]).is_file(), dept)
            self.assertTrue((engine.REPO / "services" / lib["service"]).is_dir(), dept)


class Invariants(unittest.TestCase):
    def test_opt_out_is_honoured_by_a_revoke_or_an_alert(self):
        act = {"email": "active", "sms": "active"}
        self.assertTrue(invariants.opt_out_honoured_or_surfaced(_obs(act, {**act, "sms": "revoked"}))[0])
        self.assertTrue(invariants.opt_out_honoured_or_surfaced(_obs(act, act, ["OPT_OUT_POSSIBLE"]))[0])
        self.assertFalse(invariants.opt_out_honoured_or_surfaced(_obs(act, act, ["SOMETHING_ELSE"]))[0])

    def test_echo_needs_identifiers_and_finds_them_case_blind(self):
        steps = [{"action": "m", "status": 201, "body": {"note": "contact OTHER@x.test"}}]
        self.assertFalse(invariants.no_echo(_obs(steps=steps, foreign=["other@x.test"]))[0])
        self.assertTrue(invariants.no_echo(_obs(steps=steps, foreign=["nobody@x.test"]))[0])
        self.assertFalse(invariants.no_echo(_obs(steps=steps))[0])      # nothing to compare: never a silent pass

    def test_a_predicate_that_cannot_run_does_not_hold(self):
        self.assertFalse(invariants.evaluate({"check": "status_in", "args": {"step": "missing", "codes": [201]}},
                                             _obs())[0])
        self.assertFalse(invariants.evaluate({"check": "no_such_check"}, _obs())[0])

    def test_refused_or_field_equals(self):
        obs = _obs(steps=[{"action": "v", "status": 422, "body": {}}])
        self.assertTrue(invariants.refused_or_field_equals(obs, "v", [422], "v", "x", 1)[0])
        obs = _obs(steps=[{"action": "v", "status": 201, "body": {"x": 2}}])
        self.assertFalse(invariants.refused_or_field_equals(obs, "v", [422], "v", "x", 1)[0])


class Gate(unittest.TestCase):
    CASE = {"case_id": "d/s#000.1@1", "scenario": "s", "persona": "p", "chain": ["homoglyph"], "seed_value": "x",
            "input": {"text": "x"},
            "invariants": [{"id": "m", "severity": "MUST", "check": "status_in", "args": {"step": "a", "codes": [201]}},
                           {"id": "s", "severity": "SHOULD", "check": "status_in", "args": {"step": "a", "codes": [200]}}]}

    def _v(self, status=201, error=None, known=None):
        case = {**self.CASE, "known_failure": known}
        return engine.judge(case, {"steps": [{"action": "a", "status": status, "body": {}}], "state": {},
                                   "error": error, "elapsed_ms": 1.0})

    def test_outcomes(self):
        self.assertEqual(self._v(201)["outcome"], engine.PASS)          # a SHOULD miss alone is not a FAIL
        self.assertEqual(self._v(500)["outcome"], engine.FAIL)
        self.assertEqual(self._v(201, error="boom")["outcome"], engine.ERROR)   # a crash is never a pass

    def test_gate_rule(self):
        self.assertEqual(engine.summarise("d", [self._v(201)])["gate"], "PASS")
        self.assertEqual(engine.summarise("d", [self._v(201), self._v(500)])["gate"], "FAIL")
        self.assertEqual(engine.summarise("d", [self._v(201), self._v(500, known="WR-F999")])["gate"], "PASS")
        self.assertEqual(engine.summarise("d", [self._v(201), self._v(201, error="x")])["gate"], "FAIL")
        self.assertEqual(engine.summarise("d", [])["gate"], "FAIL")                 # nothing run is not a pass
        s = engine.summarise("d", [self._v(201), self._v(500, known="WR-F999")])
        self.assertEqual(s["must_pass_rate"], 1.0)
        self.assertEqual(s["known_failures"][0]["finding"], "WR-F999")

    def test_report_is_deterministic_apart_from_timing(self):
        a = engine.report({"d": [self._v(201)]}, 1, None)
        v = self._v(201)
        v["elapsed_ms"] = 99.0
        b = engine.report({"d": [v]}, 1, None)
        self.assertEqual(engine.deterministic_view(a), engine.deterministic_view(b))
        self.assertIn("War room report", engine.markdown(a))
        self.assertEqual(a["llm_personas"], "NOT_CONNECTED")


class ReplayLibrary(unittest.TestCase):
    def test_promote_only_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = engine.REPLAY
            engine.REPLAY = Path(tmp)
            try:
                old = {"replay_id": "d/R0001", "scenario": "s", "input": {"text": "old"}, "chain": [],
                       "known_failure": "WR-F999"}
                engine.save_replay("d", {"library": "d", "cases": [old]})
                fail = engine.judge({**Gate.CASE, "known_failure": None},
                                    {"steps": [{"action": "a", "status": 500, "body": {}}], "state": {}, "error": None})
                added = engine.promote({"d": [fail, fail]}, "2026-10-10")
                doc = engine.load_replay("d")
                self.assertEqual(added, {"d": ["d/R0002"]})
                self.assertEqual(doc["cases"][0], old)
                self.assertIsNone(doc["cases"][1]["known_failure"])          # blocks until a person triages it
                self.assertEqual(engine.promote({"d": [fail]}, "2026-10-11"), {})
            finally:
                engine.REPLAY = saved

    def test_every_known_failure_names_a_finding_in_findings_md(self):
        findings = (engine.HERE / "findings.md").read_text(encoding="utf-8")
        for path in sorted(engine.REPLAY.glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            lib = engine.load_library(path.stem)
            scenarios = {sc["id"] for sc in lib["scenarios"]}
            for c in doc["cases"]:
                self.assertIn(c["scenario"], scenarios, c["replay_id"])
                # a fixed case keeps its finding id in "fixed" (README "When a case fails", step 4); a null
                # known_failure without one is untriaged
                finding = c["known_failure"] or c.get("fixed")
                self.assertTrue(finding, f"{c['replay_id']}: untriaged (known_failure is null, no fixed finding)")
                self.assertFalse(c["known_failure"] and c.get("fixed"), f"{c['replay_id']}: both known and fixed")
                self.assertIn(f"## {finding}", findings, c["replay_id"])
                self.assertIn(c["replay_id"], findings)
                if c.get("fixed"):
                    section = findings.split(f"## {finding}", 1)[1].split("\n## ", 1)[0]
                    self.assertIn("Status: FIXED", section, f"{c['replay_id']}: {finding} is not marked FIXED")


class Sandbox(unittest.TestCase):
    def test_the_worker_refuses_the_network_and_reports_a_crash_as_an_error(self):
        driver = '''
import socket
from types import SimpleNamespace
def prepare_env(env): pass
def import_harness(): pass
def setup(tmp): return SimpleNamespace(steps=[])
def teardown(ctx): pass
def observe(ctx): return {}
def call_out(ctx):
    socket.create_connection(("192.0.2.1", 443))
def fine(ctx):
    return {"status": 201, "body": {"ok": True}}
ACTIONS = {"call_out": call_out, "fine": fine}
'''
        with tempfile.TemporaryDirectory() as tmp:
            svc = Path(tmp) / "svc"
            (svc / "src").mkdir(parents=True)
            (svc / "tests").mkdir()
            (Path(tmp) / "drv.py").write_text(driver)
            cases = [{"case_id": "a", "input": {}, "steps": [{"action": "call_out"}]},
                     {"case_id": "b", "input": {}, "steps": [{"action": "fine", "as": "x"}]}]
            p = subprocess.run([sys.executable, "-B", str(engine.HERE / "worker.py"), str(svc), str(Path(tmp) / "drv.py")],
                               input=json.dumps(cases), capture_output=True, text=True)
            res = {json.loads(line[15:])["case_id"]: json.loads(line[15:]) for line in p.stdout.splitlines()
                   if line.startswith("WARROOM-RESULT ")}
        self.assertIn("network access attempted", res["a"]["error"])
        self.assertIsNone(res["b"]["error"])
        self.assertEqual(res["b"]["steps"], [{"action": "x", "status": 201, "body": {"ok": True}}])


class Personas(unittest.TestCase):
    def test_the_llm_port_is_not_connected_and_never_fakes(self):
        out = personas.LLMPersonaPort().generate("service-py", 5, 1)
        self.assertEqual(out["status"], "NOT_CONNECTED")
        self.assertEqual(out["personas"], [])


if __name__ == "__main__":
    unittest.main()
