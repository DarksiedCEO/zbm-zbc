"""Dormant complete-payload boundary. All credentials/runtime provider facts are owned fixtures."""
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import resend_receipts as R
from ports import NotBuiltClientMail, ReceiptDeliveryOutcome


def canonical(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def world():
    now = datetime.now(timezone.utc)
    profile = R.ResendTransportProfile("owned-account", "revision-1", "Owned <sender@example.test>")
    body = canonical({"from": profile.sender, "to": ["recipient@example.test"],
                      "subject": "Owned receipt", "text": "Owned receipt\nNo actual mail"})
    request = R.FrozenResendRequest("resend-receipt-v1", "resend", profile.profile_id, profile.revision,
                                  body, hashlib.sha256(body).hexdigest(), secrets.token_hex(24),
                                  now - timedelta(minutes=1), now + timedelta(hours=22))
    transport = R.ResendReceiptTransport(profile, secrets.token_hex(32), 1)
    return transport, request, now


def fake_client(monkeypatch, status=200, body=None, raw=None, headers=None):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, stream=httpx.ByteStream(raw if raw is not None else json.dumps(body or {"id": "owned-message-id"}).encode()),
                              headers=headers or {"Content-Type": "application/json"}, request=request)
    monkeypatch.setattr(R, "_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler),
                                                                 trust_env=False, follow_redirects=False, timeout=timeout))
    return calls


def test_complete_wire_bytes_and_no_retry(monkeypatch):
    t, r, now = world()
    calls = fake_client(monkeypatch)
    for _ in range(2):
        assert R._attempt(t, r, now, 1) == "accepted"
    assert len(calls) == 2
    for call in calls:
        assert call.method == "POST" and str(call.url) == "https://api.resend.com/emails"
        assert call.content == r.body and call.headers["Idempotency-Key"] == r.idempotency_key
        assert call.headers["Authorization"] == "Bearer " + t.credential
    assert r.body == calls[0].content == calls[1].content


@pytest.mark.parametrize("case", ["legacy", "schema", "provider", "profile", "revision", "digest", "future",
    "expired", "long_window", "zero_window", "naive", "offset", "key_header", "key_long", "body_extra",
    "sender_drift", "recipient_many", "subject_header", "text_large", "noncanonical", "mutable_bytes"])
def test_invalid_frozen_input_has_zero_worker(case, monkeypatch):
    t, r, now = world()
    if case == "legacy": r = {"to": "recipient@example.test", "subject": "legacy", "text": "legacy", "idempotency_key": "legacy"}
    if case == "schema": r = replace(r, version="future")
    if case == "provider": r = replace(r, provider="other")
    if case == "profile": r = replace(r, profile_id="different")
    if case == "revision": r = replace(r, profile_revision="revision-2")
    if case == "digest": r = replace(r, body_sha256="0" * 64)
    if case == "future": r = replace(r, first_attempt_at=now + timedelta(seconds=1))
    if case == "expired": r = replace(r, retry_until=now)
    if case == "long_window": r = replace(r, retry_until=r.first_attempt_at + timedelta(hours=23, seconds=1))
    if case == "zero_window": r = replace(r, retry_until=r.first_attempt_at)
    if case == "naive": r = replace(r, first_attempt_at=r.first_attempt_at.replace(tzinfo=None))
    if case == "offset": r = replace(r, retry_until=r.retry_until.astimezone(timezone(timedelta(hours=1))))
    if case == "key_header": r = replace(r, idempotency_key=r.idempotency_key + "\r\nInjected: x")
    if case == "key_long": r = replace(r, idempotency_key="x" * 257)
    if case in ("body_extra", "sender_drift", "recipient_many", "subject_header", "text_large", "noncanonical"):
        body = json.loads(r.body)
        if case == "body_extra": body["html"] = "extra"
        if case == "sender_drift": body["from"] = "other@example.test"
        if case == "recipient_many": body["to"].append("other@example.test")
        if case == "subject_header": body["subject"] += "\r\nother"
        if case == "text_large": body["text"] = "x" * 32769
        raw = json.dumps(body).encode() if case == "noncanonical" else canonical(body)
        r = replace(r, body=raw, body_sha256=hashlib.sha256(raw).hexdigest())
    if case == "mutable_bytes": r = replace(r, body=bytearray(r.body))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("invalid input spawned"))
    assert t.send_frozen(r, now) == ReceiptDeliveryOutcome("failed")


@pytest.mark.parametrize("code,name,outcome", [(400,"validation_error","failed"), (401,"missing_api_key","failed"),
    (403,"restricted_api_key","failed"), (409,"concurrent_idempotent_requests","uncertain"),
    (409,"invalid_idempotent_request","uncertain"), (429,"rate_limit_exceeded","uncertain"),
    (500,"application_error","uncertain"), (503,"service_unavailable","uncertain"),
    (401,"unknown_error","uncertain"), (400,"missing_api_key","uncertain")])
