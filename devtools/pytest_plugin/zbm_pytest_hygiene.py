"""pytest plugin for the repo-wide hygiene check (fix wave 25, founder ruling R-HYGIENE / H9).

Loaded by ``devtools/hygiene_check.py run`` for every Python suite (``-p zbm_pytest_hygiene``; the wrapper puts this
directory on the interpreter's sys.path itself, never on PYTHONPATH, so nothing the tests start inherits it). It can
also be used by hand from a service directory::

    python -c "import sys; sys.path.insert(0, '../../devtools/pytest_plugin'); import pytest; \\
               sys.exit(pytest.main(['-q', '-p', 'zbm_pytest_hygiene']))"

What it does (stdlib + pytest only):
  - records how many tests were collected and every test's outcome, and the reason of every skip/xfail, and writes
    them as JSON to ``$ZBM_HYGIENE_RESULTS`` when that is set (the wrapper compares the count with
    docs/test-counts.md and the skip reasons with the suite's allowlist);
  - asks pytest to keep NO tmp_path directories after the session (``tmp_path_retention_policy = none``): the suite's
    private TMPDIR must be empty at the end, and pytest's own retention (the last three basetemps) would otherwise
    fill it by design;
  - when run WITHOUT the wrapper (no ``$ZBM_HYGIENE_RESULTS``), fails the session itself if ``git status
    --porcelain`` changed during the run or a child process of this pytest process is still alive at the end.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_STATE: dict = {}


def _git_status(cwd: str) -> str | None:
    try:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _live_children(pid: int) -> list[int]:
    """Linux: every live descendant of ``pid`` (from /proc/<p>/task/<t>/children); elsewhere: []."""
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        task_dir = Path(f"/proc/{p}/task")
        if not task_dir.is_dir():
            continue
        for t in task_dir.iterdir():
            try:
                kids = (t / "children").read_text().split()
            except OSError:
                continue
            for k in kids:
                k = int(k)
                try:
                    state = Path(f"/proc/{k}/stat").read_text().rsplit(")", 1)[1].split()[0]
                except (OSError, IndexError):
                    continue
                if state != "Z":
                    out.append(k)
                todo.append(k)
    return out


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    try:
        config._inicache["tmp_path_retention_policy"] = "none"   # noqa: SLF001 — pytest has no public setter
    except Exception:  # pragma: no cover - very old pytest
        pass
    _STATE.update(collected=0, outcomes={}, skips=[], standalone=not os.environ.get("ZBM_HYGIENE_RESULTS"),
                  git_before=None)
    if _STATE["standalone"]:
        _STATE["git_before"] = _git_status(str(config.rootpath))


def pytest_collection_finish(session):
    _STATE["collected"] = len(session.items)


def pytest_runtest_logreport(report):
    outcomes = _STATE["outcomes"]
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed") or \
            (report.when == "teardown" and report.outcome == "failed"):
        key = report.nodeid
        if report.skipped:
            reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
            reason = reason[len("Skipped: "):] if reason.startswith("Skipped: ") else reason
            if hasattr(report, "wasxfail"):
                outcomes[key] = "xfailed"
                _STATE["skips"].append({"nodeid": key, "kind": "xfail", "reason": str(report.wasxfail)})
            else:
                outcomes[key] = "skipped"
                _STATE["skips"].append({"nodeid": key, "kind": "skip", "reason": str(reason)})
        elif report.failed:
            outcomes[key] = "failed"
        elif key not in outcomes:
            outcomes[key] = "xpassed" if hasattr(report, "wasxfail") else "passed"


def pytest_sessionfinish(session, exitstatus):
    counts: dict[str, int] = {}
    for o in _STATE["outcomes"].values():
        counts[o] = counts.get(o, 0) + 1
    result = {"collected": _STATE["collected"], "counts": counts, "skips": _STATE["skips"],
              "exitstatus": int(exitstatus)}
    path = os.environ.get("ZBM_HYGIENE_RESULTS")
    if path:
        Path(path).write_text(json.dumps(result, indent=1, sort_keys=True))
        return
    problems = []
    after = _git_status(str(session.config.rootpath))
    if _STATE["git_before"] is not None and after is not None and after != _STATE["git_before"]:
        problems.append(f"git status --porcelain changed during the run:\n{after}")
    kids = _live_children(os.getpid())
    if kids:
        problems.append(f"child processes of this pytest run still alive at the end: {kids}")
    if problems:
        for p in problems:
            session.config.get_terminal_writer().line(f"HYGIENE: {p}", red=True)
        session.exitstatus = 3


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    """With ``tmp_path_retention_policy = none`` pytest removes every basetemp it made but leaves their (now empty)
    parent, ``$TMPDIR/pytest-of-<user>``. Remove it when — and only when — it is empty (``os.rmdir``): anything still
    inside is left for rule R3 to report."""
    import getpass
    import tempfile
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "unknown"
    root = Path(tempfile.gettempdir()) / f"pytest-of-{user}"
    try:
        os.rmdir(root)
    except OSError:
        pass
