"""SUITE_PLAN section 0 (corpus drift guard): every corpus of ``corpora.py`` regenerates exactly the stream whose
sha256 ``data/manifest.json`` holds (texts and cell ids hashed, never stored). A generator change without a manifest
change fails here; changing the manifest is a spec change (``devtools/conformance/calibrate.py``). The generators read the
runtime's Unicode tables, so a different Unicode version skips with that reason. The reviewers' corpora are pinned to
the review's own files (priya-spec/1.1 C-5): the same hashes as rv-priya18's p1 / bnd / p2v2 / ord and rv-priya19's
m1 / b19 / cp19 / oq19 data files."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import corpora
import pytest

MANIFEST = Path(__file__).resolve().parent / "data" / "manifest.json"
# the reviews' own data files, as hashed by the suite's fingerprint (n, sha256 of the texts): the ported generators
# must reproduce them case for case
REVIEW_FILES = {
    "rv18_p1": (452000, "f3b9241ed17adb9fe73ca7ea852bd0536522f9f1ac515aec46f8f724bfcca380"),
    "rv18_bnd": (70346, "e766f110ee60df49d739224c8e3207c7492d66a886697902135f97daef4f2b22"),
    "rv18_p2v2": (42476, "f663584323a4952039efdfb39702db60c414d9d167dca8688b5f8839709cb6a5"),
    "rv18_ord": (22600, "8ae51798124e62208d4f0dbaec25da63334c468ba3c019f30acca63021f58bd7"),
    "rv19_m1": (291200, "b72e3f54efa49cf8979ae0a55ffddb99f076598037e5a2724f4095c95146ed89"),
    "rv19_b19": (11532, "ffc0024000e28bd9a59c3b9d2937e94f609249ec3f5454a27169ffee45a0bb25"),
    "rv19_cp19": (43680, "49a5135e1f94c69ff3027bedb29753a11fcb5f040a046cba204f08d763f640fd"),
    "rv19_oq19": (24400, "4e0867837ca0196382318cc0b3b68db9aaa0a5e056e24ddfa7470c9a898b871d"),
    # rv-priya20 (review of a394a985): the review kept per-case verdict files, not text files; its files' case counts
    # (m20 260,000, b20 37,663, cp20 43,680, oq20 24,400) and every case it listed as kept (633, 70, 4 and the glued
    # vanity probe's 556 of 1,680) were reproduced at the same index, text for text, before these sha256 were pinned
    "rv20_m20": (260000, "47321775f232f9e46fe0b0604e7ad3566fd2f41937d2fb62c6961498432eadf5"),
    "rv20_b20": (37663, "0cbe05c66ee522ca7ef65cfd8875aa7e29bd33430dc67feb011a4e30cf9196c1"),
    "rv20_cp20": (43680, "a53fb1884ddc13632101ec2689cff280d7a5bea67c192a0acbaa5562b142ecc7"),
    "rv20_vg": (1680, "063cf982a07770458b84dc349692e9cf9e5f46988c87f1a68559c926828f22d0"),
    "rv20_oq20": (24400, "55385527ac7834d22a167b44aed6a23d8f998f10d6822336947b3c07ed727969"),
}


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def _check(name: str):
    want = _manifest()["corpora"][name]
    if want["unidata"] != unicodedata.unidata_version:
        pytest.skip(f"generators read Unicode {unicodedata.unidata_version}; manifest made with {want['unidata']}")
    got = corpora.fingerprint(name)
    assert (got["n"], got["texts"], got["cells"]) == (want["n"], want["texts"], want["cells"]), name


@pytest.mark.conformance
@pytest.mark.parametrize("name", sorted(set(corpora.PERSONAL) | set(corpora.ORDINARY)))
def test_manifest_corpus_stream_is_frozen(name):
    _check(name)


@pytest.mark.conformance
def test_manifest_lists_every_corpus_and_both_baselines():
    m = _manifest()
    assert set(m["corpora"]) == set(corpora.PERSONAL) | set(corpora.ORDINARY)
    assert set(m["baseline"]) == {f"{n}.{r}" for n in corpora.PERSONAL for r in corpora.REVISIONS}
    assert m["spec"] == "priya-spec/1.1"


@pytest.mark.conformance
@pytest.mark.parametrize("name", sorted(REVIEW_FILES))
def test_C5_ported_generators_reproduce_the_reviews_files(name):
    m = _manifest()["corpora"][name]
    if m["unidata"] != unicodedata.unidata_version:
        pytest.skip("Unicode version differs from the manifest's")
    assert (m["n"], m["texts"]) == REVIEW_FILES[name]
