"""
Delivery (28) service layer: state, record-first plumbing, the run lifecycle, evidence (spec §A, §B, §C.8, §E).

Order of every state change (ADR 0006 decisions 4-5 / finance-py pattern), without exception:
  1. the operation's own ledger events are recorded (deterministic ``dlv-<abbrev>-<40 hex>`` ids);
  2. ONE local-log line holding the operation's records is anchored on the ledger (``local_log_appended``), then
     appended (fsynced);
  3. only then is it applied to memory (and an evidence file written, 0444) and answered.
A failure at 1-2 raises ``Unavailable`` (HTTP 503 / the engine marks the run failed): nothing was created, started,
committed or reported. State is event-sourced from the log; no data dir → in-memory (``/health`` says so) and no
run survives a restart (S9: a run found in a live state at start-up is recorded ``fix_run_failed``).

Runs execute one at a time on a worker thread (``_worker``) inside ``FixEngine.execute``'s exception boundary.

Wave 23: a finding's engine end state is ``candidate_passed_checks`` (D1: the engine never claims "fixed"); only
``review`` (caller ``aegis``, an explicit verdict per finding, every review flag of an accepted finding named) sets
``accepted``. The admission RED check of reviewer-authored reproductions runs its containers OUTSIDE the service lock
(B2, N22-D-5): a recorded ``reproduction_red_check_started`` reserves the service's run slot (a pending admission is
in flight for the service), the lock is released while the containers run, and the result is recorded and the run
admitted (or refused) in ONE later hold. A failing review is refused 409 before anything is recorded while another
run (or pending admission) of the service is in flight, and its child run is created in the same critical section
and the same local-log line as the review itself (B1, N22-D-4).

Round 19 R5: ``run_update`` / ``finding_update`` / ``finding_transition`` refuse (``Conflict``) on a run that is not
live (failed, reviewed or awaiting review) — after ``fix_run_cancelled`` or the deadline nothing is recorded on the
run except the post-mortem events (``run_interrupted``, ``sandbox_released``, ``agent_usage``), and nothing that is
not recorded takes effect.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from zbm_delivery import evidence_audit as EA
from zbm_delivery import fsops, registry
from zbm_delivery import reasons as R
from zbm_delivery import textguard
from zbm_delivery.clock import Clock, SystemClock, iso, parse_iso
from zbm_delivery.config import POLICY_VERSION, Settings
from zbm_delivery.engine import states
from zbm_delivery.errors import Conflict, DlvError, Forbidden, Invalid, NotFound, Refused, Unavailable
from zbm_delivery.ledger import LedgerConflict, LedgerQueryFailed, LedgerRecordError, Recorder, canonical, derived_id
from zbm_delivery.models import ID_RE
from zbm_delivery.ports import NoChatBackend
from zbm_delivery.gitport import GitRefused
from zbm_delivery.runner import detect_framework, node_id_in_text, reproduction_problem, same_test
from zbm_delivery.store import RecordLog, StoreWriteError

IDEMPOTENCY_WINDOW = timedelta(minutes=15)
IDEMPOTENCY_MAX = 200_000
EVIDENCE = EA.ACTOR
GATE = "intel_01_gate"
ENGINE = "intel_08_engine"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_SUBJECT_BAD = __import__("re").compile(r"[^A-Za-z0-9._:-]")
EVIDENCE_KINDS = ("brief", "test_output", "suite_output", "diff", "report", "review", "prompt_manifest",
                  "config_snapshot", "sandbox_exec")
EVIDENCE_MAX_BYTES = 8 * 1024 * 1024
# R5: the only run events accepted once a run is no longer live (bookkeeping of a stopped run)
POST_MORTEM_EVENTS = ("run_interrupted", "sandbox_released", "agent_usage", "agent_usage_linked")
# the finding states a run can end awaiting review with (wave 23, D1: the engine's end states; none is "fixed")
DONE_STATES = states.ENGINE_DONE_STATES
# a run in one of these holds its service's run slot (B1/B2: so does a pending admission)
LIVE_RUN_STATES = ("received", "preparing", "running", "suite", "reporting")


def _note_problem(note: str) -> Optional[str]:
    """Wave 24 (E2): why a review note is not a note — shorter than REVIEW_NOTE_MIN characters, or one character
    making up more than half of it (``xxxx…``, ``a.a.a.…``); None when it is acceptable as text. (Whether it is TRUE
    is the reviewer's attestation, never the engine's.)"""
    from collections import Counter

    from zbm_delivery.models import REVIEW_NOTE_MIN
    note = (note or "").strip()
    if len(note) < REVIEW_NOTE_MIN:
        return f"the note is shorter than {REVIEW_NOTE_MIN} characters"
    ch, n = Counter(note).most_common(1)[0]
    if n * 2 > len(note):
        return f"the note is filler ({ch!r} is {n} of its {len(note)} characters)"
    return None


def rid(prefix: str, *parts: Any) -> str:
    """``dlv-<prefix>-`` + 26 Crockford base32 characters derived from the identity (retries are idempotent)."""
    d = hashlib.sha256(canonical(list(parts)).encode("utf-8", "surrogatepass")).digest()
    n = int.from_bytes(d[:17], "big")
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[n & 31])
        n >>= 5
    return f"dlv-{prefix}-" + "".join(out)


def sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8", "surrogatepass")).hexdigest()


def sha_text(s: str) -> str:
    return hashlib.sha256(str(s).encode("utf-8", "surrogatepass")).hexdigest()


def facts_sha256(facts: Any) -> str:
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8", "surrogatepass")).hexdigest()


def evidence_id_for(content: bytes) -> str:
    return "dlv-ev-" + hashlib.sha256(content).hexdigest()[:26]


class Op:
    """One operation: its ledger events and its local-log records."""

    def __init__(self, svc: "DeliveryService", op_id: str, actor: str, subject: str):
        self.svc, self.op_id, self.actor, self.subject = svc, op_id, actor, subject[:128]
        self.events: list[str] = []
        self.ops: list[tuple[str, dict]] = []
        self.files: list[tuple[str, str, bytes]] = []  # (run_id, evidence_id, content) written after the commit

    def record(self, event_id: str, event_type: str, actor: str, subject: str, payload: dict, summary: str) -> str:
        self.svc._record(event_id, event_type, actor, subject[:128], payload, summary)
        if event_id not in self.events:
            self.events.append(event_id)
        return event_id

    def add(self, kind: str, rec: dict) -> dict:
        self.ops.append((kind, rec))
        return rec


class DeliveryService:
    def __init__(self, settings: Settings, recorder: Recorder, log: RecordLog, *, gate_report, docker, egress,
                 chat_backend, git, harness_factory: Optional[Callable], provider_factory: Optional[Callable],
                 prompts: dict, policy_seed: dict, test_seed: dict, clock: Optional[Clock] = None,
                 docker_available: Optional[Callable[[], bool]] = None):
        self.cfg = settings
        self.recorder = recorder
        self.log = log
        self.gate = gate_report
        self.docker = docker
        self.egress = egress
        self.chat_backend = chat_backend
        self.git = git
        self.prompts = prompts
        self.policy_seed = policy_seed
        self.test_seed = test_seed
        self.clock = clock or SystemClock()
        self.lock = threading.RLock()
        self._docker_available = docker_available or (lambda: False)
        self.runs: dict[str, dict] = {}
        self.findings: dict[str, dict[str, dict]] = {}
        self.finding_docs: dict[str, dict[str, dict]] = {}       # run_id -> finding_id -> the §B.1 finding (data)
        self.evidence_index: dict[str, dict[str, dict]] = {}     # run_id -> evidence_id -> meta
        self._evidence_mem: dict[str, bytes] = {}
        self.idem: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
        self._ledger_conflict = False
        self.instance_id = secrets.token_hex(8)
        self.reconcile_mode = settings.reconcile_mode
        self.reconcile_required: list[str] = []
        self._reconciling = False
        self._replaying = True
        self.evidence_root = os.path.join(settings.data_dir, "evidence") if settings.data_dir else ""
        if self.evidence_root:
            if os.path.exists(self.evidence_root) and not os.path.isdir(self.evidence_root):
                raise RuntimeError("refusing to start: the evidence root exists and is not a directory (evidence dir not writable)")
            try:
                os.makedirs(self.evidence_root, exist_ok=True)
                import tempfile
                with tempfile.TemporaryFile(dir=self.evidence_root) as probe:   # an unnamed, self-removing probe
                    probe.write(b"")
            except OSError as exc:
                raise RuntimeError(f"refusing to start: the evidence root is not writable ({type(exc).__name__})") from None
            fsops.protect(self.evidence_root)
            fsops.protect(os.path.join(settings.data_dir, "dlv_log.jsonl"))
        self._queue: "queue.Queue[str]" = queue.Queue()
        # B2 (wave 23): admissions whose reviewer-test RED containers are running outside the lock — admission id ->
        # {service, request_id, review_of}; each holds its service's run slot until it is admitted or refused
        self._admissions: dict[str, dict] = {}
        # wave 24 (E6, N23-D-7): admissions cancelled while their containers ran — refused when the containers end
        self._cancelled_admissions: set[str] = set()
        self._engine = None
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        for rec in self.log.iter_records():
            for kind, r in rec["data"].get("ops", []):
                self._apply(kind, r)
        self._replaying = False
        if not self.log.in_memory:
            try:
                a = self.assess_log()
            except LedgerQueryFailed as exc:
                if len(self.log):
                    raise RuntimeError(f"refusing to start: the local log cannot be verified against the evidence ledger "
                                       f"({exc})") from None
                # an EMPTY local log with no readable ledger (spec C.1.2: the service starts, /health says
                # ledger: unconfigured, every run is 503): nothing exists yet that could have been forged or lost
                a = EA.Assessment()
            if a.fatal or (a.voidable and not self.reconcile_mode):
                hint = ("" if a.fatal or not a.voidable else
                        " -- only Andre can void these: start with DLV_RECONCILE_MODE=1 and POST /dlv/v1/reconcile")
                raise RuntimeError("refusing to start: " + "; ".join(a.problems) + hint)
            self.reconcile_required = list(a.voidable)
        registry.install(registry.Runtime(
            settings=settings, recorder=recorder, docker=docker, chat_backend=chat_backend, egress=egress,
            policy_seed=policy_seed, test_seed=test_seed, clock=self.clock, on_ledger_failure=self._on_ledger_failure,
            record=self._record_plain, resolve_sandbox_path=self._resolve_sandbox_path,
            resolve_sandbox_paths=self._resolve_sandbox_paths, evidence_root=self.evidence_root,
            record_if_live=self._record_if_live))
        if self.reconcile_mode:
            return
        if not self.log.in_memory and len(self.log):
            try:
                self._write_lease()
            except Unavailable as exc:
                raise RuntimeError(f"refusing to start: the instance lease could not be recorded ({exc.reason})") from None
        self._fail_live_runs_at_start()
        self._rescan_legacy_runs()
        self._reap("start")
        if harness_factory is not None and provider_factory is not None:
            from zbm_delivery.engine.loop import FixEngine
            self._engine = FixEngine(self, settings, git, harness_factory, provider_factory, prompts, test_seed, policy_seed)
            self._worker = threading.Thread(target=self._work, name="dlv-engine", daemon=True)
            self._worker.start()

    # ================================================================== plumbing

    def now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def now_iso(self) -> str:
        return iso(self.now())

    @staticmethod
    def parse_time(value: str) -> datetime:
        return parse_iso(value)

    def _record(self, event_id, event_type, actor, subject, payload, summary) -> str:
        self._ledger_conflict = False
        if self.reconcile_mode and not self._reconciling:
            raise Unavailable("reconcile mode (DLV_RECONCILE_MODE=1): only Andre's POST /dlv/v1/reconcile is answered; "
                              "restart without it once the log is reconciled", ledger_write="not_recorded")
        subject = _SUBJECT_BAD.sub("-", str(subject))[:128] or "delivery"
        if textguard.secret_shapes_in(payload):
            raise Unavailable("refusing to record a payload with a secret-shaped value", ledger_write="not_recorded")
        try:
            return self.recorder.record(event_id, event_type, actor, subject, payload, summary)
        except LedgerRecordError as exc:
            self._ledger_conflict = isinstance(exc, LedgerConflict)
            raise Unavailable(f"evidence ledger write failed ({type(exc).__name__}); nothing took effect",
                              ledger_write="unknown" if exc.took_effect != False else "not_recorded") from None  # noqa: E712

    def _record_plain(self, event_id, event_type, actor, subject, payload, summary) -> str:
        """The adapters' record callback (guardrail, sandbox, egress, git): a ledger event AND a local-log line,
        anchored first — the same discipline as every API operation. Raises ``Unavailable``."""
        with self.lock:
            op = Op(self, event_id, actor, subject)
            op.record(event_id, event_type, actor, subject, payload, summary)
            op.add("event", {"event_id": event_id, "event_type": event_type, "actor": actor, "subject": subject,
                             "payload": payload})
            self._commit(op)
            return event_id

    def _record_if_live(self, status_fn, event_id, event_type, actor, subject, payload, summary) -> bool:
        """Wave 22 (G4, N21-D-4): ``_record_plain`` only while ``status_fn()`` is live — the status read and the record
        under ONE hold of the service lock. Cancel and the deadline change a run's status under the same lock, so an
        engine container is either recorded started before ``fix_run_cancelled`` (and killed by the interrupt that
        follows it) or never recorded started at all."""
        with self.lock:
            if status_fn() in states.TERMINAL or status_fn() == "awaiting_review":
                return False
            self._record_plain(event_id, event_type, actor, subject, payload, summary)
            return True

    def _commit(self, op: Op) -> None:
        if not op.ops:
            return
        data = {"ops": [[k, r] for k, r in op.ops], "ledger_event_ids": list(op.events), "anchored": True}
        rec, line = self.log.prepare(op.ops[0][0], iso(self.now()), data)
        line_sha = hashlib.sha256(line).hexdigest()
        epoch = self.log.epoch or line_sha[:16]
        self._record(EA.anchor_id(epoch, rec["seq"], line_sha), EA.ANCHOR_TYPE, EVIDENCE, EA.LOG_SUBJECT,
                     {"epoch": epoch, "seq": rec["seq"], "line_sha256": line_sha, "kind": rec["kind"]},
                     f"Local log line {rec['seq']} ({rec['kind']}) anchored")
        try:
            self.log.append_prepared(rec, line)
        except StoreWriteError as exc:
            raise Unavailable(f"local store write failed ({exc}); nothing took effect") from None
        for kind, r in op.ops:
            self._apply(kind, r)
        for run_id, ev_id, content in op.files:
            self._write_evidence_file(run_id, ev_id, content)

    def _apply(self, kind: str, r: dict) -> None:
        if kind == "run":
            self.runs[r["run_id"]] = r
        elif kind == "finding":
            if r.get("state") in states.LEGACY_FINDING_STATES:       # D1 migration alias: "fixed" read as the new name
                r = {**r, "state": states.normalize_finding_state(r["state"])}
            self.findings.setdefault(r["run_id"], {})[r["finding_id"]] = r
        elif kind == "finding_doc":
            self.finding_docs.setdefault(r["run_id"], {})[r["finding"]["id"]] = r["finding"]
        elif kind == "evidence":
            self.evidence_index.setdefault(r["run_id"], {})[r["evidence_id"]] = r
        elif kind == "idem":
            key = (r["principal"], r["request_id"])
            self.idem[key] = {"h": r["h"], "at": parse_iso(r["at"]), "response": r["response"]}
            self.idem.move_to_end(key)
            while len(self.idem) > IDEMPOTENCY_MAX:
                self.idem.popitem(last=False)
        # event, lease, reconcile, founder_refused, injection, audit: evidence only

    def _write_evidence_file(self, r_run_id: str, evidence_id: str, content: bytes) -> None:  # noqa: N803
        if not self.evidence_root:
            self._evidence_mem[evidence_id] = content
            return
        run_dir = os.path.join(self.evidence_root, r_run_id)
        os.makedirs(run_dir, exist_ok=True)
        path = os.path.join(run_dir, evidence_id)
        if os.path.exists(path):
            return                          # content-addressed: same id = same bytes
        tmp = path + ".part"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o444)
        os.replace(tmp, path)

    # --- idempotency (15 min; different body → 409; the stored answer persists with the operation) ---------------------

    def _idem(self, principal: str, request_id: str, route: str, body: Any) -> tuple[tuple, str, Optional[dict]]:
        if not isinstance(request_id, str) or not ID_RE.fullmatch(request_id):
            raise Invalid("request_id must be 1-128 characters of [A-Za-z0-9._:-]")
        key = (principal, request_id)
        h = sha({"route": route, "body": body})
        ent = self.idem.get(key)
        if ent is not None:
            if ent["h"] != h:
                raise Conflict("request_id already used with a different body")
            if self.now() - ent["at"] > IDEMPOTENCY_WINDOW:
                raise Conflict("request_id reused (first used more than 15 minutes ago)")
        return key, h, ent

    def _idem_add(self, op: Op, key: tuple, h: str, response: dict) -> dict:
        op.add("idem", {"principal": key[0], "request_id": key[1], "h": h, "at": iso(self.now()), "response": response})
        return response

    # --- log vs ledger ---------------------------------------------------------------------------------------------------

    def assess_log(self) -> EA.Assessment:
        client = self.recorder.client
        if not hasattr(client, "entries"):
            raise LedgerQueryFailed("this ledger client cannot read entries")
        entries = client.entries()
        shas = self.log.line_shas()
        lines, referenced, leases, reconciles = [], set(), [], []
        rulings: set[str] = set()
        for rec, s in zip(self.log.iter_records(), shas, strict=False):
            d = rec["data"]
            lines.append((rec["seq"], s, bool(d.get("anchored"))))
            referenced.update(d.get("ledger_event_ids") or [])
            for kind, r in d.get("ops", []):
                if kind == "lease":
                    leases.append((rec["seq"], r.get("instance_id"), r.get("lease_event_id")))
                elif kind == "reconcile":
                    reconciles.append((rec["seq"], r.get("payload"), r.get("reconcile_event_id"), None))
        return EA.assess(entries, self.log.epoch, lines, referenced, 0, strict=not self.log.in_memory,
                         local_rulings=rulings, local_leases=leases, reconciles=reconciles, local_versions={})

    def _write_lease(self) -> None:
        with self.lock:
            head = len(self.log)
            head_sha = self.log.line_shas()[-1] if head else "0" * 64
            eid = EA.lease_id(self.log.epoch or "0" * 16, self.instance_id, head, head_sha)
            op = Op(self, f"lease|{eid}", GATE, EA.LOG_SUBJECT)
            op.record(eid, EA.LEASE_TYPE, GATE, EA.LOG_SUBJECT, {"instance_id": self.instance_id, "head_seq": head,
                                                                    "head_sha256": head_sha}, "Instance lease recorded")
            op.add("lease", {"instance_id": self.instance_id, "lease_event_id": eid, "head_seq": head})
            self._commit(op)

    def _fail_live_runs_at_start(self) -> None:
        for run_id, run in list(self.runs.items()):
            if run.get("status") not in states.TERMINAL and run.get("status") != "awaiting_review":
                try:
                    self.run_transition(run_id, "failed", "fix_run_failed",
                                        {"run_id": run_id, "code": "HARNESS_ERROR", "why": "restart"},
                                        f"Run {run_id} was live at restart: failed",
                                        {"reasons": [R.item("HARNESS_ERROR", "the service restarted while the run was live")],
                                         "finished_at": self.now_iso()})
                except Unavailable:
                    self.mark_failed_unrecorded(run_id, R.item("HARNESS_ERROR", "restart; ledger unavailable"))

    def _rescan_legacy_runs(self) -> None:
        """Wave 24 (E5, N23-D-5): a run awaiting review whose records predate the review flags or the embedded source
        diff (no ``review_flags`` on a finding, or no ``src_diff_sha256`` on the run — every record written before
        waves 23/24) is re-scanned from its commits at load: flags recomputed per committed finding, the complete
        source diff recorded, the report regenerated, and ONE record-first operation (``run_rescanned_for_review``
        on the ledger, then the local-log line) puts the new records in place. Its old report (the legacy "fixed"
        wording) is never served again. A run that cannot be re-scanned (no git, a commit or framework that cannot
        be read) is failed — it cannot be reviewed on records nobody can check."""
        for run_id, run in sorted(self.runs.items()):
            if run.get("status") != "awaiting_review":
                continue
            recs = self.findings.get(run_id, {})
            if run.get("src_diff_sha256") and all("review_flags" in r for r in recs.values()):
                continue
            try:
                self._rescan_run(run_id)
            except Unavailable:
                raise RuntimeError(f"refusing to start: the legacy run {run_id} could not be re-scanned "
                                   "(the ledger or the store is unavailable)") from None
            except Exception as exc:  # noqa: BLE001 - any reason the records cannot be rebuilt: the run is failed
                try:
                    self.run_transition(run_id, "failed", "fix_run_failed",
                                        {"run_id": run_id, "code": "EVIDENCE_UNAVAILABLE", "why": "legacy_rescan_failed",
                                         "error": type(exc).__name__},
                                        f"Legacy run {run_id} could not be re-scanned: failed",
                                        {"reasons": [R.item("EVIDENCE_UNAVAILABLE",
                                                            f"legacy run not re-scannable ({type(exc).__name__})")],
                                         "finished_at": self.now_iso()})
                except Unavailable:
                    raise RuntimeError(f"refusing to start: the legacy run {run_id} could neither be re-scanned nor "
                                       "failed (the ledger is unavailable)") from None

    def _rescan_run(self, run_id: str) -> None:
        from zbm_delivery.engine import report as report_mod
        from zbm_delivery.engine import review_flags as RF
        from zbm_delivery.engine import srcdiff
        from zbm_delivery.runner import path_class

        if self.git is None:
            raise GitRefused("no git port")
        run = json.loads(json.dumps(self.runs[run_id]))
        service, base = run["service"], run["base_sha"]
        head = run["commits"][-1]["sha"] if run.get("commits") else base
        fw = srcdiff.framework_at(self.git, self.test_seed, service, head, run_id)
        if fw is None:
            raise GitRefused("no seeded framework at the run's head")
        tg, ig = fw.get("test_file_globs", []), fw.get("test_infra_globs", [])
        prefix = f"services/{service}/"

        def is_src(p: str) -> bool:
            return not p.startswith(prefix) or path_class(p[len(prefix):], tg, ig) == "src"

        repo = self.git.repo
        reviewer = {rt["path"]: rt["content"] for rt in self.reviewer_tests(run_id)}
        findings = []
        for fid, rec0 in sorted(self.findings.get(run_id, {}).items()):
            rec = json.loads(json.dumps(rec0))
            flags: list[dict] = []
            sha = rec.get("commit_sha")
            if sha:
                diff = self.git.commit_diff(repo, sha, run_id)
                flags = RF.number(RF.scan(diff, service, is_test=lambda p: not is_src(p),
                                          file_text=lambda p, _s=sha: self.git.show_file(_s, p, repo, run_id)), fid)
            solo = rec.get("standalone_check") or {}
            if rec.get("state") == "needs_review_runner_dependent" or solo.get("outcome") == "runner_dependent":
                target = solo.get("target") or ""
                path, _, name = target.partition("::")
                text = reviewer.get(path)
                if text is None and path:
                    text = self.git.show_file(head, f"{prefix}{path}", repo, run_id)
                why = (solo.get("verification") or {}).get("why") or (solo.get("reverted") or {}).get("why") or "-"
                flags.append(RF.runner_dependent_flag(fid, target, f"{prefix}{path}", RF.def_line(text, name), why))
            rec["review_flags"] = flags
            findings.append(rec)
        text, paths = srcdiff.src_diff(self.git, fw, service, repo, base, head, run_id)
        ev_src, digest, problem = srcdiff.record(self, run_id, text, paths, base, head, "run_rescanned_for_review")
        if problem:
            raise ValueError(problem)
        run.update(src_diff_sha256=digest, src_diff_evidence_id=ev_src)
        body = report_mod.render(run, findings, lambda ev: self.evidence_text(run_id, ev))
        data = body.encode("utf-8", "surrogatepass")
        if len(data) > srcdiff.REPORT_MAX_BYTES:
            raise ValueError("the regenerated report does not fit one evidence file")
        ev_rep = self.evidence_put(run_id, "report", data)
        report_sha = sha_text(body)
        with self.lock:
            op = Op(self, f"rescan|{run_id}|{digest}", ENGINE, run_id)
            eid = op.record(derived_id("rsc", run_id, digest, report_sha), "run_rescanned_for_review", ENGINE, run_id,
                            {"run_id": run_id, "src_diff_sha256": digest, "src_diff_evidence_id": ev_src,
                             "report_sha256": report_sha, "report_evidence_id": ev_rep, "head_sha": head,
                             "paths": paths[:200], "flags": {f["finding_id"]: len(f["review_flags"]) for f in findings},
                             "legacy_report_evidence_id": run.get("report_evidence_id")},
                            f"Legacy run re-scanned for review ({run_id})")
            run.update(report_sha256=report_sha, report_evidence_id=ev_rep)
            run["ledger_event_ids"] = list(run.get("ledger_event_ids") or []) + [eid]
            op.add("run", run)
            for rec in findings:
                op.add("finding", rec)
            self._commit(op)

    # ================================================================== worker

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                run_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._engine.execute(run_id)
            except Exception:  # noqa: BLE001 - the engine has its own boundary; nothing escapes to the thread
                self.mark_failed_unrecorded(run_id, R.item("HARNESS_ERROR", "engine boundary"))
            finally:
                self._queue.task_done()

    def wait_idle(self, timeout: float = 600.0) -> None:
        """Tests and the live runner: block until no run is live AND the engine thread has settled — every queued run's
        ``FixEngine.execute`` has RETURNED (``task_done`` is called after it, so ``unfinished_tasks`` counts a run
        that is still inside the engine). Wave 23 (B3, N22-D-6): a run's status turns terminal (a cancel) while the
        engine thread may still be inside ``docker run`` and about to record what it does with that container; the
        status alone is not the settled signal."""
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                live = [r for r in self.runs.values() if r["status"] in LIVE_RUN_STATES]
            if not live and self._queue.unfinished_tasks == 0:
                return
            time.sleep(0.05)
        raise TimeoutError("engine did not go idle")

    def stop(self) -> None:
        self._stop.set()
        self._reap("stop")

    def _reap(self, where: str) -> None:
        """R6: remove every container/volume carrying our run label (a crash, a failed destroy); each is recorded
        ``sandbox_reaped`` first. Never by name pattern — by the exact label the engine set."""
        if self.reconcile_mode or self.docker is None:
            return
        from zbm_delivery.adapters.sandbox import reap
        try:
            reap(self.docker, self._record_plain, where=where)
        except Exception:  # noqa: BLE001 - the reaper is best effort; a daemon that is gone has nothing to reap
            pass

    # ================================================================== health / policy

    def sandbox_available(self) -> bool:
        try:
            return bool(self._docker_available())
        except Exception:  # noqa: BLE001
            return False

    def llm_state(self) -> str:
        if isinstance(self.chat_backend, NoChatBackend) or self.chat_backend is None:
            return "unconfigured"
        return "fake" if getattr(self.chat_backend, "fake", False) else "configured"

    def health(self) -> dict:
        ledger = "unconfigured" if type(self.recorder.client).__name__ == "UnconfiguredLedgerClient" else "configured"
        return {"status": "ok", "service": "delivery-py", "in_memory": self.log.in_memory, "ledger": ledger,
                "sandbox": "available" if self.sandbox_available() else "unavailable", "llm": self.llm_state(),
                "non_production": self.cfg.non_production, "config_sha256": self.gate.config_sha256,
                "prompts_manifest_sha256": self.gate.prompts_manifest_sha256, "policy_version": POLICY_VERSION,
                "deerflow_commit": self.gate.deerflow_commit or self.cfg.deerflow_commit,
                "reconcile_mode": self.reconcile_mode, "reconcile_required": bool(self.reconcile_required),
                "runs_live": sum(1 for r in self.runs.values() if r["status"] in states.LIVE or r["status"] in ("received", "preparing", "reporting"))}

    def policy_view(self) -> dict:
        classes = {name: {"decision": c.get("decision"), "tools": c.get("tools", [])} for name, c in self.policy_seed["classes"].items()}
        return {"policy_version": POLICY_VERSION, "classes": classes,
                "test_commands": {n: {"test": f["test"], "suite": f["suite"]} for n, f in self.test_seed["frameworks"].items()},
                "prompts_manifest_sha256": self.gate.prompts_manifest_sha256, "policy_seed_sha256": self.gate.seeds.get("tool_policy_seed"),
                "test_commands_sha256": self.gate.seeds.get("test_commands_seed")}

    def _stamp(self, resp: dict) -> dict:
        resp.setdefault("policy_version", POLICY_VERSION)
        resp.setdefault("prompts_manifest_sha256", self.gate.prompts_manifest_sha256)
        return resp

    # ================================================================== ingest (§C.8.1)

    def _refuse(self, principal: str, request_id: str, facts: str, code: str, message: str) -> None:
        eid = derived_id("ref", principal, request_id, facts, code)
        try:
            self._record_plain(eid, "fix_run_refused", GATE, request_id, {"request_id": request_id, "facts_sha256": facts,
                                                                          "code": code, "principal": principal},
                               f"Fix run refused: {code}")
        except Unavailable:
            pass                              # a refusal stands whether or not it was recorded
        raise Refused(message, [R.item(code, message)], request_id=request_id, facts_sha256=facts, took_effect=False)

    def _refuse_unrunnable(self, principal: str, request_id: str, facts: str, finding_id: str, node: Optional[str],
                           why: str) -> None:
        """Wave 21 (R1, N20-D-3): a finding whose ``reproduction`` names no test the service's toolchain can run on
        the base tree is refused at ingestion — 422 ``reproduction_not_runnable``, nothing created. The refusal is
        recorded when the ledger answers; it stands either way."""
        code = "REPRODUCTION_NOT_RUNNABLE"
        message = f"finding {finding_id}: reproduction not runnable: {why}"
        eid = derived_id("ref", principal, request_id, facts, code, finding_id)
        try:
            self._record_plain(eid, "fix_run_refused", GATE, request_id, {"request_id": request_id, "facts_sha256": facts,
                                                                          "code": code, "principal": principal,
                                                                          "finding_id": finding_id, "target": node},
                               f"Fix run refused: {code} ({finding_id})")
        except Unavailable:
            pass
        raise Invalid(message, code="reproduction_not_runnable", finding_id=finding_id, target=node,
                      reasons=[R.item(code, message)], request_id=request_id, facts_sha256=facts, took_effect=False)

    def _check_reproductions(self, caller: str, request_id: str, facts: str, base_sha: str, service: str,
                             findings: list[dict]) -> None:
        """Every finding's reproduction resolves to a test of the service's toolchain AT THE BASE COMMIT (R1): the
        engine runs it in the verification checkout (must pass) and the reverted one (must fail) before
        ``candidate_passed_checks``,
        and on the untouched base tree for a ``DISPROOF``. There is no prose-reproduction route."""
        prefix = f"services/{service}/"

        def exists(rel: str) -> bool:
            return self.git.blob_exists(base_sha, prefix + rel, run_id="-")

        def read(rel: str) -> Optional[str]:
            return self.git.show_file(base_sha, prefix + rel, self.git.repo, run_id="-")

        for f in findings:
            rt = f.get("reproduction_test")
            if rt:
                # L2: a reviewer-authored RED test is a NEW file of the service directory, overlaid on the base;
                # the finding's reproduction must name it, and it must pass the same content rules as any test
                node, why = self._reviewer_test_problem(f, rt, exists, read)
            else:
                node, why = reproduction_problem(self.test_seed, f.get("reproduction") or "", exists, read)
            if why is not None:
                self._refuse_unrunnable(caller, request_id, facts, f["id"], node, why)

    def _reviewer_test_problem(self, f: dict, rt: dict, exists, read) -> tuple[Optional[str], Optional[str]]:
        """Wave 21 (L2): ``(node, why-not)`` for a finding carrying ``reproduction_test``."""
        path, content = rt["path"], rt["content"]
        if exists(path):
            return None, f"reproduction_test.path {path} is already a file of the base commit (it must be a new test file)"
        node, why = reproduction_problem(self.test_seed, f.get("reproduction") or "",
                                         lambda rel: rel == path or exists(rel),
                                         lambda rel: content if rel == path else read(rel))
        if why is not None:
            return node, why
        if node.partition("::")[0] != path:
            return node, f"the reproduction names {node}, not a test of reproduction_test.path {path}"
        fw_name = detect_framework(self.test_seed, exists)
        fw = self.test_seed["frameworks"][fw_name]
        for rule in fw.get("test_content_deny", []):
            if re.search(rule["pattern"], content):
                return node, f"reproduction_test matches the test content rule {rule['name']}"
        return node, None

    def reviewer_tests(self, run_id: str) -> list[dict]:
        """L2: the run's reviewer-authored tests, ``{finding_id, path, content, sha256}`` (service-relative path)."""
        with self.lock:
            out = []
            for fid, doc in sorted(self.finding_docs.get(run_id, {}).items()):
                rt = doc.get("reproduction_test")
                if rt:
                    out.append({"finding_id": fid, "path": rt["path"], "content": rt["content"],
                                "sha256": hashlib.sha256(rt["content"].encode("utf-8", "surrogatepass")).hexdigest()})
            return out

    def _refuse_red(self, caller: str, request_id: str, facts: str, code: str, finding_id: str, node: Optional[str],
                    message: str, extra: dict) -> None:
        """422 for a reviewer-authored reproduction that is not verified RED; the refusal is recorded when the ledger
        answers and stands either way (nothing was created)."""
        eid = derived_id("ref", caller, request_id, facts, code, finding_id)
        try:
            self._record_plain(eid, "fix_run_refused", GATE, request_id,
                               {"request_id": request_id, "facts_sha256": facts, "code": code, "principal": caller,
                                "finding_id": finding_id, "target": node, **extra},
                               f"Fix run refused: {code} ({finding_id})")
        except Unavailable:
            pass
        raise Invalid(message, code=code.lower(), finding_id=finding_id, target=node, reasons=[R.item(code, message)],
                      request_id=request_id, facts_sha256=facts, took_effect=False)

    # --- admission (B2, wave 23): the reviewer-test RED containers run OUTSIDE the service lock -------------------------

    def _in_flight(self, service: str) -> Optional[str]:
        """The run — or pending admission — that holds ``service``'s run slot, or None. The caller holds the lock."""
        for r in self.runs.values():
            if r["service"] == service and r["status"] in LIVE_RUN_STATES:
                return r["run_id"]
        for adm, a in self._admissions.items():
            if a["service"] == service:
                return adm
        return None

    def _refuse_busy(self, service: str, request_id: str, facts: str) -> None:
        holder = self._in_flight(service)
        if holder is not None:
            raise Conflict("a run for this service is in progress", reasons=[R.item("RUN_IN_PROGRESS", holder)],
                           request_id=request_id, facts_sha256=facts)

    def _red_begin(self, caller: str, request_id: str, facts: str, base_sha: str, service: str, principal,
                   findings: list[dict], *, review_of: Optional[str] = None) -> Optional[dict]:
        """Phase 1 of the admission RED check (the caller holds the lock and has refused a busy service). No
        reviewer-authored reproduction → None (nothing to run). Otherwise the pending admission is RECORDED
        (``reproduction_red_check_started``: record-first — an unrecorded reservation does not exist, ``Unavailable``)
        and reserves the service's run slot until ``_red_finish`` admits or refuses; the lock is then released by the
        caller while the containers run. No engine or no sandbox → 422 ``reproduction_red_unverified`` now."""
        items = [f for f in findings if f.get("reproduction_test")]
        if not items:
            return None
        adm = rid("run", "admission", caller, request_id, facts)     # its own id: never the run's event ids
        if adm in self._admissions:
            raise Conflict("this admission is already in progress", reasons=[R.item("RUN_IN_PROGRESS", adm)],
                           request_id=request_id, facts_sha256=facts)
        if self._engine is None or not hasattr(self._engine, "reproduction_red") or not self.sandbox_available():
            f = items[0]
            node = node_id_in_text(f.get("reproduction") or "")
            self._refuse_red(caller, request_id, facts, "REPRODUCTION_RED_UNVERIFIED", f["id"], node,
                             f"finding {f['id']}: the reviewer-authored reproduction {node} could not be run RED on the "
                             "base commit (no engine or no sandbox): refused, never admitted unchecked", {"why": "no_sandbox"})
        self._record_plain(derived_id("rcs", adm, base_sha), "reproduction_red_check_started", GATE, request_id,
                           {"request_id": request_id, "facts_sha256": facts, "admission_id": adm, "service": service,
                            "base_sha": base_sha, "finding_ids": [f["id"] for f in items], "review_of": review_of},
                           f"Admission RED check started for {service}: {len(items)} reviewer test(s)")
        self._admissions[adm] = {"service": service, "request_id": request_id, "caller": caller, "review_of": review_of}
        return {"adm": adm, "items": items, "caller": caller, "request_id": request_id, "facts": facts,
                "base_sha": base_sha, "service": service, "principal": principal}

    def _red_run(self, pending: dict) -> list[tuple]:
        """Phase 2 — NO service lock held: run each reviewer-authored reproduction on the base in a fresh engine
        container. Returns ``[(finding, node, TestRun|None, exception|None)]``; stops at the first result that can only
        refuse the admission (an exception or a verdict other than ``fail``): no container runs for nothing."""
        if self.lock._is_owned():
            raise RuntimeError("B2: the admission RED containers never run under the service lock")
        out: list[tuple] = []
        adm = pending["adm"]
        for f in pending["items"]:
            node = node_id_in_text(f.get("reproduction") or "")
            try:
                t = self._engine.reproduction_red(run_id=adm, service=pending["service"], base_sha=pending["base_sha"],
                                                  principal_user_id=pending["principal"].user_id(adm), finding_id=f["id"],
                                                  path=f["reproduction_test"]["path"],
                                                  content=f["reproduction_test"]["content"], node=node)
            except Exception as exc:  # noqa: BLE001 - Unavailable included: the RED check did not complete → refused
                out.append((f, node, None, exc))
                break
            out.append((f, node, t, None))
            if t.verdict != "fail":
                break
        return out

    def _red_record_locked(self, pending: dict, results: list[tuple]) -> dict[str, str]:
        """Phase 3 (the caller holds the lock): record each verdict ``reproduction_red_checked`` and return its event
        id per finding, or refuse. A passing test → 422 ``reproduction_not_red``; an exception, an ``unknown`` verdict
        or a verdict that could not be recorded → 422 ``reproduction_red_unverified``; a finding with no result (a
        container loop stopped early) cannot reach here — an earlier result already refused."""
        caller, request_id, facts, adm = pending["caller"], pending["request_id"], pending["facts"], pending["adm"]
        code = "REPRODUCTION_RED_UNVERIFIED"
        out: dict[str, str] = {}
        for f, node, t, exc in results:
            if exc is not None:
                self._refuse_red(caller, request_id, facts, code, f["id"], node,
                                 f"finding {f['id']}: the RED check of the reviewer-authored reproduction {node} could not "
                                 f"complete ({type(exc).__name__}): refused, never admitted unchecked",
                                 {"why": type(exc).__name__[:60], "admission_id": adm})
            if t.verdict == "pass":
                self._refuse_red(caller, request_id, facts, "REPRODUCTION_NOT_RED", f["id"], node,
                                 f"finding {f['id']}: the reviewer-authored reproduction {node} passes on the base commit",
                                 {"verdict": t.verdict, "exit": t.exit, "output_sha256": t.output_sha256, "admission_id": adm})
            if t.verdict != "fail":
                why = (t.counts.why if t.counts else "unknown")[:120]
                self._refuse_red(caller, request_id, facts, code, f["id"], node,
                                 f"finding {f['id']}: the reviewer-authored reproduction {node} could not be verified RED on "
                                 f"the base commit ({why}): refused, never admitted unchecked",
                                 {"verdict": t.verdict, "exit": t.exit, "output_sha256": t.output_sha256, "admission_id": adm})
            eid = derived_id("red", adm, f["id"], t.output_sha256)
            try:
                self._record_plain(eid, "reproduction_red_checked", GATE, request_id,
                                   {"request_id": request_id, "facts_sha256": facts, "admission_id": adm,
                                    "finding_id": f["id"], "target": node, "base_sha": pending["base_sha"],
                                    "verdict": t.verdict, "exit": t.exit, "output_sha256": t.output_sha256,
                                    "test_sha256": sha_text(f["reproduction_test"]["content"])},
                                   f"Reviewer reproduction RED on base for {f['id']}")
            except Unavailable:
                self._refuse_red(caller, request_id, facts, code, f["id"], node,
                                 f"finding {f['id']}: the RED verdict of the reviewer-authored reproduction {node} could not "
                                 "be recorded: refused, never admitted unchecked", {"why": "record_failed", "admission_id": adm})
            out[f["id"]] = eid
        return out

    def _red_finish(self, pending: dict, admit) -> dict:
        """Phases 2-3: the containers without the lock, then ONE hold of the lock that releases the reservation,
        records the verdicts and admits (``admit(red_checks)``) or refuses. The reservation is released whatever
        happens (a refusal, an exception)."""
        try:
            results = self._red_run(pending)
            with self.lock:
                self._admissions.pop(pending["adm"], None)
                if pending["adm"] in self._cancelled_admissions:
                    # wave 24 (E6): cancelled by the operator while the containers ran — nothing is recorded or
                    # admitted from them (the cancel was recorded first and freed the slot)
                    self._cancelled_admissions.discard(pending["adm"])
                    raise Conflict("this admission was cancelled while its RED check ran: nothing was admitted",
                                   reasons=[R.item("CANCELLED", pending["adm"])], request_id=pending["request_id"],
                                   facts_sha256=pending["facts"], admission_id=pending["adm"])
                red = self._red_record_locked(pending, results)
                return admit(red)
        finally:
            with self.lock:
                self._admissions.pop(pending["adm"], None)

    def create_fix_run(self, caller: str, body: dict) -> dict:
        """Admission (§C.8.1). The static checks run under the service lock; a document with reviewer-authored
        reproductions reserves the service's run slot (recorded), runs their RED check with the lock RELEASED, and
        is admitted or refused in one later hold (B2, wave 23)."""
        from zbm_delivery.adapters.identity import PrincipalMissing, principal_for
        request_id = body["request_id"]
        facts = facts_sha256(body)
        try:
            principal = principal_for(caller)
        except PrincipalMissing:
            self._refuse(caller, request_id, facts, "PRINCIPAL_MISSING", "the caller maps to no principal")
        with self.lock:
            key, h, ent = self._idem(caller, request_id, "fix-runs", body)
            if ent is not None:
                return ent["response"]
            if self._engine is None:
                self._refuse(caller, request_id, facts, "HARNESS_ERROR", "the engine is not wired in this process")
            if not self.sandbox_available():
                self._refuse(caller, request_id, facts, "SANDBOX_UNAVAILABLE", "docker daemon not reachable")
            if self.llm_state() == "unconfigured":
                self._refuse(caller, request_id, facts, "LLM_NOT_CONFIGURED", getattr(self.chat_backend, "why", "no model"))
            service = body["service"]
            self._refuse_busy(service, request_id, facts)
            try:
                base_sha = self.git.rev_parse(body["base_sha"], run_id="-")
            except Unavailable:
                raise                                    # the git crossing could not be recorded: 503
            except Exception:  # noqa: BLE001
                self._refuse(caller, request_id, facts, "GIT_REFUSED", "base_sha is not a commit of the repository")
            if body["base_ref"] != self.cfg.base_ref:
                self._refuse(caller, request_id, facts, "GIT_REFUSED", "base_ref is not DLV_BASE_REF")
            for f in body["findings"]:
                if not self.git.blob_exists(base_sha, f["file"], run_id="-"):
                    self._refuse(caller, request_id, facts, "GIT_REFUSED", f"finding {f['id']}: file is not in the base commit")
            try:
                self._check_reproductions(caller, request_id, facts, base_sha, service, body["findings"])
            except GitRefused:
                self._refuse(caller, request_id, facts, "GIT_REFUSED", "the base commit could not be read")
            pending = self._red_begin(caller, request_id, facts, base_sha, service, principal, body["findings"])
            if pending is None:
                resp, run_id, _ = self._admit_locked(caller, body, base_sha, principal, {}, key, h)
                self._queue.put(run_id)
                return resp

        def admit(red: dict) -> dict:
            resp, run_id, _ = self._admit_locked(caller, body, base_sha, principal, red, key, h,
                                                 admission_id=pending["adm"])
            self._queue.put(run_id)
            return resp
        return self._red_finish(pending, admit)

    def _admit_locked(self, caller: str, body: dict, base_sha: str, principal, red_checks: dict, key: tuple, h: str, *,
                      parent_run_id: Optional[str] = None, op: Optional[Op] = None,
                      admission_id: Optional[str] = None) -> tuple[dict, str, Op]:
        """Create the run (the caller holds the lock and has made every check). With ``op`` (a review's) the run's
        records join that operation — one local-log line with the review — and the caller commits and queues;
        otherwise the operation is committed here and the caller queues the run."""
        request_id = body["request_id"]
        facts = facts_sha256(body)
        service = body["service"]
        red_checks = dict(red_checks or {})
        missing = [f["id"] for f in body["findings"] if f.get("reproduction_test") and not red_checks.get(f["id"])]
        if missing:
            self._refuse_red(caller, request_id, facts, "REPRODUCTION_RED_UNVERIFIED", missing[0], None,
                             f"finding {missing[0]}: no admission RED check for its reviewer-authored reproduction",
                             {"why": "no_red_check"})
        run_id = rid("run", "run", caller, request_id, facts)
        if run_id in self.runs:
            raise Conflict("run already exists")
        injection = textguard.injection_rules_in({k: v for k, v in body.items() if k != "request_id"})
        now = self.now()
        run = {"run_id": run_id, "request_id": request_id, "facts_sha256": facts, "caller": caller,
               "principal": {"kind": principal.kind, "id": principal.id, "tenant": principal.tenant},
               "principal_user_id": principal.user_id(run_id), "thread_id": f"dlv-{run_id[-26:]}",
               "service": service, "base_ref": body["base_ref"], "base_sha": base_sha, "branch": None,
               "worktree_path": None, "status": "received", "finding_ids": [f["id"] for f in body["findings"]],
               "suite": {"before": None, "after": None}, "commits": [], "report_sha256": None,
               "report_evidence_id": None, "review": None, "deadline_at": iso(now + timedelta(seconds=self.cfg.run_wall_clock_s)),
               "llm": {"provider": getattr(self.chat_backend, "provider", "unconfigured"),
                       "model": getattr(self.chat_backend, "model", ""), "fake": bool(getattr(self.chat_backend, "fake", False))},
               "sandbox": {"image_digest": None, "container_id_sha256": None}, "policy_version": POLICY_VERSION,
               "prompts_manifest_sha256": self.gate.prompts_manifest_sha256, "created_at": iso(now), "finished_at": None,
               "ledger_event_ids": [], "reasons": [], "parent_run_id": parent_run_id, "new_defects": [],
               "source": body["source"], "evidence": [], "injection_rules": injection}
        if parent_run_id is not None:
            parent = self.runs[parent_run_id]
            run["branch"], run["worktree_path"] = parent["branch"], parent["worktree_path"]
        own = op is None
        if own:
            op = Op(self, f"fix-run|{run_id}", caller, run_id)
        eid = op.record(derived_id("rcv", run_id), "fix_run_received", caller, run_id,
                        {"run_id": run_id, "request_id": request_id, "facts_sha256": facts, "service": service,
                         "base_sha": base_sha, "finding_ids": run["finding_ids"], "parent_run_id": parent_run_id,
                         "source_sha256": body["source"]["sha256"], "admission_id": admission_id,
                         "reviewer_tests": [{"finding_id": f["id"], "path": f["reproduction_test"]["path"],
                                             "sha256": sha_text(f["reproduction_test"]["content"]), "author": "reviewer"}
                                            for f in body["findings"] if f.get("reproduction_test")]},
                        f"Fix run received for {service}: {len(body['findings'])} finding(s)")
        run["ledger_event_ids"].append(eid)
        if injection:
            iid = op.record(derived_id("inj", run_id), "injection_text_ignored", GATE, run_id,
                            {"run_id": run_id, "rules": injection, "count": len(injection)},
                            f"Injection text in the findings document ignored ({len(injection)} rule(s))")
            run["ledger_event_ids"].append(iid)
        op.add("run", run)
        for f in body["findings"]:
            op.add("finding_doc", {"run_id": run_id, "finding": f})
            op.add("finding", {"run_id": run_id, "finding_id": f["id"], "severity": f["severity"],
                               "title_sha256": sha_text(f["title"]), "file": f["file"], "line": f["line"],
                               "class_hint": f.get("class_hint"), "state": "queued", "rounds": 0, "red": None,
                               "green": None, "revert_check": None, "sweep": None, "disproof": None,
                               "changed_tests": [], "commit_sha": None, "commit_files": [], "agent": None,
                               "reasons": [], "suite_failures": [], "brief_evidence_id": None, "review_flags": [],
                               "injection_rules": textguard.injection_rules_in(f),
                               "reviewer_test": ({"path": f["reproduction_test"]["path"], "author": "reviewer",
                                                  "sha256": sha_text(f["reproduction_test"]["content"]),
                                                  "red_checked_event_id": red_checks[f["id"]]}
                                                 if f.get("reproduction_test") else None)})
        resp = self._stamp({"run_id": run_id, "status": "received", "request_id": request_id, "facts_sha256": facts,
                            "ledger_event_id": eid})
        self._idem_add(op, key, h, resp)
        if own:
            self._commit(op)
        return resp, run_id, op

    # ================================================================== views (§D)

    def run_view(self, run_id: str) -> dict:
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:
                raise NotFound("no such run")
            return self._stamp(json.loads(json.dumps(run)))

    def findings_view(self, run_id: str) -> dict:
        with self.lock:
            if run_id not in self.runs:
                raise NotFound("no such run")
            return self._stamp({"run_id": run_id, "findings": json.loads(json.dumps(list(self.findings.get(run_id, {}).values())))})

    def report_text(self, run_id: str) -> str:
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:
                raise NotFound("no such run")
            ev = run.get("report_evidence_id")
            if not ev:
                raise NotFound("no report for this run yet")
            if not run.get("src_diff_sha256"):
                # wave 24 (E5): a report written before the source diff was embedded (the legacy "fixed" wording, no
                # spelling-list warning) is never served; an awaiting_review run is re-scanned at load instead
                raise Conflict("this run's report predates the embedded source diff and is not served",
                               reasons=[R.item("REVIEW_STATE", "legacy report")])
            return self.evidence_read(run_id, ev).decode("utf-8", "replace")

    def evidence_meta(self, run_id: str, evidence_id: str) -> dict:
        with self.lock:
            if run_id not in self.runs:
                raise NotFound("no such run")
            meta = self.evidence_index.get(run_id, {}).get(evidence_id)
            if meta is None:
                raise NotFound("no such evidence")
            return meta

    def evidence_read(self, run_id: str, evidence_id: str) -> bytes:
        meta = self.evidence_meta(run_id, evidence_id)
        if not self.evidence_root:
            data = self._evidence_mem.get(evidence_id)
            if data is None:
                raise Unavailable("evidence not available in memory (restart)")
        else:
            path = os.path.join(self.evidence_root, run_id, evidence_id)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                raise Unavailable("evidence file unreadable") from None
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise Unavailable("evidence file does not match its recorded hash (tampered)")
        return data

    def evidence_text(self, run_id: str, evidence_id: str) -> str:
        try:
            return self.evidence_read(run_id, evidence_id).decode("utf-8", "replace")
        except (NotFound, Unavailable):
            return ""

    # ================================================================== engine-facing (record-first)

    def run_get(self, run_id: str) -> dict:
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:
                raise NotFound("no such run")
            return json.loads(json.dumps(run))

    def run_status(self, run_id: str) -> str:
        with self.lock:
            run = self.runs.get(run_id)
            return run["status"] if run else "failed"

    def findings_get(self, run_id: str) -> list[dict]:
        with self.lock:
            return json.loads(json.dumps(list(self.findings.get(run_id, {}).values())))

    def findings_get_one(self, run_id: str, finding_id: str) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.findings[run_id][finding_id]))

    def finding_document(self, run_id: str, finding_id: str) -> dict:
        with self.lock:
            return dict(self.finding_docs[run_id][finding_id])

    def injection_hits(self, run_id: str, finding_id: str) -> list[str]:
        with self.lock:
            return list(self.findings[run_id][finding_id].get("injection_rules") or [])

    def attributable_failures(self, run_id: str, finding_id: str, baseline_failures: list[str]) -> set[str]:
        """Baseline failures attributable to ANOTHER finding of this run not yet at an engine end state (file match,
        or the node id named in that finding's reproduction) may remain while this finding is worked on (§C.8.4
        step 5)."""
        out: set[str] = set()
        with self.lock:
            docs = self.finding_docs.get(run_id, {})
            recs = self.findings.get(run_id, {})
            service = self.runs[run_id]["service"]
            for fid, doc in docs.items():
                if fid == finding_id or recs.get(fid, {}).get("state") in DONE_STATES:
                    continue
                rel = doc["file"][len(f"services/{service}/"):]
                repro = doc.get("reproduction", "")
                repro_target = node_id_in_text(repro)
                for name in baseline_failures:
                    if name.split("::", 1)[0] == rel or name in repro or (repro_target and same_test(name, repro_target)):
                        out.add(name)
        return out

    def _require_live(self, run: dict, event_type: str) -> None:
        """R5: a run that is failed / reviewed / awaiting review accepts no further engine record (the post-mortem
        events excepted); an in-memory unrecorded failure counts as not live too."""
        if event_type in POST_MORTEM_EVENTS:
            return
        if run.get("status") in states.TERMINAL or run.get("status") == "awaiting_review" or run.get("unrecorded_failure"):
            raise Conflict(f"the run is not live ({run.get('status')}); {event_type} refused")

    def protected_reproductions(self, run_id: str) -> list[str]:
        """R3: the reproduction node ids of every OPEN finding of the run (a CHANGED_TEST touching one is denied)."""
        with self.lock:
            docs = self.finding_docs.get(run_id, {})
            recs = self.findings.get(run_id, {})
            out = []
            for fid, doc in docs.items():
                if recs.get(fid, {}).get("state") in DONE_STATES + states.REVIEW_STATES + ("reviewed",):
                    continue
                t = node_id_in_text(doc.get("reproduction") or "")
                if t:
                    out.append(t)
            return sorted(set(out))

    def run_transition(self, run_id: str, to: str, event_type: str, payload: dict, summary: str,
                       fields: Optional[dict] = None) -> str:
        with self.lock:
            run = self.runs[run_id]
            problem = states.run_transition_problem(run["status"], to)
            if problem:
                raise Conflict(problem)
            new = {**json.loads(json.dumps(run)), **(fields or {}), "status": to}
            op = Op(self, f"run|{run_id}|{event_type}|{len(new['ledger_event_ids'])}", ENGINE, run_id)
            eid = op.record(derived_id("run", run_id, event_type, to, len(run["ledger_event_ids"])), event_type, ENGINE, run_id,
                            {**payload, "from": run["status"], "to": to}, summary)
            new["ledger_event_ids"] = list(run["ledger_event_ids"]) + [eid]
            if to in states.TERMINAL and not new.get("finished_at"):
                new["finished_at"] = self.now_iso()
            op.add("run", new)
            self._commit(op)
            return eid

    def run_update(self, run_id: str, event_type: str, payload: dict, summary: str, fields: Optional[dict] = None) -> str:
        with self.lock:
            run = self.runs[run_id]
            self._require_live(run, event_type)
            new = {**json.loads(json.dumps(run)), **(fields or {})}
            op = Op(self, f"run|{run_id}|{event_type}|{len(run['ledger_event_ids'])}", ENGINE, run_id)
            eid = op.record(derived_id("run", run_id, event_type, len(run["ledger_event_ids"]), sha(payload)), event_type,
                            ENGINE, run_id, payload, summary)
            new["ledger_event_ids"] = list(run["ledger_event_ids"]) + [eid]
            op.add("run", new)
            self._commit(op)
            return eid

    def try_run_update(self, run_id: str, event_type: str, payload: dict, summary: str) -> Optional[str]:
        try:
            return self.run_update(run_id, event_type, payload, summary)
        except (Unavailable, KeyError, Conflict):
            return None

    def finding_update(self, run_id: str, finding_id: str, event_type: str, payload: dict, summary: str,
                       fields: Optional[dict] = None, run_fields: Optional[dict] = None) -> str:
        with self.lock:
            run = self.runs[run_id]
            self._require_live(run, event_type)
            rec = self.findings[run_id][finding_id]
            new = {**json.loads(json.dumps(rec)), **(fields or {})}
            op = Op(self, f"finding|{run_id}|{finding_id}|{event_type}|{len(run['ledger_event_ids'])}", ENGINE, run_id)
            eid = op.record(derived_id("fnd", run_id, finding_id, event_type, len(run["ledger_event_ids"]), sha(payload)),
                            event_type, ENGINE, f"{run_id}:{finding_id}", payload, summary)
            new_run = {**json.loads(json.dumps(run)), **(run_fields or {})}
            new_run["ledger_event_ids"] = list(run["ledger_event_ids"]) + [eid]
            op.add("finding", new)
            op.add("run", new_run)
            self._commit(op)
            return eid

    def finding_transition(self, run_id: str, finding_id: str, to: str, fields: dict, evidence_ids: list[str], *,
                           red_test_name: Optional[str] = None) -> bool:
        with self.lock:
            run = self.runs[run_id]
            self._require_live(run, "finding_state_changed")
            rec = self.findings[run_id][finding_id]
            candidate = {**json.loads(json.dumps(rec)), **fields}
            problem = states.finding_transition_problem(candidate, to, red_test_name=red_test_name)
            n = len(run["ledger_event_ids"])
            if problem:
                op = Op(self, f"finding|{run_id}|{finding_id}|refused|{n}", ENGINE, run_id)
                eid = op.record(derived_id("fnr", run_id, finding_id, rec["state"], to, n), "finding_transition_refused", ENGINE,
                                f"{run_id}:{finding_id}", {"run_id": run_id, "finding_id": finding_id, "from": rec["state"], "to": to,
                                                           "problem": problem[:160]},
                                f"Finding {finding_id}: {rec['state']} -> {to} refused")
                new_run = json.loads(json.dumps(run))
                new_run["ledger_event_ids"].append(eid)
                op.add("run", new_run)
                self._commit(op)
                return False
            candidate["state"] = to
            op = Op(self, f"finding|{run_id}|{finding_id}|{to}|{n}", ENGINE, run_id)
            eid = op.record(derived_id("fns", run_id, finding_id, rec["state"], to, n), "finding_state_changed", ENGINE,
                            f"{run_id}:{finding_id}", {"run_id": run_id, "finding_id": finding_id, "from": rec["state"], "to": to,
                                                       "evidence_ids": sorted(set(evidence_ids))[:50]},
                            f"Finding {finding_id}: {rec['state']} -> {to}")
            new_run = json.loads(json.dumps(run))
            new_run["ledger_event_ids"].append(eid)
            op.add("finding", candidate)
            op.add("run", new_run)
            self._commit(op)
            return True

    def evidence_put(self, run_id: str, kind: str, content: bytes) -> str:
        if kind not in EVIDENCE_KINDS:
            raise Invalid("unknown evidence kind")
        if len(content) > EVIDENCE_MAX_BYTES:
            content = content[:EVIDENCE_MAX_BYTES] + b"\n[evidence truncated at 8 MiB]\n"
        if textguard.secret_shapes(content.decode("utf-8", "replace")):
            content = textguard.redact(content.decode("utf-8", "replace")).encode("utf-8")
        ev_id = evidence_id_for(content)
        with self.lock:
            run = self.runs[run_id]
            if ev_id in self.evidence_index.get(run_id, {}):
                return ev_id
            meta = {"run_id": run_id, "evidence_id": ev_id, "kind": kind, "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content), "at": self.now_iso()}
            op = Op(self, f"evidence|{run_id}|{ev_id}", EVIDENCE, run_id)
            op.add("evidence", meta)
            new_run = json.loads(json.dumps(run))
            new_run["evidence"] = list(run.get("evidence") or []) + [{"evidence_id": ev_id, "kind": kind, "sha256": meta["sha256"], "bytes": len(content)}]
            op.add("run", new_run)
            op.files.append((run_id, ev_id, content))
            self._commit(op)
            return ev_id

    def run_finished(self, run_id: str) -> None:
        return None

    def mark_failed_unrecorded(self, run_id: str, reason: dict) -> None:
        """The ledger is down: the in-memory run is failed so the loop stops; the state is NOT persisted (a
        restart replays the last recorded state and fails it then). Wave 23 (B4, N22-D-7): a run that is ALREADY
        terminal (cancelled, failed) is marked too — its status stays, ``unrecorded_failure`` and the reason say that
        something happened on it that the ledger does not hold (e.g. a kill that could not be recorded first)."""
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:
                return
            if run["status"] not in states.TERMINAL:
                run["status"] = "failed"
            run["reasons"] = list(run.get("reasons") or []) + [reason]
            run["unrecorded_failure"] = True

    def _on_ledger_failure(self, run_id: str, why: str) -> None:
        self.mark_failed_unrecorded(run_id, R.item("LEDGER_UNAVAILABLE", why[:160]))

    def _resolve_sandbox_path(self, run_id: str, path: str) -> Optional[str]:
        return self._resolve_sandbox_paths(run_id, [path])[0]

    def _resolve_sandbox_paths(self, run_id: str, paths: list[str]) -> list[Optional[str]]:
        """R8: every operand of one tool call resolved inside the run's sandbox in ONE exec."""
        b = registry.by_run(run_id)
        if b is None or b.container_name is None or self._engine is None:
            return [None] * len(paths)
        provider = self._engine.provider_factory()
        box = provider.get(b.container_name)
        if box is None:
            return [None] * len(paths)
        try:
            return box.realpath_many([p if p.startswith("/") else f"{b.workspace}/{p}" for p in paths])
        except Exception:  # noqa: BLE001
            return [None] * len(paths)

    # ================================================================== review (§C.8.7), cancel

    def _review_checks(self, caller: str, run_id: str, body: dict, request_id: str, facts: str) -> dict:
        """Everything a review is refused on before anything is recorded (the caller holds the lock): the run's state,
        a review of the run already in progress, the reopened / new finding ids, and (wave 23, D1/D2/D3) the
        per-finding verdicts, the runner-dependent notes and the flags each accepted finding must address; wave 24
        (E2): the src_diff_sha256 an accept must carry and a real note per flag."""
        run = self.runs.get(run_id)
        if run is None:
            raise NotFound("no such run")
        if run["status"] != "awaiting_review":
            raise Conflict("the run is not awaiting review", reasons=[R.item("REVIEW_STATE", run["status"])],
                           request_id=request_id, facts_sha256=facts)
        pending = [a for a, x in self._admissions.items() if x.get("review_of") == run_id]
        if pending:
            raise Conflict("a review of this run is in progress", reasons=[R.item("RUN_IN_PROGRESS", pending[0])],
                           request_id=request_id, facts_sha256=facts)
        known = set(run["finding_ids"])
        unknown = [x for x in body.get("reopened", []) if x not in known]
        if unknown:
            raise Invalid("reopened ids must be findings of this run: " + ", ".join(unknown[:10]))
        for f in body.get("new_findings", []):
            if not f["file"].startswith(f"services/{run['service']}/"):
                raise Invalid("new findings must be inside the run's service")
            if f["id"] in known:
                raise Invalid("a new finding id collides with a finding of this run")
        recs = self.findings.get(run_id, {})
        done = {fid for fid, r in recs.items() if r.get("state") in DONE_STATES}
        verdicts = {v["finding_id"]: v for v in body.get("finding_verdicts") or []}
        stray = sorted(fid for fid in verdicts if fid not in done)
        if stray:
            raise Invalid("finding_verdicts name findings that are not awaiting a verdict in this run: " + ", ".join(stray[:10]))
        if body["verdict"] == "pass":
            missing = sorted(done - set(verdicts))
            if missing:
                raise Invalid("a pass needs an explicit verdict for every finding of the run (D1); missing: "
                              + ", ".join(missing[:20]))
        accepted = sorted(fid for fid, v in verdicts.items() if v["verdict"] == "accept")
        if accepted:
            # wave 24 (E2, N23-D-1/-4): an accept binds the reviewer to the complete source diff the report embeds
            if any("review_flags" not in recs[fid] for fid in accepted) or not run.get("src_diff_sha256"):
                raise Conflict("this run's records predate the review flags / source diff and have not been "
                               "re-scanned: nothing can be accepted on them", reasons=[R.item("REVIEW_STATE", "legacy run")],
                               request_id=request_id, facts_sha256=facts)
            if body.get("src_diff_sha256") != run["src_diff_sha256"]:
                msg = ("an accepting review must carry the run's src_diff_sha256 — the sha256 of the complete source "
                       "diff embedded in the report — attesting that the reviewer read it (E2)")
                raise Invalid(msg, code="diff_not_attested", reasons=[R.item("REVIEW_STATE", "diff_not_attested")],
                              request_id=request_id, facts_sha256=facts)
        for fid in accepted:
            if recs[fid]["state"] == "needs_review_runner_dependent":
                problem = _note_problem((verdicts[fid].get("note") or "").strip())
                if problem:
                    raise Invalid(f"finding {fid} is runner-dependent: accepting it needs a review note saying what "
                                  f"the reviewer checked (D3) — {problem}")
        notes: list[tuple[str, str]] = []
        run_flags = {fl["id"]: fid for fid, r in recs.items() for fl in (r.get("review_flags") or [])}
        addressed = {x["flag_id"]: (x.get("note") or "").strip() for x in body.get("flags_addressed") or []}
        strange = sorted(x for x in addressed if x not in run_flags)
        if strange:
            raise Invalid("flags_addressed names flag ids this run does not have: " + ", ".join(strange[:20]))
        required = sorted(fl for fl, fid in run_flags.items() if fid in accepted)
        unaddressed = [fl for fl in required if fl not in addressed]
        if unaddressed:
            raise Invalid("accepting a finding with review flags needs every flag id in flags_addressed (D2); "
                          "not addressed: " + ", ".join(unaddressed[:40]))
        for fl in required:
            problem = _note_problem(addressed[fl])
            if problem:
                raise Invalid(f"flag {fl}: an accepting review needs a note on what the reviewer checked there (E2) — "
                              f"{problem}")
            notes.append((f"flag {fl}", addressed[fl]))
        seen: dict[str, str] = {}
        for where, note in notes:
            if note in seen:
                raise Invalid(f"the note on {where} is the same text as the note on {seen[note]}: each note says what "
                              "was checked at its own flag (E2)")
            seen[note] = where
        return run

    def review(self, caller: str, run_id: str, body: dict) -> dict:
        """§C.8.7, wave 23. A pass needs an explicit ``accept`` for every finding; a fail reopens what it lists (and
        accepts what it accepts). A fail while another run or admission of the service is in flight is refused 409
        BEFORE anything is recorded (B1); a fail with reviewer-authored reproductions runs their RED check with the
        lock released (B2, the service's run slot reserved); the review, the findings' new states and the child run
        are one operation (one local-log line) in one hold of the lock."""
        if caller != "aegis":
            # wave 24 (E6, N23-D-6): the route allows only aegis; the service checks it too, so no other path into
            # review() (a future route, an internal call) can accept findings
            raise Forbidden("only the aegis caller reviews a fix run")
        request_id = body["request_id"]
        facts = facts_sha256(body)
        with self.lock:
            key, h, ent = self._idem(caller, request_id, f"review/{run_id}", body)
            if ent is not None:
                return ent["response"]
            run = self._review_checks(caller, run_id, body, request_id, facts)
            if body["verdict"] == "pass":
                return self._record_review_locked(caller, run_id, body, request_id, facts, key, h, None, None)
            self._refuse_busy(run["service"], request_id, facts)                 # B1: before anything is recorded
            # wave 21 (R1): the child run's findings must each name a reproduction runnable on the child's base (this
            # run's head) — checked BEFORE the review is recorded, so a refused review changes nothing
            head0 = run["commits"][-1]["sha"] if run.get("commits") else run["base_sha"]
            docs0 = self.finding_docs[run_id]
            child_findings = ([dict(docs0[fid]) for fid in body.get("reopened", [])]
                              + [dict(f) for f in body.get("new_findings", [])])
            try:
                self._check_reproductions(caller, request_id, facts, head0, run["service"], child_findings)
            except GitRefused:
                raise Invalid("the run's head commit could not be read") from None
            from zbm_delivery.adapters.identity import PrincipalMissing, principal_for
            try:
                principal = principal_for(caller)
            except PrincipalMissing:
                raise Invalid("the caller maps to no principal") from None
            pending = self._red_begin(caller, request_id, facts, head0, run["service"], principal, child_findings,
                                      review_of=run_id)
            if pending is None:
                return self._record_review_locked(caller, run_id, body, request_id, facts, key, h, {}, principal)

        def admit(red: dict) -> dict:
            # the lock is held again; the containers ran without it — nothing else could admit a run of the service
            # (its slot was reserved) or review this run (a review of it was pending), but check the state again
            self._review_checks(caller, run_id, body, request_id, facts)
            return self._record_review_locked(caller, run_id, body, request_id, facts, key, h, red, principal)
        return self._red_finish(pending, admit)

    def _record_review_locked(self, caller: str, run_id: str, body: dict, request_id: str, facts: str, key: tuple,
                              h: str, child_red: Optional[dict], principal) -> dict:
        run = self.runs[run_id]
        to = "reviewed_pass" if body["verdict"] == "pass" else "reviewed_fail"
        verdicts = {v["finding_id"]: v for v in body.get("finding_verdicts") or []}
        reopened = list(body.get("reopened", []))
        accepted = sorted(fid for fid, v in verdicts.items() if v["verdict"] == "accept")
        review = {"request_id": request_id, "facts_sha256": facts, "verdict": body["verdict"],
                  "review_ref": body["review_ref"], "sha256": body["sha256"], "reopened_ids": reopened,
                  "new_finding_ids": [f["id"] for f in body.get("new_findings", [])], "received_at": self.now_iso(),
                  "caller": caller, "accepted_ids": accepted, "src_diff_sha256": body.get("src_diff_sha256"),
                  "flags_addressed": [{"flag_id": x["flag_id"], "note": x.get("note") or "",
                                       "note_sha256": sha_text(x.get("note") or "")}
                                      for x in sorted(body.get("flags_addressed") or [], key=lambda x: x["flag_id"])],
                  "finding_verdicts": [{"finding_id": fid, "verdict": v["verdict"], "note": v.get("note") or "",
                                        "note_sha256": sha_text(v.get("note") or "")} for fid, v in sorted(verdicts.items())]}
        new = json.loads(json.dumps(run))
        new.update(status=to, review=review, finished_at=self.now_iso())
        op = Op(self, f"review|{run_id}|{request_id}", caller, run_id)
        child_id = None
        child = None
        # everything that could refuse is decided BEFORE the first record: the findings' review transitions and the
        # child run's admission (its RED checks, its id, its request id)
        updates = []
        for fid, rec0 in self.findings[run_id].items():
            target = "accepted" if fid in accepted else "reopened" if fid in reopened else None
            if target is None:
                continue
            problem = states.review_transition_problem(rec0, target)
            if problem:
                raise Invalid(f"finding {fid}: {problem}")
            updates.append((fid, rec0, target))
        if body["verdict"] == "fail":
            docs = self.finding_docs[run_id]
            findings = [dict(docs[fid]) for fid in reopened] + [dict(f) for f in body.get("new_findings", [])]
            head = new["commits"][-1]["sha"] if new.get("commits") else new["base_sha"]
            child = self._child_body(request_id, body, run, findings, head)
            child_id = rid("run", "run", caller, child["request_id"], facts_sha256(child))
            if child_id in self.runs:
                raise Conflict("the child run already exists", request_id=request_id, facts_sha256=facts)
            ckey, ch, cent = self._idem(caller, child["request_id"], "fix-runs", child)
            if cent is not None:
                raise Conflict("the child run's request id was already used", request_id=request_id, facts_sha256=facts)
            missing = [f["id"] for f in findings if f.get("reproduction_test") and not (child_red or {}).get(f["id"])]
            if missing:
                self._refuse_red(caller, request_id, facts, "REPRODUCTION_RED_UNVERIFIED", missing[0], None,
                                 f"finding {missing[0]}: no admission RED check for its reviewer-authored reproduction",
                                 {"why": "no_red_check"})
        eid = op.record(derived_id("rev", run_id, request_id, facts), "fix_run_reviewed", caller, run_id,
                        {"run_id": run_id, "request_id": request_id, "facts_sha256": facts, "verdict": body["verdict"],
                         "review_sha256": body["sha256"], "reopened": reopened, "accepted": accepted,
                         "src_diff_sha256": review["src_diff_sha256"],
                         "flags_addressed": [x["flag_id"] for x in review["flags_addressed"]],
                         "flag_notes_sha256": {x["flag_id"]: x["note_sha256"] for x in review["flags_addressed"]},
                         "notes_sha256": {fid: sha_text(v.get("note") or "") for fid, v in sorted(verdicts.items())},
                         "new_findings": review["new_finding_ids"], "from": run["status"], "to": to,
                         "next_run_id": child_id},
                        f"Fix run reviewed: {body['verdict']} ({run_id})")
        new["ledger_event_ids"].append(eid)
        for fid, rec0, target in updates:
            rec = json.loads(json.dumps(rec0))
            rec["state"] = target
            rec["review"] = {"verdict": "accept" if target == "accepted" else "reopen", "request_id": request_id,
                             "note_sha256": sha_text((verdicts.get(fid) or {}).get("note") or ""),
                             "flags_addressed": sorted(fl["id"] for fl in rec0.get("review_flags") or []
                                                       if fl["id"] in {x["flag_id"] for x in review["flags_addressed"]})}
            op.add("finding", rec)
        op.add("run", new)
        resp = self._stamp({"run_id": run_id, "status": to, "next_run_id": child_id, "request_id": request_id,
                            "facts_sha256": facts})
        if child is not None:
            _, got, _ = self._admit_locked(caller, child, child["base_sha"], principal, child_red or {}, ckey, ch,
                                           parent_run_id=run_id, op=op)
            if got != child_id:
                raise Conflict("the child run id is not the one the review recorded")
        self._idem_add(op, key, h, resp)
        self._commit(op)
        if child_id is not None:
            self._queue.put(child_id)
        return resp

    @staticmethod
    def _child_body(request_id: str, body: dict, run: dict, findings: list[dict], head: str) -> dict:
        return {"request_id": f"{request_id}:rerun", "source": {"kind": "aegis_review", "ref": body["review_ref"],
                                                               "sha256": body["sha256"]},
                "base_ref": run["base_ref"], "base_sha": head, "service": run["service"], "findings": findings}

    def cancel(self, caller: str, run_id: str, body: dict) -> dict:
        request_id = body["request_id"]
        facts = facts_sha256(body)
        with self.lock:
            key, h, ent = self._idem(caller, request_id, f"cancel/{run_id}", body)
            if ent is not None:
                return ent["response"]
            run = self.runs.get(run_id)
            if run is None and run_id in self._admissions:
                return self._cancel_admission_locked(caller, run_id, body, request_id, facts, key, h)
            if run is None:
                raise NotFound("no such run")
            if run["status"] in states.TERMINAL or run["status"] == "awaiting_review":
                raise Conflict("the run is not cancellable in its state", reasons=[R.item("CANCELLED", run["status"])],
                               request_id=request_id, facts_sha256=facts)
            eid = self.run_transition(run_id, "failed", "fix_run_cancelled",
                                      {"run_id": run_id, "request_id": request_id, "facts_sha256": facts, "reason_sha256": sha_text(body["reason"])},
                                      f"Fix run cancelled ({run_id})", {"reasons": [R.item("CANCELLED", "cancelled by " + caller)]})
            resp = self._stamp({"run_id": run_id, "status": "failed", "request_id": request_id, "facts_sha256": facts, "ledger_event_id": eid})
            op = Op(self, f"cancel-idem|{run_id}|{request_id}", caller, run_id)
            self._idem_add(op, key, h, resp)
            self._commit(op)
            if self._engine is not None and hasattr(self._engine, "interrupt"):
                self._engine.interrupt(run_id, "cancel")           # R6: the in-flight turn is stopped, not just flagged
            return resp

    def _cancel_admission_locked(self, caller: str, adm: str, body: dict, request_id: str, facts: str, key: tuple,
                                 h: str) -> dict:
        """Wave 24 (E6, N23-D-7): the operator cancels a PENDING admission (its reviewer-test RED containers running
        outside the lock). Recorded first (``admission_cancelled``, with the idempotency record in one local-log
        line); then the service's slot is free at once, and when the containers end their results are discarded and
        the admission (or the review that started it) is refused 409 — nothing is admitted from it."""
        a = self._admissions[adm]
        op = Op(self, f"admission-cancel|{adm}|{request_id}", caller, adm)
        eid = op.record(derived_id("acx", adm, request_id), "admission_cancelled", caller, adm,
                        {"admission_id": adm, "service": a["service"], "review_of": a.get("review_of"),
                         "admission_request_id": a["request_id"], "request_id": request_id, "facts_sha256": facts,
                         "reason_sha256": sha_text(body["reason"])},
                        f"Pending admission cancelled ({a['service']})")
        resp = self._stamp({"run_id": adm, "status": "cancelled", "request_id": request_id, "facts_sha256": facts,
                            "ledger_event_id": eid})
        self._idem_add(op, key, h, resp)
        self._commit(op)
        self._admissions.pop(adm, None)            # the slot is free now
        self._cancelled_admissions.add(adm)
        return resp

    # ================================================================== audit, reconcile, founder refusals

    def audit_export(self, principal: str, since: Optional[str], until: Optional[str], cursor: int) -> dict:
        with self.lock:
            s = parse_iso(since) if since else None
            u = parse_iso(until) if until else None
            out, last = [], cursor
            for rec in self.log.iter_records(cursor + 1):
                last = rec["seq"]
                at = parse_iso(rec["at"])
                if (s and at < s) or (u and at > u):
                    continue
                out.append(EA.export_entry(rec))
                if len(out) >= EA.PAGE:
                    break
            more = last < len(self.log)
            eid = self._record(derived_id("aud", principal, since, until, cursor, len(self.log)), "audit_export_issued",
                               EVIDENCE, "audit_export", {"cursor": cursor, "count": len(out), "next_cursor": last if more else None},
                               f"Audit export page served ({len(out)} records)")
            return self._stamp({"records": out, "next_cursor": last if more else None, "ledger_event_id": eid,
                                "chain_valid": self.log.verify()})

    def founder_refused(self, route: str, reason: str) -> None:
        with self.lock:
            if self.reconcile_mode:
                return
            try:
                self._record_plain(derived_id("fr", route, reason, self.now_iso()), "founder_approval_refused", GATE, route,
                                   {"route": route, "reason_sha256": sha_text(reason)}, f"Andre approval refused on {route}")
            except Unavailable:
                pass

    def reconcile_plan(self) -> dict:
        with self.lock:
            try:
                a = self.assess_log()
            except LedgerQueryFailed as exc:
                raise Unavailable(f"the ledger cannot be read: {exc}") from None
            return {"epoch": self.log.epoch, "head_seq": len(self.log),
                    "head_sha256": self.log.line_shas()[-1] if len(self.log) else "0" * 64,
                    "fatal": a.fatal, "voidable": a.voidable, "void_lines": sorted(a.void_lines),
                    "void_event_ids": sorted(a.void_event_ids), "reconcile_mode": self.reconcile_mode}

    def reconcile(self, request_id: str, head_sha256: str, void_lines: list[int], void_event_ids: list[str]) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, "reconcile", {"head": head_sha256, "lines": void_lines, "ids": void_event_ids})
            if ent is not None:
                return ent["response"]
            plan = self.reconcile_plan()
            if plan["fatal"]:
                raise Conflict("cannot reconcile: " + "; ".join(plan["fatal"]))
            if plan["head_sha256"] != head_sha256:
                raise Conflict("the local log head moved since you read the plan; read GET /dlv/v1/reconcile again")
            if not plan["voidable"]:
                raise Conflict("nothing to reconcile: the local log matches the ledger")
            if set(void_lines) - set(plan["void_lines"]) or set(void_event_ids) - set(plan["void_event_ids"]):
                raise Invalid("void_lines / void_event_ids must be a subset of the plan")
            payload = EA.reconcile_payload(plan["epoch"], None, plan["head_seq"], plan["head_sha256"], void_lines, void_event_ids)
            eid = EA.reconcile_id(plan["epoch"], plan["head_seq"], payload)
            self._reconciling = True
            try:
                op = Op(self, f"reconcile|{eid}", "andre", EA.LOG_SUBJECT)
                op.record(eid, EA.RECONCILE_TYPE, "andre", EA.LOG_SUBJECT, payload,
                          f"Andre reconciled the local log at line {plan['head_seq']}: {len(payload['void_event_ids'])} event(s) voided")
                op.add("reconcile", {"payload": payload, "reconcile_event_id": eid, "request_id": request_id})
                resp = {"reconcile_event_id": eid, "voided": payload["void_event_ids"], "void_lines": payload["void_lines"],
                        "restart_required": self.reconcile_mode, "request_id": request_id}
                self._idem_add(op, key, h, resp)
                self._commit(op)
            finally:
                self._reconciling = False
            left = self.assess_log()
            self.reconcile_required = list(left.voidable)
            resp["remaining_problems"] = left.problems
            return self._stamp(resp)


__all__ = ["DeliveryService", "DlvError", "Op", "facts_sha256", "rid", "sha", "sha_text", "evidence_id_for"]
