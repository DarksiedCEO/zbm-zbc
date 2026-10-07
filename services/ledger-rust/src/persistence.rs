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
//!
//! Sweep F, Oct 6 2026 (docs/adr/0003 section 13):
//!
//! - F-1, single writer: `open` takes an exclusive, non-blocking `flock` on
//!   `<log>.lock` BEFORE reading anything and holds it for the life of the
//!   `PersistentLedger`. A second opener (another process, or the same process
//!   twice) gets `PersistError::Locked` and must not start. The lock file is
//!   never removed (removing it would let a third opener lock a different
//!   inode than the holder).
//! - F-6, deletion and rollback: after every append the head checkpoint
//!   (`entries`, `head_seq`, `head_hash`) is written to `<log>.head` — temp
//!   file, fsync, rename, directory fsync — and the append is acknowledged
//!   only after that. On open the log must reach the checkpoint: a missing
//!   log, a log with fewer entries than the checkpoint, or a log whose entry
//!   at `head_seq` does not carry `head_hash` is refused
//!   (`PersistError::Checkpoint`) unless the operator passes the bound reset
//!   (AEGIS M3, below), which is logged loudly. A log exactly one entry AHEAD
//!   of the checkpoint is accepted (a crash between the log fsync and the
//!   head rename; the entry still has to verify on the chain) and the
//!   checkpoint is moved up. (Before AEGIS M1/M2: any number ahead, and a
//!   non-empty log with no head file, opened.) The head file lives next to the log, so
//!   whoever can rewrite the log can rewrite it too: it catches accidental
//!   deletion, truncation and restores of an old copy, not a deliberate
//!   forger with write access — a signed, externally published head is
//!   future work (ADR 0003 section 13).
//! - F-13: a blank (empty or whitespace-only) line anywhere in the log is
//!   corruption — the writer never produces one. An unterminated final
//!   segment that is a complete JSON value is refused too: a torn write is a
//!   strict prefix of `<json>\n`, and no strict prefix of a JSON object is
//!   itself valid JSON, so such a tail is an entry whose newline was removed
//!   after the fact, not a crash. Only a genuinely partial final line is still
//!   preserved and truncated as above.
//! - AEGIS M1-M3 (Oct 7 2026): only a log exactly ONE entry ahead of the
//!   checkpoint opens (the crash window); a non-empty log with no checkpoint
//!   opens only with `LedgerOpenOptions::migrate` equal to its `log_binding`;
//!   the operator reset only with `reset` equal to the `reset_binding` of the
//!   exact (checkpoint, log head) pair the refusal names — so a value left in
//!   the environment never stays armed. See `check_against_head`.
//! - AEGIS L4/L5: `verify_log_file` re-reads and re-hashes the log from disk;
//!   `ReadIndex` answers filtered / scoped reads without a scan.
//! - F-7: an optional `finding_id` index (`append_finding_idempotent`), the
//!   same semantics as `event_id`, used only when the caller asks for it. A
//!   log may legitimately hold the same `finding_id` several times (every
//!   writer before this change appended unconditionally), so the index maps
//!   an id to every position holding it and a duplicate is never corruption.

use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::{genesis_hash, EventInput, Ledger, LedgerEntry, LedgerError, LedgerRecordInput};

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
    /// Sweep F-1: another opener holds `<log>.lock`. Nothing was read.
    Locked(String),
    /// Sweep F-6: the log does not reach the head checkpoint (missing,
    /// shorter, or a different entry at the checkpoint), or the checkpoint
    /// file itself is unreadable. Nothing was modified.
    Checkpoint(String),
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
            PersistError::Locked(reason) => write!(f, "ledger log is locked by another writer: {reason}"),
            PersistError::Checkpoint(reason) => write!(f, "ledger head checkpoint check failed: {reason}"),
        }
    }
}

impl std::error::Error for PersistError {}

impl From<io::Error> for PersistError {
    fn from(e: io::Error) -> Self {
        PersistError::Io(e)
    }
}

/// Result of `PersistentLedger::append_event` (contract section 2) and of
/// `PersistentLedger::append_finding_idempotent` (sweep F-7).
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

/// The same three outcomes, for either kind of idempotent append.
pub type AppendOutcome<'a> = EventAppendOutcome<'a>;

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

/// Sweep F-6: the durable head checkpoint, `<log>.head`. `entries` is the
/// number of entries the log held after the last acknowledged append;
/// `head_seq` is `entries - 1` (null when empty) and `head_hash` that entry's
/// hash (the genesis hash when empty). Also the body of `GET /ledger/head`.
///
/// AEGIS N1 (Oct 7 2026): `migrated_from` / `reset_from` record, durably and
/// for good, that the chain before this ledger's checkpoint history began was
/// accepted by an operator override: the `log_binding` of the log a
/// `LEDGER_MIGRATE_LEGACY` migrated, and the `reset_binding` a
/// `LEDGER_ALLOW_RESET` accepted. Both are omitted when absent, so the
/// checkpoint of a ledger that never needed an override is byte-identical to
/// before.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HeadCheckpoint {
    pub entries: u64,
    pub head_seq: Option<u64>,
    pub head_hash: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub migrated_from: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reset_from: Option<String>,
}

/// AEGIS N1: the override history carried by every checkpoint write.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct Provenance {
    migrated_from: Option<String>,
    reset_from: Option<String>,
}

impl HeadCheckpoint {
    fn of(ledger: &Ledger) -> HeadCheckpoint {
        match ledger.entries().last() {
            None => HeadCheckpoint { entries: 0, head_seq: None, head_hash: genesis_hash(), migrated_from: None, reset_from: None },
            Some(e) => HeadCheckpoint {
                entries: ledger.len() as u64,
                head_seq: Some(e.seq()),
                head_hash: e.hash().to_string(),
                migrated_from: None,
                reset_from: None,
            },
        }
    }

    fn with(mut self, p: &Provenance) -> HeadCheckpoint {
        self.migrated_from = p.migrated_from.clone();
        self.reset_from = p.reset_from.clone();
        self
    }

    fn provenance(&self) -> Provenance {
        Provenance { migrated_from: self.migrated_from.clone(), reset_from: self.reset_from.clone() }
    }

    fn is_well_formed(&self) -> bool {
        let hex = self.head_hash.len() == 64 && self.head_hash.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
        let seq_ok = match self.head_seq {
            None => self.entries == 0,
            Some(s) => self.entries > 0 && s == self.entries - 1,
        };
        let migrated_ok = self.migrated_from.as_deref().is_none_or(is_log_binding);
        let reset_ok = self.reset_from.as_deref().is_none_or(is_reset_binding);
        hex && seq_ok && migrated_ok && reset_ok
    }
}

/// Options for `PersistentLedger::open_with`.
///
/// AEGIS M2/M3 (Oct 7 2026): both operator overrides are BOUND to the exact
/// state they accept, so a value left in the environment after use is never
/// armed for a later, different situation. The value an override needs is
/// printed by the refusal it answers (`reset_binding` / `log_binding`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LedgerOpenOptions {
    /// Sweep F-6 operator override, `LEDGER_ALLOW_RESET=<binding>`: accept a
    /// log that does not reach its head checkpoint (or a malformed
    /// checkpoint) and move the checkpoint to whatever the verified log
    /// holds — only when the value equals `<checkpoint>/<log head>` of THIS
    /// refusal (see `reset_binding`). Never skips chain verification, the
    /// single-writer lock or any corruption check.
    pub reset: Option<String>,
    /// AEGIS M2, `LEDGER_MIGRATE_LEGACY=<binding>`: a non-empty log with no
    /// head checkpoint (written by a binary older than sweep F, or whose head
    /// file was deleted) opens only when the value equals the log's own head
    /// binding (`log_binding`), and the checkpoint is then created from it.
    pub migrate: Option<String>,
}

// AEGIS N1: a value that is set but matches no refusal of THIS open refuses
// the open (`open_with_sink`), and the server exits after an override is
// applied (`bin/server.rs`), so a server never serves with either set — a
// value left behind cannot re-arm itself when an old backup is restored.

/// What `open` did about the head checkpoint (AEGIS M1-M3).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct OpenReport {
    /// No log entries and no checkpoint: a new ledger (logged `ledger_created`).
    pub created: bool,
    /// The log was exactly one entry ahead; the checkpoint was moved up.
    pub moved_up: bool,
    /// `LEDGER_ALLOW_RESET` matched and the checkpoint was reset.
    pub reset_used: bool,
    /// `LEDGER_MIGRATE_LEGACY` matched and the checkpoint was created.
    pub migrate_used: bool,
}

/// AEGIS M2/M3: `<entries>:<first 16 hex of the head hash>` — the binding of
/// a log (or checkpoint) head. Not a secret: it only ties an operator's
/// override to one exact state.
pub fn log_binding(entries: u64, head_hash: &str) -> String {
    format!("{entries}:{}", &head_hash[..head_hash.len().min(16)])
}

/// AEGIS M3: the `LEDGER_ALLOW_RESET` value that accepts moving the
/// checkpoint `from` (a `log_binding`, or `unreadable:<16 hex of the head
/// file's SHA-256>`) to the verified log head `to` (a `log_binding`).
pub fn reset_binding(from: &str, to: &str) -> String {
    format!("{from}/{to}")
}

