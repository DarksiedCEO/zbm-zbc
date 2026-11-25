"""The held-out exam stays secret, and every threshold is tested exactly at its line (AEGIS review of e41e57b5)."""

from __future__ import annotations

import secrets
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poolgen import build, call_bank, frontdesk_oracle, frontdesk_sets, oracle, pool_b, seo_local

FRESH = secrets.token_hex(16)   # stands in for a privately drawn held-out seed


class HeldOutSecrecy(unittest.TestCase):
    def test_no_subject_file_contains_the_seed(self):
        for name, text in build.render(FRESH).items():
            self.assertNotIn(FRESH, text, name)
            self.assertNotIn(FRESH[:8], text, name)

    def test_every_answer_file_changes_with_the_seed(self):
        dev, fresh = build.digests("dev-a"), build.digests(FRESH)
        for name in dev:
            self.assertNotEqual(dev[name], fresh[name], name)

    def test_ids_from_the_public_key_predict_nothing(self):
        for gen, id_field in ((pool_b.generate, "business_id"), (call_bank.generate, "call_id"),
                              (frontdesk_sets.generate_f2, "missed_call_id"),
                              (frontdesk_sets.generate_f4, "case_id"), (frontdesk_sets.generate_f5, "send_id"),
                              (seo_local.generate_l4, "business_id"), (seo_local.generate_l5, "pair_id")):
            dev_ids = {k[id_field] for k in gen("dev-a")[1]}
            fresh_ids = {k[id_field] for k in gen(FRESH)[1]}
            self.assertFalse(dev_ids & fresh_ids, id_field)

    def test_position_does_not_give_the_answer(self):
        """Answers lined up by position across two seeds must disagree often (no fixed order of classes)."""
        def same_position_agreement(gen, field):
            a, b = gen("dev-a")[1], gen(FRESH)[1]
            return sum(x[field] == y[field] for x, y in zip(a, b)) / len(a)
        self.assertLess(same_position_agreement(frontdesk_sets.generate_f2, "action"), 0.8)
        self.assertLess(same_position_agreement(frontdesk_sets.generate_f4, "path"), 0.8)
        self.assertLess(same_position_agreement(frontdesk_sets.generate_f5, "episode"), 0.5)
        self.assertLess(same_position_agreement(pool_b.generate, "control"), 0.95)

    def test_fresh_seed_is_still_proven(self):
        w, k = pool_b.generate(FRESH)
        self.assertEqual(oracle.check_pool_b(w, k), [])
        c, ck = call_bank.generate(FRESH)
        self.assertEqual(oracle.check_call_bank(c, ck), [])
        self.assertEqual(frontdesk_oracle.check_f5(*frontdesk_sets.generate_f5(FRESH)), [])


class Boundaries(unittest.TestCase):
    """Each threshold appears exactly on its line, on the right side, so an off-by-one detector fails."""

    def test_f2_quiet_hours_and_repeat_window_lines(self):
        seen = set()
        for seed in ("dev-a", "dev-b", "dev-c"):
            for e, k in zip(*frontdesk_sets.generate_f2(seed)):
                t, gap = e["recipient_local_time"], e["seconds_since_last_text_to_number"]
                if t in ("20:59", "08:00") and k["action"] == "send_now":
                    seen.add(t + " sends")
                if t in ("21:00", "07:59") and k["action"] == "defer_until_0800":
                    seen.add(t + " waits")
                if gap == 300 and k["action"] == "send_now":
                    seen.add("300s sends")
                if gap == 299 and k["action"] == "do_not_send":
                    seen.add("299s holds")
        self.assertEqual(seen, {"20:59 sends", "08:00 sends", "21:00 waits", "07:59 waits", "300s sends",
                                "299s holds"})

    def test_pool_b_lines(self):
        world, _ = pool_b.generate("dev-a")
        w2, _ = pool_b.generate("dev-b")
        world = world + w2
        form = [b["website"]["forms"][0]["human_reply_minutes"] for b in world]
        dm = [b["dm_inbox"]["human_reply_minutes"] for b in world]
        cb = [b["phone_line"]["callback_minutes"] for b in world]
        taps = [vp["call_button_px"] for b in world for vp in b["website"]["mobile"].values()]
        for values, line in ((form, oracle.FORM_SLOW_REPLY_MIN), (dm, oracle.DM_SLOW_MIN), (cb, oracle.CALLBACK_SLOW_MIN)):
            self.assertIn(line, values)        # exactly on the line: NOT slow
            self.assertIn(line + 1, values)    # one past: slow
        self.assertIn(oracle.TAP_TARGET_MIN_PX, taps)
        self.assertIn(oracle.TAP_TARGET_MIN_PX - 1, taps)
        firsts = [min(r["days_ago"] for r in b["reviews"]) for b in world]
        self.assertIn(oracle.RECENCY_GAP_DAYS, firsts)
        self.assertIn(oracle.RECENCY_GAP_DAYS + 1, firsts)

    def test_f5_look_alikes_are_allowed(self):
        _, key = frontdesk_sets.generate_f5("dev-a")
        self.assertTrue(any(k["episode"] == "look_alike_word_then_send" and k["decision"] == "allow" for k in key))
        for word in frontdesk_sets.LOOK_ALIKES:
            self.assertFalse(frontdesk_oracle._looks_like_stop(word), word)
        for word in frontdesk_sets.TYPO_STOPS:
            self.assertTrue(frontdesk_oracle._looks_like_stop(word), word)


if __name__ == "__main__":
    unittest.main()
