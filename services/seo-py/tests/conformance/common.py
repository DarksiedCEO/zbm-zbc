"""Shared helpers of the conformance suite: seeded random sources, the SPEC N table and the assertion that a test
ran at least its SPEC count (SUITE_PLAN 5: a generator that silently yields fewer cases fails)."""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
_CAT = None
SEEDS_FAST = (7, 11)
SEEDS_LEAD = (7, 11, 12, 13, 14, 15, 20261113)


def seeds(lead: bool = False) -> tuple:
    """The fast tier's fixed seeds, the lead gate's (plus one from the date), or the one ``CONFORMANCE_SEED``."""
    from datetime import date
    one = os.environ.get("CONFORMANCE_SEED")
    if one:
        return (int(one),)
    if lead:
        return SEEDS_LEAD + (int(date.today().strftime("%Y%m%d")),)
    return SEEDS_FAST


def catalogue() -> dict:
    global _CAT
    if _CAT is None:
        with open(HERE / "catalogue.json", encoding="ascii") as fh:
            _CAT = json.load(fh)
    return _CAT


def spec_n(item: str, lead: bool = False) -> int:
    n = catalogue()["spec_n"].get(item)
    if n is None:
        raise KeyError(f"no SPEC N for {item}")
    return n[1] if lead else n[0]


def rng(seed: int, salt: str) -> random.Random:
    """One Random per (seed, test): the same seed always gives the same cases."""
    return random.Random(f"{seed}:{salt}")


def check_ran(item: str, ran: int, lead: bool = False) -> None:
    need = spec_n(item, lead)
    assert ran >= need, f"{item}: ran {ran} cases, SPEC requires >= {need}"


def lead_enabled(config) -> bool:
    return "lead_gate" in (getattr(config.option, "markexpr", "") or "") and \
        "not lead_gate" not in (config.option.markexpr or "")


def seed_note(seed: int, i) -> str:
    return f"(seed {seed}, case {i}; replay with CONFORMANCE_SEED={seed})"


def env_seed():
    v = os.environ.get("CONFORMANCE_SEED")
    return int(v) if v else None
