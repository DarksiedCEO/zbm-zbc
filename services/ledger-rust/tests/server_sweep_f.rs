//! Bug sweep F (Oct 6 2026, integration 5d49ee9), against the REAL compiled
//! server binary over real TCP — one test (or a few) per reproduced finding.
//! Every test here fails against the 5d49ee9 binary and passes after the fix
//! (the probes they reproduce are named in each doc comment; the sweep's
//! probes were `probe_ledger.py` P1-P7 and `probe_scale.py`).
//!
//! Tokens are plain labelled test strings (never secret-shaped), padded to the
//! 32-byte minimum (F-12). Per-caller token hashes are computed at run time.

mod common;

use std::io::Write as _;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use common::PortFile;
use ledger_rust::{EventInput, Ledger};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const TOKEN: &str = "sweep-f-shared-test-token-0123456789";

/// The scratch log, its side files and the server's captured stderr.
struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        common::remove_ledger_files(&self.0);
        let _ = std::fs::remove_file(self.stderr_path());
        let _ = std::fs::remove_file(self.callers_path());
        let dir = self.0.parent().unwrap().to_path_buf();
        let prefix = format!("{}.torn-", self.0.file_name().unwrap().to_str().unwrap());
        for e in std::fs::read_dir(dir).unwrap().flatten() {
            if e.file_name().to_str().unwrap().starts_with(&prefix) {
                let _ = std::fs::remove_file(e.path());
            }
        }
    }
}

impl Scratch {
    fn new(label: &str) -> Scratch {
        let p = common::scratch_dir().join(format!("ledger_sweepf_{label}_{}.jsonl", common::unique_suffix()));
        Scratch(p)
    }
    fn stderr_path(&self) -> PathBuf {
        PathBuf::from(format!("{}.stderr", self.0.display()))
    }
    fn callers_path(&self) -> PathBuf {
        PathBuf::from(format!("{}.callers.json", self.0.display()))
    }
    fn head_path(&self) -> PathBuf {
        PathBuf::from(format!("{}.head", self.0.display()))
    }
    fn stderr(&self) -> String {
        std::fs::read_to_string(self.stderr_path()).unwrap_or_default()
    }
}

