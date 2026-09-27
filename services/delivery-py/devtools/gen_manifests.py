"""
DEVTOOLS ONLY. Regenerate ``seed/skills_manifest.json`` and ``seed/prompts_manifest.json`` (``{relative path:
sha256}`` for every file under ``skills/`` and ``prompts/``) and print every hash ``config.py`` pins (spec §0.4(c),
§C.1.3-6, §C.9, §I). ``--write`` rewrites the two manifests; ``--pin`` rewrites the ``PINNED_*`` constants in
``src/zbm_delivery/config.py``. Every change here is an ADR 0011 amendment (the pins are listed there).
"""

from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from zbm_delivery.gate import sha256_file, walk_hashes  # noqa: E402

PINS = {
    "PINNED_DEERFLOW_CONFIG_SHA256": os.path.join(ROOT, "config", "deerflow.engine.yaml"),
    "PINNED_EXTENSIONS_CONFIG_SHA256": os.path.join(ROOT, "config", "extensions_config.json"),
    "PINNED_SKILLS_MANIFEST_SHA256": os.path.join(ROOT, "seed", "skills_manifest.json"),
    "PINNED_PROMPTS_MANIFEST_SHA256": os.path.join(ROOT, "seed", "prompts_manifest.json"),
    "PINNED_TOOL_POLICY_SHA256": os.path.join(ROOT, "seed", "tool_policy_seed.json"),
    "PINNED_TEST_COMMANDS_SHA256": os.path.join(ROOT, "seed", "test_commands_seed.json"),
    "PINNED_LICENCE_ALLOWLIST_SHA256": os.path.join(ROOT, "seed", "licence_allowlist.json"),
    "PINNED_LICENCE_EXCEPTIONS_SHA256": os.path.join(ROOT, "seed", "licence_exceptions.json"),
}


def manifest(root: str) -> dict:
    hashes, problems = walk_hashes(root)
    if problems:
        raise SystemExit("refusing: " + "; ".join(problems))
    return dict(sorted(hashes.items()))


def main(argv: list[str]) -> int:
    write = "--write" in argv
    pin = "--pin" in argv
    for name, root in (("skills_manifest", os.path.join(ROOT, "skills")), ("prompts_manifest", os.path.join(ROOT, "prompts"))):
        m = manifest(root)
        path = os.path.join(ROOT, "seed", f"{name}.json")
        text = json.dumps(m, indent=2, sort_keys=True) + "\n"
        if write:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        print(f"{name}: {len(m)} file(s)" + (" written" if write else ""))
        for k, v in m.items():
            print(f"  {v}  {k}")
    values = {k: sha256_file(p) for k, p in PINS.items()}
    for k, v in values.items():
        print(f"{k} = \"{v}\"")
    if pin:
        cfg = os.path.join(ROOT, "src", "zbm_delivery", "config.py")
        with open(cfg, "r", encoding="utf-8") as fh:
            src = fh.read()
        for k, v in values.items():
            src, n = re.subn(rf'^{k} = "[^"]*"$', f'{k} = "{v}"', src, flags=re.M)
            if n != 1:
                raise SystemExit(f"could not pin {k}")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(src)
        print("config.py pins rewritten")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

