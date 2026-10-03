//! Fix wave 4 (Sep 24 2026), AEGIS finding "one slow connection freezes the
//! whole ledger" (HIGH, no auth needed). Real compiled binary, real TCP
//! sockets, real files — same raw-HTTP harness style as the other
//! integration test files.
//!
//! Before the fix, the server handled requests one at a time on its main
//! thread with no socket deadlines. A client that sent `Content-Length: 5000`
//! and a partial body with a WRONG token stalled every other client: the 401
//! was written, then tiny_http's `Request::respond` dropped the request and
//! `EqualReader::drop` blocked reading the 4988 unsent body bytes (gdb stack in
//! ADR 0003 section 7). The same held for a right-token slow body
//! (`read_body`), a client that never reads a large response (the response
//! write), and an absurd `Content-Length` (read up to 64 KiB before the 413).
//!
//! Every test here asserts that `/health` AND a normal authenticated append
//! are served while the bad client(s) are stalled, and that the server itself
//! cuts the stalled connection by its documented deadline. The concurrency
//! tests pin that appends stay strictly serialized.
//!
//! Fix wave 25 (scout C2-4/C2-5): no fixed sleep is a barrier and no latency
//! bound is asserted on a loaded box.
//!   - Barrier: before "others" are tried, the bad connections are CONFIRMED
//!     to be held by the server: the server runs with a small connection cap,
//!     holders fill the remaining slots, and `/health` must then be shed (503)
//!     — which it can only be if every bad connection holds a slot — after
//!     which the holders are dropped and `/health` must answer 200 again. A
//!     slow reader is confirmed by the bytes the server has already queued to
//!     it. A wrong-token client is confirmed by its 401 having arrived.
//!   - "Served while stalled" is an ordering, not a stopwatch: others get
//!     their answers, and only THEN is each bad connection checked to be still
//!     held (nothing came back on it yet). A server that serialised behind the
//!     stalled client could only answer the others after cutting it.
//!   - Deadlines: the cut is an event (the read ends because the server
//!     closed); lower bounds (no earlier than the deadline) are kept, they
//!     cannot be broken by load; an upper bound remains only where it
//!     separates two causes (a wrong-token 401 closed BEFORE the body deadline
//!     could have fired), and it is the server's own deadline, not a literal.

mod common;

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use common::PortFile;
use serde_json::{json, Value};

const TOKEN: &str = "slow-clients-test-token";
/// Server deadlines (src/bin/server.rs, ADR 0003 section 7).
const HEADER_READ_TIMEOUT: Duration = Duration::from_secs(5);
const BODY_READ_TIMEOUT: Duration = Duration::from_secs(5);
const REQUEST_DEADLINE: Duration = Duration::from_secs(15);
/// Hang guard on top of a server deadline: a test that waits this long fails
/// as hung; it is not a measurement.
const SLACK: Duration = Duration::from_secs(2);

struct ServerHandle {
    child: Child,
    port: u16,
    _port_file: PortFile,
}

impl Drop for ServerHandle {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

struct Scratch(PathBuf);
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

fn scratch(label: &str) -> Scratch {
    let p = std::env::temp_dir().join(format!("ledger_slow_{label}_{}.jsonl", common::unique_suffix()));
    let _ = std::fs::remove_file(&p);
    Scratch(p)
}

/// Fix wave 21 (N20-M-3): the server binds port 0 and writes the bound port to
/// LEDGER_PORT_FILE; no port is picked here and released for the server to race for.
fn start_with(log: &Scratch, env: &[(&str, &str)]) -> ServerHandle {
    let pf = PortFile::new("slow");
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_server"));
    cmd.env("LEDGER_SERVICE_TOKEN", TOKEN)
        .env("LEDGER_LOG_PATH", log.0.to_str().unwrap())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    common::ephemeral(&mut cmd, &pf);
    for (k, v) in env {
        cmd.env(k, v);
    }
    let child = cmd.spawn().expect("failed to spawn ledger-rust server");
    // Fix wave 25 (scout C2-11): the child is owned by a ServerHandle (kill + wait on drop) BEFORE anything below can
    // panic; a panic in the readiness loop used to leave a bare `Child`, which std does not kill on drop.
    let mut h = ServerHandle { child, port: 0, _port_file: pf };
    h.port = common::wait_port(&mut h.child, &h._port_file, Duration::from_secs(30)).unwrap_or_else(|e| panic!("{e}"));
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        if let Some(st) = h.child.try_wait().unwrap() {
            panic!("server exited before coming up: {st}");
        }
        if let Ok((200, _)) = request(h.port, "GET", "/health", None, None) {
            break;
        }
        assert!(Instant::now() < deadline, "server did not come up");
        std::thread::sleep(Duration::from_millis(50));
    }
    h
}

