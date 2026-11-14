"""Heading-order reference (SPEC B-RO-04): a heading level greater than the previous level + 1 is a skip; the first
heading is compared with nothing. Returns the [from, to] pairs, at most 10."""

from __future__ import annotations


def skips(levels: list) -> list:
    out = []
    prev = None
    for lv in levels:
        if prev is not None and lv > prev + 1:
            out.append([prev, lv])
        prev = lv
    return out[:10]


def self_test() -> None:
    assert skips([1, 2, 3]) == [] and skips([1, 3]) == [[1, 3]] and skips([2, 4, 1, 6]) == [[2, 4], [1, 6]]
