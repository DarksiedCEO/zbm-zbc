"""
ZBM (Z Best Media, advertising agency) Creative Production layer — ADR 0005.

Eight intelligences, one module each:
  1 brief_writer       2 creative_lead      3 audience_insight
  4 placement_spec     5 rights_provenance  6 hook_retention
  7 creative_memory    8 creative_quality
plus `brief` (the 15-field brief model), `results` (measured-result model)
and `workflow` (sequencing + ledger + gates; no judgement of its own).

This package must never import from `zbc` (tests/test_import_boundary.py).
"""
