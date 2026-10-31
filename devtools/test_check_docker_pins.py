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

    def test_copy_from_and_mount_from_images_are_checked(self):
        """AEGIS wave A F-4: COPY --from=<image> and RUN --mount=from=<image> pull images too."""
        self.assertEqual(pins.unpinned("FROM python@sha256:" + DIGEST + "\nCOPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uv\n"),
                         ["ghcr.io/astral-sh/uv:0.8.17"])
        self.assertEqual(pins.unpinned("FROM python@sha256:" + DIGEST + "\nRUN --mount=type=bind,from=alpine:3,target=/a true\n"),
                         ["alpine:3"])
        self.assertEqual(pins.unpinned("FROM python@sha256:" + DIGEST + "\nCOPY --chown=1:1 --from=alpine:3 /a /a\n"),
                         ["alpine:3"])
        ok = (f"FROM golang@sha256:{DIGEST} AS build\nFROM python@sha256:{DIGEST}\nCOPY --from=build /x /x\n"
              f"COPY --from=0 /y /y\nCOPY --from=ghcr.io/astral-sh/uv:0.8.17@sha256:{DIGEST} /uv /uv\n"
              f"RUN --mount=type=cache,target=/c \\\n    --mount=from=build,target=/b true\n")
        self.assertEqual(pins.unpinned(ok), [])

    def test_onbuild_escape_and_syntax_directives(self):
        """AEGIS round 2 N-9: `ONBUILD COPY --from=<image>` and a `# escape=` directive with backtick continuations were
        not detected; a `# syntax=<image>` directive pulls the BuildKit frontend image (also a base image)."""
        base = f"FROM python@sha256:{DIGEST}\n"
        self.assertEqual(pins.unpinned(base + "ONBUILD COPY --from=alpine:3 /a /a\n"), ["alpine:3"])
        self.assertEqual(pins.unpinned(base + "onbuild RUN --mount=from=alpine:3,target=/a true\n"), ["alpine:3"])
        text = "# escape=`\n" + base + "COPY `\n  --from=alpine:3 /a /a\n"
        self.assertEqual(pins.unpinned(text), ["alpine:3"])
        text = "# escape=`\nFROM `\n  golang:1.27\n"
        self.assertEqual(pins.unpinned(text), ["golang:1.27"])
        # a backslash is NOT a continuation under escape=` (and the directive only counts at the very top)
        self.assertEqual(pins.unpinned("RUN true\n# escape=`\n" + base), [])
        self.assertEqual(pins.unpinned("# syntax=docker/dockerfile:1.7\n" + base), ["docker/dockerfile:1.7"])
        self.assertEqual(pins.unpinned(f"# syntax=docker/dockerfile:1.7@sha256:{DIGEST}\n" + base), [])
        self.assertEqual(pins.unpinned("#syntax = docker/dockerfile:1\n" + base), ["docker/dockerfile:1"])
        # a syntax line after the first instruction is a plain comment
        self.assertEqual(pins.unpinned(base + "# syntax=docker/dockerfile:1\n"), [])

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
