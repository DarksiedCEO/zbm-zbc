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
   ``money_events_acknowledged: true``; then it also opens a ``restore_reimport_required`` break that stays open until
   a Stripe re-import has run for the period. A cleanup that silently forgets paid invoices is not possible.

3. The Stripe re-import (``POST /fin/v1/stripe/reimport``, Andre). Lists the payments, refunds and disputes Stripe
   created in a period (read only) and books every one Finance does not have, through the SAME code a webhook uses
   (idempotent per Stripe object; Compliance asked per payment). A page Finance lost is re-linked from Stripe's own
   Checkout Session (same invoice id, amount and currency) so the payment matches its invoice again. Always a dry run
   first: the real run names the dry run's ``plan_sha256`` and is refused if Stripe's answer changed since.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Optional

import money as M
import reasons as R
from clock import iso, parse_iso
from errors import Conflict, Invalid, NotFound, Unavailable
from intelligences import i01_journal as J
from intelligences import i02_receivables as I2
from intelligences import i10_evidence_audit as i10
from ledger import derived_id, payload_sha256
from ports import StripeDispute, StripeList, StripePayment
from service import EVIDENCE as EVIDENCE_ACTOR, Gather, InvalidReasons, Op, PostingRefused, Refused, rid, sha
from svc_credits import CN_SUMMARY, VOID_SUMMARY, credit_note_payload, void_payload

ACTOR = I2.ACTOR
MAX_REIMPORT_DAYS = 92
alert_log = logging.getLogger("finance.alert")
# ledger event types that are money: a reconcile that voids any of them needs the re-import
MONEY_EVENT_TYPES = ("journal_entry_posted", "receipt_matched", "receipt_unapplied", "invoice_issued",
                     "invoice_approved", "invoice_voided", "credit_note_issued", "credit_note_refund_linked",
                     "stripe_event_applied", "stripe_refund_booked", "stripe_checkout_created", "dispute_opened",
                     "dispute_closed", "compliance_hold_opened", "client_receipt_created")
# AEGIS R2-1/R2-4: effects a restore can lose that Stripe does not hold. Each is an INTENT on the ledger, recorded
# before the local commit; after the commit Finance records an ``effect_committed`` mark naming it (svc_restore
# ``_mark_committed``). Only an intent with its authentic mark is ever rebuilt; any other one is an item for Andre.
EFFECT_TYPES = ("credit_note_issued", "invoice_voided", "deadline_recorded", "client_mail_queued")
COMMIT_TYPE = "effect_committed"
COMMIT_SUMMARY = re.compile(r"Committed (fin-[a-z0-9]{1,12}-[0-9a-f]{40}) at line (\d+) ([0-9a-f]{64}) mac "
                            r"([0-9a-f]{64}|-)")
MAIL_SUMMARY = re.compile(r"Client email queued \(([a-z_]+)\) for (\S+)")
DONE = ("rebuilt", "disposed", "sent_before_restore", "follows_parent", "follows_refund")
PAUSE_ALERT_EVERY_S = 300