fn start(log: &Scratch) -> ServerHandle {
    start_with(log, &[])
}

/// One HTTP/1.1 request on a fresh connection, read to its Content-Length
/// (fix wave 21, N20-M-1: never to EOF); the client itself gives up after
/// 15 s so a hung server fails the test instead of hanging it.
fn request(port: u16, method: &str, path: &str, auth: Option<&str>, body: Option<&str>) -> std::io::Result<(u16, String)> {
    let bearer = auth.map(|t| format!("Bearer {t}"));
    let raw = common::raw_request(method, path, bearer.as_deref(), body.unwrap_or(""));
    let resp = common::exchange(port, &raw, Duration::from_secs(15))?;
    Ok((resp.status, resp.text()))
}

fn authed(port: u16, method: &str, path: &str, body: Option<&str>) -> (u16, Value) {
    let (status, text) = request(port, method, path, Some(TOKEN), body).unwrap();
    let v = serde_json::from_str(&text).unwrap_or_else(|e| panic!("non-JSON body ({e}): {text:?}"));
    (status, v)
}

fn event_value(event_id: &str, summary: &str) -> Value {
    json!({
        "event_id": event_id,
        "department": "onboarding",
        "event_type": "compliance_ruling",
        "actor": "intel_15_compliance",
        "subject_id": "client_123",
        "payload_sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "summary": summary
    })
}

fn event(event_id: &str) -> String {
    event_value(event_id, "Activation blocked: 2 requirements unmet").to_string()
}

/// The core assertion of this file: `/health` and an authenticated append are
/// served, and only AFTER both answers is every connection in `stalled`
/// checked to be still held by the server (nothing came back on it). A server
/// that made the others wait behind a stalled client could only answer them
/// after it had cut that client, which this order catches without a clock.
///
/// The barrier tests run the server with a small connection cap, and a slot is released asynchronously after its
/// answer, so a request can meet a slot that is still being released and be SHED (503, at once). A shed is retried
/// (hang guard: the request deadline); it cannot hide the regression, because the stalled connections are checked
/// only after the others were served.
fn assert_others_served_while_stalled(port: u16, label: &str, stalled: &[&TcpStream]) {
    let guard = Instant::now() + REQUEST_DEADLINE;
    let served = |method: &str, path: &str, body: Option<&str>, want: u16| loop {
        let (status, text) = request(port, method, path, if body.is_some() { Some(TOKEN) } else { None }, body).unwrap();
        if status == 503 && Instant::now() < guard {
            std::thread::sleep(Duration::from_millis(20));
            continue;
        }
        assert_eq!(status, want, "{label}: {method} {path}: {text}");
        break;
    };
    served("GET", "/health", None, 200);
    let id = format!("ok-{}", common::unique_suffix());
    served("POST", "/ledger/events", Some(&event(&id)), 201);
    let released = stalled.iter().filter(|c| !still_held(c)).count();
    assert_eq!(released, 0, "{label}: {released} stalled connection(s) had already been answered or cut when the others were served");
}