struct Server {
    child: Child,
    port: u16,
    _pf: PortFile,
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// The server on `log`, with LEDGER_SERVICE_TOKEN=TOKEN unless `env`
/// overrides it (a `None` value removes the variable).
fn spawn(log: &Scratch, env: &[(&str, Option<&str>)]) -> (Child, PortFile) {
    let pf = PortFile::new("sweepf");
    let stderr = std::fs::OpenOptions::new().create(true).append(true).open(log.stderr_path()).unwrap();
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_server"));
    cmd.env("LEDGER_SERVICE_TOKEN", TOKEN)
        .env("LEDGER_LOG_PATH", log.0.to_str().unwrap())
        .env_remove("LEDGER_CALLERS_FILE")
        .env_remove("LEDGER_ALLOW_SHARED_TOKEN")
        .env_remove("LEDGER_ALLOW_RESET")
        .stdout(Stdio::null())
        .stderr(Stdio::from(stderr));
    common::ephemeral(&mut cmd, &pf);
    for (k, v) in env {
        match v {
            Some(v) => cmd.env(k, v),
            None => cmd.env_remove(k),
        };
    }
    (cmd.spawn().expect("spawn ledger-rust"), pf)
}

fn try_start(log: &Scratch, env: &[(&str, Option<&str>)]) -> Result<Server, String> {
    let (mut child, pf) = spawn(log, env);
    match common::wait_port(&mut child, &pf, Duration::from_secs(15)) {
        Ok(port) => Ok(Server { child, port, _pf: pf }),
        Err(e) => {
            let _ = child.kill();
            let _ = child.wait();
            Err(e)
        }
    }
}

fn start(log: &Scratch, env: &[(&str, Option<&str>)]) -> Server {
    try_start(log, env).unwrap_or_else(|e| panic!("{e}\n--- server stderr ---\n{}", log.stderr()))
}

/// Runs the server expecting it to refuse to start: Ok(exit code), or Err if
/// it came up.
fn refused(log: &Scratch, env: &[(&str, Option<&str>)]) -> Result<i32, String> {
    let (mut child, pf) = spawn(log, env);
    let deadline = std::time::Instant::now() + Duration::from_secs(15);
    loop {
        if let Some(st) = child.try_wait().unwrap() {
            return Ok(st.code().unwrap_or(-1));
        }
        if pf.read().is_some() {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("the server started\n--- stderr ---\n{}", log.stderr()));
        }
        if std::time::Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err("the server neither started nor exited".into());
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn http(port: u16, method: &str, path: &str, token: Option<&str>, headers: &[(&str, &str)], body: &str) -> (u16, String) {
    let mut extra = String::new();
    if let Some(t) = token {
        extra.push_str(&format!("Authorization: Bearer {t}\r\n"));
    }
    for (k, v) in headers {
        extra.push_str(&format!("{k}: {v}\r\n"));
    }
    let raw = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{extra}Content-Type: application/json\r\nContent-Length: {}\r\n\
         Connection: close\r\n\r\n{body}",
        body.len()
    );
    let r = common::exchange(port, raw.as_bytes(), Duration::from_secs(60)).expect("exchange");
    (r.status, r.text())
}

fn get(port: u16, path: &str) -> (u16, String) {
    http(port, "GET", path, Some(TOKEN), &[], "")
}

fn get_json(port: u16, path: &str) -> Value {
    let (st, text) = get(port, path);
    assert_eq!(st, 200, "{path}: {text}");
    serde_json::from_str(&text).unwrap()
}

fn event_body(event_id: &str, department: &str, event_type: &str) -> String {
    json!({
        "event_id": event_id, "department": department, "event_type": event_type, "actor": "sweep_f_test",
        "subject_id": "subject_1", "payload_sha256": "ab".repeat(32), "summary": "sweep F test event"
    })
    .to_string()
}

fn finding_body(finding_id: &str, amount: &str) -> String {
    json!({
        "finding_id": finding_id, "agent_id": "agent-a", "entity_id": "ord_1", "leak_category": "discount_misuse",
        "amount_usd": amount, "value_classification": "observed", "decision_confidence": "high"
    })
    .to_string()
}

fn post_event(port: u16, token: &str, body: &str) -> (u16, String) {
    http(port, "POST", "/ledger/events", Some(token), &[], body)
}

fn post_finding(port: u16, token: &str, headers: &[(&str, &str)], body: &str) -> (u16, String) {
    http(port, "POST", "/ledger/append", Some(token), headers, body)
}

fn entry_count(port: u16) -> u64 {
    get_json(port, "/ledger/verify")["entries"].as_u64().unwrap()
}

fn fill(port: u16, n: usize) {
    for i in 0..n {
        assert_eq!(post_finding(port, TOKEN, &[], &finding_body(&format!("f-{i}"), "1.00")).0, 201);
    }
}

// --- F-1: single writer -----------------------------------------------------------

/// Probe P1: two servers on one log both started and both appended (seq 0
/// twice), forking the chain; the next restart refused to start. Now the
/// second refuses to start, the first keeps serving, and the log stays one
/// chain.
#[test]
fn f1_a_second_server_on_the_same_log_refuses_to_start() {
    let log = Scratch::new("f1");
    let a = start(&log, &[]);
    fill(a.port, 1);
    let code = refused(&log, &[]).unwrap_or_else(|e| panic!("second writer: {e}"));
    assert_eq!(code, 1);
    let err = log.stderr();
    assert!(err.contains("locked by another writer") && err.contains(".lock"), "{err}");
    fill(a.port, 1); // f-0 again, a second entry: the first server is unaffected
    assert_eq!(get_json(a.port, "/ledger/verify"), json!({"valid": true, "entries": 2}));
    drop(a);
    let c = start(&log, &[]);
    assert_eq!(get_json(c.port, "/ledger/verify"), json!({"valid": true, "entries": 2}), "one chain on disk");
}

// --- F-6: deletion and rollback ---------------------------------------------------

/// Probe P2: deleting the whole log restarted as a fresh, "valid" ledger.
#[test]
fn f6_a_deleted_log_refuses_restart_and_only_an_explicit_reset_starts_fresh() {
    let log = Scratch::new("f6_deleted");
    {
        let s = start(&log, &[]);
        fill(s.port, 3);
    }
    std::fs::remove_file(&log.0).unwrap();
    assert_eq!(refused(&log, &[]).unwrap_or_else(|e| panic!("{e}")), 1);
    assert!(log.stderr().contains("MISSING"), "{}", log.stderr());
    assert!(!log.0.exists(), "a refused start creates no log");
    // AEGIS M3: the bare "1" of sweep F (and any other unbound value) is refused; the refusal names the value.
    for bad in ["true", "yes", "1"] {
        assert_eq!(refused(&log, &[("LEDGER_ALLOW_RESET", Some(bad))]).unwrap_or_else(|e| panic!("{e}")), 1, "{bad}");
    }
    let v = advised(&log.stderr(), "LEDGER_ALLOW_RESET");
    let err = common::apply_override(&log.0, TOKEN, "LEDGER_ALLOW_RESET", &v);
    assert!(err.contains("OPERATOR RESET"), "{err}");
    let s = start(&log, &[]);
    assert_eq!(get_json(s.port, "/ledger/verify"), json!({"valid": true, "entries": 0}));
}

/// Probe P3: dropping whole acknowledged lines from the end of the log
/// verified as valid. And a log replaced by another valid chain.
#[test]
fn f6_a_truncated_or_replaced_log_refuses_restart() {
    let log = Scratch::new("f6_trunc");
    {
        let s = start(&log, &[]);
        fill(s.port, 3);
    }
    let text = std::fs::read_to_string(&log.0).unwrap();
    std::fs::write(&log.0, text.lines().next().unwrap().to_string() + "\n").unwrap();
    assert_eq!(refused(&log, &[]).unwrap_or_else(|e| panic!("{e}")), 1);
    assert!(log.stderr().contains("holds 1 entries but the head checkpoint records 3"), "{}", log.stderr());

    let other = Scratch::new("f6_other");
    {
        let s = start(&other, &[]);
        for i in 0..3 {
            assert_eq!(post_finding(s.port, TOKEN, &[], &finding_body(&format!("g-{i}"), "9.00")).0, 201);
        }
    }
    std::fs::copy(&other.0, &log.0).unwrap();
    assert_eq!(refused(&log, &[]).unwrap_or_else(|e| panic!("{e}")), 1);
    assert!(log.stderr().contains("does not contain the checkpointed head"), "{}", log.stderr());
}

/// F-6: GET /ledger/head is the durable checkpoint (authenticated).
#[test]
fn f6_get_ledger_head_reports_the_checkpoint() {
    let log = Scratch::new("f6_head");
    let s = start(&log, &[]);
    let h0 = get_json(s.port, "/ledger/head");
    assert_eq!(h0["entries"], 0);
    assert_eq!(h0["head_seq"], Value::Null);
    fill(s.port, 2);
    let h = get_json(s.port, "/ledger/head");
    let entries: Value = get_json(s.port, "/ledger/entries");
    assert_eq!(h, json!({"entries": 2, "head_seq": 1, "head_hash": entries[1]["hash"]}));
    let on_disk: Value = serde_json::from_str(&std::fs::read_to_string(log.head_path()).unwrap()).unwrap();
    assert_eq!(on_disk, h);
    assert_eq!(http(s.port, "GET", "/ledger/head", None, &[], "").0, 401);
}

// --- F-13: torn tail and blank lines ----------------------------------------------

/// Probe P4: removing ONE byte (the final newline) of an acknowledged log
/// moved the last acknowledged entry to a side file and verified "valid".
#[test]
fn f13_an_acknowledged_entry_without_its_newline_refuses_restart() {
    let log = Scratch::new("f13_nl");
    {
        let s = start(&log, &[]);
        fill(s.port, 2);
    }
    let full = std::fs::read(&log.0).unwrap();
    std::fs::write(&log.0, &full[..full.len() - 1]).unwrap();
    assert_eq!(refused(&log, &[]).unwrap_or_else(|e| panic!("{e}")), 1);
    assert!(log.stderr().contains("complete JSON value"), "{}", log.stderr());
    assert_eq!(std::fs::read(&log.0).unwrap(), &full[..full.len() - 1], "file untouched");
}

/// F-13: a blank line was silently skipped on load.
#[test]
fn f13_a_blank_line_refuses_restart() {
    let log = Scratch::new("f13_blank");
    {
        let s = start(&log, &[]);
        fill(s.port, 2);
    }
    let text = std::fs::read_to_string(&log.0).unwrap();
    let l: Vec<&str> = text.lines().collect();
    std::fs::write(&log.0, format!("{}\n\n{}\n", l[0], l[1])).unwrap();
    assert_eq!(refused(&log, &[]).unwrap_or_else(|e| panic!("{e}")), 1);
    assert!(log.stderr().contains("blank line"), "{}", log.stderr());
}

// --- F-7: finding idempotency -----------------------------------------------------

/// Probe P5: 60 posts of 5 finding_ids made 60 entries. With an
/// Idempotency-Key the finding_id gets event_id semantics; without one the
/// append is unchanged.
#[test]
fn f7_idempotency_key_makes_a_finding_append_idempotent_and_is_opt_in() {
    let log = Scratch::new("f7");
    let s = start(&log, &[]);
    let key = [("Idempotency-Key", "f-1")];
    let (st, first) = post_finding(s.port, TOKEN, &key, &finding_body("f-1", "10.00"));
    assert_eq!(st, 201, "{first}");
    let (st, again) = post_finding(s.port, TOKEN, &key, &finding_body("f-1", "10.00"));
    assert_eq!(st, 200, "{again}");
    assert_eq!(serde_json::from_str::<Value>(&again).unwrap(), serde_json::from_str::<Value>(&first).unwrap());
    let (st, conflict) = post_finding(s.port, TOKEN, &key, &finding_body("f-1", "11.00"));
    assert_eq!(st, 409, "{conflict}");
    assert_eq!(serde_json::from_str::<Value>(&conflict).unwrap()["finding_id"], "f-1");
    let (st, mismatch) = post_finding(s.port, TOKEN, &[("Idempotency-Key", "f-2")], &finding_body("f-1", "10.00"));
    assert_eq!(st, 400, "{mismatch}");
    assert_eq!(entry_count(s.port), 1, "retry, conflict and mismatch wrote nothing");
    // No header: unchanged, a duplicate is appended.
    assert_eq!(post_finding(s.port, TOKEN, &[], &finding_body("f-1", "10.00")).0, 201);
    assert_eq!(entry_count(s.port), 2);
    // The index survives a restart.
    drop(s);
    let s = start(&log, &[]);
    assert_eq!(post_finding(s.port, TOKEN, &key, &finding_body("f-1", "10.00")).0, 200);
    assert_eq!(post_finding(s.port, TOKEN, &key, &finding_body("f-1", "12.00")).0, 409);
    assert_eq!(entry_count(s.port), 2);
}

// --- F-12: token length -----------------------------------------------------------

/// Probe P7: a 1-character LEDGER_SERVICE_TOKEN was accepted.
#[test]
fn f12_a_shared_token_under_32_bytes_refuses_to_start() {
    let log = Scratch::new("f12");
    for short in ["x", "0123456789012345678901234567890"] {
        assert_eq!(refused(&log, &[("LEDGER_SERVICE_TOKEN", Some(short))]).unwrap_or_else(|e| panic!("{e}")), 1);
        assert!(log.stderr().contains("at least 32"), "{}", log.stderr());
    }
    let exactly_32 = "01234567890123456789012345678901";
    let s = start(&log, &[("LEDGER_SERVICE_TOKEN", Some(exactly_32))]);
    assert_eq!(http(s.port, "GET", "/ledger/verify", Some(exactly_32), &[], "").0, 200);
}

// --- F-2: paginated reads, incremental verify, appends not starved ---------------

/// GET /ledger/entries?after_seq=&limit=&department=&event_type=; with no
/// query the whole ledger exactly as before.
#[test]
fn f2_entries_are_paginated_and_filtered_and_unchanged_without_a_query() {
    let log = Scratch::new("f2_pages");
    let s = start(&log, &[]);
    // seq: 0 finding, 1 sales/note, 2 finance/payout, 3 sales/call, 4 finding, 5 sales/note
    assert_eq!(post_finding(s.port, TOKEN, &[], &finding_body("f-0", "1.00")).0, 201);
    for (i, (d, t)) in [("sales", "note"), ("finance", "payout"), ("sales", "call")].iter().enumerate() {
        assert_eq!(post_event(s.port, TOKEN, &event_body(&format!("e-{i}"), d, t)).0, 201);
    }
    assert_eq!(post_finding(s.port, TOKEN, &[], &finding_body("f-4", "1.00")).0, 201);
    assert_eq!(post_event(s.port, TOKEN, &event_body("e-5", "sales", "note")).0, 201);

    let all = get_json(s.port, "/ledger/entries");
    let all = all.as_array().unwrap();
    assert_eq!(all.len(), 6);
    let seqs = |path: &str| -> Vec<u64> {
        get_json(s.port, path).as_array().unwrap().iter().map(|e| e["seq"].as_u64().unwrap()).collect()
    };
    assert_eq!(seqs("/ledger/entries?limit=2"), [0, 1]);
    assert_eq!(seqs("/ledger/entries?after_seq=1&limit=2"), [2, 3]);
    assert_eq!(seqs("/ledger/entries?after_seq=3"), [4, 5]);
    assert_eq!(seqs("/ledger/entries?after_seq=5"), Vec::<u64>::new());
    assert_eq!(seqs("/ledger/entries?after_seq=99999999999"), Vec::<u64>::new());
    assert_eq!(seqs("/ledger/entries?department=sales"), [1, 3, 5]);
    assert_eq!(seqs("/ledger/entries?department=sales&event_type=note"), [1, 5]);
    assert_eq!(seqs("/ledger/entries?event_type=payout"), [2]);
    assert_eq!(seqs("/ledger/entries?department=sales&after_seq=1&limit=1"), [3]);
    // A page is the same entries the full read returns.
    assert_eq!(get_json(s.port, "/ledger/entries?after_seq=0&limit=1")[0], all[1]);
    for bad in [
        "limit=0", "limit=10001", "limit=-1", "after_seq=x", "after_seq=", "department=Sales", "department=a%20b",
        "offset=1", "limit=1&limit=2", "limit", "kind=event",
    ] {
        let (st, body) = get(s.port, &format!("/ledger/entries?{bad}"));
        assert_eq!(st, 400, "{bad}: {body}");
    }
}

/// GET /ledger/verify keeps its responses; `?full=1` re-verifies everything.
#[test]
fn f2_verify_is_incremental_with_a_full_reverify_on_demand() {
    let log = Scratch::new("f2_verify");
    let s = start(&log, &[]);
    fill(s.port, 3);
    assert_eq!(get_json(s.port, "/ledger/verify"), json!({"valid": true, "entries": 3}));
    fill(s.port, 2);
    assert_eq!(get_json(s.port, "/ledger/verify"), json!({"valid": true, "entries": 5}));
    assert_eq!(get_json(s.port, "/ledger/verify?full=1"), json!({"valid": true, "entries": 5}));
    assert_eq!(get_json(s.port, "/ledger/verify?full=0"), json!({"valid": true, "entries": 5}));
    assert_eq!(get(s.port, "/ledger/verify?full=2").0, 400);
}

/// Writes a valid log of `n` events with the library's own hashing.
fn write_big_log(path: &Path, n: usize) {
    let mut ledger = Ledger::new();
    let mut out = std::io::BufWriter::new(std::fs::File::create(path).unwrap());
    for i in 0..n {
        ledger.append_event(EventInput {
            event_id: format!("big-{i}"),
            department: "sales".into(),
            event_type: "log_anchor".into(),
            actor: "sales_rep_agent".into(),
            subject_id: "log_1".into(),
            payload_sha256: "a".repeat(64),
            summary: format!("bulk entry #{i}"),
        });
        writeln!(out, "{}", serde_json::to_string(&ledger.entries()[i]).unwrap()).unwrap();
    }
    out.flush().unwrap();
    drop(out);
    common::write_head_for(path);
}

/// Probe P6/scale: full reads held the one mutex while serializing, on a
/// blocking pool readers could fill, so an append queued behind every reader
/// in front of it. Now an append goes ahead of the next reader chunk. Order,
/// not wall-clock: 24 full reads are started first, and the append must be
/// answered before most of them are.
#[test]
fn f2_an_append_is_not_queued_behind_concurrent_full_reads() {
    let log = Scratch::new("f2_starve");
    write_big_log(&log.0, 5_000);
    let s = start(&log, &[]);
    let port = s.port;
    let done = Arc::new(AtomicUsize::new(0));
    let readers: Vec<_> = (0..24)
        .map(|_| {
            let done = Arc::clone(&done);
            std::thread::spawn(move || {
                // Raw exchange: a reader still queued at REQUEST_DEADLINE on a slow machine is dropped by the
                // server (by design); that is not what this test is about, so it is not a failure here.
                let raw = common::raw_request("GET", "/ledger/entries", Some(&format!("Bearer {TOKEN}")), "");
                let status = common::exchange(port, &raw, Duration::from_secs(60)).ok().map(|r| r.status);
                done.fetch_add(1, Ordering::SeqCst);
                status
            })
        })
        .collect();
    std::thread::sleep(Duration::from_millis(200)); // let the reads get going (not an assertion)
    let (st, body) = post_event(port, TOKEN, &event_body("after-readers", "sales", "note"));
    let finished_before_append = done.load(Ordering::SeqCst);
    assert_eq!(st, 201, "{body}");
    for r in readers {
        let status = r.join().unwrap();
        assert!(status.is_none() || status == Some(200), "{status:?}");
    }
    assert!(
        finished_before_append <= 8,
        "the append was answered only after {finished_before_append} of 24 full reads"
    );
    assert_eq!(serde_json::from_str::<Value>(&body).unwrap()["seq"], 5_000);
}

// --- F-4: per-caller tokens with department scopes -------------------------------

const SALES_TOKEN: &str = "sweep-f-sales-writer-test-token-0123";
const FINANCE_TOKEN: &str = "sweep-f-finance-writer-test-token-01";
const RR_TOKEN: &str = "sweep-f-revenue-recovery-test-token-0";
const READER_TOKEN: &str = "sweep-f-dashboard-reader-test-token-0";

fn sha256_hex(s: &str) -> String {
    Sha256::digest(s.as_bytes()).iter().map(|b| format!("{b:02x}")).collect()
}

fn write_callers(log: &Scratch) -> String {
    let callers = json!({
        sha256_hex(SALES_TOKEN): {"caller": "sales-py", "departments": ["sales"], "scope": "write"},
        sha256_hex(FINANCE_TOKEN): {"caller": "finance-py", "departments": ["finance", "treasury"], "scope": "write"},
        sha256_hex(RR_TOKEN): {"caller": "orchestrator-go", "departments": ["revenue_recovery"], "scope": "write"},
        sha256_hex(READER_TOKEN): {"caller": "dashboard", "scope": "read", "read_all": true},
    });
    std::fs::write(log.callers_path(), callers.to_string()).unwrap();
    log.callers_path().to_str().unwrap().to_string()
}

/// The value an operator is told to set, taken from the LAST refusal in a
/// server's stderr (AEGIS M2/M3: overrides are bound to the exact state).
pub(crate) fn advised(stderr: &str, name: &str) -> String {
    let at = stderr.rfind(&format!("restart ONCE with {name}=")).unwrap_or_else(|| panic!("no {name} advice in:\n{stderr}"));
    let rest = &stderr[at + "restart ONCE with ".len() + name.len() + 1..];
    rest.split_whitespace().next().unwrap().to_string()
}

/// Probe P5 "cross-department write": any holder of the one token could
/// record evidence for any department.
#[test]
fn f4_a_cross_department_write_is_refused() {
    let log = Scratch::new("f4_dept");
    let callers = write_callers(&log);
    let s = start(&log, &[("LEDGER_CALLERS_FILE", Some(&callers))]);
    assert_eq!(post_event(s.port, SALES_TOKEN, &event_body("s-1", "sales", "note")).0, 201);
    let (st, body) = post_event(s.port, SALES_TOKEN, &event_body("fin-forged-1", "finance", "payout_approved"));
    assert_eq!(st, 403, "{body}");
    assert!(body.contains("sales-py") && body.contains("finance"), "{body}");
    assert_eq!(post_event(s.port, FINANCE_TOKEN, &event_body("fin-1", "finance", "payout_approved")).0, 201);
    assert_eq!(post_event(s.port, FINANCE_TOKEN, &event_body("tr-1", "treasury", "move")).0, 201);
    // Findings belong to revenue_recovery.
    assert_eq!(post_finding(s.port, SALES_TOKEN, &[], &finding_body("f-1", "1.00")).0, 403);
    assert_eq!(post_finding(s.port, RR_TOKEN, &[], &finding_body("f-1", "1.00")).0, 201);
    assert_eq!(post_event(s.port, RR_TOKEN, &event_body("rr-1", "sales", "note")).0, 403);
    // AEGIS M4: a write caller reads its own department; the read_all dashboard reads everything.
    let entries = http(s.port, "GET", "/ledger/entries", Some(READER_TOKEN), &[], "");
    assert_eq!(entries.0, 200, "the read_all caller can read");
    let ids: Vec<String> = serde_json::from_str::<Value>(&entries.1)
        .unwrap()
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e.get("event_id").or_else(|| e.get("finding_id")).unwrap().as_str().unwrap().to_string())
        .collect();
    assert_eq!(ids, ["s-1", "fin-1", "tr-1", "f-1"], "nothing refused was written");
    // An unknown token, and the shared token (not allowed here), are 401.
    assert_eq!(post_event(s.port, "sweep-f-unknown-caller-test-token-00", &event_body("x-1", "sales", "note")).0, 401);
    assert_eq!(post_event(s.port, TOKEN, &event_body("x-2", "sales", "note")).0, 401);
    assert_eq!(http(s.port, "GET", "/ledger/verify", Some(TOKEN), &[], "").0, 401);
    assert!(log.stderr().contains("sales-py"), "the startup log names the callers");
}

