"""
Shared reference data and plumbing for Creative Production (ADR 0005).

Everything in this package is REFERENCE DATA or an INTERFACE, never a
decision-maker: the Platform Rules Registry, rights records, the evidence
ledger client, actor identities, the founder approval token check, text
normalisation, and the fail-closed stand-ins for departments that do not
exist yet. Neither `zbm` nor `zbc` decision code lives here, and nothing in
this package imports from `zbm` or `zbc` (enforced by
tests/test_import_boundary.py).
"""
