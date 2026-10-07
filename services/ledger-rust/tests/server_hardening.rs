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
//!   D4  a non-ASCII header byte: until fix wave 4 tiny_http dropped the
//!       connection before any of our code ran (ADR 0003 section 5). Since the
//!       move to hyper (section 7) such requests get real answers; pinned
//!       here: auth is never bypassed, process stays up.
//!   Cosmetic: the startup log names the real bind address.
//!
//! The F5 write-failure test sets RLIMIT_FSIZE on the real process (in
//! bytes, via setrlimit(2) between fork and exec) so the kernel genuinely
//! fails the write — not a simulated failure. Fix wave 16 (portability): it
//! used `sh -c 'ulimit -f N'`, whose unit is 512 bytes in dash (Linux
//! /bin/sh) but 1024 bytes in bash 3.2 (macOS /bin/sh), so on macOS the
//! "too big" append fit and got 201. And XNU refuses an over-limit write
//! whole (EFBIG, no bytes written) where Linux writes a partial line first,
//! so the torn-tail fixture is now written directly (identical on every OS);
//! the real SIGXFSZ-kill reproduction is kept as an extra Linux-only test.

mod common;

use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use common::PortFile;
use serde_json::{json, Value};

const TOKEN: &str = "hardening-test-token-0123456789abcdef";

struct ServerHandle {
    child: Child,
    port: u16,
}

/// A spawned server and the port file it reports its bound port in.
struct Spawned {
    child: Child,
    port_file: PortFile,
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
        common::remove_ledger_files(&self.0);
        let _ = std::fs::remove_file(self.stderr_path());
    }
}
impl Scratch {
    fn stderr_path(&self) -> PathBuf {
        PathBuf::from(format!("{}.stderr", self.0.display()))
    }
}

