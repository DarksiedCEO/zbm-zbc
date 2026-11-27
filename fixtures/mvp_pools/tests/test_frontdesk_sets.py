"""Behaviour tests for the F2 (text-back), F4 (transfer router) and F5 (consent keeper) test sets."""

from __future__ import annotations

import copy
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poolgen import common, f3_transcripts
from poolgen import frontdesk_oracle as oracle
from poolgen import frontdesk_sets as sets

SEED = "dev-a"
F2, F2K = sets.generate_f2(SEED)
F4, F4K = sets.generate_f4(SEED)
F5, F5K = sets.generate_f5(SEED)


class TextBack(unittest.TestCase):
    def test_size_and_every_case(self):
        self.assertEqual(len(F2), 300)
        cases = Counter(k["case"] for k in F2K)
        for case in ("landline", "blocked_caller_id", "opted_out", "repeat_within_5_min", "quiet_hours", "send"):
            self.assertGreater(cases[case], 0, case)
        self.assertEqual({e["status"] for e in F2} & {"busy", "after_hours", "dropped", "voicemail"},
                         {"busy", "after_hours", "dropped", "voicemail"})

    def test_key_is_proven(self):
        self.assertEqual(oracle.check_f2(F2, F2K), [])

    def test_boundaries_present(self):
        # just over 5 minutes since the last text may text again; 21:00 exactly is quiet
        self.assertTrue(any((e["seconds_since_last_text_to_number"] or 0) > 300 and k["action"] == "send_now"
                            for e, k in zip(F2, F2K)))
        self.assertTrue(all(k["action"] != "send_now" or k["max_latency_seconds"] == 30 for k in F2K))

    def test_negative_control(self):
        k = copy.deepcopy(F2K)
        i = next(i for i, x in enumerate(k) if x["case"] == "opted_out")
        k[i]["action"] = "send_now"
        self.assertTrue(oracle.check_f2(F2, k))


class Transfer(unittest.TestCase):
    def test_size_and_cases(self):
        self.assertEqual(len(F4), 200)
        self.assertEqual({k["case"] for k in F4K}, {c for c, _ in sets.F4_KINDS})

    def test_every_path_ends_connected_or_in_a_fallback(self):
        for k in F4K:
            self.assertIn(k["path"], ("connected", "fallback_message_and_text_back", "caller_hung_up_log_and_text_back"))
            self.assertIn("drop_caller_without_fallback", k["must_not"])
            self.assertLessEqual(k["max_seconds_to_connect_or_fallback"], 30)

    def test_key_is_proven(self):
        self.assertEqual(oracle.check_f4(F4, F4K), [])

    def test_negative_control(self):
        k = copy.deepcopy(F4K)
        i = next(i for i, x in enumerate(k) if x["case"] == "nobody_on_call")
        k[i]["path"], k[i]["connect_to"] = "connected", "staff-a"
        self.assertTrue(oracle.check_f4(F4, k))


class Consent(unittest.TestCase):
    def test_size(self):
        self.assertGreaterEqual(len(F5), 10000)
        self.assertEqual({k["episode"] for k in F5K}, {e[0] for e in sets.F5_EPISODES})

    def test_spanish_typo_and_carrier_cases_present(self):
        texts = {e.get("text") for e in F5 if e["kind"] == "inbound_sms"}
        self.assertTrue({"ALTO", "alto"} & texts)
        self.assertTrue(set(sets.TYPO_STOPS) & texts)
        kinds = {e["kind"] for e in F5}
        self.assertTrue({"carrier_opt_out", "number_reassigned", "opt_in", "send_attempt"} <= kinds)

    def test_race_is_real_send_sometimes_listed_before_stop(self):
        # Inside one instant the file order is shuffled, so a keeper that applies events in file order would fail.
        race_clients = {k["send_id"][len("send-"):] for k in F5K if k["episode"] == "race_stop_and_send_same_instant"}
        order: dict[str, list[str]] = {}
        for e in F5:
            if e["client_id"] in race_clients and e["kind"] in ("send_attempt", "inbound_sms"):
                order.setdefault(e["client_id"], []).append(e["kind"])
        self.assertTrue(any(kinds[0] == "send_attempt" for kinds in order.values()))
        self.assertTrue(any(kinds[0] == "inbound_sms" for kinds in order.values()))

    def test_key_is_proven(self):
        self.assertEqual(oracle.check_f5(F5, F5K), [])

    def test_naive_file_order_keeper_fails_the_race(self):
        """Negative control: replaying in plain file order lets a racing send through, so the set bites."""
        state, let_through = {}, 0
        for e in F5:
            key = (e["client_id"], e["number"])
            if e["kind"] == "inbound_sms" and e["text"] == "STOP":
                state[key] = "blocked"
            if e["kind"] == "send_attempt" and state.get(key) != "blocked":
                ep = next(k for k in F5K if k["send_id"] == e["send_id"])
                let_through += ep["episode"] == "race_stop_and_send_same_instant"
        self.assertGreater(let_through, 0)

    def test_start_keyword_never_restores(self):
        for k in F5K:
            if k["episode"] == "stop_then_start_keyword_then_send":
                self.assertEqual((k["decision"], k["reason"]), ("block", "start_keyword_does_not_restore"))

    def test_every_phone_is_fictional(self):
        for e in F5 + F2 + F4:
            for field in ("number", "caller_id", "caller_number"):
                if e.get(field):
                    self.assertTrue(common.is_fixture_phone(e[field]), e[field])

    def test_negative_control(self):
        k = copy.deepcopy(F5K)
        i = next(i for i, x in enumerate(k) if x["episode"] == "typo_stop_then_send")
        k[i]["decision"], k[i]["reason"] = "allow", None
        self.assertTrue(oracle.check_f5(F5, k))


class Summarizer(unittest.TestCase):
    T, K = f3_transcripts.generate(SEED)

    def test_size_and_cases(self):
        self.assertEqual(len(self.T), 300)
        self.assertTrue(any(k["must_flag_low_confidence"] for k in self.K))
        self.assertTrue(any(k["corrected_value_must_win"] for k in self.K))
        self.assertTrue(any(k["forbidden_terms_in_summary"] for k in self.K))
        self.assertTrue(any(k["key_facts"]["urgency"] == "emergency" for k in self.K))
        self.assertTrue(any(t["language"] == "es" for t in self.T))

    def test_key_is_proven(self):
        self.assertEqual(oracle.check_f3(self.T, self.K), [])

    def test_low_confidence_number_is_never_the_answer(self):
        for t, k in zip(self.T, self.K):
            low = [x["text"] for x in t["turns"] if x["speaker"] == "caller" and 0 < x["confidence"] < 0.6]
            for text in low:
                self.assertNotIn((k["key_facts"]["callback_number"] or "")[-4:], text.replace(" ", ""))

    def test_recording_notice_opens_every_transcript(self):
        for t in self.T:
            self.assertTrue("recorded" in t["turns"][0]["text"] or "grabada" in t["turns"][0]["text"])


class Separation(unittest.TestCase):
    def test_subject_files_carry_no_answers(self):
        for rows in (F2, F4, F5):
            for r in rows:
                self.assertFalse({"action", "decision", "reason", "path", "connect_to", "case", "episode"} & set(r), r)


if __name__ == "__main__":
    unittest.main()
