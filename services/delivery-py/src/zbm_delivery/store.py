"""
Local append-only record log (spec §B, ADR 0006 decisions 4-5 pattern; copied from finance-py src/store.py). Stdlib only.

``DLV_DATA_DIR/dlv_log.jsonl``: one JSON object per line,
``{"seq", "kind", "at", "data", "prev_line_sha256", "record_sha256"}``, where
``prev_line_sha256`` is the SHA-256 of the previous line's exact bytes (the
first line carries 64 zeros) and ``record_sha256`` is the line's own hash
(without that field), so an edit of the LAST line is caught too. The chain
detects edits, deletions, reordering and torn writes; a whole-file rewrite
with recomputed hashes, a truncated tail or a deleted log is caught against
the ledger instead: the service anchors every line (``prepare`` gives the
exact line before it is written) on the ledger first (AEGIS N14-4, ``evidence_audit.assess``). Every append is flushed and fsynced before the
caller lets the record take effect. At start the whole chain is verified;
any mismatch (edited, deleted, reordered or torn line) refuses start-up.

Without a data dir the log lives in memory (``in_memory = True``, reported
by /health) and nothing survives a restart — after a restart no run
exists (fail closed; spec S9).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Iterator, Optional

GENESIS = "0" * 64
LOG_NAME = "dlv_log.jsonl"


class StoreCorrupt(RuntimeError):
    pass


class StoreWriteError(RuntimeError):
    pass


def _line_sha(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


def encode(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def verify_lines(lines: list[bytes]) -> list[dict]:
    """Verify a whole chain; returns the decoded records or raises StoreCorrupt."""
    prev = GENESIS
    out = []
    for i, line in enumerate(lines):
        try:
            rec = json.loads(line)
        except ValueError as exc:
            raise StoreCorrupt(f"log line {i + 1} is not JSON") from exc
        if not isinstance(rec, dict) or rec.get("prev_line_sha256") != prev or rec.get("seq") != i + 1:
            raise StoreCorrupt(f"log chain broken at line {i + 1}")
        body = {k: v for k, v in rec.items() if k != "record_sha256"}
        if rec.get("record_sha256") != hashlib.sha256(encode(body)).hexdigest():
            raise StoreCorrupt(f"log line {i + 1} does not match its own hash")
        if encode(rec) != line:
            raise StoreCorrupt(f"log line {i + 1} is not in canonical form")
        prev = _line_sha(line)
        out.append(rec)
    return out


class RecordLog:
    def __init__(self, data_dir: Optional[str]):
        self.lock = threading.RLock()
        self.data_dir = data_dir
        self.in_memory = not data_dir
        self._lines: list[bytes] = []
        self.path: Optional[str] = None
        self.fail_next_append = False  # tests: simulate a disk failure
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
            self.path = os.path.join(data_dir, LOG_NAME)
            if os.path.exists(self.path):
                with open(self.path, "rb") as fh:
                    raw = fh.read()
                if raw and not raw.endswith(b"\n"):
                    raise StoreCorrupt("log ends with a torn line; refusing to start (inspect the file)")
                self._lines = [ln for ln in raw.split(b"\n") if ln] if raw else []
                verify_lines(self._lines)

    @property
    def records(self) -> list[dict]:
        with self.lock:
            return [json.loads(ln) for ln in self._lines]

    def __len__(self) -> int:
        return len(self._lines)

    def iter_records(self, start_seq: int = 1) -> Iterator[dict]:
        with self.lock:
            lines = list(self._lines[max(0, start_seq - 1):])
        for ln in lines:
            yield json.loads(ln)

    @property
    def epoch(self) -> Optional[str]:
        """Identity of THIS log: the first 16 hex of its first line's SHA-256 (None while empty).
        Ledger anchors carry it, so another log's anchors are never mistaken for ours."""
        with self.lock:
            return _line_sha(self._lines[0])[:16] if self._lines else None

    def line_shas(self) -> list[str]:
        with self.lock:
            return [_line_sha(ln) for ln in self._lines]

    def prepare(self, kind: str, at: str, data: dict) -> tuple[dict, bytes]:
        """The exact next line, without writing it (so it can be anchored on the ledger first)."""
        with self.lock:
            prev = _line_sha(self._lines[-1]) if self._lines else GENESIS
            rec = {"seq": len(self._lines) + 1, "kind": kind, "at": at, "data": data, "prev_line_sha256": prev}
            rec["record_sha256"] = hashlib.sha256(encode(rec)).hexdigest()
            return rec, encode(rec)

    def append(self, kind: str, at: str, data: dict) -> dict:
        rec, line = self.prepare(kind, at, data)
        return self.append_prepared(rec, line)

    def append_prepared(self, rec: dict, line: bytes) -> dict:
        with self.lock:
            if self.fail_next_append:
                self.fail_next_append = False
                raise StoreWriteError("simulated local store failure")
            if rec["seq"] != len(self._lines) + 1:
                raise StoreWriteError("log moved on since the line was prepared")
            if self.path:
                try:
                    fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                    try:
                        os.write(fd, line + b"\n")
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                except OSError as exc:
                    raise StoreWriteError(f"local log write failed: {type(exc).__name__}") from exc
            self._lines.append(line)
            return rec

    def verify(self) -> bool:
        """Re-verify the chain as stored (the file, when there is one)."""
        with self.lock:
            try:
                if self.path:
                    with open(self.path, "rb") as fh:
                        raw = fh.read()
                    if raw and not raw.endswith(b"\n"):
                        return False
                    lines = [ln for ln in raw.split(b"\n") if ln]
                    if len(lines) != len(self._lines):
                        return False
                else:
                    lines = self._lines
                verify_lines(lines)
                return True
            except (OSError, StoreCorrupt):
                return False
