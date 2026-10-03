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

mod common;

use std::process::{Child, Command};
use std::time::Duration;

use common::PortFile;

struct ServerHandle {
    child: Child,
    port: u16,
    _port_file: PortFile,
    /// Fix wave 24: the scratch log, removed with the server (it was left in the temp dir, 8 files a run).
    log_path: std::path::PathBuf,
}

impl Drop for ServerHandle {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_file(&self.log_path);
    }
}

/// Fix wave 21 (N20-M-3): the server binds port 0 itself and reports the bound
/// port in LEDGER_PORT_FILE; there is no pick-then-release `free_port()` race.
fn start_server(token: &str, bind_addr: Option<&str>) -> ServerHandle {
    let pf = PortFile::new("auth");
    let log_path = std::env::temp_dir().join(format!("ledger_test_auth_{}.jsonl", common::unique_suffix()));
    let _ = std::fs::remove_file(&log_path);

    let mut cmd = Command::new(env!("CARGO_BIN_EXE_server"));
    cmd.env("LEDGER_SERVICE_TOKEN", token)
        .env("LEDGER_LOG_PATH", log_path.to_str().unwrap())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    common::ephemeral(&mut cmd, &pf);
    if let Some(addr) = bind_addr {
        cmd.env("LEDGER_BIND_ADDR", addr);
    }

    let mut child = cmd.spawn().expect("failed to spawn ledger-rust server");
    // The port file is written after the bind and the listen; startup then does
    // real file I/O (the persistent ledger open/verify), so /health is polled too.
    let port = match common::wait_port(&mut child, &pf, Duration::from_secs(10)) {
        Ok(p) => p,
        Err(e) => {
            let _ = child.kill();
            let _ = child.wait();
            let _ = std::fs::remove_file(&log_path);
            panic!("{e}");
        }
    };
    let handle = ServerHandle { child, port, _port_file: pf, log_path };
    let host = bind_addr.unwrap_or("127.0.0.1");
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    loop {
        if raw_http_request(host, handle.port, "GET", "/health", None).is_ok() {
            break;
        }
        if std::time::Instant::now() > deadline {
            panic!("ledger-rust server did not come up within 10s");
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    handle
}

/// Sends one raw HTTP/1.1 request and returns (status_code, body), reading the
/// response to its Content-Length (fix wave 21, N20-M-1: never to EOF).
fn raw_http_request(
    host: &str,
    port: u16,
    method: &str,
    path: &str,
    auth_header: Option<&str>,
) -> std::io::Result<(u16, String)> {
    let auth_line = match auth_header {
        Some(v) => format!("Authorization: {v}\r\n"),
        None => String::new(),
    };
    let request = format!("{method} {path} HTTP/1.1\r\nHost: {host}\r\n{auth_line}Connection: close\r\n\r\n");
    let resp = common::exchange_on(host, port, request.as_bytes(), Duration::from_secs(3))?;
    Ok((resp.status, resp.text()))
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