fn scratch(label: &str) -> Scratch {
    let p = common::scratch_dir().join(format!("ledger_hardening_{label}_{}.jsonl", common::unique_suffix()));
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

/// The head checkpoint the server keeps next to `log` (sweep F-6).
fn head_file(log: &Path) -> PathBuf {
    let mut name = log.file_name().unwrap().to_os_string();
    name.push(".head");
    log.with_file_name(name)
}

/// A file-size limit (RLIMIT_FSIZE) for the spawned server, in BYTES.
#[derive(Clone, Copy)]
struct Fsize {
    bytes: u64,
    /// SIGXFSZ ignored: the over-limit write fails with EFBIG and the process
    /// lives. Otherwise the default action applies: the kernel kills it.
    ignore_sigxfsz: bool,
}

/// setrlimit(2) / signal(2), declared here so the tests need no extra crate.
/// RLIMIT_FSIZE is 1, SIGXFSZ is 25 and SIG_IGN is 1 on both Linux
/// (x86_64, aarch64) and macOS/BSD; rlim_t is a u64 on all of them.
mod sys {
    #[repr(C)]
    pub struct RLimit {
        pub cur: u64,
        pub max: u64,
    }
    extern "C" {
        pub fn setrlimit(resource: i32, rlim: *const RLimit) -> i32;
        pub fn signal(sig: i32, handler: usize) -> usize;
    }
    pub const RLIMIT_FSIZE: i32 = 1;
    pub const SIGXFSZ: i32 = 25;
    pub const SIG_IGN: usize = 1;
    pub const SIG_ERR: usize = usize::MAX;
}

/// Spawns the server binary directly (no shell), optionally under a
/// file-size limit applied in the child between fork and exec. Fix wave 21
/// (N20-M-3): the server binds port 0 and reports the port in a port file.
fn spawn(log: &Scratch, fsize: Option<Fsize>) -> Spawned {
    spawn_with(log, fsize, &[])
}

fn spawn_with(log: &Scratch, fsize: Option<Fsize>, env: &[(&str, &str)]) -> Spawned {
    let port_file = PortFile::new("hardening");
    let bin = env!("CARGO_BIN_EXE_server");
    let mut cmd = Command::new(bin);
    cmd.envs(env.iter().copied());
    if let Some(Fsize { bytes, ignore_sigxfsz }) = fsize {
        // SAFETY: only async-signal-safe syscalls run in the forked child.
        unsafe {
            cmd.pre_exec(move || {
                if ignore_sigxfsz && sys::signal(sys::SIGXFSZ, sys::SIG_IGN) == sys::SIG_ERR {
                    return Err(std::io::Error::last_os_error());
                }
                let lim = sys::RLimit { cur: bytes, max: bytes };
                if sys::setrlimit(sys::RLIMIT_FSIZE, &lim) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }
    let stderr = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log.stderr_path())
        .unwrap();
    common::ephemeral(&mut cmd, &port_file);
    let child = cmd
        .env("LEDGER_SERVICE_TOKEN", TOKEN)
        .env("LEDGER_LOG_PATH", log.0.to_str().unwrap())
        .stdout(Stdio::null())
        .stderr(Stdio::from(stderr))
        .spawn()
        .expect("failed to spawn ledger-rust server");
    Spawned { child, port_file }
}

/// Waits for the bound port, then /health. Returns Err (with the exit status)
/// if the process exits first. Ok(port).
fn wait_up(child: &mut Child, port_file: &PortFile) -> Result<u16, String> {
    let port = common::wait_port(child, port_file, Duration::from_secs(10))?;
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(st) = child.try_wait().unwrap() {
            return Err(format!("server exited before coming up: {st}"));
        }
        if let Ok((200, _)) = request(port, "GET", "/health", None, None) {
            return Ok(port);
        }
        if std::time::Instant::now() > deadline {
            return Err("server did not come up within 10s".into());
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

fn start(log: &Scratch, fsize: Option<Fsize>) -> ServerHandle {
    start_with(log, fsize, &[])
}

/// A legacy (pre-checkpoint) log needs the one-shot migrate value (AEGIS M2).
fn start_migrating(log: &Scratch) -> ServerHandle {
    let binding = common::migrate_binding(&log.0);
    common::apply_override(&log.0, TOKEN, "LEDGER_MIGRATE_LEGACY", &binding);
    start_with(log, None, &[])
}

fn start_with(log: &Scratch, fsize: Option<Fsize>, env: &[(&str, &str)]) -> ServerHandle {
    let Spawned { mut child, port_file } = spawn_with(log, fsize, env);
    match wait_up(&mut child, &port_file) {
        Ok(port) => ServerHandle { child, port },
        Err(e) => {
            let _ = child.kill();
            let _ = child.wait();
            let stderr = std::fs::read_to_string(log.stderr_path()).unwrap_or_default();
            panic!("{e}\n--- server stderr ---\n{stderr}");
        }
    }
}

/// Runs the server to completion and returns (success, stderr) — for logs
/// the server must refuse to open.
fn run_expecting_exit(log: &Scratch) -> (bool, String) {
    let Spawned { mut child, port_file } = spawn(log, None);
    let r = wait_up(&mut child, &port_file);
    let _ = child.kill();
    let status = child.wait().unwrap();
    let stderr = std::fs::read_to_string(log.stderr_path()).unwrap_or_default();
    (r.is_ok() || status.success(), stderr)
}

/// Sends raw bytes and reads one response to its Content-Length (fix wave 21,
/// N20-M-1: never to EOF). Returns (status, body).
fn raw(port: u16, bytes: &[u8]) -> std::io::Result<(u16, String)> {
    let resp = common::exchange(port, bytes, Duration::from_secs(5))?;
    Ok((resp.status, resp.text()))
}

fn request(port: u16, method: &str, path: &str, auth: Option<&str>, body: Option<&str>) -> std::io::Result<(u16, String)> {
    raw(port, &common::raw_request(method, path, auth, body.unwrap_or("")))
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

/// AEGIS F5: a crash mid-write leaves a partial, unterminated final line.
/// Before the fix, the restart refused to start ("EOF while parsing").
///
/// Fix wave 16: the torn tail is written directly — four real entries from
/// the real server, then the fourth line cut in half with no newline, which
/// is exactly what a process killed mid-`write` leaves behind. This is
/// deterministic and identical on every OS. (The old fixture relied on the
/// kernel killing the process part-way through a write under RLIMIT_FSIZE;
/// XNU refuses an over-limit write whole, so on macOS no torn line ever
/// appeared. That kernel-kill reproduction is kept, Linux-only, in
/// `f5_real_sigxfsz_kill_mid_write_leaves_a_torn_tail_that_recovers`.)
#[test]
fn f5_crash_mid_write_torn_tail_is_truncated_preserved_and_restart_succeeds() {
    let log = scratch("torn");
    // Sweep F-6: an append is acknowledged only after the head checkpoint is
    // written, so a process killed mid-write of the 4th line never wrote the
    // checkpoint for it: the head file is the one from after the 3rd append.
    let head_after_three = {
        let s = start(&log, None);
        for i in 0..3 {
            assert_eq!(authed(s.port, "POST", "/ledger/events", Some(&event(&format!("e{i}")))).0, 201);
        }
        let head = std::fs::read(head_file(&log.0)).unwrap();
        assert_eq!(authed(s.port, "POST", "/ledger/events", Some(&event("e3"))).0, 201);
        head
    };
    let full = std::fs::read(&log.0).unwrap();
    let lines: Vec<&[u8]> = full.split_inclusive(|&b| b == b'\n').collect();
    assert_eq!(lines.len(), 4);
    assert!(lines.iter().all(|l| l.ends_with(b"\n")));
    let complete: usize = lines[..3].iter().map(|l| l.len()).sum();
    std::fs::write(&log.0, &full[..complete + lines[3].len() / 2]).unwrap();
    std::fs::write(head_file(&log.0), &head_after_three).unwrap();
    assert_torn_tail_recovers(&log, 3);
}

/// The real-crash reproduction (Linux only): the server runs with
/// RLIMIT_FSIZE = 1024 bytes and the default SIGXFSZ action, so the Linux
/// kernel writes the part of the line that fits and then KILLS the process,
/// leaving a genuine torn final line. XNU (macOS) rejects the whole
/// over-limit write before writing any byte, so it cannot produce this
/// shape; the deterministic test above covers macOS.
#[cfg(target_os = "linux")]
#[test]
fn f5_real_sigxfsz_kill_mid_write_leaves_a_torn_tail_that_recovers() {
    let log = scratch("torn_kill");
    let mut acked = 0usize;
    {
        // The limit applies to every file the server writes, its captured stderr included: 4 KiB leaves room for
        // the startup lines (AEGIS M2 added a structured `ledger_created` line; macOS temp paths are long) and is
        // still reached by the log within the first dozen events.
        let s = start(&log, Some(Fsize { bytes: 4096, ignore_sigxfsz: false }));
        for i in 0..40 {
            match request(s.port, "POST", "/ledger/events", Some(&format!("Bearer {TOKEN}")), Some(&event(&format!("e{i}")))) {
                Ok((201, _)) => acked += 1,
                _ => break, // process killed by SIGXFSZ mid-write: no response
            }
        }
    }
    assert!(acked >= 1, "at least one event must be acknowledged before the limit");
    assert_torn_tail_recovers(&log, acked);
}

/// Given a log whose first `acked` lines are complete entries followed by a
/// torn, unterminated tail: restart truncates the tail, preserves it byte for
/// byte in exactly one side file, logs loudly, and the chain keeps growing.
fn assert_torn_tail_recovers(log: &Scratch, acked: usize) {
    let before = std::fs::read(&log.0).unwrap();
    assert!(!before.ends_with(b"\n"), "precondition: the crash left a torn, unterminated final line");
    let last_nl = before.iter().rposition(|&b| b == b'\n').map(|p| p + 1).unwrap_or(0);
    let torn = before[last_nl..].to_vec();
    assert!(!torn.is_empty());
    assert_eq!(before[..last_nl].iter().filter(|&&b| b == b'\n').count(), acked, "every acked entry is a complete line");

    // Restart with no limit: must come up with exactly the acknowledged entries.
    let s = start(log, None);
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
    let s = start(log, None);
    let (_, v) = authed(s.port, "GET", "/ledger/verify", None);
    assert_eq!(v, json!({"valid": true, "entries": acked + 1}));
}

// --- F5 (related): a failed write is rolled back ------------------------------------

/// SIGXFSZ ignored + RLIMIT_FSIZE = 512 bytes: a genuine write failure
/// (EFBIG) in the real process. On Linux the kernel first writes the PARTIAL
/// line that fits; on macOS XNU refuses the whole write. Either way the
/// append must return 500, the file must be back at its pre-append bytes,
/// and memory must not advance. Before the fix the partial bytes stayed on
/// disk and the next restart refused to start. A second phase forces the
/// zero-bytes-written shape on every OS (limit = current file length).
#[test]
fn f5_failed_write_is_rolled_back_on_disk_and_in_memory() {
    let log = scratch("writefail");
    let s = start(&log, Some(Fsize { bytes: 512, ignore_sigxfsz: true }));

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
    drop(s);

    // Zero bytes can be written (limit == current length): the shape XNU
    // produces for every over-limit write, forced here on every OS.
    let s = start(&log, Some(Fsize { bytes: good.len() as u64, ignore_sigxfsz: true }));
    let (st, v) = authed(s.port, "POST", "/ledger/events", Some(&event("too-big-1")));
    assert_eq!(st, 500, "a write that writes nothing must be a 500: {v}");
    assert_eq!(std::fs::read(&log.0).unwrap(), good, "file untouched");
    let (_, entries) = authed(s.port, "GET", "/ledger/entries", None);
    assert_eq!(entries.as_array().unwrap().len(), 1, "memory did not advance");
    drop(s);

    let s = start(&log, None);
    let (_, v) = authed(s.port, "GET", "/ledger/verify", None);
    assert_eq!(v, json!({"valid": true, "entries": 1}));
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

    let s = start_migrating(&log);
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

// --- D4: non-ASCII header bytes (was a pinned tiny_http limitation) ------------------------

/// Before fix wave 4, tiny_http 0.12.0 rejected any non-ASCII byte in the
/// request head inside its connection thread and closed the socket without a
/// response (this test used to pin that). The server now runs on hyper,
/// which accepts obs-text bytes (0x80-0xFF) in header VALUES as RFC 9110
/// allows, so these requests get real answers. What must still hold:
/// nothing bypasses auth — an Authorization value that is not visible ASCII
/// is a 401 — and a correctly authenticated request with an unrelated
/// non-ASCII header is processed normally (recorded exactly once).
#[test]
fn d4_non_ascii_header_bytes_get_real_answers_and_never_bypass_auth() {
    let log = scratch("d4");
    let mut s = start(&log, None);
    let body = finding("f-nonascii").to_string();
    let cases: Vec<(Vec<u8>, u16)> = vec![
        (b"GET /health HTTP/1.1\r\nHost: x\r\nX-Note: caf\xc3\xa9\r\nConnection: close\r\n\r\n".to_vec(), 200),
        (
            format!("GET /ledger/entries HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nX-Note: \u{e9}\r\nConnection: close\r\n\r\n").into_bytes(),
            200,
        ),
        (b"GET /ledger/entries HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer caf\xe9\r\nConnection: close\r\n\r\n".to_vec(), 401),
        (
            format!("GET /ledger/entries HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\u{e9}\r\nConnection: close\r\n\r\n")
                .into_bytes(),
            401,
        ),
        (
            format!(
                "POST /ledger/append HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nX-Note: \u{2713}\r\n\
                 Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .into_bytes(),
            201,
        ),
    ];
    for (i, (req, want)) in cases.iter().enumerate() {
        let (status, resp) = raw(s.port, req).unwrap();
        assert_eq!(status, *want, "case {i}: {resp:?}");
        assert!(s.child.try_wait().unwrap().is_none(), "case {i}: process must stay up");
    }
    // Still serving, still enforcing auth; only the authenticated POST wrote.
    assert_eq!(request(s.port, "GET", "/health", None, None).unwrap().0, 200);
    assert_eq!(request(s.port, "GET", "/ledger/entries", None, None).unwrap().0, 401);
    let (st, entries) = authed(s.port, "GET", "/ledger/entries", None);
    assert_eq!(st, 200);
    let entries = entries.as_array().unwrap();
    assert_eq!(entries.len(), 1);
    assert_eq!(entries[0]["finding_id"], "f-nonascii");
    assert_eq!(std::fs::read_to_string(&log.0).unwrap().lines().count(), 1);
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
/// pointed somewhere every write fails, the server must still start, serve,
/// record and verify.
///
/// Fix wave 16: this opened /dev/full, which macOS does not have (the test
/// panicked with NotFound before starting the server). Now every target is
/// tried that exists on the OS: a socket whose peer is closed (EPIPE; SIGPIPE
/// is ignored by the Rust runtime) and a read-only descriptor (EBADF) on
/// every Unix, plus /dev/full (ENOSPC) where it exists (Linux).
#[test]
fn unwritable_stderr_does_not_kill_the_server() {
    use std::os::fd::OwnedFd;
    use std::os::unix::net::UnixStream;

    let mut targets: Vec<(&str, Stdio)> = Vec::new();
    let (w, r) = UnixStream::pair().unwrap();
    drop(r);
    targets.push(("socket with closed peer (EPIPE)", Stdio::from(OwnedFd::from(w))));
    let ro = scratch("stderr_ro");
    std::fs::write(&ro.0, b"").unwrap();
    targets.push(("read-only descriptor (EBADF)", Stdio::from(std::fs::File::open(&ro.0).unwrap())));
    if Path::new("/dev/full").exists() {
        targets.push(("/dev/full (ENOSPC)", Stdio::from(std::fs::OpenOptions::new().write(true).open("/dev/full").unwrap())));
    }

    for (name, stderr) in targets {
        let log = scratch("devfull");
        // the port comes back in a file, not on stderr (which cannot be written here)
        let port_file = PortFile::new("devfull");
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_server"));
        common::ephemeral(&mut cmd, &port_file);
        let mut child = cmd
            .env("LEDGER_SERVICE_TOKEN", TOKEN)
            .env("LEDGER_LOG_PATH", log.0.to_str().unwrap())
            .stdout(Stdio::null())
            .stderr(stderr)
            .spawn()
            .unwrap();
        let up = wait_up(&mut child, &port_file);
        let port = *up.as_ref().unwrap_or(&0);
        let mut s = ServerHandle { child, port };
        up.unwrap_or_else(|e| panic!("{name}: server must come up even though every stderr write fails: {e}"));
        assert_eq!(authed(s.port, "POST", "/ledger/events", Some(&event("e1"))).0, 201, "{name}");
        let (_, v) = authed(s.port, "GET", "/ledger/verify", None);
        assert_eq!(v, json!({"valid": true, "entries": 1}), "{name}");
        assert!(s.child.try_wait().unwrap().is_none(), "{name}: process must stay up");
    }
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

    let s = start_migrating(&log);
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