/// Barrier (fix wave 25, scout C2-5): every connection in `bad` holds one of the server's `cap` slots. Holders fill
/// the remaining `cap - bad.len()` slots (each confirmed held), `/health` must then be shed with 503 — only possible
/// if the bad connections hold the rest — and once the holders are dropped `/health` answers 200 again (polled: slot
/// release is asynchronous). Panics if the barrier cannot be established.
fn confirm_held(port: u16, cap: usize, bad: &[&TcpStream]) {
    assert!(bad.iter().all(|c| still_held(c)), "a bad connection was answered before the barrier");
    let mut holders: Vec<TcpStream> = Vec::new();
    let guard = Instant::now() + REQUEST_DEADLINE; // hang guard
    while holders.len() < cap - bad.len() {
        let h = slow_body_client(port, TOKEN, "/ledger/events");
        holders.push(h);
        holders.retain(still_held_after_accept);
        assert!(Instant::now() < guard, "holders kept being shed: the bad connections hold more slots than expected?");
    }
    let (status, body) = request(port, "GET", "/health", None, None).unwrap();
    assert_eq!(status, 503, "barrier: with {} holders and {} bad connections /health was not shed: {body}", holders.len(), bad.len());
    drop(holders);
    loop {
        if let Ok((200, _)) = request(port, "GET", "/health", None, None) {
            break;
        }
        assert!(Instant::now() < guard, "the holders' slots were never released");
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(bad.iter().all(|c| still_held(c)), "a bad connection was answered during the barrier");
}

/// A fresh holder counts once the server has had the chance to shed it: a shed
/// holder receives a 503 (and EOF) at once, a held one receives nothing. The
/// /health round trip after it orders the check behind the server's accept.
fn still_held_after_accept(h: &TcpStream) -> bool {
    let port = h.peer_addr().unwrap().port();
    let _ = request(port, "GET", "/health", None, None); // a round trip through the accept loop
    still_held(h)
}

fn connect(port: u16) -> TcpStream {
    let s = TcpStream::connect(("127.0.0.1", port)).unwrap();
    s.set_read_timeout(Some(REQUEST_DEADLINE + SLACK + SLACK)).unwrap();
    s
}

/// Opens a connection and sends a request head declaring a 5000-byte body,
/// plus the first 12 bytes of it — exactly the AEGIS `slow.py` probe.
fn slow_body_client(port: u16, token: &str, path: &str) -> TcpStream {
    let mut s = connect(port);
    let head = format!(
        "POST {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\n\
         Content-Type: application/json\r\nContent-Length: 5000\r\n\r\n{{\"event_id\":"
    );
    s.write_all(head.as_bytes()).unwrap();
    s
}

/// Reads until EOF (or error) and returns (bytes, time from `since`).
fn read_until_closed(s: &mut TcpStream, since: Instant) -> (String, Duration) {
    let mut out = Vec::new();
    let mut buf = [0u8; 8192];
    loop {
        match s.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => out.extend_from_slice(&buf[..n]),
        }
    }
    (String::from_utf8_lossy(&out).to_string(), since.elapsed())
}

fn status_of(resp: &str) -> u16 {
    resp.lines().next().and_then(|l| l.split_whitespace().nth(1)).and_then(|s| s.parse().ok()).unwrap_or(0)
}

// --- the AEGIS reproduction: slow body with a WRONG token ---------------------------------

#[test]
fn slow_body_with_wrong_token_does_not_block_others_and_gets_prompt_401() {
    let log = scratch("wrongtok");
    let s = start(&log);
    let since = Instant::now();
    let mut bad = slow_body_client(s.port, "WRONG", "/ledger/events");
    // Barrier: the 401 has arrived, so the server has read this request's head and answered it.
    let first = common::read_response(&mut bad).expect("no answer to the wrong-token head");
    assert_eq!(first.status, 401, "stalled wrong-token client");

    // The stalled client is closed WITHOUT the server waiting for (draining) the 4988 missing body bytes: EOF arrives
    // before the body deadline could have fired (that deadline is what would close it otherwise). Read at once, right
    // after the 401 (fix wave 25, E-C review): the E0 version read it only after serving the others, so the others'
    // own service time counted against this bound.
    let (rest, elapsed) = read_until_closed(&mut bad, since);
    assert_eq!(rest, "", "bytes after the 401");
    assert!(elapsed < BODY_READ_TIMEOUT, "401 + close took {elapsed:?}: the body deadline closed it, the server waited for the body");

    assert_others_served_while_stalled(s.port, "after a wrong-token slow body", &[]);
}

// --- slow body with the RIGHT token ------------------------------------------------------------

