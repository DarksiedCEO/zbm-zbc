"""core-spec/2 G-2: the CI lane and the heavy lane as test lists (the ONE definition; LANES_CORE2.md explains it).

CI lane (G-2: T, V, CV-10 stratified, BL, AL, RV, LP sample; <= 10 min on a quiet box):
    python tests/conformance/tools/ci_lane.py ci          (or: python -m pytest -q -p no:cacheprovider -m core2_ci
                                                            <every path of CI>; the marker is set by conftest.py)
Heavy lane (full CV-10a / FS / CV-8 / LP, plus the reported SP matrix; lead gate, sharded):
    CONFORMANCE_HEAVY=1 CONFORMANCE_SHARD=i/n python tests/conformance/tools/ci_lane.py heavy

What each CI entry samples (spec ids; counts only, never a value):
  T      every sink module test; T-4 static + dynamic (t4_recorder); T-5 smuggling; T-6 canary every route
  V      V-1..V-5 (V-6 upgrade = heavy: test_core_upgrade needs CONFORMANCE_HEAVY=1)
  CV-10  test_core_vm: >= 10 cells per (entry, SP, SEP) stratum of every VM corpus (seed 20261114, CI_PER_STRATUM 10)
         plus ALL of CV-10b (f1); test_core_cv10cde: 20% of FS (vi % 5 == 0), CV-10d CI, CV-10e; CV-8 sample
  BL     test_cv_bounded_label: BL-3 CI sample, BL-2 CI sample AND (F16b-2) the full BL-2 rows of every L-4 script
         entry = EVERY SEP-3b verb x placement x NF x 3 seeds (test_bl2_script_entries_full_negatives_masked_zero)
  AL     AL-1 unit + merged spans, AL-2 e2e (2 tenants x 1,000)
  RV     RV-1..RV-4 (test_reveal)
  LP     test_core_lp_ti: the CV-10a CI subset (10 per stratum, same seed / shards) + TI
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))           # services/seo-py

CI = {
    "T": ["tests/test_sink_fields.py", "tests/test_log_sink_typed.py", "tests/test_error_body_typed.py",
          "tests/test_outbound_typed.py", "tests/test_t4_type_decisions.py", "tests/test_t4_recorder_cases.py",
          "tests/test_t4_recorder_gateoff.py", "tests/test_withhold_t2_ref.py", "tests/test_audit_evidence_legacy.py",
          "tests/conformance/test_core_smuggling.py", "tests/test_canary_all_routes.py"],
    "V": ["tests/test_vault.py", "tests/test_vault_scope.py", "tests/test_vault_v1_proof.py",
          "tests/test_vault_v2_refusal.py", "tests/test_vault_wiring.py"],
    "CV-10": ["tests/test_cv_shape.py", "tests/test_cv_exempt.py", "tests/test_client_view.py",
              "tests/test_cv_monotonic.py", "tests/conformance/test_core_cv_other.py",
              "tests/conformance/test_core_vm.py", "tests/conformance/test_core_cv10cde.py",
              "tests/conformance/test_core_cv8.py"],
    "BL": ["tests/test_cv_bounded_label.py"],
    "AL": ["tests/test_cv_allowlist.py", "tests/test_cv_allowlist_service.py", "tests/test_cv_al_merged.py",
           "tests/conformance/test_core_al_e2e.py"],
    "RV": ["tests/test_reveal.py"],
    "LP": ["tests/conformance/test_core_lp_ti.py"],
}

# heavy lane: the conformance_heavy tests of these files (CONFORMANCE_HEAVY=1, sharded); SP matrix = report only
HEAVY = {
    "CV-10a full + CV-10b": ["tests/conformance/test_core_vm.py"],
    "FS full (CV-10c) + CV-10d full": ["tests/conformance/test_core_cv10cde.py"],
    "CV-8 full": ["tests/conformance/test_core_cv8.py"],
    "LP-1 full + TI": ["tests/conformance/test_core_lp_ti.py"],
    "V-6 upgrade": ["tests/conformance/test_core_upgrade.py"],
    "SP matrix (report, no pass/fail)": ["tests/conformance/test_core_sp_report.py"],
}


def paths(lane: str) -> list:
    groups = CI if lane == "ci" else HEAVY
    out: list = []
    for ps in groups.values():
        out += [p for p in ps if p not in out]
    return out


def missing(lane: str) -> list:
    return [p for p in paths(lane) if not os.path.isfile(os.path.join(ROOT, p))]


def is_ci(fspath: str) -> bool:
    rel = os.path.relpath(str(fspath), ROOT).replace(os.sep, "/")
    return rel in _CI_SET


_CI_SET = frozenset(paths("ci"))
