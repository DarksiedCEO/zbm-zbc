"""Writes ``registry.json`` (spec item -> conformance test node ids) from the collected suite, by the item id each test
name carries. Run when a test is ADDED (the suite only grows: ``test_registry.py`` fails when a registered node
disappears, an item has no test, or the count drops below the floor):

    python tests/conformance/tools/registry.py
"""
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE.parent))
from conformance.spec_items import GATE_ONLY, ITEMS  # noqa: E402

RULES = [
    (r"SP_class.*\[(SP-\d\d)\]", lambda m: [m.group(1)]),
    (r"SEP_separator.*\[(SEP-[0-9ab]+)\]", lambda m: [m.group(1)]),
    (r"SEP_5", lambda m: ["SEP-5"]), (r"test_core_L_(\d)", lambda m: [f"L-{m.group(1)}"]),
    (r"test_core_M_6_P_1_P_3", lambda m: ["M-6", "P-1", "P-3"]), (r"test_core_M_(\d)_", lambda m: [f"M-{m.group(1)}"]),
    (r"manifest", lambda m: ["M-4", "G-7"]), (r"P_5_must", lambda m: ["P-5"]),
    (r"P_6([a-f])_", lambda m: [f"P-6{m.group(1)}", "G-6"]), (r"test_core_P_4", lambda m: ["P-4"]),
    (r"test_core_M_3", lambda m: ["M-3", "U-2"]), (r"O_4_PIN", lambda m: ["O-4 PIN"]),
    (r"O_4_N_", lambda m: ["O-4 N"]), (r"O_4_F_", lambda m: ["O-4 F"]), (r"O_4_U_", lambda m: ["O-4 U", "U-1"]),
    (r"O_4_I_", lambda m: ["O-4 I", "S-1"]), (r"O_4_MV_", lambda m: ["O-4 MV", "S-1"]),
    (r"O_4_H_", lambda m: ["O-4 H"]), (r"H_1_", lambda m: ["H-1"]),
    (r"O_1_O_4_W", lambda m: ["O-1", "O-4 W"]), (r"O_4_W_full", lambda m: ["O-4 W"]), (r"O_3_", lambda m: ["O-3"]),
    (r"O_2_", lambda m: ["O-2"]), (r"O_4_UP", lambda m: ["O-4 UP"]),
    (r"R_matrix_residual.*\[(R-[a-z0-9]+)-", lambda m: [m.group(1)]),
    (r"R_measured_elsewhere\[(R-[a-z0-9]+)\]", lambda m: [m.group(1)] + (["P-2"] if m.group(1) == "R-new" else [])),
    (r"test_core_R_([a-z0-9]+)_", lambda m: [f"R-{m.group(1)}"]),
    (r"K_1_K_2_K_3_K_4_N_1", lambda m: ["K-1", "K-2", "K-3", "K-4", "N-1"]), (r"K_1_", lambda m: ["K-1"]),
    (r"K_5_", lambda m: ["K-5"]), (r"N_2_", lambda m: ["N-2"]), (r"PF_(\d)_", lambda m: [f"PF-{m.group(1)}"]),
    (r"IM_(\d)_", lambda m: [f"IM-{m.group(1)}"]), (r"test_core_G_(\d)_", lambda m: [f"G-{m.group(1)}"]),
]


def collect() -> list:
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--collect-only",
                        str(HERE)], capture_output=True, text=True, cwd=HERE.parent.parent)
    return sorted(line.split("tests/conformance/", 1)[1] for line in p.stdout.splitlines()
                  if "::" in line and "tests/conformance/" in line)


def build(nodes) -> dict:
    reg: dict = {i: [] for i in ITEMS}
    for n in nodes:
        for rx, fn in RULES:
            m = re.search(rx, n)
            if m:
                for item in fn(m):
                    if item in reg and n not in reg[item]:
                        reg[item].append(n)
    for g in GATE_ONLY:
        reg[g].append("gate:tools/gate.sh")
    reg["G-2"] = [n for n in nodes if "test_registry" in n] + ["gate:tools/gate.sh"]
    return reg


if __name__ == "__main__":
    nodes = collect()
    reg = build(nodes)
    missing = [i for i, v in reg.items() if not v]
    out = {"spec": "core-spec/1", "floor": len(nodes), "items": reg}
    (HERE / "registry.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="ascii")
    print(f"nodes={len(nodes)} items={len(reg)} items without a test={missing}")
