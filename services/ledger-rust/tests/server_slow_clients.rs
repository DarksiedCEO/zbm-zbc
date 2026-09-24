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
//! complete within `PROMPT` while the bad client(s) are stalled, and that the
//! server itself cuts the stalled connection within its documented deadline.
//! The concurrency tests pin that appends stay strictly serialized.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

const TOKEN: &str = "slow-clients-test-token";
/// A well-behaved client must be served within this while others stall.
const PROMPT: Duration = Duration::from_secs(1);
/// Server deadlines (src/bin/server.rs, ADR 0003 section 7).
const HEADER_READ_TIMEOUT: Duration = Duration::from_secs(5);
const BODY_READ_TIMEOUT: Duration = Duration::from_secs(5);
const REQUEST_DEADLINE: Duration = Duration::from_secs(15);
/// Scheduling slack on top of a server deadline before the test calls it hung.
const SLACK: Duration = Duration::from_secs(2);

struct ServerHandle {
    child: Child,
    port: u16,
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
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let p = std::env::temp_dir().join(format!("ledger_slow_{label}_{}_{nanos}.jsonl", std::process::id()));
    let _ = std::fs::remove_file(&p);
    Scratch(p)
}

fn free_port() -> u16 {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.local_addr().unwrap().port()
}

fn start_with(log: &Scratch, env: &[(&str, &str)]) -> ServerHandle {
    let port = free_port();
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_server"));
    cmd.env("LEDGER_SERVICE_TOKEN", TOKEN)
        .env("LEDGER_PORT", port.to_string())
        .env("LEDGER_LOG_PATH", log.0.to_str().unwrap())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    for (k, v) in env {
        cmd.env(k, v);
    }
    let mut child = cmd.spawn().expect("failed to spawn ledger-rust server");
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        if let Some(st) = child.try_wait().unwrap() {
            panic!("server exited before coming up: {st}");
        }
        if let Ok((200, _)) = request(port, "GET", "/health", None, None) {
            break;
        }
        assert!(Instant::now() < deadline, "server did not come up");
        std::thread::sleep(Duration::from_millis(50));
    }
    ServerHandle { child, port }
}

fn start(log: &Scratch) -> ServerHandle {
    start_with(log, &[])
}

/// One HTTP/1.1 request on a fresh connection; the client itself gives up
/// after 15 s so a hung server fails the test instead of hanging it.
fn request(port: u16, method: &str, path: &str, auth: Option<&str>, body: Option<&str>) -> std::io::Result<(u16, String)> {
    let mut stream = TcpStream::connect(("127.0.0.1", port))?;
    stream.set_read_timeout(Some(Duration::from_secs(15)))?;
    let auth_line = auth.map(|t| format!("Authorization: Bearer {t}\r\n")).unwrap_or_default();
    let body = body.unwrap_or("");
    let req = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth_line}Content-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(req.as_bytes())?;
    let mut out = Vec::new();
    stream.read_to_end(&mut out)?;
    let resp = String::from_utf8_lossy(&out).to_string();
    let status = resp
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    Ok((status, resp.split("\r\n\r\n").nth(1).unwrap_or("").to_string()))
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

