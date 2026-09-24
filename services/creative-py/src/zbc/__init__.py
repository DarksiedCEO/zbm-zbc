"""
ZBC (Z Best Clips, clipping agency) Creative Production layer — ADR 0005.

Unit of work: a CAMPAIGN. Nine intelligences, one module each:
  1 campaign_rulebook   1a rulebook_writer   2 source_mining
  3 hook_angle          4 platform_rules     5 rights_clearance
  6 campaign_kit        7 creative_memory    8 clip_review
plus `rulebook` (versioned, freezable rulebook model), `payout_eligibility`
(a gate aggregator, not an intelligence — it holds no money) and
`workflow` (sequencing + ledger + gates).

This package must never import from `zbm` (tests/test_import_boundary.py).
"""
