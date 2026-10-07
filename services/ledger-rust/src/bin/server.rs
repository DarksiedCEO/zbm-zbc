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
//!
//! Fix wave 21, Sep 28 2026 (docs/adr/0003 section 8), AEGIS N20-M-1: every
//! path that answers without reading the whole request (the 503 load shed,
//! 401/404 before the body, 413 on the declared length, 408, a hyper-level
//! refusal) used to close the socket with request bytes still unread, so the
//! kernel sent RST after the response (Linux: EPIPE in SO_ERROR after EOF;
//! macOS: ECONNRESET on the client's read, the Mac "connection reset by
//! peer"). RFC 9112 section 9.6 graceful close now: after the response the
//! write side is shut down (FIN), then up to DRAIN_MAX_BYTES are read and
//! discarded for at most DRAIN_TIMEOUT, then the socket is closed. The answer
//! still never waits for the body; only the close is deferred, and it is
//! bounded. hyper serves without shutting the socket down itself
//! (`poll_without_shutdown`), so the socket comes back whatever way the
//! connection ended; no extra descriptor per connection. A connection that
//! has been answered gives its connection slot back BEFORE it drains (the
//! cap counts connections being served, as before); draining sockets — served
//! and load-shed alike — have their own bound (DRAINS_MAX), past which a
//! socket is closed at once (the old behaviour, RST included).
//!
//! Fix wave 21 (N20-M-3): LEDGER_PORT=0 binds an ephemeral port, and
//! LEDGER_PORT_FILE, when set, receives the bound port so a caller never has
//! to guess a free port and race another process for it.
//!
//! Fix wave 22 (AEGIS N21-C-2, lead ruling G7): the port file is written only
//! AFTER the ledger log opened (a server that refuses to start announces no
//! port); a target that is a symlink is refused (the server does not start);
//! the number goes to a temp file in the target's directory created
//! O_CREAT|O_EXCL|O_NOFOLLOW with a random suffix, mode 0600, fsynced, then
//! renamed into place (rename never follows a link at the destination); the
//! file is removed on SIGTERM/SIGINT and on a clean exit — only while it is
//! still the file this server wrote (same device and inode). Before, the temp
//! name was `<path>.tmp-<pid>` (predictable) and a link planted there was
//! followed: the server overwrote the link's target.
//!
//! Fix wave 23 (AEGIS N22-C-3): the PARENT directory is never reached through
//! a symlink either. The parent path is walked one component at a time from
//! `/` (absolute) or the working directory (relative), each opened
//! O_DIRECTORY|O_NOFOLLOW (O_PATH on Linux) and checked to be a directory; a
//! symlink anywhere in it refuses the start. The temp file is created, the
//! target checked and the rename done relative to that directory descriptor
//! (openat / fstatat / renameat), which stays open for the life of the process,
//! so the removal (unlinkat, after fstatat checks device and inode) acts on the
//! directory the file was written in even if a path component is swapped later.
//! The removal runs on SIGTERM, SIGINT, SIGHUP and SIGQUIT (the handler then
//! restores the default disposition and re-raises: the process still dies of
//! the signal) and on a clean exit. A caller whose temp directory sits behind
//! a symlink (macOS `/var` -> `/private/var`) passes the canonical path.
//!
//! Fix wave 24 (AEGIS N23-S-2): the handlers used to be installed only after
//! the rename, so a stop signal while the temp file existed left it behind
//! (AEGIS, aimed SIGTERM: 297/300) and one between the rename and the
//! handlers left the port file. Now the stop signals are blocked across the
//! whole publish (main blocks them before the runtime starts its threads, so
//! only the main thread ever takes one, and blocking it there blocks it for
//! the process); the removal is armed BEFORE anything is written (directory,
//! temp name, published name, then the temp file's device/inode before the
//! rename); the handler removes the temp name while the publish is in
//! progress and the published name while it is still this server's file.
//!
//! Fix wave 25 (AEGIS N24-S-8), the residual no handler can close: SIGKILL
//! (and the kernel's OOM killer, which sends it) cannot be blocked, caught or
//! handled, so a process killed that way leaves whatever it had written —
//! the `.<name>.tmp-<hex>` temp file if it dies during the publish (AEGIS
//! round 24: 49/50 aimed kills), the port file if it dies after (50/50). A
//! reader of the port file must therefore treat it as a HINT: the port it
//! names may be dead or reused, so connect and check (`GET /health`) before
//! trusting it, and a stale `.*.tmp-*` beside it is debris to remove, never
//! to read. The same holds for any crash that skips the handlers (an abort,
//! a power loss).
//!
//! Sweep F, Oct 6 2026 (docs/adr/0003 section 13; persistence.rs for F-1, F-6,
//! F-13):
//!   - F-1: one writer per log (`<log>.lock`); a second server on the same
//!     LEDGER_LOG_PATH refuses to start.
//!   - F-6: head checkpoint `<log>.head`; a missing, truncated or replaced
//!     log refuses to start unless LEDGER_ALLOW_RESET=1 (logged). New route
//!     GET /ledger/head -> {"entries","head_seq","head_hash"}.
//!   - F-7: POST /ledger/append with `Idempotency-Key: <finding_id>` (the
//!     header value must equal the body's finding_id, else 400) is idempotent
//!     on finding_id: 201 new / 200 identical / 409 different content.
//!     Without the header nothing changes.
//!   - F-12: LEDGER_SERVICE_TOKEN must be at least 32 bytes.
//!   - F-2: GET /ledger/entries?after_seq=&limit=&department=&event_type=
//!     (paginated, filtered; no query string = the whole ledger exactly as
//!     before); reads copy their snapshot under the lock in chunks and
//!     serialize outside it; at most one read per CPU (max 4) runs at once and a
//!     waiting append goes ahead of every next reader chunk (writer
//!     priority), so an append is never queued behind readers;
//!     GET /ledger/verify verifies only what is new since the last verified
//!     head (the full chain was verified at start), `?full=1` re-verifies
//!     everything.
//!   - F-4: optional per-caller tokens, LEDGER_CALLERS_FILE (see
//!     `load_callers`): a write must come from a caller whose departments
//!     include the event's department (findings: `revenue_recovery`), a
//!     read-only caller may only GET entries/verify/head (403 otherwise), and
//!     the shared token is then accepted only with LEDGER_ALLOW_SHARED_TOKEN=1.
//!     Unset: unchanged, with a startup warning.
//!   - A failed bind (or runtime start) exits 1 with a message, never a panic.
//!
//! AEGIS review of d6b1cd9, Oct 7 2026 (docs/adr/0003 section 13; persistence.rs
//! for M1-M3, L4, L5):
//!   - M1/M2/M3: only a log exactly one entry past its checkpoint starts; a
//!     non-empty log without a checkpoint needs LEDGER_MIGRATE_LEGACY, and an
//!     operator reset LEDGER_ALLOW_RESET, each set to the value the refusal
//!     prints (bound to that exact state; `load_binding`). Checkpoint decisions
//!     are structured `EVENT {json}` lines (a new ledger: `ledger_created`).
//!   - M4: reads are scoped (`Principal::read_scope`): a caller reads its
//!     departments (+ `read_departments`), `read_all` reads everything; a
//!     `department` filter outside the scope is a 403.
//!   - L1/L2/L3: a duplicate token hash or an empty LEDGER_CALLERS_FILE refuses
//!     to start; a configured caller token under 32 bytes is a 401.
//!   - L4: `GET /ledger/verify?full=1` also re-reads and re-hashes the log from
//!     disk (`verify_log_file`) outside the lock.
//!   - L5: filtered / scoped pages come from the per-department index
//!     (`entries_selected`), not a scan.
use std::convert::Infallible;
use std::ffi::CString;
use std::future::Future;
use std::io::{Read, Write};
use std::net::{SocketAddr, ToSocketAddrs};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::collections::HashMap;
use std::sync::atomic::{AtomicI32, AtomicPtr, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use http_body_util::{BodyExt, Full, Limited};
use hyper::body::{Bytes, Incoming};
use hyper::header::{HeaderValue, AUTHORIZATION, CONNECTION, CONTENT_LENGTH, CONTENT_TYPE, RETRY_AFTER};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Method, Request, Response, StatusCode};
use hyper_util::rt::{TokioIo, TokioTimer};
use ledger_rust::{
    genesis_hash, is_log_binding, is_reset_binding, ledger_log, verify_entry, verify_log_file, AppendOutcome,
    EntryFilter, EventAppendOutcome, EventInput, LedgerEntry, LedgerError, LedgerOpenOptions, LedgerRecordInput,
    PersistError, PersistentLedger, FINDINGS_DEPARTMENT,
};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
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
/// Graceful close (RFC 9112 section 9.6, fix wave 21): after the response and
/// the FIN, unread request bytes are read and discarded for at most this long…
const DRAIN_TIMEOUT: Duration = Duration::from_secs(1);
/// …or until this many bytes were discarded, whichever comes first; then the
/// socket is closed (a peer still sending past this gets the kernel's RST).
const DRAIN_MAX_BYTES: usize = 64 * 1024;
/// At most this many answered connections (served or load-shed) drain at
/// once; past it a socket is closed immediately. Separate from the connection
/// cap so a peer slow to close never holds a serving slot.
const DRAINS_MAX: usize = 512;

