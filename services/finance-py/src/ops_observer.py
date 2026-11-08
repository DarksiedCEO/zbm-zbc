"""Dormant observation state, not a sender or a financial journal (ADR0039)."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone

from health_poll import _CODES

STATE_FILE = "ops-observer.json"
MARKER_FILE = "ops-observer.initialized"
MAX_BYTES = 128 * 1024
MAX_ITEMS = 512
MAX_GENERATION = 2**63 - 1
SOURCES = ("local:founder", "health:finance", "health:legal", "health:compliance", "health:ledger",
           "remote:holds", "remote:expiry")
COVERAGE = ("COMPLETE", "UNKNOWN", "INCOMPLETE", "UNCONFIGURED")
CATEGORIES = ("compliance_hold", "service_unhealthy")
_MARKER = b"ops-observer-v1\n"
_BREAK = re.compile(r"fin-brk-[0123456789ABCDEFGHJKMNPQRSTVWXYZ]{26}")
_EXTRA = frozenset({"bad_body", "bad_status", "bad_reason", "bad_warning", "bad_write_ok", "write_refused",
                    "unreachable", "deadline_exceeded", "poll_budget_exhausted", "bad_encoding", "response_too_large"})


def safe_health_code(value):
    if not isinstance(value, str) or len(value) > 512:
        return False
    if value.startswith("warning:"):
        value = value[8:]
    return all(x in _CODES or x in _EXTRA or re.fullmatch(r"http_[1-5][0-9]{2}", x) for x in value.split(","))


def stamp(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("invalid observation time")
    return value.astimezone(timezone.utc).isoformat()


def parse(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid observation time")
    result = datetime.fromisoformat(value)
    stamp(result)
    return result


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def preview_id(value):
    return hashlib.sha256(encoded({k: v for k, v in value.items() if k != "preview_id"})).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    source: str
    coverage: str
    observed_at: datetime
    items: dict


def valid_items(source, items):
    if not isinstance(items, dict) or len(items) > MAX_ITEMS:
        raise ValueError("invalid observation items")
    for key, category in items.items():
        if source == "local:founder":
            good = isinstance(key, str) and _BREAK.fullmatch(key) and category == "compliance_hold"
        elif source.startswith("health:"):
            good = safe_health_code(key) and category == "service_unhealthy"
        else:
            good = False  # Remote feeds have no complete-feed contract in this slice.
        if not good:
            raise ValueError("invalid observation item")


def validate(value):
    if not isinstance(value, dict) or set(value) != {"schema", "generation", "observed_at", "sources", "preview_id"}:
        raise ValueError("invalid observation state")
    if type(value["schema"]) is not int or value["schema"] != 1 or type(value["generation"]) is not int or not 1 <= value["generation"] <= MAX_GENERATION:
        raise ValueError("invalid observation generation")
    now = parse(value["observed_at"])
    sources = value["sources"]
    if not isinstance(sources, dict) or set(sources) != set(SOURCES):
        raise ValueError("invalid observation sources")
    count = 0
    for name, s in sources.items():
        if not isinstance(s, dict) or set(s) != {"coverage", "observed_at", "generation", "stale", "items"}:
            raise ValueError("invalid observation source")
        if s["coverage"] not in COVERAGE or type(s["stale"]) is not bool:
            raise ValueError("invalid source coverage")
        if name.startswith("remote:") and s["coverage"] != "UNCONFIGURED":
            raise ValueError("unsupported remote coverage")
        if s["stale"] != (s["coverage"] != "COMPLETE"):
            raise ValueError("invalid source freshness")
        if type(s["generation"]) is not int or not 1 <= s["generation"] <= value["generation"]:
            raise ValueError("invalid source generation")
        if parse(s["observed_at"]) > now:
            raise ValueError("future source")
        valid_items(name, s["items"])
        count += len(s["items"])
    if count > MAX_ITEMS or value["preview_id"] != preview_id(value):
        raise ValueError("invalid observation bound or identity")
    if len(encoded(value)) > MAX_BYTES:
        raise ValueError("observation too large")


class ObserverState:
    """Caller holds the Finance directory claim and observer mutex for every access.

    Post-replace fsync errors latch unknown even if bytes moved. Losing both files externally
    cannot be distinguished from first initialization; this is not a backup/rollback authority.
    """
    def __init__(self, directory):
        self.directory = directory
        self.current = None
        self.fault = False
        self.established = False
        try:
            with self._directory() as d:
                marker = self._read(d, MARKER_FILE, 64)
                raw = self._read(d, STATE_FILE, MAX_BYTES)
                if marker is None and raw is None:
                    return
                self.established = True
                if marker != _MARKER or raw is None:
                    raise ValueError("missing observation state")
                value = json.loads(raw)
                validate(value)
                self.current = value
        except (OSError, ValueError, TypeError, RecursionError):
            self.fault = True

    def _directory(self):
        class Directory:
            def __enter__(inner):
                inner.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                s = os.fstat(inner.fd)
                if s.st_uid != os.getuid() or s.st_mode & 0o022:
                    os.close(inner.fd)
                    raise ValueError("unsafe observation directory")
                return inner.fd
            def __exit__(inner, *args):
                os.close(inner.fd)
        return Directory()

    @staticmethod
    def _read(d, name, limit):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=d)
        except FileNotFoundError:
            return None
        try:
            s = os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077 or s.st_size > limit:
                raise ValueError("unsafe observation file")
            raw = os.read(fd, limit + 1)
            if len(raw) > limit:
                raise ValueError("oversized observation file")
            return raw
        finally:
            os.close(fd)

    @staticmethod
    def _write(fd, raw):
        try:
            remaining = memoryview(raw)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("short observation write")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    def _save(self, value):
        with self._directory() as d:
            marker = self._read(d, MARKER_FILE, 64)
            raw = self._read(d, STATE_FILE, MAX_BYTES)
            if self.established:
                if marker != _MARKER or raw is None or self.current is None or raw != encoded(self.current):
                    raise ValueError("observation state changed")
            else:
                if marker is not None or raw is not None:
                    raise ValueError("observation state appeared")
                fd = os.open(MARKER_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=d)
                try:
                    self._write(fd, _MARKER)
                    os.fsync(d)
                finally:
                    self.established = True
            temp = ".ops-observer-" + secrets.token_hex(16)
            try:
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=d)
                self._write(fd, encoded(value))
                os.replace(temp, STATE_FILE, src_dir_fd=d, dst_dir_fd=d)
                os.fsync(d)
            finally:
                try:
                    os.unlink(temp, dir_fd=d)
                except FileNotFoundError:
                    pass

    def result(self, persisted=False):
        state = self.current
        sources = state["sources"] if state else {}
        counts = {category: sum(list(s["items"].values()).count(category) for s in sources.values()) for category in CATEGORIES}
        return {"status": "degraded" if self.fault or not state or any(s["coverage"] != "COMPLETE" for s in sources.values()) else "observed",
                "persisted": persisted, "generation": state["generation"] if state else 0,
                "preview_id": state["preview_id"] if state else None,
                "coverage": {name: sources[name]["coverage"] if name in sources and not self.fault else "UNKNOWN" for name in SOURCES},
                "open_count": sum(counts.values()),
                "stale_count": sum(len(s["items"]) for s in sources.values() if s["stale"] or self.fault),
                "preview": counts, "persistence": "unknown" if self.fault else "committed" if state else "uninitialized"}

    def commit(self, snapshots, now):
        if self.fault:
            return self.result()
        try:
            timestamp = stamp(now)
            prior = self.current
            if prior and now < parse(prior["observed_at"]):
                raise ValueError("clock rollback")
            gen = (prior["generation"] if prior else 0) + 1
            next_state = {"schema": 1, "generation": gen, "observed_at": timestamp, "sources": {}, "preview_id": ""}
            incoming = {}
            for s in snapshots:
                if not isinstance(s, Snapshot) or s.source not in SOURCES or s.source in incoming or s.coverage not in COVERAGE:
                    raise ValueError("invalid source snapshot")
                valid_items(s.source, s.items)
                stamp(s.observed_at)
                incoming[s.source] = s
            for name in SOURCES:
                s = incoming.get(name)
                coverage = s.coverage if s else "UNCONFIGURED"
                if s and not 0 <= (now - s.observed_at).total_seconds() <= 300:
                    coverage = "UNKNOWN"
                old = prior["sources"][name] if prior else None
                items = copy.deepcopy(s.items) if s and coverage == "COMPLETE" else copy.deepcopy(old["items"] if old else {})
                if s and coverage != "COMPLETE":
                    items.update(s.items)
                next_state["sources"][name] = {"coverage": coverage, "observed_at": stamp(s.observed_at) if s and coverage == "COMPLETE" else timestamp,
                                                "generation": gen, "stale": coverage != "COMPLETE", "items": items}
            next_state["preview_id"] = preview_id(next_state)
            validate(next_state)
            self._save(next_state)
            self.current = next_state
            return self.result(True)
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            self.fault = True
            return self.result()
