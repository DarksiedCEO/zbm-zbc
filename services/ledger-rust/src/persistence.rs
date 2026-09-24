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
//!   corrupt complete line, a hash mismatch, a broken prev_hash pointer, an
//!   ambiguous canonical form) is returned as an error, never swallowed.
//!   The single exception is an unterminated final line, which was never
//!   acknowledged (see "Crash safety" below).
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
//!
//! Crash safety (fix wave 1, AEGIS F5, docs/adr/0003 section 5):
//!
//! - An append reports success only after the WHOLE line including its
//!   trailing `\n` has been written and fsync'd. So a final segment of the
//!   file that is not newline-terminated was never acknowledged to anyone.
//!   On open it is treated as a torn write: the bytes are copied to a side
//!   file `<log>.torn-<unix_nanos>` (fsync'd), the log is truncated back to
//!   the end of its last complete line (fsync'd), and a loud warning is
//!   logged. This happens only AFTER every complete line has been parsed and
//!   the full chain verified, so corruption anywhere else — a mid-file line,
//!   or a complete newline-terminated line that fails to parse or verify —
//!   still refuses to open and the file is left untouched.
//! - If a write, flush or fsync fails partway through an append, the file is
//!   truncated back to its pre-append length and the in-memory ledger does
//!   not advance. If even that rollback fails, the ledger is "poisoned": it
//!   refuses every further append until restart (restart then applies the
//!   torn-tail rule above), because writing after an unknown partial line
//!   would turn a recoverable torn tail into mid-file corruption.

use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Write};
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
    /// The record/event was refused before anything was written (it would
    /// have an ambiguous canonical form). Maps to HTTP 400.
    Invalid(String),
    /// A failed append could not be rolled back; no further appends are
    /// accepted by this process (see module docs).
    Poisoned(String),
}

impl std::fmt::Display for PersistError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PersistError::Io(e) => write!(f, "ledger log I/O error: {e}"),
            PersistError::Corrupt { line, reason } => {
                write!(f, "ledger log corrupt at line {line}: {reason}")
            }
            PersistError::ChainInvalid(e) => write!(f, "ledger chain integrity failure: {e:?}"),
            PersistError::Invalid(reason) => write!(f, "refused before writing: {reason}"),
            PersistError::Poisoned(reason) => write!(
                f,
                "ledger refuses appends until restart: an earlier failed append could not be rolled back ({reason})"
            ),
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

/// The append handle, behind a trait so tests can inject a real file that
/// fails partway through a write (see tests below). Production uses `File`.
pub(crate) trait LogSink: Write + Send + std::fmt::Debug {
    fn sync_data(&mut self) -> io::Result<()>;
    fn set_len(&mut self, len: u64) -> io::Result<()>;
    fn current_len(&mut self) -> io::Result<u64>;
}

impl LogSink for File {
    fn sync_data(&mut self) -> io::Result<()> {
        File::sync_data(self)
    }
    fn set_len(&mut self, len: u64) -> io::Result<()> {
        File::set_len(self, len)
    }
    fn current_len(&mut self) -> io::Result<u64> {
        Ok(self.metadata()?.len())
    }
}

/// What `open` did about a torn final line, if anything.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TornTailRecovery {
    /// Number of unterminated bytes removed from the end of the log.
    pub torn_bytes: usize,
    /// Log length after truncation (end of the last complete line).
    pub truncated_to: u64,
    /// Side file holding exactly the removed bytes.
    pub preserved_at: PathBuf,
}

#[derive(Debug)]
pub struct PersistentLedger {
    ledger: Ledger,
    event_index: HashMap<String, usize>,
    file: Box<dyn LogSink>,
    path: PathBuf,
    torn_tail: Option<TornTailRecovery>,
    poisoned: Option<String>,
}

fn fsync_dir(path: &Path) -> io::Result<()> {
    let dir = match path.parent() {
        Some(p) if !p.as_os_str().is_empty() => p.to_path_buf(),
        _ => PathBuf::from("."),
    };
    File::open(dir)?.sync_all()
}

