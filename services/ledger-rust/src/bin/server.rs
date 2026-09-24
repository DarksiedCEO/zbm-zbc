//! Minimal REST wrapper around the Ledger core (lib.rs). HTTP/1.1 is served
//! by hyper on a small tokio runtime (since fix wave 4; it was tiny_http,
//! which gives no access to the socket and so cannot enforce deadlines —
//! see the fix wave 4 notes below and docs/adr/0003 section 7).
//!
//! Persistence: backed by `PersistentLedger` (see persistence.rs) — an
//! append-only JSONL log at LEDGER_LOG_PATH. Startup fails closed: if the
//! log file exists but its hash chain does not verify (corruption or
//! tampering), the process refuses to start rather than silently serving
//! a fresh, empty ledger that would hide the problem.
//!
//! Independent review, Sep 22 2026 (CONFIRMED, both — this is the single
//! most important service to protect and had neither):
//!   1. Hardcoded `0.0.0.0:{port}` bind, no loopback default or override.
//!      Fixed: `LEDGER_BIND_ADDR` env var, defaults to `127.0.0.1`.
//!   2. Zero authentication on any route, including POST /ledger/append
//!      (anyone who could reach the port could forge or read the entire
//!      evidence ledger) and GET /ledger/entries (read the whole ledger).
//!      Fixed: `LEDGER_SERVICE_TOKEN` env var, fail-closed at startup if
//!      unset (same pattern as detection-py's ZBM_SERVICE_TOKEN and
//!      fulfillment-py's FULFILLMENT_SERVICE_TOKEN), required as
//!      `Authorization: Bearer <token>` on every route except /health,
//!      checked with a constant-time byte comparison (no hmac crate is a
//!      dependency here, so this is implemented directly rather than
//!      pulled in for one comparison — see `constant_time_eq` below).
//!
//! Sep 24 2026 (docs/adr/0003):
//!   - `amount_usd` on POST /ledger/append must be a two-decimal money
//!     string ("12.30") or null; a JSON number is rejected with 400.
//!     Since fix wave 2 (ADR 0003 section 1a) it must also be at most
//!     "999999999999999.99"; larger amounts get 400. Already-persisted
//!     over-bound amounts still load and verify.
//!   - POST /ledger/events records a generic, idempotent event on the same
//!     hash chain (201 new / 200 identical retry / 409 conflicting content /
//!     400 invalid / 401 unauthenticated). Every entry now carries `kind`.
//!   - Request bodies are capped at MAX_BODY_BYTES; larger bodies get 413.
//!
//! Fix wave 1, Sep 24 2026 (docs/adr/0003 sections 4-5):
//!   - POST /ledger/append rejects `|`, control characters, and the literal
//!     string "null" in optional fields with 400 (AEGIS F7).
//!   - A torn final log line left by a crash is preserved and truncated on
//!     startup instead of refusing to start (AEGIS F5); see persistence.rs.
//!   - The startup log names the real bound address.
//!   - (Superseded in fix wave 4: the tiny_http limitation that dropped any
//!     request whose head held a non-ASCII byte is gone; hyper accepts such
//!     bytes in header values as obs-text, and an Authorization value that
//!     is not visible ASCII is simply a 401.)
//!
//! Fix wave 3, Sep 24 2026 (docs/adr/0003 section 6):
//!   - GET /ledger/verify on an empty ledger is 200 {"valid":true,"entries":0}
//!     (was 409 "Empty"); 409 now always means the chain failed to verify.
//!   - Unknown fields are refused: in a persisted log line (startup refuses)
//!     and in a POST /ledger/append body (400) (AEGIS N8).

