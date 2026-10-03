"""Static check of the Docker builds in .github/workflows/ci.yml (fix wave 26a, W26-3). Standard library only; CI runs
it in the `hygiene-static` job:

    python3 -B -m unittest devtools/test_ci_docker_args.py -v

CI #2 (run 37043738804) failed `delivery-docker-live` at "Build the sandbox image": `docker/sandbox.Dockerfile`
declares `ARG NODE_SHA256` with no default and requires it (`test -n "${NODE_SHA256}"`), and the workflow passed
only `--build-arg BASE_DIGEST`. No Docker daemon is needed to see that, so this test reads the files:

- every `docker build -f <Dockerfile>` in the workflow passes a `--build-arg` for every ARG that Dockerfile declares
  WITHOUT a default, and passes no `--build-arg` the Dockerfile does not declare (a typo would be silently unused);
- the pinned values the workflow passes are well-formed and are the ones recorded in ADR 0011's "Pinned hashes"
  table (the Node tarball sha256 for the Node version passed, the recorded python:3.12-slim base digest), and the
  Node version passed is the Dockerfile's default (the one the Dockerfile comment documents).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CI = REPO / ".github" / "workflows" / "ci.yml"
ADR = REPO / "docs" / "adr" / "0011-delivery-department-architecture.md"


def _logical_lines(text: str) -> list[str]:
    """Join backslash-continued lines (Dockerfile and shell alike)."""
    out, cur = [], ""
    for line in text.splitlines():
        if line.rstrip().endswith("\\"):
            cur += line.rstrip()[:-1] + " "
            continue
        out.append(cur + line)
        cur = ""
    if cur:
        out.append(cur)
    return out


def dockerfile_args(path: Path) -> dict[str, str | None]:
    """ARG name -> default (None when declared without one). Comments and continuations handled."""
    args: dict[str, str | None] = {}
    for line in _logical_lines(path.read_text()):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"(?i)^ARG\s+(.*)$", s)
        if not m:
            continue
        for tok in m.group(1).split():
            name, eq, default = tok.partition("=")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise AssertionError(f"{path}: cannot parse ARG token {tok!r}")
            # a later declaration with a default does not remove the requirement of an earlier one without (fix
            # wave 26b, R26-4: it did — `args[name] = default` overwrote the None)
            if name in args and args[name] is None:
                continue
            args[name] = default.strip("\"'") if eq else args.get(name)
    return args


def workflow_builds(text: str) -> list[tuple[str, dict[str, str]]]:
    """(Dockerfile path, {build-arg name: value as written}) for every `docker build -f ...` in the workflow."""
    builds = []
    for line in _logical_lines(text):
        if not re.search(r"\bdocker\s+(?:buildx\s+)?build\s", line) or line.lstrip().startswith("#"):
            continue
        f = re.search(r"\s(?:-f|--file)[ =](\S+)", line)
        assert f, f"docker build without -f in ci.yml: {line.strip()}"
        passed = {m.group(1): m.group(2) for m in re.finditer(r"--build-arg[ =]([A-Za-z_]\w*)=(\S*)", line)}
        builds.append((f.group(1).strip("\"'"), passed))
    return builds


def workflow_env(text: str, name: str) -> list[str]:
    return [m.group(1) for m in re.finditer(rf"^\s*{name}:\s*\"?([^\"\s#]+)\"?", text, re.M)]


class DockerBuildArgs(unittest.TestCase):
    def setUp(self):
        self.ci = CI.read_text()
        self.builds = workflow_builds(self.ci)

    def test_the_workflow_builds_the_sandbox_image(self):
        self.assertIn("services/delivery-py/docker/sandbox.Dockerfile", [b[0] for b in self.builds])

    def test_every_required_arg_is_passed_and_every_passed_arg_is_declared(self):
        for dockerfile, passed in self.builds:
            declared = dockerfile_args(REPO / dockerfile)
            required = sorted(n for n, d in declared.items() if d is None)
            missing = [n for n in required if n not in passed]
            self.assertEqual(missing, [], f"ci.yml builds {dockerfile} without --build-arg for {missing} "
                                          f"(declared there with no default)")
            unknown = sorted(n for n in passed if n not in declared)
            self.assertEqual(unknown, [], f"ci.yml passes --build-arg {unknown} that {dockerfile} does not declare")
            for n in passed:
                self.assertTrue(passed[n], f"ci.yml passes an empty --build-arg {n} to {dockerfile}")

    def test_the_pinned_values_are_the_ones_adr_0011_records(self):
        adr = ADR.read_text()
        dockerfile = REPO / "services/delivery-py/docker/sandbox.Dockerfile"
        declared = dockerfile_args(dockerfile)
        versions, shas = workflow_env(self.ci, "NODE_VERSION"), workflow_env(self.ci, "NODE_SHA256")
        self.assertEqual(len(versions), 1, f"ci.yml must set NODE_VERSION exactly once: {versions}")
        self.assertEqual(len(shas), 1, f"ci.yml must set NODE_SHA256 exactly once: {shas}")
        self.assertEqual(versions[0], declared.get("NODE_VERSION"),
                         "ci.yml's NODE_VERSION differs from sandbox.Dockerfile's default")
        self.assertRegex(shas[0], r"^[0-9a-f]{64}$")
        row = [ln for ln in adr.splitlines() if f"node-v{versions[0]}-linux-x64.tar.xz" in ln and shas[0] in ln]
        self.assertTrue(row, f"ADR 0011 has no pin-table row for node-v{versions[0]}-linux-x64.tar.xz = {shas[0]}")
        recorded = workflow_env(self.ci, "RECORDED_BASE_DIGEST")
        self.assertEqual(len(recorded), 1, f"ci.yml must set RECORDED_BASE_DIGEST exactly once: {recorded}")
        self.assertRegex(recorded[0], r"^[0-9a-f]{64}$")
        row = [ln for ln in adr.splitlines() if "python:3.12-slim" in ln and recorded[0] in ln]
        self.assertTrue(row, f"ADR 0011 has no pin-table row for python:3.12-slim = {recorded[0]}")


    def test_the_toolchain_pins_are_the_ones_adr_0011_records(self):
        # fix wave 26b (W26-3b): the Go tarball and the rustup installer are pinned like the Node tarball — a
        # required sha256 build argument, the value set once in ci.yml and recorded in ADR 0011's table; the Rust
        # toolchain is a concrete version, never the floating `stable`
        adr = ADR.read_text()
        declared = dockerfile_args(REPO / "services/delivery-py/docker/sandbox.Dockerfile")
        for ver_name, sha_name, artefact in (("GO_VERSION", "GO_SHA256", "go{v}.linux-amd64.tar.gz"),
                                             ("RUSTUP_VERSION", "RUSTUP_INIT_SHA256",
                                              "rustup-init {v} (x86_64-unknown-linux-gnu)")):
            versions, shas = workflow_env(self.ci, ver_name), workflow_env(self.ci, sha_name)
            self.assertEqual(len(versions), 1, f"ci.yml must set {ver_name} exactly once: {versions}")
            self.assertEqual(len(shas), 1, f"ci.yml must set {sha_name} exactly once: {shas}")
            self.assertEqual(versions[0], declared.get(ver_name), f"ci.yml's {ver_name} differs from the Dockerfile's")
            self.assertIn(sha_name, declared)
            self.assertIsNone(declared[sha_name], f"{sha_name} must be a required build argument (no default)")
            self.assertRegex(shas[0], r"^[0-9a-f]{64}$")
            name = artefact.format(v=versions[0])
            self.assertTrue([ln for ln in adr.splitlines() if name in ln and shas[0] in ln],
                            f"ADR 0011 has no pin-table row for {name} = {shas[0]}")
        rust = workflow_env(self.ci, "RUST_TOOLCHAIN")
        self.assertEqual(len(rust), 1, f"ci.yml must set RUST_TOOLCHAIN exactly once: {rust}")
        self.assertRegex(rust[0], r"^\d+\.\d+\.\d+$", "the sandbox's Rust toolchain must be a concrete version")
        self.assertEqual(rust[0], declared.get("RUST_TOOLCHAIN"))
        self.assertTrue([ln for ln in adr.splitlines() if "sandbox Rust toolchain" in ln and rust[0] in ln],
                        f"ADR 0011 has no row for the sandbox Rust toolchain {rust[0]}")

    def test_every_download_in_the_sandbox_dockerfile_is_checksummed(self):
        # W26-3b: nothing fetched at build time is run or unpacked unverified (no `curl … | sh`, no `curl … | tar`)
        text = (REPO / "services/delivery-py/docker/sandbox.Dockerfile").read_text()
        for line in _logical_lines(text):
            if line.lstrip().startswith("#") or not re.search(r"\bcurl\s+-", line):
                continue                      # (the apt package named curl is not a download)
            self.assertNotRegex(line, r"curl [^&]*\|", f"a download piped straight into a program: {line.strip()}")
            self.assertIn("sha256sum -c", line, f"a download with no checksum check: {line.strip()}")

    def test_the_base_digest_step_never_builds_from_an_unrecorded_digest(self):
        # R26-4 (AEGIS r26): the DLV_SANDBOX_BASE_DIGEST repository variable was used without being compared with
        # RECORDED_BASE_DIGEST, and without it the build took whatever python:3.12-slim was current. The step's own
        # shell is run here with a stub `docker`: the digest it outputs is always the recorded one; a variable that
        # differs fails the step.
        import os
        import subprocess
        import tempfile
        script = workflow_step_script(self.ci, "Resolve the python:3.12-slim base digest")
        recorded = workflow_env(self.ci, "RECORDED_BASE_DIGEST")[0]
        other = "f" * 64
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "docker"
            stub.write_text(f"#!/bin/sh\necho sha256:{other}\n")
            stub.chmod(0o755)
            out = Path(d) / "out"

            def run(pinned):
                out.write_text("")
                env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}", GITHUB_OUTPUT=str(out),
                           RECORDED_BASE_DIGEST=recorded, PINNED=pinned)
                r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
                return r.returncode, out.read_text(), r.stdout + r.stderr

            for pinned in ("", recorded):
                rc, got, log = run(pinned)
                self.assertEqual(rc, 0, log)
                self.assertEqual(got.strip(), f"digest={recorded}", (pinned, log))
            for pinned in (other, "not-hex"):
                rc, got, log = run(pinned)
                self.assertNotEqual(rc, 0, (pinned, log))
                self.assertNotIn("digest=", got)


def workflow_step_script(text: str, step_name: str) -> str:
    """The `run: |` block of the step named `step_name` (by indentation; standard library only)."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(rf"^\s*- name: {re.escape(step_name)}\s*$", ln))
    run_at = next(i for i in range(start + 1, len(lines)) if re.match(r"^\s*run: \|\s*$", lines[i]))
    body = []
    indent = None
    for ln in lines[run_at + 1:]:
        if not ln.strip():
            body.append("")
            continue
        cur = len(ln) - len(ln.lstrip())
        if indent is None:
            indent = cur
        if cur < indent:
            break
        body.append(ln[indent:])
    return "\n".join(body) + "\n"


