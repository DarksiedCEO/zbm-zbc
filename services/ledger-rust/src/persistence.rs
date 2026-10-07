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
//!   (`PersistError::Checkpoint`) unless the operator passes `allow_reset`
//!   (`LEDGER_ALLOW_RESET=1`), which is logged loudly. A log AHEAD of the
//!   checkpoint is accepted (a crash between the log fsync and the head
//!   rename leaves it one ahead; every extra entry still has to verify on the
//!   chain) and the checkpoint is moved up. A log with no head file at all is
//!   a log written by an older binary: the checkpoint is created from the
//!   verified log, with a warning. The head file lives next to the log, so
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
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HeadCheckpoint {
    pub entries: u64,
    pub head_seq: Option<u64>,
    pub head_hash: String,
}

impl HeadCheckpoint {
    fn of(ledger: &Ledger) -> HeadCheckpoint {
        match ledger.entries().last() {
            None => HeadCheckpoint { entries: 0, head_seq: None, head_hash: genesis_hash() },
            Some(e) => HeadCheckpoint { entries: ledger.len() as u64, head_seq: Some(e.seq()), head_hash: e.hash().to_string() },
        }
    }

    fn is_well_formed(&self) -> bool {
        let hex = self.head_hash.len() == 64 && self.head_hash.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
        let seq_ok = match self.head_seq {
            None => self.entries == 0,
            Some(s) => self.entries > 0 && s == self.entries - 1,
        };
        hex && seq_ok
    }
}

/// Options for `PersistentLedger::open_with`.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct LedgerOpenOptions {
    /// Sweep F-6 operator override (`LEDGER_ALLOW_RESET=1`): accept a log
    /// that does not reach the head checkpoint (or a missing / unreadable
    /// checkpoint) and move the checkpoint to whatever the verified log
    /// holds. Never skips chain verification, the single-writer lock or any
    /// corruption check.
    pub allow_reset: bool,
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