def test_status_name_pair_mapping_no_retry(monkeypatch, code, name, outcome):
    t, r, now = world()
    secret = secrets.token_hex(32)
    calls = fake_client(monkeypatch, code, {"name": name, "message": secret, "statusCode": code})
    assert R._attempt(t, r, now, 1) == outcome and len(calls) == 1


@pytest.mark.parametrize("raw", [b'{}', b'[]', b'{"id":true}', b'{"id":""}', b'{"id":"a","name":"error"}',
    b'{"id":"a","id":"b"}', b'{"id":NaN}', b'{broken', b'{"id":"line\\nfeed"}',
    b'{"id":"' + b'x'*129 + b'"}', b'x'*(64*1024+1)])
def test_malformed_success_uncertain(monkeypatch, raw):
    t, r, now = world()
    calls = fake_client(monkeypatch, raw=raw)
    assert R._attempt(t, r, now, 1) == "uncertain" and len(calls) == 1


def test_redirect_never_followed(monkeypatch):
    t, r, now = world()
    calls = fake_client(monkeypatch,302,headers={"Content-Type":"application/json","Location":"https://owned-sink.example.test"})
    assert R._attempt(t, r, now, 1) == "uncertain" and len(calls) == 1


def test_private_reprs_and_defaults(hr):
    t, r, _ = world()
    for value in (t.credential, r.idempotency_key, "recipient@example.test", "sender@example.test", "Owned receipt"):
        assert value not in repr((t, t.profile, r))
    assert isinstance(hr.svc.ports.client_mail, type(hr.f["client_mail"]))
    assert NotBuiltClientMail.enabled is False
    assert not hasattr(t, "deliver_receipt") and not hasattr(t, "enabled")


def worker_command(code):
    return [sys.executable, "-I", "-B", "-c",
            "import sys;sys.path.insert(0," + repr(os.path.dirname(R.__file__)) + ");import resend_receipts as m;" + code]


def test_real_worker_mock_acceptance_and_frozen_reconstruction(monkeypatch):
    t, r, now = world()
    code = ("import httpx; m._client=lambda timeout:httpx.Client(transport=httpx.MockTransport("
            "lambda q:httpx.Response(200,stream=httpx.ByteStream(b'{\"id\":\"owned-message-id\"}'),headers={'Content-Type':'application/json'},request=q)),trust_env=False,"
            "follow_redirects=False,timeout=timeout);m._worker_main()")
    monkeypatch.setattr(R,"_worker_command",lambda:worker_command(code))
    assert t.send_frozen(r, now).outcome == "accepted"
    fresh = R.ResendReceiptTransport(t.profile,t.credential,1)
    assert fresh.send_frozen(replace(r),now).outcome == "accepted"


@pytest.mark.parametrize("code", ["import time;time.sleep(10)", "import os;os.write(1,b'x'*8192);import time;time.sleep(10)",
                                   "print('{}')", "raise SystemExit(2)"])
def test_worker_failure_or_overflow_uncertain_and_reaped(monkeypatch, code):
    t,r,now=world();children=[];original=subprocess.Popen
    def capture(*a,**kw):
        p=original(*a,**kw);children.append(p);return p
    monkeypatch.setattr(subprocess,"Popen",capture)
    monkeypatch.setattr(R,"_worker_command",lambda:worker_command(code))
    assert t.send_frozen(r,now).outcome == "uncertain"
    assert len(children)==1 and children[0].poll() is not None
    with pytest.raises(ProcessLookupError):os.kill(children[0].pid,0)


@pytest.fixture
def local_server():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    state = {"mode": "ok", "calls": [], "accepted": 0, "release": threading.Event(), "stage": None}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["calls"].append((self.path, body, dict(self.headers)))
            if state["mode"] == "headers":
                state["stage"] = "headers"
                state["release"].wait(10)
            if state["mode"] == "lost":
                state["accepted"] += 1
                self.connection.close()
                return
            try:
                self.send_response(302 if state["mode"] == "redirect" else 200)
                self.send_header("Content-Type","application/json")
                if state["mode"] == "redirect": self.send_header("Location",state["origin"]+"/sink")
                self.end_headers()
                if state["mode"] == "trickle":
                    state["stage"] = "trickle"
                    for _ in range(100):
                        self.wfile.write(b' ');self.wfile.flush()
                        if state["release"].wait(0.1): break
                else:
                    self.wfile.write(b'{"id":"owned-message-id"}')
            except (BrokenPipeError,ConnectionResetError): pass
    class OwnedServer(ThreadingHTTPServer):
        daemon_threads = False
    server=OwnedServer(("127.0.0.1",0),Handler)
    state["origin"]=f"http://127.0.0.1:{server.server_port}"
    thread=threading.Thread(target=server.serve_forever);thread.start()
    yield state
    state["release"].set()
    server.shutdown();server.server_close();thread.join(5)
    assert not thread.is_alive()