#[test]
fn slow_body_with_right_token_does_not_block_others_and_is_cut_off() {
    let log = scratch("righttok");
    let s = start_capped(&log, CAP);
    let since = Instant::now();
    let mut bad = slow_body_client(s.port, TOKEN, "/ledger/events");
    confirm_held(s.port, CAP, &[&bad]);

    assert_others_served_while_stalled(s.port, "right-token slow body", &[&bad]);

    // The server gives up on the body at BODY_READ_TIMEOUT: 408 (the deadline's own answer), closed, nothing
    // recorded from it. Lower bound only: load can make the cut later, never earlier.
    let (resp, elapsed) = read_until_closed(&mut bad, since);
    assert_eq!(status_of(&resp), 408, "stalled right-token client: {resp:?}");
    assert!(elapsed >= BODY_READ_TIMEOUT - Duration::from_millis(500), "cut off too early: {elapsed:?}");
    let (_, entries) = authed(s.port, "GET", "/ledger/entries", None);
    assert_eq!(entries.as_array().unwrap().len(), 1, "only the prompt append was recorded");
}

/// A slow-body trickle (one byte every 500 ms — never idle long enough for a
/// per-read timeout) is still cut at BODY_READ_TIMEOUT: the deadline is total,
/// not per read.
#[test]
fn trickled_body_is_cut_off_at_the_total_body_deadline() {
    let log = scratch("trickle");
    let s = start_capped(&log, CAP);
    let mut bad = slow_body_client(s.port, TOKEN, "/ledger/append");
    confirm_held(s.port, CAP, &[&bad]);
    let mut writer = bad.try_clone().unwrap();
    // 40 bytes, one every 500 ms: 20 s of trickle, never idle for a per-read timeout. The thread reports whether it
    // was still trickling when the server cut the connection.
    let trickle = std::thread::spawn(move || {
        for _ in 0..40 {
            std::thread::sleep(Duration::from_millis(500));
            if writer.write_all(b" ").is_err() {
                return true; // the server closed while the body was still arriving
            }
        }
        false
    });
    assert_others_served_while_stalled(s.port, "trickled body", &[&bad]);
    let (resp, _) = read_until_closed(&mut bad, Instant::now());
    assert_eq!(status_of(&resp), 408, "{resp:?}");
    // The cut came while the client was still sending: an ordering, no stopwatch. A per-read deadline would never
    // fire (the trickle is never idle) and the connection would outlive the trickle.
    // (After the 408 and the server's bounded drain, the next byte is answered with RST and the one after fails:
    // the trickle sees its error within ~2 s of the cut, long before its 20 s are over.)
    drop(bad);
    assert!(trickle.join().unwrap(), "the trickle finished before the server cut the connection");
}

// --- slow header ---------------------------------------------------------------------------------

#[test]
fn slow_header_does_not_block_others_and_is_cut_off() {
    let log = scratch("slowhead");
    let s = start_capped(&log, CAP);
    let since = Instant::now();
    let mut bad = connect(s.port);
    bad.write_all(b"POST /ledger/events HTTP/1.1\r\nHost: x\r\nAuthor").unwrap();
    confirm_held(s.port, CAP, &[&bad]);

    assert_others_served_while_stalled(s.port, "slow header", &[&bad]);

    // Cut by the server (the read ends; its timeout is the hang guard), no earlier than the header deadline.
    let (_, elapsed) = read_until_closed(&mut bad, since);
    assert!(elapsed >= HEADER_READ_TIMEOUT - Duration::from_millis(500), "cut off too early: {elapsed:?}");
}

/// A connection that sends nothing at all is closed by the header deadline too.
#[test]
fn idle_connection_is_closed_by_the_header_deadline() {
    let log = scratch("idle");
    let s = start_capped(&log, CAP);
    let since = Instant::now();
    let mut bad = connect(s.port);
    confirm_held(s.port, CAP, &[&bad]);
    assert_others_served_while_stalled(s.port, "idle connection", &[&bad]);
    let (got, elapsed) = read_until_closed(&mut bad, since);
    assert_eq!(got, "", "an idle connection is closed, not answered");
    assert!(elapsed >= HEADER_READ_TIMEOUT - Duration::from_millis(500), "cut off too early: {elapsed:?}");
}

