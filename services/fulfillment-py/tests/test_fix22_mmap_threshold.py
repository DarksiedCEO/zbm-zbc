"""
Fix wave 22 (lead ruling G5; AEGIS N21-C-1): the 128-sender test's RSS peak (bound 96 MiB, not raised) failed under
load in round 21 (2/10 module, 5/20 single). Attribution (tracemalloc at the traced peak, and RSS A/B under 3 busy
loops): the in-flight body budget (64 MiB) and uvicorn's per-connection body buffering are the live bytes — the
latter now bounded by src/graceful_close.py (16 KiB reads) — and the run-to-run variance of the peak is glibc's
DYNAMIC mmap threshold: once a large block is freed, later body buffers come from the heap and freed heap blocks
below the top stay resident. `python3 -m api` now fixes the threshold at 128 KiB (api._fix_mmap_threshold).

This test proves the mechanism in a child process (glibc only): free one 4 MiB block, then allocate 96 blocks of
512 KiB and free all but the last. With the dynamic threshold the 95 freed blocks sit in the heap under the
survivor and stay resident (~48 MiB); with the fixed threshold each was its own mapping and is gone.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

CHILD = r'''
import sys
sys.path.insert(0, sys.argv[1])
fix = sys.argv[2] == "fixed"
import api
if fix:
    assert api._fix_mmap_threshold()
from _procinfo import rss_kib
import os
base = rss_kib(os.getpid())
big = bytearray(4 * 1024 * 1024)
del big                                    # an mmapped block freed: glibc's dynamic threshold rises to 4 MiB
blocks = [bytearray(512 * 1024) for _ in range(96)]
keep = blocks[-1]
del blocks
print((rss_kib(os.getpid()) - base) // 1024, flush=True)
'''


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="glibc's mallopt (the fix is glibc only; a no-op elsewhere)")
@pytest.mark.parametrize("mode,retained_max,retained_min", [("dynamic", None, 32), ("fixed", 8, None)])
def test_freed_body_sized_blocks_leave_rss_only_with_the_threshold_fixed(mode, retained_max, retained_min):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": os.pathsep.join([str(SRC), str(Path(__file__).parent)]),
           "FULFILLMENT_SERVICE_TOKEN": "mmap-test-token-not-a-secret"}
    env.pop("MALLOC_MMAP_THRESHOLD_", None)
    r = subprocess.run([sys.executable, "-c", CHILD, str(SRC), mode], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    retained = int(r.stdout.strip().splitlines()[-1])
    print(f"{mode}: {retained} MiB still resident after freeing 95 x 512 KiB")
    if retained_max is not None:
        assert retained < retained_max, retained
    if retained_min is not None:
        assert retained >= retained_min, retained                  # the mechanism the fix removes is real here


def test_the_launcher_fixes_the_threshold():
    import api
    src = Path(api.__file__).read_text()
    main = src[src.index("def main() -> None:"):]
    assert "_fix_mmap_threshold()" in main and api._MMAP_THRESHOLD_BYTES == 128 * 1024
