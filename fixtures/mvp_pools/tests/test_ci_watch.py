"""CI-1 watch corpus: meaningful changes are reported, noise never is."""

from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poolgen import ci_watch, ci_watch_oracle, common

SNAPS, KEY = ci_watch.generate("dev-a")


class WatchCorpus(unittest.TestCase):
    def test_size_and_every_change_type(self):
        self.assertEqual(len({s["page_id"] for s in SNAPS}), 30)
        self.assertEqual(len(SNAPS), 300)
        types = Counter(c["type"] for k in KEY for c in k["meaningful_changes"])
        self.assertEqual(set(types), set(ci_watch.CHANGES))

    def test_noise_only_versions_still_change_the_html(self):
        """A naive text diff would alert on these; the right answer is silence."""
        prev, silent_but_changed = {}, 0
        for s, k in zip(SNAPS, KEY):
            if s["page_id"] in prev and not k["meaningful_changes"] and prev[s["page_id"]] != s["html"]:
                silent_but_changed += 1
            prev[s["page_id"]] = s["html"]
        self.assertGreater(silent_but_changed, 150)

    def test_noise_sources_present(self):
        html = " ".join(s["html"] for s in SNAPS)
        for marker in ("utm_source=", "cookie-banner", "csrf-token", "ab-headline", "Updated 2026-11-", "testimonials"):
            self.assertIn(marker, html)

    def test_first_version_has_no_change(self):
        self.assertTrue(all(not k["meaningful_changes"] for k in KEY if k["version"] == 0))

    def test_key_is_proven(self):
        self.assertEqual(ci_watch_oracle.check(SNAPS, KEY), [])

    def test_hosts_are_fictional(self):
        for s in SNAPS:
            self.assertTrue(common.is_fixture_domain(s["url"].split("/")[2]))


if __name__ == "__main__":
    unittest.main()
