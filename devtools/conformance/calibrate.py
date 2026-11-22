"""SUITE_PLAN C-1, Priya half (priya-spec/1 LP-1; priya-spec/1.1 C-5): the frozen baseline bits and the corpus manifest
of the Priya conformance suite.

    python devtools/conformance/calibrate.py [--jobs 2] [--only NAME,NAME] [--manifest-only]

1. Exports each baseline release (``corpora.REVISIONS``: 644b4901, 65f8b92a) with ``git archive`` into a temporary
   directory: nothing of an old release is vendored into the source tree.
2. Generates every corpus of ``tests/conformance/corpora.py`` and, in a worker process per (release, corpus) that
   imports that release's ``agents`` package, classifies each case's raw text and its stored key (that release's
   ``priya.normalize``) exactly as that release's Priya decides.
3. Writes ``services/seo-py/tests/conformance/data/baseline/<corpus>.<release>.bits.gz`` (two bits per case: the
   raw text dropped, and the row dropped - raw or stored key, Priya's decision; packed little-endian; gzip with a zero timestamp, so a re-run writes the same bytes) and
   ``data/manifest.json`` (per corpus: case count, sha256 of the texts and of the keys, the Unicode version the
   generators read; per baseline file: its sha256). No text is written anywhere.

Run by the lead once per spec version (builders never regenerate the bits: a changed generator is a spec change).
The heavy lane can re-verify the bits from git history (``test_priya_lost_protection``)."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "services" / "seo-py" / "tests"
CONF = TESTS / "conformance"
DATA = CONF / "data"


def pack(bits: list) -> bytes:
    """Two bits per case (raw, key) packed little-endian into bytes, gzip with mtime 0."""
    out = bytearray((len(bits) * 2 + 7) // 8)
    for i, (r, k) in enumerate(bits):
        if r:
            out[(2 * i) // 8] |= 1 << ((2 * i) % 8)
        if k:
            out[(2 * i + 1) // 8] |= 1 << ((2 * i + 1) % 8)
    return gzip.compress(bytes(out), mtime=0)


def unpack(blob: bytes, n: int) -> list:
    raw = gzip.decompress(blob)
    return [((raw[(2 * i) // 8] >> ((2 * i) % 8)) & 1, (raw[(2 * i + 1) // 8] >> ((2 * i + 1) % 8)) & 1)
            for i in range(n)]


def worker(src: str, name: str, out: str) -> None:
    """In a fresh process: the release's agents on the path first, then the suite's generators."""
    sys.path[:0] = [src, str(CONF), str(TESTS)]
    import corpora
    from agents import priya
    from agents import search_performance as sp
    bits = []
    for _key, q in corpora.rows(name):
        raw = sp.classify_query(q, ["acme"]) == "pii_suspect"
        # the second bit is Priya's decision (raw OR key): the key is classified only when the raw text is kept
        bits.append((raw, raw or sp.classify_query(priya.normalize(q), ["acme"]) == "pii_suspect"))
    Path(out).write_bytes(pack(bits))
    print(json.dumps({"corpus": name, "n": len(bits), "dropped": sum(1 for r, k in bits if r or k)}), flush=True)


def export(rev: str, dest: Path) -> Path:
    tar = dest / f"{rev[:8]}.tar"
    with open(tar, "wb") as fh:
        subprocess.run(["git", "archive", "--format=tar", rev, "services/seo-py/src"], cwd=REPO, stdout=fh, check=True)
    with tarfile.open(tar) as tf:
        tf.extractall(dest / rev[:8], filter="data")
    return dest / rev[:8] / "services" / "seo-py" / "src"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--only", default="")
    ap.add_argument("--manifest-only", action="store_true")
    ap.add_argument("--worker", nargs=3, metavar=("SRC", "CORPUS", "OUT"))
    a = ap.parse_args()
    if a.worker:
        worker(*a.worker)
        return 0
    sys.path[:0] = [str(CONF), str(TESTS)]
    import corpora
    # the CI lane's corpora first (small), then the heavy ones
    names = [n for n in a.only.split(",") if n] or sorted(corpora.PERSONAL, key=lambda n: (n in corpora.HEAVY, n))
    (DATA / "baseline").mkdir(parents=True, exist_ok=True)
    mpath = DATA / "manifest.json"
    manifest = json.loads(mpath.read_text()) if mpath.exists() else {"corpora": {}, "baseline": {}}
    for n in list(corpora.PERSONAL) + list(corpora.ORDINARY):
        if not a.only or n in names:
            manifest["corpora"][n] = corpora.fingerprint(n)
            print(json.dumps({"fingerprint": n, "n": manifest["corpora"][n]["n"]}), flush=True)
    if not a.manifest_only:
        with tempfile.TemporaryDirectory() as tmp:
            srcs = {short: export(full, Path(tmp)) for short, full in corpora.REVISIONS.items()}
            jobs = [(short, n) for n in names for short in srcs]

            def run(job):
                short, n = job
                out = DATA / "baseline" / f"{n}.{short}.bits.gz"
                subprocess.run([sys.executable, __file__, "--worker", str(srcs[short]), n, str(out)], check=True,
                               env={**os.environ, "PYTHONHASHSEED": "0"})
                return short, n, hashlib.sha256(out.read_bytes()).hexdigest()
            with ThreadPoolExecutor(a.jobs) as pool:
                for short, n, h in pool.map(run, jobs):
                    manifest["baseline"][f"{n}.{short}"] = {"sha256": h, "n": manifest["corpora"][n]["n"]}
    manifest["spec"] = "priya-spec/1.1"
    manifest["python"] = "%d.%d" % sys.version_info[:2]
    mpath.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
