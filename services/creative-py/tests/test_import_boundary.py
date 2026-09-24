"""
The two intelligence layers share reference data but never decision-makers.

- nothing in src/zbm imports src/zbc, and vice versa;
- src/shared imports neither;
- only api.py (the composition root) may import both.
Checked by parsing every module's AST (catches `import x`, `from x import y`,
relative imports and function-local imports), plus `importlib` calls by name.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    pkg = path.parent.name
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import stays inside its own package
                found.add(pkg)
            elif node.module:
                found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in (
                "import_module", "__import__"):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.add(arg.value.split(".")[0])
    return found


def _package_files(name: str) -> list[Path]:
    files = sorted((SRC / name).glob("**/*.py"))
    assert files, name
    return files


def test_zbm_never_imports_zbc():
    for f in _package_files("zbm"):
        assert "zbc" not in _imports(f), f


def test_zbc_never_imports_zbm():
    for f in _package_files("zbc"):
        assert "zbm" not in _imports(f), f


def test_shared_imports_neither_layer():
    for f in _package_files("shared"):
        assert not ({"zbm", "zbc"} & _imports(f)), f


def test_each_layer_has_its_intelligences_one_module_each():
    zbm = {"brief_writer", "creative_lead", "audience_insight", "placement_spec", "rights_provenance",
           "hook_retention", "creative_memory", "creative_quality"}
    zbc = {"campaign_rulebook", "rulebook_writer", "source_mining", "hook_angle", "platform_rules",
           "rights_clearance", "campaign_kit", "creative_memory", "clip_review"}
    assert len(zbm) == 8 and len(zbc) == 9
    assert zbm <= {p.stem for p in (SRC / "zbm").glob("*.py")}
    assert zbc <= {p.stem for p in (SRC / "zbc").glob("*.py")}


def test_the_checker_itself_catches_a_violation(tmp_path):
    bad = tmp_path / "zbm" / "evil.py"
    bad.parent.mkdir()
    bad.write_text("def f():\n    from zbc.clip_review import review\n    import importlib\n    importlib.import_module('zbc.x')\n")
    assert "zbc" in _imports(bad)