class RestoreMixin:

    # ================================================================== 1. quiesce + snapshot marker

    def quiesced(self) -> Optional[dict]:
        """The open backup window, if any. AEGIS R2-6: a window always ENDS (FIN_BACKUP_PAUSE_MAX_MIN, at most 120
        minutes): live, and after a restart (the persisted marker's ``until``). A restarted Finance inside the
        window stays paused (fail closed, AEGIS F6) and logs an ALERT every 5 minutes; a marker left open past its
        window no longer pauses anything but alerts the same way until Andre resumes it."""
        now = self._now()
        q = getattr(self, "_quiesce", None)
        if q is not None:
            if now < q["until"]:
                return q
            self._quiesce = None
        for m in self.db["backup_markers"].values():
            if m.get("status") == "quiesced":
                until = parse_iso(m["until"]) if m.get("until") else now
                if now < until and q is None:
                    self._pause_alert(f"paused_for_backup marker={m['marker_id']} until={m['until']} (restarted inside "
                                      "the window)")
                    return {"marker_id": m["marker_id"], "restarted": True, "until": until}
                self._pause_alert(f"backup_marker_left_open marker={m['marker_id']} window ended {m.get('until')}: "
                                  "resume it (POST /fin/v1/backup/resume)")
        return None

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

    def _evidence_key(self) -> Optional[bytes]:
        """The key of the MAC on commit marks (AEGIS R2-5), derived from FIN_CONTACT_HASH_KEY (domain-separated)."""
        k = getattr(self.cfg, "contact_hash_key", None)
        return hmac.new(k, b"zbm-finance-commit-mark-v1", hashlib.sha256).digest() if k else None

    def _commit_mac(self, intent: str, seq: int, line_sha: str) -> str:
        k = self._evidence_key()
        return hmac.new(k, f"{intent}|{seq}|{line_sha}".encode(), hashlib.sha256).hexdigest() if k else "-"

    def _mark_committed(self, op: Op, seq: int, line_sha: str) -> None:
        """Called by ``_commit`` once a line is appended: each EFFECT intent in it gets its ``effect_committed`` mark.
        A mark that cannot be written is retried by the hourly job; until then a restore treats the intent as
        unproven (an item for Andre, never booked by itself)."""
        for n in getattr(op, "named", []):
            if n.get("event_type") in EFFECT_TYPES:
                self._write_commit_mark(n["event_id"], seq, line_sha)

    def _write_commit_mark(self, intent: str, seq: int, line_sha: str) -> bool:
        mac = self._commit_mac(intent, seq, line_sha)
        try:
            self._record(derived_id("cmt", intent), COMMIT_TYPE, EVIDENCE_ACTOR, intent,
                         {"intent_event_id": intent, "line_seq": seq, "line_sha256": line_sha, "mac": mac},
                         f"Committed {intent} at line {seq} {line_sha} mac {mac}")
            return True
        except Exception:  # noqa: BLE001 - the effect stands; the mark is retried (and a restore asks Andre)
            pend = self.__dict__.setdefault("_unmarked", [])
            if (intent, seq, line_sha) not in pend:
                pend.append((intent, seq, line_sha))
            alert_log.error("ALERT finance: commit_mark_unwritten intent=%s line=%s", intent, seq)
            return False

    def _retry_commit_marks(self) -> int:
        pend = list(self.__dict__.get("_unmarked", []))
        done = [p for p in pend if self._write_commit_mark(*p)]
        self._unmarked = [p for p in self.__dict__.get("_unmarked", []) if p not in done]
        return len(done)

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
            except Exception:  # noqa: BLE001 - unreadable ledger: no position (the rebuild then refuses: fail closed)
                idx = None
        return {"log_head_seq": n, "at": at, "ledger_index": idx, "date": self._business_date(at)}

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

    def _restore_refusal(self, what: str, invoice_id: Optional[str] = None) -> None:
        """AEGIS F2 / R2-2, scoped to the invoices the restore touched. A Stripe page is refused for any invoice the
        lost tail names (a void the restore lost must never become payable). A credit note or void is refused only
        while an item of that invoice is still to be REBUILT automatically (a manual re-issue then would double it);
        an item Andre must review leaves the manual re-issue open, and he then disposes of the item."""
        for b in self._restore_open():
            items = (b.get("items") or {}).values()
            if what == "Stripe page":
                hit = invoice_id in (b.get("affected_invoices") or []) or any(
                    it.get("invoice_id") == invoice_id and it["status"] not in DONE for it in items)
            else:
                hit = any(it.get("invoice_id") == invoice_id and it["status"] == "pending" for it in items)
            if hit:
                raise Conflict(f"Finance was restored from a backup and this invoice has evidence still to be "
                               f"re-imported: no {what} for it until the re-import has rebuilt it or Andre has "
                               "disposed of it (docs/runbooks/restore-and-reimport.md, section 5)")

    def _cn_number_floor(self) -> int:
        return max([int(b.get("cn_number_floor") or 0) for b in self.db["breaks"].values()
                    if b.get("leg") == "restore_reimport_required"] or [0])

    def _scan_tail(self, ledger_index: Optional[int]) -> dict:
        """Every EFFECT the ledger recorded after the restored state, classified (AEGIS R2-1, R2-4, R2-5):
        ``pending`` (a verified intent with its authentic commit mark: the re-import rebuilds it), ``needs_review``
        (unproven, uncommitted, unauthenticated, or a kind never rebuilt automatically: Andre disposes of it), or a
        done state for client emails that follow their parent."""
        if ledger_index is None:
            return {"items": {}, "affected_invoices": [], "cn_number_floor": 0, "scanned": False}
        entries = self.recorder.client.entries()
        tail = [(i, e) for i, e in enumerate(entries) if i > ledger_index and e.get("department") == i10.DEPT]
        marks, sent, affected, floor = {}, set(), set(), 0
        for i, e in tail:
            et = e.get("event_type")
            if et == COMMIT_TYPE:
                m = COMMIT_SUMMARY.fullmatch(str(e.get("summary") or ""))
                if m:
                    marks.setdefault(m.group(1), (e, m))
            elif et == "client_mail_sent":
                sent.add(e.get("subject_id"))
            elif et in ("stripe_checkout_created", "invoice_issued", "invoice_approved"):
                affected.add(e.get("subject_id"))
        items: dict = {}
        for i, e in tail:
            et = e.get("event_type")
            if et not in EFFECT_TYPES:
                continue
            eid = e.get("event_id")
            it = {"event_id": eid, "event_type": et, "order": i, "status": "needs_review", "why": None,
                  "invoice_id": None}
            ok, why = self._mark_ok(eid, marks)
            if et == "credit_note_issued":
                m = CN_SUMMARY.fullmatch(str(e.get("summary") or ""))
                if not m:
                    it["why"] = "unreadable credit note evidence"
                else:
                    number, cnid, iid, amount, to_refund, due_from, reason, seq = m.groups()
                    floor = max(floor, int(number.rsplit("-", 1)[1]))
                    it.update(kind="credit_note", ref=cnid, credit_note_id=cnid, number=number, invoice_id=iid,
                              amount=amount, to_refund=to_refund, refund_due_from=None if due_from == "-" else due_from,
                              reason_code=reason, line=int(seq))
                    payload = credit_note_payload(cnid, number, iid, amount, to_refund, reason, it["refund_due_from"])
                    verified = self._verified(e, derived_id("cn", cnid), et, payload, int(seq))
                    it["why"] = why if not ok else (None if verified else "the summary does not hash to the payload")
                    if ok and verified:
                        it["status"] = "pending"
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
        return {"items": items, "affected_invoices": sorted(a for a in affected if a),
                "cn_number_floor": floor, "scanned": True}

    def _mark_ok(self, intent: str, marks: dict) -> tuple[bool, Optional[str]]:
        hit = marks.get(intent)
        if hit is None:
            return False, "an attempt only: no commit mark (the operation answered an error, or its mark was lost)"
        e, m = hit
        if e.get("event_id") != derived_id("cmt", intent):
            return False, "the commit mark's id does not match"
        mac = m.group(4)
        if mac == "-" or self._evidence_key() is None:
            return False, "the commit mark is not authenticated (FIN_CONTACT_HASH_KEY not set)"
        want = self._commit_mac(intent, int(m.group(2)), m.group(3))
        if not hmac.compare_digest(mac, want):
            return False, "the commit mark's MAC does not verify (not written by this Finance)"
        psha = payload_sha256({"intent_event_id": intent, "line_seq": int(m.group(2)), "line_sha256": m.group(3),
                               "mac": mac})
        if psha != e.get("payload_sha256"):
            return False, "the commit mark's summary does not hash to its payload"
        return True, None

    def _restore_reimport_break(self, plan: dict) -> Optional[str]:
        """Committed right after the reconcile line: a break that stays open until re-imports have covered the whole
        window from the backup's day to the day the break opened, with nothing left missing (CH-3, AEGIS F1), and
        every lost item is rebuilt or disposed of by Andre (AEGIS R2-2)."""
        if not plan.get("money_at_risk"):
            return None
        rp = plan.get("restore_point") or {}
        since = rp.get("date") or self._today_la().isoformat()
        bid = rid("brk", "restore_reimport_required", plan["head_sha256"] or "empty")
        if bid in self.db["breaks"]:
            return bid
        try:
            scan = self._scan_tail(rp.get("ledger_index"))
        except Exception:  # noqa: BLE001 - unreadable: the break opens with nothing rebuildable (fail closed)
            scan = {"items": {}, "affected_invoices": [], "cn_number_floor": 0, "scanned": False}
        op = Op(self, f"restore|{bid}", "andre", bid)
        op.put("breaks", bid, {"break_id": bid, "leg": "restore_reimport_required", "subject": "zbm:1060",
                               "difference": "0.00", "opened_at": iso(self._now()),
                               "opened_on": self._business_today().isoformat(), "owner": "andre", "severity": "high",
                               "explanation_code": "unknown", "status": "open", "receipt_id": None,
                               "since": since, "restore_point": rp, "ledger_index": rp.get("ledger_index"),
                               "covered": [], "money_events": len(plan["money_at_risk"]), "resolution": None,
                               **scan,
                               "note": f"restored from a backup of {since}: run POST /fin/v1/stripe/reimport (dry run, "
                                       f"then real) for every day from {since} to {self._business_today().isoformat()}; "
                                       "dispose of every item it cannot rebuild"})
        op.record(derived_id("brk", bid), "break_opened", "intel_07_reconciliation", bid,
                  {"break_id": bid, "leg": "restore_reimport_required", "difference": "0.00"},
                  "Break opened: restored from a backup; Stripe re-import required")
        self._commit(op)
        return bid

    @staticmethod
    def _items_done(b: dict) -> bool:
        return all(it["status"] in DONE for it in (b.get("items") or {}).values())

    def _maybe_close_restore(self, op: Op, b: dict, basis: dict) -> bool:
        if b.get("scanned") is False and b.get("ledger_index") is None and b.get("money_events"):
            return False                          # the lost tail could not be read: Andre restores again or by hand
        if self._covers(b.get("covered") or [], b.get("since"), b.get("opened_on")) and self._items_done(b):
            op.put("breaks", b["break_id"], {**b, "status": "resolved", "resolved_at": iso(self._now()),
                                             "resolution": {**basis, "covered": b.get("covered")}})
            return True
        op.put("breaks", b["break_id"], b)
        return False

    def restore_item_disposition(self, request_id: str, event_id: str, body: dict) -> dict:
        """AEGIS R2-2: Andre's decision on one lost item the re-import does not (or cannot) rebuild: ``lost`` (with
        the reason) or ``reissued`` (naming what he re-issued: a credit note, a voided invoice, a deadline break or a
        client email). Recorded on the ledger; the item leaves the plan."""
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
            if body["disposition"] == "reissued":
                ref = body.get("reissued_as")
                known = ref in self.db["credit_notes"] or ref in self.db["breaks"] or ref in self.db["mail_outbox"] \
                    or (self.db["invoices"].get(ref) or {}).get("status") == "void"
                if not known:
                    raise Conflict("reissued_as must name what was re-issued: a credit note, a voided invoice, a "
                                   "deadline break or a client email")
            new_items = {**b["items"], event_id: {**it, "status": "disposed",
                                                  "disposition": {"disposition": body["disposition"],
                                                                  "reason": body["reason"],
                                                                  "reissued_as": body.get("reissued_as"),
                                                                  "at": iso(self._now())}}}
            op = Op(self, f"rdisp|{request_id}", "andre", event_id)
            op.record(derived_id("rdisp", event_id), "restore_item_disposed", "andre", event_id,
                      {"event_id": event_id, "disposition": body["disposition"],
                       "reason_sha256": sha({"r": body["reason"]}), "reissued_as": body.get("reissued_as")},
                      f"Restore item disposed: {body['disposition']}")
            closed = self._maybe_close_restore(op, {**b, "items": new_items}, {"basis": "dispositions"})
            resp = self._idem_add(op, key, h, {"event_id": event_id, "status": "disposed", "break_closed": closed,
                                               "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
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
            closed = []
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
                if self._maybe_close_restore(op, upd, {"basis": "stripe_reimport", "import_id": iid}):
                    closed.append(b["break_id"])
            resp = self._idem_add(op, key, h, {"dry_run": False, "import_id": iid, "results": results,
                                               "failed": len(failed), "clean": clean, "breaks_resolved": closed,
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
