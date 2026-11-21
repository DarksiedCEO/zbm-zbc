"""Dormant one-shot Stripe housekeeping. No Finance volume, scheduler, or provider client is opened here."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

JOB_PATH = "/fin/v1/jobs/stripe-sessions/run"
MAX_RESPONSE = 64 * 1024
MAX_WORKER_OUTPUT = 4096
COUNTERS = ("closed", "failed", "checked", "refused_clients", "activation_unknown", "activation_checked",
            "holds_released", "holds_changed")
PREFIX = "FIN_HOUSEKEEPING_"
RID = re.compile(r"stripe-sessions-[0-9]{8}T[0-9]{2}Z")


@dataclass(frozen=True)
class Settings:
    origin: str = field(repr=False)
    service_token: str = field(repr=False)
    caller_token: str = field(repr=False)
    deadline: int = 60


def _settings(env: dict) -> Settings | None:
    enabled = env.get(PREFIX + "ENABLED", "0")
    if enabled not in ("0", "1"):
        raise ValueError("settings")
    if enabled == "0":
        return None
    origin = env.get(PREFIX + "ORIGIN", "")
    if not isinstance(origin, str) or not origin.isascii() or any(ord(c) <= 32 for c in origin) or "\\" in origin:
        raise ValueError("origin")
    u = urlsplit(origin)
    if u.scheme not in ("http", "https") or u.username is not None or u.password is not None \
            or u.path or u.query or u.fragment or "?" in origin or "#" in origin or not u.hostname:
        raise ValueError("origin")
    if u.port is not None and not 1 <= u.port <= 65535 or u.netloc.endswith(":"):
        raise ValueError("origin")
    host = u.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
        if len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                  for label in host.split(".")):
            raise ValueError("origin") from None
    # Plain HTTP only on an explicit private service DNS name or literal loopback (local owned test/server use).
    if u.scheme == "http" and not ((address is not None and address.is_loopback)
                                   or host.endswith(".railway.internal")):
        raise ValueError("origin")
    tokens = [env.get(PREFIX + name, "") for name in ("SERVICE_TOKEN", "CALLER_TOKEN")]
    if any(not isinstance(t, str) or not 32 <= len(t) <= 4096 or not all(33 <= ord(c) <= 126 for c in t)
           or "{{" in t for t in tokens) or tokens[0] == tokens[1]:
        raise ValueError("tokens")
    raw = env.get(PREFIX + "DEADLINE_SECONDS", "60")
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,3}", raw) or not 1 <= int(raw) <= 120:
        raise ValueError("deadline")
    return Settings(origin, *tokens, int(raw))


def request_id(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware clock required")
    return "stripe-sessions-" + now.astimezone(timezone.utc).strftime("%Y%m%dT%HZ")


def _result(status: str, rid: str | None, attempts: int, counters: dict | None = None) -> dict:
    return {"job": "stripe-sessions", "request_id": rid, "status": status,
            "attempts": attempts, "counters": counters or {}}


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("nonfinite")


def _json(raw: bytes):
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)


def _counters(value) -> bool:
    return isinstance(value, dict) and set(value) == set(COUNTERS) \
        and all(type(n) is int and 0 <= n <= 1_000_000_000 for n in value.values()) \
        and value["closed"] + value["failed"] == value["checked"] <= 25 \
        and value["refused_clients"] <= value["activation_checked"] <= 25 \
        and value["refused_clients"] + value["activation_unknown"] <= value["activation_checked"]


def _completed(raw: bytes, rid: str) -> dict:
    body = _json(raw)
    if not isinstance(body, dict) or set(body) != {"job", "request_id", "day", "summary", "already_ran"} \
            or body["job"] != "stripe-sessions" or body["request_id"] != rid \
            or type(body["already_ran"]) is not bool or not isinstance(body["day"], str) \
            or date.fromisoformat(body["day"]).isoformat() != body["day"] or not _counters(body["summary"]):
        raise ValueError("response")
    counts = body["summary"]
    return _result("degraded" if counts["failed"] or counts["activation_unknown"] else "completed", rid, 1, counts)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(settings: Settings, rid: str) -> dict:
    request = Request(settings.origin + JOB_PATH, method="POST", data=json.dumps({"request_id": rid}).encode(),
                      headers={"Authorization": "Bearer " + settings.service_token,
                               "X-FIN-Caller-Token": settings.caller_token, "Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=settings.deadline) as reply:
            if reply.status != 200 or reply.headers.get_content_type() != "application/json":
                return _result("outcome_unknown", rid, 1)
            raw = reply.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                return _result("outcome_unknown", rid, 1)
            return _completed(raw, rid)
    except HTTPError as exc:
        code = exc.code
        exc.close()
        return _result("refused" if code in (401, 403) else "outcome_unknown", rid, 1)
    except Exception:  # noqa: BLE001 - bounded fixed result, never exception/URL/body output
        return _result("outcome_unknown", rid, 1)


def _worker_main() -> None:
    result = _result("outcome_unknown", None, 1)
    try:
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise ValueError("input")
        payload = _json(raw)
        rid = payload["request_id"]
        if not isinstance(rid, str) or not RID.fullmatch(rid):
            raise ValueError("request")
        settings = _settings(payload["settings"])
        if settings is None:
            raise ValueError("disabled worker")
        result = _request(settings, rid)
    except Exception:  # noqa: BLE001 - stderr and arbitrary exception data never leave the worker
        pass
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")


def _worker_command() -> list[str]:
    return [sys.executable, "-I", "-B", os.path.abspath(__file__), "--worker"]


def run(env: dict, now: datetime) -> dict:
    try:
        settings = _settings(env)
        if settings is None:
            return _result("disabled", None, 0)
        rid = request_id(now)
    except Exception:  # noqa: BLE001 - no configuration/exception value is returned
        return _result("invalid_settings", None, 0)
    safe_input = {PREFIX + "ENABLED": "1", PREFIX + "ORIGIN": settings.origin,
                  PREFIX + "SERVICE_TOKEN": settings.service_token, PREFIX + "CALLER_TOKEN": settings.caller_token,
                  PREFIX + "DEADLINE_SECONDS": str(settings.deadline)}
    payload = json.dumps({"settings": safe_input, "request_id": rid}).encode()
    result = _result("outcome_unknown", rid, 1)
    child = None
    started = time.monotonic()
    try:
        # Fresh minimal environment: no inherited credentials, proxy settings, or Python startup hooks.
        child = subprocess.Popen(_worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, env={"PATH": os.defpath}, close_fds=True)
        remaining = max(0, settings.deadline - (time.monotonic() - started))
        raw, _ = child.communicate(payload, timeout=remaining)
        if time.monotonic() - started > settings.deadline or child.returncode != 0 or len(raw) > MAX_WORKER_OUTPUT:
            return result
        parsed = _json(raw)
        if not isinstance(parsed, dict) or set(parsed) != set(result) or parsed["job"] != "stripe-sessions" \
                or parsed["request_id"] != rid or type(parsed["attempts"]) is not int or parsed["attempts"] != 1:
            return result
        status = parsed["status"]
        if status in ("completed", "degraded") and _counters(parsed["counters"]):
            degraded = bool(parsed["counters"]["failed"] or parsed["counters"]["activation_unknown"])
            if (status == "degraded") == degraded:
                return parsed
        elif status in ("refused", "outcome_unknown") and parsed["counters"] == {}:
            return parsed
        return result
    except Exception:  # noqa: BLE001 - timeout, DNS/read uncertainty, launch/output failure are never retried
        return result
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()  # only the worker owned by this invocation; no process-group or global cleanup
            child.communicate()  # reap and close pipes; worker starts no descendants


def exit_code(result: dict) -> int:
    return 0 if result["status"] in ("disabled", "completed") else 1


def main() -> int:
    if sys.argv[1:] == ["--worker"]:
        _worker_main()
        return 0
    if sys.argv[1:]:
        result = _result("invalid_settings", None, 0)
    else:
        result = run(dict(os.environ), datetime.now(timezone.utc))
    print(json.dumps(result, separators=(",", ":")))
    return exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
