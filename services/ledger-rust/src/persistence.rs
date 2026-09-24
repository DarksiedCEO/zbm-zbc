//! File-backed persistence for the Ledger: an append-only JSONL log that
//! survives process restarts.
//!
//! Design rationale (Decision: fail closed for security, per founder
//! engineering requirements): a tamper-evident ledger that silently starts
//! up empty — or worse, starts up "successfully" on a corrupted/tampered
//! log file — defeats the entire point of hash-chaining. So:
//!
//! - On open, every persisted entry is replayed and the FULL chain is
//!   verified before the ledger is usable. Any integrity failure (a
//!   truncated line, a hash mismatch, a broken prev_hash pointer) is
//!   returned as an error, never swallowed.
//! - On append, the entry is written to disk and fsync'd BEFORE it is
//!   added to the in-memory ledger. If the disk write fails, the
//!   in-memory state is left untouched and the error propagates to the
//!   caller — memory and disk can never diverge in a way that would let
//!   a caller believe something was recorded when it wasn't.
//!
//! This is intentionally a flat JSONL file with no compaction, indexing,
//! or concurrent-writer support — single-process, single-writer, matching
//! how `bin/server.rs` actually runs it (one `Mutex<PersistentLedger>`).
//! A real multi-instance deployment would need a different storage layer;
//! that is out of scope for this pass and is not pretended otherwise.
//!
//! Events (Sep 24 2026): `kind: "event"` entries share the same log file and
//! the same hash chain as findings. An in-memory `event_id -> position` index
//! is rebuilt on every open; a log containing the same event_id twice is
//! treated as corrupt (fail closed), since the append path never writes one.

use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

use crate::{EventInput, Ledger, LedgerEntry, LedgerError, LedgerRecordInput};

#[derive(Debug)]
pub enum PersistError {
    Io(io::Error),
    /// The log file exists but a line could not be parsed as a
    /// `LedgerEntry` — truncated write, disk corruption, or manual
    /// tampering that broke the JSON itself (as opposed to tampering that
    /// leaves valid JSON but breaks the hash chain, which surfaces as
    /// `ChainInvalid` instead).
    Corrupt { line: usize, reason: String },
    /// The log file parsed cleanly but the hash chain does not verify —
    /// the ledger's core tamper-evidence guarantee has been violated.
    ChainInvalid(LedgerError),
}

impl std::fmt::Display for PersistError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PersistError::Io(e) => write!(f, "ledger log I/O error: {e}"),
            PersistError::Corrupt { line, reason } => {
                write!(f, "ledger log corrupt at line {line}: {reason}")
            }
            PersistError::ChainInvalid(e) => write!(f, "ledger chain integrity failure: {e:?}"),
        }
    }
}

impl std::error::Error for PersistError {}

impl From<io::Error> for PersistError {
    fn from(e: io::Error) -> Self {
        PersistError::Io(e)
    }
}