// --- oversized Content-Length ------------------------------------------------------------------

/// A declared body over 64 KiB is refused from the header alone: 413 at once,
/// no body byte sent, nothing read.
#[test]
fn oversized_content_length_is_413_before_any_body_is_read() {
    let log = scratch("bigcl");
    let s = start(&log);
    for path in ["/ledger/events", "/ledger/append"] {
        let mut c = connect(s.port);
        let head = format!(
            "POST {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n\
             Content-Type: application/json\r\nContent-Length: 10000000\r\n\r\n"
        );
        c.write_all(head.as_bytes()).unwrap();
        // No body byte is ever sent: an answer at all proves it came from the header alone (a server waiting for the
        // body could only answer at the body deadline, with 408).
        let (resp, _) = read_until_closed(&mut c, Instant::now());
        assert_eq!(status_of(&resp), 413, "{path}: {resp:?}");
    }
    // Exactly at the limit is still read (and then judged on content).
    let at_limit = format!("{{\"pad\":\"{}\"}}", "a".repeat(64 * 1024 - 10));
    assert_eq!(at_limit.len(), 64 * 1024);
    let (status, v) = authed(s.port, "POST", "/ledger/events", Some(&at_limit));
    assert_eq!(status, 400, "{v}");
}

// --- slow reader ---------------------------------------------------------------------------------

/// Writes a valid N-entry log straight to disk (built with the library's own
/// hash chain) so the test does not pay N fsyncs.
fn write_big_log(log: &Scratch, n: usize) {
    let mut ledger = ledger_rust::Ledger::new();
    let mut out = String::new();
    for i in 0..n {
        let input: ledger_rust::EventInput =
            serde_json::from_value(event_value(&format!("big-{i}"), &"x".repeat(200))).unwrap();
        let entry = ledger.append_event(input);
        out.push_str(&serde_json::to_string(entry).unwrap());
        out.push('\n');
    }
    std::fs::write(&log.0, out).unwrap();
}

/// A client asks for the whole (multi-MB) ledger and never reads the
/// response. Its write must not hold up anyone else, and the server drops
/// it at the request deadline.
#[test]
fn slow_reader_does_not_block_others_and_is_cut_off() {
    let log = scratch("slowreader");
    write_big_log(&log, 30000);
    let s = start(&log);
    let since = Instant::now();
    let mut bad = connect(s.port);
    bad.write_all(format!("GET /ledger/entries HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n\r\n").as_bytes())
        .unwrap();
    // Barrier (fix wave 25): the server is writing this response — at least 64 KiB of it already sit unread in this
    // socket's receive queue (the old barrier was a 1.5 s sleep). The response is multi-MB, so it cannot be done.
    queued_at_least(&bad, 64 * 1024);

    assert_others_served_while_stalled(s.port, "slow reader", &[]);
    // Ordering: the others were served while this response was still being written. The only thing that cuts the
    // slow reader is the request deadline, which starts after `since` (the server cannot have read the request
    // before it was sent), so others answered before `since + REQUEST_DEADLINE` were answered before the cut. A
    // server that serialised them behind the slow reader answers them only after the cut. (Fix wave 25, E-C review:
    // the E0 check drained the socket until it would block, which could consume the whole multi-MB response — and
    // the truncation assertion below with it — whenever the server kept up with the reader. This bound is the
    // server's own deadline, not a literal; reviewed allowlist entry in devtools/hygiene_allowlist.json.)
    let elapsed = since.elapsed();
    assert!(elapsed < REQUEST_DEADLINE, "others were answered only after {elapsed:?}: after the slow reader's cut");

    // Wait out the request deadline, then read: the server must have given up
    // (connection closed before the full multi-MB body was delivered).
    std::thread::sleep((REQUEST_DEADLINE + SLACK).saturating_sub(since.elapsed()));
    let (resp, _) = read_until_closed(&mut bad, since);
    assert_eq!(status_of(&resp), 200);
    let body_len = resp.split("\r\n\r\n").nth(1).unwrap_or("").len();
    let (_, entries) = request(s.port, "GET", "/ledger/entries", Some(TOKEN), None).unwrap();
    assert!(
        body_len < entries.len(),
        "the never-reading client got the whole {} byte body; the server did not enforce the write deadline",
        entries.len()
    );
}

