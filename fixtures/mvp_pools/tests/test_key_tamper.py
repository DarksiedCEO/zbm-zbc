"""Every non-label field of every answer key is proven: changing any one of them, or adding or dropping a row, is caught.

Labels (the planted class names a set is organised by: pool-B classes are re-derived, but the call bank's
``scenario``, F2/F4 ``case`` and F5 ``episode`` are names, and the health terms are checked against the call text)
are excluded from the field sweep by name below.
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
}


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

    def test_extra_and_missing_rows_are_caught(self):
        for name, (subject, key, check, _labels) in SETS.items():
            with self.subTest(set=name, case="extra"):
                self.assertTrue(check(subject, key + [copy.deepcopy(key[0])]))
            with self.subTest(set=name, case="missing"):
                self.assertTrue(check(subject, key[1:]))


if __name__ == "__main__":
    unittest.main()