/// The core assertion of this file: while bad clients are stalled, `/health`
/// and an authenticated append each complete within PROMPT.
fn assert_others_served_promptly(port: u16, label: &str) {
    let t = Instant::now();
    let (status, _) = request(port, "GET", "/health", None, None).unwrap();
    let health = t.elapsed();
    assert_eq!(status, 200, "{label}: /health status");
    assert!(health < PROMPT, "{label}: /health took {health:?} (limit {PROMPT:?})");

    let id = format!("ok-{}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos());
    let t = Instant::now();
    let (status, _) = authed(port, "POST", "/ledger/events", Some(&event(&id)));
    let append = t.elapsed();
    assert_eq!(status, 201, "{label}: authenticated append status");
    assert!(append < PROMPT, "{label}: authenticated append took {append:?} (limit {PROMPT:?})");
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
    std::thread::sleep(Duration::from_millis(300));

    assert_others_served_promptly(s.port, "wrong-token slow body");

    // The stalled client itself is answered 401 and closed WITHOUT the server
    // waiting for (draining) the 4988 missing body bytes.
    let (resp, elapsed) = read_until_closed(&mut bad, since);
    assert_eq!(status_of(&resp), 401, "stalled wrong-token client: {resp:?}");
    assert!(elapsed < Duration::from_secs(3), "401 + close took {elapsed:?}; server waited for the body");
}

// --- slow body with the RIGHT token ------------------------------------------------------------

#[test]
fn slow_body_with_right_token_does_not_block_others_and_is_cut_off() {
    let log = scratch("righttok");
    let s = start(&log);
    let since = Instant::now();
    let mut bad = slow_body_client(s.port, TOKEN, "/ledger/events");
    std::thread::sleep(Duration::from_millis(300));

    assert_others_served_promptly(s.port, "right-token slow body");

    // The server gives up on the body after BODY_READ_TIMEOUT: 408, closed,
    // nothing recorded from it.
    let (resp, elapsed) = read_until_closed(&mut bad, since);
    assert_eq!(status_of(&resp), 408, "stalled right-token client: {resp:?}");
    assert!(elapsed >= BODY_READ_TIMEOUT - Duration::from_millis(500), "cut off too early: {elapsed:?}");
    assert!(elapsed < BODY_READ_TIMEOUT + SLACK, "not cut off within the body deadline: {elapsed:?}");
    let (_, entries) = authed(s.port, "GET", "/ledger/entries", None);
    assert_eq!(entries.as_array().unwrap().len(), 1, "only the prompt append was recorded");
}

/// A slow-body trickle (one byte every 500 ms — never idle long enough for a
/// per-read timeout) is still cut at BODY_READ_TIMEOUT: the deadline is total,
/// not per read.
#[test]
fn trickled_body_is_cut_off_at_the_total_body_deadline() {
    let log = scratch("trickle");
    let s = start(&log);
    let since = Instant::now();
    let mut bad = slow_body_client(s.port, TOKEN, "/ledger/append");
    let mut writer = bad.try_clone().unwrap();
    let trickle = std::thread::spawn(move || {
        for _ in 0..40 {
            std::thread::sleep(Duration::from_millis(500));
            if writer.write_all(b" ").is_err() {
                break;
            }
        }
    });
    std::thread::sleep(Duration::from_millis(300));
    assert_others_served_promptly(s.port, "trickled body");
    let (resp, elapsed) = read_until_closed(&mut bad, since);
    assert_eq!(status_of(&resp), 408, "{resp:?}");
    assert!(elapsed < BODY_READ_TIMEOUT + SLACK, "trickle kept the connection for {elapsed:?}");
    trickle.join().unwrap();
}

// --- slow header ---------------------------------------------------------------------------------

#[test]
fn slow_header_does_not_block_others_and_is_cut_off() {
    let log = scratch("slowhead");
    let s = start(&log);
    let since = Instant::now();
    let mut bad = connect(s.port);
    bad.write_all(b"POST /ledger/events HTTP/1.1\r\nHost: x\r\nAuthor").unwrap();
    std::thread::sleep(Duration::from_millis(300));

    assert_others_served_promptly(s.port, "slow header");

    let (_, elapsed) = read_until_closed(&mut bad, since);
    assert!(elapsed < HEADER_READ_TIMEOUT + SLACK, "slow-header connection held for {elapsed:?}");
}

/// A connection that sends nothing at all is closed by the header deadline too.
#[test]
fn idle_connection_is_closed_by_the_header_deadline() {
    let log = scratch("idle");
    let s = start(&log);
    let since = Instant::now();
    let mut bad = connect(s.port);
    assert_others_served_promptly(s.port, "idle connection");
    let (_, elapsed) = read_until_closed(&mut bad, since);
    assert!(elapsed < HEADER_READ_TIMEOUT + SLACK, "idle connection held for {elapsed:?}");
}

// --- oversized Content-Length ------------------------------------------------------------------

/// A declared body over 64 KiB is refused from the header alone: 413 at once,
/// no body byte sent, nothing read.
#[test]
fn oversized_content_length_is_413_before_any_body_is_read() {
    let log = scratch("bigcl");
    let s = start(&log);
    for path in ["/ledger/events", "/ledger/append"] {
        let since = Instant::now();
        let mut c = connect(s.port);
        let head = format!(
            "POST {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n\
             Content-Type: application/json\r\nContent-Length: 10000000\r\n\r\n"
        );
        c.write_all(head.as_bytes()).unwrap();
        let (resp, elapsed) = read_until_closed(&mut c, since);
        assert_eq!(status_of(&resp), 413, "{path}: {resp:?}");
        assert!(elapsed < PROMPT, "{path}: 413 took {elapsed:?} (server waited for the body)");
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
    std::thread::sleep(Duration::from_millis(1500)); // let the server fill the socket buffers

    assert_others_served_promptly(s.port, "slow reader");

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
    let s = start(&log);
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
    std::thread::sleep(Duration::from_millis(300));
    for round in 0..3 {
        assert_others_served_promptly(s.port, &format!("150 slow clients, round {round}"));
    }
    drop(bad);
}

/// Past the connection cap the server sheds load with an immediate 503
/// instead of queueing the caller behind stalled connections, and it
/// recovers as soon as the stalled connections hit their deadline.
#[test]
fn over_connection_cap_gets_prompt_503_and_recovers() {
    let log = scratch("cap");
    let s = start_with(&log, &[("LEDGER_MAX_CONNECTIONS", "4")]);
    let since = Instant::now();
    let bad: Vec<TcpStream> = (0..4).map(|_| slow_body_client(s.port, TOKEN, "/ledger/events")).collect();
    std::thread::sleep(Duration::from_millis(300));
    let t = Instant::now();
    let (status, body) = request(s.port, "GET", "/health", None, None).unwrap();
    assert_eq!(status, 503, "{body}");
    assert!(t.elapsed() < PROMPT, "503 took {:?}", t.elapsed());
    std::thread::sleep((BODY_READ_TIMEOUT + SLACK).saturating_sub(since.elapsed()));
    assert_others_served_promptly(s.port, "after the stalled connections were cut");
    drop(bad);
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