/// Result of `PersistentLedger::append_event` (contract section 2).
#[derive(Debug)]
pub enum EventAppendOutcome<'a> {
    /// New event, persisted and appended (HTTP 201).
    Created(&'a LedgerEntry),
    /// Same event_id already recorded with identical content (HTTP 200).
    /// Nothing was written.
    Existing(&'a LedgerEntry),
    /// Same event_id already recorded with DIFFERENT content (HTTP 409).
    /// Nothing was written; the existing entry is returned for context.
    Conflict(&'a LedgerEntry),
}

#[derive(Debug)]
pub struct PersistentLedger {
    ledger: Ledger,
    event_index: HashMap<String, usize>,
    file: File,
    path: PathBuf,
}

impl PersistentLedger {
    /// Opens (or creates) the ledger log at `path`. If the file already
    /// has entries, every one is replayed into memory and the full hash
    /// chain is verified before this returns `Ok` — a corrupt or tampered
    /// log file is a hard error, not a silent reset to empty.
    ///
    /// Legacy log lines (written before Sep 24 2026: numeric `amount_usd`,
    /// no `kind`) load as findings with the amount converted exactly as the
    /// old hash formatted it, so their hashes still verify.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self, PersistError> {
        let path = path.as_ref().to_path_buf();

        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }

        let mut entries: Vec<LedgerEntry> = Vec::new();
        let mut event_index: HashMap<String, usize> = HashMap::new();
        if path.exists() {
            let f = File::open(&path)?;
            let reader = BufReader::new(f);
            for (i, line) in reader.lines().enumerate() {
                let line = line?;
                if line.trim().is_empty() {
                    continue;
                }
                let entry: LedgerEntry = serde_json::from_str(&line).map_err(|e| {
                    PersistError::Corrupt {
                        line: i + 1,
                        reason: e.to_string(),
                    }
                })?;
                if let LedgerEntry::Event(ev) = &entry {
                    if event_index.insert(ev.event_id.clone(), entries.len()).is_some() {
                        return Err(PersistError::Corrupt {
                            line: i + 1,
                            reason: format!("duplicate event_id {:?} in ledger log", ev.event_id),
                        });
                    }
                }
                entries.push(entry);
            }
        }

        let ledger = Ledger::from_entries(entries);
        if !ledger.is_empty() {
            ledger.verify_chain().map_err(PersistError::ChainInvalid)?;
        }

        let file = OpenOptions::new().create(true).append(true).open(&path)?;

        Ok(PersistentLedger { ledger, event_index, file, path })
    }

    pub fn len(&self) -> usize {
        self.ledger.len()
    }

    pub fn is_empty(&self) -> bool {
        self.ledger.is_empty()
    }

    pub fn entries(&self) -> &[LedgerEntry] {
        self.ledger.entries()
    }

    pub fn verify_chain(&self) -> Result<(), LedgerError> {
        self.ledger.verify_chain()
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Writes one already-built entry to disk and fsyncs, and only THEN
    /// commits it to the in-memory ledger. On any disk error the in-memory
    /// ledger is left exactly as it was.
    fn persist_then_push(&mut self, entry: LedgerEntry) -> Result<&LedgerEntry, PersistError> {
        let mut line = serde_json::to_string(&entry).map_err(|e| PersistError::Corrupt {
            line: 0,
            reason: format!("failed to serialize new entry: {e}"),
        })?;
        line.push('\n');

        self.file.write_all(line.as_bytes())?;
        self.file.flush()?;
        self.file.sync_data()?;

        self.ledger.push_entry(entry);
        Ok(self.ledger.entries().last().expect("just pushed"))
    }

    /// Appends a new finding record: builds the entry (pure, no mutation),
    /// writes it to disk and fsyncs, and only THEN commits it to the
    /// in-memory ledger. If the disk write fails at any point, the
    /// in-memory ledger is left exactly as it was and the error is returned
    /// — the caller must treat this as "not recorded," full stop, never as
    /// a partial success.
    pub fn append(&mut self, record: LedgerRecordInput) -> Result<&LedgerEntry, PersistError> {
        let entry = self.ledger.build_entry(record);
        self.persist_then_push(entry)
    }

    /// Idempotent event append on the shared chain. `input` must already
    /// have passed `EventInput::validate`. Same durability-before-
    /// visibility rule as `append`.
    pub fn append_event(&mut self, input: EventInput) -> Result<EventAppendOutcome<'_>, PersistError> {
        if let Some(&pos) = self.event_index.get(&input.event_id) {
            let existing = &self.ledger.entries()[pos];
            let same = existing
                .as_event()
                .map(|e| e.same_content_as(&input))
                .unwrap_or(false);
            return Ok(if same {
                EventAppendOutcome::Existing(existing)
            } else {
                EventAppendOutcome::Conflict(existing)
            });
        }

        let event_id = input.event_id.clone();
        let pos = self.ledger.len();
        let entry = self.ledger.build_event_entry(input);
        self.persist_then_push(entry)?;
        self.event_index.insert(event_id, pos);
        Ok(EventAppendOutcome::Created(&self.ledger.entries()[pos]))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn scratch_path(label: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!(
            "zbm_ledger_test_{label}_{}_{nanos}.jsonl",
            std::process::id()
        ))
    }

    fn sample_record(finding_id: &str, amount: &str) -> LedgerRecordInput {
        LedgerRecordInput {
            finding_id: finding_id.to_string(),
            agent_id: "affiliate-coupon-extension-v1".to_string(),
            entity_id: finding_id.to_string(),
            leak_category: "affiliate_coupon_extension".to_string(),
            amount_usd: Some(crate::Money::parse(amount).unwrap()),
            value_classification: Some("attributed".to_string()),
            decision_confidence: Some("high".to_string()),
        }
    }

    #[test]
    fn appends_persist_across_reopen() {
        let path = scratch_path("reopen");
        let _cleanup = ScratchFile(path.clone());

        {
            let mut pl = PersistentLedger::open(&path).expect("open 1");
            pl.append(sample_record("f-0", "100.00")).expect("append 0");
            pl.append(sample_record("f-1", "200.00")).expect("append 1");
            assert_eq!(pl.len(), 2);
        } // drop — simulates process restart

        let pl2 = PersistentLedger::open(&path).expect("open 2 (replay)");
        assert_eq!(pl2.len(), 2);
        assert_eq!(pl2.verify_chain(), Ok(()));
        assert_eq!(pl2.entries()[0].as_finding().unwrap().finding_id, "f-0");
        assert_eq!(pl2.entries()[1].as_finding().unwrap().finding_id, "f-1");
        assert_eq!(pl2.entries()[1].prev_hash(), pl2.entries()[0].hash());
    }

    #[test]
    fn opening_a_fresh_path_starts_empty_and_valid() {
        let path = scratch_path("fresh");
        let _cleanup = ScratchFile(path.clone());

        let pl = PersistentLedger::open(&path).expect("open fresh");
        assert!(pl.is_empty());
        assert_eq!(pl.len(), 0);
    }

    #[test]
    fn tampered_log_file_fails_to_open() {
        let path = scratch_path("tampered");
        let _cleanup = ScratchFile(path.clone());

        {
            let mut pl = PersistentLedger::open(&path).expect("open 1");
            pl.append(sample_record("f-0", "100.00")).expect("append 0");
            pl.append(sample_record("f-1", "200.00")).expect("append 1");
        }

        // Simulate tampering: rewrite the file with entry 0's amount
        // changed, without recomputing hashes — exactly what a
        // compromised-disk attacker would produce.
        let contents = std::fs::read_to_string(&path).unwrap();
        let mut lines: Vec<String> = contents.lines().map(|s| s.to_string()).collect();
        let mut first: serde_json::Value = serde_json::from_str(&lines[0]).unwrap();
        first["amount_usd"] = serde_json::json!("999999.00");
        lines[0] = first.to_string();
        std::fs::write(&path, lines.join("\n") + "\n").unwrap();

        let result = PersistentLedger::open(&path);
        assert!(
            matches!(result, Err(PersistError::ChainInvalid(_))),
            "expected ChainInvalid, got: {result:?}"
        );
    }

    #[test]
    fn corrupt_json_line_fails_to_open() {
        let path = scratch_path("corrupt");
        let _cleanup = ScratchFile(path.clone());

        std::fs::write(&path, "{ this is not valid json\n").unwrap();

        let result = PersistentLedger::open(&path);
        assert!(
            matches!(result, Err(PersistError::Corrupt { .. })),
            "expected Corrupt, got: {result:?}"
        );
    }

    #[test]
    fn failed_append_does_not_advance_in_memory_state() {
        // Directly exercise build_entry/push_entry ordering: if a disk
        // write were to fail, the in-memory ledger must still reflect
        // only what's confirmed on disk. We simulate this by checking
        // that build_entry (pure) does not mutate the underlying Ledger.
        let ledger = Ledger::new();
        let before = ledger.len();
        let _entry = ledger.build_entry(sample_record("f-0", "50.00"));
        assert_eq!(ledger.len(), before, "build_entry must not mutate the ledger");
    }

    fn sample_event(event_id: &str, summary: &str) -> EventInput {
        EventInput {
            event_id: event_id.to_string(),
            department: "onboarding".to_string(),
            event_type: "compliance_ruling".to_string(),
            actor: "intel_15_compliance".to_string(),
            subject_id: "client_123".to_string(),
            payload_sha256: "c".repeat(64),
            summary: summary.to_string(),
        }
    }

    #[test]
    fn events_persist_across_reopen_and_idempotency_survives_restart() {
        let path = scratch_path("events_reopen");
        let _cleanup = ScratchFile(path.clone());
        let first_hash;
        {
            let mut pl = PersistentLedger::open(&path).expect("open 1");
            pl.append(sample_record("f-0", "10.00")).expect("finding");
            match pl.append_event(sample_event("onb-1", "blocked")).expect("event") {
                EventAppendOutcome::Created(e) => first_hash = e.hash().to_string(),
                other => panic!("expected Created, got {other:?}"),
            }
            pl.append(sample_record("f-2", "20.00")).expect("finding 2");
        }

        let mut pl = PersistentLedger::open(&path).expect("open 2 (replay)");
        assert_eq!(pl.len(), 3);
        assert_eq!(pl.verify_chain(), Ok(()));
        assert_eq!(pl.entries()[1].as_event().unwrap().event_id, "onb-1");

        // Identical retry after restart: 200-equivalent, nothing written.
        match pl.append_event(sample_event("onb-1", "blocked")).unwrap() {
            EventAppendOutcome::Existing(e) => assert_eq!(e.hash(), first_hash),
            other => panic!("expected Existing, got {other:?}"),
        }
        // Different content after restart: conflict, nothing written.
        match pl.append_event(sample_event("onb-1", "approved")).unwrap() {
            EventAppendOutcome::Conflict(e) => assert_eq!(e.hash(), first_hash),
            other => panic!("expected Conflict, got {other:?}"),
        }
        assert_eq!(pl.len(), 3);
        let lines = std::fs::read_to_string(&path).unwrap().lines().count();
        assert_eq!(lines, 3, "retries and conflicts must not write to disk");
    }

    #[test]
    fn duplicate_event_id_in_log_fails_to_open() {
        let path = scratch_path("dup_event");
        let _cleanup = ScratchFile(path.clone());
        {
            let mut pl = PersistentLedger::open(&path).expect("open");
            pl.append_event(sample_event("onb-1", "x")).unwrap();
        }
        // Hand-append a correctly-chained second entry that reuses the id.
        let mut l = Ledger::from_entries(PersistentLedger::open(&path).unwrap().entries().to_vec());
        let dup = l.build_event_entry(sample_event("onb-1", "y"));
        l.push_entry(dup.clone());
        assert_eq!(l.verify_chain(), Ok(()), "chain itself is valid");
        let mut f = OpenOptions::new().append(true).open(&path).unwrap();
        writeln!(f, "{}", serde_json::to_string(&dup).unwrap()).unwrap();

        let result = PersistentLedger::open(&path);
        assert!(
            matches!(result, Err(PersistError::Corrupt { line: 2, .. })),
            "expected Corrupt at line 2, got: {result:?}"
        );
    }

    /// Legacy fixture: tests/fixtures/legacy_ledger_v1.jsonl was written by
    /// the ACTUAL pre-change server binary (built from commit 9531fc2) over
    /// real HTTP, with numeric amount_usd values including 0.1,
    /// 0.30000000000000004, 1234567.89 and a null. The new code must load
    /// it, verify it, extend it with findings and events, and re-open it.
    #[test]
    fn legacy_persisted_file_from_old_binary_loads_verifies_and_extends() {
        let fixture = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/legacy_ledger_v1.jsonl");
        let path = scratch_path("legacy_fixture");
        let _cleanup = ScratchFile(path.clone());
        std::fs::copy(fixture, &path).unwrap();
        let original = std::fs::read_to_string(&path).unwrap();
        assert!(original.contains("\"amount_usd\":0.30000000000000004"));
        assert!(!original.contains("\"kind\""));

        {
            let mut pl = PersistentLedger::open(&path).expect("legacy file must open and verify");
            assert_eq!(pl.len(), 11);
            let amounts: Vec<Option<String>> = pl
                .entries()
                .iter()
                .map(|e| e.as_finding().unwrap().amount_usd.as_ref().map(|m| m.to_string()))
                .collect();
            let expected = [
                "120.00", "54.38", "89.99", "39.00", "900.00", "0.10", "2.01", "49.99", "0.30", "1234567.89",
            ];
            for (i, want) in expected.iter().enumerate() {
                assert_eq!(amounts[i].as_deref(), Some(*want), "entry {i}");
            }
            assert_eq!(amounts[10], None);

            pl.append(sample_record("f-new", "12.30")).unwrap();
            pl.append_event(sample_event("onb-after-legacy", "recorded after legacy entries")).unwrap();
            assert_eq!(pl.verify_chain(), Ok(()));
        }

        let pl = PersistentLedger::open(&path).expect("reopen mixed legacy + new log");
        assert_eq!(pl.len(), 13);
        assert_eq!(pl.verify_chain(), Ok(()));
        // Legacy lines on disk are never rewritten.
        assert!(std::fs::read_to_string(&path).unwrap().starts_with(&original));
    }

    #[test]
    fn tampered_legacy_numeric_amount_is_detected() {
        let fixture = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/legacy_ledger_v1.jsonl");
        let path = scratch_path("legacy_tamper");
        let _cleanup = ScratchFile(path.clone());
        let text = std::fs::read_to_string(fixture).unwrap();
        // 49.99 -> 49.98 : a one-cent change to a legacy numeric amount.
        let tampered = text.replacen("\"amount_usd\":49.99", "\"amount_usd\":49.98", 1);
        assert_ne!(text, tampered);
        std::fs::write(&path, tampered).unwrap();
        let result = PersistentLedger::open(&path);
        assert!(matches!(result, Err(PersistError::ChainInvalid(_))), "got: {result:?}");
    }

    /// RAII cleanup for the scratch files these tests write to /tmp.
    struct ScratchFile(PathBuf);
    impl Drop for ScratchFile {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }
}
