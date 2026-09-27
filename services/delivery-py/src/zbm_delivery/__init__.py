"""
zbm_delivery — Client Delivery & Operations (28): the AEGIS fix engine and the agent runtime adapters around the
pinned deer-flow harness (DEPT28_SPEC rev 1, ADR 0011).

Package layout (spec §C): ``config``/``gate`` (refuse to start), ``adapters/`` (sandbox, guardrail, egress, model,
identity, receipts — the classes deer-flow instantiates by class path), ``engine/`` (states, brief, report, parsers,
loop), ``runner``/``gitport`` (the engine's own test and git execution), ``service``/``api`` (record-first HTTP
surface). ``api``, ``config``, ``ledger`` and ``store`` import nothing from ``deerflow`` (spec §D).
"""

DEERFLOW_COMMIT = "345f08be00c8a9495079b732a39b46aa9af1584e"
SUPERPOWERS_COMMIT = "8ca22dba9a94f28898bbce59f2537ff4d87c747d"
