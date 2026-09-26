"""Fix wave 8 (Sep 24 2026) — AEGIS round-7 finding on onboarding-py.

N7-6 LOW  the auto-built ledger fixture (``ledger_rust_binary`` in
          conftest.py, fix wave 7) ran ``cargo build`` and then GUESSED the
          artifact at services/ledger-rust/target/release/server, ignoring
          CARGO_TARGET_DIR. With it set and no binary at the guessed path
          the suite failed with "cargo build of ledger-rust failed (exit
          0): ... Finished release" (the build had succeeded, elsewhere);
          with a stale binary at the guessed path the live tests silently
          ran against that stale binary. Now the artifact path comes from
          ``cargo build --message-format=json`` (cargo's own answer, so
          CARGO_TARGET_DIR, a ``.cargo/config`` ``build.target-dir`` and
          any future rule are honored); cargo runs unconditionally, so a
          binary older than the sources is always rebuilt; without cargo the
          binary is looked up where cargo would have put it and a stale one
          is a printed skip, never a silent run.

The tests drive the resolver in a fresh interpreter (like the suite's
own use) against a COPY of the ledger-rust crate in pytest's temp dir, so
a "stale binary at the default path" can be planted without touching the
real build. The copy's target dir is seeded from the real one (mtimes
preserved) so only the crate itself compiles (~10 s once per run).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import LEDGER_RUST_DIR, ledger_rust_binary

ROOT = Path(__file__).resolve().parents[1]
SRC_FILES = ("Cargo.toml", "Cargo.lock", "src")


def _resolve(crate: Path, env_over: dict[str, str], *, unset: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    """Run conftest.ledger_rust_binary() in a fresh interpreter, pointed at
    ``crate`` (the module globals the resolver reads), with ``env_over``
    applied and ``unset`` removed from the environment."""
    env = {k: v for k, v in os.environ.items() if k not in ("ONBOARDING_LEDGER_RUST_BIN", "CARGO_TARGET_DIR", *unset)}
    env.update(env_over)
    code = (
        "import conftest, pathlib\n"
        f"conftest.LEDGER_RUST_DIR = pathlib.Path({str(crate)!r})\n"
        "conftest.LEDGER_RUST_DEFAULT_BIN = conftest.LEDGER_RUST_DIR / 'target' / 'release' / 'server'\n"
        "print(conftest.ledger_rust_binary())\n"
    )
    return subprocess.run([sys.executable, "-c", code], cwd=str(ROOT / "tests"), env=env,
                          capture_output=True, text=True, timeout=1200)


@pytest.fixture(scope="module")
def crate_copy(tmp_path_factory) -> tuple[Path, Path]:
    """(copy of the ledger-rust crate, a CARGO_TARGET_DIR seeded from the real
    build). Both in pytest's temp dir, nothing under the checkout."""
    if not shutil.which("cargo"):
        pytest.skip("cargo is not on PATH: the N7-6 build-resolution tests need it")
    real_bin = ledger_rust_binary()  # ensures the real target dir is built (and warm)
    base = tmp_path_factory.mktemp("n7_6")
    crate = base / "ledger-rust"
    crate.mkdir()
    for name in SRC_FILES:
        src = LEDGER_RUST_DIR / name
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, crate / name)
    ctd = base / "ctd"
    real_target = real_bin.parents[1] if real_bin.name == "server" and real_bin.parent.name == "release" else None
    if real_target and (real_target / "release").is_dir():
        shutil.copytree(real_target, ctd, symlinks=True, copy_function=shutil.copy2)
        (ctd / "release" / "server").unlink(missing_ok=True)  # the artifact is what is under test
    return crate, ctd


# Native executable magic (fix wave 16: macOS builds Mach-O, not ELF). A
# shell script ("#!/b...") or any other file is still rejected.
_NATIVE_EXEC_MAGIC = (
    b"\x7fELF",  # ELF (Linux, BSD)
    b"\xcf\xfa\xed\xfe",  # Mach-O 64-bit, little-endian (MH_MAGIC_64 0xFEEDFACF): arm64 / x86_64 macOS
    b"\xce\xfa\xed\xfe",  # Mach-O 32-bit, little-endian (MH_MAGIC 0xFEEDFACE)
    b"\xca\xfe\xba\xbe",  # Mach-O universal / fat (FAT_MAGIC 0xCAFEBABE)
    b"\xca\xfe\xba\xbf",  # Mach-O universal / fat, 64-bit offsets (FAT_MAGIC_64)
)


def _is_ledger_server(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK) and p.read_bytes()[:4] in _NATIVE_EXEC_MAGIC



