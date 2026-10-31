"""
Backups and restores (red team CH-3; runbook docs/runbooks/restore-and-reimport.md; ADR 0026).

1. A coordinated snapshot. Railway backs up each volume on its own (daily schedules at independent times; manual
   backups whenever asked) and says nothing about consistency across volumes or while a service writes. So:
   ``POST /fin/v1/backup/quiesce`` (Andre) records a snapshot MARKER (the local log head, the journal heads) on the
   evidence ledger and refuses every Finance write for up to N minutes (503 ``QUIESCED``: Stripe and the cron service
   simply retry later). Andre then takes manual backups of every department volume and of the LEDGER LAST, and
   ``POST /fin/v1/backup/resume`` with the marker id ends the window and says whether Finance's log stayed at the marker
   (``consistent``). With the ledger newest, a restore of that set can only be RECONCILABLE, never FATAL (a ledger older
   than a department is the red team's R2, which no reconcile can fix).

2. The restore guard. After a restore the ledger holds evidence the restored Finance log does not. The reconcile plan
   (``GET /fin/v1/reconcile``) now lists ``money_at_risk``: the voidable ledger events that are money (payments,
   invoices, refunds, credit notes, journal entries). Andre's reconcile refuses while any is listed, unless he passes
   ``money_events_acknowledged: true``; then it first writes the RESTORE LOCKDOWN to disk and commits itself with a
   ``restore_reimport_required`` break (one line). While the lockdown is on no money goes out (AEGIS r3); only Andre
   lifts it, once the re-import covers every day and every lost item is rebuilt or disposed of. A cleanup that
   silently forgets paid invoices is not possible.

3. The Stripe re-import (``POST /fin/v1/stripe/reimport``, Andre). Lists the payments, refunds and disputes Stripe
   created in a period (read only) and books every one Finance does not have, through the SAME code a webhook uses
   (idempotent per Stripe object; Compliance asked per payment). A page Finance lost is re-linked from Stripe's own
   Checkout Session (same invoice id, amount and currency) so the payment matches its invoice again. Always a dry run
   first: the real run names the dry run's ``plan_sha256`` and is refused if Stripe's answer changed since.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

import chart as C
import money as M
import reasons as R
import restore_evidence as RE
from clock import iso, parse_iso
from errors import Conflict, Invalid, NotFound, Unavailable
from intelligences import i01_journal as J
from intelligences import i02_receivables as I2
from intelligences import i04_payout_run as I4
from intelligences import i10_evidence_audit as i10
from ledger import derived_id, payload_sha256
from ports import RailLookup, StripeDispute, StripeList, StripePayment
from service import EVIDENCE as EVIDENCE_ACTOR, RUNNABLE as RUNNABLE_STATES
from service import Gather, InvalidReasons, Op, PostingRefused, Refused, rid, sha
from svc_credits import CN_SUMMARY, VOID_SUMMARY, credit_note_payload, void_payload

ACTOR = I2.ACTOR
MAX_REIMPORT_DAYS = 92
alert_log = logging.getLogger("finance.alert")
log = logging.getLogger("finance")
# ledger event types that are money: a reconcile that voids any of them needs the re-import
MONEY_EVENT_TYPES = ("journal_entry_posted", "receipt_matched", "receipt_unapplied", "invoice_issued",
                     "invoice_approved", "invoice_voided", "credit_note_issued", "credit_note_refund_linked",
                     "stripe_event_applied", "stripe_refund_booked", "stripe_checkout_created", "dispute_opened",
                     "dispute_closed", "compliance_hold_opened", "client_receipt_created")
# AEGIS R2-1/R2-4: effects a restore can lose that Stripe does not hold. Each is an INTENT on the ledger, recorded
# before the local commit; after the commit Finance records an ``effect_committed`` mark naming it (svc_restore
# ``_mark_committed``). Only an intent with its authentic mark is ever rebuilt; any other one is an item for Andre.
EFFECT_TYPES = ("credit_note_issued", "invoice_voided", "deadline_recorded", "client_mail_queued")
# AEGIS r4 R4-3: money that went OUT (or toward the payout rail): never rebuilt, always an item for Andre (moved /
# not_moved, from the bank or rail record), and its subject (transfer, payout item or batch, refund, media buy) is
# FENCED: never executed, submitted, released or repaid again unless Andre says it did not move
MONEY_OUT_TYPES = ("treasury_posting_committed", "item_submitted", "batch_release_started", "refund_paid",
                   "media_vendor_payment_recorded")
COMMIT_TYPE = "effect_committed"
COMMIT_SUMMARY = re.compile(r"Committed (fin-[a-z0-9]{1,12}-[0-9a-f]{40}) at line (\d+) ([0-9a-f]{64}) key "
                            r"([0-9a-f]{12}|-) mac ([0-9a-f]{64}|-)")
MAIL_SUMMARY = re.compile(r"Client email queued \(([a-z_]+)\) for (\S+)")
DONE = ("rebuilt", "disposed", "sent_before_restore", "follows_parent", "follows_refund", "follows_movement")
PAUSE_ALERT_EVERY_S = 300
# AEGIS r3 R3-1/R3-2: the restore lockdown, a durable state in the data directory written BEFORE a restore's reconcile
# changes anything; while it is on, no money goes out (credit notes, voids, Stripe pages, payouts, refunds)
LOCKDOWN_FILE = "restore_lockdown.json"
# AEGIS r5 R5-1: after the lift, money out stays refused until a reconciliation run AFTER the lift is fully clean
POST_PHASE = "post_restore_reconcile_required"
BANK_ACCOUNTS = (("zbc", "1020"), ("zbc", "1010"), ("zbm", "1010"))
RECON_LEGS = ("L1", "L2", "L3", "L4")
LOCKDOWN_MSG = ("RESTORE_LOCKDOWN: Finance was restored from a backup; no {what} until Andre lifts the lockdown "
                "(POST /fin/v1/restore/lockdown/lift, after the re-import and every restore item; "
                "docs/runbooks/restore-and-reimport.md section 5)")


class RestoreMixin:

    # ================================================================== 1. quiesce + snapshot marker

    def quiesced(self) -> Optional[dict]:
        """The open backup window, if any. AEGIS R2-6: a window always ENDS (FIN_BACKUP_PAUSE_MAX_MIN, at most 120
        minutes): live, and after a restart (the persisted marker's ``until``). A restarted Finance inside the
        window stays paused (fail closed, AEGIS F6) and logs an ALERT every 5 minutes; a marker left open past its
        window no longer pauses anything but alerts the same way until Andre resumes it."""
        now = self._now()
        mono = self._mono()
        q = getattr(self, "_quiesce", None)
        if q is not None:
            # AEGIS R3-7: the window ends at its wall-clock end OR after its length on the monotonic clock (a clock
            # stepped back never stretches it)
            if now < q["until"] and (q.get("mono_until") is None or mono < q["mono_until"]):
                return q
            self._quiesce = None
            self.__dict__.setdefault("_ended_windows", set()).add(q["marker_id"])   # never re-paused in this process
        cap_s = 60 * int(getattr(self.cfg, "backup_pause_max_min", 30))
        for m in self.db["backup_markers"].values():
            if m.get("status") == "quiesced":
                until = parse_iso(m["until"]) if m.get("until") else now
                booted = mono - getattr(self, "_boot_mono", mono)
                if now < until and booted < cap_s and q is None and \
                        m["marker_id"] not in self.__dict__.get("_ended_windows", ()):
                    self._pause_alert(f"paused_for_backup marker={m['marker_id']} until={m['until']} (restarted inside "
                                      "the window)")
                    return {"marker_id": m["marker_id"], "restarted": True, "until": until}
                self._pause_alert(f"backup_marker_left_open marker={m['marker_id']} window ended {m.get('until')}: "
                                  "resume it (POST /fin/v1/backup/resume)")
        return None

    def _mono(self):
        return getattr(self, "_monotonic", _time.monotonic)()

    def _pause_alert(self, text: str) -> None:
        last = getattr(self, "_pause_alert_at", None)
        now = self._now()
        if last is None or (now - last).total_seconds() >= PAUSE_ALERT_EVERY_S:
            self._pause_alert_at = now
            alert_log.error("ALERT finance: %s", text)

    def pause_status(self) -> dict:
        q = self.quiesced()
        left = [m["marker_id"] for m in self.db["backup_markers"].values() if m.get("status") == "quiesced"]
        return {"paused": q is not None, "marker_id": (q or {}).get("marker_id"),
                "until": iso(q["until"]) if q and q.get("until") else None,
                "restarted_inside_window": bool(q and q.get("restarted")), "markers_left_open": left}

    def _heads(self) -> dict:
        shas = self.log.line_shas()
        return {"log_head_seq": len(shas), "log_head_sha256": shas[-1] if shas else None,
                "journal_heads": {k: v for k, v in sorted(self.heads.items())}}

    def backup_quiesce(self, request_id: str, minutes: int) -> dict:
        cap = getattr(self.cfg, "backup_pause_max_min", 30)
        if minutes > cap:
            raise Invalid(f"a backup pause lasts at most {cap} minutes (FIN_BACKUP_PAUSE_MAX_MIN)")
        with self.lock:
            if self.quiesced():
                raise Conflict("Finance is already quiesced for a backup; resume it first")
            now = self._now()
            mid = rid("bkp", request_id, iso(now))
            marker = {"marker_id": mid, "requested_at": iso(now), "until": iso(now + timedelta(minutes=minutes)),
                      "status": "quiesced", **self._heads()}
            op = Op(self, f"bkp|{request_id}", "andre", mid)
            op.record(derived_id("bkp", mid), "backup_marker_recorded", "andre", mid,
                      {"marker_id": mid, "log_head_seq": marker["log_head_seq"],
                       "log_head_sha256": marker["log_head_sha256"], "journal_heads": marker["journal_heads"]},
                      f"Backup marker at local log line {marker['log_head_seq']}: Finance writes paused")
            op.put("backup_markers", mid, marker)
            self._commit(op)
            # the marker line itself is the last line a consistent snapshot may hold
            marker = {**marker, **self._heads()}
            self._quiesce = {"marker_id": mid, "until": now + timedelta(minutes=minutes),
                             "mono_until": self._mono() + 60 * minutes,
                             "log_head_seq": marker["log_head_seq"], "log_head_sha256": marker["log_head_sha256"]}
            return {"marker": marker, "writes_refused_until": marker["until"],
                    "next": "take manual backups of finance-data, legal-data, compliance-data and seo-data, then "
                            "ledger-data LAST; then POST /fin/v1/backup/resume with this marker_id"}

    def backup_resume(self, request_id: str, marker_id: str) -> dict:
        with self.lock:
            m = self.db["backup_markers"].get(marker_id)
            if m is None:
                raise NotFound("no such backup marker")
            q = getattr(self, "_quiesce", None)
            heads = self._heads()
            if q is None or q["marker_id"] != marker_id:
                reason = "restarted_or_restored"       # this process never opened the window (a deploy, a restore)
            elif self._now() >= q["until"]:
                reason = "window_expired"
            elif heads["log_head_sha256"] != q["log_head_sha256"]:
                reason = "log_moved"
            else:
                reason = None
            consistent = reason is None
            self._quiesce = None
            op = Op(self, f"bkpr|{request_id}", "andre", marker_id)
            op.record(derived_id("bkpr", marker_id), "backup_marker_closed", "andre", marker_id,
                      {"marker_id": marker_id, "consistent": consistent, "reason": reason,
                       "log_head_seq": heads["log_head_seq"]},
                      f"Backup window closed ({'consistent' if consistent else reason})")
            op.put("backup_markers", marker_id, {**m, "status": "closed", "closed_at": iso(self._now()),
                                                 "consistent": consistent, "reason": reason})
            self._commit(op)
            detail = {None: "the backup set taken in this window is consistent for Finance",
                      "restarted_or_restored": "closed after a restart or a restore (expected after a restore; after a "
                                               "deploy inside the window, take the backup set again)",
                      "window_expired": "the window ran out before resume: take the backup set again",
                      "log_moved": "Finance wrote during the window: take the backup set again"}[reason]
            return {"marker_id": marker_id, "consistent": consistent, "reason": reason,
                    "log_head_seq_at_marker": m["log_head_seq"], "detail": detail}

    # ================================================================== 2. effects, commit marks, the restore guard

    @staticmethod
    def _derive_mark_key(k: bytes) -> bytes:
        return hmac.new(k, b"zbm-finance-commit-mark-v1", hashlib.sha256).digest()

    @staticmethod
    def _key_id(dk: bytes) -> str:
        return hashlib.sha256(b"zbm-finance-commit-mark-key-id|" + dk).hexdigest()[:12]

    def _evidence_key(self) -> Optional[bytes]:
        """The key of the MAC on commit marks (AEGIS R2-5), derived from FIN_CONTACT_HASH_KEY (domain-separated)."""
        k = getattr(self.cfg, "contact_hash_key", None)
        return self._derive_mark_key(k) if k else None

    def _evidence_keys(self) -> dict:
        """AEGIS R3-9/R3-10: key id -> mark key, the current key and every old key kept for verification (a rotated
        FIN_CONTACT_HASH_KEY lists the new key first, then the old ones)."""
        out = {}
        for k in [getattr(self.cfg, "contact_hash_key", None), *(getattr(self.cfg, "contact_hash_old_keys", ()) or ())]:
            if k:
                dk = self._derive_mark_key(k)
                out.setdefault(self._key_id(dk), dk)
        return out

    def _commit_mac(self, intent: str, seq: int, line_sha: str, dk: Optional[bytes] = None) -> str:
        k = dk if dk is not None else self._evidence_key()
        return hmac.new(k, f"{intent}|{seq}|{line_sha}".encode(), hashlib.sha256).hexdigest() if k else "-"

    def restore_rebuild_status(self) -> dict:
        """AEGIS R3-9: whether a restore can rebuild anything automatically (marks need FIN_CONTACT_HASH_KEY)."""
        keys = self._evidence_keys()
        dk = self._evidence_key()
        unknown = sorted({it["why"].split("key id ", 1)[1][:12] for b in self._restore_open()
                          for it in (b.get("items") or {}).values()
                          if it.get("why") and "unknown key id " in it["why"]})
        reason = None if dk else "FIN_CONTACT_HASH_KEY is not set: commit marks are unauthenticated"
        if unknown:
            reason = "restore items carry marks of a key that is not configured (rotated away): " + ",".join(unknown)
        return {"unavailable": reason is not None, "reason": reason, "key_id": self._key_id(dk) if dk else None,
                "old_key_ids": sorted(k for k in keys if not dk or k != self._key_id(dk))}

    def _mark_committed(self, op: Op, seq: int, line_sha: str) -> None:
        """Called by ``_commit`` once a line is appended: each EFFECT intent in it gets its ``effect_committed`` mark.
        A mark that cannot be written is retried by the hourly job; until then a restore treats the intent as
        unproven (an item for Andre, never booked by itself)."""
        for n in getattr(op, "named", []):
            if n.get("event_type") in EFFECT_TYPES:
                self._write_commit_mark(n["event_id"], seq, line_sha)

    def _write_commit_mark(self, intent: str, seq: int, line_sha: str) -> bool:
        dk = self._evidence_key()
        kid = self._key_id(dk) if dk else "-"
        mac = self._commit_mac(intent, seq, line_sha, dk)
        try:
            self._record(derived_id("cmt", intent), COMMIT_TYPE, EVIDENCE_ACTOR, intent,
                         {"intent_event_id": intent, "line_seq": seq, "line_sha256": line_sha, "key_id": kid,
                          "mac": mac},
                         f"Committed {intent} at line {seq} {line_sha} key {kid} mac {mac}")
            return True
        except Exception:  # noqa: BLE001 - the effect stands; the mark is retried (and a restore asks Andre)
            pend = self.__dict__.setdefault("_unmarked", [])
            if (intent, seq, line_sha) not in pend:
                pend.append((intent, seq, line_sha))
            alert_log.error("ALERT finance: commit_mark_unwritten intent=%s line=%s", intent, seq)
            return False

    def _retry_commit_marks(self) -> int:
        """AEGIS R3-4: the retry queue is the local log itself (each line names its evidence), so it survives a
        restart: every EFFECT intent of a committed line whose mark the ledger lacks is written again."""
        if not self.log.in_memory:
            try:
                self._unmarked = self._unmarked_from_log(self.recorder.client.entries())
            except Exception:  # noqa: BLE001 - unreadable now: keep the queue as it is, the next run retries
                pass
        pend = list(self.__dict__.get("_unmarked", []))
        done = [p for p in pend if self._write_commit_mark(*p)]
        self._unmarked = [p for p in self.__dict__.get("_unmarked", []) if p not in done]
        return len(done)

    def _unmarked_from_log(self, entries: list) -> list:
        have = {e.get("event_id") for e in entries if e.get("event_type") == COMMIT_TYPE}
        out = []
        for rec, line_sha in zip(self.log.iter_records(), self.log.line_shas()):
            for n in rec["data"].get("evidence") or []:
                if n.get("event_type") in EFFECT_TYPES and derived_id("cmt", n["event_id"]) not in have:
                    out.append((n["event_id"], rec["seq"], line_sha))
        return out

    # ------------------------------------------------------------------ the restore lockdown (AEGIS r3)

    def _lockdown_path(self) -> Optional[str]:
        d = getattr(self.cfg, "data_dir", None)
        return os.path.join(d, LOCKDOWN_FILE) if d and not self.log.in_memory else None

    def _lockdown_read(self) -> Optional[dict]:
        path = self._lockdown_path()
        if path is None or not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as fh:
                st = json.loads(fh.read(65536).decode())
            if not isinstance(st, dict) or st.get("state") not in ("on", "clear", "lifted", POST_PHASE):
                raise ValueError("unknown state")
            return st
        except (OSError, ValueError):
            # unreadable: fail closed
            return {"state": "on", "detected": True, "build_incomplete": True, "breaks": [],
                    "why": "the lockdown file could not be read (fail closed)"}

    def _lockdown_write(self, st: dict) -> None:
        """Atomic and durable (temp file, fsync, rename, directory fsync). In memory only without a data directory."""
        path = self._lockdown_path()
        if path is not None:
            tmp = f"{path}.tmp"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, json.dumps(st, sort_keys=True).encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, path)
                dfd = os.open(os.path.dirname(path), os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError as exc:
                raise Unavailable(f"the restore lockdown could not be written ({type(exc).__name__}); nothing was "
                                  "done") from None
        self._lockdown = st

    def restore_lockdown(self) -> Optional[dict]:
        st = getattr(self, "_lockdown", None)
        return st if st and st.get("state") == "on" else None

    def restore_locked(self) -> bool:
        """AEGIS r4 R4-5: locked while the lockdown is on OR any restore break is still open (either alone locks)."""
        return self.restore_lockdown() is not None or bool(self._restore_open())

    def lockdown_status(self) -> dict:
        st = self.restore_lockdown()
        return {"on": self.restore_locked(), "detected": bool(st and st.get("detected")),
                "build_incomplete": bool(st and st.get("build_incomplete"))
                or any(b.get("build_incomplete") for b in self._restore_open()),
                "why": (st or {}).get("why"),
                "post_restore_reconcile_required": self.restore_post_phase() is not None}

    def restore_post_phase(self) -> Optional[dict]:
        st = getattr(self, "_lockdown", None)
        return st if st and st.get("state") == POST_PHASE else None

    def _lockdown_alert(self) -> None:
        if not self.restore_locked():
            return
        self._lockdown_escalate()
        last = getattr(self, "_lockdown_alert_at", None)
        now = self._now()
        if last is None or (now - last).total_seconds() >= PAUSE_ALERT_EVERY_S:
            self._lockdown_alert_at = now
            st = self.restore_lockdown() or {}
            alert_log.error("ALERT finance: restore_lockdown on%s%s: no credit note, void, Stripe page, payout or "
                            "refund until Andre lifts it (runbook restore-and-reimport section 5)",
                            " (detected at start: ledger anchors beyond the local log)" if st.get("detected") else "",
                            " build_incomplete" if self.lockdown_status()["build_incomplete"] else "")

    def _lockdown_escalate(self) -> None:
        """AEGIS r4 R4-8: a lockdown longer than 2 business days escalates (MSA 4.12(f)/(g) and 4.10 clocks keep
        running), and again every day after, naming the contract deadlines due within 10 business days."""
        from svc_credits import add_business_days
        st = self.restore_lockdown() or {}
        opened = st.get("opened_at") or min([b.get("opened_at") or "" for b in self._restore_open()] or [""])
        if not opened:
            return
        today = self._business_today()
        start = date.fromisoformat(self._business_date(opened))
        if today < add_business_days(start, 2) or getattr(self, "_lockdown_escalated_on", None) == today:
            return
        self._lockdown_escalated_on = today
        horizon = add_business_days(today, 10).isoformat()
        due = sorted(f"{b['leg']}:{b.get('invoice_id') or b.get('mail_id') or b['break_id']} by {b['due_by']}"
                     for b in self.db["breaks"].values() if b["status"] != "resolved" and b.get("due_by")
                     and b["due_by"] <= horizon)
        alert_log.error("ALERT finance: restore_lockdown_long since %s (more than 2 business days): contract "
                        "deadlines due within 10 business days: %s. MSA 4.12(g) cancels and credits of UNPAID "
                        "invoices are still allowed (Andre); refunds due go through the Stripe Dashboard", start,
                        "; ".join(due[:20]) or "none")

    def _lost_anchors(self, entries: list, after: int) -> int:
        """Finance log anchors on the ledger (from index ``after``) that name no line of the local log and that no
        honoured reconcile declared void (AEGIS r4 R4-10): lines a restore (or a failed write) took away."""
        shas = self.log.line_shas()
        mine = {i10.anchor_id(self.log.epoch, rec["seq"], s) for rec, s in zip(self.log.iter_records(), shas)}
        for rec in self.log.iter_records():
            for kind, r in rec["data"].get("ops", []):
                if kind == "reconcile":
                    mine.update((r.get("payload") or {}).get("void_event_ids") or [])
        return sum(1 for i, e in enumerate(entries) if i >= after and e.get("department") == i10.DEPT
                   and e.get("event_type") == i10.ANCHOR_TYPE and e.get("event_id") not in mine)

    def _restore_boot(self) -> None:
        """At start: the lockdown is read from disk; without it (or after a lift) the ledger is checked for lines the
        local log lacks, and any there turn the lockdown on (fail closed). Commit marks still owed are written."""
        self._boot_mono = self._mono()
        self._unmarked = []
        self._lockdown = self._lockdown_read()
        if self.log.in_memory:
            return
        if getattr(self.cfg, "contact_hash_key", None) is None:
            alert_log.warning("WARNING finance: FIN_CONTACT_HASH_KEY is not set: commit marks are unauthenticated and "
                              "a restore rebuilds nothing automatically (restore_rebuild_unavailable)")
        try:
            entries = self.recorder.client.entries()
        except Exception:  # noqa: BLE001
            raise RuntimeError("refusing to start: the evidence ledger could not be read to check for a restore "
                               "lockdown") from None
        st = self._lockdown
        # in reconcile mode nothing writes anyway, and a normal start refuses unreconciled lines: detect afterwards
        if not self.reconcile_mode and (st is None or st.get("state") != "on"):
            lost = self._lost_anchors(entries, int((st or {}).get("ack_ledger_len") or 0))
            if lost:
                new = {"state": "on", "detected": True, "build_incomplete": True, "breaks": [],
                       "lost_anchors": lost, "opened_at": iso(self._now())}
                try:
                    self._lockdown_write(new)
                except Unavailable:
                    self._lockdown = new                      # on in memory at least
        if self._restore_open() and self.restore_lockdown() is None:
            # AEGIS r4 R4-5: an open restore break always means a lockdown, whatever the file says
            new = {"state": "on", "detected": True, "build_incomplete": False, "opened_at": iso(self._now()),
                   "breaks": sorted(b["break_id"] for b in self._restore_open()),
                   "why": "a restore break is open"}
            try:
                self._lockdown_write(new)
            except Unavailable:
                self._lockdown = new
        if not self.reconcile_mode:
            self._unmarked = self._unmarked_from_log(entries)
            self._retry_commit_marks()
        self._lockdown_alert()

    def _lockdown_enter(self, plan: dict, bid: str, incomplete: bool) -> None:
        """Write-ahead: called by the reconcile BEFORE it changes anything; if this fails nothing is done."""
        old = self.restore_lockdown() or {}
        self._lockdown_write({"state": "on", "detected": False, "build_incomplete": bool(incomplete),
                              "breaks": sorted(set(old.get("breaks") or []) | {bid}),
                              "restore_point": plan.get("restore_point"), "opened_at": iso(self._now())})
        self._lockdown_alert_at = None
        self._lockdown_alert()

    def _lockdown_clear_after_reconcile(self) -> None:
        """A reconcile with nothing at risk: the lines it declared void are acknowledged (no lockdown for them)."""
        if self._restore_open() or self.restore_post_phase() is not None or (
                self.restore_lockdown() is not None and not (self.restore_lockdown() or {}).get("detected")):
            return       # AEGIS r4 R4-5 / r6 R6-2: never while a restore break is open or the post-restore phase runs
        try:
            n = len(self.recorder.client.entries())
            self._lockdown_write({"state": "clear", "ack_ledger_len": n, "at": iso(self._now())})
        except Exception:  # noqa: BLE001 - not written: the next start detects the void lines (fail closed)
            pass

    def start_watch(self, interval_s: int = 60) -> threading.Thread:
        """AEGIS R3-7: the pause and lockdown ALERT lines repeat every 5 minutes without needing a request."""
        def loop():
            while not self._closed:
                self._watch_tick()
                _time.sleep(interval_s)
        t = threading.Thread(target=loop, name="finance-watch", daemon=True)
        t.start()
        return t

    def _watch_tick(self) -> None:
        try:
            with self.lock:
                self.quiesced()
                self._lockdown_alert()
                self._parked_alert()
        except Exception:  # noqa: BLE001 - the watch never dies on one bad tick
            log.error("finance watch tick failed")

    def money_at_risk(self, void_event_ids, restore_point: Optional[dict] = None) -> list[dict]:
        """Voidable ledger events (a reconcile plan) that are money, plus (AEGIS R2-4) every credit note, void,
        deadline and client email the ledger recorded after the restored state: none is dropped without the
        acknowledgement and the restore break."""
        ids = set(void_event_ids or ())
        idx = (restore_point or {}).get("ledger_index")
        if not ids and idx is None:
            return []
        try:
            entries = self.recorder.client.entries()
        except Exception:  # noqa: BLE001 - unreadable: every voidable event counts as money (fail closed)
            return [{"event_id": i, "event_type": "unknown"} for i in sorted(ids)] or \
                [{"event_id": "unknown", "event_type": "ledger_unreadable"}]
        out = {e["event_id"]: {"event_id": e["event_id"], "event_type": e["event_type"],
                               "summary": str(e.get("summary") or "")[:120]}
               for i, e in enumerate(entries)
               if (e.get("event_id") in ids and e.get("event_type") in MONEY_EVENT_TYPES)
               or (idx is not None and i > idx and e.get("department") == i10.DEPT
                   and e.get("event_type") in EFFECT_TYPES)}
        return sorted(out.values(), key=lambda x: x["event_id"])

    def restore_guard(self, plan: dict, acknowledged: bool) -> None:
        if plan.get("money_at_risk") and not acknowledged:
            raise Conflict("RESTORE_GUARD: this reconcile would declare void ledger evidence of money Finance no "
                           f"longer holds ({len(plan['money_at_risk'])} event(s): payments, invoices, refunds, credit "
                           "notes, voids, deadlines or client emails made after the backup). They are NOT deleted at "
                           "Stripe: reconcile with money_events_acknowledged=true, then run the Stripe re-import for "
                           "the period (docs/runbooks/restore-and-reimport.md). Never treat this as a cleanup.",
                           money_at_risk=plan["money_at_risk"][:50])

    def restore_point(self) -> dict:
        """AEGIS F1: the point in time the restored state reflects, read BEFORE the reconcile line is appended: the
        local log's last line (its commit time and its anchor's position on the ledger)."""
        shas = self.log.line_shas()
        n, at = len(shas), None
        for rec in self.log.iter_records():
            at = rec.get("at") or at
        idx = None
        if n:
            aid = i10.anchor_id(self.log.epoch, n, shas[-1])
            try:
                for i, e in enumerate(self.recorder.client.entries()):
                    if e.get("event_id") == aid:
                        idx = i
                        break
            except Exception:  # noqa: BLE001 - unreadable ledger: no position (AEGIS r4 R4-6: the reconcile refuses)
                return {"log_head_seq": n, "log_head_sha256": shas[-1], "at": at, "ledger_index": None,
                        "date": self._business_date(at), "read_failed": True}
        return {"log_head_seq": n, "log_head_sha256": shas[-1] if n else None, "at": at, "ledger_index": idx,
                "date": self._business_date(at)}

    def _business_date(self, at) -> str:
        from zoneinfo import ZoneInfo
        try:
            d = datetime.fromisoformat(str(at).replace("Z", "+00:00")) if at else self._now()
        except ValueError:
            d = self._now()
        return d.astimezone(ZoneInfo(self.cfg.business_tz)).date().isoformat()

    def _restore_open(self) -> list[dict]:
        return [b for b in self.db["breaks"].values()
                if b.get("leg") == "restore_reimport_required" and b["status"] != "resolved"]

    def _restore_refusal(self, what: str, invoice_id: Optional[str] = None, unpaid_only: bool = False) -> bool:
        """AEGIS r3/r4: while locked (the lockdown on, or a restore break open) every credit note, void and new
        Stripe page is refused, for every invoice. One narrow exception keeps MSA 4.12(g) meetable (R4-8): Andre may
        void an UNPAID invoice, or credit one down without any refund, when the restore's item list is complete and
        nothing the restore lost touches that invoice (no money moves). Returns True when the exception applied."""
        if not self.restore_locked():
            return False
        if unpaid_only and what in ("void", "credit note") and invoice_id:
            why = self._lockdown_exception_refusal(invoice_id)
            if why is None:
                return True
            raise Conflict(LOCKDOWN_MSG.format(what=what) + f"; the unpaid-invoice exception does not apply: {why}")
        raise Conflict(LOCKDOWN_MSG.format(what=what))

    def _lockdown_exception_refusal(self, invoice_id: str) -> Optional[str]:
        if self.lockdown_status()["build_incomplete"] or (self.restore_lockdown() or {}).get("detected"):
            return "the list of what the restore lost is not complete"
        inv = self.db["invoices"].get(invoice_id) or {}
        if inv.get("status") != "issued" or self._paid_gross(invoice_id) > 0 or M.D(inv.get("refunded") or "0.00") > 0:
            return "the invoice is not unpaid"
        for b in self.db["breaks"].values():
            if b.get("leg") == "restore_reimport_required" and any(
                    it.get("invoice_id") == invoice_id for it in (b.get("items") or {}).values()):
                return "something the restore lost touches this invoice"
        if invoice_id in self._fenced():
            return "money moved on this invoice after the backup"
        # AEGIS r5 R5-4: only once nothing can still show this invoice was paid after the backup
        for b in self._restore_open():
            if invoice_id in (b.get("affected_invoices") or []):
                return "a payment page or issue of this invoice was recorded after the backup"
            if not self._covers(b.get("covered") or [], b.get("since"), b.get("opened_on")):
                return "the Stripe re-import has not covered every day since the backup"
            if not self._refeed_covers(b):
                return "Andre has not confirmed the bank lines since the backup were fed again"
            # AEGIS r6 R6-3: no bank line since the backup refers to this invoice (matched or not)
            recent = [r for r in self.db["receipts"].values() if str(r.get("value_date") or "")[:10] >= (b.get("since") or "")]
            if any(r.get("invoice_id") == invoice_id or r.get("reference_token") == invoice_id for r in recent):
                return "a bank line since the backup refers to this invoice"
            # AEGIS r7 R7-4: an unattributed bank line since the backup could be this invoice's payment
            # (only that client's invoices when the line names its client; every invoice when it cannot be told)
            if any(r.get("status") == "unapplied" and not r.get("invoice_id") and r.get("entity") == inv.get("entity")
                   and (not r.get("client_id") or r.get("client_id") == inv.get("client_id")) for r in recent):
                return "an unattributed bank line since the backup is not resolved (apply or return it first)"
        # AEGIS r7 R7-3: the bank's own balance equals the books for every bank account (nothing paid is unfed)
        for ent_, acct in BANK_ACCOUNTS:
            why = self._bank_balance_mismatch(ent_, acct)
            if why:
                return why
        return None

    def _bank_balance_mismatch(self, ent_: str, acct: str, declared=None) -> Optional[str]:
        """AEGIS r7 R7-3: the bank feed's own balance (independent of Andre) vs Andre's declared closing balance, or
        vs the books less the money recorded as moved out of that account and not booked yet (signed: an unfed
        payment IN can never hide behind a lost payment OUT)."""
        try:
            bb = self.ports.bank.balance(ent_, acct)
        except Exception:  # noqa: BLE001
            bb = None
        if not getattr(bb, "available", False):
            return f"{ent_}:{acct} the bank balance could not be read"
        bank = M.D(bb.balance)
        if declared is not None:
            return None if declared == bank else f"{ent_}:{acct} statement closing {M.sfmt(declared)}, bank {M.sfmt(bank)}"
        books = self.bal(acct, entity=ent_)
        out = self._pending_bank_out().get((ent_, acct), M.ZERO)
        if M.q(books - out) != bank:
            return (f"{ent_}:{acct} books {M.sfmt(books)} less {M.sfmt(out)} moved out and not booked = "
                    f"{M.sfmt(M.q(books - out))}, bank {M.sfmt(bank)}: a bank line is not fed (or not booked) yet")
        return None

    def _pending_bank_out(self) -> dict:
        """{(entity, bank account): money recorded as moved OUT of it after the backup and not booked yet}."""
        out: dict = {}
        for b in self.db["breaks"].values():
            if b.get("leg") != "restore_reimport_required":
                continue
            for it in (b.get("items") or {}).values():
                d = it.get("disposition") or {}
                if d.get("disposition") != "moved" or it.get("corrected_by") or it.get("status") == "follows_movement":
                    continue
                der = self._derive_money(it)
                amt = M.D(d.get("moved_amount") or "0.00")
                credits = [ln["account"] for ln in der["lines"] if ln["credit"] != "0.00"] if der else []
                if der is None:
                    fx = RE.manual_lines(it.get("event_type") or "", "funding", amt, d.get("counterparty") or "",
                                         C.RAIL_ACCOUNT["stripe"])
                    credits = [fx[2]] if fx else []
                for a in credits:
                    if ("zbc", a) in BANK_ACCOUNTS:
                        out[("zbc", a)] = M.q(out.get(("zbc", a), M.ZERO) + amt)
        return out

    def _refeed_covers(self, b: dict) -> bool:
        rf = b.get("bank_refeed") or {}
        return bool(rf) and rf.get("since", "9999") <= (b.get("since") or "") \
            and rf.get("until", "") >= (b.get("opened_on") or "9999")

    def _fenced(self) -> set:
        """AEGIS r4 R4-2/R4-3: subjects whose money may have moved after the backup (from the ledger, and in-flight
        transfers and payout items of the restored books). Kept after the lift; only Andre's ``not_moved`` releases
        one (when every item naming it says so)."""
        out, freed = set(), {}
        for b in self.db["breaks"].values():
            if b.get("leg") != "restore_reimport_required":
                continue
            out.update(b.get("fenced") or [])
            for it in (b.get("items") or {}).values():
                if it.get("kind") in ("money_out", "in_flight") and it.get("ref") and it["status"] != "follows_movement":
                    ok = (it.get("disposition") or {}).get("disposition") == "not_moved"
                    freed[it["ref"]] = freed.get(it["ref"], True) and ok
        return out - {k for k, v in freed.items() if v}

    def _subject_fences(self) -> set:
        """AEGIS r5 R5-1 / r6 R6-1/R6-6: money recorded as ``moved`` fences its COUNTERPARTY (the payee of a payout,
        the campaign and client of a refund; derived from the lost event when it verifies): no money goes to it, in
        any amount, until the debt is actually extinguished: the payables the restore correction named are settled,
        or the lost refund is recorded paid. The old ids stay fenced for good (``_fenced``)."""
        out = set()
        for b in self.db["breaks"].values():
            if b.get("leg") != "restore_reimport_required":
                continue
            for it in (b.get("items") or {}).values():
                d = it.get("disposition") or {}
                if d.get("disposition") != "moved" or self._debt_settled(it):
                    continue
                out.update(c for c in (d.get("counterparties") or [d.get("counterparty")]) if c)
        return out

    def _debt_settled(self, it: dict) -> bool:
        pays = it.get("settled_payables") or []
        if pays:
            return all((self.db["payables"].get(p) or {}).get("status") == "settled" for p in pays)
        rf = it.get("settled_refund")
        if rf:
            return (self.db["refunds"].get(rf) or {}).get("status") == "paid"
        if (it.get("event_type") or "").startswith("rail_"):
            # a rail event for a lost payout: settled when that payout's own item is booked
            return any(o.get("ref") == it.get("ref") and o.get("event_type") == "item_submitted"
                       and self._debt_settled(o) for b in self.db["breaks"].values()
                       if b.get("leg") == "restore_reimport_required" for o in (b.get("items") or {}).values())
        return bool(it.get("corrected_by")) and it.get("debt") == "none"

    def _restore_touched(self) -> set:
        """Every subject a money item of any restore named (moved or not): its submit always looks the rail up first."""
        return {it["ref"] for b in self.db["breaks"].values() if b.get("leg") == "restore_reimport_required"
                for it in (b.get("items") or {}).values() if it.get("kind") in ("money_out", "in_flight") and it.get("ref")}

    def _money_out_refusal(self, what: str, *subjects, counterparties=(), amount=None) -> None:
        """AEGIS r4 R4-1/R4-2/R4-4: the service-level choke point for every bank transfer, rail submit, release and
        repayment: nothing while locked, nothing until the post-restore reconciliation is clean (r5 R5-1), never a
        fenced subject, and never the same counterparty and amount as money recorded as moved."""
        if self.restore_locked():
            raise Conflict(LOCKDOWN_MSG.format(what=what))
        if self.restore_post_phase() is not None:
            raise Conflict(f"POST_RESTORE_RECONCILE_REQUIRED: no {what} until a reconciliation run after the lift is "
                           "fully clean (POST /fin/v1/reconciliations/run; runbook restore-and-reimport section 5)")
        hits = [c for c in counterparties if c and c in self._subject_fences()]
        if hits:
            raise Conflict(f"RESTORE_FENCED: {what} refused: money to {hits[0]} was recorded as moved after a restore "
                           "and its debt is not settled yet (book it with the restore correction; any amount is "
                           "refused meanwhile, so the same money is never paid twice)")
        hit = sorted(s for s in subjects if s and s in self._fenced())
        if hit:
            raise Conflict(f"RESTORE_FENCED: {what} refused: the ledger shows money for {hit[0]} may have moved after "
                           "the restored backup; check the bank or rail, then record the restore item as moved or "
                           "not_moved (docs/runbooks/restore-and-reimport.md section 5)")

    def _cn_number_floor(self, year: Optional[int] = None) -> int:
        """AEGIS R3-6: per year, and only from credit notes whose evidence verified with an authentic mark."""
        year = str(year or self._business_today().year)
        out = 0
        for b in self.db["breaks"].values():
            if b.get("leg") == "restore_reimport_required":
                fl = b.get("cn_number_floor")
                # AEGIS r4 R4-13: a round-2 break kept one int floor (no year): it counts for every year
                out = max(out, int(fl.get(year) or 0) if isinstance(fl, dict) else int(fl or 0))
        return out

    def _scan_tail(self, ledger_index: Optional[int]) -> dict:
        """Every EFFECT the ledger recorded after the restored state, classified (AEGIS R2-1, R2-4, R2-5):
        ``pending`` (a verified intent with its authentic commit mark: the re-import rebuilds it), ``needs_review``
        (unproven, uncommitted, unauthenticated, or a kind never rebuilt automatically: Andre disposes of it), or a
        done state for client emails that follow their parent."""
        if ledger_index is None:
            return {"items": {}, "affected_invoices": [], "cn_number_floor": {}, "scanned": False, "fenced": []}
        entries = self.recorder.client.entries()
        tail = [(i, e) for i, e in enumerate(entries) if i > ledger_index and e.get("department") == i10.DEPT]
        marks, sent, affected, floor = {}, set(), set(), {}
        for i, e in tail:
            et = e.get("event_type")
            if et == COMMIT_TYPE:
                m = COMMIT_SUMMARY.fullmatch(str(e.get("summary") or ""))
                if m:
                    marks.setdefault(m.group(1), (e, m))
            elif et in ("client_mail_sent", "client_receipt_sent"):            # AEGIS r6 R6-7: receipts too
                sent.add(e.get("subject_id"))
            elif et in ("stripe_checkout_created", "invoice_issued", "invoice_approved"):
                affected.add(e.get("subject_id"))
        items_out: dict = {}
        for i, e in tail:
            if e.get("event_type") in MONEY_OUT_TYPES:
                items_out[e.get("event_id")] = {
                    "event_id": e.get("event_id"), "event_type": e.get("event_type"), "order": i,
                    "status": "needs_review", "kind": "money_out", "ref": e.get("subject_id"), "invoice_id": None,
                    "client_id": None, "recorded_at": e.get("recorded_at"), "summary": str(e.get("summary") or "")[:280],
                    "payload_sha256": e.get("payload_sha256"),
                    "why": "money went out after the backup (per the ledger): check the bank or rail, then record it "
                           "as moved (with the reference) or not_moved"}
        # AEGIS r7 R7-1: a posting attempt the ledger shows reversed (the bank refused it) moved nothing
        for i, e in tail:
            if e.get("event_type") == "treasury_posting_reversed":
                cands = [it for it in items_out.values() if it["event_type"] == "treasury_posting_committed"
                         and it["ref"] == e.get("subject_id") and it["order"] < i]
                if cands:
                    last = max(cands, key=lambda x: x["order"])
                    last.update(reversed_on_ledger=True,
                                why="this posting attempt was reversed on the ledger (the bank refused it): record it "
                                    "as not_moved")
        items: dict = dict(items_out)
        for i, e in tail:
            et = e.get("event_type")
            if et not in EFFECT_TYPES:
                continue
            eid = e.get("event_id")
            it = {"event_id": eid, "event_type": et, "order": i, "status": "needs_review", "why": None,
                  "invoice_id": None, "recorded_at": e.get("recorded_at")}
            ok, why = self._mark_ok(eid, marks)
            if et == "credit_note_issued":
                m = CN_SUMMARY.fullmatch(str(e.get("summary") or ""))
                if not m:
                    it["why"] = "unreadable credit note evidence"
                else:
                    number, cnid, iid, amount, to_refund, due_from, reason, seq = m.groups()
                    it.update(kind="credit_note", ref=cnid, credit_note_id=cnid, number=number, invoice_id=iid,
                              amount=amount, to_refund=to_refund, refund_due_from=None if due_from == "-" else due_from,
                              reason_code=reason, line=int(seq))
                    payload = credit_note_payload(cnid, number, iid, amount, to_refund, reason, it["refund_due_from"])
                    verified = self._verified(e, derived_id("cn", cnid), et, payload, int(seq))
                    it["why"] = why if not ok else (None if verified else "the summary does not hash to the payload")
                    if ok and verified:
                        it["status"] = "pending"
                        yr = number.split("-")[1]                       # AEGIS R3-6: authentic numbers only
                        floor[yr] = max(floor.get(yr, 0), int(number.rsplit("-", 1)[1]))
            elif et == "invoice_voided":
                m = VOID_SUMMARY.fullmatch(str(e.get("summary") or ""))
                if not m:
                    it["why"] = "unreadable void evidence"
                else:
                    iid, amount, reason, seq = m.groups()
                    it.update(kind="void", ref=iid, invoice_id=iid, amount=amount, reason_code=reason, line=int(seq))
                    verified = self._verified(e, derived_id("invv", iid), et, void_payload(iid, amount, reason),
                                              int(seq))
                    it["why"] = why if not ok else (None if verified else "the summary does not hash to the payload")
                    if ok and verified:
                        it["status"] = "pending"
            elif et == "deadline_recorded":
                it.update(kind="deadline", ref=e.get("subject_id"), summary=str(e.get("summary") or "")[:200],
                          why=why if not ok else "a contract deadline recorded after the backup: re-record it "
                                                 "(sow-ended / acknowledgement), then dispose of this item")
            else:
                m = MAIL_SUMMARY.fullmatch(str(e.get("summary") or ""))
                kind, ref = (m.group(1), m.group(2)) if m else ("unknown", None)
                it.update(kind="mail", ref=e.get("subject_id"), mail_kind=kind, parent=ref)
                if e.get("subject_id") in sent:
                    it.update(status="sent_before_restore", why=None)
                elif kind in ("credit_note", "invoice_cancelled"):
                    it.update(status="follows_parent", why=None)      # its rebuild re-queues it (never sent yet)
                elif kind == "refund_sent":
                    it.update(status="follows_refund", why=None)      # re-queued when the refund is re-imported
                else:
                    it["why"] = why if not ok else "queued but not sent before the restore: send it again, then " \
                                                   "dispose of this item"
            if it.get("invoice_id"):
                affected.add(it["invoice_id"])
            m_cl = re.search(r" client (\S+) ", str(e.get("summary") or ""))
            inv_row = self.db["invoices"].get(it.get("invoice_id") or "")
            it["client_id"] = (inv_row or {}).get("client_id") or (m_cl.group(1) if m_cl else None)
            if it["status"] == "needs_review" and (it.get("why") or "").startswith("an attempt only") and \
                    self._anchor_at(entries, it.get("line")):
                it["why"] = ("possibly committed: a log line anchor exists at its line number but no commit mark "
                             "(unauthenticated: a forged event could claim this too); check what the client received "
                             "before disposing of it")
            items[eid] = it
        # a client email follows its parent only if that parent is rebuilt automatically
        parents = {it.get("ref"): it for it in items.values() if it.get("kind") in ("credit_note", "void")}
        for it in items.values():
            if it.get("status") == "follows_parent":
                par = parents.get(it.get("parent"))
                if par is None or par["status"] != "pending":
                    it.update(status="needs_review", why="its credit note or void is not rebuilt automatically: "
                                                         "send the email again if it applies, then dispose of it")
        mails_sent = {it["parent"] for it in items.values() if it.get("kind") == "mail"
                      and it["status"] == "sent_before_restore" and it.get("parent")}
        for it in items.values():
            if it.get("kind") in ("credit_note", "void"):
                it["mail_sent_before_restore"] = it.get("ref") in mails_sent
        return {"items": items, "affected_invoices": sorted(a for a in affected if a), "mail_sent": sorted(sent),
                "fenced": sorted({it["ref"] for it in items_out.values() if it.get("ref")}),
                "cn_number_floor": floor, "scanned": True}

    def _anchor_at(self, entries: list, seq) -> bool:
        if not seq:
            return False
        pre = f"fin-log-{self.log.epoch}-{int(seq)}-"
        return sum(1 for e in entries if e.get("event_type") == i10.ANCHOR_TYPE and e.get("department") == i10.DEPT
                   and str(e.get("event_id") or "").startswith(pre)) == 1

    def _mark_ok(self, intent: str, marks: dict) -> tuple[bool, Optional[str]]:
        hit = marks.get(intent)
        if hit is None:
            return False, "an attempt only: no commit mark (the operation answered an error, or its mark was lost)"
        e, m = hit
        if e.get("event_id") != derived_id("cmt", intent):
            return False, "the commit mark's id does not match"
        if e.get("department") != i10.DEPT:
            return False, "the commit mark is not Finance's"
        kid, mac = m.group(4), m.group(5)
        if mac == "-" or kid == "-":
            return False, "the commit mark is not authenticated (FIN_CONTACT_HASH_KEY was not set when it was written)"
        dk = self._evidence_keys().get(kid)
        if dk is None:
            return False, (f"the commit mark is under unknown key id {kid} (a rotated key no longer configured: list "
                           "it after the current key in FIN_CONTACT_HASH_KEY)")
        want = self._commit_mac(intent, int(m.group(2)), m.group(3), dk)
        if not hmac.compare_digest(mac, want):
            return False, "the commit mark's MAC does not verify (not written by this Finance)"
        psha = payload_sha256({"intent_event_id": intent, "line_seq": int(m.group(2)), "line_sha256": m.group(3),
                               "key_id": kid, "mac": mac})
        if psha != e.get("payload_sha256"):
            return False, "the commit mark's summary does not hash to its payload"
        return True, None

    def _restore_break_row(self, plan: dict) -> tuple[str, dict]:
        """AEGIS r3 R3-1/R3-2: the restore break, built BEFORE the reconcile commits and committed IN the reconcile's
        own line (one op), so a reconcile without its break cannot exist. A scan that fails or cannot run leaves the
        break ``build_incomplete`` (the lockdown cannot be lifted; every re-import run scans again)."""
        rp = plan.get("restore_point") or {}
        since = rp.get("date") or self._today_la().isoformat()
        bid = rid("brk", "restore_reimport_required", plan["head_sha256"] or "empty")
        if bid in self.db["breaks"]:
            return bid, self.db["breaks"][bid]
        try:
            scan = self._scan_tail(rp.get("ledger_index"))
        except Exception:  # noqa: BLE001 - unreadable: nothing listed yet, and the build is incomplete (fail closed)
            scan = {"items": {}, "affected_invoices": [], "cn_number_floor": {}, "scanned": False, "fenced": []}
        # AEGIS r4 R4-2: transfers and payout items the restored books hold in flight may have moved after the backup
        inflight = self._inflight_items()
        scan = {**scan, "items": self._dedupe_movements({**inflight, **scan["items"]}),
                "fenced": sorted(set(scan.get("fenced") or []) | {it["ref"] for it in inflight.values()})}
        row = {"break_id": bid, "leg": "restore_reimport_required", "subject": "zbm:1060",
               "difference": "0.00", "opened_at": iso(self._now()),
               "opened_on": self._business_today().isoformat(), "owner": "andre", "severity": "high",
               "explanation_code": "unknown", "status": "open", "receipt_id": None,
               "since": since, "restore_point": rp, "ledger_index": rp.get("ledger_index"),
               "head_seq": plan.get("head_seq"), "head_sha256": plan.get("head_sha256"),
               "covered": [], "money_events": len(plan["money_at_risk"]), "resolution": None,
               **scan, "build_incomplete": not scan["scanned"], "ready_to_lift": False,
               "note": f"restored from a backup of {since}: Finance is in RESTORE LOCKDOWN. Run POST "
                       f"/fin/v1/stripe/reimport (dry run, then real) for every day from {since} to "
                       f"{self._business_today().isoformat()}, settle every item it cannot rebuild, then Andre lifts "
                       "the lockdown (POST /fin/v1/restore/lockdown/lift)"}
        return bid, row

    def _inflight_items(self) -> dict:
        out = {}
        for t in self.db["treasury_ops"].values():
            if t.get("status") in ("approved", "executing", "bank_unknown"):
                out[f"inflight:{t['op_id']}"] = {"event_id": f"inflight:{t['op_id']}", "event_type": "treasury_op",
                                                 "kind": "in_flight", "ref": t["op_id"], "status": "needs_review",
                                                 "order": -1, "invoice_id": None, "client_id": None,
                                                 "why": f"a {t.get('kind')} transfer the restored books hold as "
                                                        f"{t['status']}: it may have moved after the backup; check the "
                                                        "bank, then record it as moved or not_moved"}
        for it in self.db["items"].values():
            if it.get("status") == "submitting":
                out[f"inflight:{it['item_id']}"] = {"event_id": f"inflight:{it['item_id']}", "event_type": "payout_item",
                                                    "kind": "in_flight", "ref": it["item_id"], "status": "needs_review",
                                                    "order": -1, "invoice_id": None, "client_id": None,
                                                    "why": "a payout item the restored books hold as submitting: check "
                                                           "the rail, then record it as moved or not_moved"}
        return out

    def _lift_missing(self, b: dict) -> list[str]:
        out = []
        if b.get("build_incomplete") or b.get("scanned") is False:
            out.append("the list of what the restore lost is incomplete (run a re-import dry run to read the ledger "
                       "again)")
        if not self._covers(b.get("covered") or [], b.get("since"), b.get("opened_on")):
            out.append(f"the re-import has not covered every day from {b.get('since')} to {b.get('opened_on')}")
        for ent_, acct in BANK_ACCOUNTS:                    # AEGIS r7 R7-3: the bank's own balances, every account
            why = self._bank_balance_mismatch(ent_, acct)
            if why:
                out.append(why)
        if not self._refeed_covers(b):
            out.append(f"Andre has not confirmed the bank lines from {b.get('since')} to {b.get('opened_on')} were fed "
                       "again (POST /fin/v1/restore/bank-refeed)")
        left = [i for i, it in (b.get("items") or {}).items() if it["status"] not in DONE]
        if left:
            out.append(f"{len(left)} restore item(s) are not rebuilt or disposed of: {', '.join(sorted(left)[:5])}")
        return out

    def _lockdown_effects(self) -> list[dict]:
        return [{"kind": "void", "invoice_id": i["invoice_id"]} for i in self.db["invoices"].values()
                if i.get("voided_during_lockdown")] + \
               [{"kind": "credit_note", "credit_note_id": c["credit_note_id"], "invoice_id": c["invoice_id"]}
                for c in self.db["credit_notes"].values() if c.get("issued_during_lockdown")]

    def _restore_progress(self, op: Op, b: dict) -> bool:
        """Never closes anything (AEGIS r3: lifting is Andre's explicit act); it records whether the lift is ready."""
        ready = not self._lift_missing(b)
        op.put("breaks", b["break_id"], {**b, "ready_to_lift": ready})
        return ready

    def _restore_rescan(self) -> None:
        """AEGIS R3-1: while a restore break's list is incomplete, every re-import run reads the ledger again first;
        until that works nothing is planned."""
        with self.lock:
            todo = [b for b in self._restore_open() if b.get("build_incomplete") or b.get("scanned") is False]
            for b in todo:
                idx = b.get("ledger_index")
                if idx is None:
                    # AEGIS r4 R4-6: find the restored head's anchor again (its id is deterministic)
                    idx = self._head_index(b.get("head_seq"), b.get("head_sha256"))
                    if idx is None:
                        raise Conflict("RESTORE_BUILD_INCOMPLETE: the restored log head's anchor is not on the ledger, "
                                       "so what the restore lost cannot be listed; restore again (runbook section 3)")
                    b = {**b, "ledger_index": idx, "restore_point": {**(b.get("restore_point") or {}),
                                                                     "ledger_index": idx}}
                try:
                    scan = self._scan_tail(idx)
                except Exception:  # noqa: BLE001
                    raise Unavailable("RESTORE_BUILD_INCOMPLETE: the evidence ledger could not be read to list what "
                                      "the restore lost; nothing was planned (retry)") from None
                if not scan.get("scanned"):
                    raise Unavailable("RESTORE_BUILD_INCOMPLETE: the restore's list could not be built; nothing was "
                                      "planned (retry)")
                op = Op(self, f"rescan|{b['break_id']}|{len(self.log)}", "andre", b["break_id"])
                op.record(derived_id("rscan", b["break_id"]), "restore_items_listed", "andre", b["break_id"],
                          {"break_id": b["break_id"], "items": len(scan["items"])},
                          f"Restore items listed after a failed read: {len(scan['items'])}")
                inflight = {k: v for k, v in (b.get("items") or {}).items() if k.startswith("inflight:")}
                self._restore_progress(op, {**b, **scan, "items": self._dedupe_movements({**inflight, **scan["items"]}),
                                            "fenced": sorted(set(b.get("fenced") or []) | set(scan.get("fenced") or [])),
                                            "build_incomplete": False})
                self._commit(op)
            st = self.restore_lockdown()
            if todo and st and st.get("build_incomplete") and not any(
                    x.get("build_incomplete") for x in self._restore_open()):
                self._lockdown_write({**st, "build_incomplete": False})

    def _head_index(self, seq, head_sha) -> Optional[int]:
        if not seq or not head_sha:
            return None
        aid = i10.anchor_id(self.log.epoch, int(seq), head_sha)
        try:
            for i, e in enumerate(self.recorder.client.entries()):
                if e.get("event_id") == aid:
                    return i
        except Exception:  # noqa: BLE001
            raise Unavailable("RESTORE_BUILD_INCOMPLETE: the evidence ledger could not be read; nothing was planned "
                              "(retry)") from None
        return None

    def restore_item_disposition(self, request_id: str, event_id: str, body: dict) -> dict:
        """AEGIS R2-2 / R3-3: Andre's decision on one lost item the re-import CANNOT rebuild (status ``needs_review``;
        an item the re-import rebuilds is never disposed of): ``lost`` (with the reason), ``reissued`` (naming an
        invoice or credit note of the same client created after the item), or ``reissue_after_lift`` (a credit note
        or void to re-issue once the lockdown is lifted: the lift opens a break for it). Recorded on the ledger."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"restore/items/{event_id}", body)
            if ent:
                return ent["response"]
            for b in self._restore_open():
                if event_id in (b.get("items") or {}):
                    break
            else:
                raise NotFound("no open restore item with that ledger event id")
            it = b["items"][event_id]
            if it["status"] in DONE:
                raise Conflict(f"this item is already {it['status']}")
            if it["status"] != "needs_review":
                raise Conflict("the re-import rebuilds this item from verified evidence: it cannot be disposed of "
                               "(run the re-import)")
            money = it.get("kind") in ("money_out", "in_flight")
            if money != (body["disposition"] in ("moved", "not_moved")):
                raise Conflict("money that went out is recorded as moved (with the bank or rail reference in the reason) "
                               "or not_moved; every other item as lost, reissued or reissue_after_lift")
            if body["disposition"] == "reissue_after_lift" and it.get("kind") not in ("credit_note", "void"):
                raise Conflict("only a credit note or a void is re-issued after the lift")
            if body["disposition"] == "reissued":
                self._check_reissued_as(it, body.get("reissued_as"))
            evidence = self._money_evidence(it, body, b) if money else None
            if evidence and evidence.get("pending"):
                # AEGIS r6 R6-4: the rail did not find it: Andre asks again after 24 h (a re-lookup) with his statement
                new_items = {**b["items"], event_id: {**it, "not_moved_first_lookup_at": evidence["lookup_at"],
                                                      "not_moved_lookups": evidence["lookups"]}}
                op = Op(self, f"rdispp|{request_id}", "andre", event_id)
                op.record(derived_id("rdispp", event_id, request_id), "restore_not_moved_lookup", "andre", event_id,
                          {"event_id": event_id, "lookups": len(evidence["lookups"])},
                          "Rail looked up for a not_moved claim: not found; asked again after 24 h")
                self._restore_progress(op, {**b, "items": new_items})
                resp = self._idem_add(op, key, h, {"event_id": event_id, "status": "pending_not_moved",
                                                   "retry_after": evidence["retry_after"],
                                                   "ledger_event_ids": op.events, "request_id": request_id})
                self._commit(op)
                return resp
            new_items = {**b["items"], event_id: {**it, "status": "disposed",
                                                  "disposition": {"disposition": body["disposition"],
                                                                  "reason": body["reason"],
                                                                  "reissued_as": body.get("reissued_as"),
                                                                  **(evidence or {}),
                                                                  "at": iso(self._now())}}}
            op = Op(self, f"rdisp|{request_id}", "andre", event_id)
            op.record(derived_id("rdisp", event_id), "restore_item_disposed", "andre", event_id,
                      {"event_id": event_id, "disposition": body["disposition"],
                       "reason_sha256": sha({"r": body["reason"]}), "reissued_as": body.get("reissued_as")},
                      f"Restore item disposed: {body['disposition']}")
            ready = self._restore_progress(op, {**b, "items": new_items})
            resp = self._idem_add(op, key, h, {"event_id": event_id, "status": "disposed", "ready_to_lift": ready,
                                               "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    @staticmethod
    def _movement_key(it: dict) -> Optional[str]:
        """AEGIS r7 R7-1: the money movement an item stands for (one transfer, one payout item, one refund)."""
        et, ref = it.get("event_type") or "", it.get("ref")
        if not ref:
            return None
        if et == "treasury_posting_committed":
            m = RE.XFER_SUMMARY.fullmatch(str(it.get("summary") or ""))
            if m and m.group(1) == "refund_payment" and m.group(9) != "-":
                return f"refund:{m.group(9)}"             # a refund's transfer is the refund's own movement
            return f"xfer:{ref}"
        if et == "treasury_op":
            return f"xfer:{ref}"
        if et in ("item_submitted", "payout_item") or et.startswith("rail_"):
            return f"item:{ref}"
        if et == "refund_paid":
            return f"refund:{ref}"
        return f"{et}:{ref}"

    def _dedupe_movements(self, items: dict) -> dict:
        """One movement, one item for Andre: the ledger's latest unreversed attempt (else the restored books'
        in-flight view) is the item; the others FOLLOW it (closed with it, never booked on their own)."""
        groups: dict = {}
        for k, it in items.items():
            if it.get("kind") in ("money_out", "in_flight") and self._movement_key(it):
                groups.setdefault(self._movement_key(it), []).append(k)
        out = dict(items)
        for mk, keys in groups.items():
            if len(keys) < 2:
                continue
            ranked = sorted(keys, key=lambda k: (not out[k].get("reversed_on_ledger"),
                                                 out[k].get("event_type") != "treasury_op", out[k].get("order", -1)))
            primary = ranked[-1]
            for k in keys:
                if k != primary:
                    out[k] = {**out[k], "status": "follows_movement", "primary": primary,
                              "why": f"the same money movement as {primary}: settled with it"}
        return out

    def _movement_booked(self, it: dict) -> Optional[str]:
        """The correction that already booked this movement, if any (one movement is booked at most once)."""
        mk = self._movement_key(it)
        for b in self.db["breaks"].values():
            if b.get("leg") != "restore_reimport_required":
                continue
            for o in (b.get("items") or {}).values():
                if o is not it and o.get("corrected_by") and not o.get("covered_by") and self._movement_key(o) == mk:
                    return o["corrected_by"]
        return None

    def _derive_money(self, it: dict) -> Optional[dict]:
        """AEGIS r6 R6-1: what a lost money-out event did, from its own verified ledger evidence (or, for a transfer
        the restored books hold in flight, from that operation). None when nothing verifies (fail closed)."""
        summ, kind = str(it.get("summary") or ""), it.get("event_type")
        e = {"event_id": it.get("event_id"), "payload_sha256": it.get("payload_sha256")}
        if it.get("reversed_on_ledger"):
            return None                                   # the bank refused that attempt: nothing moved, nothing booked
        if kind == "payout_item":
            x = self.db["items"].get(it.get("ref") or "")
            if x is not None and x.get("payee_id") and x.get("net") and x.get("rail") in C.RAIL_ACCOUNT:
                return {"what": "payout", "amount": x["net"], "counterparties": [x["payee_id"]], "payee": x["payee_id"],
                        "key": x.get("idempotency_key"), "rail": x["rail"], "batch": x.get("batch_id"), "entity": "zbc",
                        "own_payables": list(x.get("payable_ids") or []),
                        "lines": [J.dr("2020", M.D(x["net"]), f"payee:{x['payee_id']}"),
                                  J.cr(C.RAIL_ACCOUNT[x["rail"]], M.D(x["net"]))]}
            return None
        if kind == "item_submitted":
            m = RE.ITEM_SUMMARY.fullmatch(summ)
            if m:
                rail, net, payee, batch, period, seq = m.groups()
                key = I4.idempotency_key(batch, payee, period)
                if self._verified(e, derived_id("isub", it["ref"]), kind,
                                  RE.item_payload(it["ref"], net, key, payee, rail, batch), int(seq)):
                    return {"what": "payout", "amount": net, "counterparties": [payee], "payee": payee, "key": key,
                            "rail": rail, "batch": batch, "entity": "zbc",
                            "lines": [J.dr("2020", M.D(net), f"payee:{payee}"), J.cr(C.RAIL_ACCOUNT[rail], M.D(net))]}
        elif kind == "treasury_posting_committed":
            m = RE.XFER_SUMMARY.fullmatch(summ)
            if m:
                k, n, flow, amt, da, ds, ca, cs, ref, seq = m.groups()
                payload = RE.xfer_payload(it["ref"], int(n), k, flow, amt, (da, None if ds == "-" else ds),
                                          (ca, None if cs == "-" else cs), None if ref == "-" else ref)
                if self._verified(e, derived_id("trxp", it["ref"], int(n)), kind, payload, int(seq)):
                    return {"what": f"transfer:{k}", "amount": amt, "counterparties": [] if k != "refund_payment"
                            else [ref], "ref_id": None if ref == "-" else ref, "entity": "zbc", "flow": flow,
                            "lines": [J.dr(da, M.D(amt), None if ds == "-" else ds),
                                      J.cr(ca, M.D(amt), None if cs == "-" else cs)]}
        elif kind == "refund_paid":
            m = RE.REFUND_PAID_SUMMARY.fullmatch(summ)
            if m:
                amt, camp, client, seq = m.groups()
                if self._verified(e, derived_id("rfdpd", it["ref"]), kind,
                                  RE.refund_paid_payload(it["ref"], amt, camp, client), int(seq)):
                    return {"what": "refund", "amount": amt, "counterparties": [camp, client], "campaign": camp,
                            "client": client, "entity": "zbc",
                            "lines": [J.dr("2010", M.D(amt), f"campaign:{camp}"), J.cr("1020", M.D(amt))]}
        elif kind == "treasury_op":
            t = self.db["treasury_ops"].get(it.get("ref") or "")
            if t is not None and t["kind"] in ("funding", "sweep", "top_up", "refund_payment") \
                    and (t["kind"] != "refund_payment" or t.get("ref_id") in self.db["refunds"]):
                try:
                    flow, lines, _ = self._transfer_lines(t)
                except (KeyError, Invalid):
                    return None
                rf = self.db["refunds"].get(t.get("ref_id") or "") if t["kind"] == "refund_payment" else None
                return {"what": f"transfer:{t['kind']}", "amount": t["amount"], "entity": "zbc", "flow": flow,
                        "counterparties": [rf["campaign_id"], rf["client_id"]] if rf else [], "ref_id": t.get("ref_id"),
                        "inflight_op": t["op_id"], "lines": lines}
        return None

    def _restore_payout_keys(self, it: dict, b: dict) -> list[tuple]:
        """(rail, idempotency key, item id) of every payout this money item names: from the verified lost event, the
        restored books, or (a release) the item-submitted events of the same batch."""
        ref, et = it.get("ref"), it.get("event_type")
        if et in ("item_submitted", "payout_item") or et.startswith("rail_"):
            d = self._derive_money(it) if et == "item_submitted" else None
            if d:
                return [(d["rail"], d["key"], ref)]
            x = self.db["items"].get(ref or "")
            return [(x["rail"], x["idempotency_key"], ref)] if x else []
        if et == "batch_release_started":
            out = [(x["rail"], x["idempotency_key"], x["item_id"]) for x in self.db["items"].values()
                   if x.get("batch_id") == ref]
            for o in (b.get("items") or {}).values():
                d = self._derive_money(o) if o.get("event_type") == "item_submitted" else None
                if d and d["batch"] == ref and all(k[2] != o["ref"] for k in out):
                    out.append((d["rail"], d["key"], o["ref"]))
            return out
        return []

    def _money_evidence(self, it: dict, body: dict, b: dict) -> dict:
        """AEGIS r5/r6: ``moved`` names the provider reference; the amount and counterparty come from the lost event
        when it verifies (Andre's values must then agree), else from Andre. ``not_moved``: every payout is looked up
        at the rail by its idempotency key (found -> refused; the rail unreachable -> 503); not found -> only after a
        24 h wait, a second lookup and Andre's statement reference and date. A rail event item is not_moved only if
        the rail itself said failed or returned. Bank money needs Andre's attestation."""
        d = self._derive_money(it)
        if body["disposition"] == "moved":
            if not body.get("provider_ref"):
                raise Invalid("moved needs provider_ref (the bank or rail reference)")
            if d is not None:
                if body.get("moved_amount") is not None and M.D(body["moved_amount"]) != M.D(d["amount"]):
                    raise Conflict(f"the lost event moved {d['amount']}, not {M.fmt(M.D(body['moved_amount']))}")
                if body.get("counterparty") and body["counterparty"] not in d["counterparties"]:
                    raise Conflict("the lost event names another counterparty")
                cps, amount = d["counterparties"], d["amount"]
            elif it.get("event_type") in ("batch_release_started", "media_vendor_payment_recorded"):
                # the money itself is on its item-submitted / vendor records: the id stays fenced, no counterparty
                cps, amount = [], M.fmt(M.D(body["moved_amount"])) if body.get("moved_amount") is not None else "0.00"
            else:
                if body.get("moved_amount") is None or not body.get("counterparty"):
                    raise Invalid("this lost event cannot be verified: moved needs moved_amount and counterparty too")
                cps, amount = [body["counterparty"]], M.fmt(M.D(body["moved_amount"]))
            return {"provider_ref": body["provider_ref"], "moved_amount": M.fmt(M.D(amount)),
                    "counterparty": (cps or [None])[0], "counterparties": cps, "derived": d is not None,
                    "statement_date": body["statement_date"].isoformat() if body.get("statement_date") else None}
        et = it.get("event_type") or ""
        if et.startswith("rail_"):
            if et in ("rail_failed", "rail_returned"):
                return {"evidence": {"rail_event": et}}
            raise Conflict("the rail reported this payout as paid: record it as moved")
        keys = self._restore_payout_keys(it, b)
        payoutish = et in ("item_submitted", "batch_release_started", "payout_item")
        if payoutish and not keys:
            raise Conflict("no idempotency key is known for this payout: record its item-submitted items first, or "
                           "record it as moved")
        if keys:
            looks = []
            for rail, key_, iid in keys:
                try:
                    lk = self._rail(rail).lookup(key_, iid)
                except Exception:  # noqa: BLE001
                    lk = None
                if not isinstance(lk, RailLookup) or not lk.available:
                    raise Unavailable("the rail could not be asked about this payout; nothing was recorded (retry)")
                if lk.found:
                    raise Conflict(f"the rail shows payout item {iid} was received ({lk.status}): record it as "
                                   "moved, with the rail reference")
                looks.append({"item_id": iid, "found": False, "at": iso(self._now())})
            first = it.get("not_moved_first_lookup_at")
            if not first or self._now() - parse_iso(first) < timedelta(hours=24):
                return {"pending": True, "lookup_at": first or iso(self._now()), "lookups": looks,
                        "retry_after": iso(parse_iso(first or iso(self._now())) + timedelta(hours=24))}
            if not (body.get("provider_ref") and body.get("statement_date")):
                raise Invalid("not_moved after the second lookup needs provider_ref (the rail statement reference) and "
                              "statement_date")
            return {"evidence": {"rail_lookups": (it.get("not_moved_lookups") or []) + looks,
                                 "attestation": {"provider_ref": body["provider_ref"],
                                                 "statement_date": body["statement_date"].isoformat()}}}
        if not (body.get("provider_ref") and body.get("statement_date")):
            raise Invalid("not_moved for bank money needs Andre's attestation: provider_ref (the bank statement "
                          "reference) and statement_date")
        return {"evidence": {"attestation": {"provider_ref": body["provider_ref"],
                                             "statement_date": body["statement_date"].isoformat()}}}

    def restore_bank_refeed(self, request_id: str, body: dict) -> dict:
        """AEGIS r5 R5-4: Andre confirms the bank lines from the backup's day to today were fed again (bank events),
        so a payment made by bank after the backup is in the books before the lift or any 4.12(g) exception."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, "restore/bank-refeed", body)
            if ent:
                return ent["response"]
            open_ = self._restore_open()
            if not open_:
                raise Conflict("no restore break is open")
            if body["since"] > body["until"] or body["until"] > self._business_today():
                raise Invalid("since..until must be a past window")
            # AEGIS r6 R6-3: backed by data: every business day of the window, every bank account, the statement's
            # credit lines and total must equal the bank lines Finance has booked for that day (feed them first)
            days = {(x["day"].isoformat(), x["entity"], x["account"]): x for x in body["statement_days"]}
            booked: dict = {}
            for r in self.db["receipts"].values():
                if r.get("bank_txn_ref_sha256") and r.get("value_date"):
                    k = (str(r["value_date"])[:10], r.get("entity"), r.get("into_account"))
                    n, tot = booked.get(k, (0, M.ZERO))
                    booked[k] = (n + 1, M.q(tot + M.D(r["amount"])))
            wrong = []
            day = body["since"]
            while day <= body["until"]:                   # AEGIS r7 R7-5: every CALENDAR day (weekend value dates too)
                for ent_, acct in BANK_ACCOUNTS:
                    st = days.get((day.isoformat(), ent_, acct))
                    got = booked.get((day.isoformat(), ent_, acct), (0, M.ZERO))
                    if st is None:
                        wrong.append(f"{day.isoformat()} {ent_}:{acct} missing from the statement days")
                    elif (st["credit_lines"], M.D(st["credit_total"])) != got:
                        wrong.append(f"{day.isoformat()} {ent_}:{acct} statement {st['credit_lines']} line(s) "
                                     f"{M.fmt(M.D(st['credit_total']))}, booked {got[0]} line(s) {M.fmt(got[1])}")
                day += timedelta(days=1)
            if any(k[0] < body["since"].isoformat() or k[0] > body["until"].isoformat() for k in days):
                wrong.append("a statement day outside the window")
            # AEGIS r7 R7-3: the statement's closing balance on the last day equals the bank's own balance (the bank
            # feed, independent of Andre) and the books, for every bank account
            for ent_, acct in BANK_ACCOUNTS:
                st = days.get((body["until"].isoformat(), ent_, acct))
                if st is None:
                    continue
                why = self._bank_balance_mismatch(ent_, acct, M.D(st["closing_balance"]))
                if why:
                    wrong.append(why)
            if wrong:
                raise Conflict("RESTORE_REFEED_MISMATCH: feed the bank lines first (POST /fin/v1/bank/events); "
                               + "; ".join(wrong[:10]), mismatches=wrong[:50])
            rf = {"since": body["since"].isoformat(), "until": body["until"].isoformat(),
                  "statement_ref": body["statement_ref"], "at": iso(self._now()), "by": "andre",
                  "days_checked": len(days)}
            op = Op(self, f"refeed|{request_id}", "andre", "restore-bank-refeed")
            for b in open_:
                self._restore_progress(op, {**b, "bank_refeed": rf})
            op.record(derived_id("refeed", request_id), "restore_bank_refeed_confirmed", "andre", "restore-bank-refeed",
                      {k: rf[k] for k in ("since", "until", "statement_ref")},
                      f"Andre confirmed bank lines {rf['since']}..{rf['until']} were fed again")
            resp = self._idem_add(op, key, h, {"bank_refeed": rf, "ledger_event_ids": op.events,
                                               "request_id": request_id})
            self._commit(op)
            return resp

    def _restore_item(self, event_id: str) -> tuple:
        for b in self.db["breaks"].values():
            if b.get("leg") == "restore_reimport_required" and event_id in (b.get("items") or {}):
                return b, b["items"][event_id]
        raise NotFound("no restore item with that ledger event id")

    def _booking_checks(self, it: dict, d: dict, body: dict) -> None:
        if it.get("kind") not in ("money_out", "in_flight") or d.get("disposition") != "moved":
            raise Conflict("a restore booking books only money Andre recorded as moved")
        if it["status"] == "follows_movement":
            raise Conflict(f"this item follows {it.get('primary')}: book the money movement there, once")
        if it.get("corrected_by"):
            raise Conflict("this restore item is already booked")
        if body["provider_ref"] != d.get("provider_ref"):
            raise Conflict("provider_ref must be the bank or rail reference recorded on the item")
        if self._movement_booked(it):
            raise Conflict("this money movement is already booked by another restore item (one movement, one booking)")
        for b in self.db["breaks"].values():                  # AEGIS r7 R7-1: one booking per bank / rail reference
            for o in (b.get("items") or {}).values() if b.get("leg") == "restore_reimport_required" else ():
                if o is not it and o.get("corrected_by") and not o.get("covered_by") \
                        and (o.get("disposition") or {}).get("provider_ref") == body["provider_ref"]:
                    raise Conflict("this bank or rail reference already booked another restore item")

    def _payables_for(self, payee: str, amount, ids: list, own: Optional[list], allow_none: bool = False) -> list:
        if not ids:
            if allow_none and not any(p["payee_id"] == payee and p["status"] in RUNNABLE_STATES
                                      for p in self.db["payables"].values()):
                return []                                  # nothing of this payee is payable: nothing to settle twice
            raise Conflict("a payout booking names the payables it paid (payable_ids)")
        pays = [self.db["payables"].get(pid) for pid in ids]
        if any(p is None or p["status"] not in RUNNABLE_STATES for p in pays):
            raise Conflict("payable_ids must name unpaid payables")
        if any(p["payee_id"] != payee for p in pays) or M.total(M.D(p["current_amount"]) for p in pays) != M.D(amount):
            raise Conflict("the payables must be the payee's and add up to the payout's net")
        if own is not None and sorted(own) != sorted(ids):
            raise Conflict("the payables must be the payout item's own")
        return pays

    def _book_restore(self, op: Op, brk: dict, it: dict, event_id: str, entity: str, lines: list, pays: list,
                      prof: Optional[dict], rf_row: Optional[dict], inflight_op: Optional[str], amount,
                      provider_ref: str, basis: str) -> dict:
        try:
            # a FACT: the money already moved (the bank / rail says so); the treasury gate stops decisions, not history
            e = self._post(op, entity, lines, "FRC", {"kind": "restore_correction", "id": event_id},
                           f"FRC|{event_id}|{len(self.log)}", approval_ref=op.op_id, actor="andre", fact=True)
        except PostingRefused as exc:
            raise InvalidReasons("restore booking refused", exc.reasons) from None
        extra: dict = {"corrected_by": e["entry_id"], "debt": "none", "booking": basis}
        for p in pays:
            op.put("payables", p["payable_id"], {**p, "status": "settled", "settled_by_restore_correction": event_id})
        if pays:
            extra = {**extra, "settled_payables": [p["payable_id"] for p in pays]}
            extra.pop("debt")
        if prof is not None:
            fid = rid("rfd", prof["campaign_id"], f"restore|{event_id}")
            op.put("refunds", fid, {"refund_id": fid, "campaign_id": prof["campaign_id"], "client_id": prof["client_id"],
                                    "amount": M.fmt(M.D(amount)), "admin_fee": "0.00", "status": "paid",
                                    "paid_at": iso(self._now()), "restored_by_correction": event_id,
                                    "provider_ref": provider_ref})
            op.put("profiles", prof["campaign_id"], {**prof, "status": "closed"})
            extra = {**extra, "settled_refund": fid}
            extra.pop("debt", None)
        if rf_row is not None:
            op.put("refunds", rf_row["refund_id"], {**rf_row, "status": "paid", "paid_at": iso(self._now()),
                                                    "restored_by_correction": event_id})
            extra = {**extra, "settled_refund": rf_row["refund_id"]}
            extra.pop("debt", None)
        if inflight_op:
            t = self.db["treasury_ops"][inflight_op]
            op.put("treasury_ops", t["op_id"], {**t, "status": "done", "bank_ref": provider_ref,
                                                "done_at": iso(self._now()), "restored_by_correction": event_id})
        resolved = []
        touched = {f"{entity}:{ln['account']}" for ln in lines}
        for x in list(self.db["breaks"].values()):
            if x.get("leg") in RECON_LEGS and x["status"] != "resolved" and any(
                    str(x.get("subject") or "").startswith(t) for t in touched):
                op.put("breaks", x["break_id"], {**x, "status": "resolved", "resolution": {
                    "basis": basis, "entry_id": e["entry_id"], "restore_item": event_id,
                    "approved_by": "andre", "at": iso(self._now())}})
                resolved.append(x["break_id"])
        items = {**brk["items"], event_id: {**it, **extra}}
        mk = self._movement_key(it)
        for k, o in items.items():
            # the same movement's other items (and a refund's own transfer item) are booked by this one
            od = self._derive_money(o) if o.get("event_type") == "treasury_posting_committed" else None
            same = k != event_id and (o.get("status") == "follows_movement" and o.get("primary") == event_id
                                      or (it.get("event_type") == "refund_paid" and od and od.get("ref_id") == it["ref"])
                                      or (self._movement_key(o) == mk and o.get("kind") in ("money_out", "in_flight")))
            if same:
                items[k] = {**o, "corrected_by": e["entry_id"], "covered_by": event_id, "debt": "none"}
        op.put("breaks", brk["break_id"], {**brk, "items": items})
        op.record(derived_id("rcorr", event_id, e["entry_id"]), "restore_correction_posted", "andre", event_id,
                  {"event_id": event_id, "entry_id": e["entry_id"], "amount": M.fmt(M.D(amount)),
                   "provider_ref": provider_ref, "breaks_resolved": resolved, "basis": basis},
                  f"Restore {basis} posted for a moved item: {M.fmt(M.D(amount))}")
        return {"entry": e, "breaks_resolved": resolved}

    def restore_correction(self, request_id: str, event_id: str, body: dict) -> dict:
        """AEGIS r5 R5-2 / r6 R6-1 / r7 R7-1: books money a restore lost that Andre recorded as ``moved``, DERIVED
        from the lost event itself (its verified ledger evidence, or the in-flight operation / payout item of the
        restored books): accounts, amounts, entity and counterparty are never typed. A payout names its payables (the
        payee's, adding up to the net; the item's own when held); a refund its campaign. One money movement is
        booked once (its other items follow), one bank or rail reference books one movement."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"restore/items/{event_id}/correction", body)
            if ent:
                return ent["response"]
            self.require_rules()
            brk, it = self._restore_item(event_id)
            d = it.get("disposition") or {}
            self._booking_checks(it, d, body)
            der = self._derive_money(it)
            if der is None:
                raise Conflict("this lost event cannot be derived (it does not verify, or it is a reversed attempt): "
                               "book it with POST /fin/v1/restore/items/{event_id}/manual-booking (the accounts are "
                               "fixed by the event type)")
            if M.D(der["amount"]) != M.D(d["moved_amount"]):
                raise Conflict(f"the lost event moved {der['amount']}, the disposition says {d['moved_amount']}")
            if der["what"] == "transfer:refund_payment" and not der.get("inflight_op"):
                raise Conflict("a refund's transfer is booked through its refund_paid item")
            pays, prof, rf_row = [], None, None
            if der["what"] == "payout":
                held = self.db["items"].get(it.get("ref") or "")
                own = der.get("own_payables") if der.get("own_payables") is not None else (
                    list(held.get("payable_ids") or []) if held is not None else None)
                pays = self._payables_for(der["payee"], der["amount"], body.get("payable_ids") or [], own)
            elif der["what"] == "refund":
                if body.get("refund_campaign_id") != der["campaign"]:
                    raise Conflict("a refund correction names the refund's campaign (refund_campaign_id)")
                prof = self.db["profiles"].get(der["campaign"])
                if prof is None:
                    raise Conflict("the refund's campaign is not in the restored books")
            elif der.get("inflight_op") and der["what"] == "transfer:refund_payment":
                rf_row = self.db["refunds"][der["ref_id"]]
            elif body.get("payable_ids") or body.get("refund_campaign_id"):
                raise Conflict("this correction settles no payables or refund")
            op = Op(self, f"rcorr|{request_id}", "andre", event_id)
            out = self._book_restore(op, brk, it, event_id, der["entity"], der["lines"], pays, prof, rf_row,
                                     der.get("inflight_op"), der["amount"], d["provider_ref"], "restore_correction")
            resp = self._idem_add(op, key, h, {**out, "derived": der["what"], "ledger_event_ids": op.events,
                                               "request_id": request_id})
            self._commit(op)
            return resp

    def restore_manual_booking(self, request_id: str, event_id: str, body: dict) -> dict:
        """AEGIS r7 R7-2b: the way out for a moved item that cannot be derived (an old summary, a lost evidence row):
        Andre gives the amount, the counterparty, the rail (payouts) and the evidence; the ACCOUNTS are fixed by the
        event type (payout: Dr 2020 payee / Cr the rail account; refund: Dr 2010 campaign / Cr 1020; funding: Dr the
        rail account / Cr 1020), the entity is ZBC. It settles the item and its fence like a derived correction."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"restore/items/{event_id}/manual-booking", body)
            if ent:
                return ent["response"]
            self.require_rules()
            brk, it = self._restore_item(event_id)
            d = it.get("disposition") or {}
            self._booking_checks(it, d, body)
            if self._derive_money(it) is not None:
                raise Conflict("this lost event can be derived: use the restore correction (nothing typed)")
            if it.get("reversed_on_ledger"):
                raise Conflict("this posting attempt was reversed on the ledger: nothing moved (record not_moved)")
            amount = M.D(body["amount"])
            if d.get("moved_amount") and M.D(d["moved_amount"]) != amount:
                raise Conflict(f"the disposition says {d['moved_amount']}")
            cps = d.get("counterparties") or []
            if cps and body["counterparty"] not in cps:
                raise Conflict("the counterparty must be the one recorded on the disposition")
            et = it.get("event_type") or ""
            kind = body.get("transfer_kind")
            fixed = RE.manual_lines(et, kind, amount, body["counterparty"], C.RAIL_ACCOUNT[body.get("rail") or "stripe"])
            if fixed is None:
                raise Conflict("this kind of item books nothing (a release, a vendor payment, a sweep or top-up is "
                               "settled by its own records)")
            da, ds, ca, cs = fixed
            lines = [J.dr(da, amount, ds), J.cr(ca, amount, cs)]
            pays, prof, subs = [], None, list(dict.fromkeys(body.get("submission_ids") or []))
            if da == "2020":
                pays = self._payables_for(body["counterparty"], amount, body.get("payable_ids") or [], None,
                                          allow_none=True)
                if pays and subs:
                    raise Conflict("name the payables or, when the restored books lack them, the submissions")
                if not pays:
                    # AEGIS r7 R7-8: the payables were accrued after the backup. The submissions the payout paid are
                    # named, so a re-sent handoff for them is accrued and settled at once (never paid again)
                    if not subs:
                        raise Conflict("the restored books hold no payable of this payout: name the submissions it "
                                       "paid (submission_ids), so a re-sent handoff for them is settled, not paid")
                    taken = [x for x in subs if any(pp["submission_id"] == x for pp in self.db["payables"].values())
                             or self._restore_paid_submission(x, None)]
                    if taken:
                        raise Conflict(f"submission {taken[0]} already has a payable or a restore booking")
            elif subs:
                raise Conflict("submission_ids belong to a payout booking only")
            elif et == "refund_paid":
                prof = self.db["profiles"].get(body["counterparty"])
                if prof is None:
                    raise Conflict("the refund's campaign is not in the restored books")
            op = Op(self, f"rman|{request_id}", "andre", event_id)
            op.record(derived_id("rman", event_id, request_id), "restore_manual_booking", "andre", event_id,
                      {"event_id": event_id, "amount": M.fmt(amount), "counterparty": body["counterparty"],
                       "reason_sha256": sha({"r": body["reason"]}), "evidence_sha256": body["evidence_sha256"]},
                      f"Andre books a moved restore item by hand: {M.fmt(amount)}")
            out = self._book_restore(op, brk, {**it, "manual_booking": {"reason": body["reason"],
                                                                        "evidence_sha256": body["evidence_sha256"],
                                                                        "counterparty": body["counterparty"],
                                                                        "settles_submissions": subs}},
                                     event_id, "zbc", lines, pays, prof, None, None, amount, d["provider_ref"],
                                     "manual_restore_booking")
            resp = self._idem_add(op, key, h, {**out, "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def _restore_paid_submission(self, submission_id: str, payee_id: Optional[str]) -> Optional[tuple]:
        """AEGIS r7 R7-8: (restore item, booked amount) of an active manual restore booking that named this
        submission as paid (its payable was accrued after the backup), or None."""
        for b in self.db["breaks"].values():
            if b.get("leg") != "restore_reimport_required":
                continue
            for k, it in (b.get("items") or {}).items():
                mb = it.get("manual_booking") or {}
                if it.get("corrected_by") and submission_id in (mb.get("settles_submissions") or []) \
                        and (payee_id is None or mb.get("counterparty") == payee_id):
                    return k, M.D((it.get("disposition") or {}).get("moved_amount") or "0.00"), it.get("corrected_by")
        return None

    def restore_booking_reversal(self, request_id: str, event_id: str, body: dict) -> dict:
        """AEGIS r7 R7-1: an error is never permanent. Andre reverses a restore booking: the exact reversal of the FRC
        entry (same accounts, opposite sides, linked), the payables / refund it settled are un-settled, the item is
        open for booking again (and its counterparty fenced again). Recorded with the reason."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"restore/items/{event_id}/correction/reversal", body)
            if ent:
                return ent["response"]
            self.require_rules()
            brk, it = self._restore_item(event_id)
            eid = it.get("corrected_by")
            if not eid or it.get("covered_by"):
                raise Conflict("this restore item has no booking of its own to reverse")
            orig = self.entries_by_id.get(eid)
            if orig is None or orig["memo_code"] != "FRC":
                raise Conflict("the booking is not a restore correction")
            op = Op(self, f"rrev|{request_id}", "andre", event_id)
            try:
                rev = self._post(op, orig["entity"], J.reversal_lines(orig), "FRC",
                                 {"kind": "restore_correction", "id": event_id}, f"FRC-R|{eid}",
                                 approval_ref=request_id, reverses=eid, actor="andre")
            except PostingRefused as exc:
                raise InvalidReasons("restore booking reversal refused", exc.reasons) from None
            for pid in sorted(set(it.get("settled_payables") or []) | {
                    q["payable_id"] for q in self.db["payables"].values()
                    if q.get("settled_by_restore_correction") == event_id}):   # R7-8: re-sent handoffs too
                p = self.db["payables"].get(pid)
                if p and p.get("settled_by_restore_correction") == event_id:
                    op.put("payables", pid, {**p, "status": "returned", "settled_by_restore_correction": None,
                                             "restore_correction_reversed": event_id})
            rf = self.db["refunds"].get(it.get("settled_refund") or "")
            if rf and rf.get("restored_by_correction") == event_id:
                op.put("refunds", rf["refund_id"], {**rf, "status": "reversed_restore_booking"})
            items = {}
            for k, o in brk["items"].items():
                if k == event_id or o.get("covered_by") == event_id:
                    o = {kk: vv for kk, vv in o.items() if kk not in ("corrected_by", "covered_by", "debt",
                                                                        "settled_payables", "settled_refund",
                                                                        "manual_booking")}
                    if k == event_id:
                        o["reversed_bookings"] = (o.get("reversed_bookings") or []) + [
                            {"entry_id": eid, "reversal_entry_id": rev["entry_id"], "reason": body["reason"],
                             "at": iso(self._now())}]
                items[k] = o
            op.put("breaks", brk["break_id"], {**brk, "items": items})
            op.record(derived_id("rrev", event_id, eid), "restore_correction_reversed", "andre", event_id,
                      {"event_id": event_id, "entry_id": eid, "reversal_entry_id": rev["entry_id"],
                       "reason_sha256": sha({"r": body["reason"]})}, "Andre reversed a restore booking")
            resp = self._idem_add(op, key, h, {"reversal_entry": rev, "ledger_event_ids": op.events,
                                               "request_id": request_id})
            self._commit(op)
            return resp

    def _post_restore_check(self, legs: list, opened: list) -> bool:
        """AEGIS r5 R5-1: called by every reconciliation run: in the post-restore phase, a run whose legs all match
        and that leaves no reconciliation break open ends the phase (money out resumes)."""
        if self.restore_post_phase() is None:
            return False
        if any(r.get("restore_parked") and r.get("held") for r in self.db["rail_events"].values()):
            return False                         # AEGIS r7 R7-6: a parked rail event is still waiting
        pending = self._pending_restore_subjects()
        bad = [leg for leg in legs if leg["status"] not in ("matched", "not_in_use") and not self._explained_by_pending(
            leg.get("subject"), leg.get("difference"), pending)]
        if bad:
            return False
        return not any(b.get("leg") in RECON_LEGS and b["status"] in ("open", "explained")
                       and not self._explained_by_pending(b.get("subject"), b.get("difference"), pending)
                       for b in self.db["breaks"].values())

    def _pending_restore_subjects(self) -> dict:
        """AEGIS r7 R7-2c: unbooked moved items, as {reconciliation subject: unbooked amount}. Their counterparties
        stay fenced; everyone else's money is not held up by them."""
        out: dict = {}
        for b in self.db["breaks"].values():
            if b.get("leg") != "restore_reimport_required":
                continue
            for it in (b.get("items") or {}).values():
                d = it.get("disposition") or {}
                if d.get("disposition") != "moved" or self._debt_settled(it) or it.get("status") == "follows_movement":
                    continue
                der = self._derive_money(it)
                amt = M.D(d.get("moved_amount") or "0.00")
                if der is not None:
                    accts = {(ln["account"], ln.get("subledger")) for ln in der["lines"]}
                else:
                    fx = RE.manual_lines(it.get("event_type") or "", "funding", amt, d.get("counterparty") or "",
                                         C.RAIL_ACCOUNT["stripe"])
                    accts = {(fx[0], fx[1]), (fx[2], fx[3])} if fx else set()
                for a, sub in accts:
                    for subj in (f"zbc:{a}", f"zbc:{a}:{sub}" if sub else None):
                        if subj:
                            out[subj] = M.q(out.get(subj, M.ZERO) + amt)
        return out

    @staticmethod
    def _explained_by_pending(subject, difference, pending: dict) -> bool:
        if not subject or subject not in pending or difference is None:
            return False
        try:
            return abs(M.D(str(difference))) == pending[subject]
        except Exception:  # noqa: BLE001
            return False

    def _post_restore_clear(self, recon_id: str) -> None:
        st = self.restore_post_phase() or {}
        try:
            self._lockdown_write({**st, "state": "lifted", "post_restore_recon_id": recon_id,
                                  "post_restore_clean_at": iso(self._now())})
        except Unavailable:
            pass                              # still in the post phase: the next clean run ends it

    def _restore_rail_item(self, op: Op, rail_name: str, ev: dict) -> None:
        eid = f"rail:{rail_name}:{ev['event_id']}"
        for b in self._restore_open():
            items = dict(b.get("items") or {})
            if eid in items:
                continue
            items[eid] = {"event_id": eid, "event_type": f"rail_{ev['type']}", "kind": "in_flight",
                          "ref": ev["item_id"], "status": "needs_review", "order": -1, "invoice_id": None,
                          "client_id": None, "why": f"the rail reported {ev['type']} for payout item {ev['item_id']}, "
                                                    "which the restored books do not hold: record it as moved or "
                                                    "not_moved"}
            self._restore_progress(op, {**b, "items": items, "fenced": sorted(set(b.get("fenced") or [])
                                                                               | {ev["item_id"]})})

    def _restore_mail_dedupe(self, prefix: str, ids: Optional[list] = None) -> int:
        """AEGIS r5 R5-8 / r7 R7-7: outbox messages and client receipts the books hold as pending but the LEDGER shows
        were sent (Resend accepted them) are marked sent, never sent a second time; Resend's idempotency key covers
        only 24 h. Checked on every send path, with or without a restore break (a restore with no money event too).
        An unreadable ledger sends nothing (503)."""
        with self.lock:
            cands = set(ids) if ids is not None else (
                {k for k, r in self.db["mail_outbox"].items() if r["status"] in ("pending", "sending")}
                | {k for k, r in self.db["client_receipts"].items() if r["status"] in ("pending_send", "sending")})
        sent: set = set()
        if cands and hasattr(self.recorder.client, "entries"):
            try:
                sent = {e.get("subject_id") for e in self.recorder.client.entries()
                        if e.get("event_type") in ("client_mail_sent", "client_receipt_sent")
                        and e.get("subject_id") in cands}
            except Exception:  # noqa: BLE001
                raise Unavailable("the evidence ledger could not be read to check what was already sent; nothing was "
                                  "sent (retry)") from None
        with self.lock:
            sent |= {m for b in self.db["breaks"].values() if b.get("leg") == "restore_reimport_required"
                     for m in (b.get("mail_sent") or [])}
            todo = [r for k, r in self.db["mail_outbox"].items() if k in sent and r["status"] in ("pending", "sending")]
            rcts = [r for k, r in self.db["client_receipts"].items()                    # AEGIS r6 R6-7
                    if k in sent and r["status"] in ("pending_send", "sending")]
            if not todo and not rcts:
                return 0
            op = Op(self, f"{prefix}|maildedupe|{len(self.log)}", I2.ACTOR, "restore-mail")
            for r in todo:
                op.put("mail_outbox", r["mail_id"], {**r, "status": "sent", "sent_before_restore": True})
            for r in rcts:
                op.put("client_receipts", r["client_receipt_id"], {**r, "status": "sent", "sent_before_restore": True})
            self._commit(op)
            return len(todo) + len(rcts)

    def _check_reissued_as(self, it: dict, ref: Optional[str]) -> None:
        """AEGIS r4 R4-7: bound to the item: a lost credit note -> a credit note on the SAME invoice for at least the
        same amount and refund, made after the item; a lost void -> the same invoice voided after the item. Anything
        else is ``lost`` (with the reason) or ``reissue_after_lift``."""
        try:
            entries = self.recorder.client.entries()
        except Exception:  # noqa: BLE001
            raise Unavailable("the evidence ledger could not be read; nothing was disposed of (retry)") from None

        def after(subject, types) -> bool:
            return any(i > int(it.get("order") or 0) and e.get("department") == i10.DEPT
                       and e.get("subject_id") == subject and e.get("event_type") in types
                       for i, e in enumerate(entries))
        if it.get("kind") == "credit_note":
            cn = self.db["credit_notes"].get(ref or "")
            if cn is None or cn.get("invoice_id") != it.get("invoice_id") \
                    or M.D(cn.get("amount") or "0.00") < M.D(it.get("amount") or "0.00") \
                    or M.D(cn.get("to_refund") or "0.00") < M.D(it.get("to_refund") or "0.00"):
                raise Conflict("reissued_as must be a credit note on the same invoice for at least the same amount and "
                               "refund")
            if not after(ref, ("credit_note_issued",)):
                raise Conflict("reissued_as must have been issued after the lost item")
            return
        if it.get("kind") == "void":
            inv = self.db["invoices"].get(ref or "") or {}
            if ref != it.get("invoice_id") or inv.get("status") != "void" or not after(ref, ("invoice_voided",)):
                raise Conflict("reissued_as must be the same invoice, voided after the lost item")
            return
        raise Conflict("only a lost credit note or void is recorded as reissued; record this item as lost (with the "
                       "reason) instead")

    def restore_lockdown_lift(self, request_id: str, body: dict) -> dict:
        """AEGIS r3: the ONLY way out of the restore lockdown, Andre's explicit act, refused until the list of what
        the restore lost is complete, the re-import covers every day, and every item is rebuilt or disposed of."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, "restore/lockdown/lift", body)
            if ent:
                # AEGIS r4 R4-9: a replay tells the state as it is now
                return {**ent["response"], "lifted": not self.restore_locked(), "locked": self.restore_locked()}
            st = self.restore_lockdown() or {}
            if not self.restore_locked():
                raise Conflict("Finance is not in a restore lockdown")
            if self.reconcile_mode or self.reconcile_required:
                raise Conflict("reconcile first (the local log does not match the ledger)")
            open_ = self._restore_open()
            missing = [f"{b['break_id']}: {m}" for b in open_ for m in self._lift_missing(b)]
            ever = [b for b in self.db["breaks"].values() if b.get("leg") == "restore_reimport_required"]
            if not open_ and not ever and not st.get("detected"):
                missing.append("no restore break was committed: reconcile again")
            touched = {it.get("invoice_id") for b in open_ for it in (b.get("items") or {}).values()}
            effects = self._lockdown_effects()
            missing += [f"{e['kind']} on {e['invoice_id']} made during the lockdown touches a restore item"
                        for e in effects if e["invoice_id"] in touched]
            if missing:
                raise Conflict("RESTORE_LOCKDOWN stays on: " + "; ".join(missing), missing=missing)
            try:
                n = len(self.recorder.client.entries())
            except Exception:  # noqa: BLE001
                raise Unavailable("the evidence ledger could not be read; the lockdown stays on (retry)") from None
            op = Op(self, f"lift|{request_id}", "andre", "restore-lockdown")
            lid = rid("lift", request_id)
            res = {"basis": "lifted_by_andre", "reason": body["reason"], "lift_id": lid}
            for b in open_:
                op.put("breaks", b["break_id"], {**b, "status": "resolved", "resolved_at": iso(self._now()),
                                                 "ready_to_lift": True, "resolution": {**res, "covered": b["covered"]}})
                for it in (b.get("items") or {}).values():
                    if (it.get("disposition") or {}).get("disposition") == "reissue_after_lift":
                        rb = rid("brk", "restore_reissue_due", it["event_id"])
                        op.put("breaks", rb, {"break_id": rb, "leg": "restore_reissue_due", "status": "open",
                                              "subject": f"zbm:invoice:{it.get('invoice_id')}", "difference": "0.00",
                                              "opened_at": iso(self._now()), "owner": "andre", "severity": "high",
                                              "explanation_code": "unknown", "receipt_id": None, "resolution": None,
                                              "item": {k: it.get(k) for k in ("event_id", "kind", "invoice_id",
                                                                              "number", "amount", "to_refund",
                                                                              "refund_due_from", "reason_code")},
                                              "note": "re-issue this lost item now that the lockdown is lifted"})
            op.record(derived_id("lift", lid), "restore_lockdown_lifted", "andre", "restore-lockdown",
                      {"lift_id": lid, "breaks": [b["break_id"] for b in open_], "reason_sha256": sha({"r": body["reason"]}),
                       "detected": bool(st.get("detected"))}, "Restore lockdown lifted by Andre")
            resp = self._idem_add(op, key, h, {"lifted": True, "lift_id": lid, "next": POST_PHASE,
                                               "breaks_resolved": [b["break_id"] for b in open_],
                                               "lockdown_effects": effects, "fenced": sorted(self._fenced()),
                                               "ledger_event_ids": op.events, "request_id": request_id})
            # AEGIS r4 R4-9: the file first (a failure here changes nothing), then the commit; if the commit fails
            # the restore breaks stay open, which alone keeps Finance locked (fail closed), and a retry lifts
            prior = getattr(self, "_lockdown", None)
            # AEGIS r5 R5-1: not "lifted" yet: money out waits for a clean reconciliation run after the lift
            self._lockdown_write({"state": POST_PHASE, "ack_ledger_len": n, "lifted_at": iso(self._now()),
                                  "lift_id": lid, "reason": body["reason"]})
            try:
                self._commit(op)
            except BaseException:
                self._lockdown = prior if prior and prior.get("state") == "on" else self._lockdown
                raise
        try:
            self.replay_held_rail_events(f"lift|{request_id}")       # AEGIS r5 R5-5: the parked rail events
        except Unavailable:
            pass                                                      # the rail-sync job applies them later
        return resp

    # ================================================================== 3. the Stripe re-import

    def _period(self, since, until) -> tuple[int, int]:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(self.cfg.business_tz)
        if since > until:
            raise Invalid("since is after until")
        if (until - since).days + 1 > MAX_REIMPORT_DAYS:
            raise Invalid(f"a re-import covers at most {MAX_REIMPORT_DAYS} days; run it in parts")
        if until > self._business_today():
            raise Invalid("until is in the future")
        gte = int(datetime.combine(since, time(0), tz).timestamp())
        lt = int(datetime.combine(until + timedelta(days=1), time(0), tz).timestamp())
        return gte, lt

    def _ledger_tail(self) -> Optional[dict]:
        """AEGIS F2: while a restore break is open, what the evidence ledger recorded AFTER the restored state: the
        credit notes and voids to rebuild, and where each payment and refund was booked (ledger order is the order
        the books saw them in). None without an open restore break; a restore break with no ledger position, or an
        unreadable ledger, refuses (fail closed)."""
        open_ = self._restore_open()
        if not open_:
            return None
        idxs = [b.get("ledger_index") for b in open_]
        if any(i is None for i in idxs):
            raise Conflict("the restore break has no ledger position for the backup: credit notes and voids cannot be "
                           "rebuilt; restore again or rebuild by hand (runbook section 5)")
        start = min(idxs)
        try:
            entries = self.recorder.client.entries()
        except Exception:  # noqa: BLE001
            raise Unavailable("the evidence ledger could not be read; nothing was planned (retry)") from None
        tail = {"payment_at": {}, "refunds": {}}
        for i, e in enumerate(entries):
            if i <= start or e.get("department") != i10.DEPT:
                continue
            et, subj = e.get("event_type"), e.get("subject_id")
            if et in ("receipt_matched", "receipt_unapplied"):
                tail["payment_at"].setdefault(subj, i)
            elif et == "stripe_refund_booked":
                tail["refunds"].setdefault(subj, []).append((i, e))    # verified per payment in the plan (R2-10)
        return tail

    def _refund_steps(self, tail: Optional[dict], rct: str, pi: str) -> list[tuple[int, str]]:
        """AEGIS R2-10: the refund totals the books saw, from VERIFIED ``stripe_refund_booked`` events only."""
        from svc_stripe import REFUND_SUMMARY, refund_payload
        out = []
        for i, e in (tail or {}).get("refunds", {}).get(rct, []):
            m = REFUND_SUMMARY.fullmatch(str(e.get("summary") or ""))
            if not m:
                continue
            delta, total, iid, linked, seq = m.groups()
            payload = refund_payload(rct, pi, delta, total, None if iid == "-" else iid, linked)
            if self._verified(e, derived_id("rfds", rct, total), "stripe_refund_booked", payload, int(seq)):
                out.append((i, total))
        return out

    @staticmethod
    def _verified(e: dict, rk: str, event_type: str, payload: dict, seq: int) -> bool:
        """The ledger keeps a payload's SHA-256, not the payload: a rebuilt fact counts only when it hashes to it."""
        psha = payload_sha256({**payload, "rk": rk, "seq": seq})
        return psha == e.get("payload_sha256") and i10.evidence_id(rk, event_type, psha) == e.get("event_id")

    def _rebuild_actions(self) -> list[dict]:
        """AEGIS R2-1: only items classified ``pending`` at the reconcile (verified intent + authentic commit mark)."""
        out = []
        for b in self._restore_open():
            for it in (b.get("items") or {}).values():
                if it["status"] != "pending":
                    continue
                if it["kind"] == "credit_note" and it["credit_note_id"] not in self.db["credit_notes"]:
                    out.append({"action": "rebuild_credit_note", "order": it["order"], "event_id": it["event_id"],
                                **{k: it[k] for k in ("credit_note_id", "number", "invoice_id", "amount", "to_refund",
                                                       "refund_due_from", "reason_code", "line",
                                                       "mail_sent_before_restore")}})
                elif it["kind"] == "void" and (self.db["invoices"].get(it["invoice_id"]) or {}).get("status") != "void":
                    out.append({"action": "rebuild_void", "order": it["order"], "event_id": it["event_id"],
                                **{k: it[k] for k in ("invoice_id", "amount", "reason_code", "line",
                                                       "mail_sent_before_restore")}})
        return out

    def _reimport_plan(self, g: Gather, since, until) -> dict:
        port = self.ports.stripe_in
        gte, lt = self._period(since, until)
        lists = {k: g.call("stripe", "list", (k, gte, lt), lambda k=k: port.list_objects(k, gte, lt),
                           StripeList(False)) for k in ("payment_intents", "refunds", "disputes")}
        if any(not isinstance(v, StripeList) or not v.available for v in lists.values()):
            raise Unavailable("Stripe could not be listed; nothing was planned (retry)", ledger_event_ids=g.events)
        if any(not v.complete for v in lists.values()):
            raise Conflict("more Stripe objects in this period than one re-import reads: run it for a shorter period")
        with self.lock:
            tail = self._ledger_tail()
        pis = sorted({i["payment_intent"] for v in lists.values() for i in v.items if i.get("payment_intent")})
        disputes = sorted({i["id"] for i in lists["disputes"].items})
        actions: list[dict] = []
        late = 10 ** 12                                   # after everything the ledger orders
        for pi in pis:
            p = g.call("stripe", "payment", (pi,), lambda pi=pi: port.payment(pi), StripePayment(False))
            if not p.available:
                raise Unavailable("a Stripe payment could not be read; nothing was planned (retry)",
                                  ledger_event_ids=g.events)
            if not p.found or p.livemode != self.cfg.stripe_livemode:
                continue
            if p.status != "succeeded" or p.charge_status != "succeeded":
                continue
            rct = rid("rct", "stripe", pi)
            with self.lock:
                rc = self.db["receipts"].get(rct)
                inv = self.db["invoices"].get(p.invoice_id) if p.invoice_id else None
                has_page = inv is not None and any(s["invoice_id"] == inv["invoice_id"]
                                                   for s in self.db["stripe_sessions"].values())
            booked_refund = M.D((rc.get("stripe") or {}).get("refunded") or "0.00") if rc else M.ZERO
            stripe_refund = M.D(p.amount_refunded) if p.amount_refunded is not None else M.ZERO
            # where the books saw this payment and its refunds before the restore (AEGIS F2: ledger order)
            at = (tail or {}).get("payment_at", {}).get(rct, late)
            steps = [(i, tot) for i, tot in self._refund_steps(tail, rct, pi) if M.D(tot) > booked_refund]
            if rc is None:
                if inv is not None and not has_page:
                    sl = g.call("stripe", "sessions_for_payment", (pi,), lambda pi=pi: port.sessions_for_payment(pi),
                                StripeList(False))
                    if not sl.available:
                        raise Unavailable("Stripe's Checkout pages could not be read; nothing was planned (retry)",
                                          ledger_event_ids=g.events)
                    for s in sl.items:
                        if s["client_reference_id"] == inv["invoice_id"] and s["currency"] == "usd" \
                                and s["livemode"] == self.cfg.stripe_livemode and s["status"] == "complete":
                            actions.append({"action": "relink_page", "order": at, "sub": 0, "session_id": s["id"],
                                            "payment_intent": pi, "invoice_id": inv["invoice_id"],
                                            "amount": f"{s['amount_total'] // 100}.{s['amount_total'] % 100:02d}"
                                            if type(s["amount_total"]) is int and s["amount_total"] >= 0 else None,
                                            "methods": [m for m in s["allowed_payment_method_types"]
                                                        if m in ("card", "us_bank_account")]})
                            break
                actions.append({"action": "book_payment", "order": at, "sub": 1, "payment_intent": pi,
                                "amount": p.amount_received, "invoice_id": p.invoice_id,
                                # refunds the ledger saw later are booked at their own place
                                "refund_cap": "0.00" if steps else None})
                if not steps:
                    booked_refund = stripe_refund
            last = booked_refund
            for i, tot in steps:
                cap = min(M.D(tot), stripe_refund)
                if cap > last:
                    actions.append({"action": "book_refund", "order": i, "sub": 2, "payment_intent": pi,
                                    "refund_cap": M.fmt(cap), "invoice_id": p.invoice_id})
                    last = cap
            if stripe_refund != last and p.amount_refunded is not None:
                actions.append({"action": "book_refund", "order": late, "sub": 2, "payment_intent": pi,
                                "refund_cap": None, "refunded_total": p.amount_refunded,
                                "invoice_id": rc.get("invoice_id") if rc else p.invoice_id})
        for du in disputes:
            d = g.call("stripe", "dispute", (du,), lambda du=du: port.dispute(du), StripeDispute(False))
            if not d.available:
                raise Unavailable("a Stripe dispute could not be read; nothing was planned (retry)",
                                  ledger_event_ids=g.events)
            if not d.found or d.livemode != self.cfg.stripe_livemode:
                continue
            with self.lock:
                rec = self.db["stripe_disputes"].get(du) or {}
            posted = set(rec.get("posted_txns") or [])
            if not rec.get("recorded") or any(t["txn_id"] not in posted for t in d.txns) or rec.get("status") != d.status:
                actions.append({"action": "book_dispute", "order": late, "sub": 3, "dispute_id": du,
                                "payment_intent": d.payment_intent, "status": d.status})
        if tail is not None:
            with self.lock:
                actions += [{**a, "sub": 1} for a in self._rebuild_actions()]
        actions.sort(key=lambda a: (a["order"], a["sub"], a.get("payment_intent") or "", a.get("dispute_id") or "",
                                    a.get("credit_note_id") or a.get("invoice_id") or ""))
        for a in actions:
            a.pop("sub", None)
        plan = {"since": since.isoformat(), "until": until.isoformat(), "livemode": self.cfg.stripe_livemode,
                "seen": {"payment_intents": len(lists["payment_intents"].items), "refunds": len(lists["refunds"].items),
                         "disputes": len(disputes)}, "actions": actions}
        plan["plan_sha256"] = sha({k: plan[k] for k in ("since", "until", "livemode", "actions")})
        return plan

    def stripe_reimport(self, request_id: str, body: dict) -> dict:
        if not self.cfg.stripe_incoming:
            raise Refused("Stripe is not wired", [R.item("DEPENDENCY_UNAVAILABLE:stripe", "FIN_STRIPE_INCOMING is not set")])
        key, h, ent = self._idem("andre", request_id, "stripe/reimport", body)
        if ent:
            return ent["response"]
        self.require_rules()
        self._restore_rescan()                       # AEGIS R3-1: an incomplete restore list is read again first
        since, until, dry = body["since"], body["until"], body.get("dry_run", True)
        g = Gather(self, f"reimp|{request_id}", "andre", "stripe-reimport")
        with self._stripe_turn("stripe re-import (plan)"):
            plan = self._reimport_plan(g, since, until)
        params = f"{since.isoformat()}|{until.isoformat()}"
        if dry:
            with self.lock:
                iid = rid("imp", params, plan["plan_sha256"])
                op = Op(self, f"reimp|{request_id}", "andre", iid, g)
                op.record(derived_id("imp", iid), "stripe_reimport_planned", "andre", iid,
                          {"import_id": iid, "since": plan["since"], "until": plan["until"],
                           "plan_sha256": plan["plan_sha256"], "actions": len(plan["actions"])},
                          f"Stripe re-import planned (dry run): {len(plan['actions'])} action(s)")
                op.put("stripe_imports", iid, {"import_id": iid, "params": params, "status": "planned",
                                               "plan_sha256": plan["plan_sha256"], "planned_at": iso(self._now()),
                                               "actions": len(plan["actions"])})
                resp = self._idem_add(op, key, h, {"dry_run": True, "import_id": iid, "plan": plan,
                                                   "ledger_event_ids": op.events, "request_id": request_id})
                self._commit(op)
                return resp
        if not body.get("plan_sha256"):
            raise Conflict("a real re-import names the plan_sha256 of its dry run (run with dry_run=true first)")
        with self.lock:
            iid = rid("imp", params, body["plan_sha256"])
            prior = self.db["stripe_imports"].get(iid)
        if prior is None or prior["status"] != "planned":
            raise Conflict("no dry run of this period with that plan_sha256 is waiting: run the dry run first")
        if plan["plan_sha256"] != body["plan_sha256"]:
            raise Conflict("Stripe's record or Finance's books changed since the dry run: dry-run again and review it",
                           plan_sha256_now=plan["plan_sha256"])
        results = []
        for a in plan["actions"]:
            try:
                results.append({**a, "result": self._reimport_one(request_id, a)})
            except (Unavailable, Conflict, Invalid, InvalidReasons, Refused) as exc:
                results.append({**a, "result": "failed", "why": str(getattr(exc, "reason", exc))[:160]})
        failed = [r for r in results if r["result"] == "failed"]
        clean = False
        # AEGIS R2-2: a rebuild that fails becomes an item for Andre below, so it does not stop the window counting
        to_andre = {r["event_id"] for r in failed if r["action"].startswith("rebuild_")}
        if len(to_andre) == len(failed):
            # AEGIS F1: books == Stripe for this window means a fresh plan for it finds nothing left to do
            with self._stripe_turn("stripe re-import (check)"):
                fresh = self._reimport_plan(Gather(self, f"reimp|{request_id}|check", "andre", "stripe-reimport"),
                                            since, until)["actions"]
            clean = not [a for a in fresh if a.get("event_id") not in to_andre]
        with self.lock:
            op = Op(self, f"reimp|{request_id}|done", "andre", iid, g)
            op.put("stripe_imports", iid, {**self.db["stripe_imports"][iid], "status": "partial" if failed else "done",
                                           "ran_at": iso(self._now()), "failed": len(failed)})
            op.record(derived_id("impd", iid, request_id), "stripe_reimport_run", "andre", iid,
                      {"import_id": iid, "actions": len(results), "failed": len(failed)},
                      f"Stripe re-import ran: {len(results) - len(failed)} booked or present, {len(failed)} failed")
            ready = []
            outcome = {r["event_id"]: r for r in results if r["action"].startswith("rebuild_")}
            for b in self._restore_open():
                items = dict(b.get("items") or {})
                for eid, r in outcome.items():
                    if eid in items:
                        if r["result"] in ("rebuilt", "present"):
                            items[eid] = {**items[eid], "status": "rebuilt"}
                        elif r["result"] == "failed":
                            # AEGIS R2-2: an item that cannot be rebuilt goes to Andre (it never blocks anything else)
                            items[eid] = {**items[eid], "status": "needs_review", "why": r.get("why")}
                upd = {**b, "items": items}
                if clean:
                    upd["covered"] = list(b.get("covered") or []) + [[plan["since"], plan["until"]]]
                if self._restore_progress(op, upd):
                    ready.append(b["break_id"])
            resp = self._idem_add(op, key, h, {"dry_run": False, "import_id": iid, "results": results,
                                               "failed": len(failed), "clean": clean, "ready_to_lift": ready,
                                               "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    @staticmethod
    def _covers(windows: list, since: Optional[str], until: Optional[str]) -> bool:
        """Every day from ``since`` to ``until`` lies in one of the verified windows."""
        if not since or not until:
            return False
        from datetime import date as _d
        day, end = _d.fromisoformat(since), _d.fromisoformat(until)
        spans = [(_d.fromisoformat(a), _d.fromisoformat(b)) for a, b in windows]
        while day <= end:
            if not any(a <= day <= b for a, b in spans):
                return False
            day += timedelta(days=1)
        return True

    def _reimport_one(self, request_id: str, a: dict) -> str:
        """One planned action, booked exactly as a webhook would book it (idempotent per Stripe object)."""
        port = self.ports.stripe_in
        with self._stripe_turn("stripe re-import"):
            subj = a.get("payment_intent") or a.get("dispute_id") or a.get("credit_note_id") or a.get("invoice_id") \
                or "stripe-reimport"
            g = Gather(self, f"reimp1|{request_id}|{a['action']}|{subj}|{a.get('order')}", ACTOR, subj)
            if a["action"] == "relink_page":
                with self.lock:
                    if a["session_id"] in self.db["stripe_sessions"]:
                        return "present"
                    inv = self.db["invoices"].get(a["invoice_id"])
                    if inv is None:
                        return "skipped_no_invoice"
                    op = Op(self, g.op_id, ACTOR, a["invoice_id"], g)
                    op.put("stripe_sessions", a["session_id"], {
                        "session_id": a["session_id"], "invoice_id": a["invoice_id"], "url": None, "expires_at": None,
                        "methods": a["methods"], "total": a["amount"], "status": "complete",
                        "payment_intent": a["payment_intent"], "created_by": "stripe_reimport",
                        "created_at": iso(self._now()), "restored_from_stripe": True})
                    op.record(derived_id("relink", a["session_id"]), "stripe_checkout_relinked", "andre",
                              a["invoice_id"], {"invoice_id": a["invoice_id"], "session_id": a["session_id"]},
                              "A Stripe page Finance lost was re-linked from Stripe (re-import)")
                    for b in list(self.db["breaks"].values()):
                        if b.get("leg") == "stripe_unknown_session" and b.get("session_id") == a["session_id"] \
                                and b["status"] != "resolved":
                            op.put("breaks", b["break_id"], {**b, "status": "resolved", "resolved_at": iso(self._now()),
                                                             "resolution": {"basis": "stripe_reimport_relinked"}})
                    self._commit(op)
                    return "relinked"
            if a["action"] in ("book_payment", "book_refund"):
                return self._reimport_payment(g, a["payment_intent"], a.get("refund_cap"))
            if a["action"] == "rebuild_credit_note":
                return self._rebuild_credit_note(request_id, g, a)
            if a["action"] == "rebuild_void":
                return self._rebuild_void(request_id, g, a)
            d = g.call("stripe", "dispute", (a["dispute_id"],), lambda: port.dispute(a["dispute_id"]),
                       StripeDispute(False))
            if not d.available:
                raise Unavailable("Stripe could not be read")
            with self.lock:
                booked = d.payment_intent and rid("rct", "stripe", d.payment_intent) in self.db["receipts"]
            if d.payment_intent and not booked:
                # a dispute is never booked before its payment (AEGIS H2): the payment first, in this same turn
                self._reimport_payment(Gather(self, f"{g.op_id}|payment", ACTOR, d.payment_intent), d.payment_intent)
            with self.lock:
                op = Op(self, g.op_id, ACTOR, a["dispute_id"], g)
                try:
                    status = self._stripe_dispute(op, d)
                except PostingRefused as exc:
                    raise InvalidReasons("the re-imported dispute could not be posted", exc.reasons) from None
                self._commit(op)
                return status

    def _reimport_payment(self, g: Gather, pi: str, cap: Optional[str] = None) -> str:
        """Book one payment (and its refunds up to ``cap``, the total the ledger saw at that point; None = all of
        Stripe's) exactly as its webhook would; the caller holds the Stripe turn."""
        import dataclasses
        port = self.ports.stripe_in
        p = g.call("stripe", "payment", (pi,), lambda: port.payment(pi), StripePayment(False))
        if not p.available:
            raise Unavailable("Stripe could not be read")
        if cap is not None and p.amount_refunded is not None and M.D(p.amount_refunded) > M.D(cap):
            p = dataclasses.replace(p, amount_refunded=M.fmt(M.D(cap)))
        check = self._payment_activation(g, p)
        with self.lock:
            op = Op(self, g.op_id, ACTOR, pi, g)
            op.compliance_check = check
            try:
                status = self._stripe_payment(op, p)
            except PostingRefused as exc:
                raise InvalidReasons("the re-imported payment could not be posted", exc.reasons) from None
            self._commit(op)
            return status

    # ================================================================== rebuilding from the ledger (AEGIS F2)

    def _rebuild_credit_note(self, request_id: str, g: Gather, a: dict) -> str:
        if a.get("problem"):
            raise Conflict(f"a credit note event on the ledger cannot be read ({a['event_id']}): rebuild it by hand")
        cnid = a["credit_note_id"]
        payload = credit_note_payload(cnid, a["number"], a["invoice_id"], a["amount"], a["to_refund"],
                                      a["reason_code"], a["refund_due_from"])
        e = self._ledger_event(a["event_id"])
        if e is None or e.get("department") != i10.DEPT or \
                not self._verified(e, derived_id("cn", cnid), "credit_note_issued", payload, a["line"]):
            raise Conflict(f"credit note {a['number']}: the ledger's evidence does not verify; not rebuilt")
        with self.lock:
            if cnid in self.db["credit_notes"]:
                return "present"
            inv = self.db["invoices"].get(a["invoice_id"])
            if inv is None:
                raise Conflict(f"credit note {a['number']}: its invoice is not in the restored books")
            amount, to_refund = M.D(a["amount"]), M.D(a["to_refund"])
            st = self._credit_state(inv)
            if amount > st["creditable"] or to_refund > st["refundable"] or (amount - to_refund) > st["ar_open"]:
                raise Conflict(f"credit note {a['number']}: the restored books cannot carry it as it was issued")
            op = Op(self, f"{g.op_id}|cn", "andre", cnid, g)
            split = {"to_ar": M.q(amount - to_refund), "to_refund": to_refund, "state": st,
                     "refund_due_from": a["refund_due_from"]}
            self._book_credit_note(op, inv, amount, split, {"reason_code": a["reason_code"], "note": None},
                                   request_id, cnid=cnid, number=a["number"], rebuilt=True,
                                   # AEGIS R2-4: the client's email goes out again only if it never did
                                   resend_mail=not a.get("mail_sent_before_restore"))
            self._commit(op)
            return "rebuilt"

    def _rebuild_void(self, request_id: str, g: Gather, a: dict) -> str:
        if a.get("problem"):
            raise Conflict(f"a void event on the ledger cannot be read ({a['event_id']}): rebuild it by hand")
        iid = a["invoice_id"]
        e = self._ledger_event(a["event_id"])
        if e is None or e.get("department") != i10.DEPT or not self._verified(e, derived_id("invv", iid), "invoice_voided",
                                           void_payload(iid, a["amount"], a["reason_code"]), a["line"]):
            raise Conflict(f"the void of {iid}: the ledger's evidence does not verify; not rebuilt")
        with self.lock:
            inv = self.db["invoices"].get(iid)
            if inv is None:
                raise Conflict(f"the void of {iid}: the invoice is not in the restored books")
            if inv["status"] == "void":
                return "present"
            due = I2.amount_due(inv)
            if inv["status"] != "issued" or due != M.D(a["amount"]):
                raise Conflict(f"the void of {iid}: the restored invoice is {inv['status']} owing {M.fmt(due)}, not "
                               f"what was voided ({a['amount']})")
            acct, sub = self._credit_account(inv)
            op = Op(self, f"{g.op_id}|void", "andre", iid, g)
            entry_id = None
            if due > 0:
                try:
                    entry_id = self._post(op, "zbm", [J.dr(acct, due, sub), J.cr("1100", due, f"client:{inv['client_id']}")],
                                          "F11v", {"kind": "invoice_void", "id": iid}, f"F11v|{iid}",
                                          approval_ref=request_id)["entry_id"]
                except PostingRefused as exc:
                    raise InvalidReasons("void posting refused", exc.reasons) from None
            now = iso(self._now())
            op.put("invoices", iid, {**inv, "status": "void", "voided_at": now, "voided_by": "andre",
                                     "void_entry_id": entry_id, "void_reason": a["reason_code"],
                                     "rebuilt_from_ledger": True,
                                     "credited": M.fmt(M.D(inv.get("credited") or "0.00") + due),
                                     "credited_ar": M.fmt(M.D(inv.get("credited_ar") or "0.00") + due)})
            op.record(derived_id("invvrb", iid), "invoice_void_rebuilt", "andre", iid,
                      {"invoice_id": iid, "entry_id": entry_id}, "A void was rebuilt from the ledger after a restore")
            self._resolve_deadlines(op, "cancel_or_credit_due", iid, "voided")
            if not a.get("mail_sent_before_restore"):
                self._enqueue_client_mail(op, "invoice_cancelled", inv["client_id"], iid,
                                          {"invoice_id": iid, "amount_cancelled": M.fmt(due),
                                           "cancelled_on": self._business_today().isoformat()})
            self._commit(op)
            return "rebuilt"

    def _ledger_event(self, event_id: str) -> Optional[dict]:
        try:
            for e in self.recorder.client.entries():
                if e.get("event_id") == event_id:
                    return e
        except Exception:  # noqa: BLE001
            raise Unavailable("the evidence ledger could not be read") from None
        return None