#[test]
fn f4_a_read_only_token_can_only_read_entries_verify_and_head() {
    let log = Scratch::new("f4_read");
    let callers = write_callers(&log);
    let s = start(&log, &[("LEDGER_CALLERS_FILE", Some(&callers))]);
    assert_eq!(post_event(s.port, SALES_TOKEN, &event_body("s-1", "sales", "note")).0, 201);
    for path in ["/ledger/entries", "/ledger/entries?limit=1", "/ledger/verify", "/ledger/verify?full=1", "/ledger/head"] {
        assert_eq!(http(s.port, "GET", path, Some(READER_TOKEN), &[], "").0, 200, "{path}");
    }
    let (st, body) = post_event(s.port, READER_TOKEN, &event_body("r-1", "sales", "note"));
    assert_eq!(st, 403, "{body}");
    assert!(body.contains("read-only"), "{body}");
    assert_eq!(post_finding(s.port, READER_TOKEN, &[], &finding_body("f-1", "1.00")).0, 403);
    assert_eq!(entry_count_as(s.port, READER_TOKEN), 1);
}

fn entry_count_as(port: u16, token: &str) -> u64 {
    let (st, text) = http(port, "GET", "/ledger/verify", Some(token), &[], "");
    assert_eq!(st, 200, "{text}");
    serde_json::from_str::<Value>(&text).unwrap()["entries"].as_u64().unwrap()
}