//!
//! Fix wave 4, Sep 24 2026 (docs/adr/0003 section 7), AEGIS HIGH "one slow
//! connection freezes the whole ledger": requests were handled one at a time
//! on the main thread with no socket deadlines, and tiny_http drained any
//! unread request body on the main thread after responding
//! (`Request::respond` -> drop -> `EqualReader::drop`, proven with gdb). A
//! wrong-token client that declared a 5000-byte body and sent 12 bytes held
//! /health for as long as it liked. Now:
//!   - every connection runs in its own tokio task; at most
//!     LEDGER_MAX_CONNECTIONS (default 512) at once, beyond that an
//!     immediate 503; listen backlog 128;
//!   - deadlines per connection: request head within HEADER_READ_TIMEOUT,
//!     body within BODY_READ_TIMEOUT (total, not per read), and the whole
//!     connection (head, body, handling, response write) within
//!     REQUEST_DEADLINE, after which the socket is dropped;
//!   - one request per connection (`Connection: close`), so an unanswered
//!     or unauthenticated request is answered and closed without ever
//!     reading (draining) its body;
//!   - a declared Content-Length over MAX_BODY_BYTES is a 413 before any
//!     body byte is read;
//!   - the ledger Mutex is taken only after the body has been fully read and
//!     parsed, inside a bounded blocking pool, so appends stay strictly
//!     serialized exactly as before (same chain, same idempotency rules).
use std::convert::Infallible;
use std::net::{SocketAddr, ToSocketAddrs};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use http_body_util::{BodyExt, Full, Limited};
use hyper::body::{Bytes, Incoming};
use hyper::header::{HeaderValue, AUTHORIZATION, CONNECTION, CONTENT_LENGTH, CONTENT_TYPE, RETRY_AFTER};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Method, Request, Response, StatusCode};
use hyper_util::rt::{TokioIo, TokioTimer};
use ledger_rust::{ledger_log, EventAppendOutcome, EventInput, LedgerRecordInput, PersistError, PersistentLedger};
use tokio::io::AsyncWriteExt;
use tokio::net::{TcpListener, TcpSocket, TcpStream};
use tokio::sync::Semaphore;

/// Upper bound on a request body. A finding record or an event is well
/// under 2 KiB; anything near this size is not a legitimate caller.
const MAX_BODY_BYTES: u64 = 64 * 1024;

/// The request line and headers must arrive within this (hyper closes the
/// connection otherwise). Also closes connections that send nothing.
const HEADER_READ_TIMEOUT: Duration = Duration::from_secs(5);
/// The whole body must arrive within this, measured from when the handler
/// starts reading it — a total deadline, so a byte-at-a-time trickle is cut
/// off too. Answered 408.
const BODY_READ_TIMEOUT: Duration = Duration::from_secs(5);
/// Hard cap on a connection's whole life: head + body + handling + writing
/// the response. Bounds a client that never reads its response. Larger than
/// HEADER_READ_TIMEOUT + BODY_READ_TIMEOUT so a request whose body arrived
/// just in time still has 5 s for the append and the response write.
const REQUEST_DEADLINE: Duration = Duration::from_secs(15);
/// Default cap on concurrently open connections (LEDGER_MAX_CONNECTIONS).
const DEFAULT_MAX_CONNECTIONS: usize = 512;
/// Kernel accept queue length for the listening socket.
const LISTEN_BACKLOG: u32 = 128;
/// hyper's read buffer, which bounds the request head (larger heads get 431).
const MAX_READ_BUF: usize = 16 * 1024;
/// Async worker threads: they only move bytes; ledger work is on the
/// blocking pool.
const WORKER_THREADS: usize = 4;
/// Blocking pool for ledger work (lock + fsync). Appends serialize on the
/// Mutex anyway; this bounds threads, not throughput.
const MAX_BLOCKING_THREADS: usize = 16;
/// How long a load-shed connection gets to receive its 503.
const SHED_WRITE_TIMEOUT: Duration = Duration::from_secs(1);

type Body = Full<Bytes>;

struct App {
    ledger: Mutex<PersistentLedger>,
    required_token: String,
}

/// Locks the ledger. A poisoned lock means a panic happened mid-operation;
/// the old single-threaded server died on any panic, and the in-memory state
/// can no longer be trusted, so the process exits (fail closed; the log on
/// disk is re-verified on restart).
fn lock(ledger: &Mutex<PersistentLedger>) -> MutexGuard<'_, PersistentLedger> {
    ledger.lock().unwrap_or_else(|_| {
        ledger_log!("ledger-rust: FATAL — ledger lock poisoned by an earlier panic; exiting (fail closed)");
        std::process::exit(1);
    })
}

fn error_json(status: u16, msg: String) -> (u16, String) {
    (status, serde_json::json!({"error": msg}).to_string())
}

/// Reads the whole body, at most MAX_BODY_BYTES, within BODY_READ_TIMEOUT.
/// A declared Content-Length over the cap is refused before any body byte is
/// read. Err carries the (status, body) response to send.
async fn read_body(request: Request<Incoming>) -> Result<String, (u16, String)> {
    let declared = request
        .headers()
        .get(CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.trim().parse::<u64>().ok());
    if declared.is_some_and(|n| n > MAX_BODY_BYTES) {
        return Err(error_json(413, format!("request body exceeds {MAX_BODY_BYTES} bytes")));
    }
    let limited = Limited::new(request.into_body(), MAX_BODY_BYTES as usize);
    let bytes = match tokio::time::timeout(BODY_READ_TIMEOUT, limited.collect()).await {
        Err(_) => {
            return Err(error_json(
                408,
                format!("request body not received within {}s", BODY_READ_TIMEOUT.as_secs()),
            ))
        }
        Ok(Err(e)) if e.is::<http_body_util::LengthLimitError>() => {
            return Err(error_json(413, format!("request body exceeds {MAX_BODY_BYTES} bytes")));
        }
        Ok(Err(e)) => return Err(error_json(400, format!("failed to read body: {e}"))),
        Ok(Ok(collected)) => collected.to_bytes(),
    };
    String::from_utf8(bytes.to_vec()).map_err(|e| error_json(400, format!("failed to read body as UTF-8: {e}")))
}

