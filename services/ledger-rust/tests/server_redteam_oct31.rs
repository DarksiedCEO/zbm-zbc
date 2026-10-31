//! Red team Oct 30-31 2026 (scratchpad/audit/redteam-access.md, redteam-chaos.md, redteam-money.md), fix wave A,
//! against the REAL compiled server binary over real TCP. Each test fails against the base binary
//! (feat/compliance-gate-deploy 5c2538b) and passes after the fix:
//!
//! - RTA-1: one write caller pre-claimed another department's deterministic event id (lateral_probe.out C:
//!   seo-py 201, then finance-py 409 "already recorded with different content").
//! - CH-6: GET /health stayed 200 while the ledger could not write (c04 L1, disk full).
//! - RTM-07: a consistent rewrite or a rollback with a rewritten `.head` started as "chain verified"
//!   (ledger_tamper.py); nothing outside the system anchored the head.
//!
//! Tokens are plain labelled test strings padded to the 32-byte minimum.

mod common;

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use common::PortFile;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const SEO_TOKEN: &str = "oct31-seo-writer-test-token-0123456789";
const FINANCE_TOKEN: &str = "oct31-finance-writer-test-token-012345";
const SIGNING_KEY: &str = "oct31-head-signing-test-key-0123456789ab";

struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        common::remove_ledger_files(&self.0);
        let _ = std::fs::remove_file(self.stderr_path());
        let _ = std::fs::remove_file(self.callers_path());
    }
}

