"""Writes ``data/manifest.json``: the stream hash of every generator's output (SUITE_PLAN 0, corpus drift guard).
Changing a generator changes a hash; the manifest is then a spec change (it is committed with the spec version).

    python tests/conformance/tools/manifest.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "src"))
from conformance import core_matrix as M  # noqa: E402
from conformance import corpora as C  # noqa: E402

out = {"spec": "core-spec/1", "core_matrix_ci": M.stream_hash(M.ci_cells())}
for name, fn in (("neutral", C.neutral), ("fuzz", C.fuzz), ("hosts", C.hosts), ("real_hosts", C.real_hosts),
                 ("urls", C.urls), ("machine", C.machine), ("wsample", C.wsample)):
    out[name] = C.stream_hash(fn())
(HERE / "data" / "manifest.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="ascii")
print(out)