fn handle_event(ledger: &Mutex<PersistentLedger>, input: EventInput) -> (u16, String) {
    let event_id = input.event_id.clone();
    let mut l = lock(ledger);
    match l.append_event(input) {
        Ok(EventAppendOutcome::Created(entry)) => (201, serde_json::to_string(entry).unwrap()),
        Ok(EventAppendOutcome::Existing(entry)) => (200, serde_json::to_string(entry).unwrap()),
        Ok(EventAppendOutcome::Conflict(_)) => (
            409,
            serde_json::json!({
                "error": format!(
                    "event_id {event_id:?} is already recorded with different content; \
                     an event_id can only ever describe one event"
                ),
                "event_id": event_id,
            })
            .to_string(),
        ),
        Err(PersistError::Invalid(reason)) => {
            (400, serde_json::json!({"error": format!("invalid event: {reason}")}).to_string())
        }
        Err(e) => {
            // Same rule as findings: in-memory state untouched, caller must
            // treat this as NOT recorded.
            ledger_log!("ledger-rust: event append failed to persist: {e}");
            (500, serde_json::json!({"error": format!("failed to persist event: {e}")}).to_string())
        }
    }
}

/// Parses and validates an event body. Runs BEFORE the ledger lock.
fn parse_event(body: &str) -> Result<EventInput, (u16, String)> {
    let input: EventInput = serde_json::from_str(body)
        .map_err(|e| (400, serde_json::json!({"error": format!("invalid event: {e}")}).to_string()))?;
    input
        .validate()
        .map_err(|e| (400, serde_json::json!({"error": format!("invalid event: {e}")}).to_string()))?;
    Ok(input)
}

fn handle_append(ledger: &Mutex<PersistentLedger>, record: LedgerRecordInput) -> (u16, String) {
    let mut l = lock(ledger);
    match l.append(record) {
        Ok(entry) => (201, serde_json::to_string(entry).unwrap()),
        Err(PersistError::Invalid(reason)) => (
            400,
            serde_json::json!({"error": format!("invalid LedgerRecordInput: {reason}")}).to_string(),
        ),
        Err(e) => {
            // Disk write failed — the in-memory ledger was deliberately left
            // untouched (see PersistentLedger::append). Surface this as a
            // hard server error: the caller must not treat this as "recorded."
            ledger_log!("ledger-rust: append failed to persist: {e}");
            (500, serde_json::json!({"error": format!("failed to persist entry: {e}")}).to_string())
        }
    }
}

/// Parses and validates a finding body. Runs BEFORE the ledger lock.
fn parse_append(body: &str) -> Result<LedgerRecordInput, (u16, String)> {
    serde_json::from_str::<LedgerRecordInput>(body)
        .map_err(|e| e.to_string())
        .and_then(|r| r.validate().map(|()| r))
        .map_err(|e| (400, serde_json::json!({"error": format!("invalid LedgerRecordInput: {e}")}).to_string()))
}

/// Runs ledger work (lock + disk) on the bounded blocking pool so a slow
/// fsync never stalls the async workers that serve /health and move bytes.
/// A panic inside is fatal, exactly as it was for the old single-threaded
/// server (see `lock`).
async fn on_ledger<F>(app: &Arc<App>, f: F) -> (u16, String)
where
    F: FnOnce(&Mutex<PersistentLedger>) -> (u16, String) + Send + 'static,
{
    let app = Arc::clone(app);
    match tokio::task::spawn_blocking(move || f(&app.ledger)).await {
        Ok(r) => r,
        Err(e) => {
            ledger_log!("ledger-rust: FATAL — ledger operation panicked ({e}); exiting (fail closed)");
            std::process::exit(1);
        }
    }
}