fn is_hex16(s: &str) -> bool {
    s.len() == 16 && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn is_count(s: &str) -> bool {
    !s.is_empty() && s.len() <= 20 && s.bytes().all(|b| b.is_ascii_digit())
}

/// True when `v` has the shape of a `log_binding` (`<digits>:<16 hex>`).
pub fn is_log_binding(v: &str) -> bool {
    matches!(v.split_once(':'), Some((n, h)) if is_count(n) && is_hex16(h))
}

/// True when `v` has the shape of a `reset_binding`.
pub fn is_reset_binding(v: &str) -> bool {
    match v.split_once('/') {
        Some((from, to)) => {
            let from_ok = is_log_binding(from) || matches!(from.split_once(':'), Some(("unreadable", h)) if is_hex16(h));
            from_ok && is_log_binding(to)
        }
        None => false,
    }
}

/// `<log>` + `suffix` in the same directory (`<log>.lock`, `<log>.head`).
fn sibling(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(suffix);
    path.with_file_name(name)
}

/// The single-writer lock file of the ledger log at `log` (sweep F-1).
pub fn lock_path_for(log: &Path) -> PathBuf {
    sibling(log, ".lock")
}

/// The head checkpoint file of the ledger log at `log` (sweep F-6).
pub fn head_path_for(log: &Path) -> PathBuf {
    sibling(log, ".head")
}

fn head_tmp_path_for(log: &Path) -> PathBuf {
    sibling(log, ".head.tmp")
}

/// The department findings (`POST /ledger/append`) are recorded for (sweep
/// F-4). A read scope that includes it sees findings (AEGIS M4).
pub const FINDINGS_DEPARTMENT: &str = "revenue_recovery";

/// AEGIS L5/M4: which entries a filtered or scoped read returns.
///
/// `department` / `event_type` are the query filters of
/// `GET /ledger/entries` and match events only (a finding has neither;
/// unchanged from sweep F-2). `scope` is the caller's read scope: `None`
/// reads everything; `Some(departments)` reads only events of those
/// departments, plus findings when it includes `revenue_recovery`. A
/// `department` filter outside the scope is the caller's error (the server
/// answers 403 before it gets here); here it simply selects nothing.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct EntryFilter {
    pub department: Option<String>,
    pub event_type: Option<String>,
    pub scope: Option<Vec<String>>,
}

impl EntryFilter {
    /// True when the filter selects every entry (the plain range read).
    pub fn is_everything(&self) -> bool {
        self.department.is_none() && self.event_type.is_none() && self.scope.is_none()
    }
}

/// AEGIS L5: seq-ordered position lists per department, per event type, per
/// (department, event type), and of findings — so a filtered page costs
/// O(log n) to find its start in each list it reads plus O(page · log k) to
/// merge k lists, instead of a scan of every entry after `after_seq`. Rebuilt
/// on open; extended by every append. Positions equal `seq`.
#[derive(Debug, Default)]
struct ReadIndex {
    findings: Vec<usize>,
    by_department: HashMap<String, Vec<usize>>,
    by_type: HashMap<String, Vec<usize>>,
    by_department_type: HashMap<String, HashMap<String, Vec<usize>>>,
}

impl ReadIndex {
    fn add(&mut self, pos: usize, entry: &LedgerEntry) {
        match entry {
            LedgerEntry::Finding(_) => self.findings.push(pos),
            LedgerEntry::Event(ev) => {
                self.by_department.entry(ev.department.clone()).or_default().push(pos);
                self.by_type.entry(ev.event_type.clone()).or_default().push(pos);
                self.by_department_type
                    .entry(ev.department.clone())
                    .or_default()
                    .entry(ev.event_type.clone())
                    .or_default()
                    .push(pos);
            }
        }
    }

    /// The position lists whose union (they are disjoint) is what `f` selects;
    /// None means "every position" (no filter, no scope).
    fn sources<'a>(&'a self, f: &EntryFilter) -> Option<Vec<&'a [usize]>> {
        const NONE: &[usize] = &[];
        let dept = |d: &str| self.by_department.get(d).map_or(NONE, Vec::as_slice);
        let dept_type = |d: &str, t: &str| {
            self.by_department_type.get(d).and_then(|m| m.get(t)).map_or(NONE, Vec::as_slice)
        };
        let in_scope = |d: &str| f.scope.as_ref().is_none_or(|s| s.iter().any(|x| x == d));
        Some(match (&f.department, &f.event_type, &f.scope) {
            (None, None, None) => return None,
            (Some(d), _, _) if !in_scope(d) => vec![],
            (Some(d), None, _) => vec![dept(d)],
            (Some(d), Some(t), _) => vec![dept_type(d, t)],
            (None, Some(t), None) => vec![self.by_type.get(t.as_str()).map_or(NONE, Vec::as_slice)],
            (None, Some(t), Some(scope)) => scope.iter().map(|d| dept_type(d, t)).collect(),
            (None, None, Some(scope)) => {
                let mut v: Vec<&[usize]> = scope.iter().map(|d| dept(d)).collect();
                if scope.iter().any(|d| d == FINDINGS_DEPARTMENT) {
                    v.push(&self.findings);
                }
                v
            }
        })
    }

    /// Positions `p` with `from <= p < to` selected by `f`, ascending, at most
    /// `max`. `work` counts the steps taken (list starts found + positions
    /// merged), for the complexity guard in the tests.
    fn select(&self, f: &EntryFilter, from: usize, to: usize, max: usize, work: &mut usize) -> Vec<usize> {
        use std::cmp::Reverse;
        use std::collections::BinaryHeap;
        let Some(lists) = self.sources(f) else {
            let end = to.min(from.saturating_add(max));
            *work += end.saturating_sub(from);
            return (from..end.max(from)).collect();
        };
        let mut heap = BinaryHeap::new();
        let mut cursors: Vec<usize> = Vec::with_capacity(lists.len());
        for (k, list) in lists.iter().enumerate() {
            let i = list.partition_point(|&p| p < from);
            *work += 1;
            cursors.push(i);
            if let Some(&p) = list.get(i) {
                heap.push(Reverse((p, k)));
            }
        }
        let mut out = Vec::new();
        while out.len() < max {
            let Some(Reverse((p, k))) = heap.pop() else { break };
            *work += 1;
            if p >= to {
                break;
            }
            out.push(p);
            cursors[k] += 1;
            if let Some(&next) = lists[k].get(cursors[k]) {
                heap.push(Reverse((next, k)));
            }
        }
        out
    }
}

/// Longest log line `verify_log_file` accepts (an entry is a few KiB at most;
/// request bodies are capped at 64 KiB).
const MAX_LOG_LINE_BYTES: usize = 1024 * 1024;

/// AEGIS L4: re-reads the first `log_bytes` bytes of the log at `path` from
/// disk, parses every line with the same strictness as `open` (UTF-8, no
/// blank line, no unknown field), re-hashes the chain from the genesis hash
/// and checks that it holds exactly `entries` entries ending at `head_hash`.
/// Streams line by line (memory bounded by the longest line); takes no lock,
/// so the caller runs it outside the ledger mutex. Entries appended after the
/// snapshot (beyond `log_bytes`) are not read.
pub fn verify_log_file(path: &Path, log_bytes: u64, entries: usize, head_hash: &str) -> Result<(), String> {
    use std::io::BufRead;
    let file = File::open(path).map_err(|e| format!("cannot open {} to re-verify it: {e}", path.display()))?;
    let on_disk = file.metadata().map_err(|e| format!("cannot stat {}: {e}", path.display()))?.len();
    if on_disk < log_bytes {
        return Err(format!(
            "{} holds {on_disk} bytes on disk, fewer than the {log_bytes} bytes of the {entries} acknowledged entries",
            path.display()
        ));
    }
    let mut reader = io::BufReader::new(file.take(log_bytes));
    let mut prev = genesis_hash();
    let mut n = 0usize;
    let mut line = Vec::new();
    loop {
        line.clear();
        let got = (&mut reader)
            .take(MAX_LOG_LINE_BYTES as u64 + 1)
            .read_until(b'\n', &mut line)
            .map_err(|e| format!("read error at line {}: {e}", n + 1))?;
        if got == 0 {
            break;
        }
        if line.last() != Some(&b'\n') {
            return Err(if line.len() > MAX_LOG_LINE_BYTES {
                format!("line {} is longer than {MAX_LOG_LINE_BYTES} bytes", n + 1)
            } else {
                format!("line {} is not newline-terminated within the acknowledged bytes", n + 1)
            });
        }
        let text = std::str::from_utf8(&line[..line.len() - 1]).map_err(|e| format!("line {} is not UTF-8: {e}", n + 1))?;
        if text.trim().is_empty() {
            return Err(format!("line {} is blank", n + 1));
        }
        let entry: LedgerEntry = serde_json::from_str(text).map_err(|e| format!("line {} does not parse: {e}", n + 1))?;
        crate::verify_entry(&entry, n as u64, &prev).map_err(|e| format!("line {}: {e:?}", n + 1))?;
        prev = entry.hash().to_string();
        n += 1;
    }
    if n != entries {
        return Err(format!("the log on disk holds {n} entries in its acknowledged bytes, memory holds {entries}"));
    }
    if prev != head_hash {
        return Err(format!(
            "the log on disk ends at hash {prev}, the in-memory head is {head_hash} (the file was rewritten)"
        ));
    }
    Ok(())
}

#[derive(Debug)]
pub struct PersistentLedger {
    ledger: Ledger,
    event_index: HashMap<String, usize>,
    /// finding_id -> every position holding it (sweep F-7; duplicates are
    /// legal in a log, see module docs).
    finding_index: HashMap<String, Vec<usize>>,
    /// AEGIS L5: per-department / per-event-type positions.
    read_index: ReadIndex,
    /// Bytes of the log that hold the acknowledged entries (AEGIS L4).
    log_bytes: u64,
    file: Box<dyn LogSink>,
    path: PathBuf,
    torn_tail: Option<TornTailRecovery>,
    report: OpenReport,
    /// AEGIS N1: carried into every checkpoint write.
    provenance: Provenance,
    poisoned: Option<String>,
    /// Holds the exclusive flock on `<log>.lock` for as long as this value
    /// lives (sweep F-1). Never read; dropping it releases the lock.
    _lock: File,
    #[cfg(test)]
    fail_next_head_write: bool,
}

fn fsync_dir(path: &Path) -> io::Result<()> {
    let dir = match path.parent() {
        Some(p) if !p.as_os_str().is_empty() => p.to_path_buf(),
        _ => PathBuf::from("."),
    };
    File::open(dir)?.sync_all()
}

/// Sweep F-1: opens (creating, mode 0600, never through a symlink)
/// `<log>.lock` and takes an exclusive, non-blocking flock on it.
fn acquire_writer_lock(log: &Path) -> Result<File, PersistError> {
    let lock_path = lock_path_for(log);
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&lock_path)?;
    // SAFETY: flock on a descriptor owned by `file`, which outlives the call.
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        let e = io::Error::last_os_error();
        return Err(if e.raw_os_error() == Some(libc::EWOULDBLOCK) {
            PersistError::Locked(format!(
                "{} is held by another ledger process (or another open handle in this one); exactly one \
                 writer may serve a ledger log, so this one refuses to start",
                lock_path.display()
            ))
        } else {
            PersistError::Io(e)
        });
    }
    Ok(file)
}

