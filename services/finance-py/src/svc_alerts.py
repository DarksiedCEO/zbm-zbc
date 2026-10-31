"""
Founder alerts, Finance's side (ADR 0027): which open conditions need Andre, read from Finance's own state and from
Compliance, handed to the dispatcher (``founder_alerts``). Read-only towards the books: this never records, commits or
reads through the ledger, so it works while the ledger is down and can never block or undo bookkeeping.
"""

from __future__ import annotations

from typing import Optional

import founder_alerts as FA

DISPUTE_LEGS = ("stripe_dispute", "dispute_receivable", "stripe_dispute_anomaly")


class AlertsMixin:

    @property
    def alerts(self) -> FA.Dispatcher:
        d = getattr(self, "_founder_alerts", None)
        if d is None:
            cfg = getattr(self.cfg, "founder_alerts", None) or FA.AlertSettings()
            cfg.tz = getattr(self.cfg, "business_tz", None) or cfg.tz
            sender = getattr(self.ports, "founder_mail", None)
            if cfg.enabled and sender is None:
                cfg.problem = "no alert sender is wired (FOUNDER_ALERT_* and RESEND_API_KEY)"
            url = getattr(self.cfg, "ledger_url", None)
            d = FA.Dispatcher(cfg, FA.StateFile(getattr(self.cfg, "data_dir", None)), sender,
                              probe=lambda: FA.ledger_probe(url))
            self._founder_alerts = d
        return d

    def _alert_category(self, b: dict) -> Optional[str]:
        leg = b.get("leg")
        if leg in self.COMPLIANCE_LEGS:
            return "compliance_hold"
        if leg == "client_mail_failed":
            return "client_mail_failed"
        if leg == "stripe_unknown_session":
            return "unknown_paid_checkout"
        if leg in DISPUTE_LEGS:
            return "payment_disputed"
        if b.get("recon_id"):
            return "reconciliation_mismatch"
        if b.get("severity") in ("high", "critical"):
            return "finance_high_break"
        return None

    def founder_alert_conditions(self) -> tuple[list, Optional[bool]]:
        """(conditions, compliance_up). ``compliance_up`` is None when Compliance is not wired (nothing to watch)."""
        conds = []
        with self.lock:
            for b in self.db["breaks"].values():
                if b.get("status") != "open":
                    continue
                cat = self._alert_category(b)
                if cat:
                    conds.append(FA.Condition.make(cat, "finance", b.get("break_id")))
        port = self.ports.compliance
        feed = getattr(port, "all_open_holds", None)
        if feed is None:
            return conds, None
        try:
            ans = feed()
        except Exception:  # noqa: BLE001 - an adapter that raises is "Compliance did not answer"
            ans = None
        if not getattr(ans, "available", False):
            return conds, False
        conds += [FA.Condition.make("compliance_hold", "compliance", h) for h in getattr(ans, "open_hold_ids", ())]
        try:
            exp = port.rule_expiry() if hasattr(port, "rule_expiry") else None
        except Exception:  # noqa: BLE001
            exp = None
        if isinstance(exp, dict) and exp.get("available") and isinstance(exp.get("within_14_days"), list):
            conds += [FA.Condition.make("compliance_rule_expiring", "compliance", oid)
                      for oid in exp["within_14_days"][:50] if isinstance(oid, str)]
        return conds, True

    def run_founder_alerts(self) -> dict:
        """``POST /fin/v1/jobs/founder-alerts/run`` (ops_alerts every 5 minutes; scheduler). Counts only."""
        conds, compliance_up = self.founder_alert_conditions()
        return self.alerts.run(self._now(), conds, compliance_up)
