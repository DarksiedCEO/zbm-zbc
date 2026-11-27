"""Behaviour tests for the MVP fixture pools (RR pool B, front-desk call bank).

Run from fixtures/mvp_pools:  python3 -B -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import json
import re
import sys
import unicodedata
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poolgen import build, call_bank, common, oracle, pool_b
from poolgen.verticals import VERTICAL_IDS, VERTICALS

SEED = "dev-a"
WORLD, BKEY = pool_b.generate(SEED)
CALLS, CKEY = call_bank.generate(SEED)


class Stream(unittest.TestCase):
    def test_known_vector(self):
        # Pins the random stream itself: a change here silently changes every frozen test set.
        s = common.Stream("vector")
        self.assertEqual([s.below(1000) for _ in range(5)], VECTOR)

    def test_below_is_in_range_and_unbiased_enough(self):
        s = common.Stream("range")
        counts = Counter(s.below(7) for _ in range(7000))
        self.assertEqual(set(counts), set(range(7)))
        self.assertTrue(all(800 < c < 1200 for c in counts.values()), counts)

    def test_bad_arguments_refused(self):
        s = common.Stream("x")
        for bad in (lambda: s.below(0), lambda: s.between(3, 2), lambda: s.pick([])):
            with self.assertRaises(ValueError):
                bad()


# Literal on purpose (computed once, Nov 25 2026): never derive it from the code under test.
VECTOR = [759, 376, 308, 213, 486]


class Determinism(unittest.TestCase):
    def test_same_seed_same_bytes(self):
        self.assertEqual(build.render(SEED), build.render(SEED))

    def test_different_seed_different_pool(self):
        self.assertNotEqual(build.digests("dev-a"), build.digests("dev-b"))

    def test_pinned_dev_seeds_match(self):
        pinned = build.load_seeds()
        self.assertEqual(pinned["generator"], common.GENERATOR_VERSION)
        for seed, files in pinned["dev_seeds"].items():
            with self.subTest(seed=seed):
                self.assertEqual(build.digests(seed), files)

    def test_verify_fails_on_an_empty_or_partial_pin_list(self):
        """Counterfeit round 1: verify passed with no seeds pinned, or with only one file pinned per seed."""
        import contextlib
        import io
        real = build.load_seeds()
        seed, files = next(iter(real["dev_seeds"].items()))
        cases = {"empty": {**real, "dev_seeds": {}},
                 "one file": {**real, "dev_seeds": {seed: dict([next(iter(files.items()))])}},
                 "extra file": {**real, "dev_seeds": {seed: {**files, "extra.jsonl": "0" * 64}}}}
        original = build.load_seeds
        try:
            for label, pins in cases.items():
                build.load_seeds = lambda pins=pins: pins
                with self.subTest(case=label), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(build.cmd_verify(None), 1)
            build.load_seeds = original
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(build.cmd_verify(None), 0)
        finally:
            build.load_seeds = original

    def test_no_held_out_seed_is_committed(self):
        names = set(build.load_seeds()["dev_seeds"])
        self.assertTrue(names and all(n.startswith("dev-") for n in names), names)


class FixtureSafety(unittest.TestCase):
    """Every rendered value is fictional: phones in 555-0100..0199, hosts reserved, no email or SSN shapes.

    The checks read DECODED values (every file is JSON lines, parsed), never the raw text, so a JSON escape such as
    "\n" in front of a number cannot hide it (AEGIS test-verification on b76b94f2, High 1). A number is any run of
    digit groups joined by up to three non-alphanumeric characters (any space, dash, dot, slash, underscore,
    bracket or plus), in any Unicode digit form; every 7-, 10- or 11-digit span of whole groups is checked, so a
    number next to other numbers or an extension is still seen (a 7-digit local span must end in a 4-digit group,
    so "1-602-555" inside a fictional "+1-602-555-0117" is not read as a local number). A host is any domain-shaped token anywhere in a
    value, so a second address hidden after the first, "www." or bare forms, "//host" and other schemes are all
    read (High 2). Generated id fields are the one exemption: they must match the id shape exactly, and only the
    phone scan skips them (their hex can look like digits).
    Residual, by construction: numbers spelled in words, and hosts written with spaces or "[.]", are not detected."""

    ID_FIELDS = frozenset({"business_id", "call_id", "case_id", "citation_id", "client_id", "hidden_effect_lead_ids",
                           "lead_id", "lead_ids", "missed_call_id", "page_id", "pair_id", "request_id", "review_id",
                           "send_id", "transcript_id"})
    ID_SHAPE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z]+)*-[0-9a-f]{8,16}(?:-L\d{3})?")
    DIRECTORY_PATH = re.compile(r"(/biz/)[0-9a-f]{8}$")      # the directory's listing id in a citation URL
    CHAIN = re.compile(r"\d+(?:[\W_]{1,3}\d+)*")
    GROUP = re.compile(r"\d+")
    DOMAIN = re.compile(r"(?<![\w-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}(?![\w-])",
                        re.IGNORECASE)
    IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
    EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
    SSN = re.compile(r"(?<!\d)\d{3}[\W_]{1,3}\d{2}[\W_]{1,3}\d{4}(?!\d)")

    @staticmethod
    def _strings(obj, key=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield from FixtureSafety._strings(v, k)
        elif isinstance(obj, list):
            for v in obj:
                yield from FixtureSafety._strings(v, key)
        elif isinstance(obj, str):
            yield key, obj

    def _values(self, files):
        for name, text in files.items():
            self.assertTrue(name.endswith(".jsonl"), f"{name}: only JSON lines are scanned, so nothing else may ship")
            for n, line in enumerate(text.splitlines(), 1):
                if line.strip():
                    for key, value in self._strings(json.loads(line)):
                        yield f"{name}:{n}:{key}", key, value

    @classmethod
    def numbers(cls, text: str):
        """Every 7-, 10- or 11-digit span of whole digit groups, as "+1" plus 10 digits or the bare 7 digits."""
        for chain in cls.CHAIN.finditer(text):
            groups = ["".join(str(unicodedata.digit(c)) for c in g) for g in cls.GROUP.findall(chain.group())]
            for i in range(len(groups)):
                span = ""
                for g in groups[i:]:
                    span += g
                    if len(span) > 11:
                        break
                    if len(span) == 11 and span[0] == "1":
                        yield "+" + span
                    elif len(span) == 10:
                        yield "+1" + span
                    elif len(span) == 7 and len(g) in (4, 7):    # a local number ends in its 4-digit line
                        yield span

    @staticmethod
    def fictional_number(number: str) -> bool:
        if len(number) == 7:
            return number[:3] == "555" and 100 <= int(number[3:]) <= 199
        return common.is_fixture_phone(number)

    @classmethod
    def phone_text(cls, key: str, value: str) -> str:
        if key in cls.ID_FIELDS:
            return ""
        return cls.DIRECTORY_PATH.sub(r"\1", value) if key == "url" else value

    @classmethod
    def hosts(cls, text: str):
        yield from (m.group().lower() for m in cls.DOMAIN.finditer(text))

    @classmethod
    def offending(cls, key: str, value: str) -> list:
        """Everything in one decoded value that is not fictional, empty when the value is safe."""
        bad = [n for n in cls.numbers(cls.phone_text(key, value)) if not cls.fictional_number(n)]
        bad += [h for h in cls.hosts(value) if not common.is_fixture_domain(h)]
        bad += cls.IPV4.findall(value) + cls.EMAIL.findall(value) + cls.SSN.findall(cls.phone_text(key, value))
        return bad

    def test_every_rendered_value_is_fictional(self):
        for where, key, value in self._values(build.render(SEED)):
            bad = self.offending(key, value)
            if bad:
                self.fail(f"{where}: {bad} in {value[:120]!r}")

    def test_id_fields_hold_only_ids(self):
        seen = set()
        for where, key, value in self._values(build.render(SEED)):
            if key in self.ID_FIELDS:
                seen.add(key)
                self.assertRegex(value, rf"^{self.ID_SHAPE.pattern}$", where)
        self.assertEqual(seen, self.ID_FIELDS)        # an exemption nothing uses is removed, not left open

    def test_the_scan_catches_what_review_slipped_past_it(self):
        """The forms AEGIS got past the b76b94f2 checks, each put into a real rendered line and read back the same
        way the scan reads every file."""
        name, text = next(iter(build.render(SEED).items()))
        row = json.loads(text.splitlines()[0])
        unsafe = ["call\n2138675309", "call\n213 867 5309", "213  867  5309", "213/867/5309", "867-5309",
                  "2138675309x12", "213 867 5309", "213–867–5309", "213_867_5309", "213\t867\t5309",
                  "+1 - 213 - 867 - 5309", "２１３８６７５３０９",
                  "+12138675309", "(213) 867-5309", "213.867.5309", "1-213-867-5309", "213-555-0200",
                  "213-555-0099", "555-0200", "on 2026-01-05 call 213 867 5309 ext 4",
                  "see https://good.test\nhttps://realbiz.com", "www.realbiz.com", "realbiz.com/menu",
                  "//realbiz.com", "ftp://realbiz.com", "https://x.test@realbiz.com/", "https://realbiz.com#.test/",
                  "https://realbiz.com?x=.test/", "https://realbiz.com\\.test/", "REALBIZ.COM",
                  "http://8.8.8.8/", "a@realbiz.com", "mailto:a@shop.test", "123-45-6789", "123 45 6789"]
        for form in unsafe:
            with self.subTest(form=form):
                line = common.canonical_json({**row, "note": form})
                found = [b for _, key, value in self._values({name: line}) for b in self.offending(key, value)]
                self.assertTrue(found, form)
        safe = ["213-555-0142", "+12135550100", "+1 (213) 555-0199", "555-0156", "https://shop-1.test/book",
                "www.example.com", "$1,500 per month", "2026-12-25", "08:00-17:00", "Suite 210"]
        for form in safe:
            with self.subTest(form=form):
                line = common.canonical_json({**row, "note": form})
                self.assertEqual([b for _, key, value in self._values({name: line})
                                  for b in self.offending(key, value)], [], form)

    def test_the_fictional_range_is_exactly_0100_to_0199(self):
        for number, ok in (("+12135550100", True), ("+12135550199", True), ("+12135550099", False),
                           ("+12135550200", False), ("+12135551234", False), ("+12135560150", False),
                           ("5550100", True), ("5550199", True), ("5550099", False), ("5550200", False)):
            with self.subTest(number=number):
                self.assertIs(self.fictional_number(number), ok)

    def test_every_website_domain_is_reserved(self):
        for biz in WORLD:
            self.assertTrue(common.is_fixture_domain(biz["website"]["domain"]))

    def test_every_record_is_labelled_fixture(self):
        for row in WORLD + CALLS:
            self.assertEqual(row["fixture"], common.FIXTURE_LABEL)
        for biz in WORLD:
            self.assertIn("(Fixture)", biz["name"])


class AnswerKeySeparation(unittest.TestCase):
    def test_world_carries_no_answer_words(self):
        text = "".join(common.canonical_json(b) for b in WORLD).lower()
        for word in ("planted", "expected_findings", "must_refuse", "\"control\"", "not_a_fault", "hidden_effect"):
            self.assertNotIn(word, text, word)

    def test_calls_carry_no_answer_fields(self):
        for call in CALLS:
            self.assertFalse({"scenario", "expected_route", "emergency", "must_not", "must_capture"} & set(call))


class PoolB(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(len(WORLD), pool_b.LEAKY_COUNT + pool_b.CONTROL_COUNT)
        self.assertGreaterEqual(pool_b.LEAKY_COUNT, 120)
        self.assertEqual(sum(k["control"] for k in BKEY), 20)
        per_v = Counter(b["vertical"] for b in WORLD)
        self.assertEqual(set(per_v), set(VERTICAL_IDS))
        self.assertTrue(all(n >= 15 for n in per_v.values()), per_v)
        self.assertEqual(Counter(b["website"]["forms"][0]["builder"] for b in WORLD).keys(),
                         set(pool_b.FORM_BUILDERS))

    def test_every_class_is_tested_and_every_detector_has_a_refusal_case(self):
        found = Counter(f["class"] for k in BKEY for f in k["expected_findings"])
        for cls in pool_b.ALL_CLASSES:
            with self.subTest(cls=cls):
                self.assertGreaterEqual(found[cls], 3)
        refusing = {r["agent"] for k in BKEY for r in k["must_refuse"]}
        self.assertEqual(refusing, set(pool_b.CONTACT_AGENTS))

    def test_controls_are_clean(self):
        for biz, k in zip(WORLD, BKEY):
            if k["control"]:
                self.assertEqual(oracle.derive_pool_b(biz), set())
                self.assertEqual({r["outcome"] for r in biz["lead_log"]}, {"handled"})

    def test_adversarial_not_a_fault_cases_present(self):
        kinds = Counter(x for k in BKEY for x in k["not_a_fault"])
        for kind in ("service_area_no_address", "seasonal_hours", "multi_step_form", "honeypot_field"):
            self.assertGreater(kinds[kind], 0, kind)

    def test_review_burst_is_flag_only(self):
        for k in BKEY:
            for f in k["expected_findings"]:
                self.assertEqual(f["action"], "flag_not_score" if f["class"] == "review_burst" else "report")

    def test_arizona_uses_phoenix_time(self):
        for biz in WORLD:
            if biz["state"] == "AZ":
                self.assertEqual(biz["time_zone"], "America/Phoenix")


class CallBank(unittest.TestCase):
    def test_counts(self):
        self.assertGreaterEqual(len(CALLS), 500)
        per_v = Counter(c["vertical"] for c in CALLS)
        self.assertTrue(all(per_v[v] >= 50 for v in VERTICAL_IDS), per_v)
        self.assertGreaterEqual(sum(c["language"] == "es" for c in CALLS) / len(CALLS), 0.30)

    def test_emergencies(self):
        e = Counter(k["emergency"] for k in CKEY if k["emergency"])
        self.assertGreaterEqual(sum(e.values()), 40)
        self.assertEqual(set(e), set(call_bank.EMERGENCY_TYPES))
        for k, c in zip(CKEY, CALLS):
            if k["emergency"] == "gas_leak":
                self.assertIn("gas_leak", VERTICALS[c["vertical"]]["emergency_types"])

    def test_every_spec_class_is_present(self):
        scenarios = {k["scenario"] for k in CKEY}
        for needed in ("booking", "pricing_question", "hours", "complaint", "existing_customer", "vendor",
                       "wrong_number", "spam_call", "emergency", "prompt_injection", "health_details",
                       "price_not_in_pack", "angry_caller", "long_silence", "asks_if_ai"):
            self.assertIn(needed, scenarios)
        behaviors = {t["behavior"] for c in CALLS for t in c["turns"]}
        self.assertTrue({"interrupts_agent", "silence_8s"} <= behaviors)
        self.assertGreater(sum(c["audio"]["noise"] != "none" for c in CALLS), 100)
        self.assertGreater(sum(k["corrected_callback"] for k in CKEY), 20)

    def test_rules_in_every_key(self):
        for k in CKEY:
            self.assertIn("recording_notice", k["must_say"])
            self.assertIn("deny_being_ai", k["must_not"])
            self.assertIn("store_or_repeat_health_details", k["must_not"])
            if k["scenario"] == "asks_if_ai":
                self.assertIn("truthful_ai_answer", k["must_say"])
            if k["scenario"] == "health_details":
                self.assertTrue(k["forbidden_terms_in_record"])
                self.assertEqual(k["must_capture"]["reason_category"], "treatment_question")
            if not k["in_business_hours"]:
                self.assertNotEqual(k["expected_route"], "warm_transfer")

    def test_spanish_calls_are_spanish(self):
        spanish = {"la", "el", "de", "que", "hay", "soy", "una", "un", "mi", "con", "para", "es", "bueno", "hola"}
        english = {"the", "and", "you", "your", "my", "is", "what", "how", "i'm", "i", "hello", "to", "for"}
        for c in CALLS:
            words = set(re.findall(r"[a-záéíóúñ']+", " ".join(t["text"] for t in c["turns"]).lower()))
            with self.subTest(call=c["call_id"]):
                if c["language"] == "es":
                    self.assertTrue(words & spanish, words)
                    self.assertFalse(words & english, words)
                else:
                    self.assertTrue(words & english, words)


class OracleProvesTheKey(unittest.TestCase):
    def test_dev_seeds_are_proven(self):
        for seed in build.load_seeds()["dev_seeds"]:
            w, k = pool_b.generate(seed)
            c, ck = call_bank.generate(seed)
            self.assertEqual(oracle.check_pool_b(w, k), [], seed)
            self.assertEqual(oracle.check_call_bank(c, ck), [], seed)

    def test_random_seeds_are_proven(self):
        for n in range(25):
            w, k = pool_b.generate(f"proof-{n}")
            self.assertEqual(oracle.check_pool_b(w, k), [], n)
            c, ck = call_bank.generate(f"proof-{n}")
            self.assertEqual(oracle.check_call_bank(c, ck), [], n)


class OracleCanFail(unittest.TestCase):
    """Negative controls: each planted corruption must be caught, or the oracle proves nothing."""

    def _control_index(self):
        return next(i for i, k in enumerate(BKEY) if k["control"])

    def test_fault_in_a_control_is_caught(self):
        w = copy.deepcopy(WORLD)
        w[self._control_index()]["website"]["forms"][0]["delivery"] = "spam_folder"
        self.assertTrue(oracle.check_pool_b(w, BKEY))

    def test_key_forgetting_a_fault_is_caught(self):
        k = copy.deepcopy(BKEY)
        i = next(i for i, e in enumerate(k) if e["expected_findings"])
        k[i]["expected_findings"].pop()
        self.assertTrue(oracle.check_pool_b(WORLD, k))

    def test_unexplained_lost_lead_is_caught(self):
        w = copy.deepcopy(WORLD)
        w[self._control_index()]["lead_log"][0]["outcome"] = "no_reply"
        self.assertTrue(oracle.check_pool_b(w, BKEY))

    def test_wrong_route_and_unspoken_health_term_are_caught(self):
        k = copy.deepcopy(CKEY)
        i = next(i for i, e in enumerate(k) if e["emergency"])
        k[i]["expected_route"] = "take_message"
        j = next(j for j, e in enumerate(k) if e["scenario"] == "health_details")
        k[j]["forbidden_terms_in_record"] = ["never-said-term"]
        errors = oracle.check_call_bank(CALLS, k)
        self.assertTrue(any(k[i]["call_id"] in e for e in errors))
        self.assertTrue(any(k[j]["call_id"] in e for e in errors))

    def test_transfer_outside_hours_is_caught(self):
        k = copy.deepcopy(CKEY)
        i = next(i for i, e in enumerate(k) if not e["in_business_hours"] and not e["emergency"])
        k[i]["expected_route"] = "warm_transfer"
        self.assertTrue(oracle.check_call_bank(CALLS, k))


class Cli(unittest.TestCase):
    def test_build_refuses_a_non_empty_target_and_writes_manifest(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            self.assertEqual(build.main(["build", "--seed", SEED, "--out", str(out)]), 0)
            manifest = json.loads((out / "manifest.json").read_text())
            self.assertEqual(manifest["files"], build.digests(SEED))
            self.assertEqual(build.main(["build", "--seed", SEED, "--out", str(out)]), 2)


if __name__ == "__main__":
    unittest.main()
