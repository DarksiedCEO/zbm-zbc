"""SEO-L1 listing sync and SEO-L6 review requests: the planted traps exist and the keys are proven."""

from __future__ import annotations

import copy
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poolgen import common, local_ops, local_ops_oracle

L1_S, L1_K = local_ops.generate_l1("dev-a")
L6_S, L6_K = local_ops.generate_l6("dev-a")


class ListingSync(unittest.TestCase):
    def test_size_and_every_action(self):
        self.assertEqual(len(L1_S), 60)
        self.assertTrue(all(len(s["listings"]) == 5 for s in L1_S))
        actions = Counter(p["action"] for k in L1_K for p in k["platforms"])
        self.assertEqual(set(actions), {"none", "update_with_approval", "surface_conflict", "report_platform_error"})
        self.assertTrue(all(n >= 30 for n in actions.values()), actions)

    def test_owner_edit_is_never_overwritten(self):
        for s, k in zip(L1_S, L1_K):
            for lst, ans in zip(s["listings"], k["platforms"]):
                if any(e["edited_by"] == "owner" and e["days_ago"] < lst["last_sync_days_ago"] for e in lst["edit_log"]):
                    self.assertNotEqual(ans["action"], "update_with_approval")

    def test_platform_error_is_never_marked_synced(self):
        for s, k in zip(L1_S, L1_K):
            for lst, ans in zip(s["listings"], k["platforms"]):
                if lst["api_status"] != "ok":
                    self.assertFalse(ans["mark_synced"])

    def test_formatting_and_old_edit_traps_are_present_and_in_sync(self):
        national = old_owner = 0
        for s, k in zip(L1_S, L1_K):
            for lst, ans in zip(s["listings"], k["platforms"]):
                if lst["fields"]["phone"].startswith("("):
                    national += 1
                    self.assertEqual(ans["action"], "none")
                if lst["edit_log"] and all(e["days_ago"] > lst["last_sync_days_ago"] for e in lst["edit_log"]):
                    old_owner += 1
                    self.assertEqual(ans["action"], "none")
        self.assertGreater(national, 10)
        self.assertGreater(old_owner, 5)

    def test_key_is_proven_and_can_fail(self):
        self.assertEqual(local_ops_oracle.check_l1(L1_S, L1_K), [])
        bad = copy.deepcopy(L1_K)
        for p in bad[0]["platforms"]:
            p["action"] = "update_with_approval" if p["action"] == "surface_conflict" else p["action"]
        conflicts = [p for p in L1_K[0]["platforms"] if p["action"] == "surface_conflict"]
        if conflicts:
            self.assertTrue(local_ops_oracle.check_l1(L1_S, bad))

    def test_fixture_data_only(self):
        for s in L1_S:
            self.assertTrue(common.is_fixture_phone(s["approved_record"]["phone"]))
            self.assertTrue(common.is_fixture_domain(s["approved_record"]["website"].split("/")[2]))


class ReviewRequests(unittest.TestCase):
    def test_size_and_mix(self):
        self.assertEqual(len(L6_S), 300)
        sends = [k for k in L6_K if not k["reasons"]]
        self.assertEqual(len(sends), 120)
        seen = Counter("+".join(k["reasons"]) for k in L6_K if k["reasons"])
        want = {"+".join(sorted(c.split("+"))) for c, _ in local_ops.L6_CASES} - {"send"}
        self.assertEqual(set(seen), want)
        self.assertEqual(sum(len(k["reasons"]) == 2 for k in L6_K), 20)

    def test_every_ftc_violation_refuses(self):
        for k in L6_K:
            if set(k["reasons"]) & {"gating", "incentive", "suggested_text", "review_suppression"}:
                self.assertEqual(k["decision"], "refuse")

    def test_look_alike_free_and_email_without_sms_consent_still_send(self):
        free = email_no_sms = 0
        for s, k in zip(L6_S, L6_K):
            if "Feel free" in s["message"] and k["decision"] == "send":
                free += 1
            if s["channel"] == "email" and not s["customer"]["sms_consent"] and k["decision"] == "send":
                email_no_sms += 1
        self.assertGreater(free, 10)
        self.assertGreater(email_no_sms, 10)

    def test_key_is_proven(self):
        self.assertEqual(local_ops_oracle.check_l6(L6_S, L6_K), [])

    def test_dropping_one_of_two_reasons_is_caught(self):
        for i, k in enumerate(L6_K):
            if len(k["reasons"]) == 2:
                for keep in k["reasons"]:
                    bad = copy.deepcopy(L6_K)
                    bad[i]["reasons"] = [keep]
                    with self.subTest(row=i, keep=keep):
                        self.assertTrue(local_ops_oracle.check_l6(L6_S, bad))

    def test_oracle_rejects_a_send_for_every_refusal_row(self):
        for i, k in enumerate(L6_K):
            if k["decision"] == "refuse":
                bad = copy.deepcopy(L6_K)
                bad[i] = dict(k, decision="send", reasons=[])
                with self.subTest(row=i):
                    self.assertTrue(local_ops_oracle.check_l6(L6_S, bad))


if __name__ == "__main__":
    unittest.main()