/// With per-caller tokens configured the legacy shared token works only with
/// LEDGER_ALLOW_SHARED_TOKEN=1 (then with its old, unscoped access).
#[test]
fn f4_the_shared_token_needs_an_explicit_flag_once_callers_are_configured() {
    let log = Scratch::new("f4_shared");
    let callers = write_callers(&log);
    let s = start(&log, &[("LEDGER_CALLERS_FILE", Some(&callers)), ("LEDGER_ALLOW_SHARED_TOKEN", Some("1"))]);
    assert_eq!(post_event(s.port, TOKEN, &event_body("any-1", "finance", "payout_approved")).0, 201);
    assert_eq!(post_finding(s.port, TOKEN, &[], &finding_body("f-1", "1.00")).0, 201);
    assert_eq!(post_event(s.port, SALES_TOKEN, &event_body("fin-2", "finance", "x")).0, 403, "callers stay scoped");
    assert!(log.stderr().contains("LEDGER_ALLOW_SHARED_TOKEN=1"), "{}", log.stderr());
    drop(s);
    // The flag still requires a long enough shared token.
    assert_eq!(
        refused(&log, &[
            ("LEDGER_CALLERS_FILE", Some(&callers)),
            ("LEDGER_ALLOW_SHARED_TOKEN", Some("1")),
            ("LEDGER_SERVICE_TOKEN", Some("short")),
        ])
        .unwrap_or_else(|e| panic!("{e}")),
        1
    );
    // Without the flag the shared token may even be unset.
    let s = start(&log, &[("LEDGER_CALLERS_FILE", Some(&callers)), ("LEDGER_SERVICE_TOKEN", None)]);
    assert_eq!(post_event(s.port, SALES_TOKEN, &event_body("s-9", "sales", "note")).0, 201);
}

