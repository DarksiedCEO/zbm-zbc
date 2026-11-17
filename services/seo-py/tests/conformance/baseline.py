"""Frozen baseline verdicts of core-spec/1 (SUITE_PLAN C-1): for each corpus draw of ``corpora`` and each release
that a spec item names (ffd28ca8, cbdf5e78, 2b26d980), one verdict string per corpus item, gzip-compressed JSON, no
text. Produced ONCE by ``tools/calibrate.py`` from the releases' trees; builders never regenerate them. Each file
carries the stream hash of the corpus it was made from, so a corpus that drifts fails before any comparison."""
import gzip
import json
from pathlib import Path

DIR = Path(__file__).resolve().parent / "data" / "baseline"


def path(corpus: str, release: str) -> Path:
    return DIR / f"{corpus}.{release}.json.gz"


def write(corpus: str, release: str, stream: str, verdicts: list) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    blob = json.dumps({"corpus": corpus, "release": release, "stream": stream, "verdicts": verdicts},
                      ensure_ascii=True, separators=(",", ":")).encode("ascii")
    with open(path(corpus, release), "wb") as f:
        f.write(gzip.compress(blob, mtime=0))


def read(corpus: str, release: str, stream: str) -> list:
    d = json.loads(gzip.decompress(path(corpus, release).read_bytes()).decode("ascii"))
    assert d["corpus"] == corpus and d["release"] == release
    assert d["stream"] == stream, f"corpus {corpus} drifted from its frozen baseline (a spec change)"
    return d["verdicts"]


def code(v: tuple) -> str:
    """One item's verdict tuple as a short string ("1", "0", or a class name for a sink lane)."""
    return "|".join(x if isinstance(x, str) else ("1" if x else "0") for x in v)