/// Sweep F-12: the shared token's minimum length in bytes.
const MIN_TOKEN_BYTES: usize = 32;
/// Sweep F-2: reads (entries, verify) running at once: one per CPU, at most
/// MAX_READ_SLOTS. A full read is CPU-bound (copy + serialize), so more reads
/// than CPUs only slow every read down (measured: 6 concurrent 500k-entry
/// reads on 2 CPUs took up to 10.1 s with 4 slots, 5.6 s with 2). The blocking
/// pool has MAX_BLOCKING_THREADS; capping reads well below it keeps threads
/// free for appends, which never wait for a read slot.
const MAX_READ_SLOTS: usize = 4;

fn read_slots() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1).clamp(1, MAX_READ_SLOTS)
}
/// Sweep F-2: entries copied per lock hold by a reader.
const READ_CHUNK: usize = 2048;
/// Sweep F-2: page size of GET /ledger/entries when a query is given without
/// `limit`, and the largest `limit` accepted.
const DEFAULT_PAGE: usize = 1000;
const MAX_PAGE: usize = 10_000;
/// Sweep F-4: largest LEDGER_CALLERS_FILE accepted.
const MAX_CALLERS_FILE_BYTES: u64 = 1024 * 1024;

type Body = Full<Bytes>;

/// Sweep F-2: writer priority on the ledger mutex. A writer announces itself
/// before it locks; a reader, before EACH chunk it copies, waits while any
/// writer is waiting. Readers hold the lock only for one chunk copy, so an
/// append waits for at most the chunk being copied at that moment, however
/// many readers there are. (std's Mutex makes no fairness promise: readers
/// re-locking in a loop could otherwise keep winning it.)
struct WriterPriority {
    waiting: Mutex<usize>,
    cv: Condvar,
}

/// Decrements the waiting-writer count when the write is done (or unwinds).
struct WriterTurn<'a>(&'a WriterPriority);

impl Drop for WriterTurn<'_> {
    fn drop(&mut self) {
        let mut w = self.0.waiting.lock().unwrap_or_else(PoisonError::into_inner);
        *w -= 1;
        if *w == 0 {
            self.0.cv.notify_all();
        }
    }
}

impl WriterPriority {
    fn new() -> WriterPriority {
        WriterPriority { waiting: Mutex::new(0), cv: Condvar::new() }
    }

    fn write<R>(&self, ledger: &Mutex<PersistentLedger>, f: impl FnOnce(&mut PersistentLedger) -> R) -> R {
        *self.waiting.lock().unwrap_or_else(PoisonError::into_inner) += 1;
        let _turn = WriterTurn(self);
        let mut l = lock(ledger);
        f(&mut l)
    }

    fn read<R>(&self, ledger: &Mutex<PersistentLedger>, f: impl FnOnce(&PersistentLedger) -> R) -> R {
        {
            let mut w = self.waiting.lock().unwrap_or_else(PoisonError::into_inner);
            while *w > 0 {
                // Bounded wait: a missed wake-up costs at most this long.
                w = self.cv.wait_timeout(w, Duration::from_millis(5)).unwrap_or_else(PoisonError::into_inner).0;
            }
        }
        let l = lock(ledger);
        f(&l)
    }
}

/// Sweep F-4: one entry of LEDGER_CALLERS_FILE.
#[derive(Debug, Clone, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Caller {
    caller: String,
    /// Write caller: the departments it writes (and reads). Read caller: the
    /// departments it reads.
    #[serde(default)]
    departments: Vec<String>,
    scope: Scope,
    /// AEGIS M4: further departments a write caller may READ (not write).
    #[serde(default)]
    read_departments: Vec<String>,
    /// AEGIS M4: reads every department and every finding (dashboard,
    /// compliance, audit). Exclusive with `read_departments`.
    #[serde(default)]
    read_all: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
enum Scope {
    Write,
    Read,
}

/// Who an authenticated request is.
#[derive(Debug, Clone)]
enum Principal {
    /// The shared LEDGER_SERVICE_TOKEN: every route, every department
    /// (exactly the behaviour before sweep F-4).
    Shared,
    Caller(Arc<Caller>),
}

impl Principal {
    fn can_write(&self) -> bool {
        match self {
            Principal::Shared => true,
            Principal::Caller(c) => c.scope == Scope::Write,
        }
    }

    fn may_write_department(&self, department: &str) -> bool {
        match self {
            Principal::Shared => true,
            Principal::Caller(c) => c.scope == Scope::Write && c.departments.iter().any(|d| d == department),
        }
    }

    fn name(&self) -> &str {
        match self {
            Principal::Shared => "the shared token",
            Principal::Caller(c) => &c.caller,
        }
    }

    /// AEGIS M4: the departments this principal may read; None = everything
    /// (the shared token, or a caller with `read_all`). Findings are visible
    /// when the scope includes `revenue_recovery`.
    fn read_scope(&self) -> Option<Vec<String>> {
        match self {
            Principal::Shared => None,
            Principal::Caller(c) if c.read_all => None,
            Principal::Caller(c) => {
                let mut v: Vec<String> = c.departments.iter().chain(c.read_departments.iter()).cloned().collect();
                v.sort();
                v.dedup();
                Some(v)
            }
        }
    }
}

/// Authentication configuration (sweep F-4, F-12).
struct Auth {
    /// The shared token, when it is accepted at all.
    shared: Option<String>,
    /// sha256(token) hex -> caller, when LEDGER_CALLERS_FILE is set.
    callers: Option<HashMap<String, Arc<Caller>>>,
}

impl Auth {
    fn authenticate(&self, token: &str) -> Option<Principal> {
        if let Some(callers) = &self.callers {
            // Looked up by the token's SHA-256: lookup timing can only reveal
            // something about a hash, never about the token itself.
            if let Some(c) = callers.get(&hex_sha256(token.as_bytes())) {
                // AEGIS L3: the file holds only a hash, so a caller token's
                // length can be checked only when it is presented. A short
                // token is refused even though its hash is configured.
                if token.len() < MIN_TOKEN_BYTES {
                    ledger_log!(
                        "ledger-rust: WARNING — caller {:?} presented a configured token of {} bytes; tokens must be \
                         at least {MIN_TOKEN_BYTES} bytes (refused, 401). Issue it a new token.",
                        c.caller,
                        token.len()
                    );
                    return None;
                }
                return Some(Principal::Caller(Arc::clone(c)));
            }
        }
        match &self.shared {
            Some(shared) if constant_time_eq(token, shared) => Some(Principal::Shared),
            _ => None,
        }
    }
}

