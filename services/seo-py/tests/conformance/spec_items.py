"""The numbered items of core-spec/1 (2026-11-13) that the conformance suite holds a test for (G-7, SUITE_PLAN
"Registry"). X (contradictions) and Q (owner questions) are not testable items; the gate-only items (G-1, G-3, G-4,
G-5, G-9) are steps of ``tools/gate.sh``."""
SPEC_VERSION = "core-spec/1"
ITEMS = (
    [f"L-{i}" for i in range(1, 9)]
    + [f"SP-{i:02d}" for i in range(1, 41)]
    + ["SEP-1", "SEP-2", "SEP-3a", "SEP-3b", "SEP-4", "SEP-4b", "SEP-5"]
    + [f"M-{i}" for i in range(1, 7)]
    + ["P-1", "P-2", "P-3", "P-4", "P-5", "P-6a", "P-6b", "P-6c", "P-6d", "P-6e", "P-6f"]
    + ["O-1", "O-2", "O-3", "O-4 W", "O-4 PIN", "O-4 N", "O-4 H", "O-4 U", "O-4 I", "O-4 MV", "O-4 F", "O-4 UP"]
    + [f"K-{i}" for i in range(1, 6)] + ["N-1", "N-2", "U-1", "U-2", "H-1", "S-1"]
    + ["R-a", "R-b", "R-c", "R-d1", "R-d2", "R-d3", "R-d4", "R-d5", "R-d6", "R-d7", "R-d8", "R-g", "R-h", "R-71",
       "R-sigma", "R-hi", "R-hg", "R-o3", "R-o4", "R-o5", "R-p", "R-kl", "R-new"]
    + ["PF-1", "PF-2", "PF-3", "PF-4", "IM-1", "IM-2"]
    + [f"G-{i}" for i in range(1, 10)]
)
GATE_ONLY = ("G-1", "G-3", "G-4", "G-5", "G-9")
