//! Fix wave 1 (Sep 24 2026) regression tests, against the REAL compiled
//! server binary over a real TCP socket and real files — same raw-HTTP
//! harness style as tests/server_auth.rs and tests/server_events.rs.
//!
//!   F5  a crash mid-write (torn final line) must not brick the ledger; a
//!       failed write must be rolled back on disk and in memory.
//!   F6  every log the pre-Decimal binary (commit 9531fc2) could write —
//!       including negative, -0.0 and sub-cent negative amounts — loads.
//!   F7  the finding canonical string must be unambiguous: `|`, control
//!       characters and the literal "null" are refused on append, and a
//!       re-split / null-forged log refuses to start.
//!   D4  a non-ASCII header byte: tiny_http drops the connection before any
//!       of our code runs (documented limitation, ADR 0003 section 5). Pinned
//!       here: connection closed, process stays up, nothing bypassed.
//!   Cosmetic: the startup log names the real bind address.
//!
//! The F5 tests use RLIMIT_FSIZE (`ulimit -f`) on the real process to make
//! the kernel genuinely fail the write — not a simulated failure.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use serde_json::{json, Value};

const TOKEN: &str = "hardening-test-token";

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

/// Removes the scratch log and every side file next to it on drop.
struct Scratch(PathBuf);
impl Drop for Scratch {
    fn drop(&mut self) {
        for f in side_files(&self.0) {
            let _ = std::fs::remove_file(f);
        }
        let _ = std::fs::remove_file(&self.0);
        let _ = std::fs::remove_file(self.stderr_path());
    }
}
impl Scratch {
    fn stderr_path(&self) -> PathBuf {
        PathBuf::from(format!("{}.stderr", self.0.display()))
    }
}

fn scratch(label: &str) -> Scratch {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let p = std::env::temp_dir().join(format!("ledger_hardening_{label}_{}_{nanos}.jsonl", std::process::id()));
    let _ = std::fs::remove_file(&p);
    Scratch(p)
}

/// Files `<log>.torn-*` that the torn-tail recovery writes next to the log.
fn side_files(log: &Path) -> Vec<PathBuf> {
    let dir = log.parent().unwrap();
    let prefix = format!("{}.torn-", log.file_name().unwrap().to_str().unwrap());
    std::fs::read_dir(dir)
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.file_name().unwrap().to_str().unwrap().starts_with(&prefix))
        .collect()
}

fn free_port() -> u16 {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.local_addr().unwrap().port()
}

/// Spawns the server, optionally through `sh -c '<prelude>; exec server'`
/// so a shell prelude (ulimit / trap) applies to the real process.
fn spawn(log: &Scratch, prelude: Option<&str>) -> (Child, u16) {
    let port = free_port();
    let bin = env!("CARGO_BIN_EXE_server");
    let mut cmd = match prelude {
        None => Command::new(bin),
        Some(p) => {
            let mut c = Command::new("sh");
            c.arg("-c").arg(format!("{p}; exec \"$0\"")).arg(bin);
            c
        }
    };
    let stderr = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log.stderr_path())
        .unwrap();
    let child = cmd
        .env("LEDGER_SERVICE_TOKEN", TOKEN)
        .env("LEDGER_PORT", port.to_string())
        .env("LEDGER_LOG_PATH", log.0.to_str().unwrap())
        .stdout(Stdio::null())
        .stderr(Stdio::from(stderr))
        .spawn()
        .expect("failed to spawn ledger-rust server");
    (child, port)
}