// --- many slow clients at once -------------------------------------------------------------------

#[test]
fn many_concurrent_slow_clients_do_not_block_others() {
    let log = scratch("many");
    let s = start_capped(&log, 150 + 2);
    let mut bad = Vec::new();
    for i in 0..150 {
        let c = match i % 3 {
            0 => slow_body_client(s.port, "WRONG", "/ledger/events"),
            1 => slow_body_client(s.port, TOKEN, "/ledger/append"),
            _ => {
                let mut c = connect(s.port);
                c.write_all(b"GET /health HTTP/1.1\r\nHo").unwrap();
                c
            }
        };
        bad.push(c);
    }
    // The wrong-token third is answered 401 at once (that is the fix) and closed: only the right-token slow bodies
    // and the slow heads are held.
    let refs: Vec<&TcpStream> = bad.iter().enumerate().filter(|(i, _)| i % 3 != 0).map(|(_, c)| c).collect();
    confirm_held(s.port, 150 + 2, &refs);
    for round in 0..3 {
        assert_others_served_while_stalled(s.port, &format!("150 slow clients, round {round}"), &refs);
    }
    drop(bad);
}

/// Whether a slow-body holder still occupies a connection slot: nothing has
/// come back on it (a shed holder got a 503 or was closed). Non-blocking.
fn still_held(h: &TcpStream) -> bool {
    h.set_nonblocking(true).unwrap();
    let mut b = [0u8; 64];
    let held = matches!(h.peek(&mut b), Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock);
    h.set_nonblocking(false).unwrap();
    held
}