/// Constant-time equality check for the bearer token comparison, mirroring
/// the semantics of Python's `hmac.compare_digest` (this codebase's other
/// two services use that directly): a length mismatch is allowed to
/// short-circuit (length is not the secret), but for equal-length inputs
/// every byte is compared regardless of an early mismatch, so a timing
/// side-channel can't be used to recover the token byte-by-byte.
fn constant_time_eq(a: &str, b: &str) -> bool {
    let a = a.as_bytes();
    let b = b.as_bytes();
    if a.len() != b.len() {
        return false;
    }
    let mut diff: u8 = 0;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

/// The first Authorization header's `Bearer <token>` value. A value that is
/// not visible ASCII (obs-text bytes) yields None, i.e. 401.
fn extract_bearer_token(request: &Request<Incoming>) -> Option<String> {
    request
        .headers()
        .get(AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer ").map(|s| s.to_string()))
}

fn unauthorized_response() -> (u16, String) {
    (
        401,
        serde_json::json!({"error": "missing, malformed, or invalid Authorization header (expected: Bearer <token>)"})
            .to_string(),
    )
}

fn json_response((status, body): (u16, String)) -> Response<Body> {
    let mut resp = Response::new(Full::new(Bytes::from(body)));
    *resp.status_mut() = StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    resp
}

async fn handle(request: Request<Incoming>, app: Arc<App>) -> (u16, String) {
    let method = request.method().clone();
    let url = request
        .uri()
        .path_and_query()
        .map(|p| p.as_str().to_string())
        .unwrap_or_else(|| request.uri().path().to_string());

    // Auth gate: every route except /health requires a valid bearer
    // token, checked before any route logic runs and before any body byte
    // is read (fail-closed — an unmatched or malformed Authorization header
    // never falls through to a handler). /health stays open for basic
    // liveness checks, matching detection-py/fulfillment-py.
    if url != "/health" {
        let authorized = match extract_bearer_token(&request) {
            Some(token) => constant_time_eq(&token, &app.required_token),
            None => false,
        };
        if !authorized {
            return unauthorized_response();
        }
    }

    match (method, url.as_str()) {
        (Method::GET, "/health") => (200, serde_json::json!({"status": "ok", "service": "ledger-rust"}).to_string()),

        (Method::GET, "/ledger/entries") => {
            on_ledger(&app, |ledger| (200, serde_json::to_string(lock(ledger).entries()).unwrap())).await
        }

        (Method::GET, "/ledger/verify") => {
            on_ledger(&app, |ledger| {
                let l = lock(ledger);
                match l.verify_chain() {
                    Ok(()) => (200, serde_json::json!({"valid": true, "entries": l.len()}).to_string()),
                    Err(e) => (409, serde_json::json!({"valid": false, "error": format!("{e:?}")}).to_string()),
                }
            })
            .await
        }

        (Method::POST, "/ledger/events") => match read_body(request).await.and_then(|b| parse_event(&b)) {
            Err(resp) => resp,
            Ok(input) => on_ledger(&app, move |ledger| handle_event(ledger, input)).await,
        },

        (Method::POST, "/ledger/append") => match read_body(request).await.and_then(|b| parse_append(&b)) {
            Err(resp) => resp,
            Ok(record) => on_ledger(&app, move |ledger| handle_append(ledger, record)).await,
        },

        _ => (404, serde_json::json!({"error": "not found"}).to_string()),
    }
}

/// Serves exactly one request on `stream` within REQUEST_DEADLINE. Dropping
/// the connection future on timeout closes the socket; ledger work already
/// handed to the blocking pool still completes (it is never torn halfway).
async fn serve_connection(stream: TcpStream, app: Arc<App>) {
    let service = service_fn(move |request| {
        let app = Arc::clone(&app);
        async move { Ok::<_, Infallible>(json_response(handle(request, app).await)) }
    });
    let conn = http1::Builder::new()
        .timer(TokioTimer::new())
        .header_read_timeout(HEADER_READ_TIMEOUT)
        .keep_alive(false)
        .max_buf_size(MAX_READ_BUF)
        .serve_connection(TokioIo::new(stream), service);
    // Timeout or connection error: either way the socket is closed on drop,
    // and there is no one left to answer.
    let _ = tokio::time::timeout(REQUEST_DEADLINE, conn).await;
}

/// Over the connection cap: answer 503 at once (bounded by
/// SHED_WRITE_TIMEOUT) and close, without reading the request.
async fn shed(mut stream: TcpStream) {
    let body = serde_json::json!({"error": "ledger-rust is at its connection limit; retry shortly"}).to_string();
    let resp = format!(
        "HTTP/1.1 503 Service Unavailable\r\n{CONTENT_TYPE}: application/json\r\n{RETRY_AFTER}: 1\r\n\
         {CONNECTION}: close\r\n{CONTENT_LENGTH}: {}\r\n\r\n{body}",
        body.len()
    );
    let _ = tokio::time::timeout(SHED_WRITE_TIMEOUT, async {
        stream.write_all(resp.as_bytes()).await?;
        stream.shutdown().await
    })
    .await;
}

async fn serve(listener: TcpListener, app: Arc<App>, max_connections: usize) {
    let slots = Arc::new(Semaphore::new(max_connections));
    loop {
        let stream = match listener.accept().await {
            Ok((stream, _)) => stream,
            Err(e) => {
                // e.g. EMFILE: back off briefly instead of spinning; the
                // pending connections stay in the kernel backlog.
                ledger_log!("ledger-rust: accept failed: {e}");
                tokio::time::sleep(Duration::from_millis(100)).await;
                continue;
            }
        };
        let _ = stream.set_nodelay(true);
        match Arc::clone(&slots).try_acquire_owned() {
            Ok(slot) => {
                let app = Arc::clone(&app);
                tokio::spawn(async move {
                    serve_connection(stream, app).await;
                    drop(slot);
                });
            }
            Err(_) => {
                tokio::spawn(shed(stream));
            }
        }
    }
}

fn bind(addr: &str) -> std::io::Result<TcpListener> {
    let sock_addr: SocketAddr = addr
        .to_socket_addrs()?
        .next()
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidInput, format!("{addr} resolves to no address")))?;
    let socket = if sock_addr.is_ipv4() { TcpSocket::new_v4()? } else { TcpSocket::new_v6()? };
    socket.set_reuseaddr(true)?;
    socket.bind(sock_addr)?;
    socket.listen(LISTEN_BACKLOG)
}