#[derive(Debug)]
pub struct PersistentLedger {
    ledger: Ledger,
    event_index: HashMap<String, usize>,
    /// finding_id -> every position holding it (sweep F-7; duplicates are
    /// legal in a log, see module docs).
    finding_index: HashMap<String, Vec<usize>>,
    file: Box<dyn LogSink>,
    path: PathBuf,
    torn_tail: Option<TornTailRecovery>,
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

/// Reads `<log>.head`: Ok(None) when it does not exist.
fn read_head(log: &Path) -> Result<Option<HeadCheckpoint>, PersistError> {
    let p = head_path_for(log);
    let text = match std::fs::read_to_string(&p) {
        Ok(t) => t,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(PersistError::Checkpoint(format!("cannot read {}: {e}", p.display()))),
    };
    let head: HeadCheckpoint = serde_json::from_str(text.trim_end_matches('\n'))
        .map_err(|e| PersistError::Checkpoint(format!("{} is not a head checkpoint: {e}", p.display())))?;
    if !head.is_well_formed() {
        return Err(PersistError::Checkpoint(format!("{} is not a well-formed head checkpoint: {text:?}", p.display())));
    }
    Ok(Some(head))
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

/// Sweep F-6: compares the verified log with the head checkpoint. Ok(true)
/// when the checkpoint must be (re)written, Ok(false) when it already matches.
fn check_against_head(
    path: &Path,
    log_existed: bool,
    ledger: &Ledger,
    head: Result<Option<HeadCheckpoint>, PersistError>,
    opts: LedgerOpenOptions,
) -> Result<bool, PersistError> {
    let n = ledger.len() as u64;
    let refusal: String = match head {
        Err(PersistError::Checkpoint(reason)) => reason,
        Err(other) => return Err(other),
        Ok(None) => {
            if n > 0 {
                crate::ledger_log!(
                    "ledger-rust: WARNING — {} has {n} verified entries but no head checkpoint ({}); this log was \
                     written by a binary older than the checkpoint (sweep F-6). Creating the checkpoint now at \
                     entries={n}. Deletion or rollback BEFORE this point cannot be detected.",
                    path.display(),
                    head_path_for(path).display()
                );
            }
            return Ok(true);
        }
        Ok(Some(cp)) => {
            if !log_existed {
                format!(
                    "the log file {} is MISSING but the head checkpoint {} records {} entries (head hash {}): \
                     the log was deleted or moved",
                    path.display(),
                    head_path_for(path).display(),
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
                let at = if cp.entries == 0 { genesis_hash() } else { ledger.entries()[(cp.entries - 1) as usize].hash().to_string() };
                if at != cp.head_hash {
                    format!(
                        "the log {} does not contain the checkpointed head: entry {:?} has hash {at}, the head \
                         checkpoint records {} (the log was rewritten or replaced)",
                        path.display(),
                        cp.head_seq,
                        cp.head_hash
                    )
                } else if n > cp.entries {
                    crate::ledger_log!(
                        "ledger-rust: note — {} holds {n} verified entries, {} more than the head checkpoint ({}); \
                         an append was interrupted between the log fsync and the checkpoint write. Moving the \
                         checkpoint up.",
                        path.display(),
                        n - cp.entries,
                        cp.entries
                    );
                    return Ok(true);
                } else {
                    return Ok(false);
                }
            }
        }
    };
    if !opts.allow_reset {
        return Err(PersistError::Checkpoint(format!(
            "{refusal}. Refusing to start (fail closed). Restore the log from backup; or, if you accept the log \
             as it is now, restart once with LEDGER_ALLOW_RESET=1 (logged) to move the checkpoint to it."
        )));
    }
    crate::ledger_log!(
        "ledger-rust: WARNING — LEDGER_ALLOW_RESET=1: OPERATOR RESET of the head checkpoint. {refusal}. Accepting \
         the verified log as it is now ({n} entries) and rewriting {}.",
        head_path_for(path).display()
    );
    Ok(true)
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
        let write_checkpoint = check_against_head(&path, existed, &ledger, head, opts)?;

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
            match write_head(&path, &HeadCheckpoint::of(&ledger)) {
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
            file: wrap(file),
            path,
            torn_tail,
            poisoned: None,
            _lock: lock,
            #[cfg(test)]
            fail_next_head_write: false,
        })
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

    /// Clones entries `from..to` (clamped); see `Ledger::clone_range`.
    pub fn clone_range(&self, from: usize, to: usize) -> Vec<LedgerEntry> {
        self.ledger.clone_range(from, to)
    }

    /// The current head (equal to the durable checkpoint after every
    /// acknowledged append; sweep F-6, `GET /ledger/head`).
    pub fn head(&self) -> HeadCheckpoint {
        HeadCheckpoint::of(&self.ledger)
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
                };
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
            PersistentLedger::open_with(&path, LedgerOpenOptions { allow_reset: true }),
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

    // --- sweep F (Oct 6 2026) ----------------------------------------------------

    fn reset() -> LedgerOpenOptions {
        LedgerOpenOptions { allow_reset: true }
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
        assert_eq!(head_on_disk(&path), HeadCheckpoint { entries: 0, head_seq: None, head_hash: crate::genesis_hash() });
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
        let pl = PersistentLedger::open_with(&path, reset()).expect("operator reset");
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
        let pl = PersistentLedger::open_with(&path, reset()).unwrap();
        assert_eq!(pl.head(), head_on_disk(&path));
        assert_eq!(pl.entries()[0].as_finding().unwrap().finding_id, "g-0");
    }

    /// F-6: a crash between the log fsync and the checkpoint rename leaves
    /// the log one entry ahead; that is accepted (every entry still verifies)
    /// and the checkpoint moves up. A log written before checkpoints existed
    /// (no head file) gets one.
    #[test]
    fn a_log_ahead_of_its_checkpoint_or_without_one_opens_and_is_checkpointed() {
        let path = scratch_path("ahead");
        let _cleanup = ScratchFile(path.clone());
        let behind;
        {
            let mut pl = PersistentLedger::open(&path).unwrap();
            pl.append(sample_record("f-0", "1.00")).unwrap();
            behind = std::fs::read(head_path_for(&path)).unwrap();
            pl.append(sample_record("f-1", "1.00")).unwrap();
        }
        std::fs::write(head_path_for(&path), &behind).unwrap();
        let pl = PersistentLedger::open(&path).expect("one entry ahead is a crash window, not tampering");
        assert_eq!(head_on_disk(&path), pl.head());
        assert_eq!(pl.head().entries, 2);
        drop(pl);

        std::fs::remove_file(head_path_for(&path)).unwrap();
        let pl = PersistentLedger::open(&path).expect("a pre-checkpoint log still opens");
        assert_eq!(head_on_disk(&path), pl.head());
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
        let pl = PersistentLedger::open_with(&path, reset()).unwrap();
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
        let mut pl = PersistentLedger::open(&path).unwrap();
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
    fn fixture_copy(name: &str) -> (PathBuf, ScratchFile) {
        let fixture = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
        let path = scratch_path("fixture_copy");
        std::fs::copy(&fixture, &path).unwrap();
        (path.clone(), ScratchFile(path))
    }
}