/// What `<log>.head` holds (sweep F-6; AEGIS M3 adds the digest of an
/// unreadable checkpoint, which the operator reset is bound to).
#[derive(Debug)]
enum HeadState {
    Missing,
    Present(HeadCheckpoint),
    /// Unreadable or malformed: why, and the first 16 hex of the SHA-256 of
    /// its bytes (of no bytes when it cannot be read at all).
    Bad { reason: String, digest16: String },
}

fn sha256_hex16(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(bytes).iter().take(8).map(|b| format!("{b:02x}")).collect()
}

/// Reads `<log>.head`.
fn read_head(log: &Path) -> HeadState {
    let p = head_path_for(log);
    let bytes = match std::fs::read(&p) {
        Ok(b) => b,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return HeadState::Missing,
        Err(e) => {
            return HeadState::Bad { reason: format!("cannot read {}: {e}", p.display()), digest16: sha256_hex16(b"") }
        }
    };
    let digest16 = sha256_hex16(&bytes);
    let text = match std::str::from_utf8(&bytes) {
        Ok(t) => t,
        Err(_) => return HeadState::Bad { reason: format!("{} is not UTF-8", p.display()), digest16 },
    };
    match serde_json::from_str::<HeadCheckpoint>(text.trim_end_matches('\n')) {
        Err(e) => HeadState::Bad { reason: format!("{} is not a head checkpoint: {e}", p.display()), digest16 },
        Ok(head) if !head.is_well_formed() => HeadState::Bad {
            reason: format!("{} is not a well-formed head checkpoint: {text:?}", p.display()),
            digest16,
        },
        Ok(head) => HeadState::Present(head),
    }
}

/// AEGIS M2: one structured (single-line JSON) startup record for an event
/// an operator must be able to find and alert on: a new ledger created, a
/// legacy log migrated, an operator reset, a checkpoint moved up.
pub(crate) fn log_startup_event(event: &str, fields: serde_json::Value) {
    let mut obj = serde_json::Map::new();
    obj.insert("event".into(), event.into());
    if let serde_json::Value::Object(f) = fields {
        obj.extend(f);
    }
    crate::ledger_log!("ledger-rust: EVENT {}", serde_json::Value::Object(obj));
}

/// Where a head-file write failed.
#[derive(Debug)]
enum HeadWriteError {
    /// Before the rename: the old checkpoint is still the one on disk.
    BeforeRename(io::Error),
    /// The rename happened; only the directory fsync failed, so the new
    /// checkpoint is visible but may not survive a power loss (in which case
    /// the log is ahead of the checkpoint on restart, which open accepts).
    DirSync(io::Error),
}

/// Writes `head` to `<log>.head` atomically: temp file (O_EXCL, never through
/// a symlink, mode 0600), fsync, rename over the head file, directory fsync.
fn write_head(log: &Path, head: &HeadCheckpoint) -> Result<(), HeadWriteError> {
    let tmp = head_tmp_path_for(log);
    let before = |e| HeadWriteError::BeforeRename(e);
    // A temp file left by a crash is debris (this process holds the writer lock).
    match std::fs::remove_file(&tmp) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(before(e)),
    }
    let mut body = serde_json::to_string(head).map_err(|e| before(io::Error::other(e)))?;
    body.push('\n');
    {
        let mut f = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&tmp)
            .map_err(before)?;
        f.write_all(body.as_bytes()).and_then(|()| f.sync_all()).map_err(|e| {
            let _ = std::fs::remove_file(&tmp);
            before(e)
        })?;
    }
    std::fs::rename(&tmp, head_path_for(log)).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        before(e)
    })?;
    fsync_dir(log).map_err(HeadWriteError::DirSync)
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