/// Waits for /health. Returns Err(exit status) if the process exits first.
fn wait_up(child: &mut Child, port: u16) -> Result<(), String> {
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(st) = child.try_wait().unwrap() {
            return Err(format!("server exited before coming up: {st}"));
        }
        if let Ok((200, _)) = request(port, "GET", "/health", None, None) {
            return Ok(());
        }
        if std::time::Instant::now() > deadline {
            return Err("server did not come up within 5s".into());
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

fn start(log: &Scratch, prelude: Option<&str>) -> ServerHandle {
    let (mut child, port) = spawn(log, prelude);
    if let Err(e) = wait_up(&mut child, port) {
        let _ = child.kill();
        let _ = child.wait();
        let stderr = std::fs::read_to_string(log.stderr_path()).unwrap_or_default();
        panic!("{e}\n--- server stderr ---\n{stderr}");
    }
    ServerHandle { child, port }
}

/// Runs the server to completion and returns (success, stderr) — for logs
/// the server must refuse to open.
fn run_expecting_exit(log: &Scratch) -> (bool, String) {
    let (mut child, port) = spawn(log, None);
    let r = wait_up(&mut child, port);
    let _ = child.kill();
    let status = child.wait().unwrap();
    let stderr = std::fs::read_to_string(log.stderr_path()).unwrap_or_default();
    (r.is_ok() || status.success(), stderr)
}

fn raw(port: u16, bytes: &[u8]) -> std::io::Result<Vec<u8>> {
    let mut stream = TcpStream::connect(("127.0.0.1", port))?;
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    stream.write_all(bytes)?;
    let mut out = Vec::new();
    stream.read_to_end(&mut out)?;
    Ok(out)
}

fn request(port: u16, method: &str, path: &str, auth: Option<&str>, body: Option<&str>) -> std::io::Result<(u16, String)> {
    let auth_line = auth.map(|v| format!("Authorization: {v}\r\n")).unwrap_or_default();
    let body = body.unwrap_or("");
    let req = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth_line}Content-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let resp = String::from_utf8_lossy(&raw(port, req.as_bytes())?).to_string();
    let status: u16 = resp
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let body = resp.split("\r\n\r\n").nth(1).unwrap_or("").to_string();
    Ok((status, body))
}

fn authed(port: u16, method: &str, path: &str, body: Option<&str>) -> (u16, Value) {
    let (status, text) = request(port, method, path, Some(&format!("Bearer {TOKEN}")), body).unwrap();
    let v = serde_json::from_str(&text).unwrap_or_else(|e| panic!("non-JSON body ({e}): {text:?}"));
    (status, v)
}

fn event(event_id: &str) -> String {
    json!({
        "event_id": event_id,
        "department": "onboarding",
        "event_type": "compliance_ruling",
        "actor": "intel_15_compliance",
        "subject_id": "client_123",
        "payload_sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "summary": "Activation blocked: 2 requirements unmet"
    })
    .to_string()
}

fn finding(finding_id: &str) -> Value {
    json!({
        "finding_id": finding_id, "agent_id": "discount-misuse-v1", "entity_id": "ord_1007",
        "leak_category": "discount_misuse", "amount_usd": "12.30",
        "value_classification": "observed", "decision_confidence": "very_high"
    })
}

fn file_len(p: &Path) -> u64 {
    std::fs::metadata(p).map(|m| m.len()).unwrap_or(0)
}

// --- F5: torn final line after a crash mid-write ---------------------------------

/// AEGIS reproduction: the server runs under `ulimit -f 2` (1024 bytes) with
/// the default SIGXFSZ action, so the kernel KILLS it in the middle of the
/// write that crosses the limit, leaving a partial, unterminated final line.
/// Before the fix, the restart refused to start ("EOF while parsing").
#[test]
fn f5_crash_mid_write_torn_tail_is_truncated_preserved_and_restart_succeeds() {
    let log = scratch("torn");
    let mut acked = 0usize;
    {
        let s = start(&log, Some("ulimit -f 2"));
        for i in 0..10 {
            match request(s.port, "POST", "/ledger/events", Some(&format!("Bearer {TOKEN}")), Some(&event(&format!("e{i}")))) {
                Ok((201, _)) => acked += 1,
                _ => break, // process killed by SIGXFSZ mid-write: no response
            }
        }
    }
    assert!(acked >= 1, "at least one event must be acknowledged before the limit");
    let before = std::fs::read(&log.0).unwrap();
    assert!(!before.ends_with(b"\n"), "precondition: the crash left a torn, unterminated final line");
    let last_nl = before.iter().rposition(|&b| b == b'\n').map(|p| p + 1).unwrap_or(0);
    let torn = before[last_nl..].to_vec();
    assert_eq!(before[..last_nl].iter().filter(|&&b| b == b'\n').count(), acked, "every acked entry is a complete line");

    // Restart with no limit: must come up with exactly the acknowledged entries.
    let s = start(&log, None);
    let (st, v) = authed(s.port, "GET", "/ledger/verify", None);
    assert_eq!(st, 200);
    assert_eq!(v, json!({"valid": true, "entries": acked}));

    let after = std::fs::read(&log.0).unwrap();
    assert_eq!(after, before[..last_nl], "torn bytes truncated, everything before kept byte-for-byte");
    let sides = side_files(&log.0);
    assert_eq!(sides.len(), 1, "torn bytes preserved to exactly one side file");
    assert_eq!(std::fs::read(&sides[0]).unwrap(), torn, "side file holds exactly the torn bytes");
    let stderr = std::fs::read_to_string(log.stderr_path()).unwrap();
    assert!(stderr.contains("TORN FINAL LINE"), "loud log line expected, got:\n{stderr}");

    // The chain keeps growing and survives another restart.
    let (st, e) = authed(s.port, "POST", "/ledger/events", Some(&event("after-recovery")));
    assert_eq!(st, 201);
    assert_eq!(e["seq"], acked);
    drop(s);
    let s = start(&log, None);
    let (_, v) = authed(s.port, "GET", "/ledger/verify", None);
    assert_eq!(v, json!({"valid": true, "entries": acked + 1}));
}

// --- F5 (related): a failed write is rolled back ------------------------------------

/// SIGXFSZ ignored + `ulimit -f 1` (512 bytes): the kernel writes a PARTIAL
/// line and then fails the write with EFBIG — a genuine write failure in the
/// real process. The append must return 500, the file must be truncated back
/// to its pre-append length, and memory must not advance. Before the fix the
/// partial bytes stayed on disk and the next restart refused to start.
#[test]
fn f5_failed_write_is_rolled_back_on_disk_and_in_memory() {
    let log = scratch("writefail");
    let s = start(&log, Some("trap '' XFSZ; ulimit -f 1"));

    let (st, _) = authed(s.port, "POST", "/ledger/events", Some(&event("fits")));
    assert_eq!(st, 201, "first event fits under 512 bytes");
    let good = std::fs::read(&log.0).unwrap();
    assert!(good.len() < 512 && good.ends_with(b"\n"));

    for id in ["too-big-1", "too-big-2"] {
        let (st, v) = authed(s.port, "POST", "/ledger/events", Some(&event(id)));
        assert_eq!(st, 500, "write failure must be a 500: {v}");
        assert_eq!(std::fs::read(&log.0).unwrap(), good, "file restored to its pre-append bytes");
    }
    let (st, v) = authed(s.port, "POST", "/ledger/append", Some(&finding("f-too-big").to_string()));
    assert_eq!(st, 500, "{v}");
    assert_eq!(file_len(&log.0), good.len() as u64);

    let (_, entries) = authed(s.port, "GET", "/ledger/entries", None);
    assert_eq!(entries.as_array().unwrap().len(), 1, "memory did not advance");
    let (_, v) = authed(s.port, "GET", "/ledger/verify", None);
    assert_eq!(v, json!({"valid": true, "entries": 1}));
    // A failed event is not in the idempotency index: no 200 for it.
    let (st, _) = authed(s.port, "POST", "/ledger/events", Some(&event("too-big-1")));
    assert_eq!(st, 500);
    drop(s);

    // Restart without the limit: clean open, no torn-tail recovery needed.
    let s = start(&log, None);
    let (_, v) = authed(s.port, "GET", "/ledger/verify", None);
    assert_eq!(v, json!({"valid": true, "entries": 1}));
    assert!(side_files(&log.0).is_empty(), "nothing torn was left behind");
    let (st, e) = authed(s.port, "POST", "/ledger/events", Some(&event("too-big-1")));
    assert_eq!(st, 201, "the failed event was never recorded, so it is new now");
    assert_eq!(e["seq"], 1);
}

/// Corruption that is NOT a torn final line still refuses to start.
#[test]
fn f5_corruption_elsewhere_still_refuses_to_start() {
    // Build a real 3-entry log first.
    let good = scratch("good_src");
    {
        let s = start(&good, None);
        for id in ["a", "b", "c"] {
            assert_eq!(authed(s.port, "POST", "/ledger/events", Some(&event(id))).0, 201);
        }
    }
    let text = std::fs::read_to_string(&good.0).unwrap();
    let lines: Vec<&str> = text.lines().collect();

    let cases: Vec<(&str, String)> = vec![
        // Mid-file garbage line (newline-terminated).
        ("mid-file torn line", format!("{}\n{}\n{}\n", lines[0], &lines[1][..40], lines[2])),
        // Complete, newline-terminated final line that does not parse.
        ("terminated unparseable final line", format!("{}\n{}\n{}\n", lines[0], lines[1], &lines[2][..40])),
        // Complete final line that parses but fails hash verification.
        ("terminated final line with bad hash", format!("{}\n{}\n{}\n", lines[0], lines[1], lines[2].replace("blocked", "approved"))),
        // A torn tail does not excuse corruption earlier in the file.
        ("mid-file corruption plus torn tail", format!("{}\n{}\n{}\n{}", lines[0], &lines[1][..40], lines[2], &lines[2][..30])),
    ];
    for (name, content) in cases {
        let log = scratch("corrupt");
        std::fs::write(&log.0, &content).unwrap();
        let (started, stderr) = run_expecting_exit(&log);
        assert!(!started, "{name}: server must refuse to start");
        assert!(stderr.contains("REFUSING TO START"), "{name}: {stderr}");
        assert_eq!(std::fs::read_to_string(&log.0).unwrap(), content, "{name}: file must not be modified");
        assert!(side_files(&log.0).is_empty(), "{name}: no side file");
    }
}

// --- F6: legacy logs from the real old binary ------------------------------------------

/// tests/fixtures/legacy_ledger_v2_negatives.jsonl was written by the REAL
/// 9531fc2 server binary over HTTP (every POST got 201 and the old binary's
/// own /ledger/verify returned {"entries":15,"valid":true}). It contains
/// -0.0, -5, -0.001, -12.345, -0.005, -1e-9 and the previously tested
/// positives. Before the fix the new binary refused to start on line 1.
#[test]
fn f6_legacy_log_with_negative_amounts_from_old_binary_is_served_and_extended() {
    let log = scratch("legacy_neg");
    let fixture = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/legacy_ledger_v2_negatives.jsonl");
    std::fs::copy(fixture, &log.0).unwrap();
    let original = std::fs::read(&log.0).unwrap();

    let s = start(&log, None);
    let (st, v) = authed(s.port, "GET", "/ledger/verify", None);
    assert_eq!(st, 200);
    assert_eq!(v, json!({"valid": true, "entries": 15}));

    let (_, entries) = authed(s.port, "GET", "/ledger/entries", None);
    let amounts: Vec<Value> = entries.as_array().unwrap().iter().map(|e| e["amount_usd"].clone()).collect();
    let expected = [
        json!("-0.00"), json!("-5.00"), json!("-0.00"), json!("120.00"), json!("54.38"), json!("0.10"),
        json!("0.30"), json!("1234567.89"), json!("-12.35"), json!("-0.01"), json!("-0.00"), json!("2.67"),
        json!("100000000000000000000.00"), Value::Null, Value::Null,
    ];
    assert_eq!(amounts, expected);

    // New appends keep the strict contract even on a legacy log.
    for bad in ["-5.00", "-0.00", "0.00"] {
        let mut f = finding("f-neg");
        f["amount_usd"] = json!(bad);
        let (st, v) = authed(s.port, "POST", "/ledger/append", Some(&f.to_string()));
        assert_eq!(st, 400, "{bad}: {v}");
    }
    assert_eq!(authed(s.port, "POST", "/ledger/append", Some(&finding("f-new").to_string())).0, 201);
    assert_eq!(authed(s.port, "POST", "/ledger/events", Some(&event("onb-after-legacy"))).0, 201);
    drop(s);

    let s = start(&log, None);
    let (_, v) = authed(s.port, "GET", "/ledger/verify", None);
    assert_eq!(v, json!({"valid": true, "entries": 17}));
    assert!(std::fs::read(&log.0).unwrap().starts_with(&original), "legacy lines never rewritten");
}

// --- F7: unambiguous finding canonical form ------------------------------------------------

#[test]
fn f7_append_rejects_pipes_control_chars_and_literal_null() {
    let log = scratch("f7_append");
    let s = start(&log, None);
    let mut cases: Vec<(String, Value)> = Vec::new();
    for field in ["finding_id", "agent_id", "entity_id", "leak_category", "value_classification", "decision_confidence"] {
        for bad in ["a|b", "|", "a\nb", "a\u{0}b", "a\u{7f}b", "a\u{85}b", "a\tb"] {
            let mut f = finding("f-x");
            f[field] = json!(bad);
            cases.push((format!("{field}={bad:?}"), f));
        }
    }
    for field in ["value_classification", "decision_confidence"] {
        let mut f = finding("f-x");
        f[field] = json!("null");
        cases.push((format!("{field}=\"null\""), f));
    }
    for (name, body) in &cases {
        let (st, v) = authed(s.port, "POST", "/ledger/append", Some(&body.to_string()));
        assert_eq!(st, 400, "{name}: {v}");
        assert!(v["error"].as_str().unwrap().contains("invalid LedgerRecordInput"), "{name}: {v}");
    }
    // Real JSON null for the optional fields is still fine, as are ordinary values.
    let mut ok = finding("f-ok");
    ok["value_classification"] = Value::Null;
    ok["decision_confidence"] = Value::Null;
    assert_eq!(authed(s.port, "POST", "/ledger/append", Some(&ok.to_string())).0, 201);
    let (_, entries) = authed(s.port, "GET", "/ledger/entries", None);
    assert_eq!(entries.as_array().unwrap().len(), 1, "only the valid finding was recorded");
}

/// The AEGIS forgery (a `|` moved between finding_id and agent_id, and a
/// null turned into the string "null") verified and served before the fix.
#[test]
fn f7_aegis_forged_resplit_log_refuses_to_start() {
    let log = scratch("f7_forge");
    std::fs::copy(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/aegis_forged_resplit.jsonl"), &log.0).unwrap();
    let (started, stderr) = run_expecting_exit(&log);
    assert!(!started, "forged log must not be served");
    assert!(stderr.contains("REFUSING TO START"), "{stderr}");
}

/// Documented decision (ADR 0003 section 4): a legacy entry the OLD binary
/// really wrote, but whose canonical string is ambiguous (a `|` in a field,
/// or the literal string "null" in an optional field), cannot be told apart
/// from a forged re-split of it, so it is refused, not trusted.
#[test]
fn f7_ambiguous_legacy_entries_from_old_binary_are_refused() {
    for fixture in ["legacy_ambiguous_pipe.jsonl", "legacy_ambiguous_null.jsonl"] {
        let log = scratch("f7_legacy_amb");
        std::fs::copy(format!("{}/tests/fixtures/{fixture}", env!("CARGO_MANIFEST_DIR")), &log.0).unwrap();
        let (started, stderr) = run_expecting_exit(&log);
        assert!(!started, "{fixture} must be refused");
        assert!(stderr.contains("ambiguous"), "{fixture}: {stderr}");
    }
}

// --- D4: non-ASCII header bytes (known tiny_http limitation, pinned) -------------------------

/// tiny_http 0.12.0 (latest release; also unchanged on its master branch)
/// rejects any non-ASCII byte in the request head inside its connection
/// thread (`ClientConnection::read_next_line` -> `ReadIoError` -> `return
/// None`) and closes the socket without a response, before any request
/// reaches this server's code. This pins the observable behavior: zero bytes
/// back, the process stays up, and nothing is read or written.
#[test]
fn d4_non_ascii_header_closes_connection_process_stays_up_nothing_bypassed() {
    let log = scratch("d4");
    let mut s = start(&log, None);
    let body = finding("f-nonascii").to_string();
    let cases: Vec<Vec<u8>> = vec![
        b"GET /health HTTP/1.1\r\nHost: x\r\nX-Note: caf\xc3\xa9\r\nConnection: close\r\n\r\n".to_vec(),
        format!("GET /ledger/entries HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nX-Note: \u{e9}\r\nConnection: close\r\n\r\n").into_bytes(),
        b"GET /ledger/entries HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer caf\xe9\r\nConnection: close\r\n\r\n".to_vec(),
        format!(
            "POST /ledger/append HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nX-Note: \u{2713}\r\n\
             Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .into_bytes(),
    ];
    for (i, req) in cases.iter().enumerate() {
        let resp = raw(s.port, req).unwrap_or_default();
        assert!(resp.is_empty(), "case {i}: expected a dropped connection, got {:?}", String::from_utf8_lossy(&resp));
        assert!(s.child.try_wait().unwrap().is_none(), "case {i}: process must stay up");
    }
    // Still serving, still enforcing auth, and nothing was written.
    assert_eq!(request(s.port, "GET", "/health", None, None).unwrap().0, 200);
    assert_eq!(request(s.port, "GET", "/ledger/entries", None, None).unwrap().0, 401);
    let (st, entries) = authed(s.port, "GET", "/ledger/entries", None);
    assert_eq!(st, 200);
    assert_eq!(entries.as_array().unwrap().len(), 0, "the non-ASCII POST recorded nothing");
    assert_eq!(file_len(&log.0), 0);
}

// --- cosmetic: real bind address in the startup log ------------------------------------------

#[test]
fn startup_log_names_the_real_bind_address() {
    let log = scratch("bindlog");
    let s = start(&log, None);
    let port = s.port;
    drop(s);
    let stderr = std::fs::read_to_string(log.stderr_path()).unwrap();
    assert!(
        stderr.contains(&format!("listening on 127.0.0.1:{port}")),
        "startup log must name the real bind address, got:\n{stderr}"
    );
}

// --- new defect found during fix wave 1: logging must never kill the server -----------------

/// `eprintln!` panics when stderr cannot be written (full disk under a
/// redirected log, closed pipe, RLIMIT_FSIZE). The server is single-threaded,
/// so one failed log line killed the evidence ledger (exit 101). With stderr
/// pointed at /dev/full (every write fails with ENOSPC) the server must still
/// start, serve, record and verify.
#[test]
fn unwritable_stderr_does_not_kill_the_server() {
    let log = scratch("devfull");
    let port = free_port();
    let devfull = std::fs::OpenOptions::new().write(true).open("/dev/full").unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_server"))
        .env("LEDGER_SERVICE_TOKEN", TOKEN)
        .env("LEDGER_PORT", port.to_string())
        .env("LEDGER_LOG_PATH", log.0.to_str().unwrap())
        .stdout(Stdio::null())
        .stderr(Stdio::from(devfull))
        .spawn()
        .unwrap();
    let up = wait_up(&mut child, port);
    let s = ServerHandle { child, port };
    up.expect("server must come up even though every stderr write fails");
    assert_eq!(authed(s.port, "POST", "/ledger/events", Some(&event("e1"))).0, 201);
    let (_, v) = authed(s.port, "GET", "/ledger/verify", None);
    assert_eq!(v, json!({"valid": true, "entries": 1}));
}

// --- ADR 0003 section 1a: money magnitude bound on NEW appends (fix wave 2) ----------------

/// Every `string_vectors` / `json_vectors` entry of the shared
/// fixtures/money_vectors.json, POSTed to the REAL server: `accept` -> 201,
/// `reject` -> 400 (column `ledger_append_expected`). Before the fix the
/// over-bound canonical strings (e.g. "1000000000000000.00") got 201.
#[test]
fn money_bound_append_verdicts_match_shared_vectors_over_real_http() {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../fixtures/money_vectors.json");
    let vectors: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let mut cases: Vec<(String, Value, &str)> = Vec::new();
    for v in vectors["string_vectors"].as_array().unwrap() {
        let input = v["input"].as_str().unwrap();
        let shown: String = if input.len() > 40 { format!("{}...({} chars)", &input[..40], input.len()) } else { input.to_string() };
        cases.push((format!("string {shown:?}"), json!(input), v["ledger_append_expected"].as_str().unwrap()));
    }
    for v in vectors["json_vectors"].as_array().unwrap() {
        let raw = v["json"].as_str().unwrap();
        cases.push((format!("json {raw}"), serde_json::from_str(raw).unwrap(), v["verdict"].as_str().unwrap()));
    }

    let log = scratch("money_vectors");
    let s = start(&log, None);
    let mut failures = Vec::new();
    let mut accepted = 0;
    for (i, (label, amount, want)) in cases.iter().enumerate() {
        let mut f = finding(&format!("f-vec-{i}"));
        f["amount_usd"] = amount.clone();
        let (st, body) = authed(s.port, "POST", "/ledger/append", Some(&f.to_string()));
        let want_status = if *want == "accept" { 201 } else { 400 };
        if st != want_status {
            failures.push(format!("{label}: want {want_status}, got {st}"));
        }
        if st == 201 {
            accepted += 1;
            assert_eq!(body["amount_usd"], *amount, "{label}: stored verbatim");
        }
    }
    assert!(failures.is_empty(), "{} mismatches:\n{}", failures.len(), failures.join("\n"));
    let (_, v) = authed(s.port, "GET", "/ledger/verify", None);
    assert_eq!(v, json!({"valid": true, "entries": accepted}));
}

/// tests/fixtures/ledger_v3_overbound_strings.jsonl was written over HTTP by
/// the REAL fix-wave-1 server binary (integration commit 23a1752), which
/// accepted over-bound strings: "12.30", "1000000000000000.00",
/// "99999999999999999999.99", "999999999999999.99" (its /ledger/verify said
/// {"entries":4,"valid":true}). The bound applies to NEW appends only: this
/// log must still load, verify, serve those amounts verbatim and extend.
#[test]
fn money_bound_log_with_over_bound_strings_from_previous_binary_still_loads() {
    let log = scratch("overbound");
    let fixture = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/ledger_v3_overbound_strings.jsonl");
    std::fs::copy(fixture, &log.0).unwrap();
    let original = std::fs::read(&log.0).unwrap();

    let s = start(&log, None);
    assert_eq!(authed(s.port, "GET", "/ledger/verify", None).1, json!({"valid": true, "entries": 4}));
    let (_, entries) = authed(s.port, "GET", "/ledger/entries", None);
    let amounts: Vec<Value> = entries.as_array().unwrap().iter().map(|e| e["amount_usd"].clone()).collect();
    assert_eq!(
        amounts,
        [json!("12.30"), json!("1000000000000000.00"), json!("99999999999999999999.99"), json!("999999999999999.99")]
    );
    let mut over = finding("f-over");
    over["amount_usd"] = json!("1000000000000000.00");
    assert_eq!(authed(s.port, "POST", "/ledger/append", Some(&over.to_string())).0, 400);
    let mut max = finding("f-max");
    max["amount_usd"] = json!("999999999999999.99");
    assert_eq!(authed(s.port, "POST", "/ledger/append", Some(&max.to_string())).0, 201);
    drop(s);

    let s = start(&log, None);
    assert_eq!(authed(s.port, "GET", "/ledger/verify", None).1, json!({"valid": true, "entries": 5}));
    assert!(std::fs::read(&log.0).unwrap().starts_with(&original), "existing lines never rewritten");
}
