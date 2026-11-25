"""The held-out exam stays secret, and the listed thresholds are tested exactly at their line (AEGIS e41e57b5, 95ba0730)."""

from __future__ import annotations

import secrets
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poolgen import (
    build,
    call_bank,
    ci_watch,
    f3_transcripts,
    frontdesk_oracle,
    frontdesk_sets,
    oracle,
    pool_b,
    seo_local,
)

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
                              (seo_local.generate_l4, "business_id"), (seo_local.generate_l5, "pair_id"),
                              (f3_transcripts.generate, "transcript_id"), (ci_watch.generate, "page_id")):
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


class NoAnswerInVisibleFields(unittest.TestCase):
    """AEGIS round 2 (95ba0730): pool B's vertical and phone followed the loop index, and review ids named the class."""

    def test_vertical_and_phone_do_not_recover_the_planted_role(self):
        for seed in ("dev-a", FRESH):
            world, key = pool_b.generate(seed)
            kb = {k["business_id"]: k for k in key}
            hits = 0
            for b in world:
                v = pool_b.VERTICAL_IDS.index(b["vertical"])
                m = int(b["main_phone"][-2:]) % 50
                guess = next((x for x in range(len(world)) if x % len(pool_b.VERTICAL_IDS) == v and x % 50 == m), 0)
                hits += (guess >= pool_b.LEAKY_COUNT) == kb[b["business_id"]]["control"]
            self.assertLess(hits / len(world), 0.95, seed)

    def test_review_ids_are_opaque_and_unique(self):
        import re
        for b in pool_b.generate(FRESH)[0]:
            ids = [r["review_id"] for r in b["reviews"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue(all(re.fullmatch(r"rev-[0-9a-f]{10}", i) for i in ids), ids[:3])

    def test_pool_b_oracle_ignores_review_ids(self):
        import copy
        world, key = pool_b.generate("dev-b")
        renamed = copy.deepcopy(world)
        for b in renamed:
            for n, r in enumerate(b["reviews"]):
                r["review_id"] = f"b{n:03d}"
        self.assertEqual(oracle.check_pool_b(renamed, key), [])


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
        replies = {r["owner_reply_after_days"] for b in world for r in b["reviews"]}
        self.assertIn(oracle.SLOW_REVIEW_REPLY_DAYS, replies)       # 7 days: not slow
        self.assertIn(oracle.SLOW_REVIEW_REPLY_DAYS + 1, replies)   # 8 days: slow
        rates = []
        for b in world:
            recent = sorted(r["days_ago"] for r in b["reviews"] if r["days_ago"] <= 730)
            burst = {d for i, x in enumerate(recent) for d in recent[i:i + oracle.BURST_COUNT]
                     if len([y for y in recent[i:] if y - x < oracle.BURST_WINDOW_DAYS]) >= oracle.BURST_COUNT
                     and d - x < oracle.BURST_WINDOW_DAYS}
            organic = [r for r in b["reviews"] if r["days_ago"] <= 730 and r["days_ago"] not in burst]
            if organic:
                rates.append(sum(r["owner_reply_after_days"] is not None for r in organic) / len(organic))
        self.assertIn(oracle.LOW_REPLY_RATE, rates)                  # exactly 30%: not a low reply rate

    def test_f5_look_alikes_are_allowed(self):
        _, key = frontdesk_sets.generate_f5("dev-a")
        self.assertTrue(any(k["episode"] == "look_alike_word_then_send" and k["decision"] == "allow" for k in key))
        for word in frontdesk_sets.LOOK_ALIKES:
            self.assertFalse(frontdesk_oracle._looks_like_stop(word), word)
        for word in frontdesk_sets.TYPO_STOPS:
            self.assertTrue(frontdesk_oracle._looks_like_stop(word), word)


if __name__ == "__main__":
    unittest.main()