/// Sweep F-6 (and AEGIS M1-M3): compares the verified log with the head
/// checkpoint. Ok(true) when the checkpoint must be (re)written, Ok(false)
/// when it already matches. Every refusal names the exact override value
/// that would accept the state it refuses, and nothing else accepts it.
///
/// | log vs checkpoint | result |
/// |---|---|
/// | equal | opens |
/// | exactly one entry ahead (crash between log fsync and checkpoint rename) | opens, checkpoint moved up |
/// | more than one ahead, behind, different entry at the head, log missing, checkpoint malformed | refused unless `reset` equals `reset_binding(checkpoint, log)` |
/// | no checkpoint, empty log (fresh install) | opens, checkpoint created, `ledger_created` logged |
/// | no checkpoint, non-empty log (pre-sweep-F binary, or a deleted head file) | refused unless `migrate` equals `log_binding(log)` |
fn check_against_head(
    path: &Path,
    log_existed: bool,
    ledger: &Ledger,
    head: HeadState,
    opts: &LedgerOpenOptions,
) -> Result<(bool, OpenReport, Provenance), PersistError> {
    let n = ledger.len() as u64;
    let log_head = HeadCheckpoint::of(ledger);
    let log_bind = log_binding(n, &log_head.head_hash);
    let head_file = head_path_for(path);
    let mut kept = Provenance::default();
    let (refusal, from): (String, String) = match head {
        HeadState::Missing if n == 0 => {
            log_startup_event(
                "ledger_created",
                serde_json::json!({
                    "log": path.display().to_string(),
                    "log_existed": log_existed,
                    "head": head_file.display().to_string(),
                    "entries": 0,
                }),
            );
            return Ok((true, OpenReport { created: true, ..OpenReport::default() }, Provenance::default()));
        }
        HeadState::Missing => {
            if opts.migrate.as_deref() == Some(log_bind.as_str()) {
                log_startup_event(
                    "legacy_log_migrated",
                    serde_json::json!({
                        "log": path.display().to_string(),
                        "entries": n,
                        "head_hash": log_head.head_hash,
                        "binding": log_bind,
                    }),
                );
                crate::ledger_log!(
                    "ledger-rust: WARNING — LEDGER_MIGRATE_LEGACY={log_bind}: creating the head checkpoint {} for \
                     {} ({n} verified entries). Deletion or rollback BEFORE this point cannot be detected. Unset \
                     LEDGER_MIGRATE_LEGACY now (it can never match this log again once an entry is appended).",
                    head_file.display(),
                    path.display()
                );
                let p = Provenance { migrated_from: Some(log_bind.clone()), reset_from: None };
                return Ok((true, OpenReport { migrate_used: true, ..OpenReport::default() }, p));
            }
            let given = match &opts.migrate {
                Some(v) => format!(" (LEDGER_MIGRATE_LEGACY={v:?} does not match this log)"),
                None => String::new(),
            };
            return Err(PersistError::Checkpoint(format!(
                "{} holds {n} verified entries but has NO head checkpoint ({}){given}: either it was written by a \
                 binary older than the checkpoint (sweep F-6), or the checkpoint was deleted — the two cannot be \
                 told apart, and deleting the checkpoint is how a rollback would hide. Refusing to start (fail \
                 closed). If this log is the one you expect (its head: entries={n}, head_hash={}), restart ONCE with \
                 LEDGER_MIGRATE_LEGACY={log_bind} to create the checkpoint from it, then unset it.",
                path.display(),
                head_file.display(),
                log_head.head_hash
            )));
        }
        HeadState::Bad { reason, digest16 } => (reason, format!("unreadable:{digest16}")),
        HeadState::Present(cp) => {
            kept = cp.provenance();
            let from = log_binding(cp.entries, &cp.head_hash);
            let reason = if !log_existed {
                format!(
                    "the log file {} is MISSING but the head checkpoint {} records {} entries (head hash {}): \
                     the log was deleted or moved",
                    path.display(),
                    head_file.display(),
                    cp.entries,
                    cp.head_hash
                )
            } else if n < cp.entries {
                format!(
                    "the log {} holds {n} entries but the head checkpoint records {} (head hash {}): acknowledged \
                     entries were removed (truncation or a restore of an older copy)",
                    path.display(),
                    cp.entries,
                    cp.head_hash
                )
            } else {
                let at = if cp.entries == 0 {
                    genesis_hash()
                } else {
                    ledger.entries()[(cp.entries - 1) as usize].hash().to_string()
                };
                if at != cp.head_hash {
                    format!(
                        "the log {} does not contain the checkpointed head: entry {:?} has hash {at}, the head \
                         checkpoint records {} (the log was rewritten or replaced)",
                        path.display(),
                        cp.head_seq,
                        cp.head_hash
                    )
                } else if n == cp.entries + 1 {
                    // AEGIS M1: the one crash window an append has (log fsynced,
                    // checkpoint not yet renamed) leaves exactly one entry.
                    log_startup_event(
                        "checkpoint_moved_up",
                        serde_json::json!({
                            "log": path.display().to_string(),
                            "from_entries": cp.entries,
                            "to_entries": n,
                            "head_hash": log_head.head_hash,
                        }),
                    );
                    return Ok((true, OpenReport { moved_up: true, ..OpenReport::default() }, kept));
                } else if n > cp.entries {
                    format!(
                        "the log {} holds {n} entries, {} more than the head checkpoint ({}): an interrupted append \
                         leaves at most ONE entry past the checkpoint, so entries were added outside this ledger \
                         (or the checkpoint was replaced by an older copy)",
                        path.display(),
                        n - cp.entries,
                        cp.entries
                    )
                } else {
                    return Ok((false, OpenReport::default(), kept));
                }
            };
            (reason, from)
        }
    };
    let accept = reset_binding(&from, &log_bind);
    match opts.reset.as_deref() {
        Some(v) if v == accept => {
            log_startup_event(
                "operator_reset",
                serde_json::json!({
                    "log": path.display().to_string(),
                    "from": from,
                    "to": log_bind,
                    "entries": n,
                    "head_hash": log_head.head_hash,
                    "reason": refusal,
                }),
            );
            crate::ledger_log!(
                "ledger-rust: WARNING — LEDGER_ALLOW_RESET={accept}: OPERATOR RESET of the head checkpoint. \
                 {refusal}. Accepting the verified log as it is now ({n} entries) and rewriting {}. Unset \
                 LEDGER_ALLOW_RESET now.",
                head_file.display()
            );
            let p = Provenance { migrated_from: kept.migrated_from, reset_from: Some(accept) };
            Ok((true, OpenReport { reset_used: true, ..OpenReport::default() }, p))
        }
        given => {
            let given = match given {
                Some(v) => format!(" LEDGER_ALLOW_RESET={v:?} does not match this state (it is bound to one exact \
                                   checkpoint and log head)."),
                None => String::new(),
            };
            Err(PersistError::Checkpoint(format!(
                "{refusal}.{given} Refusing to start (fail closed). Restore the log from backup; or, if you accept \
                 the log as it is now, restart ONCE with LEDGER_ALLOW_RESET={accept} (logged) to move the \
                 checkpoint to it, then unset it."
            )))
        }
    }
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
    ///
    /// Sweep F: takes the single-writer lock first (F-1) and checks the log
    /// against the head checkpoint (F-6); see the module docs.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self, PersistError> {
        Self::open_with(path, LedgerOpenOptions::default())
    }

    /// `open` with explicit options (the operator reset, sweep F-6).
    pub fn open_with<P: AsRef<Path>>(path: P, opts: LedgerOpenOptions) -> Result<Self, PersistError> {
        Self::open_with_sink(path, opts, |f| Box::new(f))
    }

    /// `open`, with the append handle wrapped by `wrap` (tests inject a
    /// failing writer through this; production passes the `File` through).
    pub(crate) fn open_with_sink<P: AsRef<Path>>(
        path: P,
        opts: LedgerOpenOptions,
        wrap: impl FnOnce(File) -> Box<dyn LogSink>,
    ) -> Result<Self, PersistError> {
        let path = path.as_ref().to_path_buf();
        if path.file_name().is_none() {
            return Err(PersistError::Io(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("ledger log path {} names no file", path.display()),
            )));
        }

        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }

        // F-1: the lock comes before anything is read.
        let lock = acquire_writer_lock(&path)?;

        let existed = path.exists();
        let mut bytes = Vec::new();
        if existed {
            File::open(&path)?.read_to_end(&mut bytes)?;
        }
        let head = read_head(&path);
        // Everything up to and including the last '\n' is complete lines;
        // anything after it is an unterminated (never acknowledged) tail.
        let complete_len = bytes.iter().rposition(|&b| b == b'\n').map(|p| p + 1).unwrap_or(0);

        let mut entries: Vec<LedgerEntry> = Vec::new();
        let mut event_index: HashMap<String, usize> = HashMap::new();
        let mut finding_index: HashMap<String, Vec<usize>> = HashMap::new();
        let mut read_index = ReadIndex::default();
        let mut line_no = 0;
        for (i, raw) in bytes[..complete_len].split_inclusive(|&b| b == b'\n').enumerate() {
            line_no = i + 1;
            let raw = &raw[..raw.len() - 1]; // strip the '\n'
            let line = std::str::from_utf8(raw).map_err(|e| PersistError::Corrupt {
                line: i + 1,
                reason: format!("line is not valid UTF-8: {e}"),
            })?;
            if line.trim().is_empty() {
                // F-13: the writer never writes a blank line; one in the log
                // was put there by something else.
                return Err(PersistError::Corrupt {
                    line: i + 1,
                    reason: "blank line (the ledger writer never writes one)".into(),
                });
            }
            let entry: LedgerEntry = serde_json::from_str(line).map_err(|e| PersistError::Corrupt {
                line: i + 1,
                reason: e.to_string(),
            })?;
            match &entry {
                LedgerEntry::Event(ev) => {
                    if event_index.insert(ev.event_id.clone(), entries.len()).is_some() {
                        return Err(PersistError::Corrupt {
                            line: i + 1,
                            reason: format!("duplicate event_id {:?} in ledger log", ev.event_id),
                        });
                    }
                }
                LedgerEntry::Finding(f) => finding_index.entry(f.finding_id.clone()).or_default().push(entries.len()),
            }
            read_index.add(entries.len(), &entry);
            entries.push(entry);
        }

        // F-13: a torn write is a strict prefix of `<json>\n`; no strict
        // prefix of a JSON object is valid JSON, and a real line never starts
        // with whitespace. So an unterminated tail that is blank, or that is
        // a complete JSON value, did not come from a crash mid-append.
        let torn = &bytes[complete_len..];
        if !torn.is_empty() {
            let tail_line = line_no + 1;
            if torn.iter().all(u8::is_ascii_whitespace) {
                return Err(PersistError::Corrupt {
                    line: tail_line,
                    reason: "unterminated blank final line (not a torn write: every entry starts with '{')".into(),
                });
            }
            if serde_json::from_slice::<serde_json::Value>(torn).is_ok() {
                return Err(PersistError::Corrupt {
                    line: tail_line,
                    reason: "the unterminated final line is a complete JSON value: an entry whose newline was \
                             removed after it was written (a crash mid-append leaves a strict prefix, which never \
                             parses). Refusing rather than discarding a possibly acknowledged entry; inspect it \
                             and restore the newline or move the line aside manually"
                        .into(),
                });
            }
        }

        let ledger = Ledger::from_entries(entries);
        ledger.verify_chain().map_err(PersistError::ChainInvalid)?;

        // F-6: every refusal happens before anything on disk is modified.
        let (write_checkpoint, report, provenance) = check_against_head(&path, existed, &ledger, head, &opts)?;
        // AEGIS N1: an override that is set but was not needed by this state
        // refuses the open (before anything on disk changes): it is a leftover
        // that a later restore of an old backup would silently re-arm.
        for (name, set, used) in [
            ("LEDGER_MIGRATE_LEGACY", &opts.migrate, report.migrate_used),
            ("LEDGER_ALLOW_RESET", &opts.reset, report.reset_used),
        ] {
            if let (Some(v), false) = (set, used) {
                return Err(PersistError::Checkpoint(format!(
                    "{name}={v} is set but this log does not need it (the log reaches its head checkpoint, or the \
                     value matches another state). An operator override is one-shot: refusing to start while it \
                     is set, so a leftover value can never accept a later rollback. Unset {name} and restart."
                )));
            }
        }

        // Only now — every complete line parsed and the whole chain verified —
        // is an unterminated tail treated as a torn, unacknowledged write.
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
        if write_checkpoint {
            match write_head(&path, &HeadCheckpoint::of(&ledger).with(&provenance)) {
                Ok(()) => {}
                Err(HeadWriteError::BeforeRename(e)) | Err(HeadWriteError::DirSync(e)) => {
                    return Err(PersistError::Io(e));
                }
            }
        }

        Ok(PersistentLedger {
            ledger,
            event_index,
            finding_index,
            read_index,
            log_bytes: complete_len as u64,
            file: wrap(file),
            path,
            torn_tail,
            report,
            provenance,
            poisoned: None,
            _lock: lock,
            #[cfg(test)]
            fail_next_head_write: false,
        })
    }

    /// What `open` did about the head checkpoint (AEGIS M1-M3).
    pub fn open_report(&self) -> OpenReport {
        self.report
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

    /// AEGIS L5/M4: clones the entries at positions `from..to` that `filter`
    /// selects, ascending, at most `max` (see `EntryFilter`).
    pub fn select(&self, filter: &EntryFilter, from: usize, to: usize, max: usize) -> Vec<LedgerEntry> {
        let mut work = 0;
        self.select_counted(filter, from, to, max, &mut work)
    }

    /// `select`, counting the index steps taken (the complexity guard).
    pub fn select_counted(&self, filter: &EntryFilter, from: usize, to: usize, max: usize, work: &mut usize) -> Vec<LedgerEntry> {
        let to = to.min(self.ledger.len());
        let entries = self.ledger.entries();
        self.read_index.select(filter, from, to, max, work).into_iter().map(|p| entries[p].clone()).collect()
    }

    /// AEGIS L4: the number of bytes of the log holding the acknowledged
    /// entries (what `verify_log_file` re-reads).
    pub fn log_bytes(&self) -> u64 {
        self.log_bytes
    }

    /// Clones entries `from..to` (clamped); see `Ledger::clone_range`.
    pub fn clone_range(&self, from: usize, to: usize) -> Vec<LedgerEntry> {
        self.ledger.clone_range(from, to)
    }

    /// The current head (equal to the durable checkpoint after every
    /// acknowledged append; sweep F-6, `GET /ledger/head`).
    pub fn head(&self) -> HeadCheckpoint {
        HeadCheckpoint::of(&self.ledger).with(&self.provenance)
    }

    pub fn verify_chain(&self) -> Result<(), LedgerError> {
        self.ledger.verify_chain()
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Writes one already-built entry to disk and fsyncs, then writes the
    /// head checkpoint (sweep F-6), and only THEN commits it to the
    /// in-memory ledger. On any disk error the in-memory ledger is left
    /// exactly as it was.
    ///
    /// If the write, flush or fsync fails at any point, the file is
    /// truncated back to its pre-append length (and fsync'd) before the
    /// error is returned, so no partial line is left for the next append to
    /// write after. The same rollback runs when the checkpoint cannot be
    /// written (before its rename): an append is acknowledged only once both
    /// are durable. If that rollback itself fails, the ledger is poisoned.
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
        let failed = match written {
            Err(write_err) => Some(write_err),
            Ok(()) => {
                let head = HeadCheckpoint {
                    entries: entry.seq() + 1,
                    head_seq: Some(entry.seq()),
                    head_hash: entry.hash().to_string(),
                    migrated_from: None,
                    reset_from: None,
                }
                .with(&self.provenance);
                match self.write_head_checked(&head) {
                    Ok(()) => None,
                    Err(HeadWriteError::BeforeRename(e)) => Some(io::Error::new(
                        e.kind(),
                        format!("head checkpoint {} could not be written: {e}", head_path_for(&self.path).display()),
                    )),
                    Err(HeadWriteError::DirSync(e)) => {
                        // The checkpoint is renamed into place and the log line is
                        // fsynced: the entry is recorded. Only the directory entry's
                        // durability is in doubt; after a power loss the log would be
                        // ahead of the checkpoint, which open accepts.
                        crate::ledger_log!(
                            "ledger-rust: WARNING — directory fsync after writing {} failed ({e}); the entry is \
                             recorded, the checkpoint may lag after a power loss",
                            head_path_for(&self.path).display()
                        );
                        None
                    }
                }
            }
        };
        if let Some(write_err) = failed {
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

        let pos = self.ledger.len();
        self.read_index.add(pos, &entry);
        self.log_bytes = pre_len + line.len() as u64;
        self.ledger.push_entry(entry);
        Ok(self.ledger.entries().last().expect("just pushed"))
    }

    fn write_head_checked(&mut self, head: &HeadCheckpoint) -> Result<(), HeadWriteError> {
        #[cfg(test)]
        if std::mem::take(&mut self.fail_next_head_write) {
            return Err(HeadWriteError::BeforeRename(io::Error::other("injected: head checkpoint write failed")));
        }
        write_head(&self.path, head)
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
    ///
    /// Not idempotent (unchanged): the same `finding_id` may be appended any
    /// number of times. See `append_finding_idempotent` for the opt-in.
    pub fn append(&mut self, record: LedgerRecordInput) -> Result<&LedgerEntry, PersistError> {
        record.validate().map_err(PersistError::Invalid)?;
        let finding_id = record.finding_id.clone();
        let pos = self.ledger.len();
        let entry = self.ledger.build_entry(record);
        self.persist_then_push(entry)?;
        self.finding_index.entry(finding_id).or_default().push(pos);
        Ok(&self.ledger.entries()[pos])
    }

    /// Sweep F-7: the opt-in, idempotent finding append, with `event_id`
    /// semantics on `finding_id`. If no entry holds this finding_id it is
    /// appended (`Created`); if one holds it with exactly this content,
    /// nothing is written (`Existing`, the first such entry); otherwise
    /// nothing is written (`Conflict`, the first entry with this id).
    pub fn append_finding_idempotent(&mut self, record: LedgerRecordInput) -> Result<AppendOutcome<'_>, PersistError> {
        record.validate().map_err(PersistError::Invalid)?;
        if let Some(positions) = self.finding_index.get(&record.finding_id) {
            let entries = self.ledger.entries();
            let same = positions
                .iter()
                .find(|&&p| entries[p].as_finding().is_some_and(|f| f.same_content_as(&record)));
            return Ok(match same {
                Some(&p) => AppendOutcome::Existing(&entries[p]),
                None => AppendOutcome::Conflict(&entries[positions[0]]),
            });
        }
        let pos = self.ledger.len();
        self.append(record)?;
        Ok(AppendOutcome::Created(&self.ledger.entries()[pos]))
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

    static SCRATCH_DIR: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();

    extern "C" fn remove_scratch_dir() {
        if let Some(d) = SCRATCH_DIR.get() {
            let _ = std::fs::remove_dir(d); // only when empty: a file left in it is a leak the hygiene check reports
        }
    }

    fn scratch_path(label: &str) -> PathBuf {
        // the counter keeps two paths made in the same instant apart (fix wave 26b: macOS's clock resolves only
        // microseconds; tests run in parallel threads)
        static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        // fix wave 26b (scout C C1-1): inside one per-process directory, not loose in the temp dir — made on first use,
        // removed at exit when empty, so a killed run leaves at most that one entry
        let dir = SCRATCH_DIR.get_or_init(|| {
            let d = std::env::temp_dir().join(format!("zbm-ledger-unit-{}_{nanos}", std::process::id()));
            std::fs::create_dir_all(&d).expect("scratch dir");
            // SAFETY: registers a plain `extern "C" fn()` with the C library's exit handlers (std::process::exit, which
            // the test harness ends with, runs them); it only calls remove_dir on a path set once.
            unsafe {
                libc::atexit(remove_scratch_dir);
            }
            d
        });
        dir.join(format!("zbm_ledger_test_{label}_{}_{nanos}_{n}.jsonl", std::process::id()))
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
        let pl = PersistentLedger::open_with_sink(path, LedgerOpenOptions::default(), move |inner| {
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
        // (A whitespace-only tail was in this list until sweep F-13: no real
        // line starts with whitespace, so it is not a torn write and is now
        // refused — see `blank_lines_anywhere_refuse_to_open`.)
        let tails: [&[u8]; 4] = [
            b"{\"kind\":\"event\",\"seq\":2,\"event_id\":\"e",
            b"{",
            b"{\"kind\":\"finding\",\"seq\":2,\"finding_id\":\"f-2\",\"amount_usd\":\"1.0",
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

    /// Sweep F-13 (probe P4): a complete, valid entry missing only its
    /// trailing newline used to be treated like a torn write — moved to a side
    /// file and truncated away, so removing ONE byte from the log silently
    /// deleted an acknowledged entry and the ledger verified "valid". A crash
    /// mid-append leaves a strict prefix of the line, which never parses; a
    /// tail that parses is refused and the file is left untouched.
    #[test]
    fn unterminated_but_parseable_final_line_is_refused_not_discarded() {
        let path = scratch_path("unterminated_valid");
        let _cleanup = ScratchFile(path.clone());
        {
            let mut pl = PersistentLedger::open(&path).unwrap();
            pl.append(sample_record("f-0", "10.00")).unwrap();
            pl.append(sample_record("f-1", "11.00")).unwrap();
        }
        let full = std::fs::read(&path).unwrap();
        let cut = &full[..full.len() - 1];
        std::fs::write(&path, cut).unwrap();
        match PersistentLedger::open(&path) {
            Err(PersistError::Corrupt { line: 2, reason }) => assert!(reason.contains("complete JSON value"), "{reason}"),
            other => panic!("expected Corrupt at line 2, got {other:?}"),
        }
        assert_eq!(std::fs::read(&path).unwrap(), cut, "file untouched");
        assert_eq!(torn_side_files(&path), 0, "nothing moved to a side file");
        // Even an operator reset does not discard it (it is corruption, not a checkpoint question).
        assert!(matches!(
            PersistentLedger::open_with(&path, LedgerOpenOptions { reset: Some("2:0000000000000000/2:0000000000000000".into()), ..LedgerOpenOptions::default() }),
            Err(PersistError::Corrupt { line: 2, .. })
        ));
        // Restoring the newline restores the ledger.
        std::fs::write(&path, &full).unwrap();
        assert_eq!(PersistentLedger::open(&path).unwrap().len(), 2);
    }

    /// Number of `<log>.torn-*` side files next to `path`.
    fn torn_side_files(path: &Path) -> usize {
        let prefix = format!("{}.torn-", path.file_name().unwrap().to_str().unwrap());
        std::fs::read_dir(path.parent().unwrap())
            .unwrap()
            .filter(|e| e.as_ref().unwrap().file_name().to_str().unwrap().starts_with(&prefix))
            .count()
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
        let mut pl = open_migrated(&path).expect("every log the old binary wrote must load");
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
        let (fixture, _c) = fixture_copy("aegis_forged_resplit.jsonl");
        let r = PersistentLedger::open(&fixture);
        match r {
            Err(PersistError::Corrupt { line: 1, reason }) => assert!(reason.contains("ambiguous"), "{reason}"),
            other => panic!("expected Corrupt at line 1, got {other:?}"),
        }
    }

    #[test]
    fn ambiguous_legacy_entries_from_old_binary_refuse_to_open() {
        for name in ["legacy_ambiguous_pipe.jsonl", "legacy_ambiguous_null.jsonl"] {
            let (fixture, _c) = fixture_copy(name);
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
            let mut pl = open_migrated(&path).expect("legacy file must open and verify");
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
        let (fixture, _c) = fixture_copy("aegis_unknown_field_injection.jsonl");
        assert!(std::fs::read_to_string(&fixture).unwrap().contains("\"approved_by\":\"andre\""));
        match PersistentLedger::open(&fixture) {
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
            let pl = open_migrated(&path).unwrap_or_else(|e| panic!("{name}: {e}"));
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

    // --- sweep F (Oct 6 2026) ----------------------------------------------------

    /// Any reset value (for refusals that come before the checkpoint check).
    fn reset() -> LedgerOpenOptions {
        LedgerOpenOptions { reset: Some("0:0000000000000000/0:0000000000000000".into()), ..LedgerOpenOptions::default() }
    }

    /// The `NAME=<value>` an operator is told to use, taken from a refusal.
    fn advised(reason: &str, name: &str) -> String {
        let at = reason.find(&format!("{name}=")).unwrap_or_else(|| panic!("no {name} advice in: {reason}"));
        reason[at + name.len() + 1..].split_whitespace().next().unwrap().to_string()
    }

    /// AEGIS M3: the reset is accepted only with the value the refusal printed.
    fn reset_as_advised(path: &Path) -> PersistentLedger {
        let reason = match PersistentLedger::open(path) {
            Err(PersistError::Checkpoint(r)) => r,
            other => panic!("expected a Checkpoint refusal, got {other:?}"),
        };
        let v = advised(&reason, "LEDGER_ALLOW_RESET");
        assert!(is_reset_binding(&v), "{v}");
        PersistentLedger::open_with(path, LedgerOpenOptions { reset: Some(v), ..LedgerOpenOptions::default() })
            .expect("the advised reset value opens")
    }

    fn head_on_disk(path: &Path) -> HeadCheckpoint {
        serde_json::from_str(std::fs::read_to_string(head_path_for(path)).unwrap().trim_end()).unwrap()
    }

    /// F-1: the second opener of a log is refused BEFORE it reads anything —
    /// here, before it could "recover" a torn tail the first one is not
    /// responsible for — and can open once the first is gone.
    #[test]
    fn a_second_opener_is_refused_while_the_first_holds_the_writer_lock() {
        let path = scratch_path("single_writer");
        let _cleanup = ScratchFile(path.clone());
        let mut first = PersistentLedger::open(&path).unwrap();
        first.append(sample_record("f-0", "1.00")).unwrap();
        let mut f = OpenOptions::new().append(true).open(&path).unwrap();
        f.write_all(b"{\"kind\":\"fin").unwrap();
        let before = std::fs::read(&path).unwrap();
        match PersistentLedger::open(&path) {
            Err(PersistError::Locked(reason)) => assert!(reason.contains(".lock"), "{reason}"),
            other => panic!("expected Locked, got {other:?}"),
        }
        assert!(matches!(PersistentLedger::open_with(&path, reset()), Err(PersistError::Locked(_))), "reset never bypasses the lock");
        assert_eq!(std::fs::read(&path).unwrap(), before, "the refused opener touched nothing");
        assert_eq!(torn_side_files(&path), 0);
        drop(first);
        let second = PersistentLedger::open(&path).expect("free once the first writer is gone");
        let _side = ScratchFile(second.torn_tail_recovery().unwrap().preserved_at.clone());
        assert_eq!(second.len(), 1);
    }

    /// F-6: the checkpoint is on disk after open and after every append.
    #[test]
    fn the_head_checkpoint_follows_every_append() {
        let path = scratch_path("head_follows");
        let _cleanup = ScratchFile(path.clone());
        let mut pl = PersistentLedger::open(&path).unwrap();
        assert_eq!(head_on_disk(&path), HeadCheckpoint { entries: 0, head_seq: None, head_hash: crate::genesis_hash(), migrated_from: None, reset_from: None });
        pl.append(sample_record("f-0", "1.00")).unwrap();
        pl.append_event(sample_event("onb-1", "s")).unwrap();
        let h = head_on_disk(&path);
        assert_eq!(h, pl.head());
        assert_eq!((h.entries, h.head_seq), (2, Some(1)));
        assert_eq!(h.head_hash, pl.entries()[1].hash());
        assert!(!head_tmp_path_for(&path).exists(), "no temp file left");
        #[allow(clippy::unnecessary_cast)]
        let mode = std::fs::metadata(head_path_for(&path)).map(|m| std::os::unix::fs::PermissionsExt::mode(&m.permissions()) & 0o777).unwrap();
        assert_eq!(mode, 0o600);
    }

    /// F-6 (probe P2): deleting the whole log used to start a fresh, "valid"
    /// ledger. Now refused; the operator reset accepts it and is the only way.
    #[test]
    fn a_deleted_log_refuses_to_open_unless_the_operator_resets() {
        let path = scratch_path("deleted");
        let _cleanup = ScratchFile(path.clone());
        {
            let mut pl = PersistentLedger::open(&path).unwrap();
            for i in 0..3 {
                pl.append(sample_record(&format!("f-{i}"), "1.00")).unwrap();
            }
        }
        std::fs::remove_file(&path).unwrap();
        match PersistentLedger::open(&path) {
            Err(PersistError::Checkpoint(reason)) => assert!(reason.contains("MISSING") && reason.contains("LEDGER_ALLOW_RESET"), "{reason}"),
            other => panic!("expected Checkpoint refusal, got {other:?}"),
        }
        assert!(!path.exists(), "the refusal did not create a fresh log");
        assert!(matches!(PersistentLedger::open_with(&path, reset()), Err(PersistError::Checkpoint(_))), "an unbound reset value does nothing");
        assert!(!path.exists());
        let pl = reset_as_advised(&path);
        assert!(pl.open_report().reset_used);
        assert!(pl.is_empty());
        assert_eq!(head_on_disk(&path).entries, 0);
        drop(pl);
        assert!(PersistentLedger::open(&path).is_ok(), "after the reset the checkpoint matches again");
    }

    /// F-6 (probe P3): dropping whole acknowledged lines from the end used to
    /// verify; and a log replaced by a different, internally valid chain of
    /// the same length is refused too.
    #[test]
    fn a_truncated_or_replaced_log_refuses_to_open() {
        let path = scratch_path("truncated");
        let _cleanup = ScratchFile(path.clone());
        {
            let mut pl = PersistentLedger::open(&path).unwrap();
            for i in 0..3 {
                pl.append(sample_record(&format!("f-{i}"), "1.00")).unwrap();
            }
        }
        let full = std::fs::read_to_string(&path).unwrap();
        let first_line = full.lines().next().unwrap().to_string() + "\n";
        std::fs::write(&path, &first_line).unwrap();
        match PersistentLedger::open(&path) {
            Err(PersistError::Checkpoint(reason)) => assert!(reason.contains("holds 1 entries") && reason.contains("records 3"), "{reason}"),
            other => panic!("expected Checkpoint refusal, got {other:?}"),
        }
        assert_eq!(std::fs::read_to_string(&path).unwrap(), first_line, "file untouched");

        // A different chain with the same number of entries.
        let other = scratch_path("truncated_other");
        let _c2 = ScratchFile(other.clone());
        {
            let mut pl = PersistentLedger::open(&other).unwrap();
            for i in 0..3 {
                pl.append(sample_record(&format!("g-{i}"), "2.00")).unwrap();
            }
        }
        std::fs::copy(&other, &path).unwrap();
        match PersistentLedger::open(&path) {
            Err(PersistError::Checkpoint(reason)) => assert!(reason.contains("does not contain the checkpointed head"), "{reason}"),
            other => panic!("expected Checkpoint refusal, got {other:?}"),
        }
        let pl = reset_as_advised(&path);
        assert_eq!(pl.head(), head_on_disk(&path));
        assert_eq!(pl.entries()[0].as_finding().unwrap().finding_id, "g-0");
    }

    /// F-6 / AEGIS M1: a crash between the log fsync and the checkpoint
    /// rename leaves the log exactly ONE entry ahead; that is accepted and
    /// the checkpoint moves up. Two or more ahead is not a crash window (an
    /// append is acknowledged only once its checkpoint is durable, and the
    /// next append writes its own): refused, and only the bound reset accepts it.
    #[test]
    fn only_a_log_exactly_one_entry_ahead_of_its_checkpoint_opens() {
        let path = scratch_path("ahead");
        let _cleanup = ScratchFile(path.clone());
        let mut heads = Vec::new();
        {
            let mut pl = PersistentLedger::open(&path).unwrap();
            for i in 0..3 {
                heads.push(std::fs::read(head_path_for(&path)).unwrap());
                pl.append(sample_record(&format!("f-{i}"), "1.00")).unwrap();
            }
        }
        // heads[k] is the checkpoint at k entries; the log holds 3.
        std::fs::write(head_path_for(&path), &heads[2]).unwrap();
        let pl = PersistentLedger::open(&path).expect("one entry ahead is the crash window, not tampering");
        assert!(pl.open_report().moved_up);
        assert_eq!(head_on_disk(&path), pl.head());
        assert_eq!(pl.head().entries, 3);
        drop(pl);

        for (k, label) in [(1usize, "two ahead"), (0, "three ahead (empty checkpoint)")] {
            std::fs::write(head_path_for(&path), &heads[k]).unwrap();
            let log_before = std::fs::read(&path).unwrap();
            match PersistentLedger::open(&path) {
                Err(PersistError::Checkpoint(r)) => {
                    assert!(r.contains("at most ONE entry past the checkpoint"), "{label}: {r}")
                }
                other => panic!("{label}: expected a refusal, got {other:?}"),
            }
            assert_eq!(std::fs::read(head_path_for(&path)).unwrap(), heads[k], "{label}: checkpoint untouched");
            assert_eq!(std::fs::read(&path).unwrap(), log_before, "{label}: log untouched");
        }
        let pl = reset_as_advised(&path);
        assert_eq!(pl.head().entries, 3);
    }

    /// AEGIS M2: a non-empty log with no checkpoint (a pre-sweep-F log, or a
    /// deleted head file — indistinguishable) used to open and silently get a
    /// new checkpoint. Now refused unless the one-shot migrate value bound to
    /// this log's head is given; a fresh, empty ledger is created and says so.
    #[test]
    fn a_log_without_a_checkpoint_needs_the_bound_one_shot_migrate() {
        let path = scratch_path("no_head");
        let _cleanup = ScratchFile(path.clone());
        {
            let pl = PersistentLedger::open(&path).unwrap();
            assert!(pl.open_report().created, "a new ledger is reported");
        }
        {
            let mut pl = PersistentLedger::open(&path).unwrap();
            assert!(!pl.open_report().created, "an existing empty ledger with its checkpoint is not new");
            pl.append(sample_record("f-0", "1.00")).unwrap();
            pl.append(sample_record("f-1", "1.00")).unwrap();
        }
        std::fs::remove_file(head_path_for(&path)).unwrap();
        let reason = match PersistentLedger::open(&path) {
            Err(PersistError::Checkpoint(r)) => r,
            other => panic!("expected a refusal, got {other:?}"),
        };
        assert!(reason.contains("NO head checkpoint"), "{reason}");
        assert!(!head_path_for(&path).exists(), "the refusal wrote no checkpoint");
        let v = advised(&reason, "LEDGER_MIGRATE_LEGACY");
        // A value for another state (a stale one) does nothing.
        let stale = LedgerOpenOptions { migrate: Some("1:0000000000000000".into()), ..LedgerOpenOptions::default() };
        assert!(matches!(PersistentLedger::open_with(&path, stale), Err(PersistError::Checkpoint(_))));
        let opts = LedgerOpenOptions { migrate: Some(v.clone()), ..LedgerOpenOptions::default() };
        let pl = PersistentLedger::open_with(&path, opts.clone()).unwrap();
        assert!(pl.open_report().migrate_used);
        assert_eq!(head_on_disk(&path), pl.head());
        assert_eq!(pl.head().migrated_from.as_deref(), Some(v.as_str()), "AEGIS N1: the migration is recorded");
        drop(pl);
        // AEGIS N1: left set, it refuses every open (it is not needed now), so
        // nothing can be appended while it is armed.
        match PersistentLedger::open_with(&path, opts.clone()) {
            Err(PersistError::Checkpoint(r)) => assert!(r.contains("does not need it"), "{r}"),
            other => panic!("a leftover migrate value must refuse the open: {other:?}"),
        }
        let mut pl = PersistentLedger::open(&path).unwrap();
        pl.append(sample_record("f-2", "1.00")).unwrap();
        assert_eq!(head_on_disk(&path).migrated_from.as_deref(), Some(v.as_str()), "kept by every append");
        drop(pl);
        std::fs::remove_file(head_path_for(&path)).unwrap();
        assert!(matches!(PersistentLedger::open_with(&path, opts), Err(PersistError::Checkpoint(_))));
    }

    /// AEGIS N1 repro: migrate a 3-entry log (keeping a backup of the
    /// directory as it was), append 7, restore the backup. Before: with the
    /// migrate value still set, the restored 3-entry log started and the 7
    /// acknowledged entries were gone. Now the value cannot stay set across
    /// the appends (a start with it set and not needed is refused), and the
    /// restored backup is refused again (no checkpoint).
    #[test]
    fn a_restored_pre_migration_backup_is_refused_again() {
        let path = scratch_path("n1_migrate");
        let _cleanup = ScratchFile(path.clone());
        {
            let mut pl = PersistentLedger::open(&path).unwrap();
            for i in 0..3 {
                pl.append(sample_record(&format!("f-{i}"), "1.00")).unwrap();
            }
        }
        std::fs::remove_file(head_path_for(&path)).unwrap();
        let backup = std::fs::read(&path).unwrap();
        let v = match PersistentLedger::open(&path) {
            Err(PersistError::Checkpoint(r)) => advised(&r, "LEDGER_MIGRATE_LEGACY"),
            other => panic!("{other:?}"),
        };
        let opts = LedgerOpenOptions { migrate: Some(v), ..LedgerOpenOptions::default() };
        drop(PersistentLedger::open_with(&path, opts.clone()).unwrap());
        // The value left set: the next open (where the 7 appends would happen) is refused.
        assert!(matches!(PersistentLedger::open_with(&path, opts.clone()), Err(PersistError::Checkpoint(_))));
        {
            let mut pl = PersistentLedger::open(&path).unwrap();
            for i in 3..10 {
                pl.append(sample_record(&format!("f-{i}"), "1.00")).unwrap();
            }
        }
        // Restore the pre-migration directory (log without its head file).
        std::fs::write(&path, &backup).unwrap();
        std::fs::remove_file(head_path_for(&path)).unwrap();
        match PersistentLedger::open(&path) {
            Err(PersistError::Checkpoint(r)) => assert!(r.contains("NO head checkpoint"), "{r}"),
            other => panic!("the restored backup must be refused: {other:?}"),
        }
        assert!(!head_path_for(&path).exists(), "the refusal wrote nothing");
    }

    /// AEGIS M3: a reset value is bound to the exact (checkpoint, log head)
    /// pair it was printed for; a stale value left armed accepts nothing else.
    #[test]
    fn a_reset_value_accepts_only_the_state_it_was_printed_for() {
        let path = scratch_path("reset_bound");
        let _cleanup = ScratchFile(path.clone());
        {
            let mut pl = PersistentLedger::open(&path).unwrap();
            for i in 0..3 {
                pl.append(sample_record(&format!("f-{i}"), "1.00")).unwrap();
            }
        }
        let full = std::fs::read_to_string(&path).unwrap();
        let lines: Vec<&str> = full.lines().collect();
        std::fs::write(&path, format!("{}\n", lines[0])).unwrap();
        let reason = match PersistentLedger::open(&path) {
            Err(PersistError::Checkpoint(r)) => r,
            other => panic!("{other:?}"),
        };
        let v1 = advised(&reason, "LEDGER_ALLOW_RESET");
        // A different truncation: v1 does not accept it.
        std::fs::write(&path, format!("{}\n{}\n", lines[0], lines[1])).unwrap();
        let opts = LedgerOpenOptions { reset: Some(v1.clone()), ..LedgerOpenOptions::default() };
        match PersistentLedger::open_with(&path, opts.clone()) {
            Err(PersistError::Checkpoint(r)) => assert!(r.contains("does not match this state"), "{r}"),
            other => panic!("a stale reset value must not accept another state: {other:?}"),
        }
        // The state it was printed for: accepted, once, and recorded.
        let refused_head = std::fs::read(head_path_for(&path)).unwrap();
        std::fs::write(&path, format!("{}\n", lines[0])).unwrap();
        let pl = PersistentLedger::open_with(&path, opts.clone()).unwrap();
        assert!(pl.open_report().reset_used);
        assert_eq!(pl.len(), 1);
        assert_eq!(head_on_disk(&path).reset_from.as_deref(), Some(v1.as_str()));
        drop(pl);
        // AEGIS N1: left set afterwards, it refuses the open (not needed).
        match PersistentLedger::open_with(&path, opts.clone()) {
            Err(PersistError::Checkpoint(r)) => assert!(r.contains("does not need it"), "{r}"),
            other => panic!("a leftover reset value must refuse the open: {other:?}"),
        }
        let mut pl = PersistentLedger::open(&path).unwrap();
        pl.append(sample_record("f-9", "1.00")).unwrap();
        drop(pl);
        // AEGIS N1 (reset variant): restoring EXACTLY the earlier refused state
        // (the 3-entry checkpoint and the 1-entry log) is refused without the
        // value; the value would have to be set again, by hand.
        let reset_head = std::fs::read(head_path_for(&path)).unwrap();
        assert!(String::from_utf8_lossy(&reset_head).contains("reset_from"));
        std::fs::write(&path, format!("{}\n", lines[0])).unwrap();
        std::fs::write(head_path_for(&path), &refused_head).unwrap();
        match PersistentLedger::open(&path) {
            Err(PersistError::Checkpoint(r)) => assert!(r.contains("holds 1 entries but the head checkpoint records 3"), "{r}"),
            other => panic!("the restored refused state must be refused again: {other:?}"),
        }
    }

    #[test]
    fn an_unreadable_checkpoint_refuses_to_open_unless_the_operator_resets() {
        let path = scratch_path("bad_head");
        let _cleanup = ScratchFile(path.clone());
        {
            let mut pl = PersistentLedger::open(&path).unwrap();
            pl.append(sample_record("f-0", "1.00")).unwrap();
        }
        for bad in ["", "{", "{\"entries\":1,\"head_seq\":5,\"head_hash\":\"x\"}", "{\"entries\":1,\"head_seq\":0,\"head_hash\":\"x\",\"extra\":1}"] {
            std::fs::write(head_path_for(&path), bad).unwrap();
            assert!(matches!(PersistentLedger::open(&path), Err(PersistError::Checkpoint(_))), "{bad:?}");
        }
        let pl = reset_as_advised(&path);
        assert_eq!(head_on_disk(&path), pl.head());
    }

    /// F-6: an append is acknowledged only once the checkpoint is durable too.
    /// A checkpoint write that fails rolls the log line back, so memory, log
    /// and checkpoint all stay at the previous entry.
    #[test]
    fn a_failed_checkpoint_write_rolls_the_append_back() {
        let path = scratch_path("head_fail");
        let _cleanup = ScratchFile(path.clone());
        let mut pl = PersistentLedger::open(&path).unwrap();
        pl.append(sample_record("f-0", "1.00")).unwrap();
        let log_before = std::fs::read(&path).unwrap();
        let head_before = head_on_disk(&path);
        pl.fail_next_head_write = true;
        assert!(matches!(pl.append(sample_record("f-1", "1.00")), Err(PersistError::Io(_))));
        pl.fail_next_head_write = true;
        assert!(matches!(pl.append_event(sample_event("onb-1", "s")), Err(PersistError::Io(_))));
        assert_eq!(pl.len(), 1);
        assert!(pl.event_index.is_empty());
        assert_eq!(pl.finding_index.get("f-1"), None);
        assert_eq!(std::fs::read(&path).unwrap(), log_before);
        assert_eq!(head_on_disk(&path), head_before);
        pl.append(sample_record("f-1", "1.00")).unwrap();
        drop(pl);
        assert_eq!(PersistentLedger::open(&path).unwrap().len(), 2);
    }

    /// F-13: the old loader skipped blank lines anywhere; the writer never
    /// writes one. Each case is refused and the file left untouched.
    #[test]
    fn blank_lines_anywhere_refuse_to_open() {
        let (src, good) = log_with_tail("blank_src", b"");
        let _c = ScratchFile(src);
        let text = String::from_utf8(good).unwrap();
        let l: Vec<&str> = text.lines().collect();
        let cases: Vec<(&str, String, usize)> = vec![
            ("leading empty line", format!("\n{}\n{}\n", l[0], l[1]), 1),
            ("empty line mid-file", format!("{}\n\n{}\n", l[0], l[1]), 2),
            ("whitespace line mid-file", format!("{}\n \t \n{}\n", l[0], l[1]), 2),
            ("trailing empty line", format!("{}\n{}\n\n", l[0], l[1]), 3),
            ("unterminated blank tail", format!("{}\n{}\n   ", l[0], l[1]), 3),
            ("CR-only line", format!("{}\n\r\n{}\n", l[0], l[1]), 2),
        ];
        for (name, content, bad_line) in cases {
            let p = scratch_path("blank");
            let _c = ScratchFile(p.clone());
            std::fs::write(&p, &content).unwrap();
            match PersistentLedger::open(&p) {
                Err(PersistError::Corrupt { line, reason }) => {
                    assert_eq!(line, bad_line, "{name}");
                    assert!(reason.contains("blank"), "{name}: {reason}");
                }
                other => panic!("{name}: expected Corrupt, got {other:?}"),
            }
            assert_eq!(std::fs::read_to_string(&p).unwrap(), content, "{name}: file untouched");
            assert_eq!(torn_side_files(&p), 0, "{name}");
        }
    }

    /// F-7 (probe P5: 60 posts of 5 finding_ids made 60 entries): the opt-in
    /// finding_id index has event_id semantics, survives a restart, and leaves
    /// the plain append unchanged.
    #[test]
    fn idempotent_finding_append_has_event_id_semantics() {
        let path = scratch_path("finding_idem");
        let _cleanup = ScratchFile(path.clone());
        let first_hash;
        {
            let mut pl = PersistentLedger::open(&path).unwrap();
            match pl.append_finding_idempotent(sample_record("f-0", "1.00")).unwrap() {
                AppendOutcome::Created(e) => first_hash = e.hash().to_string(),
                other => panic!("expected Created, got {other:?}"),
            }
            match pl.append_finding_idempotent(sample_record("f-0", "1.00")).unwrap() {
                AppendOutcome::Existing(e) => assert_eq!(e.hash(), first_hash),
                other => panic!("expected Existing, got {other:?}"),
            }
            assert_eq!(pl.len(), 1);
        }
        let mut pl = PersistentLedger::open(&path).unwrap();
        match pl.append_finding_idempotent(sample_record("f-0", "1.00")).unwrap() {
            AppendOutcome::Existing(e) => assert_eq!(e.hash(), first_hash, "index rebuilt on open"),
            other => panic!("expected Existing, got {other:?}"),
        }
        let mut changed = sample_record("f-0", "1.00");
        changed.decision_confidence = None;
        assert!(matches!(pl.append_finding_idempotent(changed).unwrap(), AppendOutcome::Conflict(_)));
        assert!(matches!(pl.append_finding_idempotent(sample_record("f-0", "2.00")).unwrap(), AppendOutcome::Conflict(_)));
        assert_eq!(pl.len(), 1, "neither the retry nor the conflict wrote");
        // The plain append is unchanged: duplicates are still appended...
        pl.append(sample_record("f-0", "2.00")).unwrap();
        assert_eq!(pl.len(), 2);
        // ...and once the log holds the id with two contents, either content is "Existing".
        match pl.append_finding_idempotent(sample_record("f-0", "2.00")).unwrap() {
            AppendOutcome::Existing(e) => assert_eq!(e.seq(), 1),
            other => panic!("expected Existing, got {other:?}"),
        }
        assert!(matches!(pl.append_finding_idempotent(sample_record("f-0", "3.00")).unwrap(), AppendOutcome::Conflict(e) if e.seq() == 0));
        assert_eq!(std::fs::read_to_string(&path).unwrap().lines().count(), 2);
    }

    /// F-7: a legacy (numeric amount, no kind) finding compares by its
    /// canonical money string.
    #[test]
    fn idempotent_finding_append_matches_legacy_entries() {
        let (path, _c) = fixture_copy("legacy_ledger_v1.jsonl");
        let mut pl = open_migrated(&path).unwrap();
        let f = pl.entries()[0].as_finding().unwrap().clone();
        let same = LedgerRecordInput {
            finding_id: f.finding_id.clone(),
            agent_id: f.agent_id.clone(),
            entity_id: f.entity_id.clone(),
            leak_category: f.leak_category.clone(),
            amount_usd: f.amount_usd.clone(),
            value_classification: f.value_classification.clone(),
            decision_confidence: f.decision_confidence.clone(),
        };
        assert!(matches!(pl.append_finding_idempotent(same).unwrap(), AppendOutcome::Existing(e) if e.seq() == 0));
        assert_eq!(pl.len(), 11);
    }

    /// AEGIS L5: a filtered page is answered from the per-department /
    /// per-event-type index: the work is O(lists + page), not a scan of the
    /// entries after `after_seq` (20 000 entries, the rare department last:
    /// a scan would take ~20 000 steps). And the index selects exactly what
    /// a naive filter over every entry selects, for every filter shape.
    #[test]
    fn filtered_pages_come_from_the_index_and_match_a_naive_scan() {
        let path = scratch_path("index");
        let _cleanup = ScratchFile(path.clone());
        {
            let mut l = Ledger::new();
            let mut out = io::BufWriter::new(File::create(&path).unwrap());
            for i in 0..20_000usize {
                let (dept, et) = match i % 1000 {
                    999 => ("rare", "ping"),
                    n if n % 7 == 0 => ("sales", "call"),
                    n if n % 3 == 0 => ("finance", "payout"),
                    _ => ("sales", "note"),
                };
                if i % 11 == 0 {
                    l.append(sample_record(&format!("f-{i}"), "1.00"));
                } else {
                    let mut e = sample_event(&format!("e-{i}"), "s");
                    e.department = dept.into();
                    e.event_type = et.into();
                    l.append_event(e);
                }
                writeln!(out, "{}", serde_json::to_string(&l.entries()[i]).unwrap()).unwrap();
            }
            out.flush().unwrap();
            drop(out);
            write_head(&path, &HeadCheckpoint::of(&l)).unwrap();
        }
        let pl = PersistentLedger::open(&path).unwrap();
        assert_eq!(pl.len(), 20_000);

        let f = |d: Option<&str>, t: Option<&str>, scope: Option<&[&str]>| EntryFilter {
            department: d.map(str::to_string),
            event_type: t.map(str::to_string),
            scope: scope.map(|s| s.iter().map(|x| x.to_string()).collect()),
        };
        // The guard: the rare department's 20 entries, from the start of the log.
        let mut work = 0;
        let page = pl.select_counted(&f(Some("rare"), None, None), 0, pl.len(), 10, &mut work);
        assert_eq!(page.len(), 10);
        assert!(work <= 16, "a filtered page took {work} steps");
        let mut work = 0;
        let page = pl.select_counted(&f(None, Some("ping"), Some(&["rare", "finance"])), 15_000, pl.len(), 100, &mut work);
        assert_eq!(page.iter().map(|e| e.seq()).collect::<Vec<_>>(), [15_999, 16_999, 17_999, 18_999, 19_999]);
        assert!(work <= 12, "a scoped, filtered page took {work} steps");

        let naive = |flt: &EntryFilter, from: usize, max: usize| -> Vec<u64> {
            pl.entries()[from..]
                .iter()
                .filter(|e| match e {
                    LedgerEntry::Finding(_) => {
                        flt.department.is_none()
                            && flt.event_type.is_none()
                            && flt.scope.as_ref().is_none_or(|s| s.iter().any(|d| d == FINDINGS_DEPARTMENT))
                    }
                    LedgerEntry::Event(ev) => {
                        flt.department.as_ref().is_none_or(|d| *d == ev.department)
                            && flt.event_type.as_ref().is_none_or(|t| *t == ev.event_type)
                            && flt.scope.as_ref().is_none_or(|s| s.contains(&ev.department))
                    }
                })
                .take(max)
                .map(|e| e.seq())
                .collect()
        };
        let scopes: [Option<&[&str]>; 4] =
            [None, Some(&["sales"]), Some(&["finance", "revenue_recovery"]), Some(&["nobody"])];
        for scope in scopes {
            for d in [None, Some("sales"), Some("finance"), Some("rare"), Some("none")] {
                for t in [None, Some("note"), Some("call"), Some("payout"), Some("ping")] {
                    let flt = f(d, t, scope);
                    for (from, max) in [(0, 25), (12_345, 7), (19_990, 1000), (0, 3000)] {
                        let got: Vec<u64> = pl.select(&flt, from, pl.len(), max).iter().map(|e| e.seq()).collect();
                        assert_eq!(got, naive(&flt, from, max), "{flt:?} from {from} max {max}");
                    }
                }
            }
        }
    }

    /// AEGIS L4: `verify_log_file` re-reads and re-hashes the log on disk and
    /// compares it with the in-memory head; any change to the acknowledged
    /// bytes is caught, and bytes past the snapshot are not read.
    #[test]
    fn the_disk_reverify_catches_any_change_to_the_acknowledged_bytes() {
        let path = scratch_path("disk_verify");
        let _cleanup = ScratchFile(path.clone());
        let mut pl = PersistentLedger::open(&path).unwrap();
        for i in 0..5 {
            pl.append(sample_record(&format!("f-{i}"), "1.00")).unwrap();
        }
        let (n, head, bytes) = (pl.len(), pl.head().head_hash, pl.log_bytes());
        assert_eq!(bytes, std::fs::metadata(&path).unwrap().len());
        assert_eq!(verify_log_file(&path, bytes, n, &head), Ok(()));
        let good = std::fs::read(&path).unwrap();
        let text = String::from_utf8(good.clone()).unwrap();
        // An append by someone else after the snapshot is outside it.
        let mut f = OpenOptions::new().append(true).open(&path).unwrap();
        f.write_all(b"{\"not\":\"read\"}\n").unwrap();
        assert_eq!(verify_log_file(&path, bytes, n, &head), Ok(()));
        let cases: Vec<(&str, Vec<u8>)> = vec![
            ("amount rewritten", text.replacen("\"1.00\"", "\"9.00\"", 1).into_bytes()),
            ("truncated", good[..good.len() - 10].to_vec()),
            ("a line removed", text.lines().skip(1).map(|l| format!("{l}\n")).collect::<String>().into_bytes()),
            ("blank line", text.replacen('\n', "\n\n", 1).into_bytes()),
        ];
        for (name, content) in cases {
            std::fs::write(&path, &content).unwrap();
            assert!(verify_log_file(&path, bytes, n, &head).is_err(), "{name}");
        }
        // Another valid chain of the same shape: caught by the head comparison.
        std::fs::write(&path, &good).unwrap();
        assert!(verify_log_file(&path, bytes, n, &"0".repeat(64)).unwrap_err().contains("rewritten"));
    }

    /// RAII cleanup for the scratch files these tests write to /tmp: the log
    /// and the files the ledger keeps next to it (sweep F: `.lock`, `.head`,
    /// and a `.head.tmp` a failed checkpoint write could leave).
    struct ScratchFile(PathBuf);
    impl Drop for ScratchFile {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
            for suffix in [".lock", ".head", ".head.tmp"] {
                let _ = std::fs::remove_file(super::sibling(&self.0, suffix));
            }
        }
    }

    /// A scratch copy of a checked-in fixture: opening takes `<log>.lock`
    /// and may write `<log>.head` next to the log (sweep F), which must never
    /// land in tests/fixtures.
    /// AEGIS M2: a pre-checkpoint log opens only with the advised one-shot
    /// migrate value (what an operator does once per legacy log).
    fn open_migrated(path: &Path) -> Result<PersistentLedger, PersistError> {
        match PersistentLedger::open(path) {
            Err(PersistError::Checkpoint(r)) if r.contains("NO head checkpoint") => {
                let v = advised(&r, "LEDGER_MIGRATE_LEGACY");
                assert!(is_log_binding(&v), "{v}");
                let pl = PersistentLedger::open_with(path, LedgerOpenOptions { migrate: Some(v), ..LedgerOpenOptions::default() })?;
                assert!(pl.open_report().migrate_used);
                Ok(pl)
            }
            other => other,
        }
    }

    fn fixture_copy(name: &str) -> (PathBuf, ScratchFile) {
        let fixture = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
        let path = scratch_path("fixture_copy");
        std::fs::copy(&fixture, &path).unwrap();
        (path.clone(), ScratchFile(path))
    }
}
