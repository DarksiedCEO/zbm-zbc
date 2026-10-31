"""Self-test of devtools/osv_git_audit.py (red team RTA-8, fix wave A). No network: ``ask`` is replaced."""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("osv_git_audit", HERE / "osv_git_audit.py")
osv = importlib.util.module_from_spec(spec)
sys.modules["osv_git_audit"] = osv
spec.loader.exec_module(osv)

DLV = HERE.parent / "services" / "delivery-py" / "pyproject.toml"


class OsvGitAudit(unittest.TestCase):
    def test_finds_the_deer_flow_commit_pinned_by_delivery(self):
        pins = osv.git_pins(DLV)
        self.assertTrue(pins)
        self.assertTrue(all(url == "https://github.com/bytedance/deer-flow.git" for _, url, _ in pins))
        self.assertEqual(len({rev for _, _, rev in pins}), 1)
        self.assertEqual(osv.query(pins[0][2]), {"commit": pins[0][2]})

    def test_a_reported_advisory_fails_and_none_passes(self):
        real = osv.ask
        try:
            osv.ask = lambda commit, timeout=30.0: [{"id": "GHSA-test-0000", "summary": "planted"}]
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(osv.main([str(DLV)]), 1)
            self.assertIn("GHSA-test-0000", out.getvalue())
            osv.ask = lambda commit, timeout=30.0: []
            with redirect_stdout(io.StringIO()):
                self.assertEqual(osv.main([str(DLV)]), 0)
        finally:
            osv.ask = real

    def test_a_branch_or_tag_pin_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "pyproject.toml"
            p.write_text('[tool.uv.sources]\nx = { git = "https://example.invalid/x.git", rev = "main" }\n')
            with self.assertRaises(SystemExit):
                osv.git_pins(p)


if __name__ == "__main__":
    unittest.main()