class Parser(unittest.TestCase):
    """The parsers themselves, on planted text (so a parser that sees nothing cannot pass the checks above)."""

    def test_dockerfile_args(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Dockerfile"
            p.write_text("ARG A\n# ARG COMMENTED\nFROM x@sha256:${A}\nARG B=1 C\nARG D=\"2\"\nRUN echo \\\n  ARG E\n")
            self.assertEqual(dockerfile_args(p), {"A": None, "B": "1", "C": None, "D": "2"})

    def test_a_later_default_does_not_remove_an_earlier_requirement(self):
        # R26-4 (AEGIS r26): `ARG A` then `ARG A=1` dropped A's requirement — the opposite of the parser's comment
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Dockerfile"
            p.write_text("ARG A\nFROM x\nARG A=1\nARG B=2\nARG B\n")
            self.assertEqual(dockerfile_args(p), {"A": None, "B": "2"})

    def test_workflow_step_script(self):
        text = ("    steps:\n      - name: First\n        run: |\n          echo one\n          if x; then\n"
                "            echo two\n          fi\n      - name: Second\n        run: echo three\n")
        self.assertEqual(workflow_step_script(text, "First"), "echo one\nif x; then\n  echo two\nfi\n")

    def test_workflow_builds(self):
        text = ("      - run: |\n          docker build -f a/Dockerfile \\\n            --build-arg X=\"$X\" "
                "--build-arg Y=1 -t t .\n          # docker build -f ignored\n"
                "          d=$(docker buildx imagetools inspect python:3.12-slim)\n")
        self.assertEqual(workflow_builds(text), [("a/Dockerfile", {"X": '"$X"', "Y": "1"})])


if __name__ == "__main__":
    unittest.main()
