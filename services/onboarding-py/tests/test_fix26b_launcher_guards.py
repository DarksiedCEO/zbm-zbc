"""
Fix wave 26b (scout C5-3, C5-4): onboarding-py's launcher takes its GIL switch-interval check and its SIGTERM handling
from src/launch_guard.py, the module shared byte-for-byte by the Python services' launchers (hygiene L4).

C5-3: the range check ([100 us, 50 ms], compared in whole microseconds in force) was detection-py's, copied by hand
into four launchers while four others accepted any positive number.
This launcher had a hand copy; it now calls the shared one (the shared-guard test
below fails without launch_guard).
C5-4: uvicorn re-raises the SIGTERM it captured once its graceful shutdown is done, with the disposition it found at
start; with the default one the process died of the signal (status -15) and no atexit handler ran. The launcher now
installs launch_guard's handler first: the stop is a normal exit, status 143, and exit handlers run. Proved on the
REAL launcher and uvicorn: serve.run() serves a minimal ASGI app on port 0 (the OS assigns the port; the test picks none);
the child registers an atexit handler that writes a marker file and is sent SIGTERM once it has announced its bind.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
SERVICE = "onboarding-py"
SWITCH_ENV = "ONBOARDING_SWITCH_INTERVAL_SECONDS"


def _env() -> dict:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop(SWITCH_ENV, None)
    return env


# --- C5-4: SIGTERM is a normal exit -----------------------------------------------------------------------------------

SIGTERM_CHILD = (
    "import atexit, sys\n"
    "marker = sys.argv[1]\n"
    "atexit.register(lambda: open(marker, 'w').write('atexit ran'))\n"
    "import serve\n"
    "async def app(scope, receive, send):\n"
    "    if scope['type'] == 'lifespan':\n"
    "        while True:\n"
    "            m = await receive()\n"
    "            if m['type'] == 'lifespan.startup':\n"
    "                await send({'type': 'lifespan.startup.complete'})\n"
    "            elif m['type'] == 'lifespan.shutdown':\n"
    "                await send({'type': 'lifespan.shutdown.complete'})\n"
    "                return\n"
    "    else:\n"
    "        await send({'type': 'http.response.start', 'status': 204, 'headers': []})\n"
    "        await send({'type': 'http.response.body', 'body': b''})\n"
    "serve.run(app, host='127.0.0.1', port=0)\n"
)


def test_sigterm_stops_the_launcher_through_a_normal_exit_so_atexit_handlers_run(tmp_path):
    marker = tmp_path / "atexit-ran"
    with tempfile.TemporaryFile() as out:       # unlinked at once: nothing is left behind
        proc = subprocess.Popen([sys.executable, "-c", SIGTERM_CHILD, str(marker)], cwd=SRC, env=_env(),
                                stdout=out, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 60   # hang guard only
            while b"Uvicorn running on" not in os.pread(out.fileno(), os.fstat(out.fileno()).st_size, 0):
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("the launcher did not start: "
                                       + os.pread(out.fileno(), 4000, 0).decode(errors="replace"))
                time.sleep(0.05)
            proc.send_signal(signal.SIGTERM)
            rc = proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        text = os.pread(out.fileno(), os.fstat(out.fileno()).st_size, 0).decode(errors="replace")
    assert rc == 128 + signal.SIGTERM, (rc, text[-2000:])
    assert marker.is_file() and marker.read_text() == "atexit ran", ("no atexit handler ran", rc, text[-2000:])


# --- C5-3: the interval comes from the shared guard -------------------------------------------------------------------

SHARED_CHILD = (
    "import sys, launch_guard\n"
    "real, want = launch_guard.switch_interval_from_env, sys.argv[1]\n"
    "launch_guard.switch_interval_from_env = lambda name, *a, **k: 0.0002 if name == want else real(name, *a, **k)\n"
    "import uvicorn, serve\n"
    "uvicorn.run = lambda app, **kw: None\n"
    "serve.run(object(), host='127.0.0.1', port=1)\n"
)


def test_the_launcher_takes_its_switch_interval_from_the_shared_guard():
    out = subprocess.run([sys.executable, "-c", SHARED_CHILD, SWITCH_ENV], cwd=SRC, env=_env(), capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert f"{SERVICE}: GIL switch interval in force: 200 us" in out.stderr, out.stderr[-2000:]