/// LEDGER_CALLERS_FILE unset: the shared token works exactly as before (any
/// department), with a startup warning.
#[test]
fn f4_without_a_callers_file_behaviour_is_unchanged_with_a_warning() {
    let log = Scratch::new("f4_legacy");
    let s = start(&log, &[]);
    assert_eq!(post_event(s.port, TOKEN, &event_body("fin-forged-1", "finance", "payout_approved")).0, 201);
    assert_eq!(post_finding(s.port, TOKEN, &[], &finding_body("f-1", "1.00")).0, 201);
    let err = log.stderr();
    assert!(err.contains("WARNING") && err.contains("LEDGER_CALLERS_FILE is not set"), "{err}");
}

#[test]
fn f4_a_malformed_callers_file_refuses_to_start() {
    let log = Scratch::new("f4_bad");
    let h = sha256_hex(SALES_TOKEN);
    let cases = [
        String::new(),
        "{}".to_string(),
        "[]".to_string(),
        json!({"not-a-hash": {"caller": "x", "departments": ["sales"], "scope": "write"}}).to_string(),
        json!({h.to_uppercase(): {"caller": "x", "departments": ["sales"], "scope": "write"}}).to_string(),
        json!({&h: {"caller": "x", "departments": [], "scope": "write"}}).to_string(),
        json!({&h: {"caller": "x", "departments": ["Sales"], "scope": "write"}}).to_string(),
        json!({&h: {"caller": "", "departments": ["sales"], "scope": "write"}}).to_string(),
        json!({&h: {"caller": "x", "departments": ["sales"], "scope": "admin"}}).to_string(),
        json!({&h: {"caller": "x", "departments": ["sales"], "scope": "write", "extra": 1}}).to_string(),
    ];
    for content in cases {
        std::fs::write(log.callers_path(), &content).unwrap();
        let path = log.callers_path().to_str().unwrap().to_string();
        let code = refused(&log, &[("LEDGER_CALLERS_FILE", Some(&path))]).unwrap_or_else(|e| panic!("{content}: {e}"));
        assert_eq!(code, 1, "{content}");
    }
    let missing = format!("{}.absent", log.callers_path().display());
    assert_eq!(refused(&log, &[("LEDGER_CALLERS_FILE", Some(&missing))]).unwrap_or_else(|e| panic!("{e}")), 1);
}

