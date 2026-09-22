//! Minimal REST wrapper around the Ledger core (lib.rs). Uses tiny_http —
//! a synchronous, dependency-light HTTP server — deliberately, per
//! Decision 4's phased-build discipline: this ledger has no real
//! throughput requirement yet (pre-revenue, fixture-only), so a full
//! async framework (axum/tokio) is not justified tonight. Swapping to
//! one later is a contained change behind this same REST contract.
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

use std::sync::Mutex;

use ledger_rust::{LedgerRecordInput, PersistentLedger};
use tiny_http::{Header, Method, Response, Server};

fn json_header() -> Header {
    Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..]).unwrap()
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

fn extract_bearer_token(request: &tiny_http::Request) -> Option<String> {
    request
        .headers()
        .iter()
        .find(|h| h.field.as_str().as_str().eq_ignore_ascii_case("Authorization"))
        .map(|h| h.value.as_str().to_string())
        .and_then(|v| v.strip_prefix("Bearer ").map(|s| s.to_string()))
}

fn unauthorized_response() -> (u16, String) {
    (
        401,
        serde_json::json!({"error": "missing, malformed, or invalid Authorization header (expected: Bearer <token>)"})
            .to_string(),
    )
}

fn load_required_token() -> String {
    match std::env::var("LEDGER_SERVICE_TOKEN") {
        Ok(t) if !t.is_empty() => t,
        _ => {
            eprintln!(
                "ledger-rust: REFUSING TO START — LEDGER_SERVICE_TOKEN is not set. This is \
                 the evidence ledger; it does not start unauthenticated. Set \
                 LEDGER_SERVICE_TOKEN to a shared secret before starting ledger-rust, and set \
                 the identical value wherever this service is called from."
            );
            std::process::exit(1);
        }
    }
}

fn main() {
    let required_token = load_required_token();
    let port = std::env::var("LEDGER_PORT").unwrap_or_else(|_| "8090".to_string());
    let log_path = std::env::var("LEDGER_LOG_PATH")
        .unwrap_or_else(|_| "ledger_data/ledger.jsonl".to_string());
    let bind_host = std::env::var("LEDGER_BIND_ADDR").unwrap_or_else(|_| "127.0.0.1".to_string());
    let addr = format!("{bind_host}:{port}");
    let server = Server::http(&addr).expect("failed to bind ledger-rust HTTP server");

    let ledger = match PersistentLedger::open(&log_path) {
        Ok(l) => {
            eprintln!(
                "ledger-rust: loaded {} existing entries from {log_path}, chain verified",
                l.len()
            );
            Mutex::new(l)
        }
        Err(e) => {
            eprintln!(
                "ledger-rust: REFUSING TO START — ledger log at {log_path} failed to load: {e}"
            );
            eprintln!(
                "ledger-rust: this is a fail-closed integrity check, not a crash — the log file \
                 is either corrupted or has been tampered with, and starting anyway would hide \
                 that. Resolve manually (restore from backup, or move the file aside if you \
                 accept starting a fresh ledger) before restarting."
            );
            std::process::exit(1);
        }
    };

    eprintln!("ledger-rust listening on :{port} (log: {log_path})");

    for mut request in server.incoming_requests() {
        let method = request.method().clone();
        let url = request.url().to_string();

        // Auth gate: every route except /health requires a valid bearer
        // token, checked before any route logic runs (fail-closed — an
        // unmatched or malformed Authorization header never falls through
        // to a handler). /health stays open for basic liveness checks,
        // matching the pattern already used in detection-py/fulfillment-py.
        if url != "/health" {
            let authorized = match extract_bearer_token(&request) {
                Some(token) => constant_time_eq(&token, &required_token),
                None => false,
            };
            if !authorized {
                let (status, body) = unauthorized_response();
                let response = Response::from_string(body)
                    .with_status_code(status)
                    .with_header(json_header());
                let _ = request.respond(response);
                continue;
            }
        }

        let (status, body) = match (method, url.as_str()) {
            (Method::Get, "/health") => (
                200,
                serde_json::json!({"status": "ok", "service": "ledger-rust"}).to_string(),
            ),

            (Method::Get, "/ledger/entries") => {
                let l = ledger.lock().unwrap();
                (200, serde_json::to_string(l.entries()).unwrap())
            }

            (Method::Get, "/ledger/verify") => {
                let l = ledger.lock().unwrap();
                match l.verify_chain() {
                    Ok(()) => (200, serde_json::json!({"valid": true, "entries": l.len()}).to_string()),
                    Err(e) => (
                        409,
                        serde_json::json!({"valid": false, "error": format!("{e:?}")}).to_string(),
                    ),
                }
            }

            (Method::Post, "/ledger/append") => {
                let mut body = String::new();
                if let Err(e) = request.as_reader().read_to_string(&mut body) {
                    (400, serde_json::json!({"error": format!("failed to read body: {e}")}).to_string())
                } else {
                    match serde_json::from_str::<LedgerRecordInput>(&body) {
                        Ok(record) => {
                            let mut l = ledger.lock().unwrap();
                            match l.append(record) {
                                Ok(entry) => (201, serde_json::to_string(entry).unwrap()),
                                Err(e) => {
                                    // Disk write failed — the in-memory ledger was
                                    // deliberately left untouched (see
                                    // PersistentLedger::append). Surface this as a
                                    // hard server error: the caller must not treat
                                    // this as "recorded."
                                    eprintln!("ledger-rust: append failed to persist: {e}");
                                    (
                                        500,
                                        serde_json::json!({
                                            "error": format!("failed to persist entry: {e}")
                                        })
                                        .to_string(),
                                    )
                                }
                            }
                        }
                        Err(e) => (
                            400,
                            serde_json::json!({"error": format!("invalid LedgerRecordInput: {e}")}).to_string(),
                        ),
                    }
                }
            }

            _ => (404, serde_json::json!({"error": "not found"}).to_string()),
        };

        let response = Response::from_string(body)
            .with_status_code(status)
            .with_header(json_header());
        let _ = request.respond(response);
    }
}
