"""Every non-label field of every answer key is proven: changing any one of them, or adding or dropping a row, is caught.

Labels (the planted class names a set is organised by: pool-B classes are re-derived, but the call bank's
``scenario``, F2/F4 ``case`` and F5 ``episode`` are names) are excluded from the field sweep by name below. The
caller's name and the health terms are re-derived from the call text and swept row by row, not just once per field.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poolgen import (
    call_bank,
    ci_watch,
    ci_watch_oracle,
    f3_transcripts,
    frontdesk_oracle,
    frontdesk_sets,
    local_ops,
    local_ops_oracle,
    oracle,
    pool_b,
    seo_local,
    seo_local_oracle,
)

SEED = "dev-a"
SETS = {
    "pool_b": (*pool_b.generate(SEED), oracle.check_pool_b, set()),
    "call_bank": (*call_bank.generate(SEED), oracle.check_call_bank, {"scenario"}),
    "f2": (*frontdesk_sets.generate_f2(SEED), frontdesk_oracle.check_f2, {"case"}),
    "f4": (*frontdesk_sets.generate_f4(SEED), frontdesk_oracle.check_f4, {"case"}),
    "f5": (*frontdesk_sets.generate_f5(SEED), frontdesk_oracle.check_f5, {"episode"}),
    "f3": (*f3_transcripts.generate(SEED), frontdesk_oracle.check_f3, set()),
    "seo_l4": (*seo_local.generate_l4(SEED), seo_local_oracle.check_l4, set()),
    "seo_l5": (*seo_local.generate_l5(SEED), seo_local_oracle.check_l5, set()),
    "ci_watch": (*ci_watch.generate(SEED), ci_watch_oracle.check, set()),
    "seo_l1": (*local_ops.generate_l1(SEED), local_ops_oracle.check_l1, set()),
    "seo_l6": (*local_ops.generate_l6(SEED), local_ops_oracle.check_l6, set()),
}


def subject_text(calls, call_id):
    return " ".join(t["text"] for c in calls if c["call_id"] == call_id for t in c["turns"])


def _tampered(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return value + "-tampered"
    if isinstance(value, list):
        return value[:-1] if value else ["tampered"]
    if isinstance(value, dict):
        return {**value, "tampered": True}
    return "tampered"   # None and anything else


class KeyTamper(unittest.TestCase):
    def test_every_field_change_is_caught(self):
        for name, (subject, key, check, labels) in SETS.items():
            self.assertEqual(check(subject, key), [], name)
            seen = set()
            for i, row in enumerate(key):
                for field in row:
                    if field in labels or (field, repr(type(row[field]))) in seen:
                        continue
                    seen.add((field, repr(type(row[field]))))
                    bad = copy.deepcopy(key)
                    bad[i][field] = _tampered(row[field])
                    with self.subTest(set=name, row=i, field=field):
                        self.assertTrue(check(subject, bad), f"{name}: changing {field} went unnoticed")

    def test_every_call_bank_name_and_health_list_is_checked_row_by_row(self):
        """AEGIS round 2: a per-(field, type) sweep missed rows whose check was 'appears somewhere in the text'."""
        subject, key, check, _ = SETS["call_bank"]
        for i, row in enumerate(key):
            if row["must_capture"]:
                first_word = subject_text(subject, row["call_id"]).split()[0]
                for wrong in (first_word, row["must_capture"]["name"].split()[0]):
                    bad = copy.deepcopy(key)
                    bad[i]["must_capture"]["name"] = wrong
                    with self.subTest(row=i, name=wrong):
                        self.assertTrue(check(subject, bad))
            if row["forbidden_terms_in_record"]:
                for wrong in ([], row["forbidden_terms_in_record"][:1] + ["tampered"], ["Hi"]):
                    bad = copy.deepcopy(key)
                    bad[i]["forbidden_terms_in_record"] = wrong
                    with self.subTest(row=i, terms=wrong):
                        self.assertTrue(check(subject, bad))

    def test_extra_and_missing_rows_are_caught(self):
        for name, (subject, key, check, _labels) in SETS.items():
            with self.subTest(set=name, case="extra"):
                self.assertTrue(check(subject, key + [copy.deepcopy(key[0])]))
            with self.subTest(set=name, case="missing"):
                self.assertTrue(check(subject, key[1:]))


if __name__ == "__main__":
    unittest.main()
