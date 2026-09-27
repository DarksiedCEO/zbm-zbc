"""The ten Finance intelligences (spec §C): single-task, deterministic judgment modules. The service owns records,
ports and the record-first plumbing; each module here decides one thing."""

from intelligences import (i01_journal, i02_receivables, i03_payables, i04_payout_run, i05_clawback, i06_tax,
                           i07_reconciliation, i08_treasury, i09_controls, i10_evidence_audit)

MODULES = (i01_journal, i02_receivables, i03_payables, i04_payout_run, i05_clawback, i06_tax, i07_reconciliation,
           i08_treasury, i09_controls, i10_evidence_audit)


def registry() -> list[dict]:
    return [{"number": m.NUMBER, "name": m.NAME, "actor": m.ACTOR} for m in MODULES]
