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
import urllib.parse
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poolgen import build, call_bank, common, oracle, pool_b  # noqa: E402 (after the sys.path insert)
from poolgen.verticals import VERTICAL_IDS, VERTICALS  # noqa: E402

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
    """Every rendered value is fictional: phones in 555-0100..0199, hosts reserved, no IP, email, SSN or card shapes.

    What is read (AEGIS test-verification on b76b94f2 and 2cb71287): every file is JSON lines and every line is
    parsed, refusing duplicate keys; every string, number and dict key is checked, after NFKC normalisation (so
    fullwidth, circled and superscript digits and fullwidth dots read as plain ones). Never the raw text, so a JSON
    escape such as "\n" in front of a number cannot hide it. Every Unicode decimal digit is then read as ASCII, and
    dot look-alikes as ".". A JSON number is checked for 10- and 11-digit phones and
    card shapes; a 7-digit JSON number is a count or a time, not a local phone.
    Numbers: any run of digit groups joined by up to five non-alphanumeric characters; every 7-, 10- or 11-digit span
    of whole groups that is a dialable North American number (area code and exchange start 2-9) must be in
    555-0100..0199 (a 7-digit local span must end in a 4-digit group, so "1-602-555"
    inside a fictional "+1-602-555-0117" is not read as a local number). Any 13- to 19-digit span that passes the
    Luhn check is a card shape and fails.
    Hosts: every domain-shaped token (any script) and every host after a "scheme://" must be reserved (".test" or
    "example.com"), so a second address, "www." and bare forms, "//host", "localhost", IPv6 and integer IPs are read.
    IPv4 literals (a sentence-final dot included), any email address and SSN shapes fail outright.
    Generated id fields are the one exemption, from the number scan only: each must match its field's exact
    generated shape (prefix and hex length), so an arbitrary value such as "lead-2138675309" fails.
    Residual, by construction: numbers spelled in words; digits joined by letters ("213x867x5309") or by six or
    more separators; hosts written with spaces or "[.]"; an SSN with no separators; any 12-digit number; a card
    number that fails the Luhn check; a bare IPv6 address such as "::1"; and a value in an id field with exactly the generated shape whose hex happens
    to contain digits (hash output, not something the generator could copy from a real record)."""

    HEX16 = r"[0-9a-f]{16}"
    ID_SHAPES = {
        "business_id": rf"(?:svc|loc|sync)-{HEX16}", "call_id": rf"call-{HEX16}", "case_id": rf"xfer-{HEX16}",
        "citation_id": rf"cit-{HEX16}", "client_id": rf"client-{HEX16}", "missed_call_id": rf"mc-{HEX16}",
        "page_id": rf"page-{HEX16}", "pair_id": rf"pair-{HEX16}", "request_id": rf"revreq-{HEX16}",
        "review_id": rf"rev-[0-9a-f]{{10}}|rev7-{HEX16}", "send_id": rf"send-client-{HEX16}",
        "transcript_id": rf"tr-{HEX16}", "lead_id": rf"svc-{HEX16}-L\d{{3}}", "lead_ids": rf"svc-{HEX16}-L\d{{3}}",
        "hidden_effect_lead_ids": rf"svc-{HEX16}-L\d{{3}}",
    }
    ID_FIELDS = frozenset(ID_SHAPES)
    DIRECTORY_PATH = re.compile(r"(/biz/)[0-9a-f]{8}$")      # the directory's listing id in a citation URL
    CHAIN = re.compile(r"\d+(?:[\W_]{1,5}\d+)*")
    GROUP = re.compile(r"\d+")
    DOMAIN = re.compile(r"(?<![^\W_])(?<!-)(?:[^\W_](?:[\w-]{0,61}[^\W_])?\.)+[^\W\d_][\w-]{0,61}[^\W_](?![^\W_])(?!-)")
    SCHEME_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s\"'<>]*", re.IGNORECASE)
    IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?!\d)(?!\.\d)")
    EMAIL = re.compile(r"[\w.+-]+@[\w.-]+")
    SSN = re.compile(r"(?<!\d)\d{3}[\W_]{1,5}\d{2}[\W_]{1,5}\d{4}(?!\d)")

    @classmethod
    def id_ok(cls, key: str, value: str) -> bool:
        return re.fullmatch(cls.ID_SHAPES[key], value) is not None

    @staticmethod
    def _no_duplicate_keys(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate key in {keys}")
        return dict(pairs)

    @staticmethod
    def _strings(obj, key=""):
        """Every string, number and dict key, as (field, text). A key is checked as text of its own field."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield "", k
                yield from FixtureSafety._strings(v, k)
        elif isinstance(obj, list):
            for v in obj:
                yield from FixtureSafety._strings(v, key)
        elif isinstance(obj, str):
            yield key, obj
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            yield "#" + key, str(obj)          # "#": a JSON number, where a 7-digit value is a count or a time

    def _values(self, files):
        for name, text in files.items():
            self.assertTrue(name.endswith(".jsonl"), f"{name}: only JSON lines are scanned, so nothing else may ship")
            for n, line in enumerate(text.splitlines(), 1):
                if line.strip():
                    row = json.loads(line, object_pairs_hook=self._no_duplicate_keys)
                    for key, value in self._strings(row):
                        yield f"{name}:{n}:{key}", key, value

    @classmethod
    def numbers(cls, text: str):
        """Every 7-, 10- or 11-digit span of whole digit groups, as "+1" plus 10 digits or the bare 7 digits."""
        for chain in cls.CHAIN.finditer(text):
            groups = cls.GROUP.findall(chain.group())
            for i in range(len(groups)):
                span = ""
                for g in groups[i:]:
                    span += g
                    if len(span) > 11:
                        break
                    if len(span) == 11 and span[0] == "1" and cls._nanp(span[1:]):
                        yield "+" + span
                    elif len(span) == 10 and cls._nanp(span):
                        yield "+1" + span
                    elif len(span) == 7 and len(g) in (4, 7) and span[0] in "23456789":
                        yield span                                 # a local number ends in its 4-digit line

    @staticmethod
    def _nanp(ten: str) -> bool:
        """Area code and exchange both start 2-9: anything else is not a dialable North American number."""
        return ten[0] in "23456789" and ten[3] in "23456789"

    @classmethod
    def cards(cls, text: str):
        for chain in cls.CHAIN.finditer(text):
            groups = cls.GROUP.findall(chain.group())
            for i in range(len(groups)):
                span = ""
                for g in groups[i:]:
                    span += g
                    if len(span) > 19:
                        break
                    if len(span) >= 13 and cls._luhn(span):
                        yield span

    @staticmethod
    def _luhn(digits: str) -> bool:
        total = 0
        for i, c in enumerate(reversed(digits)):
            d = int(c) * (2 if i % 2 else 1)
            total += d - 9 if d > 9 else d
        return total % 10 == 0

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
        for url in cls.SCHEME_URL.findall(text):
            try:
                host = urllib.parse.urlsplit(url.replace("\\", "/")).hostname
            except ValueError:
                host = None
            if url.lower().startswith(("http:", "https:", "ftp:", "ws:", "wss:")) or host:
                yield (host or "").lower()          # an unreadable or empty host is not reserved either

    DOT_LIKE = re.compile("[\u3002\uff61\u00b7\u2219\u06d4\u2024\u2027\u30fb]")

    @classmethod
    def normalise(cls, value: str) -> str:
        """NFKC, then every Unicode decimal digit to ASCII (NFKC leaves Arabic-Indic, Thai, Bengali, Devanagari and
        other script digits alone: AEGIS 7dc6a8a1 High 1), then dot look-alikes to "." and runs of dots to one."""
        value = unicodedata.normalize("NFKC", value)
        value = "".join(str(unicodedata.decimal(c)) if not c.isascii() and unicodedata.decimal(c, None) is not None
                        else c for c in value)
        return re.sub(r"\.{2,}", ".", cls.DOT_LIKE.sub(".", value))

    @classmethod
    def offending(cls, key: str, value: str) -> list:
        """Everything in one decoded value that is not fictional, empty when the value is safe."""
        value = cls.normalise(value)
        bad = []
        if key in cls.ID_FIELDS and not cls.id_ok(key, value):
            bad.append(f"not a generated {key}")
        digits = cls.phone_text(key, value)
        bad += [n for n in cls.numbers(digits) if not cls.fictional_number(n)
                and not (key.startswith("#") and len(n) == 7)]
        bad += list(cls.cards(digits))
        bad += [h or "(no host)" for h in cls.hosts(value) if not (h and common.is_fixture_domain(h))]
        bad += cls.IPV4.findall(value) + cls.EMAIL.findall(value) + cls.SSN.findall(digits)
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
                self.assertTrue(self.id_ok(key, value), f"{where}: {value!r}")
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
                  "http://8.8.8.8/", "a@realbiz.com", "mailto:a@shop.test", "123-45-6789", "123 45 6789",
                  # found by the 2cb71287 pass
                  "my server is 8.8.8.8.", "213    867    5309", "213     867     5309", "r\u00e9albiz.com",
                  "realbiz\u3002com", "realbiz\uff0ecom", "realbiz.com_", "_realbiz.com", "http://localhost/",
                  "a@localhost", "http://[2001:db8::1]/", "http://134744072/", "4111 1111 1111 1111",
                  "4111-1111-1111-1111", "\u2461\u2460\u2462 \u2467\u2465\u2466 \u2464\u2462\u2460\u2468",
                  "\u00b2\u00b9\u00b3\u2078\u2076\u2077\u2075\u00b3\u2070\u2079", "12 213 867 5309",
                  "8675309", "myexample.com", "realbiztest.com", "notexample.com", "123  45  6789",
                  # found by the 7dc6a8a1 pass
                  "\u0662\u0661\u0663 \u0668\u0666\u0667 \u0665\u0663\u0660\u0669",
                  "\u0e52\u0e51\u0e53 \u0e58\u0e56\u0e57 \u0e55\u0e53\u0e50\u0e59",
                  "\u09e8\u09e7\u09e9-\u09ee\u09ec\u09ed-\u09eb\u09e9\u09e6\u09ef",
                  "\u0968\u0967\u0969\u096e\u096c\u096d\u096b\u0969\u0966\u096f", "213 \u0668\u0666\u0667 5309",
                  "realbiz\u2025com", "realbiz\u00b7com", "realbiz\u2219com", "realbiz\u06d4com", "realbiz\uff61com",
                  "x.contest", "123-----45-----6789", "4222222222222", "4111 1111 1111 1111 003", "6200000000000000000",
                  "http:///path", "see /biz/8675309a"]
        for form in unsafe:
            with self.subTest(form=form):
                line = common.canonical_json({**row, "note": form})
                found = [b for _, key, value in self._values({name: line}) for b in self.offending(key, value)]
                self.assertTrue(found, form)
        safe = ["213-555-0142", "+12135550100", "+1 (213) 555-0199", "555-0156", "https://shop-1.test/book",
                "www.example.com", "$1,500 per month", "2026-12-25", "08:00-17:00", "Suite 210",
                "shop.example.com", "a.b.test", "1795777200", "version 1.2.3", "Unit 2. 555-0101 is ours.",
                "123-056-7890", "213-056-7890", "1-123-056-7890", "1234 5678 9012 3456", "86 75309"]
        for form in safe:
            with self.subTest(form=form):
                line = common.canonical_json({**row, "note": form})
                self.assertEqual([b for _, key, value in self._values({name: line})
                                  for b in self.offending(key, value)], [], form)

    def test_the_scan_reads_numbers_keys_duplicates_and_id_fields(self):
        """Not only string values: JSON numbers, dict keys, a value shadowed by a duplicate key, and an id field whose
        value is not the generated shape (AEGIS 2cb71287 High 1 and 2)."""
        name, text = next(iter(build.render(SEED).items()))
        row = json.loads(text.splitlines()[0])
        good_lead = "svc-0123456789abcdef-L001"
        for safe in ({"lead_id": good_lead}, {"url": "https://directory-02.test/biz/0123abcd"}):
            line = common.canonical_json({**row, **safe})
            self.assertEqual([b for _, key, value in self._values({name: line}) for b in self.offending(key, value)],
                             [], safe)
        lines = [common.canonical_json({**row, "phone_number": 2138675309}),
                 common.canonical_json({**row, "2138675309": "x"}),
                 common.canonical_json({**row, "realbiz.com": "x"}),
                 common.canonical_json({**row, "lead_id": "lead-2138675309"}),
                 common.canonical_json({**row, "lead_id": "svc-2138675309"}),
                 common.canonical_json({**row, "lead_id": good_lead + " 213 867 5309"}),
                 common.canonical_json({**row, "lead_id": good_lead + "\n"}),
                 common.canonical_json({**row, "client_id": "client-0123456789abcde"}),
                 common.canonical_json({**row, "lead_ids": [good_lead, "svc-2138675309"]}),
                 common.canonical_json({**row, "card": 4111111111111111}),
                 common.canonical_json({**row, "lead_id": "svc-0123456789abcdef-L12"}),
                 common.canonical_json({**row, "url": "https://directory-02.test/biz/2138675309"}),
                 common.canonical_json({**row, "note": "https://realbiz.com/biz/0123abcd"}),
                 common.canonical_json({**row, "note": "see /biz/2138675309"}),
                 common.canonical_json(row)[:-1] + ',"note":"213 867 5309","note":"x"}']
        for line in lines:
            with self.subTest(line=line[-60:]):
                try:
                    found = [b for _, key, value in self._values({name: line}) for b in self.offending(key, value)]
                except ValueError:
                    found = ["refused"]
                self.assertTrue(found, line)
        with self.assertRaises(AssertionError):                 # a file that is not JSON lines is refused
            list(self._values({"notes.txt": "213 867 5309"}))

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
