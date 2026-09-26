"""
Intelligence 9 — Offboarding (spec §C.9, CN-21; Onboarding P4 clean exit).

The exit checklist and its state. Steps, each recorded first:
(1) status ``offboarding``, no new enrolments, active enrolments withdrawn;
(2) access: CN-side access revoked at once; the hub session (hub port) and
    V&I connections — revoked immediately for ``ban`` and ``minor``; for a
    voluntary exit with unsettled clips the clipper may keep connections
    until the last ``revision_watch_end`` (default when unsettled clips
    exist), because V&I cannot certify without a connection (VI-17);
(3) Finance ``open_items``: ``none`` / ``open`` / unavailable → ``open`` or
    ``unknown`` puts the record in ``pending_finance`` and Finance is
    notified; it cannot close until Finance answers ``none``;
(4) export of everything CN holds about the clipper;
(5) deletion of contact data and handles ``post_exit_retention_days`` after
    exit unless an open dispute or an open Finance item needs them;
    acceptance records (ids and hashes) are kept ``agreement_retention_days``;
(6) ``closed``. Never deletes what Finance or an open dispute needs.
"""

from __future__ import annotations

NUMBER, NAME, ACTOR = 9, "Offboarding", "intel_09_offboarding"
TRIGGERS = ("clipper_request", "ban", "minor", "andre_decision")
IMMEDIATE_REVOKE = ("ban", "minor")
