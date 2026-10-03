"""
Fix wave 26b (scout C6-2, C5-7): devtools/live_run.py made its work dir (ledger and service logs, the run's evidence)
with mkdtemp and never removed it, so every local run left one behind; and this script read FIN_LIVE_WORKDIR where the
other four read LIVE_WORK_DIR (C5-7): the old name still works for now, with a deprecation notice. Now LIVE_WORK_DIR=<dir>, the name
every live_run.py reads, KEEPS the run's dir inside <dir> and prints where it is (CI's live-runs job asks for
$RUNNER_TEMP/live-work, then lists and removes it); unset, the dir is made in the temp dir and removed when the run
ends, passed or failed.

The real script runs here with a LEDGER_BIN that does not exist (it fails at its first start, after making the work
dir) and port 0 for every port it takes (nothing fixed is bound); TMPDIR is a fresh directory of this test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

LIVE_RUN = Path(__file__).resolve().parents[1] / "devtools" / "live_run.py"
PREFIX = "finlive-"
PORTS = ",".join(["0"] * 4)


def _run(tmp_path: Path, **extra: str) -> tuple[subprocess.CompletedProcess, Path]:
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    env = {**os.environ, "TMPDIR": str(tmpdir), "LEDGER_BIN": str(tmp_path / "no-such-ledger"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("LIVE_WORK_DIR", "FIN_LIVE_WORKDIR"):
        env.pop(name, None)
    env.update(extra)
    r = subprocess.run([sys.executable, str(LIVE_RUN), "--ports", PORTS], cwd=LIVE_RUN.parents[1], env=env,
                       capture_output=True, text=True, timeout=120)
    return r, tmpdir


def _runs_in(d: Path) -> list[Path]:
    return sorted(p for p in d.iterdir() if p.name.startswith(PREFIX))


def test_a_run_without_live_work_dir_leaves_no_work_dir(tmp_path):
    r, tmpdir = _run(tmp_path)
    assert r.returncode != 0, r.stdout[-2000:]                     # the ledger binary does not exist
    assert "no-such-ledger" in r.stdout + r.stderr, r.stderr[-2000:]  # ... and that is what failed
    assert _runs_in(tmpdir) == [], (_runs_in(tmpdir), r.stdout[-2000:])
    assert "removed" in r.stdout and "LIVE_WORK_DIR" in r.stdout, r.stdout[-2000:]


def test_live_work_dir_keeps_the_run_dir_there_and_says_where(tmp_path):
    keep = tmp_path / "live-work"
    keep.mkdir()
    r, tmpdir = _run(tmp_path, LIVE_WORK_DIR=str(keep))
    assert r.returncode != 0, r.stdout[-2000:]
    kept = _runs_in(keep)
    assert len(kept) == 1 and kept[0].is_dir(), (kept, r.stdout[-2000:])
    assert f"work dir kept (LIVE_WORK_DIR): {kept[0]}" in r.stdout, r.stdout[-2000:]
    assert _runs_in(tmpdir) == []


def test_the_old_name_fin_live_workdir_still_keeps_the_dir_and_says_it_is_deprecated(tmp_path):
    keep = tmp_path / "live-work"
    keep.mkdir()
    r, tmpdir = _run(tmp_path, FIN_LIVE_WORKDIR=str(keep))
    assert len(_runs_in(keep)) == 1 and _runs_in(tmpdir) == [], (r.stdout[-2000:], r.stderr[-2000:])
    assert "FIN_LIVE_WORKDIR is deprecated" in r.stderr and "LIVE_WORK_DIR" in r.stderr, r.stderr[-2000:]


def test_live_work_dir_wins_over_the_old_name(tmp_path):
    keep, old = tmp_path / "live-work", tmp_path / "old"
    keep.mkdir()
    old.mkdir()
    r, _ = _run(tmp_path, LIVE_WORK_DIR=str(keep), FIN_LIVE_WORKDIR=str(old))
    assert len(_runs_in(keep)) == 1 and _runs_in(old) == [], (r.stdout[-2000:], r.stderr[-2000:])
    assert "FIN_LIVE_WORKDIR is ignored" in r.stderr, r.stderr[-2000:]
