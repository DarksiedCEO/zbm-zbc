"""
Founder alerts (ADR 0027; red team OPS-H2, OPS-H5, CH-6..CH-8): the one path that tells Andre, by email, that something
needs him. Before this the only signals were ``ALERT`` log lines and high-severity breaks, which page nobody.

What alerts (a closed list of categories; nothing else can reach the email):

=========================== ================================================== ==========
category                    source                                             severity
=========================== ================================================== ==========
ledger_unreachable          the evidence ledger's /health failed for N minutes critical
finance_high_break          an open high-severity Finance break                high
compliance_hold             a payment Compliance holds, or an open hold at     high
                            Compliance (activation / sanctions) for Andre
client_mail_failed          a client email failed for good                     high
reconciliation_mismatch     a reconciliation leg that does not match           high
payment_disputed            a Stripe dispute or chargeback                     high
unknown_paid_checkout       Stripe took money on a page Finance does not know  high
compliance_unreachable      Compliance did not answer for N minutes            high
compliance_rule_expiring    a rule client activation relies on expires within  medium
                            14 days (every client turns REFUSE on that day)
=========================== ================================================== ==========

What an email may contain: the category, the severity, the source and ONE identifier (a break id, a hold id, an
obligation id), the time it was first seen, and counts. Never an amount, a client name or email address, a card or
bank detail, a hold's reason or any free text: the identifier is checked against an id pattern and replaced by a
hash when it does not match. Details stay in the service, behind Andre's token.

How it behaves:

- **Dedupe.** A condition is emailed once. It is emailed again only as a reminder (``remind_hours`` later, if it is
  still open) or after it cleared and came back.
- **Rate limit.** At most ONE digest per ``window_min`` (default 60) and ``max_per_day`` (default 30) a day: an alert
  storm is one email listing everything new.
- **Retry with backoff.** A digest that could not be sent is kept frozen (same text, same idempotency key, so a retry
  after a lost answer is never a second email) and retried after 5, 15, 30, 60, 120, 240, 480 minutes; 8 attempts end
  about 16 hours after the first, inside Resend's 24-hour idempotency window. Then it is dropped with an ALERT log line
  and its items are due again in the next digest.
- **Fail safe.** Nothing here touches the books, the local log or the ledger: the state lives in its own small file
  (``founder_alerts.json`` in the data directory), so alerts keep working while the ledger is down, and an alert
  failure never blocks or undoes bookkeeping. Every failure is itself logged (``ALERT finance: founder_alert_*``).
- **Disabled until configured**: FOUNDER_ALERT_EMAIL, FOUNDER_ALERT_FROM (alerts@ or billing@zbestmedia.com) and the
  Resend key must all be set; anything less sends nothing and says why in the job's answer.

Driven by the ``ops-alerts`` Railway cron service every 5 minutes (deploy/scheduler, ``FIN_CRON_JOBS=alerts``):
``POST /fin/v1/jobs/founder-alerts/run``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import httpx

log = logging.getLogger("finance.alert")

STATE_NAME = "founder_alerts.json"
RESEND_URL = "https://api.resend.com/emails"
TIMEOUT_S = 20

SEVERITY = {
    "ledger_unreachable": "critical",
    "finance_high_break": "high",
    "compliance_hold": "high",
    "client_mail_failed": "high",
    "reconciliation_mismatch": "high",
    "payment_disputed": "high",
    "unknown_paid_checkout": "high",
    "compliance_unreachable": "high",
    "compliance_rule_expiring": "medium",
}
CATEGORIES = tuple(SEVERITY)
WHAT_TO_DO = {
    "ledger_unreachable": "every department refuses writes until the ledger answers: check the ledger service and its "
                          "volume (Railway)",
    "finance_high_break": "GET /fin/v1/breaks with your token",
    "compliance_hold": "GET /fin/v1/breaks (payments) or GET /compliance/v1/holds with your token, then decide",
    "client_mail_failed": "fix the cause, then POST /fin/v1/client-mail/{id}/requeue, or send it by hand",
    "reconciliation_mismatch": "GET /fin/v1/breaks; explain or resolve each break",
    "payment_disputed": "Stripe Dashboard -> Disputes: respond before the deadline (chargebacks runbook)",
    "unknown_paid_checkout": "Stripe took money on a page Finance does not know: restore-and-reimport runbook",
    "compliance_unreachable": "check the compliance service (Railway); gates stay closed while it is down",
    "compliance_rule_expiring": "re-verify and approve the rule before it expires, or every client turns REFUSE",
}
BACKOFF_MIN = (5, 15, 30, 60, 120, 240, 480)
MAX_ATTEMPTS = len(BACKOFF_MIN) + 1
MAX_LINES = 40
_REF = re.compile(r"[A-Za-z0-9._:-]{1,80}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63}){1,8}")
_FROM = re.compile(r"(?:[A-Za-z0-9 .,'&-]{1,60} <(alerts|billing)@zbestmedia\.com>|(alerts|billing)@zbestmedia\.com)")


def safe_ref(ref) -> str:
    """The one identifier an email may carry: an id, or a hash of whatever it was."""
    s = str(ref if ref is not None else "")
    if _REF.fullmatch(s) and not _EMAIL.search(s) and "@" not in s:
        return s
    return "ref-" + hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()[:16]


@dataclass(frozen=True)
class Condition:
    category: str
    source: str
    ref: str

    @property
    def key(self) -> str:
        return f"{self.source}:{self.category}:{self.ref}"

    @staticmethod
    def make(category: str, source: str, ref) -> "Condition":
        if category not in SEVERITY:
            raise ValueError("unknown alert category")
        return Condition(category, safe_ref(source)[:20], safe_ref(ref))


@dataclass
class AlertSettings:
    to: Optional[str] = None
    sender: Optional[str] = None
    window_min: int = 60
    remind_hours: int = 24
    down_min: int = 15
    max_per_day: int = 30
    problem: Optional[str] = "not configured: set FOUNDER_ALERT_EMAIL, FOUNDER_ALERT_FROM and RESEND_API_KEY"

    @property
    def enabled(self) -> bool:
        return self.problem is None


def _int(env, name: str, default: int, lo: int, hi: int) -> tuple[int, Optional[str]]:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default, None
    if not raw.isdigit() or not lo <= int(raw) <= hi:
        return default, f"{name} must be a whole number {lo}-{hi}"
    return int(raw), None


def settings_from_env(env, resend_key_present: bool) -> AlertSettings:
    """Never raises: a half or wrong configuration leaves alerts OFF with the reason (a typo here must never stop
    Finance from starting: alerts are never in the money path)."""
    s = AlertSettings()
    problems = []
    s.window_min, p1 = _int(env, "FIN_ALERT_WINDOW_MIN", 60, 5, 1440)
    s.remind_hours, p2 = _int(env, "FIN_ALERT_REMIND_HOURS", 24, 1, 168)
    s.down_min, p3 = _int(env, "FIN_ALERT_LEDGER_DOWN_MIN", 15, 5, 1440)
    s.max_per_day, p4 = _int(env, "FIN_ALERT_MAX_PER_DAY", 30, 1, 288)
    problems += [p for p in (p1, p2, p3, p4) if p]
    to = (env.get("FOUNDER_ALERT_EMAIL") or "").strip()
    sender = (env.get("FOUNDER_ALERT_FROM") or "").strip()
    if not to and not sender:
        s.problem = "not configured: set FOUNDER_ALERT_EMAIL, FOUNDER_ALERT_FROM and RESEND_API_KEY"
        return s
    if not to or not _EMAIL.fullmatch(to):
        problems.append("FOUNDER_ALERT_EMAIL must be one email address")
    if not sender or not _FROM.fullmatch(sender):
        problems.append("FOUNDER_ALERT_FROM must be alerts@zbestmedia.com or billing@zbestmedia.com "
                        "(optionally 'Z Best Media <...>')")
    if not resend_key_present:
        problems.append("RESEND_API_KEY is not set (and MAIL_FROM with it)")
    s.to, s.sender = (to or None), (sender or None)
    s.problem = "; ".join(problems) if problems else None
    return s


# ------------------------------------------------------------------------------------------------ sending

@dataclass(frozen=True)
class SendOutcome:
    sent: bool
    permanent: bool = False
    code: str = ""


class ResendAlertMail:
    """Plain-text email to Andre through Resend. Holds the key; never logs it, the address or the body."""

    def __init__(self, api_key: str, sender: str, to: str, transport: Optional[httpx.BaseTransport] = None):
        self._key, self.sender, self._to, self._transport = api_key, sender, to, transport

    def __repr__(self) -> str:
        return "ResendAlertMail(***)"

    def send(self, subject: str, text: str, idempotency_key: str) -> SendOutcome:
        body = {"from": self.sender, "to": [self._to], "subject": subject, "text": text}
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                   "Idempotency-Key": idempotency_key[:256], "User-Agent": "zbm-finance-alerts/1"}
        try:
            with httpx.Client(timeout=httpx.Timeout(TIMEOUT_S), transport=self._transport, follow_redirects=False,
                              trust_env=False) as c:
                r = c.post(RESEND_URL, json=body, headers=headers)
        except httpx.HTTPError as exc:
            return SendOutcome(False, code=type(exc).__name__[:40])
        try:
            doc = r.json()
        except ValueError:
            doc = None
        name = doc.get("name") if isinstance(doc, dict) else None
        if r.status_code == 200 and isinstance(doc, dict) and isinstance(doc.get("id"), str):
            return SendOutcome(True, code="200")
        code = f"{r.status_code}:{str(name)[:40] if name else 'no-name'}"
        if r.status_code in (409, 429) and name != "invalid_idempotent_request" or r.status_code >= 500:
            return SendOutcome(False, code=code)
        return SendOutcome(False, permanent=True, code=code)


class FixtureAlertMail:
    """Sends nothing; keeps what it was asked to send (tests, local runs). ``outcomes`` scripts the answers."""

    def __init__(self):
        self.sent: list[dict] = []
        self.attempts: list[dict] = []
        self.outcomes: list[SendOutcome] = []

    def send(self, subject: str, text: str, idempotency_key: str) -> SendOutcome:
        msg = {"subject": subject, "text": text, "idempotency_key": idempotency_key}
        self.attempts.append(msg)
        out = self.outcomes.pop(0) if self.outcomes else SendOutcome(True, code="200")
        if out.sent:
            self.sent.append(msg)
        return out


def ledger_probe(url: Optional[str], transport: Optional[httpx.BaseTransport] = None) -> Optional[bool]:
    """True when the ledger's /health answers 200 {"status": "ok"}; False when it does not; None when no ledger is
    configured (nothing to watch). Unauthenticated, no redirects, no proxy, 5 seconds."""
    if not url:
        return None
    try:
        with httpx.Client(timeout=httpx.Timeout(5), transport=transport, follow_redirects=False,
                          trust_env=False) as c:
            r = c.get(url.rstrip("/") + "/health")
        return r.status_code == 200 and isinstance(r.json(), dict) and r.json().get("status") == "ok"
    except (httpx.HTTPError, ValueError):
        return False


# ------------------------------------------------------------------------------------------------ state

def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def empty_state() -> dict:
    return {"version": 1, "open": {}, "down": {}, "last_digest_at": None, "pending": None,
            "day": {"date": None, "n": 0}, "cleared_since_digest": 0}


class StateFile:
    """The alert state, outside the hash-chained log (alerts must work while the ledger is down, and are never
    evidence). Atomic replace (temp file, fsync, rename, fsync of the directory), mode 0600. No data directory: memory
    only. An unreadable file is set aside and the state starts empty (worst case: an alert is sent twice)."""

    def __init__(self, data_dir: Optional[str]):
        self.path = os.path.join(data_dir, STATE_NAME) if data_dir else None
        self._mem: Optional[dict] = None

    def load(self) -> dict:
        if self.path is None:
            return json.loads(json.dumps(self._mem)) if self._mem is not None else empty_state()
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            return empty_state()
        except OSError:
            log.error("ALERT finance: founder_alert_state_unreadable (starting empty)")
            return empty_state()
        try:
            raw = os.read(fd, 2 * 1024 * 1024)
        finally:
            os.close(fd)
        try:
            st = json.loads(raw)
            if not isinstance(st, dict) or st.get("version") != 1 or not isinstance(st.get("open"), dict):
                raise ValueError("shape")
            return {**empty_state(), **st}
        except ValueError:
            log.error("ALERT finance: founder_alert_state_corrupt (set aside, starting empty)")
            try:
                os.replace(self.path, self.path + ".corrupt")
            except OSError:
                pass
            return empty_state()

    def save(self, st: dict) -> None:
        if self.path is None:
            self._mem = json.loads(json.dumps(st))
            return
        d = os.path.dirname(self.path)
        tmp = self.path + ".tmp"
        data = json.dumps(st, sort_keys=True, separators=(",", ":")).encode()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


# ------------------------------------------------------------------------------------------------ the engine

def track_down(st: dict, name: str, up: Optional[bool], now: datetime, down_min: int, category: str) -> list:
    """Unreachable-for-N-minutes: the first failed probe is remembered; the condition exists once it is old enough."""
    if up is None:
        st["down"].pop(name, None)
        return []
    if up:
        st["down"].pop(name, None)
        return []
    since = _parse(st["down"].get(name))
    if since is None:
        st["down"][name] = _iso(now)
        since = now
    if now - since >= timedelta(minutes=down_min):
        return [Condition.make(category, name, "unreachable")]
    return []


def _digest(items: list[dict], now: datetime, open_total: int, cleared: int) -> tuple[str, str]:
    cats = sorted({i["category"] for i in items}, key=lambda c: (c != "ledger_unreachable", c))
    worst = "critical" if any(i["severity"] == "critical" for i in items) else "high" \
        if any(i["severity"] == "high" for i in items) else "medium"
    subject = f"ZBM alert ({worst}): {len(items)} item(s): {', '.join(cats)}"[:200]
    lines = [f"Z Best Media operations alert, {_iso(now)}.", "",
             "Needs you (identifiers and categories only; the details are in the service, behind your token):", ""]
    order = sorted(items, key=lambda i: ({"critical": 0, "high": 1, "medium": 2}[i["severity"]], i["category"],
                                         i["ref"]))
    for i in order[:MAX_LINES]:
        tag = " (reminder)" if i.get("reminder") else ""
        lines.append(f"- [{i['severity']}] {i['category']}  {i['source']}  {i['ref']}  first seen {i['first_seen']}"
                     f"{tag}")
    if len(order) > MAX_LINES:
        lines.append(f"- ... and {len(order) - MAX_LINES} more")
    lines += ["", f"Open now: {open_total}. Cleared since the last alert: {cleared}.", "", "What to do:"]
    for c in cats:
        lines.append(f"- {c}: {WHAT_TO_DO[c]}")
    lines += ["", "Runbook: docs/runbooks/founder-alerts.md in DarksiedCEO/zbm-zbc.",
              "This email never carries amounts, names, email addresses, card or bank details, or hold reasons."]
    return subject, "\n".join(lines)


def run(st: dict, conditions: list, now: datetime, cfg: AlertSettings, sender) -> dict:
    """One pass: update the open set, then send at most one digest (or retry the frozen one). Mutates ``st``;
    returns a summary of counts. Never raises from the sender."""
    summary = {"open": 0, "new": 0, "cleared": 0, "due": 0, "sent": 0, "send_failed": 0, "dropped": 0,
               "waiting": None}
    current = {c.key: c for c in conditions}
    for k in list(st["open"]):
        if k not in current:
            st["open"].pop(k)
            summary["cleared"] += 1
    st["cleared_since_digest"] = int(st.get("cleared_since_digest") or 0) + summary["cleared"]
    for k, c in current.items():
        if k not in st["open"]:
            st["open"][k] = {"category": c.category, "source": c.source, "ref": c.ref,
                             "severity": SEVERITY[c.category], "first_seen": _iso(now), "last_notified": None}
            summary["new"] += 1
    summary["open"] = len(st["open"])
    remind = timedelta(hours=cfg.remind_hours)

    def due_items() -> list[dict]:
        out = []
        for k, rec in st["open"].items():
            last = _parse(rec.get("last_notified"))
            if last is None or now - last >= remind:
                out.append({**rec, "key": k, "reminder": last is not None})
        return out
    due = due_items()
    summary["due"] = len(due)
    if not cfg.enabled:
        summary["waiting"] = "alerts off"
        return summary
    day = now.strftime("%Y-%m-%d")
    if st["day"].get("date") != day:
        st["day"] = {"date": day, "n": 0}

    pend = st.get("pending")
    if pend:
        nxt = _parse(pend.get("next_attempt_at"))
        if nxt is not None and now < nxt:
            summary["waiting"] = "backoff"
            return summary
        out = _send(sender, pend["subject"], pend["text"], pend["digest_id"])
        pend["attempts"] = int(pend.get("attempts") or 0) + 1
        if out.sent:
            _delivered(st, pend["keys"], now)
            summary["sent"] += 1
        elif out.permanent or pend["attempts"] >= MAX_ATTEMPTS:
            log.error("ALERT finance: founder_alert_dropped digest=%s attempts=%d code=%s", pend["digest_id"],
                      pend["attempts"], out.code)
            st["pending"] = None
            summary["dropped"] += 1
        else:
            pend["next_attempt_at"] = _iso(now + timedelta(minutes=BACKOFF_MIN[min(pend["attempts"], len(BACKOFF_MIN))
                                                                                 - 1]))
            log.error("ALERT finance: founder_alert_send_failed digest=%s attempt=%d code=%s", pend["digest_id"],
                      pend["attempts"], out.code)
            summary["send_failed"] += 1
        return summary

    if not due:
        return summary
    last = _parse(st.get("last_digest_at"))
    if last is not None and now - last < timedelta(minutes=cfg.window_min):
        summary["waiting"] = "window"
        return summary
    if st["day"]["n"] >= cfg.max_per_day:
        summary["waiting"] = "daily cap"
        return summary
    subject, text = _digest(due, now, len(st["open"]), int(st.get("cleared_since_digest") or 0))
    keys = sorted(i["key"] for i in due)
    digest_id = "zbm-alert-" + hashlib.sha256(json.dumps([keys, _iso(now)]).encode()).hexdigest()[:32]
    st["last_digest_at"] = _iso(now)
    st["day"]["n"] += 1
    out = _send(sender, subject, text, digest_id)
    if out.sent:
        _delivered(st, keys, now)
        summary["sent"] += 1
    elif out.permanent:
        log.error("ALERT finance: founder_alert_dropped digest=%s attempts=1 code=%s", digest_id, out.code)
        summary["dropped"] += 1
    else:
        st["pending"] = {"digest_id": digest_id, "subject": subject, "text": text, "keys": keys, "attempts": 1,
                         "first_attempt_at": _iso(now), "next_attempt_at": _iso(now + timedelta(minutes=BACKOFF_MIN[0]))}
        log.error("ALERT finance: founder_alert_send_failed digest=%s attempt=1 code=%s", digest_id, out.code)
        summary["send_failed"] += 1
    return summary


def _send(sender, subject: str, text: str, key: str) -> SendOutcome:
    try:
        out = sender.send(subject, text, key)
    except Exception as exc:  # noqa: BLE001 - an adapter that raises is a failed attempt, never a crash
        return SendOutcome(False, code=type(exc).__name__[:40])
    return out if isinstance(out, SendOutcome) else SendOutcome(False, code="adapter_answer")


def _delivered(st: dict, keys: list, now: datetime) -> None:
    for k in keys:
        if k in st["open"]:
            st["open"][k]["last_notified"] = _iso(now)
    st["pending"] = None
    st["cleared_since_digest"] = 0
    log.info("founder-alerts: digest sent items=%d", len(keys))


@dataclass
class Dispatcher:
    """The service's handle: settings, the sender, the state file, and one run at a time."""

    cfg: AlertSettings
    state: StateFile
    sender: object = None
    probe: Callable[[], Optional[bool]] = field(default=lambda: None)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def run(self, now: datetime, conditions: list, compliance_up: Optional[bool]) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"busy": True}
        try:
            st = self.state.load()
            try:
                up = self.probe()
            except Exception:  # noqa: BLE001
                up = False
            conds = list(conditions)
            conds += track_down(st, "ledger", up, now, self.cfg.down_min, "ledger_unreachable")
            conds += track_down(st, "compliance", compliance_up, now, self.cfg.down_min, "compliance_unreachable")
            summary = run(st, conds, now, self.cfg, self.sender)
            try:
                self.state.save(st)
                summary["state_saved"] = True
            except OSError as exc:
                log.error("ALERT finance: founder_alert_state_write_failed (%s)", type(exc).__name__)
                summary["state_saved"] = False
            summary["enabled"] = self.cfg.enabled
            if not self.cfg.enabled:
                summary["why_off"] = self.cfg.problem
            return summary
        finally:
            self._lock.release()
