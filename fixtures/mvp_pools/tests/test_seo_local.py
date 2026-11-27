"""SEO-L4 citation network and SEO-L5 listing pairs."""

from __future__ import annotations

import copy
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poolgen import common, seo_local
from poolgen import seo_local_oracle as oracle

L4, L4K = seo_local.generate_l4("dev-a")
L5, L5K = seo_local.generate_l5("dev-a")


class CitationNetwork(unittest.TestCase):
    def test_size(self):
        self.assertEqual(len(L4), 200)
        self.assertGreaterEqual(len({li["directory_id"] for b in L4 for li in b["listings"]}), 40)
        self.assertGreater(sum(len(b["listings"]) for b in L4), 1200)

    def test_every_verdict_present_and_controls_clean(self):
        verdicts = Counter(c["verdict"] for k in L4K for c in k["citations"])
        self.assertEqual(set(verdicts), {"correct", *seo_local.FAULTS})
        clean = [k for k in L4K if all(c["verdict"] == "correct" for c in k["citations"])]
        self.assertGreaterEqual(len(clean), 20)

    def test_formatting_only_traps_are_present_on_correct_listings(self):
        """St. vs Street, suite styles, phone punctuation, toll-free, case, website forms: not faults."""
        k_by = {k["business_id"]: {c["citation_id"]: c["verdict"] for c in k["citations"]} for k in L4K}
        seen = set()
        for b in L4:
            c = b["canonical"]
            for li in b["listings"]:
                if k_by[b["business_id"]][li["citation_id"]] != "correct":
                    continue
                if c["street_type_abbrev"] + "." in li["street"]:
                    seen.add("street_abbrev")
                if any(x in li["street"] for x in ("Ste ", "Ste. ", "#", "Unit ")):
                    seen.add("suite_format")
                if li["phone"] != f"({c['phone'][2:5]}) {c['phone'][5:8]}-{c['phone'][8:]}":
                    seen.add("phone_format_or_toll_free")
                if li["name"].isupper():
                    seen.add("case")
                if li["website"] != c["website"]:
                    seen.add("website_form")
        self.assertEqual(seen, {"street_abbrev", "suite_format", "phone_format_or_toll_free", "case", "website_form"})

    def test_key_is_proven_and_can_fail(self):
        self.assertEqual(oracle.check_l4(L4, L4K), [])
        # a correct listing given a number that is neither the main line nor the toll-free must be caught
        bad = copy.deepcopy(L4)
        b0 = bad[0]
        k0 = {c["citation_id"]: c["verdict"] for c in L4K[0]["citations"]}
        target = next(li for li in b0["listings"] if k0[li["citation_id"]] == "correct")
        own = {oracle.norm_phone(b0["canonical"]["phone"]), oracle.norm_phone(b0["canonical"]["toll_free"] or "")}
        target["phone"] = next(p for p in ("(213) 555-0198", "(213) 555-0197") if oracle.norm_phone(p) not in own)
        self.assertTrue(oracle.check_l4(bad, L4K))
        bad = copy.deepcopy(L4)
        next(li for li in bad[0]["listings"] if k0[li["citation_id"]] == "correct")["status"] = "permanently_closed"
        self.assertTrue(oracle.check_l4(bad, L4K))

    def test_all_fictional(self):
        for b in L4:
            self.assertTrue(common.is_fixture_phone(b["canonical"]["phone"]))
            for li in b["listings"]:
                host = li["url"].split("/")[2]
                self.assertTrue(common.is_fixture_domain(host), host)
                self.assertTrue(common.is_fixture_domain(li["website"].split("/")[2]))


class DuplicatePairs(unittest.TestCase):
    def test_size_and_kinds(self):
        self.assertEqual(len(L5), 200)
        kinds = Counter(k["kind"] for k in L5K)
        self.assertEqual(set(kinds), {k for k, _d, _n in seo_local.L5_KINDS})
        self.assertGreaterEqual(sum(not k["is_duplicate"] for k in L5K), 100)   # NOT-a-duplicate traps

    def test_removals_need_the_owner(self):
        for k in L5K:
            self.assertEqual(k["requires_owner_approval"], k["is_duplicate"])

    def test_key_is_proven(self):
        self.assertEqual(oracle.check_l5(L5, L5K), [])


if __name__ == "__main__":
    unittest.main()