// --- a failed bind exits cleanly ---------------------------------------------------

/// `.expect("failed to bind ...")` panicked (exit 101, a backtrace hint) when
/// the port was taken. Now a clean refusal: exit 1 and a message.
#[test]
fn a_failed_bind_exits_with_a_message_not_a_panic() {
    let log = Scratch::new("bind");
    let taken = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = taken.local_addr().unwrap().port().to_string();
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_server"));
    let out = cmd
        .env("LEDGER_SERVICE_TOKEN", TOKEN)
        .env("LEDGER_LOG_PATH", log.0.to_str().unwrap())
        .env("LEDGER_PORT", &port)
        .env("LEDGER_BIND_ADDR", "127.0.0.1")
        .env_remove("LEDGER_PORT_FILE")
        .env_remove("LEDGER_CALLERS_FILE")
        .output()
        .unwrap();
    let err = String::from_utf8_lossy(&out.stderr);
    assert_eq!(out.status.code(), Some(1), "{err}");
    assert!(err.contains("REFUSING TO START") && err.contains("cannot bind"), "{err}");
    assert!(!err.contains("panicked"), "{err}");
    drop(taken);
}

// --- AEGIS review of d6b1cd9 (Oct 7 2026): M1-M4, L1-L5 ----------------------------
//
// Each test fails against the d6b1cd9 binary.

/// M1: only a log exactly ONE entry ahead of its checkpoint (the crash window
/// between the log fsync and the checkpoint rename) starts; two ahead used to
/// start too, silently adopting entries no append acknowledged.
#[test]
fn aegis_m1_only_one_entry_ahead_of_the_checkpoint_starts() {
    let log = Scratch::new("m1");
    let mut heads = Vec::new();
    {
        let s = start(&log, &[]);
        for i in 0..3 {
            heads.push(std::fs::read(log.head_path()).unwrap());
            assert_eq!(post_finding(s.port, TOKEN, &[], &finding_body(&format!("f-{i}"), "1.00")).0, 201);
        }
    }
    std::fs::write(log.head_path(), &heads[1]).unwrap(); // two behind the log
    assert_eq!(refused(&log, &[]).unwrap_or_else(|e| panic!("{e}")), 1);
    assert!(log.stderr().contains("at most ONE entry past the checkpoint"), "{}", log.stderr());
    assert_eq!(std::fs::read(log.head_path()).unwrap(), heads[1], "the refusal changed nothing");
    std::fs::write(log.head_path(), &heads[2]).unwrap(); // one behind: the crash window
    let s = start(&log, &[]);
    assert!(log.stderr().contains("\"event\":\"checkpoint_moved_up\""), "{}", log.stderr());
    assert_eq!(get_json(s.port, "/ledger/head")["entries"], 3);
}

/// M2: a non-empty log without a checkpoint (an old binary's log, or a deleted
/// head file) used to start and get a fresh checkpoint: deleting the head file
/// and truncating the log read as an upgrade. Now it needs the one-shot,
/// log-bound LEDGER_MIGRATE_LEGACY; a new ledger is logged as a structured event.
#[test]
fn aegis_m2_a_log_without_a_checkpoint_needs_the_bound_one_shot_migrate() {
    let log = Scratch::new("m2");
    {
        let s = start(&log, &[]);
        fill(s.port, 3);
    }
    let created = log.stderr();
    assert_eq!(created.matches("\"event\":\"ledger_created\"").count(), 1, "{created}");
    std::fs::remove_file(log.head_path()).unwrap();
    let text = std::fs::read_to_string(&log.0).unwrap();
    std::fs::write(&log.0, text.lines().next().unwrap().to_string() + "\n").unwrap(); // and roll the log back
    assert_eq!(refused(&log, &[]).unwrap_or_else(|e| panic!("{e}")), 1);
    assert!(log.stderr().contains("NO head checkpoint"), "{}", log.stderr());
    assert!(!log.head_path().exists());
    for bad in ["1", "yes", "1:zz", "2:0000000000000000"] {
        assert_eq!(refused(&log, &[("LEDGER_MIGRATE_LEGACY", Some(bad))]).unwrap_or_else(|e| panic!("{e}")), 1, "{bad}");
    }
    let v = advised(&log.stderr(), "LEDGER_MIGRATE_LEGACY");
    assert_eq!(v, common::migrate_binding(&log.0));
    let err = common::apply_override(&log.0, TOKEN, "LEDGER_MIGRATE_LEGACY", &v);
    assert!(err.contains("\"event\":\"legacy_log_migrated\""), "{err}");
    // AEGIS N1: left set, it refuses the start (it is not needed any more).
    assert_eq!(refused(&log, &[("LEDGER_MIGRATE_LEGACY", Some(&v))]).unwrap_or_else(|e| panic!("{e}")), 1);
    assert!(log.stderr().contains("does not need it"), "{}", log.stderr());
    let s = start(&log, &[]);
    let head = get_json(s.port, "/ledger/head");
    assert_eq!(head["entries"], 1);
    assert_eq!(head["migrated_from"], v.as_str(), "the migration is recorded in the checkpoint");
}

/// AEGIS N1 repro over the real binary: migrate a 3-entry log (backup of the
/// directory kept), append 7, restore the backup. With d69f64a and the
/// migrate value left set, the server started on 3 entries and the 7
/// acknowledged ones were lost. Now no start succeeds while the value is set,
/// so it cannot be set at the restore; and without it the backup is refused.
#[test]
fn aegis_n1_a_restored_pre_migration_backup_is_refused() {
    let log = Scratch::new("n1");
    {
        let s = start(&log, &[]);
        fill(s.port, 3);
    }
    std::fs::remove_file(log.head_path()).unwrap();
    let backup = std::fs::read(&log.0).unwrap();
    let v = common::migrate_binding(&log.0);
    common::apply_override(&log.0, TOKEN, "LEDGER_MIGRATE_LEGACY", &v);
    assert_eq!(refused(&log, &[("LEDGER_MIGRATE_LEGACY", Some(&v))]).unwrap_or_else(|e| panic!("{e}")), 1);
    {
        let s = start(&log, &[]);
        for i in 3..10 {
            assert_eq!(post_finding(s.port, TOKEN, &[], &finding_body(&format!("f-{i}"), "1.00")).0, 201);
        }
    }
    std::fs::write(&log.0, &backup).unwrap();
    std::fs::remove_file(log.head_path()).unwrap();
    assert_eq!(refused(&log, &[]).unwrap_or_else(|e| panic!("{e}")), 1);
    assert!(log.stderr().rfind("NO head checkpoint").is_some(), "{}", log.stderr());
    // Re-setting the value by hand is an explicit, logged re-migration (the backup has no checkpoint and is
    // indistinguishable from a legacy log, ADR 0003 section 13 N1 residual): it is applied and the server still
    // exits without serving.
    let err = common::apply_override(&log.0, TOKEN, "LEDGER_MIGRATE_LEGACY", &v);
    assert!(err.contains("\"event\":\"legacy_log_migrated\""), "{err}");
}

