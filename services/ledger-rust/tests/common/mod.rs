//! Shared harness for the integration tests (fix wave 21, AEGIS N20-M-1 and
//! N20-M-3). Each tests/*.rs file is its own crate and uses what it needs.
//!
//! - The server is started with `LEDGER_PORT=0` and `LEDGER_PORT_FILE`: the
//!   server binds an ephemeral port itself and writes the bound port to the
//!   file, so no test ever picks a "free" port and races another process for
//!   it between the pick and the server's bind (the old `free_port()`).
//! - Responses are read to their `Content-Length`, never to EOF: a response
//!   is complete when its body is, whatever happens to the connection after.
#![allow(dead_code)]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::{Duration, Instant};

/// A port file next to the test's scratch files; removed on drop.
pub struct PortFile(pub PathBuf);

impl Drop for PortFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

impl PortFile {
    pub fn new(label: &str) -> PortFile {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let p = std::env::temp_dir().join(format!("ledger_port_{label}_{}_{nanos}.port", std::process::id()));
        let _ = std::fs::remove_file(&p);
        PortFile(p)
    }

    /// The port the server wrote, once it has written a whole line.
    pub fn read(&self) -> Option<u16> {
        let text = std::fs::read_to_string(&self.0).ok()?;
        let line = text.strip_suffix('\n')?;
        line.trim().parse().ok()
    }
}

/// `LEDGER_PORT=0` and `LEDGER_PORT_FILE=<pf>` on `cmd`.
pub fn ephemeral(cmd: &mut Command, pf: &PortFile) {
    cmd.env("LEDGER_PORT", "0").env("LEDGER_PORT_FILE", pf.0.to_str().unwrap());
}

/// Waits until the server has written its bound port, or exits, or `limit`
/// passes. Err names which.
pub fn wait_port(child: &mut Child, pf: &PortFile, limit: Duration) -> Result<u16, String> {
    let deadline = Instant::now() + limit;
    loop {
        if let Some(port) = pf.read() {
            return Ok(port);
        }
        if let Some(st) = child.try_wait().unwrap() {
            return Err(format!("server exited before binding: {st}"));
        }
        if Instant::now() > deadline {
            return Err(format!("server wrote no port file within {limit:?}"));
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// A parsed response: status, lowercased header names with their values, body.
pub struct Response {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Response {
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).to_string()
    }
}

/// Reads one HTTP/1.1 response from `stream`: the head up to CRLFCRLF, then
/// exactly `Content-Length` body bytes (to EOF only when the response carries
/// no Content-Length). Never reads past the response.
pub fn read_response(stream: &mut TcpStream) -> std::io::Result<Response> {
    let mut head = Vec::new();
    let mut byte = [0u8; 1];
    while !head.ends_with(b"\r\n\r\n") {
        let n = stream.read(&mut byte)?;
        if n == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                format!("connection closed inside the response head ({} bytes)", head.len()),
            ));
        }
        head.push(byte[0]);
        if head.len() > 64 * 1024 {
            return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "response head over 64 KiB"));
        }
    }
    let text = String::from_utf8_lossy(&head).to_string();
    let mut lines = text.split("\r\n");
    let status = lines
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let headers: Vec<(String, String)> = lines
        .filter_map(|l| l.split_once(':'))
        .map(|(k, v)| (k.trim().to_ascii_lowercase(), v.trim().to_string()))
        .collect();
    let length = headers
        .iter()
        .find(|(k, _)| k == "content-length")
        .and_then(|(_, v)| v.parse::<usize>().ok());
    let mut body = Vec::new();
    match length {
        Some(n) => {
            body.resize(n, 0);
            stream.read_exact(&mut body)?;
        }
        None => {
            stream.read_to_end(&mut body)?;
        }
    }
    Ok(Response { status, headers, body })
}

/// Sends `raw` on a fresh connection and reads one response (to its length).
pub fn exchange(port: u16, raw: &[u8], read_timeout: Duration) -> std::io::Result<Response> {
    exchange_on("127.0.0.1", port, raw, read_timeout)
}

pub fn exchange_on(host: &str, port: u16, raw: &[u8], read_timeout: Duration) -> std::io::Result<Response> {
    let mut stream = TcpStream::connect((host, port))?;
    stream.set_read_timeout(Some(read_timeout))?;
    stream.write_all(raw)?;
    read_response(&mut stream)
}

/// The raw bytes of one request with `Connection: close`.
pub fn raw_request(method: &str, path: &str, auth: Option<&str>, body: &str) -> Vec<u8> {
    let auth_line = auth.map(|v| format!("Authorization: {v}\r\n")).unwrap_or_default();
    format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth_line}Content-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
    .into_bytes()
}