fn hex_sha256(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

/// Sweep F-2: the end of the chain prefix already verified (`len` entries,
/// the last of which has `hash`), so GET /ledger/verify only verifies what is
/// new.
struct Verified {
    len: usize,
    hash: String,
}

struct App {
    ledger: Mutex<PersistentLedger>,
    gate: WriterPriority,
    reads: Semaphore,
    verified: Mutex<Verified>,
    auth: Auth,
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

fn handle_event(l: &mut PersistentLedger, input: EventInput) -> (u16, String) {
    let event_id = input.event_id.clone();
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

fn handle_append(l: &mut PersistentLedger, record: LedgerRecordInput, idempotent: bool) -> (u16, String) {
    let finding_id = record.finding_id.clone();
    let outcome = if idempotent {
        l.append_finding_idempotent(record)
    } else {
        l.append(record).map(AppendOutcome::Created)
    };
    match outcome {
        Ok(AppendOutcome::Created(entry)) => (201, serde_json::to_string(entry).unwrap()),
        Ok(AppendOutcome::Existing(entry)) => (200, serde_json::to_string(entry).unwrap()),
        Ok(AppendOutcome::Conflict(_)) => (
            409,
            serde_json::json!({
                "error": format!(
                    "finding_id {finding_id:?} is already recorded with different content; with an \
                     Idempotency-Key a finding_id can only ever describe one finding"
                ),
                "finding_id": finding_id,
            })
            .to_string(),
        ),
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

/// Sweep F-7: the opt-in. No `Idempotency-Key` header: Ok(false), the
/// unchanged non-idempotent append. One whose value is the body's finding_id:
/// Ok(true). Anything else is a 400 (a key that names a different finding
/// would make the caller believe in a guarantee it is not getting).
fn idempotency_requested(key: &Option<Result<String, ()>>, record: &LedgerRecordInput) -> Result<bool, (u16, String)> {
    match key {
        None => Ok(false),
        Some(Ok(k)) if *k == record.finding_id => Ok(true),
        Some(_) => Err(error_json(
            400,
            "Idempotency-Key must equal the body's finding_id (it makes the append idempotent on finding_id)"
                .to_string(),
        )),
    }
}

fn forbidden(msg: String) -> (u16, String) {
    error_json(403, msg)
}

/// Runs ledger work (lock + disk) on the bounded blocking pool so a slow
/// fsync never stalls the async workers that serve /health and move bytes.
/// A panic inside is fatal, exactly as it was for the old single-threaded
/// server (see `lock`).
async fn on_blocking<F>(app: &Arc<App>, f: F) -> (u16, String)
where
    F: FnOnce(&App) -> (u16, String) + Send + 'static,
{
    let app = Arc::clone(app);
    match tokio::task::spawn_blocking(move || f(&app)).await {
        Ok(r) => r,
        Err(e) => {
            ledger_log!("ledger-rust: FATAL — ledger operation panicked ({e}); exiting (fail closed)");
            std::process::exit(1);
        }
    }
}

/// A write: straight to the blocking pool (never behind a read slot), and
/// ahead of every reader's next chunk (sweep F-2).
async fn on_ledger_write<F>(app: &Arc<App>, f: F) -> (u16, String)
where
    F: FnOnce(&mut PersistentLedger) -> (u16, String) + Send + 'static,
{
    on_blocking(app, move |app| app.gate.write(&app.ledger, f)).await
}

/// A read: waits for one of the read slots (`read_slots`; async, so a waiting
/// reader holds no thread), then runs on the blocking pool.
async fn on_ledger_read<F>(app: &Arc<App>, f: F) -> (u16, String)
where
    F: FnOnce(&App) -> (u16, String) + Send + 'static,
{
    let _slot = match app.reads.acquire().await {
        Ok(slot) => slot,
        Err(_) => return error_json(503, "ledger-rust is shutting down".to_string()),
    };
    on_blocking(app, f).await
}

/// Copies entries `from..to` of the ledger in READ_CHUNK pieces, one short
/// lock hold each (writers first), handing each piece to `each` outside the
/// lock. Entries are immutable and append-only, so the pieces together are
/// exactly the snapshot `from..to` (sweep F-2). `each` returns false to stop.
fn for_each_chunk(app: &App, from: usize, to: usize, mut each: impl FnMut(usize, Vec<LedgerEntry>) -> bool) {
    let mut at = from;
    while at < to {
        let end = (at + READ_CHUNK).min(to);
        let chunk = app.gate.read(&app.ledger, |l| l.clone_range(at, end));
        if chunk.is_empty() || !each(at, chunk) {
            return;
        }
        at = end;
    }
}

/// GET /ledger/entries with no query string: the whole ledger, the same bytes
/// `serde_json::to_string(entries)` produced under the lock before sweep F-2.
fn entries_all(app: &App) -> (u16, String) {
    let len = app.gate.read(&app.ledger, |l| l.len());
    let mut out: Vec<u8> = Vec::with_capacity(2);
    out.push(b'[');
    for_each_chunk(app, 0, len, |at, chunk| {
        for (i, e) in chunk.iter().enumerate() {
            if at + i > 0 {
                out.push(b',');
            }
            serde_json::to_writer(&mut out, e).unwrap();
        }
        true
    });
    out.push(b']');
    (200, json_text(out))
}

/// The response text of serialized JSON (serde_json writes UTF-8 only).
fn json_text(bytes: Vec<u8>) -> String {
    String::from_utf8(bytes).expect("serde_json writes UTF-8")
}

/// The parsed query of GET /ledger/entries (sweep F-2).
#[derive(Debug, Default, PartialEq)]
struct EntriesQuery {
    after_seq: Option<u64>,
    limit: usize,
    department: Option<String>,
    event_type: Option<String>,
}

fn is_slug(v: &str) -> bool {
    !v.is_empty() && v.len() <= 64 && v.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
}

/// Strict: only `after_seq`, `limit`, `department`, `event_type`, each at
/// most once, each with a plain value (digits / `[a-z0-9_]`); anything else is
/// a 400, so a typo never silently widens a read.
fn parse_entries_query(query: &str) -> Result<EntriesQuery, String> {
    let mut q = EntriesQuery { limit: DEFAULT_PAGE, ..EntriesQuery::default() };
    let mut seen: Vec<&str> = Vec::new();
    for pair in query.split('&') {
        let (k, v) = pair.split_once('=').ok_or_else(|| format!("query parameter {pair:?} has no value"))?;
        if seen.contains(&k) {
            return Err(format!("query parameter {k:?} given twice"));
        }
        seen.push(k);
        let digits = |v: &str| -> Result<u64, String> {
            if v.is_empty() || v.len() > 20 || !v.bytes().all(|b| b.is_ascii_digit()) {
                return Err(format!("{k} must be a non-negative integer"));
            }
            v.parse::<u64>().map_err(|_| format!("{k} is out of range"))
        };
        match k {
            "after_seq" => q.after_seq = Some(digits(v)?),
            "limit" => {
                let n = digits(v)?;
                if n == 0 || n > MAX_PAGE as u64 {
                    return Err(format!("limit must be 1-{MAX_PAGE}"));
                }
                q.limit = n as usize;
            }
            "department" | "event_type" => {
                if !is_slug(v) {
                    return Err(format!("{k} must be 1-64 characters of [a-z0-9_]"));
                }
                if k == "department" {
                    q.department = Some(v.to_string());
                } else {
                    q.event_type = Some(v.to_string());
                }
            }
            other => {
                return Err(format!(
                    "unknown query parameter {other:?} (accepted: after_seq, limit, department, event_type)"
                ))
            }
        }
    }
    Ok(q)
}

/// GET /ledger/entries?... (and a scoped caller's GET /ledger/entries):
/// entries with seq > after_seq, in seq order, at most `max` of them, that
/// `filter` selects (department / event_type match events only; a finding has
/// neither, so a filtered read never returns one; a read scope limits it to
/// the caller's departments, AEGIS M4). A JSON array of entries, as without a
/// query. Fewer than `limit` entries means the end of the ledger was reached;
/// otherwise the next page is `after_seq=<seq of the last entry>`.
///
/// AEGIS L5: the entries come from the per-department / per-event-type index
/// (`PersistentLedger::select`), at most READ_CHUNK per lock hold, so a page
/// costs O(page + log n) per list read, not a scan of every entry after
/// `after_seq`.
fn entries_selected(app: &App, after_seq: Option<u64>, max: usize, filter: &EntryFilter) -> (u16, String) {
    let len = app.gate.read(&app.ledger, |l| l.len());
    let mut from = match after_seq {
        None => 0,
        Some(s) => usize::try_from(s).map(|s| s.saturating_add(1)).unwrap_or(usize::MAX).min(len),
    };
    let mut out: Vec<u8> = vec![b'['];
    let mut n = 0usize;
    while n < max && from < len {
        let want = (max - n).min(READ_CHUNK);
        let chunk = app.gate.read(&app.ledger, |l| l.select(filter, from, len, want));
        for e in &chunk {
            if n > 0 {
                out.push(b',');
            }
            serde_json::to_writer(&mut out, e).unwrap();
            n += 1;
        }
        match chunk.last() {
            Some(last) if chunk.len() == want => from = last.seq() as usize + 1,
            _ => break,
        }
    }
    out.push(b']');
    (200, json_text(out))
}

/// Verifies entries `from..to`, the first of which must chain to `prev`
/// (sweep F-2: the same per-entry check as `Ledger::verify_chain`, in chunks
/// copied under short lock holds). Ok(hash of entry to-1, or `prev`).
fn verify_range(app: &App, from: usize, to: usize, prev: String) -> Result<String, LedgerError> {
    let mut prev = prev;
    let mut result = Ok(());
    for_each_chunk(app, from, to, |at, chunk| {
        for (i, e) in chunk.iter().enumerate() {
            if let Err(err) = verify_entry(e, (at + i) as u64, &prev) {
                result = Err(err);
                return false;
            }
            prev = e.hash().to_string();
        }
        true
    });
    result.map(|()| prev)
}

/// GET /ledger/verify[?full=1]. Incremental by default: the prefix verified
/// at start (and by every successful verify since) is not verified again.
/// Same responses as before: 200 {"valid":true,"entries":N} or
/// 409 {"valid":false,"error":...}.
///
/// AEGIS L4: `?full=1` re-verifies the whole in-memory chain AND re-reads the
/// log from disk (`verify_log_file`: every acknowledged line parsed with the
/// open-time rules and re-hashed from the genesis hash), then checks that the
/// disk chain ends at the in-memory head of the same snapshot. Both run
/// outside the ledger mutex (the snapshot — length, head hash, acknowledged
/// byte length — is taken in one short lock hold); the disk read streams line
/// by line, so memory is bounded by the longest line.
fn verify(app: &App, full: bool) -> (u16, String) {
    let (len, head_hash, log_bytes, path) =
        app.gate.read(&app.ledger, |l| (l.len(), l.head().head_hash, l.log_bytes(), l.path().to_path_buf()));
    let (from, prev) = if full {
        (0, genesis_hash())
    } else {
        let v = app.verified.lock().unwrap_or_else(PoisonError::into_inner);
        (v.len.min(len), v.hash.clone())
    };
    match verify_range(app, from, len, prev) {
        Ok(hash) => {
            if full {
                if hash != head_hash {
                    return (
                        409,
                        serde_json::json!({"valid": false, "error": format!(
                            "in-memory chain ends at {hash}, the head is {head_hash}"
                        )})
                        .to_string(),
                    );
                }
                if let Err(e) = verify_log_file(&path, log_bytes, len, &head_hash) {
                    ledger_log!("ledger-rust: CRITICAL — GET /ledger/verify?full=1: the log on disk does not verify: {e}");
                    return (
                        409,
                        serde_json::json!({"valid": false, "error": format!("log on disk: {e}")}).to_string(),
                    );
                }
            }
            let mut v = app.verified.lock().unwrap_or_else(PoisonError::into_inner);
            if len > v.len {
                *v = Verified { len, hash };
            }
            (200, serde_json::json!({"valid": true, "entries": len}).to_string())
        }
        Err(e) => (409, serde_json::json!({"valid": false, "error": format!("{e:?}")}).to_string()),
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
    let principal = if url == "/health" {
        None
    } else {
        match extract_bearer_token(&request).and_then(|t| app.auth.authenticate(&t)) {
            Some(p) => Some(p),
            None => return unauthorized_response(),
        }
    };

    // Sweep F-2: only GET /ledger/entries and GET /ledger/verify take a query
    // string; every other URL is matched whole, exactly as before.
    let (path, query) = match url.split_once('?') {
        Some((p, q)) if p == "/ledger/entries" || p == "/ledger/verify" => (p, Some(q)),
        _ => (url.as_str(), None),
    };

    // Sweep F-4: a read-only caller never reaches a write handler (403 before
    // any body byte is read).
    if method == Method::POST && matches!(path, "/ledger/events" | "/ledger/append") {
        if let Some(p) = &principal {
            if !p.can_write() {
                return forbidden(format!("caller {:?} has a read-only token", p.name()));
            }
        }
    }

    match (method, path, query) {
        (Method::GET, "/health", None) => (200, serde_json::json!({"status": "ok", "service": "ledger-rust"}).to_string()),

        (Method::GET, "/ledger/entries", q) => {
            let scope = principal.as_ref().expect("authenticated above").read_scope();
            let q = match q.map(parse_entries_query) {
                None => None,
                Some(Err(e)) => return error_json(400, e),
                Some(Ok(q)) => Some(q),
            };
            // AEGIS M4: a filter for a department outside the read scope is
            // refused, never answered with an (indistinguishable) empty page.
            if let (Some(scope), Some(Some(d))) = (&scope, q.as_ref().map(|q| &q.department)) {
                if !scope.contains(d) {
                    return forbidden(format!(
                        "caller {:?} may not read department {d:?} (its read scope: {})",
                        principal.as_ref().map(Principal::name).unwrap_or_default(),
                        scope.join(",")
                    ));
                }
            }
            match (q, scope) {
                // The whole ledger, byte-identical to every earlier binary.
                (None, None) => on_ledger_read(&app, entries_all).await,
                (q, scope) => {
                    let (after_seq, max, department, event_type) = match q {
                        None => (None, usize::MAX, None, None),
                        Some(q) => (q.after_seq, q.limit, q.department, q.event_type),
                    };
                    let filter = EntryFilter { department, event_type, scope };
                    on_ledger_read(&app, move |app| entries_selected(app, after_seq, max, &filter)).await
                }
            }
        }

        (Method::GET, "/ledger/verify", q) => {
            let full = match q {
                None | Some("full=0") => false,
                Some("full=1") => true,
                Some(other) => return error_json(400, format!("unknown query {other:?} (accepted: full=1)")),
            };
            on_ledger_read(&app, move |app| verify(app, full)).await
        }

        (Method::GET, "/ledger/head", None) => {
            on_blocking(&app, |app| (200, serde_json::to_string(&app.gate.read(&app.ledger, |l| l.head())).unwrap()))
                .await
        }

        (Method::POST, "/ledger/events", None) => {
            let principal = principal.expect("authenticated above");
            match read_body(request).await.and_then(|b| parse_event(&b)) {
                Err(resp) => resp,
                Ok(input) if !principal.may_write_department(&input.department) => forbidden(format!(
                    "caller {:?} may not write events for department {:?}",
                    principal.name(),
                    input.department
                )),
                Ok(input) => on_ledger_write(&app, move |l| handle_event(l, input)).await,
            }
        }

        (Method::POST, "/ledger/append", None) => {
            let principal = principal.expect("authenticated above");
            let key = request
                .headers()
                .get("idempotency-key")
                .map(|v| v.to_str().map(str::to_string).map_err(|_| ()));
            match read_body(request).await.and_then(|b| parse_append(&b)) {
                Err(resp) => resp,
                Ok(_) if !principal.may_write_department(FINDINGS_DEPARTMENT) => forbidden(format!(
                    "caller {:?} may not write findings (department {FINDINGS_DEPARTMENT:?})",
                    principal.name()
                )),
                Ok(record) => match idempotency_requested(&key, &record) {
                    Err(resp) => resp,
                    Ok(idempotent) => on_ledger_write(&app, move |l| handle_append(l, record, idempotent)).await,
                },
            }
        }

        _ => (404, serde_json::json!({"error": "not found"}).to_string()),
    }
}

/// Graceful close (fix wave 21, N20-M-1): shut the write side down (FIN after
/// whatever response was written), then read and discard what the peer still
/// sends — at most DRAIN_MAX_BYTES within DRAIN_TIMEOUT — and close. Closing
/// with unread bytes in the receive queue makes the kernel send RST, which a
/// client may see before (macOS) or after (Linux) the response it was sent.
async fn graceful_close(mut stream: TcpStream) {
    let _ = tokio::time::timeout(DRAIN_TIMEOUT, stream.shutdown()).await;
    let mut buf = [0u8; 8192];
    let mut left = DRAIN_MAX_BYTES;
    let _ = tokio::time::timeout(DRAIN_TIMEOUT, async {
        while left > 0 {
            let want = left.min(buf.len());
            match stream.read(&mut buf[..want]).await {
                Ok(0) | Err(_) => break,
                Ok(n) => left -= n,
            }
        }
    })
    .await;
}

/// The service future, boxed: `poll_without_shutdown` needs an `Unpin` future.
type HandlerFuture = Pin<Box<dyn Future<Output = Result<Response<Body>, Infallible>> + Send>>;

/// Serves exactly one request on `stream` within REQUEST_DEADLINE and hands the
/// socket back for the graceful close. hyper runs WITHOUT shutting the socket
/// down itself (`poll_without_shutdown`), so the socket comes back however the
/// connection ended — the answer written, the deadline, or a protocol error
/// hyper answered itself. Ledger work already handed to the blocking pool
/// still completes (it is never torn halfway).
async fn serve_connection(stream: TcpStream, app: Arc<App>) -> TcpStream {
    let service = service_fn(move |request| {
        let app = Arc::clone(&app);
        let fut: HandlerFuture = Box::pin(async move { Ok::<_, Infallible>(json_response(handle(request, app).await)) });
        fut
    });
    let mut conn = http1::Builder::new()
        .timer(TokioTimer::new())
        .header_read_timeout(HEADER_READ_TIMEOUT)
        .keep_alive(false)
        .max_buf_size(MAX_READ_BUF)
        .serve_connection(TokioIo::new(stream), service);
    // Done, a connection error or the deadline: in every case the socket is
    // taken back and closed gracefully (anything hyper had not flushed by the
    // deadline is dropped, exactly as before).
    let _ = tokio::time::timeout(REQUEST_DEADLINE, std::future::poll_fn(|cx| conn.poll_without_shutdown(cx))).await;
    conn.into_parts().io.into_inner()
}

/// Closes an answered socket: gracefully when a drain slot is free, else at
/// once (FIN; the kernel resets it if request bytes are still unread).
async fn close_answered(mut stream: TcpStream, drains: Arc<Semaphore>) {
    match drains.try_acquire_owned() {
        Ok(permit) => {
            graceful_close(stream).await;
            drop(permit);
        }
        Err(_) => {
            let _ = tokio::time::timeout(SHED_WRITE_TIMEOUT, stream.shutdown()).await;
        }
    }
}

/// Over the connection cap: answer 503 at once (bounded by
/// SHED_WRITE_TIMEOUT) without reading the request, then close gracefully
/// (the unread request is drained, bounded) when a drain slot is free.
async fn shed(mut stream: TcpStream, drains: Arc<Semaphore>) {
    let body = serde_json::json!({"error": "ledger-rust is at its connection limit; retry shortly"}).to_string();
    let resp = format!(
        "HTTP/1.1 503 Service Unavailable\r\n{CONTENT_TYPE}: application/json\r\n{RETRY_AFTER}: 1\r\n\
         {CONNECTION}: close\r\n{CONTENT_LENGTH}: {}\r\n\r\n{body}",
        body.len()
    );
    let written = tokio::time::timeout(SHED_WRITE_TIMEOUT, stream.write_all(resp.as_bytes())).await;
    if matches!(written, Ok(Ok(()))) {
        close_answered(stream, drains).await;
    }
}

async fn serve(listener: TcpListener, app: Arc<App>, max_connections: usize) {
    let slots = Arc::new(Semaphore::new(max_connections));
    let drains = Arc::new(Semaphore::new(DRAINS_MAX));
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
                let drains = Arc::clone(&drains);
                tokio::spawn(async move {
                    let stream = serve_connection(stream, app).await;
                    drop(slot); // answered: the slot is free before the (bounded) drain
                    close_answered(stream, drains).await;
                });
            }
            Err(_) => {
                tokio::spawn(shed(stream, Arc::clone(&drains)));
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

/// The port file this server wrote: its NAME (NUL-terminated, for the signal
/// handler), the descriptor of the directory it was written in (walked without
/// following any symlink; kept open), and the device/inode of the file it
/// renamed into place. Set once.
static PORT_FILE_NAME: AtomicPtr<libc::c_char> = AtomicPtr::new(std::ptr::null_mut());
/// Fix wave 24 (F2): the temp name while the publish is in progress (null otherwise), so a stop signal that
/// lands mid-publish removes it too.
static PORT_FILE_TMP: AtomicPtr<libc::c_char> = AtomicPtr::new(std::ptr::null_mut());
static PORT_FILE_DIR_FD: AtomicI32 = AtomicI32::new(-1);
static PORT_FILE_DEV: AtomicU64 = AtomicU64::new(0);
static PORT_FILE_INO: AtomicU64 = AtomicU64::new(0);
/// The signals that remove the port file before the process dies of them.
const STOP_SIGNALS: [libc::c_int; 4] = [libc::SIGTERM, libc::SIGINT, libc::SIGHUP, libc::SIGQUIT];

fn random_hex(n: usize) -> std::io::Result<String> {
    let mut bytes = vec![0u8; n];
    std::fs::File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

fn invalid(msg: String) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::InvalidInput, msg)
}

/// A directory descriptor this module owns (closed on drop unless kept).
struct DirFd(libc::c_int);

impl Drop for DirFd {
    fn drop(&mut self) {
        if self.0 >= 0 {
            // SAFETY: closing a descriptor this struct owns.
            unsafe {
                libc::close(self.0);
            }
        }
    }
}

impl DirFd {
    fn keep(mut self) -> libc::c_int {
        std::mem::replace(&mut self.0, -1)
    }
}

#[cfg(target_os = "linux")]
const DIR_OPEN_FLAGS: libc::c_int = libc::O_PATH | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC;
#[cfg(not(target_os = "linux"))]
const DIR_OPEN_FLAGS: libc::c_int = libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC;

fn c_name(os: &std::ffi::OsStr) -> std::io::Result<CString> {
    use std::os::unix::ffi::OsStrExt;
    CString::new(os.as_bytes()).map_err(|_| invalid("NUL in the port file path".to_string()))
}

fn fstat_fd(fd: libc::c_int) -> std::io::Result<libc::stat> {
    // SAFETY: `st` is plain data; `fd` is a descriptor this module opened.
    unsafe {
        let mut st: libc::stat = std::mem::zeroed();
        if libc::fstat(fd, &mut st) != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(st)
    }
}

/// Fix wave 23 (N22-C-3): opens the parent directory of the port file one
/// component at a time, never following a symlink (O_NOFOLLOW on every
/// component; each descriptor checked to be a directory). A symlink anywhere
/// in the parent path is refused, naming the component.
fn open_parent_nofollow(parent: &Path) -> std::io::Result<DirFd> {
    use std::path::Component;
    let start: &[u8] = if parent.is_absolute() { b"/\0" } else { b".\0" };
    // SAFETY: a NUL-terminated literal path; the result is checked.
    let fd = unsafe { libc::open(start.as_ptr() as *const libc::c_char, DIR_OPEN_FLAGS & !libc::O_NOFOLLOW) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    let mut cur = DirFd(fd);
    for comp in parent.components() {
        let name = match comp {
            Component::RootDir | Component::CurDir | Component::Prefix(_) => continue,
            Component::ParentDir => std::ffi::OsStr::new(".."),
            Component::Normal(n) => n,
        };
        let c = c_name(name)?;
        // SAFETY: `cur.0` is an open directory descriptor; `c` is NUL-terminated.
        let next = unsafe { libc::openat(cur.0, c.as_ptr(), DIR_OPEN_FLAGS) };
        if next < 0 {
            let e = std::io::Error::last_os_error();
            return Err(match e.raw_os_error() {
                Some(libc::ELOOP) | Some(libc::ENOTDIR) => invalid(format!(
                    "the port file's parent directory {parent:?} reaches {name:?} through a symlink or a non-directory \
                     (refused: a symlink in the parent path is never followed; pass the canonical path)"
                )),
                _ => e,
            });
        }
        let next = DirFd(next);
        let st = fstat_fd(next.0)?;
        if (st.st_mode & libc::S_IFMT) != libc::S_IFDIR {
            return Err(invalid(format!(
                "the port file's parent directory {parent:?} reaches {name:?}, which is a symlink or not a directory \
                 (refused: a symlink in the parent path is never followed; pass the canonical path)"
            )));
        }
        cur = next;
    }
    Ok(cur)
}

/// The target inside `dir`: absent, or a regular file (a symlink or anything
/// else is refused).
fn check_target(dir: libc::c_int, name: &CString) -> std::io::Result<()> {
    // SAFETY: `st` is plain data; `dir` is an open directory descriptor; `name` is NUL-terminated.
    unsafe {
        let mut st: libc::stat = std::mem::zeroed();
        if libc::fstatat(dir, name.as_ptr(), &mut st, libc::AT_SYMLINK_NOFOLLOW) != 0 {
            let e = std::io::Error::last_os_error();
            return if e.raw_os_error() == Some(libc::ENOENT) { Ok(()) } else { Err(e) };
        }
        match st.st_mode & libc::S_IFMT {
            libc::S_IFLNK => Err(invalid("the port file path is a symlink (refused: it is never followed or replaced)".to_string())),
            libc::S_IFREG => Ok(()),
            _ => Err(invalid("the port file path exists and is not a regular file".to_string())),
        }
    }
}

/// The directory descriptor (walked without following symlinks) and the file
/// name of the port file `path`.
fn port_file_location(path: &str) -> std::io::Result<(DirFd, CString)> {
    let target = Path::new(path);
    let name = target.file_name().ok_or_else(|| invalid("the port file path names no file".to_string()))?;
    let parent = match target.parent() {
        Some(d) if !d.as_os_str().is_empty() => d.to_path_buf(),
        _ => PathBuf::from("."),
    };
    let dir = open_parent_nofollow(&parent)?;
    let c = c_name(name)?;
    check_target(dir.0, &c)?;
    Ok((dir, c))
}

/// Refuses, at start-up, a port file path the server would refuse to publish
/// (a symlinked parent component or target, a target that is not a regular file).
fn check_port_file_path(path: &str) -> std::io::Result<()> {
    port_file_location(path).map(|_| ())
}

/// The stop signals as a signal set.
fn stop_signal_set() -> libc::sigset_t {
    // SAFETY: plain data initialised by sigemptyset/sigaddset.
    unsafe {
        let mut set: libc::sigset_t = std::mem::zeroed();
        libc::sigemptyset(&mut set);
        for sig in STOP_SIGNALS {
            libc::sigaddset(&mut set, sig);
        }
        set
    }
}

/// Blocks the stop signals in the calling thread; returns the mask to restore. A stop signal sent meanwhile stays
/// pending and is delivered when the mask is restored.
fn block_stop_signals() -> libc::sigset_t {
    let set = stop_signal_set();
    // SAFETY: `set` and `old` are valid sigsets; pthread_sigmask only changes this thread's mask.
    unsafe {
        let mut old: libc::sigset_t = std::mem::zeroed();
        libc::pthread_sigmask(libc::SIG_BLOCK, &set, &mut old);
        old
    }
}

fn restore_signal_mask(old: &libc::sigset_t) {
    // SAFETY: `old` is the mask block_stop_signals returned.
    unsafe {
        libc::pthread_sigmask(libc::SIG_SETMASK, old, std::ptr::null_mut());
    }
}

/// Writes `port` to `path` (fix wave 22, G7; wave 23, N22-C-3; wave 24, F2): the parent is walked without
/// following symlinks; a temp file in that directory, `.<name>.tmp-<16 random hex>`, is created (openat
/// O_CREAT|O_EXCL|O_NOFOLLOW, mode 0600), written and fsynced, then renamed over `<name>` within the same
/// directory descriptor (renameat: atomic; a reader never sees a partial number; a link at the destination is
/// replaced, never followed).
///
/// Fix wave 24 (AEGIS N23-S-2): the stop signals are BLOCKED across the whole publish (so none is handled half
/// way: one sent meanwhile stays pending and is handled once the port file is in place and armed), and the removal
/// is armed BEFORE anything is written: the directory, the temp name and the published name are recorded first,
/// the handlers installed, and the device/inode of the temp file recorded before the rename (the rename keeps the
/// inode). The handler removes the temp name (while the publish is in progress) and the published name (only if
/// it is still the file this server wrote). Before, the handlers were installed after the rename: a SIGTERM while
/// the temp file existed left it behind (AEGIS: 297/300), one between the rename and the handlers left the port
/// file. Worker threads never take a stop signal (main blocks them before the runtime starts its threads, which
/// inherit the mask), so blocking them here in the main thread blocks them for the process.
fn publish_port_file(path: &str, port: u16) -> std::io::Result<PortFileGuard> {
    let (dir, name) = port_file_location(path)?;
    let tmp = c_name(std::ffi::OsStr::new(&format!(".{}.tmp-{}", name.to_string_lossy(), random_hex(8)?)))?;
    let old = block_stop_signals();
    let dir = dir.keep();
    PORT_FILE_DIR_FD.store(dir, Ordering::SeqCst);
    PORT_FILE_NAME.store(name.clone().into_raw(), Ordering::SeqCst);
    PORT_FILE_TMP.store(tmp.clone().into_raw(), Ordering::SeqCst);
    // SAFETY: installing a handler that only calls async-signal-safe functions.
    unsafe {
        let handler = on_stop_signal as extern "C" fn(libc::c_int) as libc::sighandler_t;
        for sig in STOP_SIGNALS {
            libc::signal(sig, handler);
        }
    }
    let written = write_and_rename(dir, &tmp, &name, port);
    PORT_FILE_TMP.store(std::ptr::null_mut(), Ordering::SeqCst); // the temp name is gone (renamed or removed)
    restore_signal_mask(&old);                                     // a pending stop signal is handled from here on
    written.map(|_| PortFileGuard)
}

/// The write itself (stop signals blocked by the caller): temp file, fsync, device/inode recorded, rename.
fn write_and_rename(dir: libc::c_int, tmp: &CString, name: &CString, port: u16) -> std::io::Result<()> {
    // SAFETY: `dir` is an open directory descriptor; `tmp` is NUL-terminated; mode passed for O_CREAT.
    let fd = unsafe {
        libc::openat(dir, tmp.as_ptr(), libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                     0o600 as libc::c_uint)
    };
    if fd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    // SAFETY: `fd` was just opened here and is owned by the File from now on.
    let mut file = unsafe { <std::fs::File as std::os::unix::io::FromRawFd>::from_raw_fd(fd) };
    let written = file.write_all(format!("{port}\n").as_bytes()).and_then(|_| file.sync_all()).and_then(|_| file.metadata());
    drop(file);
    let unlink_tmp = || {
        // SAFETY: removing the temp name this function created, relative to `dir`.
        unsafe {
            libc::unlinkat(dir, tmp.as_ptr(), 0);
        }
    };
    let meta = match written {
        Ok(m) => m,
        Err(e) => {
            unlink_tmp();
            return Err(e);
        }
    };
    PORT_FILE_DEV.store(meta.dev(), Ordering::SeqCst);
    PORT_FILE_INO.store(meta.ino(), Ordering::SeqCst);
    let renamed = check_target(dir, name).and_then(|_| {
        // SAFETY: both names are NUL-terminated and relative to the same open directory descriptor.
        if unsafe { libc::renameat(dir, tmp.as_ptr(), dir, name.as_ptr()) } != 0 {
            Err(std::io::Error::last_os_error())
        } else {
            Ok(())
        }
    });
    if let Err(e) = renamed {
        unlink_tmp();
        return Err(e);
    }
    Ok(())
}

/// Removes, in the directory the port file was written in, the temp name while a publish is in progress, and the
/// port file if it is still the one this server wrote. Called from the signal handler: only async-signal-safe
/// calls (fstatat, unlinkat).
fn remove_own_port_file() {
    let dir = PORT_FILE_DIR_FD.load(Ordering::SeqCst);
    if dir < 0 {
        return;
    }
    let tmp = PORT_FILE_TMP.load(Ordering::SeqCst);
    if !tmp.is_null() {
        // SAFETY: `tmp` points at a leaked, NUL-terminated CString; `dir` is a descriptor kept open for the process.
        unsafe {
            libc::unlinkat(dir, tmp, 0);
        }
    }
    let name = PORT_FILE_NAME.load(Ordering::SeqCst);
    if name.is_null() {
        return;
    }
    // SAFETY: `name` points at a leaked, NUL-terminated CString that lives for the process; `dir` is a descriptor
    // kept open for the process; `st` is plain data.
    // The casts are for portability: `st_ino` is `u64` on macOS and the `ino_t` alias on Linux, `st_dev` is `i32` on
    // macOS (fix wave 26b: clippy's unnecessary_cast fired on macos-26 only).
    #[allow(clippy::unnecessary_cast)]
    unsafe {
        let mut st: libc::stat = std::mem::zeroed();
        if libc::fstatat(dir, name, &mut st, libc::AT_SYMLINK_NOFOLLOW) == 0
            && (st.st_mode & libc::S_IFMT) == libc::S_IFREG
            && st.st_dev as u64 == PORT_FILE_DEV.load(Ordering::SeqCst)
            && st.st_ino as u64 == PORT_FILE_INO.load(Ordering::SeqCst)
        {
            libc::unlinkat(dir, name, 0);
        }
    }
}

extern "C" fn on_stop_signal(sig: libc::c_int) {
    remove_own_port_file();
    // SAFETY: restoring the default disposition and re-raising is async-signal-safe; the process then ends
    // exactly as it did before a handler existed (killed by `sig`).
    unsafe {
        libc::signal(sig, libc::SIG_DFL);
        libc::raise(sig);
    }
}

/// Removes the port file when `main` returns normally.
struct PortFileGuard;

impl Drop for PortFileGuard {
    fn drop(&mut self) {
        remove_own_port_file();
    }
}

/// Logs a refusal and exits 1 (fail closed, never a panic).
fn refuse(msg: String) -> ! {
    ledger_log!("ledger-rust: REFUSING TO START — {msg}");
    std::process::exit(1);
}

/// An operator switch: unset, "" or "0" is off, "1" is on; anything else
/// refuses the start (a "true" that silently meant "off" would hide intent).
fn load_flag(name: &str) -> bool {
    match std::env::var(name) {
        Err(_) => false,
        Ok(v) => match v.as_str() {
            "" | "0" => false,
            "1" => true,
            other => refuse(format!("{name}={other:?} must be 1 (on) or 0/unset (off)")),
        },
    }
}

/// LEDGER_SERVICE_TOKEN: required (unless per-caller tokens replace it,
/// below) and at least MIN_TOKEN_BYTES bytes (sweep F-12: a 1-character token
/// used to be accepted).
fn load_shared_token(required: bool) -> Option<String> {
    match std::env::var("LEDGER_SERVICE_TOKEN") {
        Ok(t) if !t.is_empty() => {
            if t.len() < MIN_TOKEN_BYTES {
                refuse(format!(
                    "LEDGER_SERVICE_TOKEN is {} bytes; the evidence ledger requires at least {MIN_TOKEN_BYTES} \
                     (e.g. 32 random bytes, hex-encoded). Set the same longer value wherever this service is \
                     called from.",
                    t.len()
                ));
            }
            Some(t)
        }
        _ if !required => None,
        _ => refuse(
            "LEDGER_SERVICE_TOKEN is not set. This is the evidence ledger; it does not start \
             unauthenticated. Set LEDGER_SERVICE_TOKEN to a shared secret before starting ledger-rust, and \
             set the identical value wherever this service is called from."
                .to_string(),
        ),
    }
}

/// The callers map as written, keys in file order with duplicates kept
/// (AEGIS L1: serde's HashMap keeps the last of two equal keys silently).
struct CallersFile(Vec<(String, Caller)>);

impl<'de> serde::Deserialize<'de> for CallersFile {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> serde::de::Visitor<'de> for V {
            type Value = CallersFile;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a JSON object mapping token hashes to callers")
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(self, mut m: A) -> Result<CallersFile, A::Error> {
                let mut v = Vec::new();
                while let Some((k, c)) = m.next_entry::<String, Caller>()? {
                    v.push((k, c));
                }
                Ok(CallersFile(v))
            }
        }
        d.deserialize_map(V)
    }
}

/// Sweep F-4: LEDGER_CALLERS_FILE, a JSON object mapping the lowercase hex
/// SHA-256 of a caller's bearer token to
/// `{"caller": "<name>", "departments": ["<dept>", ...], "scope": "write"|"read"}`.
/// Every malformed entry refuses the start (fail closed): a hash that is not
/// 64 lowercase hex characters, an empty caller name, a department that is
/// not `[a-z0-9_]{1,64}` (the event `department` rule), a write caller with
/// no departments, an unknown field, an empty file.
fn load_callers(path: &str) -> HashMap<String, Arc<Caller>> {
    let meta = std::fs::metadata(path).unwrap_or_else(|e| refuse(format!("LEDGER_CALLERS_FILE={path:?}: {e}")));
    if meta.len() > MAX_CALLERS_FILE_BYTES {
        refuse(format!("LEDGER_CALLERS_FILE={path:?} is larger than {MAX_CALLERS_FILE_BYTES} bytes"));
    }
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| refuse(format!("LEDGER_CALLERS_FILE={path:?}: {e}")));
    let raw: CallersFile = serde_json::from_str(&text)
        .unwrap_or_else(|e| refuse(format!("LEDGER_CALLERS_FILE={path:?} is not a callers map: {e}")));
    let raw = raw.0;
    if raw.is_empty() {
        refuse(format!("LEDGER_CALLERS_FILE={path:?} names no callers"));
    }
    let mut out = HashMap::new();
    for (hash, c) in raw {
        if hash.len() != 64 || !hash.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
            refuse(format!(
                "LEDGER_CALLERS_FILE: key {hash:?} is not the lowercase hex SHA-256 of a token (64 characters)"
            ));
        }
        if c.caller.trim().is_empty() || c.caller.len() > 128 || c.caller.chars().any(char::is_control) {
            refuse(format!("LEDGER_CALLERS_FILE: caller name {:?} must be 1-128 printable characters", c.caller));
        }
        if let Some(d) = c.departments.iter().chain(c.read_departments.iter()).find(|d| !is_slug(d)) {
            refuse(format!("LEDGER_CALLERS_FILE: caller {:?}: department {d:?} is not [a-z0-9_]{{1,64}}", c.caller));
        }
        if c.scope == Scope::Write && c.departments.is_empty() {
            refuse(format!("LEDGER_CALLERS_FILE: write caller {:?} lists no departments", c.caller));
        }
        // AEGIS M4: every caller's read scope is explicit.
        if c.read_all && !c.read_departments.is_empty() {
            refuse(format!(
                "LEDGER_CALLERS_FILE: caller {:?} sets both read_all and read_departments (one or the other)",
                c.caller
            ));
        }
        if c.scope == Scope::Read && !c.read_all && c.departments.is_empty() {
            refuse(format!(
                "LEDGER_CALLERS_FILE: read caller {:?} reads nothing: list its \"departments\" or set \"read_all\": true",
                c.caller
            ));
        }
        if c.scope == Scope::Read && !c.read_departments.is_empty() {
            refuse(format!(
                "LEDGER_CALLERS_FILE: read caller {:?}: read_departments is for write callers (a read caller's \
                 \"departments\" are the departments it reads)",
                c.caller
            ));
        }
        // AEGIS L1: the same token hash twice (two callers sharing one token,
        // or a copy-paste) is refused: which entry won would be arbitrary.
        if out.contains_key(&hash) {
            refuse(format!(
                "LEDGER_CALLERS_FILE: token hash {hash} is listed more than once (callers {:?} and {:?}); every \
                 caller needs its own token",
                out.get(&hash).map(|c: &Arc<Caller>| c.caller.clone()).unwrap_or_default(),
                c.caller
            ));
        }
        out.insert(hash, Arc::new(c));
    }
    out
}

/// Sweep F-4 / F-12: the authentication configuration.
fn load_auth() -> Auth {
    // AEGIS L2: set-but-empty is a misconfiguration (a templating gap), not
    // "unset": it refuses the start instead of falling back to the shared token.
    let callers_file = match std::env::var("LEDGER_CALLERS_FILE") {
        Err(std::env::VarError::NotPresent) => None,
        Err(std::env::VarError::NotUnicode(_)) => refuse("LEDGER_CALLERS_FILE is not valid UTF-8".to_string()),
        Ok(v) if v.trim().is_empty() => refuse(
            "LEDGER_CALLERS_FILE is set but empty; unset it to use the shared token, or point it at the callers file"
                .to_string(),
        ),
        Ok(v) => Some(v),
    };
    let allow_shared = load_flag("LEDGER_ALLOW_SHARED_TOKEN");
    match callers_file {
        None => {
            let shared = load_shared_token(true);
            ledger_log!(
                "ledger-rust: WARNING — LEDGER_CALLERS_FILE is not set: every holder of the shared \
                 LEDGER_SERVICE_TOKEN can read everything and write events for ANY department (a department is \
                 self-declared). Configure per-caller tokens with department scopes (LEDGER_CALLERS_FILE) for \
                 production."
            );
            if allow_shared {
                ledger_log!("ledger-rust: note — LEDGER_ALLOW_SHARED_TOKEN=1 has no effect without LEDGER_CALLERS_FILE");
            }
            Auth { shared, callers: None }
        }
        Some(path) => {
            let callers = load_callers(&path);
            let shared = if allow_shared {
                let t = load_shared_token(true);
                ledger_log!(
                    "ledger-rust: WARNING — LEDGER_ALLOW_SHARED_TOKEN=1: the legacy shared LEDGER_SERVICE_TOKEN is \
                     still accepted with full access (every department) alongside the {} per-caller token(s)",
                    callers.len()
                );
                t
            } else {
                if load_shared_token(false).is_some() {
                    ledger_log!(
                        "ledger-rust: note — LEDGER_SERVICE_TOKEN is set but NOT accepted: per-caller tokens are \
                         configured and LEDGER_ALLOW_SHARED_TOKEN is not 1"
                    );
                }
                None
            };
            let mut names: Vec<String> = callers
                .values()
                .map(|c| {
                    let reads = match Principal::Caller(Arc::clone(c)).read_scope() {
                        None => "all".to_string(),
                        Some(d) => d.join(","),
                    };
                    format!("{} ({:?}: {}; reads: {reads})", c.caller, c.scope, c.departments.join(","))
                })
                .collect();
            names.sort();
            ledger_log!("ledger-rust: per-caller tokens from {path}: {}", names.join("; "));
            Auth { shared, callers: Some(callers) }
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

/// AEGIS M2/M3: an operator override bound to one exact ledger state. Unset,
/// "" or "0" is off; otherwise the value must have the shape `valid` accepts
/// (the refusal it answers prints the exact value), or the start is refused.
/// The bare "1" of sweep F is refused with an explanation: an unbound switch
/// left in the environment stayed armed for every later start.
fn load_binding(name: &str, valid: fn(&str) -> bool, shape: &str) -> Option<String> {
    match std::env::var(name) {
        Err(_) => None,
        Ok(v) => match v.as_str() {
            "" | "0" => None,
            "1" => refuse(format!(
                "{name}=1 is no longer accepted: the override is bound to one exact ledger state ({shape}) so a \
                 value left in the environment never stays armed. Start without it; the refusal prints the exact \
                 value to use."
            )),
            v if valid(v) => Some(v.to_string()),
            other => refuse(format!("{name}={other:?} is not {shape} (or 0/unset for off)")),
        },
    }
}

fn main() {
    let auth = load_auth();
    let max_connections = load_max_connections();
    let reset = load_binding(
        "LEDGER_ALLOW_RESET",
        is_reset_binding,
        "<checkpoint entries>:<16 hex>/<log entries>:<16 hex>, as printed by the refusal",
    );
    let migrate = load_binding(
        "LEDGER_MIGRATE_LEGACY",
        is_log_binding,
        "<log entries>:<first 16 hex of the head hash>, as printed by the refusal",
    );
    let port = std::env::var("LEDGER_PORT").unwrap_or_else(|_| "8090".to_string());
    let log_path = std::env::var("LEDGER_LOG_PATH")
        .unwrap_or_else(|_| "ledger_data/ledger.jsonl".to_string());
    let bind_host = std::env::var("LEDGER_BIND_ADDR").unwrap_or_else(|_| "127.0.0.1".to_string());
    let addr = format!("{bind_host}:{port}");

    // Fix wave 24 (F2): the runtime's threads start with the stop signals blocked (they inherit this mask), so a
    // stop signal is only ever handled by the main thread — and blocking it there across the port file's publish
    // blocks it for the whole process.
    let mask = block_stop_signals();
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(WORKER_THREADS)
        .max_blocking_threads(MAX_BLOCKING_THREADS)
        .enable_io()
        .enable_time()
        .build()
        .unwrap_or_else(|e| refuse(format!("the ledger-rust runtime could not be built: {e}")));
    restore_signal_mask(&mask);
    // Sweep F: a failed bind is a clean refusal (exit 1), not a panic.
    let listener = runtime
        .block_on(async { bind(&addr) })
        .unwrap_or_else(|e| refuse(format!("cannot bind {addr}: {e}")));
    let port_file = std::env::var("LEDGER_PORT_FILE").ok();
    if let Some(pf) = &port_file {
        if let Err(e) = check_port_file_path(pf) {
            refuse(format!("LEDGER_PORT_FILE={pf:?}: {e}"));
        }
    }

    let opts = LedgerOpenOptions { reset: reset.clone(), migrate: migrate.clone() };
    let ledger = match PersistentLedger::open_with(&log_path, opts) {
        Ok(l) => {
            ledger_log!(
                "ledger-rust: loaded {} existing entries from {log_path}, chain verified, head checkpoint {}",
                l.len(),
                serde_json::to_string(&l.head()).unwrap_or_default()
            );
            for (name, v, used) in [
                ("LEDGER_ALLOW_RESET", &reset, l.open_report().reset_used),
                ("LEDGER_MIGRATE_LEGACY", &migrate, l.open_report().migrate_used),
            ] {
                if let Some(v) = v {
                    if !used {
                        ledger_log!(
                            "ledger-rust: WARNING — {name}={v} is set but was not needed (it matches no refusal of \
                             this start, so it did nothing); unset it"
                        );
                    }
                }
            }
            l
        }
        Err(e @ PersistError::Locked(_)) => refuse(format!(
            "ledger log at {log_path} failed to load: {e}. Another ledger-rust is serving this log; two writers \
             on one log fork its chain."
        )),
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

    // G7: announced only now — the log opened and verified, the socket bound
    let _port_file_guard = match &port_file {
        Some(pf) => {
            let port = match listener.local_addr() {
                Ok(a) => a.port(),
                Err(e) => refuse(format!("the bound port cannot be read: {e}")),
            };
            match publish_port_file(pf, port) {
                Ok(g) => Some(g),
                Err(e) => refuse(format!("LEDGER_PORT_FILE={pf:?} could not be written: {e}")),
            }
        }
        None => None,
    };

    // The whole chain was verified by open: GET /ledger/verify starts from here.
    let head = ledger.head();
    let verified = Verified { len: head.entries as usize, hash: head.head_hash };
    let app = Arc::new(App {
        ledger: Mutex::new(ledger),
        gate: WriterPriority::new(),
        reads: Semaphore::new(read_slots()),
        verified: Mutex::new(verified),
        auth,
    });
    runtime.block_on(serve(listener, app, max_connections));
}
