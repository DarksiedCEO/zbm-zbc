"""Self-test of devtools/check_docker_pins.py (red team RTA-6, fix wave A)."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("check_docker_pins", HERE / "check_docker_pins.py")
pins = importlib.util.module_from_spec(spec)
sys.modules["check_docker_pins"] = pins
spec.loader.exec_module(pins)

DIGEST = "a" * 64


class Pins(unittest.TestCase):
    def test_tag_only_is_refused(self):
        self.assertEqual(pins.unpinned("FROM golang:1.27.3-bookworm AS build\n"), ["golang:1.27.3-bookworm"])
        self.assertEqual(pins.unpinned("FROM rust:1.99.1-slim-bookworm AS build\n"), ["rust:1.99.1-slim-bookworm"])
        self.assertEqual(pins.unpinned("FROM python\n"), ["python"])

    def test_digest_pins_stage_references_and_build_args_pass(self):
        text = (f"FROM golang:1.27.3-bookworm@sha256:{DIGEST} AS build\nRUN go build\n"
                f"FROM python:3.12-slim@sha256:{DIGEST}\nCOPY --from=build /x /x\n"
                "FROM build AS again\nFROM scratch\nFROM python:3.12-slim@sha256:${BASE_DIGEST}\n")
        self.assertEqual(pins.unpinned(text), [])

    def test_a_short_or_uppercase_digest_is_refused(self):
        self.assertTrue(pins.unpinned("FROM python:3.12-slim@sha256:abc\n"))
        self.assertTrue(pins.unpinned(f"FROM python:3.12-slim@sha256:{'A' * 64}\n"))

    def test_the_repository_dockerfiles_are_all_found(self):
        names = {str(p.relative_to(pins.REPO)) for p in pins.dockerfiles()}
        for want in ("services/ledger-rust/Dockerfile", "services/orchestrator-go/Dockerfile.stripe-gateway",
                     "services/orchestrator-go/Dockerfile.orchestrator", "services/finance-py/Dockerfile"):
            self.assertIn(want, names)

    def test_dependabot_watches_the_docker_base_images(self):
        text = (pins.REPO / ".github" / "dependabot.yml").read_text()
        self.assertIn('package-ecosystem: "docker"', text)
        for d in ("/services/ledger-rust", "/services/orchestrator-go"):
            self.assertIn(f'"{d}"', text)


if __name__ == "__main__":
    unittest.main()
