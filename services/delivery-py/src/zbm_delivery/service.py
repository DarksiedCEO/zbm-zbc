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
from zbm_delivery.errors import Conflict, DlvError, Invalid, NotFound, Refused, Unavailable
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
            resolve_sandbox_paths=self._resolve_sandbox_paths, evidence_root=self.evidence_root))
        if self.reconcile_mode:
            return
        if not self.log.in_memory and len(self.log):
            try:
                self._write_lease()
            except Unavailable as exc:
                raise RuntimeError(f"refusing to start: the instance lease could not be recorded ({exc.reason})") from None
        self._fail_live_runs_at_start()
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
        """Tests and the live runner: block until the queue is drained."""
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                live = [r for r in self.runs.values() if r["status"] in ("received", "preparing", "running", "suite", "reporting")]
            if not live and self._queue.empty():
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
        engine runs it in the verification checkout (must pass) and the reverted one (must fail) before ``fixed``,
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

    def _red_precheck(self, caller: str, request_id: str, facts: str, base_sha: str, service: str, run_id: str,
                      principal_user_id: str, findings: list[dict]) -> None:
        """L2: every reviewer-authored reproduction must FAIL on ``base_sha`` before anything is recorded: run in a
        fresh engine container on the base tree with the test overlaid. A pass (or an unknown verdict) is refused
        — 422 ``reproduction_not_red`` — and the refusal recorded. Runs OUTSIDE the service lock (a container run
        must not stall the other runs); the caller re-checks everything under the lock afterwards."""
        items = [f for f in findings if f.get("reproduction_test")]
        if not items or self._engine is None or not hasattr(self._engine, "reproduction_red"):
            return
        for f in items:
            node = node_id_in_text(f.get("reproduction") or "")
            try:
                t = self._engine.reproduction_red(run_id=run_id, service=service, base_sha=base_sha,
                                                  principal_user_id=principal_user_id, finding_id=f["id"],
                                                  path=f["reproduction_test"]["path"],
                                                  content=f["reproduction_test"]["content"], node=node)
            except Unavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - the container could not run: refused, never admitted unchecked
                self._refuse(caller, request_id, facts, "HARNESS_ERROR",
                             f"finding {f['id']}: the reproduction RED check could not run ({type(exc).__name__})")
            if t.verdict == "fail":
                try:
                    self._record_plain(derived_id("red", run_id, f["id"], t.output_sha256), "reproduction_red_checked", GATE,
                                       request_id, {"request_id": request_id, "facts_sha256": facts, "admission_id": run_id,
                                                    "finding_id": f["id"], "target": node, "base_sha": base_sha,
                                                    "verdict": t.verdict, "exit": t.exit, "output_sha256": t.output_sha256,
                                                    "test_sha256": sha_text(f["reproduction_test"]["content"])},
                                       f"Reviewer reproduction RED on base for {f['id']}")
                except Unavailable:
                    raise
            else:
                code = "REPRODUCTION_NOT_RED"
                why = ("passes on the base commit" if t.verdict == "pass" else
                       f"could not be verified on the base commit ({(t.counts.why if t.counts else 'unknown')[:120]})")
                message = f"finding {f['id']}: the reviewer-authored reproduction {node} {why}"
                eid = derived_id("ref", caller, request_id, facts, code, f["id"])
                try:
                    self._record_plain(eid, "fix_run_refused", GATE, request_id,
                                       {"request_id": request_id, "facts_sha256": facts, "code": code, "principal": caller,
                                        "finding_id": f["id"], "target": node, "verdict": t.verdict, "exit": t.exit,
                                        "output_sha256": t.output_sha256},
                                       f"Fix run refused: {code} ({f['id']})")
                except Unavailable:
                    pass
                raise Invalid(message, code="reproduction_not_red", finding_id=f["id"], target=node,
                              reasons=[R.item(code, message)], request_id=request_id, facts_sha256=facts, took_effect=False)

    def _precheck_document(self, caller: str, principal, request_id: str, facts: str, body: dict) -> None:
        """L2: before the lock, for a findings document carrying reviewer-authored tests: a replay returns through
        the normal path; otherwise the static checks, then the RED run of each reviewer test on the base. Any state
        this needs that is missing (no engine, no sandbox, a bad base) is left to the normal path to refuse."""
        with self.lock:
            _, _, ent = self._idem(caller, request_id, "fix-runs", body)
            if ent is not None or self._engine is None or not self.sandbox_available():
                return
            try:
                base_sha = self.git.rev_parse(body["base_sha"], run_id="-")
            except Exception:  # noqa: BLE001 - the normal path refuses (GIT_REFUSED) or raises Unavailable
                return
            try:
                self._check_reproductions(caller, request_id, facts, base_sha, body["service"], body["findings"])
            except GitRefused:
                return
        adm = rid("run", "admission", caller, request_id, facts)     # its own id: never the run's event ids
        self._red_precheck(caller, request_id, facts, base_sha, body["service"], adm, principal.user_id(adm),
                           body["findings"])

    def create_fix_run(self, caller: str, body: dict, *, parent_run_id: Optional[str] = None) -> dict:
        from zbm_delivery.adapters.identity import PrincipalMissing, principal_for
        request_id = body["request_id"]
        facts = facts_sha256(body)
        try:
            principal = principal_for(caller)
        except PrincipalMissing:
            self._refuse(caller, request_id, facts, "PRINCIPAL_MISSING", "the caller maps to no principal")
        if parent_run_id is None and any(f.get("reproduction_test") for f in body["findings"]):
            self._precheck_document(caller, principal, request_id, facts, body)
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
            for r in self.runs.values():
                if r["service"] == service and r["status"] in ("received", "preparing", "running", "suite", "reporting"):
                    raise Conflict("a run for this service is in progress", reasons=[R.item("RUN_IN_PROGRESS", r["run_id"])],
                                   request_id=request_id, facts_sha256=facts)
            if parent_run_id is None:
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
            else:
                base_sha = body["base_sha"]
            try:
                self._check_reproductions(caller, request_id, facts, base_sha, service, body["findings"])
            except GitRefused:
                self._refuse(caller, request_id, facts, "GIT_REFUSED", "the base commit could not be read")
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
            op = Op(self, f"fix-run|{run_id}", caller, run_id)
            eid = op.record(derived_id("rcv", run_id), "fix_run_received", caller, run_id,
                            {"run_id": run_id, "request_id": request_id, "facts_sha256": facts, "service": service,
                             "base_sha": base_sha, "finding_ids": run["finding_ids"], "parent_run_id": parent_run_id,
                             "source_sha256": body["source"]["sha256"],
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
                                   "reasons": [], "suite_failures": [], "brief_evidence_id": None,
                                   "injection_rules": textguard.injection_rules_in(f),
                                   "reviewer_test": ({"path": f["reproduction_test"]["path"], "author": "reviewer",
                                                      "sha256": sha_text(f["reproduction_test"]["content"])}
                                                     if f.get("reproduction_test") else None)})
            resp = self._stamp({"run_id": run_id, "status": "received", "request_id": request_id, "facts_sha256": facts,
                                "ledger_event_id": eid})
            self._idem_add(op, key, h, resp)
            self._commit(op)
            self._queue.put(run_id)
            return resp

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
        """Baseline failures attributable to ANOTHER not-yet-fixed finding of this run (file match, or the node id
        named in that finding's reproduction) may remain while this finding is fixed (§C.8.4 step 5)."""
        out: set[str] = set()
        with self.lock:
            docs = self.finding_docs.get(run_id, {})
            recs = self.findings.get(run_id, {})
            service = self.runs[run_id]["service"]
            for fid, doc in docs.items():
                if fid == finding_id or recs.get(fid, {}).get("state") in ("fixed", "disproved"):
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
                if recs.get(fid, {}).get("state") in ("fixed", "disproved", "reviewed"):
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
        restart replays the last recorded state and fails it then)."""
        with self.lock:
            run = self.runs.get(run_id)
            if run is not None and run["status"] not in states.TERMINAL:
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

    def review(self, caller: str, run_id: str, body: dict) -> dict:
        request_id = body["request_id"]
        facts = facts_sha256(body)
        if body["verdict"] == "fail":
            self._precheck_review(caller, run_id, request_id, facts, body)
        with self.lock:
            key, h, ent = self._idem(caller, request_id, f"review/{run_id}", body)
            if ent is not None:
                return ent["response"]
            run = self.runs.get(run_id)
            if run is None:
                raise NotFound("no such run")
            if run["status"] != "awaiting_review":
                raise Conflict("the run is not awaiting review", reasons=[R.item("REVIEW_STATE", run["status"])],
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
            if body["verdict"] == "fail":
                # wave 21 (R1): the child run's findings must each name a reproduction runnable on the child's base
                # (this run's head) — checked BEFORE the review is recorded, so a refused review changes nothing
                head0 = run["commits"][-1]["sha"] if run.get("commits") else run["base_sha"]
                docs0 = self.finding_docs[run_id]
                try:
                    self._check_reproductions(caller, request_id, facts, head0, run["service"],
                                              [dict(docs0[fid]) for fid in body.get("reopened", [])]
                                              + [dict(f) for f in body.get("new_findings", [])])
                except GitRefused:
                    raise Invalid("the run's head commit could not be read") from None
            to = "reviewed_pass" if body["verdict"] == "pass" else "reviewed_fail"
            review = {"request_id": request_id, "facts_sha256": facts, "verdict": body["verdict"],
                      "review_ref": body["review_ref"], "sha256": body["sha256"], "reopened_ids": list(body.get("reopened", [])),
                      "new_finding_ids": [f["id"] for f in body.get("new_findings", [])], "received_at": self.now_iso(),
                      "caller": caller}
            new = json.loads(json.dumps(run))
            new.update(status=to, review=review, finished_at=self.now_iso())
            op = Op(self, f"review|{run_id}|{request_id}", caller, run_id)
            eid = op.record(derived_id("rev", run_id, request_id, facts), "fix_run_reviewed", caller, run_id,
                            {"run_id": run_id, "request_id": request_id, "facts_sha256": facts, "verdict": body["verdict"],
                             "review_sha256": body["sha256"], "reopened": review["reopened_ids"],
                             "new_findings": review["new_finding_ids"], "from": run["status"], "to": to},
                            f"Fix run reviewed: {body['verdict']} ({run_id})")
            new["ledger_event_ids"].append(eid)
            for fid in review["reopened_ids"]:
                rec = json.loads(json.dumps(self.findings[run_id][fid]))
                rec["state"] = "reviewed" if rec["state"] in ("fixed", "disproved") else rec["state"]
                op.add("finding", rec)
            if body["verdict"] == "pass":
                for rec in self.findings[run_id].values():
                    if rec["state"] in ("fixed", "disproved"):
                        r2 = json.loads(json.dumps(rec))
                        r2["state"] = "reviewed"
                        op.add("finding", r2)
            op.add("run", new)
            resp = self._stamp({"run_id": run_id, "status": to, "next_run_id": None, "request_id": request_id, "facts_sha256": facts})
            self._idem_add(op, key, h, resp)
            self._commit(op)
            if body["verdict"] == "fail":
                docs = self.finding_docs[run_id]
                findings = [dict(docs[fid]) for fid in review["reopened_ids"]] + [dict(f) for f in body.get("new_findings", [])]
                head = new["commits"][-1]["sha"] if new.get("commits") else new["base_sha"]
                child = self._child_body(request_id, body, run, findings, head)
                child_resp = self.create_fix_run(caller, child, parent_run_id=run_id)
                resp = dict(resp, next_run_id=child_resp["run_id"])
                ent2 = self.idem.get(key)
                if ent2 is not None:
                    ent2["response"] = resp
            return resp

    @staticmethod
    def _child_body(request_id: str, body: dict, run: dict, findings: list[dict], head: str) -> dict:
        return {"request_id": f"{request_id}:rerun", "source": {"kind": "aegis_review", "ref": body["review_ref"],
                                                               "sha256": body["sha256"]},
                "base_ref": run["base_ref"], "base_sha": head, "service": run["service"], "findings": findings}

    def _precheck_review(self, caller: str, run_id: str, request_id: str, facts: str, body: dict) -> None:
        """L2: a failing review whose reopened or new findings carry reviewer-authored tests: each must FAIL on the
        run's head (the child run's base) before the review is recorded. Everything else is left to review()."""
        with self.lock:
            _, _, ent = self._idem(caller, request_id, f"review/{run_id}", body)
            run = self.runs.get(run_id)
            if ent is not None or run is None or run["status"] != "awaiting_review" or self._engine is None:
                return
            docs0 = self.finding_docs.get(run_id, {})
            known = set(run["finding_ids"])
            if any(x not in known for x in body.get("reopened", [])):
                return
            findings = [dict(docs0[fid]) for fid in body.get("reopened", [])] + [dict(f) for f in body.get("new_findings", [])]
            if not any(f.get("reproduction_test") for f in findings) or not self.sandbox_available():
                return
            head0 = run["commits"][-1]["sha"] if run.get("commits") else run["base_sha"]
            service = run["service"]
            try:
                self._check_reproductions(caller, request_id, facts, head0, service, findings)
            except GitRefused:
                return
        adm = rid("run", "admission", caller, request_id, facts)
        from zbm_delivery.adapters.identity import PrincipalMissing, principal_for
        try:
            principal = principal_for(caller)
        except PrincipalMissing:
            return
        self._red_precheck(caller, request_id, facts, head0, service, adm, principal.user_id(adm), findings)

    def cancel(self, caller: str, run_id: str, body: dict) -> dict:
        request_id = body["request_id"]
        facts = facts_sha256(body)
        with self.lock:
            key, h, ent = self._idem(caller, request_id, f"cancel/{run_id}", body)
            if ent is not None:
                return ent["response"]
            run = self.runs.get(run_id)
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
