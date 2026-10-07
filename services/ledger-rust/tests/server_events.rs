//! Integration tests for POST /ledger/events and the Decimal money wire
//! format (docs/adr/0003), against the REAL compiled server binary over a
//! real TCP socket — same harness style as tests/server_auth.rs (raw
//! HTTP/1.1 over std::net::TcpStream, no HTTP client dependency).

mod common;

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::time::Duration;

use common::PortFile;
use serde_json::{json, Value};

const TOKEN: &str = "events-test-token-0123456789abcdef";

struct ServerHandle {
    child: Child,
    port: u16,
    _port_file: PortFile,
}

impl Drop for ServerHandle {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

struct ScratchFile(PathBuf);
impl Drop for ScratchFile {
    fn drop(&mut self) {
        common::remove_ledger_files(&self.0);
    }
}

fn scratch_log(label: &str) -> ScratchFile {
    let p = common::scratch_dir().join(format!("ledger_events_it_{label}_{}.jsonl", common::unique_suffix()));
    let _ = std::fs::remove_file(&p);
    ScratchFile(p)
}

/// Fix wave 21 (N20-M-3): LEDGER_PORT=0 + LEDGER_PORT_FILE, no free_port() race.
fn start_server_at(log_path: &Path) -> ServerHandle {
    let pf = PortFile::new("events");
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_server"));
    cmd.env("LEDGER_SERVICE_TOKEN", TOKEN)
        .env("LEDGER_LOG_PATH", log_path.to_str().unwrap())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    common::ephemeral(&mut cmd, &pf);
    let mut child = cmd.spawn().expect("failed to spawn ledger-rust server");
    let port = match common::wait_port(&mut child, &pf, Duration::from_secs(10)) {
        Ok(p) => p,
        Err(e) => {
            let _ = child.kill();
            let _ = child.wait();
            panic!("{e}");
        }
    };
    let handle = ServerHandle { child, port, _port_file: pf };
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    loop {
        if request(handle.port, "GET", "/health", None, None).is_ok() {
            break;
        }
        if std::time::Instant::now() > deadline {
            panic!("ledger-rust server did not come up within 10s");
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    handle
}

/// Raw HTTP/1.1 request with optional Authorization header value and body; the
/// response is read to its Content-Length (fix wave 21, N20-M-1).
fn request(
    port: u16,
    method: &str,
    path: &str,
    auth: Option<&str>,
    body: Option<&str>,
) -> std::io::Result<(u16, String)> {
    let raw = common::raw_request(method, path, auth, body.unwrap_or(""));
    let resp = common::exchange(port, &raw, Duration::from_secs(5))?;
    Ok((resp.status, resp.text()))
}

fn authed(port: u16, method: &str, path: &str, body: Option<&str>) -> (u16, Value) {
    let (status, text) = request(port, method, path, Some(&format!("Bearer {TOKEN}")), body).unwrap();
    let v = serde_json::from_str(&text).unwrap_or_else(|e| panic!("non-JSON body ({e}): {text:?}"));
    (status, v)
}

fn event(event_id: &str) -> Value {
    json!({
        "event_id": event_id,
        "department": "onboarding",
        "event_type": "compliance_ruling",
        "actor": "intel_15_compliance",
        "subject_id": "client_123",
        "payload_sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "summary": "Activation blocked: 2 requirements unmet"
    })
}

fn finding(finding_id: &str, amount: Value) -> Value {
    json!({
        "finding_id": finding_id, "agent_id": "discount-misuse-v1", "entity_id": "ord_1007",
        "leak_category": "discount_misuse", "amount_usd": amount,
        "value_classification": "observed", "decision_confidence": "very_high"
    })
}

// --- auth -------------------------------------------------------------------

#[test]
fn events_rejects_missing_wrong_and_malformed_token() {
    let log = scratch_log("auth");
    let s = start_server_at(&log.0);
    let body = event("onb-auth").to_string();
    for auth in [None, Some("Bearer wrong"), Some(TOKEN), Some("Basic abc")] {
        let (status, _) = request(s.port, "POST", "/ledger/events", auth, Some(&body)).unwrap();
        assert_eq!(status, 401, "auth header {auth:?}");
    }
    // Nothing was recorded by any rejected request.
    let (_, entries) = authed(s.port, "GET", "/ledger/entries", None);
    assert_eq!(entries.as_array().unwrap().len(), 0);
}

// --- validation --------------------------------------------------------------

#[test]
fn events_validation_failures_return_400_and_record_nothing() {
    let log = scratch_log("validation");
    let s = start_server_at(&log.0);

    let mut cases: Vec<(String, String)> = Vec::new();
    let mut push = |name: &str, v: Value| cases.push((name.to_string(), v.to_string()));

    let mut v = event("x");
    v["extra"] = json!("nope");
    push("unknown field", v);
    let mut v = event("x");
    v.as_object_mut().unwrap().remove("summary");
    push("missing summary", v);
    let mut v = event("x");
    v["actor"] = Value::Null;
    push("null actor", v);
    let mut v = event("x");
    v["department"] = json!(5);
    push("numeric department", v);
    push("bad event_id chars", event("onb 1/2"));
    push("event_id too long", event(&"a".repeat(129)));
    push("empty event_id", event(""));
    let mut v = event("x");
    v["department"] = json!("Onboarding");
    push("uppercase department", v);
    let mut v = event("x");
    v["event_type"] = json!("a".repeat(65));
    push("event_type too long", v);
    let mut v = event("x");
    v["subject_id"] = json!("client|123");
    push("pipe in subject_id", v);
    let mut v = event("x");
    v["payload_sha256"] = json!("ABC");
    push("bad sha", v);
    let mut v = event("x");
    v["payload_sha256"] = json!("9F86D081884C7D659A2FEAA0C55AD015A3BF4F1B2B0B822CD15D6C15B0F00A08");
    push("uppercase sha", v);
    let mut v = event("x");
    v["summary"] = json!("");
    push("empty summary", v);
    let mut v = event("x");
    v["summary"] = json!("a".repeat(281));
    push("summary too long", v);
    let mut v = event("x");
    v["summary"] = json!("line one\nline two");
    push("control char in summary", v);
    cases.push(("not json".into(), "{ this is not json".into()));
    cases.push(("json array".into(), "[]".into()));

    for (name, body) in &cases {
        let (status, v) = authed(s.port, "POST", "/ledger/events", Some(body));
        assert_eq!(status, 400, "{name}: {v}");
        assert!(v["error"].as_str().unwrap().contains("invalid event"), "{name}: {v}");
    }

    // Boundary-valid values are accepted.
    let mut ok = event(&"a".repeat(128));
    ok["summary"] = json!("é".repeat(280));
    let (status, _) = authed(s.port, "POST", "/ledger/events", Some(&ok.to_string()));
    assert_eq!(status, 201);

    let (_, entries) = authed(s.port, "GET", "/ledger/entries", None);
    assert_eq!(entries.as_array().unwrap().len(), 1, "only the valid event is recorded");
}

#[test]
fn oversized_body_is_rejected_with_413() {
    let log = scratch_log("oversize");
    let s = start_server_at(&log.0);
    let mut v = event("big");
    v["summary"] = json!("a".repeat(70 * 1024));
    let (status, _) = authed(s.port, "POST", "/ledger/events", Some(&v.to_string()));
    assert_eq!(status, 413);
}

// --- create / idempotency / conflict ------------------------------------------

#[test]
fn event_create_retry_and_conflict() {
    let log = scratch_log("idempotency");
    let s = start_server_at(&log.0);
    let body = event("onb-01J8ZQ").to_string();

    let (status, created) = authed(s.port, "POST", "/ledger/events", Some(&body));
    assert_eq!(status, 201);
    assert_eq!(created["kind"], "event");
    assert_eq!(created["seq"], 0);
    assert_eq!(created["event_id"], "onb-01J8ZQ");
    assert_eq!(created["department"], "onboarding");
    assert_eq!(created["event_type"], "compliance_ruling");
    assert_eq!(created["actor"], "intel_15_compliance");
    assert_eq!(created["subject_id"], "client_123");
    assert_eq!(created["summary"], "Activation blocked: 2 requirements unmet");
    assert_eq!(created["hash"].as_str().unwrap().len(), 64);
    assert!(created["recorded_at"].as_str().unwrap().parse::<chrono::DateTime<chrono::Utc>>().is_ok());
    assert_eq!(created.as_object().unwrap().len(), 12, "exact contract field set: {created}");

    // Identical retry: 200 with the SAME stored entry, nothing new written.
    let (status, retry) = authed(s.port, "POST", "/ledger/events", Some(&body));
    assert_eq!(status, 200);
    assert_eq!(retry, created);

    // Same id, different content: 409, nothing written.
    let mut changed = event("onb-01J8ZQ");
    changed["summary"] = json!("Activation approved");
    let (status, conflict) = authed(s.port, "POST", "/ledger/events", Some(&changed.to_string()));
    assert_eq!(status, 409);
    assert_eq!(conflict["event_id"], "onb-01J8ZQ");

    let mut changed_hash = event("onb-01J8ZQ");
    changed_hash["payload_sha256"] = json!("0".repeat(64));
    let (status, _) = authed(s.port, "POST", "/ledger/events", Some(&changed_hash.to_string()));
    assert_eq!(status, 409);

    let (_, entries) = authed(s.port, "GET", "/ledger/entries", None);
    assert_eq!(entries.as_array().unwrap().len(), 1);
    assert_eq!(std::fs::read_to_string(&log.0).unwrap().lines().count(), 1);
}

// --- findings wire format ---------------------------------------------------------

#[test]
fn append_requires_string_money_and_returns_kind_finding() {
    let log = scratch_log("money");
    let s = start_server_at(&log.0);

    for bad in [json!(120.0), json!(49.99), json!("12.3"), json!("0.00"), json!("-1.00"), json!("1e3"), json!(true)] {
        let (status, v) = authed(s.port, "POST", "/ledger/append", Some(&finding("f-bad", bad.clone()).to_string()));
        assert_eq!(status, 400, "amount {bad} should be rejected: {v}");
    }
    let (status, v) = authed(s.port, "POST", "/ledger/append", Some(&finding("f-num", json!(120.0)).to_string()));
    assert_eq!(status, 400);
    assert!(v["error"].as_str().unwrap().contains("must be a JSON string"), "{v}");

    let (status, f) = authed(s.port, "POST", "/ledger/append", Some(&finding("f-ok", json!("54.38")).to_string()));
    assert_eq!(status, 201);
    assert_eq!(f["kind"], "finding");
    assert_eq!(f["amount_usd"], "54.38");

    let (status, f) = authed(s.port, "POST", "/ledger/append", Some(&finding("f-null", Value::Null).to_string()));
    assert_eq!(status, 201);
    assert_eq!(f["amount_usd"], Value::Null);

    let (_, entries) = authed(s.port, "GET", "/ledger/entries", None);
    assert_eq!(entries.as_array().unwrap().len(), 2);
}

// --- mixed chain, verify, restart persistence ---------------------------------------

#[test]
fn mixed_findings_and_events_verify_and_survive_restart() {
    let log = scratch_log("restart");
    let first_event;
    {
        let s = start_server_at(&log.0);
        let (st, _) = authed(s.port, "POST", "/ledger/append", Some(&finding("f-0", json!("150.00")).to_string()));
        assert_eq!(st, 201);
        let (st, e) = authed(s.port, "POST", "/ledger/events", Some(&event("onb-1").to_string()));
        assert_eq!(st, 201);
        first_event = e;
        let (st, _) = authed(s.port, "POST", "/ledger/append", Some(&finding("f-2", json!("54.38")).to_string()));
        assert_eq!(st, 201);
        let (st, _) = authed(s.port, "POST", "/ledger/events", Some(&event("cre-7").to_string()));
        assert_eq!(st, 201);

        let (st, v) = authed(s.port, "GET", "/ledger/verify", None);
        assert_eq!(st, 200);
        assert_eq!(v, json!({"valid": true, "entries": 4}));

        let (_, entries) = authed(s.port, "GET", "/ledger/entries", None);
        let kinds: Vec<&str> = entries.as_array().unwrap().iter().map(|e| e["kind"].as_str().unwrap()).collect();
        assert_eq!(kinds, ["finding", "event", "finding", "event"]);
        let arr = entries.as_array().unwrap();
        for i in 1..arr.len() {
            assert_eq!(arr[i]["prev_hash"], arr[i - 1]["hash"], "one shared chain");
        }
    } // server killed here

    let s = start_server_at(&log.0);
    let (st, v) = authed(s.port, "GET", "/ledger/verify", None);
    assert_eq!(st, 200);
    assert_eq!(v, json!({"valid": true, "entries": 4}));

    // Idempotency index was rebuilt from disk.
    let (st, retry) = authed(s.port, "POST", "/ledger/events", Some(&event("onb-1").to_string()));
    assert_eq!(st, 200);
    assert_eq!(retry, first_event);
    let mut changed = event("onb-1");
    changed["actor"] = json!("someone_else");
    let (st, _) = authed(s.port, "POST", "/ledger/events", Some(&changed.to_string()));
    assert_eq!(st, 409);

    // And the chain keeps growing after restart.
    let (st, e) = authed(s.port, "POST", "/ledger/events", Some(&event("onb-after-restart").to_string()));
    assert_eq!(st, 201);
    assert_eq!(e["seq"], 4);
    let (_, v) = authed(s.port, "GET", "/ledger/verify", None);
    assert_eq!(v, json!({"valid": true, "entries": 5}));
}

#[test]
fn tampered_event_on_disk_refuses_to_start() {
    let log = scratch_log("tamper_event");
    {
        let s = start_server_at(&log.0);
        authed(s.port, "POST", "/ledger/append", Some(&finding("f-0", json!("1.00")).to_string()));
        authed(s.port, "POST", "/ledger/events", Some(&event("onb-1").to_string()));
    }
    let text = std::fs::read_to_string(&log.0).unwrap();
    let tampered = text.replace("Activation blocked", "Activation approved");
    assert_ne!(text, tampered);
    std::fs::write(&log.0, tampered).unwrap();

    let status = Command::new(env!("CARGO_BIN_EXE_server"))
        .env("LEDGER_SERVICE_TOKEN", TOKEN)
        .env("LEDGER_PORT", "0")
        .env("LEDGER_LOG_PATH", log.0.to_str().unwrap())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .unwrap();
    assert!(!status.success(), "server must refuse to start on a tampered event");
}

// --- legacy log served by the new binary --------------------------------------------

#[test]
fn legacy_log_from_old_binary_is_served_and_extended() {
    let log = scratch_log("legacy");
    std::fs::copy(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/legacy_ledger_v1.jsonl"), &log.0).unwrap();
    let s = start_server_at(&log.0);

    let (st, v) = authed(s.port, "GET", "/ledger/verify", None);
    assert_eq!(st, 200);
    assert_eq!(v, json!({"valid": true, "entries": 11}));

    let (_, entries) = authed(s.port, "GET", "/ledger/entries", None);
    let arr = entries.as_array().unwrap();
    assert_eq!(arr[0]["kind"], "finding");
    assert_eq!(arr[0]["amount_usd"], "120.00");
    assert_eq!(arr[5]["amount_usd"], "0.10");
    assert_eq!(arr[8]["amount_usd"], "0.30");
    assert_eq!(arr[10]["amount_usd"], Value::Null);

    let (st, _) = authed(s.port, "POST", "/ledger/append", Some(&finding("f-new", json!("12.30")).to_string()));
    assert_eq!(st, 201);
    let (st, _) = authed(s.port, "POST", "/ledger/events", Some(&event("onb-after-legacy").to_string()));
    assert_eq!(st, 201);
    let (_, v) = authed(s.port, "GET", "/ledger/verify", None);
    assert_eq!(v, json!({"valid": true, "entries": 13}));
}

// --- fix wave 3 -------------------------------------------------------------

/// An empty ledger is a valid chain: 200 {"valid":true,"entries":0}. It used
/// to answer 409 {"valid":false,"error":"Empty"}, and every caller had to
/// special-case "invalid, but actually fine".
#[test]
fn empty_ledger_verifies_as_valid_with_200() {
    let log = scratch_log("empty_verify");
    let s = start_server_at(&log.0);
    let (st, v) = authed(s.port, "GET", "/ledger/verify", None);
    assert_eq!(st, 200, "{v}");
    assert_eq!(v, json!({"valid": true, "entries": 0}));
}

/// N8 sweep: the finding append body drops nothing silently either. An
/// extra field (e.g. "approved_by") is a 400, never accepted-and-ignored,
/// matching POST /ledger/events (contract section 2).
#[test]
fn append_with_unknown_field_is_400_and_records_nothing() {
    let log = scratch_log("append_unknown");
    let s = start_server_at(&log.0);
    let mut body = finding("f-0", json!("10.00"));
    body["approved_by"] = json!("andre");
    let (st, v) = authed(s.port, "POST", "/ledger/append", Some(&body.to_string()));
    assert_eq!(st, 400, "{v}");
    assert!(v["error"].as_str().unwrap().contains("unknown field"), "{v}");
    let (_, entries) = authed(s.port, "GET", "/ledger/entries", None);
    assert_eq!(entries, json!([]));
}

/// N8 end to end: a log with an injected unknown field is refused at
/// startup (fail closed), exactly like any other corrupt line.
#[test]
fn injected_unknown_field_on_disk_refuses_to_start() {
    let log = scratch_log("inject_unknown");
    std::fs::copy(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/aegis_unknown_field_injection.jsonl"), &log.0).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_server"))
        .env("LEDGER_SERVICE_TOKEN", TOKEN)
        .env("LEDGER_PORT", "0")
        .env("LEDGER_LOG_PATH", log.0.to_str().unwrap())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("spawn server");
    // Bounded wait: on the old code the server starts and serves forever.
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    let status = loop {
        if let Some(st) = child.try_wait().unwrap() {
            break Some(st);
        }
        if std::time::Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            break None;
        }
        std::thread::sleep(Duration::from_millis(50));
    };
    let status = status.expect("server started on a log with an injected unknown field (must refuse)");
    assert!(!status.success(), "server must refuse to start");
    let mut stderr = String::new();
    child.stderr.take().unwrap().read_to_string(&mut stderr).unwrap();
    assert!(stderr.contains("unknown field") && stderr.contains("approved_by"), "{stderr}");
}
