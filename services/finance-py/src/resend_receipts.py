"""Dormant complete-payload transport; deliberately NOT a ClientMailPort or production factory.

Only explicitly constructed callers with a complete frozen request can use this boundary. No environment, receipt
journal, sender policy, credential provisioning, or automatic retries are provided (ADR0037).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ports import ReceiptDeliveryOutcome

ENDPOINT = "https://api.resend.com/emails"
MAX_BODY = 40 * 1024
MAX_RESPONSE = 64 * 1024
MAX_INPUT = 128 * 1024
MAX_OUTPUT = 128
_LABEL = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_KEY = re.compile(r"[A-Za-z0-9._:/-]{1,256}")
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_PERMANENT = {(400, "validation_error"), (400, "invalid_idempotency_key"), (401, "missing_api_key"),
              (401, "validation_error"), (401, "restricted_api_key"), (403, "invalid_permission"),
              (403, "restricted_api_key"), (403, "suspended_api_key"), (403, "validation_error")}


@dataclass(frozen=True, repr=False)
class ResendTransportProfile:
    profile_id: str
    revision: str
    sender: str


@dataclass(frozen=True, repr=False)
class FrozenResendRequest:
    version: str
    provider: str
    profile_id: str
    profile_revision: str
    body: bytes
    body_sha256: str
    idempotency_key: str
    first_attempt_at: datetime
    retry_until: datetime


def _utc(value) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _window(request: FrozenResendRequest, now: datetime) -> bool:
    return all(_utc(x) for x in (now, request.first_attempt_at, request.retry_until)) \
        and request.first_attempt_at <= now < request.retry_until \
        and timedelta(0) < request.retry_until - request.first_attempt_at <= timedelta(hours=23)


def _string(value, maximum: int, multiline: bool = False) -> bool:
    return isinstance(value, str) and 0 < len(value.encode("utf-8")) <= maximum \
        and all(ord(c) >= 32 and ord(c) != 127 or multiline and c in "\n\t" for c in value)


def _email(value) -> bool:
    if not isinstance(value, str) or not _EMAIL.fullmatch(value):
        return False
    local, domain = value.rsplit("@", 1)
    return not local.startswith(".") and not local.endswith(".") and ".." not in local \
        and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", x) for x in domain.split("."))


def _sender(value) -> bool:
    if not _string(value, 512):
        return False
    if _email(value):
        return True
    match = re.fullmatch(r"([^<>]{1,128}) <([^<>]+)>", value)
    return bool(match and match[1].strip() == match[1] and _email(match[2]))


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("invalid JSON")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("invalid JSON")


def _json(raw):
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)


def _validate(transport: ResendReceiptTransport, request, now: datetime) -> bool:
    if type(request) is not FrozenResendRequest or type(transport.profile) is not ResendTransportProfile:
        return False
    profile = transport.profile
    if not all(isinstance(s, str) and _LABEL.fullmatch(s) for s in (profile.profile_id, profile.revision)) \
            or not _sender(profile.sender) or type(transport.deadline_seconds) is not int \
            or not 1 <= transport.deadline_seconds <= 30 or not _string(transport.credential, 4096) \
            or len(transport.credential) < 32 or not transport.credential.isascii() \
            or any(c.isspace() for c in transport.credential) or "{{" in transport.credential:
        return False
    if request.version != "resend-receipt-v1" or request.provider != "resend" \
            or request.profile_id != profile.profile_id or request.profile_revision != profile.revision \
            or not isinstance(request.idempotency_key, str) or not _KEY.fullmatch(request.idempotency_key) \
            or type(request.body) is not bytes or not 0 < len(request.body) <= MAX_BODY \
            or request.body_sha256 != hashlib.sha256(request.body).hexdigest() or not _window(request, now):
        return False
    body = _json(request.body)
    if not isinstance(body, dict) or set(body) != {"from", "to", "subject", "text"} \
            or body["from"] != profile.sender or type(body["to"]) is not list or len(body["to"]) != 1 \
            or not _email(body["to"][0]) or not _string(body["subject"], 256) or not _string(body["text"], 32768, True):
        return False
    return request.body == json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _client(timeout):
    return httpx.Client(verify=True, trust_env=False, follow_redirects=False, timeout=timeout)


def _answer(status: int, raw: bytes) -> str:
    data = _json(raw)
    if not isinstance(data, dict):
        return "uncertain"
    if status == 200:
        # Only the documented id is needed. Reject mixed/error/unknown shapes; never retain or emit the id.
        identifier = data.get("id")
        return "accepted" if set(data) == {"id"} and isinstance(identifier, str) \
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier) else "uncertain"
    if set(data) != {"name", "message", "statusCode"} or type(data["statusCode"]) is not int \
            or data["statusCode"] != status or not isinstance(data["name"], str) \
            or not isinstance(data["message"], str):
        return "uncertain"
    return "failed" if (status, data["name"]) in _PERMANENT else "uncertain"


def _attempt(transport: ResendReceiptTransport, request: FrozenResendRequest, now: datetime, remaining) -> str:
    try:
        if not _validate(transport, request, now) or remaining <= 0:
            return "failed"
        # All caller-provided bytes stay frozen. No templating, sender lookup, key generation or retries.
        local_deadline = time.monotonic() + remaining
        with _client(remaining) as client:
            if not _window(request, _utc_now()) or time.monotonic() >= local_deadline:
                return "failed"  # still before HTTP I/O
            with client.stream("POST", ENDPOINT, content=request.body,
                               headers={"Authorization": "Bearer " + transport.credential,
                                        "Idempotency-Key": request.idempotency_key,
                                        "Content-Type": "application/json"}) as reply:
                if reply.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json" \
                        or reply.headers.get("Content-Encoding", "identity") != "identity":
                    return "uncertain"
                raw = bytearray()
                for chunk in reply.iter_raw(chunk_size=4096):
                    if len(raw) + len(chunk) > MAX_RESPONSE:
                        return "uncertain"
                    raw.extend(chunk)
                return _answer(reply.status_code, bytes(raw))
    except Exception:  # noqa: BLE001 - provider/private exception text never crosses this boundary
        return "uncertain"


def _worker_command() -> list[str]:
    return [sys.executable, "-I", "-B", os.path.abspath(__file__), "--worker"]


def _exchange(payload: bytes, deadline) -> bytes:
    """Bound stdin/stdout while timing the entire owned child, including DNS and trickle reads."""
    child = None
    try:
        child = subprocess.Popen(_worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, env={"PATH": os.defpath}, close_fds=True)
        with selectors.DefaultSelector() as selector:
            os.set_blocking(child.stdin.fileno(), False)
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdin, selectors.EVENT_WRITE)
            selector.register(child.stdout, selectors.EVENT_READ)
            sent, output = 0, bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                for key, _ in selector.select(remaining):
                    if key.fileobj is child.stdin:
                        sent += os.write(child.stdin.fileno(), payload[sent:sent + 16384])
                        if sent == len(payload):
                            selector.unregister(child.stdin)
                            child.stdin.close()
                    else:
                        chunk = os.read(child.stdout.fileno(), MAX_OUTPUT + 1 - len(output))
                        if not chunk:
                            child.wait(timeout=max(0, deadline - time.monotonic()))
                            if child.returncode != 0 or time.monotonic() >= deadline:
                                raise ValueError("worker")
                            return bytes(output)
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT:
                            raise ValueError("worker output")
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.wait()
            for pipe in (child.stdin, child.stdout):
                if pipe is not None:
                    pipe.close()


@dataclass(frozen=True, repr=False)
class ResendReceiptTransport:
    profile: ResendTransportProfile
    credential: str
    deadline_seconds: int = 10

    def send_frozen(self, request: FrozenResendRequest, now: datetime) -> ReceiptDeliveryOutcome:
        from ports import ReceiptDeliveryOutcome
        try:
            if not _validate(self, request, now) or not _window(request, _utc_now()):
                return ReceiptDeliveryOutcome("failed")
            payload = json.dumps({"profile_id": self.profile.profile_id, "revision": self.profile.revision,
                "sender": self.profile.sender, "credential": self.credential, "deadline_seconds": self.deadline_seconds,
                "request": {"version": request.version, "provider": request.provider, "profile_id": request.profile_id,
                    "profile_revision": request.profile_revision, "body": request.body.decode(),
                    "body_sha256": request.body_sha256, "idempotency_key": request.idempotency_key,
                    "first_attempt_at": request.first_attempt_at.isoformat(), "retry_until": request.retry_until.isoformat()}}).encode()
            if len(payload) > MAX_INPUT:
                return ReceiptDeliveryOutcome("failed")
            remaining = min(self.deadline_seconds, (request.retry_until - max(now, _utc_now())).total_seconds())
            if remaining <= 0:
                return ReceiptDeliveryOutcome("failed")
        except Exception:  # noqa: BLE001 - invalid private inputs produce only a fixed refusal
            return ReceiptDeliveryOutcome("failed")
        try:
            raw = _exchange(payload, time.monotonic() + remaining)
            result = _json(raw)
            if isinstance(result, dict) and set(result) == {"outcome"} \
                    and result["outcome"] in ("accepted", "failed", "uncertain"):
                return ReceiptDeliveryOutcome(result["outcome"])
        except Exception:  # noqa: BLE001 - launch/deadline/partial transport/output are ambiguous, never retry
            pass
        return ReceiptDeliveryOutcome("uncertain")


def _worker_main() -> None:
    outcome = "uncertain"
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("input")
        data = _json(raw)
        r = data["request"]
        request = FrozenResendRequest(r["version"], r["provider"], r["profile_id"], r["profile_revision"],
                                      r["body"].encode(), r["body_sha256"], r["idempotency_key"],
                                      datetime.fromisoformat(r["first_attempt_at"]), datetime.fromisoformat(r["retry_until"]))
        transport = ResendReceiptTransport(ResendTransportProfile(data["profile_id"], data["revision"], data["sender"]),
                                          data["credential"], data["deadline_seconds"])
        now = _utc_now()
        remaining = min(transport.deadline_seconds, (request.retry_until - now).total_seconds())
        outcome = _attempt(transport, request, now, remaining)
    except Exception:  # noqa: BLE001 - never print malformed inputs or exception text
        pass
    sys.stdout.write(json.dumps({"outcome": outcome}, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        _worker_main()
    else:
        raise SystemExit(2)  # no public CLI or environment activation