def local_worker(monkeypatch, server):
    # Test-only monkeypatch of the private worker module, never a public/configurable endpoint override.
    monkeypatch.setattr(R,"_worker_command",lambda:worker_command("m.ENDPOINT="+repr(server["origin"]+"/emails")+";m._worker_main()"))


def test_owned_http_wire_proxy_redirect_and_uncertain_acceptance(monkeypatch,local_server):
    t,r,now=world();local_worker(monkeypatch,local_server)
    monkeypatch.setenv("HTTPS_PROXY","http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY","http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY","http://127.0.0.1:1")
    assert t.send_frozen(r,now).outcome == "accepted"
    assert local_server["calls"][0][0]=="/emails" and local_server["calls"][0][1]==r.body
    assert local_server["calls"][0][2]["Idempotency-Key"]==r.idempotency_key
    local_server["mode"]="redirect"
    assert t.send_frozen(r,now).outcome == "uncertain"
    local_server["mode"]="lost"
    assert t.send_frozen(r,now).outcome == "uncertain"
    assert len(local_server["calls"])==3 and local_server["accepted"]==1
    assert all(path=="/emails" for path,_,_ in local_server["calls"])


@pytest.mark.parametrize("mode",["dns","headers","trickle"])
def test_total_deadline_covers_dns_headers_and_trickle(monkeypatch,local_server,mode,tmp_path):
    t,r,now=world();children=[];original=subprocess.Popen
    def capture(*a,**kw):
        p=original(*a,**kw);children.append(p);return p
    monkeypatch.setattr(subprocess,"Popen",capture)
    if mode=="dns":
        marker = tmp_path / "dns-entered"
        code=("import socket,time;from pathlib import Path;socket.getaddrinfo=lambda *a,**k:(Path("
              +repr(str(marker))+").write_text('entered'),time.sleep(10))[1];m._worker_main()")
        monkeypatch.setattr(R,"_worker_command",lambda:worker_command(code))
    else:
        local_server["mode"]=mode;local_worker(monkeypatch,local_server)
    start=time.monotonic()
    assert t.send_frozen(r,now).outcome=="uncertain"
    assert time.monotonic()-start < 3
    assert len(children)==1 and children[0].poll() is not None
    with pytest.raises(ProcessLookupError):os.kill(children[0].pid,0)
    assert len(local_server["calls"])==(0 if mode=="dns" else 1)
    if mode == "dns": assert marker.read_text() == "entered"
    else: assert local_server["stage"] == mode


def test_worker_rechecks_cutoff_before_any_client(monkeypatch):
    t,r,now=world()
    code=("from datetime import datetime,timezone; m._utc_now=lambda:datetime.fromisoformat("+repr(r.retry_until.isoformat())+");"
          "m._client=lambda *a:(_ for _ in ()).throw(AssertionError('must not open'));m._worker_main()")
    monkeypatch.setattr(R,"_worker_command",lambda:worker_command(code))
    assert t.send_frozen(r,now).outcome=="failed"


def test_client_setup_cannot_cross_original_cutoff(monkeypatch):
    t,r,now=world();calls=[]
    class Delayed:
        def __enter__(self):
            monkeypatch.setattr(R,"_utc_now",lambda:r.retry_until)
            return self
        def __exit__(self,*args): pass
        def stream(self,*args,**kwargs):
            calls.append(args)
            pytest.fail("cutoff crossed")
    monkeypatch.setattr(R,"_client",lambda _:Delayed())
    assert R._attempt(t,r,now,1)=="failed" and not calls


def test_actual_wall_cutoff_and_remaining_budget(monkeypatch):
    t,r,now=world()
    monkeypatch.setattr(R,"_utc_now",lambda:r.retry_until)
    monkeypatch.setattr(subprocess,"Popen",lambda *a,**kw:pytest.fail("expired spawned"))
    assert t.send_frozen(r,now).outcome=="failed"


def test_exact_23hour_window_and_client_security_options(monkeypatch):
    t,r,now=world();r=replace(r,retry_until=r.first_attempt_at+timedelta(hours=23))
    assert R._validate(t,r,now)
    options=[]
    monkeypatch.setattr(httpx,"Client",lambda **kwargs:options.append(kwargs))
    R._client(1)
    assert options==[{"verify":True,"trust_env":False,"follow_redirects":False,"timeout":1}]


def test_production_factory_disabled_even_with_unrelated_env(hr,monkeypatch):
    import api
    monkeypatch.setenv("RESEND_API_KEY",secrets.token_hex(32))
    monkeypatch.setenv("RESEND_ENABLED","1")
    ports=api.build_ports(hr.settings)
    assert type(ports.client_mail) is NotBuiltClientMail and ports.client_mail.enabled is False