/// M3: LEDGER_ALLOW_RESET=1 left in the environment stayed armed: any later
/// rollback started silently. Now the value is bound to (checkpoint, log head).
#[test]
fn aegis_m3_a_stale_reset_value_does_not_accept_a_later_rollback() {
    let log = Scratch::new("m3");
    {
        let s = start(&log, &[]);
        fill(s.port, 3);
    }
    let full = std::fs::read_to_string(&log.0).unwrap();
    let lines: Vec<&str> = full.lines().collect();
    std::fs::write(&log.0, format!("{}\n{}\n", lines[0], lines[1])).unwrap();
    assert_eq!(refused(&log, &[]).unwrap_or_else(|e| panic!("{e}")), 1);
    let v = advised(&log.stderr(), "LEDGER_ALLOW_RESET");
    let err = common::apply_override(&log.0, TOKEN, "LEDGER_ALLOW_RESET", &v);
    assert!(err.contains("\"event\":\"operator_reset\""), "{err}");
    // AEGIS N1: left set, it refuses the start; served only once it is unset.
    assert_eq!(refused(&log, &[("LEDGER_ALLOW_RESET", Some(&v))]).unwrap_or_else(|e| panic!("{e}")), 1);
    {
        let s = start(&log, &[]);
        assert!(get_json(s.port, "/ledger/head")["reset_from"].as_str().is_some_and(|r| r == v));
        fill(s.port, 2);
    }
    // The same value still in the environment; a new rollback.
    std::fs::write(&log.0, format!("{}\n", lines[0])).unwrap();
    assert_eq!(refused(&log, &[("LEDGER_ALLOW_RESET", Some(&v))]).unwrap_or_else(|e| panic!("{e}")), 1);
    assert!(log.stderr().contains("does not match this state"), "{}", log.stderr());
}

const COMPLIANCE_TOKEN: &str = "aegis-compliance-reader-test-token-01";
const SALES_PLUS_TOKEN: &str = "aegis-sales-reads-finance-test-token-1";

fn write_scoped_callers(log: &Scratch) -> String {
    let callers = json!({
        sha256_hex(SALES_TOKEN): {"caller": "sales-py", "departments": ["sales"], "scope": "write"},
        sha256_hex(FINANCE_TOKEN): {"caller": "finance-py", "departments": ["finance"], "scope": "write"},
        sha256_hex(RR_TOKEN): {"caller": "orchestrator-go", "departments": ["revenue_recovery"], "scope": "write"},
        sha256_hex(SALES_PLUS_TOKEN): {"caller": "sales-reporting", "departments": ["sales"], "scope": "write",
                                       "read_departments": ["finance"]},
        sha256_hex(READER_TOKEN): {"caller": "dashboard", "scope": "read", "read_all": true},
        sha256_hex(COMPLIANCE_TOKEN): {"caller": "compliance-auditor", "scope": "read", "departments": ["sales", "finance"]},
    });
    std::fs::write(log.callers_path(), callers.to_string()).unwrap();
    log.callers_path().to_str().unwrap().to_string()
}

fn seqs_as(port: u16, token: &str, path: &str) -> Vec<u64> {
    let (st, text) = http(port, "GET", path, Some(token), &[], "");
    assert_eq!(st, 200, "{path}: {text}");
    serde_json::from_str::<Value>(&text).unwrap().as_array().unwrap().iter().map(|e| e["seq"].as_u64().unwrap()).collect()
}

/// M4: every caller could read every department's evidence. Now a caller
/// reads its own departments (plus `read_departments`), a `read_all` caller
/// (dashboard, compliance, audit) reads everything, findings are visible to
/// `revenue_recovery` readers, and a filter outside the scope is a 403.
#[test]
fn aegis_m4_reads_are_scoped_by_department() {
    let log = Scratch::new("m4");
    let callers = write_scoped_callers(&log);
    let s = start(&log, &[("LEDGER_CALLERS_FILE", Some(&callers))]);
    // seq 0 sales/note, 1 finance/payout, 2 finding, 3 sales/call
    assert_eq!(post_event(s.port, SALES_TOKEN, &event_body("s-0", "sales", "note")).0, 201);
    assert_eq!(post_event(s.port, FINANCE_TOKEN, &event_body("f-1", "finance", "payout")).0, 201);
    assert_eq!(post_finding(s.port, RR_TOKEN, &[], &finding_body("rr-2", "1.00")).0, 201);
    assert_eq!(post_event(s.port, SALES_TOKEN, &event_body("s-3", "sales", "call")).0, 201);

    assert_eq!(seqs_as(s.port, SALES_TOKEN, "/ledger/entries"), [0, 3]);
    assert_eq!(seqs_as(s.port, FINANCE_TOKEN, "/ledger/entries"), [1]);
    assert_eq!(seqs_as(s.port, RR_TOKEN, "/ledger/entries"), [2], "findings belong to revenue_recovery");
    assert_eq!(seqs_as(s.port, SALES_PLUS_TOKEN, "/ledger/entries"), [0, 1, 3]);
    assert_eq!(seqs_as(s.port, COMPLIANCE_TOKEN, "/ledger/entries"), [0, 1, 3]);
    assert_eq!(seqs_as(s.port, READER_TOKEN, "/ledger/entries"), [0, 1, 2, 3]);
    // The dashboard's unscoped full read is byte-identical to the shared-token read of earlier binaries.
    let (_, all) = http(s.port, "GET", "/ledger/entries", Some(READER_TOKEN), &[], "");
    let (_, paged) = http(s.port, "GET", "/ledger/entries?limit=10000", Some(READER_TOKEN), &[], "");
    assert_eq!(all, paged);
    // Paging and filters inside the scope.
    assert_eq!(seqs_as(s.port, SALES_TOKEN, "/ledger/entries?after_seq=0&limit=1"), [3]);
    assert_eq!(seqs_as(s.port, SALES_TOKEN, "/ledger/entries?department=sales&event_type=call"), [3]);
    assert_eq!(seqs_as(s.port, SALES_TOKEN, "/ledger/entries?event_type=payout"), Vec::<u64>::new());
    assert_eq!(seqs_as(s.port, SALES_PLUS_TOKEN, "/ledger/entries?department=finance"), [1]);
    // A department outside the scope is refused, not answered with an empty page.
    let (st, body) = http(s.port, "GET", "/ledger/entries?department=finance", Some(SALES_TOKEN), &[], "");
    assert_eq!(st, 403, "{body}");
    assert!(body.contains("sales-py") && body.contains("finance"), "{body}");
    // verify and head carry no department data: every authenticated caller keeps them.
    assert_eq!(entry_count_as(s.port, SALES_TOKEN), 4);
    assert_eq!(http(s.port, "GET", "/ledger/head", Some(SALES_TOKEN), &[], "").0, 200);
    let err = log.stderr();
    assert!(err.contains("sales-py (Write: sales; reads: sales)") && err.contains("dashboard (Read: ; reads: all)"), "{err}");
}

