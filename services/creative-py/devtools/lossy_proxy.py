"""DEV/TEST-ONLY lossy HTTP proxy (copied from the integration run 3 harness).

Forwards to UPSTREAM; for matching requests it forwards, lets the upstream COMMIT,
then drops the response (closes the socket). Binds 127.0.0.1.
Control: GET /__ctl?drop=N&match=/ledger/events&fail=N ; GET /__stats

    python3 devtools/lossy_proxy.py <port> <upstream-url>
"""
import http.server, socketserver, sys, json, threading, urllib.parse
import httpx
PORT, UPSTREAM = int(sys.argv[1]), sys.argv[2]
state = {"fail": 0, "skip": 0, "failed": [], "drop": 0, "match": "/ledger/events", "dropped": [], "forwarded": 0}
lock = threading.Lock()
cli = httpx.Client(base_url=UPSTREAM, timeout=30)
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _do(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/__ctl":
            q = urllib.parse.parse_qs(u.query)
            with lock:
                if "drop" in q: state["drop"] = int(q["drop"][0])
                if "match" in q: state["match"] = q["match"][0]
                if "fail" in q: state["fail"] = int(q["fail"][0])
                if "skip" in q: state["skip"] = int(q["skip"][0])
            return self._send(200, json.dumps(state).encode())
        if u.path == "/__stats":
            return self._send(200, json.dumps(state).encode())
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        with lock:
            m = self.command == "POST" and u.path.startswith(state["match"])
            if m and state["skip"] > 0:
                state["skip"] -= 1; m = False
            f = m and state["fail"] > 0
            if f:
                state["fail"] -= 1; state["failed"].append(body.decode()[:400])
        if f:
            return self._send(503, b'{"error":"injected: ledger unavailable"}')
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "connection")}
        r = cli.request(self.command, self.path, content=body, headers=hdrs)
        with lock:
            state["forwarded"] += 1
            drop = self.command == "POST" and u.path.startswith(state["match"]) and state["drop"] > 0
            if drop:
                state["drop"] -= 1
                state["dropped"].append({"path": u.path, "upstream_status": r.status_code, "body": body.decode()[:300]})
        if drop:
            self.close_connection = True
            try: self.connection.shutdown(2)
            except Exception: pass
            return
        self._send(r.status_code, r.content, r.headers.get("content-type", "application/json"))
    def _send(self, code, data, ct="application/json"):
        self.send_response(code); self.send_header("content-type", ct); self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)
    do_GET = do_POST = do_PUT = do_DELETE = _do
class TS(socketserver.ThreadingMixIn, http.server.HTTPServer): daemon_threads = True
TS(("127.0.0.1", PORT), H).serve_forever()
