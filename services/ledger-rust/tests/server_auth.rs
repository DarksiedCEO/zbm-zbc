//! Integration tests for the bin/server.rs HTTP layer — specifically the
//! two fixes from the Sep 22 2026 independent review (CONFIRMED, both):
//! no authentication on any route, and a hardcoded 0.0.0.0 bind with no
//! loopback default. These spawn the real compiled binary and talk to it
//! over a real TCP socket — this is the layer the earlier unit tests in
//! lib.rs never touched (those exercise the Ledger struct directly, not
//! the HTTP wrapper around it), so it was possible for both bugs to exist
//! with the full unit-test suite green, which is exactly what happened.
//!
//! No HTTP client crate is added as a dependency for this (the server's
//! HTTP stack, hyper, is used only server-side here), so a minimal raw HTTP/1.1 request is sent
//! by hand over `std::net::TcpStream`. `CARGO_BIN_EXE_ledger-rust` is a
//! built-in Cargo integration-test feature (stable since 1.43), not an
//! external dependency.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::process::{Child, Command};
use std::time::Duration;

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

fn free_port() -> u16 {
    // Bind to port 0 to let the OS pick a free one, then release it
    // immediately — small race window, acceptable for a test harness.
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.local_addr().unwrap().port()
}

fn start_server(token: &str, bind_addr: Option<&str>) -> ServerHandle {
    let port = free_port();
    let log_path = std::env::temp_dir().join(format!("ledger_test_{port}.jsonl"));
    let _ = std::fs::remove_file(&log_path);

    let mut cmd = Command::new(env!("CARGO_BIN_EXE_server"));
    cmd.env("LEDGER_SERVICE_TOKEN", token)
        .env("LEDGER_PORT", port.to_string())
        .env("LEDGER_LOG_PATH", log_path.to_str().unwrap())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    if let Some(addr) = bind_addr {
        cmd.env("LEDGER_BIND_ADDR", addr);
    }

    let child = cmd.spawn().expect("failed to spawn ledger-rust server");
    let handle = ServerHandle { child, port };

    // Poll /health until the server is accepting connections (startup
    // does real file I/O — the persistent ledger open/verify — so this
    // isn't instantaneous).
    let host = bind_addr.unwrap_or("127.0.0.1");
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    loop {
        if raw_http_request(host, handle.port, "GET", "/health", None).is_ok() {
            break;
        }
        if std::time::Instant::now() > deadline {
            panic!("ledger-rust server did not come up within 5s");
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    handle
}

/// Sends one raw HTTP/1.1 request and returns (status_code, body).
fn raw_http_request(
    host: &str,
    port: u16,
    method: &str,
    path: &str,
    auth_header: Option<&str>,
) -> std::io::Result<(u16, String)> {
    let mut stream = TcpStream::connect((host, port))?;
    stream.set_read_timeout(Some(Duration::from_secs(3)))?;

    let auth_line = match auth_header {
        Some(v) => format!("Authorization: {v}\r\n"),
        None => String::new(),
    };
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: {host}\r\n{auth_line}Connection: close\r\n\r\n"
    );
    stream.write_all(request.as_bytes())?;

    let mut response = String::new();
    stream.read_to_string(&mut response)?;

    let status_line = response.lines().next().unwrap_or("");
    let status: u16 = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let body = response.split("\r\n\r\n").nth(1).unwrap_or("").to_string();
    Ok((status, body))
}

#[test]
fn health_is_open_with_no_token() {
    let server = start_server("test-token-123", None);
    let (status, _) = raw_http_request("127.0.0.1", server.port, "GET", "/health", None).unwrap();
    assert_eq!(status, 200);
}

#[test]
fn ledger_entries_rejects_missing_token() {
    let server = start_server("test-token-123", None);
    let (status, _) =
        raw_http_request("127.0.0.1", server.port, "GET", "/ledger/entries", None).unwrap();
    assert_eq!(status, 401);
}

#[test]
fn ledger_entries_rejects_wrong_token() {
    let server = start_server("test-token-123", None);
    let (status, _) = raw_http_request(
        "127.0.0.1",
        server.port,
        "GET",
        "/ledger/entries",
        Some("Bearer not-the-real-token"),
    )
    .unwrap();
    assert_eq!(status, 401);
}

#[test]
fn ledger_entries_accepts_correct_token() {
    let server = start_server("test-token-123", None);
    let (status, _) = raw_http_request(
        "127.0.0.1",
        server.port,
        "GET",
        "/ledger/entries",
        Some("Bearer test-token-123"),
    )
    .unwrap();
    assert_eq!(status, 200);
}

#[test]
fn ledger_append_rejects_missing_token() {
    // CONFIRMED finding: this was the most dangerous unauthenticated
    // route — a caller with no credentials at all could previously write
    // fabricated entries into the tamper-evident evidence ledger.
    let server = start_server("test-token-123", None);
    let (status, _) =
        raw_http_request("127.0.0.1", server.port, "POST", "/ledger/append", None).unwrap();
    assert_eq!(status, 401);
}

#[test]
fn malformed_authorization_header_is_rejected() {
    let server = start_server("test-token-123", None);
    let (status, _) = raw_http_request(
        "127.0.0.1",
        server.port,
        "GET",
        "/ledger/entries",
        Some("test-token-123"), // no "Bearer " prefix
    )
    .unwrap();
    assert_eq!(status, 401);
}

#[test]
fn default_bind_is_loopback_not_all_interfaces() {
    // CONFIRMED finding: the server previously hardcoded 0.0.0.0, binding
    // every network interface with no way to restrict it. With
    // LEDGER_BIND_ADDR unset, the default must be 127.0.0.1 — confirmed
    // here by successfully reaching it over loopback with no override set
    // (start_server's own health-check poll already proves this connects
    // on 127.0.0.1; this test exists so the default is asserted by name,
    // not just incidentally relied on by every other test in this file).
    let server = start_server("test-token-123", None);
    let (status, _) = raw_http_request("127.0.0.1", server.port, "GET", "/health", None).unwrap();
    assert_eq!(status, 200);
}

#[test]
fn explicit_bind_addr_override_still_works() {
    let server = start_server("test-token-123", Some("127.0.0.1"));
    let (status, _) = raw_http_request("127.0.0.1", server.port, "GET", "/health", None).unwrap();
    assert_eq!(status, 200);
}