def test_is_ledger_server_accepts_elf_and_mach_o_and_rejects_scripts(tmp_path):
    """Fix wave 16: the artifact check ran on macOS against a Mach-O binary
    and rejected it (it only knew ELF). Native magics pass; a shell script,
    unknown bytes, or a non-executable file do not."""
    def planted(name: str, head: bytes, mode: int = 0o755) -> Path:
        f = tmp_path / name
        f.write_bytes(head + b"\0" * 60)
        f.chmod(mode)
        return f

    for name, head in [("elf", b"\x7fELF\x02\x01"), ("macho64", b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01"),
                       ("fat", b"\xca\xfe\xba\xbe\x00\x00\x00\x02")]:
        assert _is_ledger_server(planted(name, head)), name
    assert not _is_ledger_server(planted("script", b"#!/bin/sh\necho STALE\n"))
    assert not _is_ledger_server(planted("junk", b"MZ\x90\x00"))
    assert not _is_ledger_server(planted("noexec", b"\x7fELF\x02\x01", mode=0o644))
    assert not _is_ledger_server(tmp_path / "missing")

# --- CARGO_TARGET_DIR set, no binary at the crate's default path -------------


def test_n7_6_cargo_target_dir_set_no_default_binary_resolves_cargo_artifact(crate_copy):
    crate, ctd = crate_copy
    assert not (crate / "target").exists()
    r = _resolve(crate, {"CARGO_TARGET_DIR": str(ctd)})
    assert r.returncode == 0, f"resolver failed although cargo built fine:\n{r.stderr[-2000:]}"
    p = Path(r.stdout.strip())
    assert p == ctd / "release" / "server", (p, r.stderr[-1000:])
    assert _is_ledger_server(p)
    assert not (crate / "target").exists(), "cargo must not have been steered away from CARGO_TARGET_DIR"


# --- CARGO_TARGET_DIR set, a STALE binary at the crate's default path --------


def test_n7_6_cargo_target_dir_set_stale_default_binary_is_not_used(crate_copy):
    crate, ctd = crate_copy
    stale = crate / "target" / "release" / "server"
    stale.parent.mkdir(parents=True)
    stale.write_text("#!/bin/sh\necho STALE\n")
    stale.chmod(0o755)
    old = time.time() - 7 * 24 * 3600
    os.utime(stale, (old, old))
    try:
        r = _resolve(crate, {"CARGO_TARGET_DIR": str(ctd)})
        assert r.returncode == 0, r.stderr[-2000:]
        p = Path(r.stdout.strip())
        assert p == ctd / "release" / "server", f"resolved the stale planted binary: {p}"
        assert _is_ledger_server(p) and p.read_bytes()[:4] != b"#!/b"
    finally:
        shutil.rmtree(crate / "target", ignore_errors=True)


# --- sources newer than the binary -> rebuilt -------------------------------


def test_n7_6_binary_older_than_sources_is_rebuilt(crate_copy):
    crate, ctd = crate_copy
    before = _resolve(crate, {"CARGO_TARGET_DIR": str(ctd)})
    assert before.returncode == 0, before.stderr[-2000:]
    p = Path(before.stdout.strip())
    mtime0 = p.stat().st_mtime
    time.sleep(1.1)  # coarse-mtime filesystems
    lib = crate / "src" / "lib.rs"
    lib.write_text(lib.read_text() + "\n// n7-6: source touched after the build\n")
    after = _resolve(crate, {"CARGO_TARGET_DIR": str(ctd)})
    assert after.returncode == 0, after.stderr[-2000:]
    assert Path(after.stdout.strip()) == p
    assert p.stat().st_mtime > mtime0, "binary was not rebuilt after its sources changed"


# --- CARGO_TARGET_DIR unset -> the crate's default target dir ---------------


def test_n7_6_cargo_target_dir_unset_uses_the_crate_default(crate_copy):
    """Against the REAL crate (already built by the session fixture): with
    the variable removed the answer is services/ledger-rust/target/release/
    server, which exists and is the ledger server."""
    r = _resolve(LEDGER_RUST_DIR, {}, unset=("CARGO_TARGET_DIR",))
    assert r.returncode == 0, r.stderr[-2000:]
    p = Path(r.stdout.strip())
    assert p == LEDGER_RUST_DIR / "target" / "release" / "server"
    assert _is_ledger_server(p)


# --- the failure paths say what happened ------------------------------------


def test_n7_6_without_cargo_a_stale_binary_is_a_printed_skip_not_a_silent_run(crate_copy):
    """No cargo on PATH, CARGO_TARGET_DIR set, and the binary there is older
    than the sources: the resolver must not hand out the stale binary. It
    skips (a build tool is missing) with a reason naming the binary, the
    staleness and cargo."""
    crate, ctd = crate_copy
    binary = ctd / "release" / "server"
    if not binary.is_file():
        assert _resolve(crate, {"CARGO_TARGET_DIR": str(ctd)}).returncode == 0
    old = time.time() - 7 * 24 * 3600
    saved = binary.stat().st_mtime
    os.utime(binary, (old, old))
    try:
        r = _resolve(crate, {"CARGO_TARGET_DIR": str(ctd), "PATH": "/nonexistent"})
        assert r.returncode != 0, f"handed out a stale binary: {r.stdout}"
        assert "Skipped" in r.stderr and str(binary) in r.stderr and "older than" in r.stderr and "cargo" in r.stderr, r.stderr[-1500:]
    finally:
        os.utime(binary, (saved, saved))


def test_n7_6_without_cargo_a_fresh_binary_under_cargo_target_dir_is_used(crate_copy):
    crate, ctd = crate_copy
    binary = ctd / "release" / "server"
    if not binary.is_file():
        assert _resolve(crate, {"CARGO_TARGET_DIR": str(ctd)}).returncode == 0
    r = _resolve(crate, {"CARGO_TARGET_DIR": str(ctd), "PATH": "/nonexistent"})
    assert r.returncode == 0, r.stderr[-2000:]
    assert Path(r.stdout.strip()) == binary


def test_n7_6_build_failure_message_names_the_real_cause(crate_copy, tmp_path):
    """A crate that does not compile fails the run with cargo's error, not
    'exit 0 ... Finished'. (A broken copy, its own target dir.)"""
    crate, ctd = crate_copy
    broken = tmp_path / "broken"
    shutil.copytree(crate, broken, ignore=shutil.ignore_patterns("target"))
    (broken / "src" / "lib.rs").write_text("this is not rust\n")
    r = _resolve(broken, {"CARGO_TARGET_DIR": str(ctd)})
    assert r.returncode != 0
    assert "Failed" in r.stderr and "cargo build of ledger-rust failed" in r.stderr and "error" in r.stderr, r.stderr[-1500:]
    assert "exit 0" not in r.stderr
