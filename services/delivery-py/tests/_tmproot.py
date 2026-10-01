"""One private temp root per test session (fix wave 21, L4).

Imported first by conftest. Everything the suite, the product under test and pytest itself create through
``tempfile`` (harness dirs, engine trees, gitport's per-process dir, pytest's tmp_path base, child processes that
honour TMPDIR) lands under SESSION_TMP, which pytest_unconfigure removes — a full run leaves no new directory in the
host temp dir. SESSION_TMP is created under the host temp dir AS GIVEN (never realpath'd), so a symlinked TMPDIR
is still exercised as a symlink. ORIG_TMP is the host temp dir, for the few deliberate cross-session caches."""
import atexit
import os
import shutil
import tempfile

ORIG_TMP = tempfile.gettempdir()
SESSION_TMP = tempfile.mkdtemp(prefix="dlv-tests-", dir=ORIG_TMP)
tempfile.tempdir = SESSION_TMP
os.environ["TMPDIR"] = SESSION_TMP


def remove_session_root() -> None:
    shutil.rmtree(SESSION_TMP, ignore_errors=True)


# Fix wave 24 (E6, N23-D-9): a process that imports this outside pytest (fakes.py imports it, and the reviewers'
# probes import fakes through helpers) has no pytest_unconfigure: the root goes when the process exits. Only the
# process that made it removes it (a forked child inherits the handler, not the ownership).
_OWNER = os.getpid()


def _remove_at_exit() -> None:
    if os.getpid() == _OWNER:
        remove_session_root()


atexit.register(_remove_at_exit)