/// Copies the torn bytes to a new side file and truncates the log back to
/// `complete_len`, fsyncing both (and the directory). If the side file
/// cannot be written, the log is NOT truncated and an error is returned —
/// evidence is never destroyed to get the service up.
fn recover_torn_tail(path: &Path, complete_len: u64, torn: &[u8]) -> Result<TornTailRecovery, PersistError> {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let mut side_name = path.file_name().unwrap_or_default().to_os_string();
    side_name.push(format!(".torn-{nanos}"));
    let side = path.with_file_name(side_name);
    {
        let mut f = OpenOptions::new().write(true).create_new(true).open(&side)?;
        f.write_all(torn)?;
        f.sync_all()?;
    }
    {
        let f = OpenOptions::new().write(true).open(path)?;
        f.set_len(complete_len)?;
        f.sync_all()?;
    }
    fsync_dir(path)?;
    Ok(TornTailRecovery { torn_bytes: torn.len(), truncated_to: complete_len, preserved_at: side })
}

impl PersistentLedger {
    /// Opens (or creates) the ledger log at `path`. If the file already
    /// has entries, every one is replayed into memory and the full hash
    /// chain is verified before this returns `Ok` — a corrupt or tampered
    /// log file is a hard error, not a silent reset to empty. A torn,
    /// unterminated final line is preserved to a side file and truncated
    /// (module docs, "Crash safety").
    ///
    /// Legacy log lines (written before Sep 24 2026: numeric `amount_usd`,
    /// no `kind`) load as findings with the amount converted exactly as the
    /// old hash formatted it, so their hashes still verify.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self, PersistError> {
        Self::open_with_sink(path, |f| Box::new(f))
    }

    /// `open`, with the append handle wrapped by `wrap` (tests inject a
    /// failing writer through this; production passes the `File` through).
    pub(crate) fn open_with_sink<P: AsRef<Path>>(
        path: P,
        wrap: impl FnOnce(File) -> Box<dyn LogSink>,
    ) -> Result<Self, PersistError> {
        let path = path.as_ref().to_path_buf();

        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }

        let existed = path.exists();
        let mut bytes = Vec::new();
        if existed {
            File::open(&path)?.read_to_end(&mut bytes)?;
        }
        // Everything up to and including the last '\n' is complete lines;
        // anything after it is an unterminated (never acknowledged) tail.
        let complete_len = bytes.iter().rposition(|&b| b == b'\n').map(|p| p + 1).unwrap_or(0);

        let mut entries: Vec<LedgerEntry> = Vec::new();
        let mut event_index: HashMap<String, usize> = HashMap::new();
        for (i, raw) in bytes[..complete_len].split_inclusive(|&b| b == b'\n').enumerate() {
            let raw = &raw[..raw.len() - 1]; // strip the '\n'
            let line = std::str::from_utf8(raw).map_err(|e| PersistError::Corrupt {
                line: i + 1,
                reason: format!("line is not valid UTF-8: {e}"),
            })?;
            if line.trim().is_empty() {
                continue;
            }
            let entry: LedgerEntry = serde_json::from_str(line).map_err(|e| PersistError::Corrupt {
                line: i + 1,
                reason: e.to_string(),
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

        let ledger = Ledger::from_entries(entries);
        ledger.verify_chain().map_err(PersistError::ChainInvalid)?;

        // Only now — every complete line parsed and the whole chain verified —
        // is an unterminated tail treated as a torn, unacknowledged write.
        let torn = &bytes[complete_len..];
        let torn_tail = if torn.is_empty() {
            None
        } else {
            let r = recover_torn_tail(&path, complete_len as u64, torn)?;
            crate::ledger_log!(
                "ledger-rust: WARNING — TORN FINAL LINE in {}: {} unterminated byte(s) after the last \
                 complete entry (a crash or failed write mid-append; never acknowledged, because an \
                 append reports success only after the full line and its newline are fsynced). \
                 Preserved them to {} and truncated the log to {} bytes. {} verified entries kept.",
                path.display(),
                r.torn_bytes,
                r.preserved_at.display(),
                r.truncated_to,
                ledger.len(),
            );
            Some(r)
        };

        let file = OpenOptions::new().create(true).append(true).open(&path)?;
        if !existed {
            // Make the new file's directory entry durable too.
            file.sync_all()?;
            fsync_dir(&path)?;
        }

        Ok(PersistentLedger { ledger, event_index, file: wrap(file), path, torn_tail, poisoned: None })
    }

    /// Set when `open` truncated a torn final line (see module docs).
    pub fn torn_tail_recovery(&self) -> Option<&TornTailRecovery> {
        self.torn_tail.as_ref()
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
    ///
    /// If the write, flush or fsync fails at any point, the file is
    /// truncated back to its pre-append length (and fsync'd) before the
    /// error is returned, so no partial line is left for the next append to
    /// write after. If that rollback itself fails, the ledger is poisoned.
    fn persist_then_push(&mut self, entry: LedgerEntry) -> Result<&LedgerEntry, PersistError> {
        if let Some(reason) = &self.poisoned {
            return Err(PersistError::Poisoned(reason.clone()));
        }
        let mut line = serde_json::to_string(&entry).map_err(|e| PersistError::Corrupt {
            line: 0,
            reason: format!("failed to serialize new entry: {e}"),
        })?;
        line.push('\n');

        let pre_len = self.file.current_len()?;
        let written = self
            .file
            .write_all(line.as_bytes())
            .and_then(|()| self.file.flush())
            .and_then(|()| self.file.sync_data());
        if let Err(write_err) = written {
            let rollback = self.file.set_len(pre_len).and_then(|()| self.file.sync_data());
            if let Err(rollback_err) = rollback {
                let reason = format!(
                    "append failed ({write_err}) and truncating {} back to {pre_len} bytes also \
                     failed ({rollback_err})",
                    self.path.display()
                );
                crate::ledger_log!("ledger-rust: CRITICAL — {reason}; refusing all further appends until restart");
                self.poisoned = Some(reason);
            }
            return Err(PersistError::Io(write_err));
        }

        self.ledger.push_entry(entry);
        Ok(self.ledger.entries().last().expect("just pushed"))
    }

    /// Appends a new finding record: builds the entry (pure, no mutation),
    /// writes it to disk and fsyncs, and only THEN commits it to the
    /// in-memory ledger. If the disk write fails at any point, the
    /// in-memory ledger is left exactly as it was and the error is returned
    /// — the caller must treat this as "not recorded," full stop, never as
    /// a partial success.
    ///
    /// The record is validated first (`LedgerRecordInput::validate`): an
    /// entry with an ambiguous canonical form is never written, because the
    /// next open would (correctly) refuse to load it.
    pub fn append(&mut self, record: LedgerRecordInput) -> Result<&LedgerEntry, PersistError> {
        record.validate().map_err(PersistError::Invalid)?;
        let entry = self.ledger.build_entry(record);
        self.persist_then_push(entry)
    }

    /// Idempotent event append on the shared chain. `input` is validated
    /// here too (`EventInput::validate`; callers should validate first to
    /// give a precise 400). Same durability-before-visibility rule as
    /// `append`.
    pub fn append_event(&mut self, input: EventInput) -> Result<EventAppendOutcome<'_>, PersistError> {
        input.validate().map_err(|e| PersistError::Invalid(e.to_string()))?;
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
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
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

    /// A real file that lets the first `pass_bytes` bytes of the NEXT write
    /// reach the disk and then fails that write (like ENOSPC/EFBIG part-way
    /// through), optionally failing fsync and/or truncation as well. One-shot:
    /// after one injected failure it behaves like the plain file again.
    #[derive(Debug)]
    struct FaultyFile {
        inner: File,
        armed: Arc<AtomicBool>,
        pass_bytes: usize,
        fail_write: bool,
        fail_sync: bool,
        fail_truncate: bool,
    }

    impl Write for FaultyFile {
        fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
            if self.armed.load(Ordering::SeqCst) && self.fail_write {
                if self.pass_bytes == 0 {
                    self.armed.store(false, Ordering::SeqCst);
                    return Err(io::Error::other("injected: no space left on device"));
                }
                let n = buf.len().min(self.pass_bytes);
                self.pass_bytes -= n;
                return self.inner.write(&buf[..n]);
            }
            self.inner.write(buf)
        }
        fn flush(&mut self) -> io::Result<()> {
            self.inner.flush()
        }
    }

    impl LogSink for FaultyFile {
        fn sync_data(&mut self) -> io::Result<()> {
            if self.armed.load(Ordering::SeqCst) && self.fail_sync {
                self.armed.store(false, Ordering::SeqCst);
                return Err(io::Error::other("injected: fsync failed"));
            }
            self.inner.sync_data()
        }
        fn set_len(&mut self, len: u64) -> io::Result<()> {
            if self.fail_truncate {
                return Err(io::Error::other("injected: truncate failed"));
            }
            self.inner.set_len(len)
        }
        fn current_len(&mut self) -> io::Result<u64> {
            self.inner.current_len()
        }
    }

    /// Opens with a FaultyFile sink; the returned flag arms one fault.
    fn open_faulty(
        path: &Path,
        pass_bytes: usize,
        fail_write: bool,
        fail_sync: bool,
        fail_truncate: bool,
    ) -> (PersistentLedger, Arc<AtomicBool>) {
        let armed = Arc::new(AtomicBool::new(false));
        let flag = armed.clone();
        let pl = PersistentLedger::open_with_sink(path, move |inner| {
            Box::new(FaultyFile { inner, armed, pass_bytes, fail_write, fail_sync, fail_truncate })
        })
        .expect("open");
        (pl, flag)
    }

    fn arm(flag: &AtomicBool) {
        flag.store(true, Ordering::SeqCst);
    }

    /// (Replaces the old version of this test, which never made a write fail
    /// — it only checked that `build_entry` is pure, so it could not catch a
    /// partial line left on disk. AEGIS F5, PLAUSIBLE case.)
    #[test]
    fn failed_append_does_not_advance_in_memory_state() {
        let path = scratch_path("failed_append");
        let _cleanup = ScratchFile(path.clone());
        let (mut pl, fault) = open_faulty(&path, 57, true, false, false);
        pl.append(sample_record("f-0", "50.00")).expect("clean append");
        let before = std::fs::read(&path).unwrap();

        arm(&fault);
        let err = pl.append(sample_record("f-1", "60.00")).expect_err("write must fail");
        assert!(matches!(err, PersistError::Io(_)), "{err:?}");
        assert_eq!(pl.len(), 1, "memory must not advance");
        assert_eq!(std::fs::read(&path).unwrap(), before, "57 partial bytes were written, then rolled back");

        // Same for events, and a failed event is not indexed.
        // A fresh 57-byte budget for the second injected failure.
        drop(pl);
        let (mut pl, fault) = open_faulty(&path, 57, true, false, false);
        arm(&fault);
        let err = pl.append_event(sample_event("onb-x", "s")).expect_err("write must fail");
        assert!(matches!(err, PersistError::Io(_)), "{err:?}");
        assert_eq!(pl.len(), 1);
        assert!(pl.event_index.is_empty(), "failed event must not enter the idempotency index");
        assert_eq!(std::fs::read(&path).unwrap(), before);

        // The fault is gone: the next appends land right after the good line.
        pl.append(sample_record("f-1", "60.00")).expect("append after rollback");
        assert!(matches!(pl.append_event(sample_event("onb-x", "s")).unwrap(), EventAppendOutcome::Created(_)));
        drop(pl);
        let pl = PersistentLedger::open(&path).expect("reopen: no torn tail, no corruption");
        assert!(pl.torn_tail_recovery().is_none());
        assert_eq!(pl.len(), 3);
        assert_eq!(pl.verify_chain(), Ok(()));
    }

    #[test]
    fn failed_fsync_rolls_back_the_complete_line() {
        let path = scratch_path("failed_fsync");
        let _cleanup = ScratchFile(path.clone());
        let (mut pl, fault) = open_faulty(&path, 0, false, true, false);
        pl.append(sample_record("f-0", "50.00")).unwrap();
        let before = std::fs::read(&path).unwrap();
        arm(&fault);
        assert!(pl.append(sample_record("f-1", "60.00")).is_err());
        assert_eq!(pl.len(), 1);
        assert_eq!(std::fs::read(&path).unwrap(), before, "fully written but un-synced line removed");
    }

    #[test]
    fn failed_rollback_poisons_until_restart_and_restart_recovers() {
        let path = scratch_path("poisoned");
        let _cleanup = ScratchFile(path.clone());
        let (mut pl, fault) = open_faulty(&path, 30, true, false, true);
        pl.append(sample_record("f-0", "50.00")).unwrap();
        let before = std::fs::read(&path).unwrap();
        arm(&fault);
        assert!(matches!(pl.append(sample_record("f-1", "60.00")), Err(PersistError::Io(_))));
        // The partial bytes could not be removed, so NOTHING more may be written.
        assert!(matches!(pl.append(sample_record("f-2", "70.00")), Err(PersistError::Poisoned(_))));
        assert!(matches!(pl.append_event(sample_event("onb-1", "s")), Err(PersistError::Poisoned(_))));
        assert_eq!(std::fs::read(&path).unwrap().len(), before.len() + 30, "only the one torn write on disk");
        drop(pl);

        let pl = PersistentLedger::open(&path).expect("restart recovers the torn tail");
        let r = pl.torn_tail_recovery().expect("torn tail recovered").clone();
        assert_eq!(r.torn_bytes, 30);
        assert_eq!(std::fs::read(&path).unwrap(), before);
        let _side = ScratchFile(r.preserved_at.clone());
        assert_eq!(pl.len(), 1);
    }

    // --- torn final line on open (AEGIS F5) -------------------------------------

    /// Writes two good entries, then appends `tail` raw.
    fn log_with_tail(label: &str, tail: &[u8]) -> (PathBuf, Vec<u8>) {
        let path = scratch_path(label);
        {
            let mut pl = PersistentLedger::open(&path).unwrap();
            pl.append(sample_record("f-0", "10.00")).unwrap();
            pl.append_event(sample_event("onb-1", "s")).unwrap();
        }
        let good = std::fs::read(&path).unwrap();
        let mut f = OpenOptions::new().append(true).open(&path).unwrap();
        f.write_all(tail).unwrap();
        (path, good)
    }

    #[test]
    fn torn_unparseable_final_line_is_preserved_and_truncated() {
        let tails: [&[u8]; 4] = [
            b"{\"kind\":\"event\",\"seq\":2,\"event_id\":\"e",
            b"{",
            b"   ",
            "{\"summary\":\"caf\u{e9}".as_bytes().split_last().unwrap().1, // cut inside a UTF-8 sequence
        ];
        for tail in tails {
            let (path, good) = log_with_tail("torn", tail);
            let _cleanup = ScratchFile(path.clone());
            let mut pl = PersistentLedger::open(&path).expect("torn tail must not brick the ledger");
            let r = pl.torn_tail_recovery().expect("recovery recorded").clone();
            let _side = ScratchFile(r.preserved_at.clone());
            assert_eq!(r.torn_bytes, tail.len());
            assert_eq!(r.truncated_to, good.len() as u64);
            assert_eq!(std::fs::read(&r.preserved_at).unwrap(), tail, "exact torn bytes preserved");
            assert_eq!(std::fs::read(&path).unwrap(), good);
            assert_eq!(pl.len(), 2);
            pl.append(sample_record("f-2", "20.00")).unwrap();
            drop(pl);
            let pl = PersistentLedger::open(&path).unwrap();
            assert!(pl.torn_tail_recovery().is_none());
            assert_eq!(pl.len(), 3);
            assert_eq!(pl.verify_chain(), Ok(()));
        }
    }

    /// A complete, valid entry missing only its trailing newline was also
    /// never acknowledged (success is reported only after the newline is
    /// fsynced), so it is handled exactly like any other torn tail.
    #[test]
    fn unterminated_but_parseable_final_line_is_also_unacknowledged() {
        let path = scratch_path("unterminated_valid");
        let _cleanup = ScratchFile(path.clone());
        {
            let mut pl = PersistentLedger::open(&path).unwrap();
            pl.append(sample_record("f-0", "10.00")).unwrap();
            pl.append(sample_record("f-1", "11.00")).unwrap();
        }
        let full = std::fs::read(&path).unwrap();
        std::fs::write(&path, &full[..full.len() - 1]).unwrap();
        let first_len = full.iter().position(|&b| b == b'\n').unwrap() + 1;
        let pl = PersistentLedger::open(&path).unwrap();
        let r = pl.torn_tail_recovery().unwrap().clone();
        let _side = ScratchFile(r.preserved_at.clone());
        assert_eq!(pl.len(), 1);
        assert_eq!(std::fs::read(&path).unwrap(), &full[..first_len]);
        assert_eq!(std::fs::read(&r.preserved_at).unwrap(), &full[first_len..full.len() - 1]);
    }

    #[test]
    fn a_log_that_is_only_a_torn_first_line_opens_empty() {
        let path = scratch_path("only_torn");
        let _cleanup = ScratchFile(path.clone());
        std::fs::write(&path, b"{\"kind\":\"fin").unwrap();
        let pl = PersistentLedger::open(&path).unwrap();
        let _side = ScratchFile(pl.torn_tail_recovery().unwrap().preserved_at.clone());
        assert!(pl.is_empty());
        assert_eq!(std::fs::read(&path).unwrap(), b"");
    }

    #[test]
    fn corruption_other_than_a_torn_tail_still_refuses_and_touches_nothing() {
        let (path, good) = log_with_tail("mid_corrupt_src", b"");
        let _cleanup = ScratchFile(path.clone());
        let text = String::from_utf8(good).unwrap();
        let lines: Vec<&str> = text.lines().collect();
        let cases: Vec<(&str, Vec<u8>)> = vec![
            ("mid-file bad line", format!("{}\n{{ nope\n{}\n", lines[0], lines[1]).into_bytes()),
            ("terminated bad final line", format!("{}\n{}\n{{ nope\n", lines[0], lines[1]).into_bytes()),
            ("terminated final line, bad hash", format!("{}\n{}\n", lines[0], lines[1].replace("\"s\"", "\"t\"")).into_bytes()),
            ("mid-file bad line plus torn tail", format!("{}\n{{ nope\n{}\n{{\"k", lines[0], lines[1]).into_bytes()),
            ("bad hash plus torn tail", format!("{}\n{}\n{{\"k", lines[0], lines[1].replace("\"s\"", "\"t\"")).into_bytes()),
            ("invalid UTF-8 in a complete line", [lines[0].as_bytes(), b"\n\xff\xfe\n"].concat()),
        ];
        for (name, content) in cases {
            let p = scratch_path("mid_corrupt");
            let _c = ScratchFile(p.clone());
            std::fs::write(&p, &content).unwrap();
            let r = PersistentLedger::open(&p);
            assert!(
                matches!(r, Err(PersistError::Corrupt { .. }) | Err(PersistError::ChainInvalid(_))),
                "{name}: expected refusal, got {r:?}"
            );
            assert_eq!(std::fs::read(&p).unwrap(), content, "{name}: file untouched");
            let dir = p.parent().unwrap();
            let prefix = format!("{}.torn-", p.file_name().unwrap().to_str().unwrap());
            let sides = std::fs::read_dir(dir)
                .unwrap()
                .filter(|e| e.as_ref().unwrap().file_name().to_str().unwrap().starts_with(&prefix))
                .count();
            assert_eq!(sides, 0, "{name}: no side file");
        }
    }

    // --- F6 / F7 on real files --------------------------------------------------

    /// Written by the REAL 9531fc2 binary over HTTP (every POST 201, and the
    /// old binary's /ledger/verify said {"entries":15,"valid":true}).
    #[test]
    fn legacy_negative_amount_log_from_old_binary_loads_verifies_and_extends() {
        let fixture = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/legacy_ledger_v2_negatives.jsonl");
        let path = scratch_path("legacy_v2");
        let _cleanup = ScratchFile(path.clone());
        std::fs::copy(fixture, &path).unwrap();
        let original = std::fs::read_to_string(&path).unwrap();
        for raw in ["\"amount_usd\":-0.0,", "\"amount_usd\":-5.0,", "\"amount_usd\":-0.001,", "\"amount_usd\":-1e-9,"] {
            assert!(original.contains(raw), "fixture must contain {raw}");
        }
        let mut pl = PersistentLedger::open(&path).expect("every log the old binary wrote must load");
        assert_eq!(pl.len(), 15);
        assert_eq!(pl.verify_chain(), Ok(()));
        let amounts: Vec<Option<&str>> = pl
            .entries()
            .iter()
            .map(|e| e.as_finding().unwrap().amount_usd.as_ref().map(|m| m.as_str()))
            .collect();
        assert_eq!(
            amounts,
            [
                Some("-0.00"), Some("-5.00"), Some("-0.00"), Some("120.00"), Some("54.38"), Some("0.10"),
                Some("0.30"), Some("1234567.89"), Some("-12.35"), Some("-0.01"), Some("-0.00"), Some("2.67"),
                Some("100000000000000000000.00"), None, None,
            ]
        );
        pl.append(sample_record("f-new", "12.30")).unwrap();
        pl.append_event(sample_event("onb-after-legacy", "after")).unwrap();
        drop(pl);
        let pl = PersistentLedger::open(&path).unwrap();
        assert_eq!(pl.len(), 17);
        assert_eq!(pl.verify_chain(), Ok(()));
        assert!(std::fs::read_to_string(&path).unwrap().starts_with(&original));
    }

    #[test]
    fn aegis_forged_resplit_log_refuses_to_open() {
        let fixture = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/aegis_forged_resplit.jsonl");
        let r = PersistentLedger::open(fixture);
        match r {
            Err(PersistError::Corrupt { line: 1, reason }) => assert!(reason.contains("ambiguous"), "{reason}"),
            other => panic!("expected Corrupt at line 1, got {other:?}"),
        }
    }

    #[test]
    fn ambiguous_legacy_entries_from_old_binary_refuse_to_open() {
        for name in ["legacy_ambiguous_pipe.jsonl", "legacy_ambiguous_null.jsonl"] {
            let fixture = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
            match PersistentLedger::open(&fixture) {
                Err(PersistError::Corrupt { line: 1, reason }) => assert!(reason.contains("ambiguous"), "{name}: {reason}"),
                other => panic!("{name}: expected Corrupt, got {other:?}"),
            }
        }
    }

    #[test]
    fn ambiguous_records_are_refused_before_anything_is_written() {
        let path = scratch_path("refuse_ambiguous");
        let _cleanup = ScratchFile(path.clone());
        let mut pl = PersistentLedger::open(&path).unwrap();
        let mut r = sample_record("f-0", "10.00");
        r.agent_id = "agent|x".into();
        assert!(matches!(pl.append(r), Err(PersistError::Invalid(_))));
        let mut r = sample_record("f-0", "10.00");
        r.decision_confidence = Some("null".into());
        assert!(matches!(pl.append(r), Err(PersistError::Invalid(_))));
        let mut e = sample_event("onb-1", "s");
        e.department = "onboarding|x".into();
        assert!(matches!(pl.append_event(e), Err(PersistError::Invalid(_))));
        assert!(pl.is_empty());
        assert_eq!(std::fs::read(&path).unwrap().len(), 0);
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

    // --- N8 (AEGIS round 2): unknown fields in persisted entries -------------

    /// tests/fixtures/aegis_unknown_field_injection.jsonl is AEGIS's probe
    /// output: a real entry written by the current binary, then given an
    /// extra `"approved_by":"andre"`. The field is not part of the hash, so
    /// the chain still verifies — before this fix the loader silently
    /// dropped it and the log opened, i.e. anyone with file access could
    /// add unhashed "evidence" that a reader of the raw log would trust.
    #[test]
    fn aegis_unknown_field_injection_refuses_to_open() {
        let fixture = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/aegis_unknown_field_injection.jsonl");
        assert!(std::fs::read_to_string(fixture).unwrap().contains("\"approved_by\":\"andre\""));
        match PersistentLedger::open(fixture) {
            Err(PersistError::Corrupt { line: 1, reason }) => {
                assert!(reason.contains("unknown field") && reason.contains("approved_by"), "{reason}")
            }
            other => panic!("expected Corrupt at line 1, got {other:?}"),
        }
    }

    /// The same injection into every persisted shape: a legacy (no kind)
    /// finding from the real old binary, a current finding, and an event.
    #[test]
    fn unknown_field_in_any_persisted_entry_kind_refuses_to_open() {
        let legacy = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/legacy_ledger_v1.jsonl"
        ))
        .unwrap();
        let path = scratch_path("unknown_field_src");
        let _cleanup = ScratchFile(path.clone());
        {
            let mut pl = PersistentLedger::open(&path).unwrap();
            pl.append(sample_record("f-0", "10.00")).unwrap();
            pl.append_event(sample_event("onb-1", "s")).unwrap();
        }
        let current = std::fs::read_to_string(&path).unwrap();
        let inject = |line: &str| -> String {
            let mut v: serde_json::Value = serde_json::from_str(line).unwrap();
            v["approved_by"] = "andre".into();
            v.to_string()
        };
        let legacy_lines: Vec<&str> = legacy.lines().collect();
        let cur: Vec<&str> = current.lines().collect();
        let cases = [
            ("legacy finding", format!("{}\n{}\n", inject(legacy_lines[0]), legacy_lines[1..].join("\n")), 1),
            ("current finding", format!("{}\n{}\n", inject(cur[0]), cur[1]), 1),
            ("event", format!("{}\n{}\n", cur[0], inject(cur[1])), 2),
        ];
        for (name, content, bad_line) in cases {
            let p = scratch_path("unknown_field");
            let _c = ScratchFile(p.clone());
            std::fs::write(&p, &content).unwrap();
            match PersistentLedger::open(&p) {
                Err(PersistError::Corrupt { line, reason }) => {
                    assert_eq!(line, bad_line, "{name}");
                    assert!(reason.contains("unknown field"), "{name}: {reason}");
                }
                other => panic!("{name}: expected Corrupt, got {other:?}"),
            }
            assert_eq!(std::fs::read_to_string(&p).unwrap(), content, "{name}: file untouched");
        }
    }

    /// The field sets of the real old binaries are the ground truth for what
    /// a persisted entry may contain: every legacy fixture must still load
    /// and verify with unknown fields denied.
    #[test]
    fn every_real_legacy_fixture_still_loads_with_unknown_fields_denied() {
        for (name, n) in [
            ("legacy_ledger_v1.jsonl", 11),
            ("legacy_ledger_v2_negatives.jsonl", 15),
            ("ledger_v3_overbound_strings.jsonl", 4),
        ] {
            let fixture = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
            let path = scratch_path("fixture_fields");
            let _cleanup = ScratchFile(path.clone());
            std::fs::copy(&fixture, &path).unwrap();
            let pl = PersistentLedger::open(&path).unwrap_or_else(|e| panic!("{name}: {e}"));
            assert_eq!(pl.len(), n, "{name}");
            assert_eq!(pl.verify_chain(), Ok(()), "{name}");
        }
    }

    #[test]
    fn a_fresh_empty_ledger_verifies() {
        let path = scratch_path("empty_verify");
        let _cleanup = ScratchFile(path.clone());
        let pl = PersistentLedger::open(&path).unwrap();
        assert!(pl.is_empty());
        assert_eq!(pl.verify_chain(), Ok(()));
    }

    /// RAII cleanup for the scratch files these tests write to /tmp.
    struct ScratchFile(PathBuf);
    impl Drop for ScratchFile {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }
}
