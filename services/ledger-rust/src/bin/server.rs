//! Minimal REST wrapper around the Ledger core (lib.rs). Uses tiny_http —
//! a synchronous, dependency-light HTTP server — deliberately, per
//! Decision 4's phased-build discipline: this ledger has no real
//! throughput requirement yet (pre-revenue, fixture-only), so a full
//! async framework (axum/tokio) is not justified tonight. Swapping to
//! one later is a contained change behind this same REST contract.

use std::io::Read;
use std::sync::Mutex;

use ledger_rust::{Ledger, LedgerRecordInput};
use tiny_http::{Header, Method, Response, Server};

fn json_header() -> Header {
    Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..]).unwrap()
}

fn main() {
    let port = std::env::var("LEDGER_PORT").unwrap_or_else(|_| "8090".to_string());
    let addr = format!("0.0.0.0:{port}");
    let server = Server::http(&addr).expect("failed to bind ledger-rust HTTP server");
    let ledger = Mutex::new(Ledger::new());

    eprintln!("ledger-rust listening on :{port}");

    for mut request in server.incoming_requests() {
        let method = request.method().clone();
        let url = request.url().to_string();

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
                            let entry = l.append(record);
                            (201, serde_json::to_string(entry).unwrap())
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
