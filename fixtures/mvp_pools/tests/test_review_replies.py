"""SEO-L7 review replies: privacy in health and legal verticals, fakes, refunds, and the traps."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poolgen import review_replies, review_replies_oracle

REVIEWS, KEY = review_replies.generate("dev-a")
BY_ID = {r["review_id"]: r for r in REVIEWS}


class ReviewReplies(unittest.TestCase):
    def test_size(self):
        self.assertEqual(len(REVIEWS), 300)
        self.assertEqual(len({r["review_id"] for r in REVIEWS}), 300)

    def test_regulated_verticals_never_confirm_or_echo(self):
        for k in KEY:
            r = BY_ID[k["review_id"]]
            if r["vertical"] in review_replies.REGULATED:
                self.assertIn("confirm_patient_or_client_relationship", k["must_not"])
                self.assertTrue(k["owner_approval_before_posting"])
                for term in k["detail_terms_never_in_reply"]:
                    self.assertIn(term, r["text"].lower())

    def test_five_star_health_review_with_a_detail_exists(self):
        self.assertTrue(any(BY_ID[k["review_id"]]["stars"] == 5 and k["detail_terms_never_in_reply"] for k in KEY))

    def test_genuine_new_reviewer_is_not_a_fake(self):
        new = [k for k in KEY if BY_ID[k["review_id"]]["reviewer"]["review_count"] == 1
               and BY_ID[k["review_id"]]["same_day_reviews_from_new_accounts"] < 5]
        self.assertGreaterEqual(len(new), 20)
        self.assertFalse(any(k["flag_suspected_fake"] for k in new))

    def test_refund_word_in_a_happy_review_is_not_a_request(self):
        happy = [k for k in KEY if "refund" in BY_ID[k["review_id"]]["text"] and BY_ID[k["review_id"]]["stars"] == 5]
        self.assertGreaterEqual(len(happy), 10)
        self.assertFalse(any(k["route_to_owner"] for k in happy))

    def test_every_edge_of_the_fake_rule_is_generated(self):
        """AEGIS round 4: each loosened fake rule must disagree with the key somewhere."""
        loosened = {
            "age 30": lambda r: r["reviewer"]["review_count"] == 1 and r["reviewer"]["account_age_days"] <= 30
            and r["same_day_reviews_from_new_accounts"] >= 5,
            "two reviews": lambda r: r["reviewer"]["review_count"] <= 2 and r["reviewer"]["account_age_days"] <= 7
            and r["same_day_reviews_from_new_accounts"] >= 5,
            "pile-up 3": lambda r: r["reviewer"]["review_count"] == 1 and r["reviewer"]["account_age_days"] <= 7
            and r["same_day_reviews_from_new_accounts"] >= 3,
            "pile-up only": lambda r: r["same_day_reviews_from_new_accounts"] >= 5,
        }
        rows = [(r, k) for seed in ("dev-a", "dev-b") for r, k in zip(*review_replies.generate(seed))]
        for name, rule in loosened.items():
            with self.subTest(rule=name):
                self.assertTrue(any(rule(r) != k["flag_suspected_fake"] for r, k in rows), name)

    def test_key_is_proven_and_every_row_can_fail(self):
        self.assertEqual(review_replies_oracle.check(REVIEWS, KEY), [])
        for i, k in enumerate(KEY):
            bad = copy.deepcopy(KEY)
            bad[i]["flag_suspected_fake"] = not k["flag_suspected_fake"]
            bad[i]["detail_terms_never_in_reply"] = k["detail_terms_never_in_reply"][1:] or ["tampered"]
            with self.subTest(row=i):
                self.assertTrue(review_replies_oracle.check(REVIEWS, bad))


if __name__ == "__main__":
    unittest.main()
