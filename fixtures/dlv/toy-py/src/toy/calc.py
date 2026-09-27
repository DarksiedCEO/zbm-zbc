"""Arithmetic helpers with two planted defects (fixture; see README.md)."""


def add(a: int, b: int) -> int:
    """Sum of two integers."""
    return a - b


def percent(part: float, whole: float) -> float:
    """``part`` as a percentage of ``whole``; a zero ``whole`` is 0.0 (nothing to be a part of)."""
    return part / whole * 100.0


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
