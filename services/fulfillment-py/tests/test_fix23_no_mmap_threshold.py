"""
Fix wave 23 (AEGIS round 22, N22-C-1 and N22-C-2; ADR 0002 Decision 21).

Wave 22 made `python3 -m api` fix glibc's mmap threshold at 128 KiB (`api._fix_mmap_threshold`). That contradicted
Decision 21 (fix wave 7): a fixed threshold was measured and REJECTED because it makes every large body ~50% more CPU
in page faults (19 vs 13 ms per 3.4 MiB); round 22 measured the same cost again (alloc/free of 256 KiB blocks ~15x
slower, growing a 4 MiB bytearray ~5x). The call is removed with its test (which also carried the musl/stub-libc
question of N22-C-2, moot without the call). What bounds the 128-sender peak is the 16 KiB bounded reads of
`src/graceful_close.py` (wave 22, ADR 0003 §9); `tests/test_fix8_n7_2_body_prealloc.py` is the proof, run 20x
single and 10x as a module under three busy loops with the call removed (ADR 0002, "Fix wave 23").
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def test_the_launcher_leaves_glibcs_mmap_threshold_alone():
    import api
    text = (SRC / "api.py").read_text(encoding="utf-8")
    assert not hasattr(api, "_fix_mmap_threshold") and not hasattr(api, "_MMAP_THRESHOLD_BYTES")
    assert "mallopt(-3" not in text and "M_MMAP_THRESHOLD" not in text
    main = text[text.index("def main() -> None:"):]
    assert "mmap" not in main.lower()


def test_decision_21s_arena_limit_and_idle_trim_stay():
    import api
    text = (SRC / "api.py").read_text(encoding="utf-8")
    main = text[text.index("def main() -> None:"):]
    assert callable(api._limit_malloc_arenas) and callable(api._malloc_trim)
    assert "_limit_malloc_arenas()" in main


def test_the_wave_22_threshold_test_is_gone_with_the_code():
    assert not (Path(__file__).parent / "test_fix22_mmap_threshold.py").exists()
