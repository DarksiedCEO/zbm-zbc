"""devtools/client_offboarding.py (ADR 0027 decision 3; MSA 15.5): the dry run across services, then the real run in
the right order (Compliance first, Finance last with Compliance's certificate hash, so the certificate on the ledger
covers both), refusing a loose config file and never printing a token. No network: the HTTP call is injected.

    python3 -B -m unittest devtools/test_client_offboarding.py -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("client_offboarding_under_test", HERE / "client_offboarding.py")
OFF = importlib.util.module_from_spec(spec)
spec.loader.exec_module(OFF)

FIN_TOKEN = "Bearer " + "f" * 64
CMP_TOKEN = "Bearer " + "c" * 64
ANDRE = "a" * 64
CERT_C = {"service": "compliance", "certificate_version": 1}
CERT_F = {"service": "finance", "certificate_version": 1}


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class Fake:
    def __init__(self):
        self.calls = []
        self.fail = {}

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body) if body else None))
        if url in self.fail:
            return self.fail[url]
        if url.endswith("/seo/v1/tenants"):
            return 200, [{"tenant_id": "ten-1", "finance_client_id": "acme-co", "domains": ["acme.example"],
                          "gsc_property": "sc-domain:acme.example", "audits": 3},
                         {"tenant_id": "ten-2", "finance_client_id": "other", "domains": ["o.example"], "audits": 1}]
        b = json.loads(body)
        svc = "compliance" if "/compliance/" in url else "finance"
        if b["dry_run"]:
            return 200, {"dry_run": True, "plan_sha256": svc[0] * 64, "blockers": [], "warnings": [],
                         "held": [{"kind": "x", "count": 1, "action": "keep_7_years", "retention": "7 years (test)"},
                                  {"kind": "y", "count": 2, "action": "keep_10_years",
                                   "retention": "10 years (test)"}]}
        cert = CERT_C if svc == "compliance" else CERT_F
        return 200, {"certificate": cert, "certificate_sha256": OFF.sha(cert)}


class ClientOffboarding(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="offb-test-")
        os.chmod(self.dir, 0o700)
        hdrs = {}
        for name, value in (("fin-auth", f"Authorization: {FIN_TOKEN}"), ("fin-andre", f"X-Andre-Approval-Token: {ANDRE}"),
                            ("cmp-auth", f"Authorization: {CMP_TOKEN}"), ("cmp-andre", f"X-Andre-Approval-Token: {ANDRE}"),
                            ("seo-auth", "Authorization: Bearer " + "s" * 64),
                            ("seo-dash", "X-SEO-Caller-Token: " + "d" * 64)):
            p = os.path.join(self.dir, name + ".hdr")
            with open(p, "w") as fh:
                fh.write(value + "\n")
            os.chmod(p, 0o600)
            hdrs[name] = p
        self.cfg = os.path.join(self.dir, "offboarding.json")
        with open(self.cfg, "w") as fh:
            json.dump({"finance": {"url": "http://127.0.0.1:18410", "header_files": [hdrs["fin-auth"], hdrs["fin-andre"]]},
                       "compliance": {"url": "http://127.0.0.1:18380",
                                      "header_files": [hdrs["cmp-auth"], hdrs["cmp-andre"]]},
                       "seo": {"url": "http://127.0.0.1:18500", "header_files": [hdrs["seo-auth"], hdrs["seo-dash"]]}},
                      fh)
        os.chmod(self.cfg, 0o600)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _plan(self, fake, client="acme-co"):
        out = io.StringIO()
        path = os.path.join(self.dir, "plan.json")
        code = OFF.main(["plan", "--config", self.cfg, "--client", client, "--out", path], http=fake, out=out)
        return code, path, out.getvalue()

    def test_plan_dry_runs_every_service_and_writes_a_private_plan(self):
        fake = Fake()
        code, path, text = self._plan(fake)
        self.assertEqual(code, 0, text)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        plan = _load(path)
        self.assertEqual(plan["services"]["finance"]["plan_sha256"], "f" * 64)
        self.assertEqual(plan["services"]["compliance"]["plan_sha256"], "c" * 64)
        seo = plan["services"]["seo"]
        self.assertEqual([t["tenant_id"] for t in seo["tenants"]], ["ten-1"])
        self.assertIn("detection", plan["manual"])
        self.assertIn("delivery", plan["manual"])
        for m, url, _h, body in fake.calls:
            if m == "POST":
                self.assertIs(body["dry_run"], True)
        self.assertIn("keep_10_years  10 years (test)", text)
        self.assertIn("keep_7_years  7 years (test)", text)
        for secret in ("f" * 64, "c" * 64 + "\n", ANDRE, "s" * 64, "d" * 64):
            self.assertNotIn(secret, text)

    def test_run_goes_compliance_first_then_finance_with_its_certificate(self):
        fake = Fake()
        self._plan(fake)
        fake.calls.clear()
        out = io.StringIO()
        cert_path = os.path.join(self.dir, "certificate.json")
        code = OFF.main(["run", "--config", self.cfg, "--plan", os.path.join(self.dir, "plan.json"), "--out",
                         cert_path], http=fake, out=out)
        self.assertEqual(code, 0, out.getvalue())
        posts = [(u, b) for m, u, _h, b in fake.calls if m == "POST"]
        self.assertEqual([u.split("/")[3] for u, _b in posts], ["compliance", "fin"])
        self.assertEqual(posts[0][1]["plan_sha256"], "c" * 64)
        self.assertEqual(posts[1][1]["peer_certificates"],
                         [{"service": "compliance", "certificate_sha256": OFF.sha(CERT_C)}])
        cert = _load(cert_path)
        self.assertEqual(cert["ledger_certificate_sha256"], OFF.sha(CERT_F))
        self.assertNotIn("acme-co", json.dumps(cert))
        self.assertTrue(cert["manual_steps_pending"])

    def test_a_refused_compliance_run_stops_before_finance(self):
        fake = Fake()
        self._plan(fake)
        fake.fail["http://127.0.0.1:18380/compliance/v1/clients/acme-co/offboarding"] = (409, {"detail": "blocked"})
        fake.calls.clear()
        out = io.StringIO()
        code = OFF.main(["run", "--config", self.cfg, "--plan", os.path.join(self.dir, "plan.json"), "--out",
                         os.path.join(self.dir, "c.json")], http=fake, out=out)
        self.assertEqual(code, 1)
        self.assertFalse([u for _m, u, _h, _b in fake.calls if "/fin/" in u])
        self.assertIn("compliance", out.getvalue())

    def test_a_plan_with_blockers_is_not_run(self):
        fake = Fake()
        orig = fake.__call__

        def blocked(method, url, headers, body):
            st, doc = orig(method, url, headers, body)
            if body and json.loads(body).get("dry_run") and "/fin/" in url:
                doc = {**doc, "blockers": ["1 Stripe dispute(s) still open"]}
            return st, doc
        out = io.StringIO()
        path = os.path.join(self.dir, "plan.json")
        self.assertEqual(OFF.main(["plan", "--config", self.cfg, "--client", "acme-co", "--out", path], http=blocked,
                                  out=out), 0)
        self.assertIn("BLOCKED", out.getvalue())
        out = io.StringIO()
        code = OFF.main(["run", "--config", self.cfg, "--plan", path, "--out", os.path.join(self.dir, "c.json")],
                        http=blocked, out=out)
        self.assertEqual(code, 1)

    def test_a_config_or_header_file_others_can_read_is_refused(self):
        os.chmod(self.cfg, 0o644)
        with self.assertRaises(SystemExit):
            OFF.main(["plan", "--config", self.cfg, "--client", "acme-co", "--out", os.path.join(self.dir, "p.json")],
                     http=Fake(), out=io.StringIO())

    def test_only_a_loopback_or_private_url_and_a_valid_client_id(self):
        with self.assertRaises(SystemExit):
            OFF.main(["plan", "--config", self.cfg, "--client", "acme co/../x", "--out",
                      os.path.join(self.dir, "p.json")], http=Fake(), out=io.StringIO())
        cfg = _load(self.cfg)
        cfg["finance"]["url"] = "http://evil.example:8410"
        with open(self.cfg, "w") as fh:
            json.dump(cfg, fh)
        with self.assertRaises(SystemExit):
            OFF.main(["plan", "--config", self.cfg, "--client", "acme-co", "--out", os.path.join(self.dir, "p.json")],
                     http=Fake(), out=io.StringIO())


if __name__ == "__main__":
    unittest.main()
