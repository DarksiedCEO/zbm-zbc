"""Evidence file for REPORTED conformance columns (FCL-CVR, core-spec/2 section 6: the CV columns of O-4 F / MV / W are
reported, not judged; the CV lane's own bar is CV-8, test_core_cv8). Counts only, never text.

Each call appends one JSON line {"row": ..., "counts": {...}} to ``$CONFORMANCE_EVIDENCE/reported_cv.jsonl`` when the
env var names a directory; it always prints the same line so the run log carries it too."""
import json
import os
from pathlib import Path

FILE = "reported_cv.jsonl"


def report(row: str, counts: dict) -> dict:
    rec = {"row": row, "counts": {str(k): v for k, v in sorted(counts.items(), key=lambda kv: str(kv[0]))}}
    line = json.dumps(rec, sort_keys=True, ensure_ascii=True)
    print(f"REPORTED {line}")
    d = os.environ.get("CONFORMANCE_EVIDENCE")
    if d:
        p = Path(d)
        p.mkdir(parents=True, exist_ok=True)
        with open(p / FILE, "a", encoding="ascii") as f:
            f.write(line + "\n")
    return rec
