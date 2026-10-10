"""Cross-service consistency of the lookalike fold (war room fixes WR-F001..WR-F005, ADR 0018). Standard library only.

    python -B -m unittest devtools/test_lookalikes.py -v

- every service's ``src/lookalikes.py`` is the same file (the hygiene lint L4 checks it too) and its generated
  SKELETON block is what ``devtools/lookalikes/generate.py`` makes from the vendored, sha256-pinned confusables.txt;
- its SHARED table is creative-py's ``shared/text.py`` CONFUSABLES, entry for entry;
- every service's fold (its own table over the shared layers) reads the war room's homoglyph sets, the letters of
  the five findings and invisible characters the same way.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "devtools" / "lookalikes"))
sys.path.insert(0, str(REPO / "devtools" / "warroom"))

import generate

SERVICES = generate.SERVICES
# (service, file, the name of its own table, case-sensitive?)
OWN_TABLES = (
    ("clipper-network-py", "src/textguard.py", "CONFUSABLES", False),
    ("onboarding-py", "src/name_key.py", "CONFUSABLES", False),
    ("service-py", "src/triage.py", "CONFUSABLES", True),
    ("verification-py", "src/intelligences/i07_duplicate_identity.py", "_LOOKALIKE", False),
)


def _literal(path: Path, name: str) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            value = node.value
            if isinstance(value, ast.Call):            # str.maketrans({...})
                value = value.args[0]
            return ast.literal_eval(value)
    raise AssertionError(f"{path}: no {name}")


def _module(svc: str):
    spec = importlib.util.spec_from_file_location(f"lookalikes_{svc.replace('-', '_')}",
                                                  REPO / "services" / svc / "src" / "lookalikes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Lookalikes(unittest.TestCase):
    def test_every_copy_is_identical_and_its_data_is_generated_from_the_pinned_source(self):
        texts = {svc: (REPO / "services" / svc / "src" / "lookalikes.py").read_bytes() for svc in SERVICES}
        self.assertEqual(len(set(texts.values())), 1, "the lookalikes.py copies differ")
        self.assertEqual(generate.main([]), 0, "stale SKELETON block: run devtools/lookalikes/generate.py --write")
        mod = _module(SERVICES[0])
        self.assertEqual(mod.SKELETON, generate.skeleton())
        self.assertEqual(mod.SKELETON_SOURCE_SHA256, generate.SOURCE_SHA256)

    def test_shared_is_creative_pys_table(self):
        creative = _literal(REPO / "services/creative-py/src/shared/text.py", "CONFUSABLES")
        self.assertEqual(_module(SERVICES[0]).SHARED, creative)

    def test_every_service_folds_the_war_room_sets_and_the_findings_letters_alike(self):
        from corpus import homoglyph_table
        probes = ["".join(v) for v in homoglyph_table(False).values()] + [
            "kiԁ.ηame", "uηs\u00adυ\u00adbsc\u00adriβe", "ζ ɡ η β ς", "ᴊօsé ɡαrςía", "JoᏚé ʛaгϲíα",
            "ｓｔｏｐ ５７０ｐ", "g\u200bu\u2060a\ufeffr", "ꓚꓮꓢꓧ"]
        mod = _module(SERVICES[0])
        seen = {}
        for svc, rel, name, cased in OWN_TABLES:
            own = _literal(REPO / "services" / svc / rel, name)
            table = mod.Table({chr(k) if isinstance(k, int) else k: v for k, v in own.items()})
            seen[svc] = [(table.fold(p) if cased else table.fold_cased(p)) for p in probes]
        ref = seen[OWN_TABLES[0][0]]
        for svc, got in seen.items():
            for p, a, b in zip(probes, ref, got):
                self.assertEqual(a, b, f"{svc} folds {p!r} as {b!r}, {OWN_TABLES[0][0]} as {a!r}")
        named = dict(zip(probes[-8:], ref[-8:]))
        self.assertEqual(named["kiԁ.ηame"], "kid.name")
        self.assertEqual(named["uηs\u00adυ\u00adbsc\u00adriβe"], "unsubscribe")
        self.assertEqual(named["ζ ɡ η β ς"], "z g n b c")
        self.assertEqual(named["ᴊօsé ɡαrςía"], "jose garcia")
        self.assertEqual(named["ｓｔｏｐ ５７０ｐ"], "stop 570p")
        self.assertEqual(named["g\u200bu\u2060a\ufeffr"], "guar")
        self.assertEqual(named["ꓚꓮꓢꓧ"], "cash")

    def test_the_fold_never_changes_a_letter_a_services_own_table_already_folds(self):
        mod = _module(SERVICES[0])
        for svc, rel, name, _ in OWN_TABLES:
            own = {chr(k) if isinstance(k, int) else k: v for k, v in _literal(REPO / "services" / svc / rel, name).items()}
            table = mod.Table(own)
            for k, v in own.items():
                self.assertEqual(table.mapping[k], v, f"{svc}: {k!r}")


if __name__ == "__main__":
    unittest.main()