/// Past the connection cap the server sheds load with an immediate 503
/// instead of queueing the caller behind stalled connections, and it
/// recovers as soon as the stalled connections hit their deadline.
///
/// Fix wave 21 (AEGIS N20-M-2): the holders used to be opened right after the
/// readiness probe, whose connection slot is released asynchronously after
/// its response — under CPU contention one holder was itself shed (2/20), so
/// only 3 slots were held and the /health probe got a slot (200). Every
/// holder is now confirmed held (nothing came back on it) before the
/// assertion, and a shed holder is replaced. The first 503 is asserted
/// exactly as before.
#[test]
fn over_connection_cap_gets_prompt_503_and_recovers() {
    let log = scratch("cap");
    let s = start_with(&log, &[("LEDGER_MAX_CONNECTIONS", "4")]);
    let mut bad: Vec<TcpStream> = (0..4).map(|_| slow_body_client(s.port, TOKEN, "/ledger/events")).collect();
    let mut replaced = 0;
    let confirm_by = Instant::now() + Duration::from_secs(3);
    loop {
        std::thread::sleep(Duration::from_millis(300));
        let shed: Vec<usize> = (0..bad.len()).filter(|&i| !still_held(&bad[i])).collect();
        if shed.is_empty() {
            break;
        }
        assert!(Instant::now() < confirm_by, "holders kept being shed after {replaced} replacements");
        for i in shed {
            bad[i] = slow_body_client(s.port, TOKEN, "/ledger/events");
            replaced += 1;
        }
    }
    // Shed, not queued: an answer at all while every slot is held can only be the shed path's (a queued request
    // would be answered only after a holder's 5 s body deadline, and with 200).
    let (status, body) = request(s.port, "GET", "/health", None, None).unwrap();
    assert_eq!(status, 503, "{body} (holders replaced: {replaced})");
    // Recovery: once the holders hit their body deadline the server answers 408 and frees their slots — wait for
    // those 408s (the event), then the others are served again.
    for h in bad.iter_mut() {
        let (resp, _) = read_until_closed(h, Instant::now());
        assert_eq!(status_of(&resp), 408, "a holder was not cut by its body deadline: {resp:?}");
    }
    let guard = Instant::now() + REQUEST_DEADLINE; // hang guard: slot release after the 408 is asynchronous
    while !matches!(request(s.port, "GET", "/health", None, None), Ok((200, _))) {
        assert!(Instant::now() < guard, "the server never recovered after the stalled connections were cut");
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_others_served_while_stalled(s.port, "after the stalled connections were cut", &[]);
    drop(bad);
}

// --- graceful close (fix wave 21, AEGIS N20-M-1) -------------------------------------------------

/// After reading a whole response (to its Content-Length) and then EOF, the
/// client socket must carry no error: the server closed gracefully instead of
/// resetting a connection it had not read to the end. Returns (status, what
/// the read after the response saw, SO_ERROR).
fn close_outcome(port: u16, raw: &[u8]) -> (u16, String, Option<std::io::Error>) {
    let mut c = TcpStream::connect(("127.0.0.1", port)).unwrap();
    c.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    // the request is written whole even when the server answers after the head
    // (413): the server must keep reading it, or the kernel resets the connection
    let _ = c.write_all(raw);
    let resp = common::read_response(&mut c).unwrap();
    let mut rest = [0u8; 256];
    let after = match c.read(&mut rest) {
        Ok(0) => "eof".to_string(),
        Ok(n) => format!("{n} extra bytes"),
        Err(e) => format!("read error: {e}"),
    };
    std::thread::sleep(Duration::from_millis(200)); // a RST that follows the FIN lands here
    (resp.status, after, c.take_error().unwrap())
}

/// Linux reports a RST that arrives after the FIN as EPIPE in SO_ERROR (the
/// reviewer's rst_witness.py: 10/10 on the shed and 413 paths); macOS turns it
/// into ECONNRESET on the read. Both fail here.
#[test]
fn every_early_answer_closes_gracefully_so_error_is_clean() {
    let log = scratch("graceful");
    let s = start(&log);
    // 413: a declared length over the cap (65 KiB, just past 64 KiB), the body sent whole
    let body = format!("{{\"pad\":\"{}\"}}", "a".repeat(65 * 1024));
    let big = common::raw_request("POST", "/ledger/events", Some(&format!("Bearer {TOKEN}")), &body);
    // 401 before the body is read, with a body sent
    let wrong = common::raw_request("POST", "/ledger/events", Some("Bearer wrong"), &"x".repeat(300));
    // 404 before the body is read, with a body sent
    let unknown = common::raw_request("POST", "/nowhere", Some(&format!("Bearer {TOKEN}")), &"y".repeat(2000));
    // a refusal hyper answers itself (400: an unparseable Content-Length), with bytes after the head
    let mut malformed = b"POST /ledger/events HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n".to_vec();
    malformed.extend(std::iter::repeat_n(b'z', 30 * 1024));
    for (label, raw, want) in [("413", &big, 413u16), ("401", &wrong, 401), ("404", &unknown, 404), ("400", &malformed, 400)] {
        for i in 0..5 {
            let (status, after, err) = close_outcome(s.port, raw);
            assert_eq!(status, want, "{label} #{i}");
            assert_eq!(after, "eof", "{label} #{i}: the read after the response");
            assert!(err.is_none(), "{label} #{i}: SO_ERROR after a whole response and EOF: {err:?}");
        }
    }
    drop(s);

    // the load-shed 503: the request is never read by the server
    let log = scratch("graceful_shed");
    let s = start_with(&log, &[("LEDGER_MAX_CONNECTIONS", "1")]);
    let mut holder = slow_body_client(s.port, TOKEN, "/ledger/events");
    let health = common::raw_request("GET", "/health", None, "");
    let mut checked = 0;
    let until = Instant::now() + Duration::from_secs(4); // inside the holder's 5 s body deadline
    while checked < 5 {
        assert!(Instant::now() < until, "only {checked} shed responses before the holder's deadline");
        if !still_held(&holder) {
            holder = slow_body_client(s.port, TOKEN, "/ledger/events");
            std::thread::sleep(Duration::from_millis(200));
            continue;
        }
        let (status, after, err) = close_outcome(s.port, &health);
        if status != 503 {
            continue; // the holder's slot was being released; re-arm and retry
        }
        assert_eq!(after, "eof", "shed #{checked}: the read after the response");
        assert!(err.is_none(), "shed #{checked}: SO_ERROR after a whole 503 and EOF: {err:?}");
        checked += 1;
    }
    drop(holder);
}

// --- concurrency guarantees (appends strictly serialized) ------------------------------------------

fn parallel<F>(n: usize, threads: usize, f: F) -> Vec<(u16, Value)>
where
    F: Fn(usize) -> (u16, Value) + Send + Sync + 'static,
{
    let f = Arc::new(f);
    let next = Arc::new(AtomicUsize::new(0));
    let handles: Vec<_> = (0..threads)
        .map(|_| {
            let f = f.clone();
            let next = next.clone();
            std::thread::spawn(move || {
                let mut out = Vec::new();
                loop {
                    let i = next.fetch_add(1, Ordering::SeqCst);
                    if i >= n {
                        break;
                    }
                    out.push(f(i));
                }
                out
            })
        })
        .collect();
    handles.into_iter().flat_map(|h| h.join().unwrap()).collect()
}

#[test]
fn eight_hundred_parallel_posts_over_200_ids_create_exactly_200_and_chain_survives_restart() {
    let log = scratch("par800");
    let s = start(&log);
    let port = s.port;
    let results = parallel(800, 64, move |i| authed(port, "POST", "/ledger/events", Some(&event(&format!("par-{}", i % 200)))));
    let created = results.iter().filter(|(st, _)| *st == 201).count();
    let existing = results.iter().filter(|(st, _)| *st == 200).count();
    assert_eq!(created, 200, "exactly one create per id");
    assert_eq!(existing, 600, "every other post is an idempotent retry");
    let mut seqs: Vec<u64> = results.iter().filter(|(st, _)| *st == 201).map(|(_, v)| v["seq"].as_u64().unwrap()).collect();
    seqs.sort_unstable();
    assert_eq!(seqs, (0..200).collect::<Vec<u64>>(), "dense, unique seqs");
    assert_eq!(authed(port, "GET", "/ledger/verify", None).1, json!({"valid": true, "entries": 200}));
    assert_eq!(std::fs::read_to_string(&log.0).unwrap().lines().count(), 200);
    drop(s);

    let s = start(&log);
    assert_eq!(authed(s.port, "GET", "/ledger/verify", None).1, json!({"valid": true, "entries": 200}));
}

#[test]
fn fifty_conflicting_posts_on_one_id_give_one_create_and_49_conflicts() {
    let log = scratch("conflict50");
    let s = start(&log);
    let port = s.port;
    let results = parallel(50, 50, move |i| {
        authed(port, "POST", "/ledger/events", Some(&event_value("same-id", &format!("variant {i}")).to_string()))
    });
    assert_eq!(results.iter().filter(|(st, _)| *st == 201).count(), 1);
    assert_eq!(results.iter().filter(|(st, _)| *st == 409).count(), 49);
    assert_eq!(authed(port, "GET", "/ledger/verify", None).1, json!({"valid": true, "entries": 1}));
    drop(s);
    let s = start(&log);
    assert_eq!(authed(s.port, "GET", "/ledger/verify", None).1, json!({"valid": true, "entries": 1}));
}

/// The connection cap the barrier tests run with: one slot for the bad connection, four for the others' requests
/// (each releases its slot asynchronously after its answer).
const CAP: usize = 5;

fn start_capped(log: &Scratch, cap: usize) -> ServerHandle {
    let cap = cap.to_string();
    start_with(log, &[("LEDGER_MAX_CONNECTIONS", cap.as_str())])
}

/// Blocks until at least `n` response bytes sit unread in `c`'s receive queue (peeked, not consumed). The hang
/// guard is the request deadline: the server must have started writing well before it.
fn queued_at_least(c: &TcpStream, n: usize) {
    let mut buf = vec![0u8; n];
    let guard = Instant::now() + REQUEST_DEADLINE;
    loop {
        c.set_nonblocking(true).unwrap();
        let got = c.peek(&mut buf);
        c.set_nonblocking(false).unwrap();
        if let Ok(k) = got {
            if k >= n {
                return;
            }
        }
        assert!(Instant::now() < guard, "the server queued fewer than {n} bytes to the slow reader");
        std::thread::sleep(Duration::from_millis(20));
    }
}
