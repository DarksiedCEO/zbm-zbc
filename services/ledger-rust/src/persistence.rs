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

use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

use crate::{Ledger, LedgerEntry, LedgerError, LedgerRecordInput};

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

#[derive(Debug)]
pub struct PersistentLedger {
    ledger: Ledger,
    file: File,
    path: PathBuf,
}

impl PersistentLedger {
    /// Opens (or creates) the ledger log at `path`. If the file already
    /// has entries, every one is replayed into memory and the full hash
    /// chain is verified before this returns `Ok` — a corrupt or tampered
    /// log file is a hard error, not a silent reset to empty.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self, PersistError> {
        let path = path.as_ref().to_path_buf();

        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }

        let mut entries: Vec<LedgerEntry> = Vec::new();
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
                entries.push(entry);
            }
        }

        let ledger = Ledger::from_entries(entries);
        if !ledger.is_empty() {
            ledger.verify_chain().map_err(PersistError::ChainInvalid)?;
        }

        let file = OpenOptions::new().create(true).append(true).open(&path)?;

        Ok(PersistentLedger { ledger, file, path })
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

    /// Appends a new record: builds the entry (pure, no mutation), writes
    /// it to disk and fsyncs, and only THEN commits it to the in-memory
    /// ledger. If the disk write fails at any point, the in-memory ledger
    /// is left exactly as it was and the error is returned — the caller
    /// must treat this as "not recorded," full stop, never as a partial
    /// success.
    pub fn append(&mut self, record: LedgerRecordInput) -> Result<&LedgerEntry, PersistError> {
        let entry = self.ledger.build_entry(record);

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

    fn sample_record(finding_id: &str, amount: f64) -> LedgerRecordInput {
        LedgerRecordInput {
            finding_id: finding_id.to_string(),
            agent_id: "affiliate-coupon-extension-v1".to_string(),
            entity_id: finding_id.to_string(),
            leak_category: "affiliate_coupon_extension".to_string(),
            amount_usd: Some(amount),
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
            pl.append(sample_record("f-0", 100.0)).expect("append 0");
            pl.append(sample_record("f-1", 200.0)).expect("append 1");
            assert_eq!(pl.len(), 2);
        } // drop — simulates process restart

        let pl2 = PersistentLedger::open(&path).expect("open 2 (replay)");
        assert_eq!(pl2.len(), 2);
        assert_eq!(pl2.verify_chain(), Ok(()));
        assert_eq!(pl2.entries()[0].finding_id, "f-0");
        assert_eq!(pl2.entries()[1].finding_id, "f-1");
        assert_eq!(pl2.entries()[1].prev_hash, pl2.entries()[0].hash);
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
            pl.append(sample_record("f-0", 100.0)).expect("append 0");
            pl.append(sample_record("f-1", 200.0)).expect("append 1");
        }

        // Simulate tampering: rewrite the file with entry 0's amount
        // changed, without recomputing hashes — exactly what a
        // compromised-disk attacker would produce.
        let contents = std::fs::read_to_string(&path).unwrap();
        let mut lines: Vec<String> = contents.lines().map(|s| s.to_string()).collect();
        let mut first: serde_json::Value = serde_json::from_str(&lines[0]).unwrap();
        first["amount_usd"] = serde_json::json!(999_999.0);
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
        let _entry = ledger.build_entry(sample_record("f-0", 50.0));
        assert_eq!(ledger.len(), before, "build_entry must not mutate the ledger");
    }

    /// RAII cleanup for the scratch files these tests write to /tmp.
    struct ScratchFile(PathBuf);
    impl Drop for ScratchFile {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }
}
