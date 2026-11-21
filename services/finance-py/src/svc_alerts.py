"""Dormant local observations. No notification or financial operation (ADR0039)."""
import time
from ops_observer import Snapshot, valid_items
import health_poll
from errors import Unavailable


class AlertsMixin:
    def observe_ops(self):
        # Same order as close: observer mutex then brief service lock. Polls/filesystem never own money lock.
        with self._observer_lock:
            with self.lock:
                if self.closed:
                    raise Unavailable("observer service is closed")
                if not self.cfg.ops_observer_enabled:
                    return {"status": "disabled", "persisted": False}
                if self._dir_lock is None or self._lock_token is None or self._ops_observer is None:
                    raise Unavailable("observer requires the Finance directory claim")
            state = self._ops_observer
            if state.fault:
                return state.result()
            observations = []
            deadline = time.monotonic() + health_poll.POLL_BUDGET_S
            for name in health_poll.REQUIRED:
                source = "health:" + name
                url = self.cfg.ops_health_targets.get(name)
                if url is None:
                    observations.append(Snapshot(source, "UNCONFIGURED", self._now(), {}))
                    continue
                acquired = self._now()
                r = health_poll.probe(name, url, deadline=deadline)
                uncertain = any(code in {"unreachable", "deadline_exceeded", "poll_budget_exhausted", "bad_body",
                                       "bad_encoding", "response_too_large", "bad_status", "bad_write_ok", "bad_warning", "bad_reason"}
                                for code in r.code.removeprefix("warning:").split(","))
                coverage = "UNKNOWN" if uncertain else "COMPLETE"
                observations.append(Snapshot(source, coverage, acquired, {} if r.ok else {r.code: "service_unhealthy"}))
            # Snapshot existing producer's complete local collection only after remote polling.
            # Rows changed after this snapshot belong to the next observation, not an authoritative live guarantee.
            with self.lock:
                items = {}
                complete = True
                for key, b in self.db["breaks"].items():
                    if b.get("kind") != "founder_alert":
                        continue
                    try:
                        valid_items("local:founder", {key: b.get("category")})
                        if b.get("break_id") != key or b.get("leg") != "compliance_hold" or b.get("status") not in ("open", "resolved"):
                            raise ValueError("unsupported local break")
                        if b["status"] == "open":
                            items[key] = "compliance_hold"
                    except (ValueError, TypeError):
                        complete = False
                observed = self._now()
            observations.append(Snapshot("local:founder", "COMPLETE" if complete else "INCOMPLETE", observed, items))
            for source in ("remote:holds", "remote:expiry"):
                observations.append(Snapshot(source, "UNCONFIGURED", observed, {}))
            return state.commit(observations, self._now())