/// M4: a read caller must say what it reads; read_all and read_departments
/// are exclusive; read_departments is for write callers. N3: read_all is for
/// read-only callers only (a write caller with it was silently accepted).
#[test]
fn aegis_m4_a_read_scope_must_be_explicit() {
    let log = Scratch::new("m4_bad");
    let h = sha256_hex(READER_TOKEN);
    for content in [
        json!({&h: {"caller": "dashboard", "scope": "read"}}),
        json!({&h: {"caller": "d", "scope": "write", "departments": ["sales"], "read_all": true, "read_departments": ["x"]}}),
        json!({&h: {"caller": "d", "scope": "read", "departments": ["sales"], "read_departments": ["x"]}}),
        json!({&h: {"caller": "d", "scope": "write", "departments": ["sales"], "read_departments": ["Bad"]}}),
        json!({&h: {"caller": "d", "scope": "read", "read_all": "yes"}}),
        // AEGIS N3: read_all on a write caller.
        json!({&h: {"caller": "d", "scope": "write", "departments": ["sales"], "read_all": true}}),
    ] {
        std::fs::write(log.callers_path(), content.to_string()).unwrap();
        let path = log.callers_path().to_str().unwrap().to_string();
        assert_eq!(refused(&log, &[("LEDGER_CALLERS_FILE", Some(&path))]).unwrap_or_else(|e| panic!("{content}: {e}")), 1, "{content}");
    }
}

/// L1: the same token hash twice was accepted (the last entry silently won).
#[test]
fn aegis_l1_a_duplicate_token_hash_refuses_to_start() {
    let log = Scratch::new("l1");
    let h = sha256_hex(SALES_TOKEN);
    let a = json!({"caller": "sales-py", "departments": ["sales"], "scope": "write"});
    let b = json!({"caller": "finance-py", "departments": ["finance"], "scope": "write"});
    std::fs::write(log.callers_path(), format!("{{\"{h}\": {a}, \"{h}\": {b}}}")).unwrap();
    let path = log.callers_path().to_str().unwrap().to_string();
    assert_eq!(refused(&log, &[("LEDGER_CALLERS_FILE", Some(&path))]).unwrap_or_else(|e| panic!("{e}")), 1);
    assert!(log.stderr().contains("listed more than once"), "{}", log.stderr());
}

/// L2: LEDGER_CALLERS_FILE="" fell back to the shared token (unscoped).
#[test]
fn aegis_l2_an_empty_callers_file_variable_refuses_to_start() {
    let log = Scratch::new("l2");
    for empty in ["", "  "] {
        assert_eq!(refused(&log, &[("LEDGER_CALLERS_FILE", Some(empty))]).unwrap_or_else(|e| panic!("{e}")), 1);
    }
    assert!(log.stderr().contains("LEDGER_CALLERS_FILE is set but empty"), "{}", log.stderr());
}

/// L3: a caller token's length could not be checked (only its hash is
/// configured); now it is checked when presented: under 32 bytes is a 401.
#[test]
fn aegis_l3_a_short_caller_token_is_refused_when_presented() {
    let log = Scratch::new("l3");
    let short = "short-caller-token";
    let callers = json!({
        sha256_hex(short): {"caller": "weak", "departments": ["sales"], "scope": "write"},
        sha256_hex(SALES_TOKEN): {"caller": "sales-py", "departments": ["sales"], "scope": "write"},
    });
    std::fs::write(log.callers_path(), callers.to_string()).unwrap();
    let path = log.callers_path().to_str().unwrap().to_string();
    let s = start(&log, &[("LEDGER_CALLERS_FILE", Some(&path))]);
    assert_eq!(post_event(s.port, short, &event_body("w-1", "sales", "note")).0, 401);
    assert_eq!(http(s.port, "GET", "/ledger/verify", Some(short), &[], "").0, 401);
    assert_eq!(post_event(s.port, SALES_TOKEN, &event_body("s-1", "sales", "note")).0, 201);
    assert!(log.stderr().contains("\"weak\" presented a configured token of 18 bytes"), "{}", log.stderr());
}

/// L4: `?full=1` re-verified memory only, so a log rewritten on disk under a
/// running ledger still verified "valid" until the next restart.
#[test]
fn aegis_l4_full_verify_rereads_the_log_from_disk() {
    let log = Scratch::new("l4");
    let s = start(&log, &[]);
    fill(s.port, 3);
    assert_eq!(get_json(s.port, "/ledger/verify?full=1"), json!({"valid": true, "entries": 3}));
    let good = std::fs::read_to_string(&log.0).unwrap();
    std::fs::write(&log.0, good.replacen("\"1.00\"", "\"9.00\"", 1)).unwrap();
    assert_eq!(get_json(s.port, "/ledger/verify"), json!({"valid": true, "entries": 3}), "incremental: memory only");
    let (st, body) = get(s.port, "/ledger/verify?full=1");
    assert_eq!(st, 409, "{body}");
    assert!(body.contains("log on disk") && body.contains("line 1"), "{body}");
    assert!(log.stderr().contains("CRITICAL"), "{}", log.stderr());
    std::fs::write(&log.0, &good).unwrap();
    assert_eq!(get_json(s.port, "/ledger/verify?full=1"), json!({"valid": true, "entries": 3}));
}
