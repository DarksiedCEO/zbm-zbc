"""Review round 11 (search_performance 2026-11-08.11): spec / model-number queries kept (no length rule on joined
letter-digit text), SSN / phone digits across filler tokens, homoglyph / enclosed / Spanish / mixed spelled digits,
spaced and "dot" IPv4, and the composed (two-obfuscation) attack corpus with its residual pinned."""

from __future__ import annotations

import attack_corpus as ac
import pytest

from agents import search_performance as sp

SPEC_KEEP = [
    "12v 100ah lifepo4 battery", "mx518 g502 comparison", "cat6 vs cat5e 100ft", "4k 144hz 27in monitor",
    "r410a r22 refrigerant", "rtx4090 vs rx7900xtx", "i7 13700k vs r9 7950x", "ddr5 6000mhz cl30 32gb",
    "wd40 vs pb blaster", "2x4 vs 2x6 lumber", "m2 nvme ssd 2tb", "usb c 65w pd charger", "5g vs 4g lte",
    "h7 led bulb 6000k", "p0420 code fix", "r134a 12oz can", "ps5 vs xbox series x", "sm58 vs sm57",
    "a7iv vs a7r5", "mp3 to mp4 converter", "rj45 cat6 connector", "x570 vs b550", "f150 5.0 v8 specs",
    "3m n95 8210", "dewalt dcd791d2 drill", "lg c3 65in oled", "s23 vs s24 ultra", "gtx1660 vs rtx3060",
    "ip68 vs ip67", "1080p vs 4k 60fps", "lol 2024", "sos 2024 meaning", "best cars 2019 vs 2020",
    "cars under 30000 in 50000 miles", "3 bed 2 bath 1500 sq ft", "2012 ford f150 3.5 ecoboost",
    "one bedroom apartment", "vitamin b 12 dosage", "zip code 78701",
]
ROUND11_DROP = [
    "1234 -ab- 5678 -ab- 9", "123 x 45 x 6789", "555 x 010 x 0199", "192 . 0 . 2 . 1", "192 dot 0 dot 2 dot 1",
    "192 (dot) 0 (dot) 2 (dot) 1", "4lll llll llll llll", "SSS-OlO-Ol99", "4lll x llll x llll x llll",
    "❹❶❶❶ ❶❶❶❶ ❶❶❶❶ ❶❶❶❶",
    " ".join(["cuatro"] + ["uno"] * 15), "4 one 1 one 1 one 1 1 1 1 1 1 1 1 1 1",
    "cuatro x uno x uno x uno x uno x uno x uno x uno x uno x uno x uno x uno x uno x uno x uno x uno",
    "f i v e f i v e f i v e z e r o o n e z e r o z e r o o n e n i n e n i n e",
    "ab12 cd34 ef56 gh78", " ".join("".join(("AKIA", "IOSFODNN7", "EXAMPLE"))[::-1]),
    "amfuzs5kb2 vazxhhbxbs zs5jb20=",
]


@pytest.mark.parametrize("q", SPEC_KEEP)
def test_spec_and_model_number_queries_are_kept(q):
    assert sp.classify_query(q, ["acme"]) != "pii_suspect", q


@pytest.mark.parametrize("q", ROUND11_DROP)
def test_round11_attacks_drop(q):
    assert sp.classify_query(q, ["acme"]) == "pii_suspect", repr(q)


def test_single_obfuscation_corpus_has_no_residual():
    allc = list(ac.cases())
    kept = [(b, n) for b, n, t in allc if sp.classify_query(t, ["acme"]) != "pii_suspect"]
    assert len(allc) >= 470 and kept == []


def test_composed_sample_has_no_residual():
    """The fast sample (round 11's one-in-11 subset); the full run is in tests/test_query_privacy_r12.py."""
    allc = list(ac.composed_cases(every=ac.COMPOSED_EVERY))
    kept = [(b, n) for b, n, t in allc if sp.classify_query(t, ["acme"]) != "pii_suspect"]
    assert len(allc) >= 1500 and kept == [], kept