impl Scratch {
    fn new(label: &str) -> Scratch {
        Scratch(common::scratch_dir().join(format!("ledger_oct31_{label}_{}.jsonl", common::unique_suffix())))
    }
    fn stderr_path(&self) -> PathBuf {
        PathBuf::from(format!("{}.stderr", self.0.display()))
    }
    fn callers_path(&self) -> PathBuf {
        PathBuf::from(format!("{}.callers.json", self.0.display()))
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

fn sha256_hex(s: &str) -> String {
    Sha256::digest(s.as_bytes()).iter().map(|b| format!("{b:02x}")).collect()
}

fn spawn(log: &Scratch, env: &[(&str, &str)]) -> (Child, PortFile) {
    let pf = PortFile::new("oct31");
    let stderr = std::fs::OpenOptions::new().create(true).append(true).open(log.stderr_path()).unwrap();
    let callers = json!({
        sha256_hex(SEO_TOKEN): {"caller": "seo-py", "departments": ["seo"], "scope": "write"},
        sha256_hex(FINANCE_TOKEN): {"caller": "finance-py", "departments": ["finance"], "scope": "write"},
    });
    std::fs::write(log.callers_path(), callers.to_string()).unwrap();
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_server"));
    cmd.env("LEDGER_LOG_PATH", log.0.to_str().unwrap())
        .env("LEDGER_CALLERS_FILE", log.callers_path())
        .env_remove("LEDGER_SERVICE_TOKEN")
        .env_remove("LEDGER_ALLOW_SHARED_TOKEN")
        .env_remove("LEDGER_ANCHORED_HEAD")
        .env_remove("LEDGER_HEAD_SIGNING_KEY")
        .env_remove("LEDGER_MIN_FREE_BYTES")
        .stdout(Stdio::null())
        .stderr(Stdio::from(stderr));
    common::ephemeral(&mut cmd, &pf);
    for (k, v) in env {
        cmd.env(k, v);
    }
    (cmd.spawn().expect("spawn ledger-rust"), pf)
}

fn start(log: &Scratch, env: &[(&str, &str)]) -> Server {
    let (mut child, pf) = spawn(log, env);
    match common::wait_port(&mut child, &pf, Duration::from_secs(15)) {
        Ok(port) => Server { child, port, _pf: pf },
        Err(e) => {
            let _ = child.kill();
            let _ = child.wait();
            panic!("{e}\n--- server stderr ---\n{}", log.stderr())
        }
    }
}

/// Ok(exit code) when the server refused to start; Err when it came up.
fn refused(log: &Scratch, env: &[(&str, &str)]) -> Result<i32, String> {
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

fn http(port: u16, method: &str, path: &str, token: Option<&str>, body: &str) -> (u16, String) {
    let auth = token.map(|t| format!("Authorization: Bearer {t}\r\n")).unwrap_or_default();
    let raw = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth}Content-Type: application/json\r\nContent-Length: {}\r\n\
         Connection: close\r\n\r\n{body}",
        body.len()
    );
    let r = common::exchange(port, raw.as_bytes(), Duration::from_secs(30)).expect("exchange");
    (r.status, r.text())
}

fn event(event_id: &str, department: &str, summary: &str) -> String {
    json!({
        "event_id": event_id, "department": department, "event_type": "invoice_issued", "actor": "oct31_test",
        "subject_id": "inv_1", "payload_sha256": "cd".repeat(32), "summary": summary
    })
    .to_string()
}

/// RTA-1, the red team's probe: `seo-py` posts `fin-inva-…` under its own department (201), then Finance's own
/// write of that id was a 409 forever (its ids are deterministic). Now ids are per department.
#[test]
fn rta1_a_caller_cannot_preclaim_another_departments_event_id() {
    let log = Scratch::new("rta1");
    let s = start(&log, &[]);
    let id = "fin-inva-b3d3ee3b0123456789abcdef0123456789abcdef";
    assert_eq!(http(s.port, "POST", "/ledger/events", Some(SEO_TOKEN), &event(id, "seo", "squat")).0, 201);
    let (st, body) = http(s.port, "POST", "/ledger/events", Some(FINANCE_TOKEN), &event(id, "finance", "approved"));
    assert_eq!(st, 201, "Finance's own id must not be blocked by another department's write: {body}");
    // Within a department the rules are unchanged: identical retry 200, other content 409.
    assert_eq!(http(s.port, "POST", "/ledger/events", Some(FINANCE_TOKEN), &event(id, "finance", "approved")).0, 200);
    assert_eq!(http(s.port, "POST", "/ledger/events", Some(FINANCE_TOKEN), &event(id, "finance", "other")).0, 409);
    // The seo caller still cannot write for finance at all (sweep F-4), and Finance reads only its own event.
    assert_eq!(http(s.port, "POST", "/ledger/events", Some(SEO_TOKEN), &event("fin-x-1", "finance", "x")).0, 403);
    let (st, text) = http(s.port, "GET", "/ledger/entries", Some(FINANCE_TOKEN), "");
    assert_eq!(st, 200);
    let mine: Vec<Value> = serde_json::from_str(&text).unwrap();
    assert_eq!(mine.len(), 1);
    assert_eq!(mine[0]["department"], "finance");
    drop(s);
    // Both entries load again (the per-department index is rebuilt on open; no migration step).
    let s = start(&log, &[]);
    assert_eq!(http(s.port, "POST", "/ledger/events", Some(FINANCE_TOKEN), &event(id, "finance", "approved")).0, 200);
    assert_eq!(http(s.port, "POST", "/ledger/events", Some(SEO_TOKEN), &event(id, "seo", "squat")).0, 200);
}

/// CH-6: /health says when the ledger cannot write. Below the free-space floor it is 503 `disk_low`; with the
/// default floor on a normal disk it is the unchanged 200 body.
#[test]
fn ch6_health_reports_degraded_when_the_disk_is_below_the_floor() {
    let log = Scratch::new("ch6");
    let s = start(&log, &[]);
    let (st, body) = http(s.port, "GET", "/health", None, "");
    assert_eq!((st, body.as_str()), (200, r#"{"service":"ledger-rust","status":"ok"}"#));
    drop(s);
    let s = start(&log, &[("LEDGER_MIN_FREE_BYTES", "18446744073709551615")]);
    let (st, body) = http(s.port, "GET", "/health", None, "");
    assert_eq!(st, 503, "{body}");
    // AEGIS wave A F-15a: Railway's deploy healthcheck (/livez) still passes, so a degraded ledger can be redeployed
    assert_eq!(http(s.port, "GET", "/livez", None, "").0, 200);
    let v: Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["status"], "degraded");
    assert_eq!(v["reasons"][0].as_str().unwrap(), "disk_low", "{body}");
    assert!(!body.contains(log.0.to_str().unwrap()), "no path in the open /health body");
    drop(s);
    assert_eq!(refused(&log, &[("LEDGER_MIN_FREE_BYTES", "lots")]), Ok(1));
}

/// RTM-07: GET /ledger/anchor gives the value to store off-system (signed when LEDGER_HEAD_SIGNING_KEY is set),
/// and a start with LEDGER_ANCHORED_HEAD refuses a ledger that is behind it or differs from it.
#[test]
fn rtm07_external_anchor_is_served_signed_and_enforced_at_start() {
    let log = Scratch::new("rtm07");
    let s = start(&log, &[("LEDGER_HEAD_SIGNING_KEY", SIGNING_KEY)]);
    for i in 0..3 {
        let id = format!("fin-x-{i}");
        assert_eq!(http(s.port, "POST", "/ledger/events", Some(FINANCE_TOKEN), &event(&id, "finance", "e")).0, 201);
    }
    assert_eq!(http(s.port, "GET", "/ledger/anchor", None, "").0, 401, "the anchor route needs a token");
    let (st, text) = http(s.port, "GET", "/ledger/anchor", Some(FINANCE_TOKEN), "");
    assert_eq!(st, 200, "{text}");
    let a: Value = serde_json::from_str(&text).unwrap();
    let anchor = a["anchor"].as_str().unwrap().to_string();
    let head_hash = a["head_hash"].as_str().unwrap();
    assert_eq!(anchor, format!("3:{head_hash}"));
    let msg = format!("zbm-ledger-anchor-v1|3|{head_hash}|{}", a["at"].as_str().unwrap());
    assert_eq!(a["signature"].as_str().unwrap(), hex::encode(ledger_rust::hmac_sha256(SIGNING_KEY.as_bytes(), msg.as_bytes())));
    assert_eq!(a["signature_alg"], "HMAC-SHA256");
    drop(s);

    // The same ledger starts against its own anchor (and against an older one it extends).
    let s = start(&log, &[("LEDGER_ANCHORED_HEAD", &anchor)]);
    assert!(log.stderr().contains("external anchor"), "{}", log.stderr());
    assert_eq!(http(s.port, "POST", "/ledger/events", Some(FINANCE_TOKEN), &event("fin-x-3", "finance", "e")).0, 201);
    drop(s);
    let s = start(&log, &[("LEDGER_ANCHORED_HEAD", &anchor)]);
    drop(s);

    // Behind the anchor (a rollback with a matching .head) or another hash at its position (a rewrite): refused.
    let ahead = format!("9:{head_hash}");
    assert_eq!(refused(&log, &[("LEDGER_ANCHORED_HEAD", &ahead)]), Ok(1));
    assert!(log.stderr().contains("rolled back"), "{}", log.stderr());
    let other = format!("3:{}", "0".repeat(64));
    assert_eq!(refused(&log, &[("LEDGER_ANCHORED_HEAD", &other)]), Ok(1));
    assert!(log.stderr().contains("rewritten"), "{}", log.stderr());
    assert_eq!(refused(&log, &[("LEDGER_ANCHORED_HEAD", "garbage")]), Ok(1));
    assert_eq!(refused(&log, &[("LEDGER_HEAD_SIGNING_KEY", "short")]), Ok(1));
}


/// AEGIS wave A F-6: a log whose last entry was stamped 100 years ahead (a bad clock before a restart) used to pin
/// every later `recorded_at` there. After the restart the wall clock is used as observed, /health warns
/// `clock_skew`, and the chain still verifies; a second restart keeps it sane.
#[test]
fn f6_an_entry_100_years_ahead_does_not_pin_later_timestamps_after_restart() {
    use ledger_rust::{EventInput, Ledger, LedgerEntry};
    let log = Scratch::new("f6");
    let mut l = Ledger::new();
    let mut e = l.build_event_entry(EventInput {
        event_id: "fin-future-0".into(), department: "finance".into(), event_type: "invoice_issued".into(),
        actor: "oct31_test".into(), subject_id: "inv_1".into(), payload_sha256: "cd".repeat(32), summary: "future".into(),
    });
    if let LedgerEntry::Event(ev) = &mut e {
        ev.recorded_at = chrono::Utc::now() + chrono::Duration::days(36525);
        ev.hash = sha256_hex(&ledger_rust::event_canonical(ev));
    }
    l.push_entry(e);
    assert_eq!(l.verify_chain(), Ok(()));
    std::fs::write(&log.0, format!("{}\n", serde_json::to_string(&l.entries()[0]).unwrap())).unwrap();
    common::write_head_for(&log.0);
    for round in 0..2 {
        let s = start(&log, &[]);
        let id = format!("fin-now-{round}");
        let (st, body) = http(s.port, "POST", "/ledger/events", Some(FINANCE_TOKEN), &event(&id, "finance", "now"));
        assert_eq!(st, 201, "{body}");
        let at: Value = serde_json::from_str(&body).unwrap();
        let year: i32 = at["recorded_at"].as_str().unwrap()[..4].parse().unwrap();
        assert!(year < 2100, "pinned to the bad clock: {}", at["recorded_at"]);
        let (st, h) = http(s.port, "GET", "/health", None, "");
        assert_eq!(st, 200, "{h}");
        if round == 0 {
            assert!(h.contains("clock_skew"), "{h}");
        }
        let (st, v) = http(s.port, "GET", "/ledger/verify", Some(FINANCE_TOKEN), "");
        assert_eq!(st, 200, "{v}");
    }
}

/// AEGIS wave A F-5: GET /ledger/anchor?at=N gives the anchor of an earlier head, so the off-ledger store can check
/// that today's head extends the one it stored.
#[test]
fn f5_anchor_at_an_earlier_head() {
    let log = Scratch::new("f5");
    let s = start(&log, &[]);
    for i in 0..3 {
        let id = format!("fin-a-{i}");
        assert_eq!(http(s.port, "POST", "/ledger/events", Some(FINANCE_TOKEN), &event(&id, "finance", "e")).0, 201);
    }
    let (_, now) = http(s.port, "GET", "/ledger/anchor", Some(FINANCE_TOKEN), "");
    let now: Value = serde_json::from_str(&now).unwrap();
    let (st, at3) = http(s.port, "GET", "/ledger/anchor?at=3", Some(FINANCE_TOKEN), "");
    assert_eq!(st, 200);
    let at3: Value = serde_json::from_str(&at3).unwrap();
    assert_eq!(at3["anchor"], now["anchor"]);
    let (st, at1) = http(s.port, "GET", "/ledger/anchor?at=1", Some(FINANCE_TOKEN), "");
    assert_eq!(st, 200);
    assert!(at1.contains("\"entries\":1"), "{at1}");
    assert_eq!(http(s.port, "GET", "/ledger/anchor?at=9", Some(FINANCE_TOKEN), "").0, 404);
    assert_eq!(http(s.port, "GET", "/ledger/anchor?x=1", Some(FINANCE_TOKEN), "").0, 400);
    assert_eq!(http(s.port, "GET", "/ledger/anchor?at=1", None, "").0, 401);
}
