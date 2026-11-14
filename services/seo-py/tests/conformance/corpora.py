"""Every frozen corpus of the Priya conformance suite (priya-spec/1, SUITE_PLAN; priya-spec/1.1 C-5: the reviewers'
generators joined the suite). One registry for the manifest (corpus drift guard, ``data/manifest.json``), the
lost-protection baseline bits (LP-1, ``data/baseline/``, written once by ``devtools/conformance/calibrate.py``) and
the modules that read them. Each corpus is a pure function of its seed: ``rows(name)`` yields (key, text) in a fixed
order, the key being the case's cell / pair / class id (never personal data). Values are synthetic, made at run time.

PERSONAL corpora hold only values of the closed list (every one must be dropped; LP-1 bits are kept for them);
ORDINARY corpora are the ordinary-query corpora of OQ-1 (labelled by the independent oracles). ``CI_STRIDE`` is the CI
lane's draw of a large corpus: every k-th case, the same cases each run."""

from __future__ import annotations

import hashlib
import unicodedata

import attack_corpus as ac
import priya_gen as g
from rvgen import rv18_bnd, rv18_ord, rv18_p1p2, rv19_g4, rv19_g5, rv19_g6, rv19_g7

# rv-priya19's "cpx" construction: the ten digit / vanity pairs it measured with more cases (its own file's seed is not
# recorded in the generator; the suite draws them with the review's composed-pair driver, seed 19, 400 per pair)
CPX_JOBS = (("nanp", "gap_other", "spelled_en"), ("vanity", "zw_bidi", "spelled_en"),
            ("nanp", "gap_other", "spelled_mixed"), ("ssn", "spelled_mixed", "gap_other"),
            ("ssn", "gap_other", "spelled_mixed"), ("ssn", "gap_other", "spelled_es"), ("ssn", "gap_other", "spelled_en"),
            ("nanp", "spelled_mixed", "gap_other"), ("card", "gap_other", "spelled_en"),
            ("e164", "gap_other", "spelled_en"))


def _matrix(n):
    for cell, _k, q in g.matrix(n):
        yield ":".join(cell), q


def _composed(n):
    for c, name, q in g.composed(n):
        yield c + "|" + name, q


PERSONAL = {
    # the builder's generators (priya-spec/1 M-1, M-2, CP; the round-10 attack corpus)
    "matrix5": lambda: _matrix(5),
    "matrix200": lambda: _matrix(200),
    "boundary": lambda: iter(g.boundary()),
    "composed4000": lambda: _composed(4000),
    "composed42000": lambda: _composed(42000),
    "attack676": lambda: ((b + "|" + n, t) for b, n, t in ac.cases()),
    "attack40672": lambda: ((b + "|" + n, t) for b, n, t in ac.composed_cases()),
    # rv-priya18 (review of 644b4901): P1 matrix, boundary set, structural composed pairs (p2v2)
    "rv18_p1": lambda: ((c + "|" + d + "|" + p, q) for c, d, p, q, *_x in rv18_p1p2.p1()),
    "rv18_bnd": lambda: ((r["c"] + "|" + r["d"] + "|" + r["p"], r["q"]) for r in rv18_bnd.cases()),
    "rv18_p2v2": lambda: ((r["c"] + "|" + r["d"], r["q"]) for r in rv18_p1p2.p2v2(42000)),
    # rv-priya19 (review of 5bf0d2a5): M-1 at N = 200, boundary set, composed pairs (70 per ordered pair), cpx
    "rv19_m1": lambda: ((r["c"] + "|" + r["d"] + "|" + r["y"], r["q"]) for r in rv19_g4.cases(200, 19)),
    "rv19_b19": lambda: ((r["c"] + "|" + r["d"], r["q"]) for r in rv19_g5.cases()),
    "rv19_cp19": lambda: ((r["c"] + "|" + r["a"] + ">" + r["b"], r["q"]) for r in rv19_g6.cases(70, 19)),
    "rv19_cpx19": lambda: ((r["c"] + "|" + r["a"] + ">" + r["b"], r["q"]) for r in rv19_g6.cases(400, 19, CPX_JOBS)),
}
ORDINARY = {
    "oq": lambda: ((c, q) for c, _i, q in g.ordinary()),
    "rv18_ord": lambda: ((r["c"], r["q"]) for r in rv18_ord.cases()),
    "rv19_oq19": lambda: ((r["c"], r["q"]) for r in rv19_g7.cases(19)),
}
COMPOSED = ("composed4000", "composed42000", "rv18_p2v2", "rv19_cp19", "rv19_cpx19")
HEAVY = ("matrix200", "composed42000", "attack40672", "rv18_p1", "rv18_bnd", "rv18_p2v2", "rv19_m1", "rv19_cp19",
         "rv19_cpx19")
# the CI lane's draw of each heavy corpus: every k-th case (about 2,000-3,500 cases each)
CI_STRIDE = {"matrix200": 0, "composed42000": 0, "attack40672": 0, "rv18_p1": 200, "rv18_bnd": 20, "rv18_p2v2": 20,
             "rv19_m1": 100, "rv19_cp19": 20, "rv19_cpx19": 2, "rv18_ord": 4, "rv19_oq19": 4}
REVISIONS = {"644b4901": "644b4901725fdd077dac60a0703591d6ce1a4088",
             "65f8b92a": "65f8b92ace21cdce68c38ad6880184a0bee537b9"}


def rows(name: str):
    return (PERSONAL.get(name) or ORDINARY[name])()


def ci_rows(name: str):
    """The CI lane's cases of a corpus: all of a CI corpus, every ``CI_STRIDE``-th of a heavy one (0: none)."""
    k = CI_STRIDE.get(name, 1)
    if not k:
        return iter(())
    return ((i, key, q) for i, (key, q) in enumerate(rows(name)) if i % k == 0)


def fingerprint(name: str) -> dict:
    """{n, sha256 of the texts, sha256 of the cell / pair / class ids ("cells"), the Unicode version the generators
    read} of one corpus."""
    ht, hk, n = hashlib.sha256(), hashlib.sha256(), 0
    for key, q in rows(name):
        ht.update(q.encode("utf-8", "surrogatepass") + b"\n")
        hk.update(key.encode("utf-8") + b"\n")
        n += 1
    return {"n": n, "texts": ht.hexdigest(), "cells": hk.hexdigest(), "unidata": unicodedata.unidata_version}