fn load_required_token() -> String {
    match std::env::var("LEDGER_SERVICE_TOKEN") {
        Ok(t) if !t.is_empty() => t,
        _ => {
            ledger_log!(
                "ledger-rust: REFUSING TO START — LEDGER_SERVICE_TOKEN is not set. This is \
                 the evidence ledger; it does not start unauthenticated. Set \
                 LEDGER_SERVICE_TOKEN to a shared secret before starting ledger-rust, and set \
                 the identical value wherever this service is called from."
            );
            std::process::exit(1);
        }
    }
}

fn load_max_connections() -> usize {
    match std::env::var("LEDGER_MAX_CONNECTIONS") {
        Err(_) => DEFAULT_MAX_CONNECTIONS,
        Ok(v) => match v.trim().parse::<usize>() {
            Ok(n) if n > 0 => n,
            _ => {
                ledger_log!("ledger-rust: REFUSING TO START — LEDGER_MAX_CONNECTIONS={v:?} is not a positive integer");
                std::process::exit(1);
            }
        },
    }
}

fn main() {
    let required_token = load_required_token();
    let max_connections = load_max_connections();
    let port = std::env::var("LEDGER_PORT").unwrap_or_else(|_| "8090".to_string());
    let log_path = std::env::var("LEDGER_LOG_PATH")
        .unwrap_or_else(|_| "ledger_data/ledger.jsonl".to_string());
    let bind_host = std::env::var("LEDGER_BIND_ADDR").unwrap_or_else(|_| "127.0.0.1".to_string());
    let addr = format!("{bind_host}:{port}");

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(WORKER_THREADS)
        .max_blocking_threads(MAX_BLOCKING_THREADS)
        .enable_io()
        .enable_time()
        .build()
        .expect("failed to build the ledger-rust runtime");
    let listener = runtime.block_on(async { bind(&addr) }).expect("failed to bind ledger-rust HTTP server");

    let ledger = match PersistentLedger::open(&log_path) {
        Ok(l) => {
            ledger_log!(
                "ledger-rust: loaded {} existing entries from {log_path}, chain verified",
                l.len()
            );
            Mutex::new(l)
        }
        Err(e) => {
            ledger_log!(
                "ledger-rust: REFUSING TO START — ledger log at {log_path} failed to load: {e}"
            );
            ledger_log!(
                "ledger-rust: this is a fail-closed integrity check, not a crash — the log file \
                 is either corrupted or has been tampered with, and starting anyway would hide \
                 that. Resolve manually (restore from backup, or move the file aside if you \
                 accept starting a fresh ledger) before restarting."
            );
            std::process::exit(1);
        }
    };

    match listener.local_addr() {
        Ok(a) => ledger_log!("ledger-rust listening on {a} (log: {log_path})"),
        Err(_) => ledger_log!("ledger-rust listening on {addr} (log: {log_path})"),
    }

    let app = Arc::new(App { ledger, required_token });
    runtime.block_on(serve(listener, app, max_connections));
}
