//! ZBM Revenue Recovery — tamper-evident evidence ledger.
//!
//! Every detection Finding that clears the correlation/valuation layer gets
//! appended here. Each entry's hash is computed over its own fields PLUS
//! the previous entry's hash, so altering or deleting any past entry breaks
//! every hash after it — the same structural idea as a blockchain, applied
//! narrowly to an audit trail rather than to consensus/currency.
//!
//! Since Sep 24 2026 the ledger holds two kinds of entry on ONE shared hash
//! chain (build contract section 2, docs/adr/0003):
//!   - `kind: "finding"` — a Revenue Recovery detection finding (unchanged
//!     hash canonical form; legacy entries with no `kind` are findings).
//!   - `kind: "event"`   — a generic, department-agnostic event record
//!     (e.g. an Onboarding compliance ruling), idempotent on `event_id`.
//!
//! This is the AEGIS-adjacent trust boundary named in Decision 6: Rust was
//! chosen here specifically for memory safety and mature crypto primitives
//! (RustCrypto's `sha2`), because tamper-evidence is exactly the property
//! that must not have a subtle bug.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Deserializer, Serialize};
use sha2::{Digest, Sha256};

/// Writes one line to stderr WITHOUT panicking if stderr is unwritable.
/// `eprintln!` panics on a failed write (full disk under a redirected log,
/// a closed pipe, RLIMIT_FSIZE), which killed the whole single-threaded
/// server — found while testing AEGIS F5 (fix wave 1). A lost log line is
/// better than a dead evidence ledger; the ledger's own durability does not
/// depend on stderr.
#[macro_export]
macro_rules! ledger_log {
    ($($arg:tt)*) => {{
        use ::std::io::Write as _;
        let _ = ::std::writeln!(::std::io::stderr(), $($arg)*);
    }};
}

mod event;
mod money;
mod persistence;
pub use event::{EventInput, EventValidationError};
pub use money::{deserialize_persisted_amount, Money, MoneyError, MAX_MONEY};
pub use persistence::{
    head_path_for, is_log_binding, is_reset_binding, verify_log_file, EntryFilter, FINDINGS_DEPARTMENT, lock_path_for, log_binding, reset_binding, AppendOutcome,
    EventAppendOutcome, HeadCheckpoint, LedgerOpenOptions, OpenReport, PersistError, PersistentLedger, TornTailRecovery,
};

pub const GENESIS_HASH_SEED: &str = "ZBM-REVENUE-RECOVERY-LEDGER-GENESIS-2026";

/// Domain-separation prefix for event canonical strings. A finding's
/// canonical string always begins with its decimal `seq` (an ASCII digit),
/// so no event canonical string (which begins with the letter `e`) can ever
/// equal a finding canonical string.
pub const EVENT_DOMAIN_PREFIX: &str = "event|";

/// The caller-supplied facts for one FINDING ledger entry. Deliberately
/// narrow — this crate does not know about detection logic, confidence
/// bands, or leak categories; it only knows how to hash-chain whatever
/// record it's handed. Keeping it decoupled from `zbm_schema` (Python) is
/// intentional: the ledger's integrity guarantee should not depend on any
/// other service's schema evolving in lockstep.
///
/// `amount_usd` is a canonical two-decimal money string (or null). A JSON
/// number is rejected on input — see money.rs.
///
/// Unknown fields are rejected (fix wave 3, AEGIS N8 sweep): a caller that
/// sends e.g. `"approved_by"` gets a 400, instead of a 201 that silently
/// dropped the field it believed it had recorded.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LedgerRecordInput {
    pub finding_id: String,
    pub agent_id: String,
    pub entity_id: String,
    pub leak_category: String,
    pub amount_usd: Option<Money>,
    pub value_classification: Option<String>,
    pub decision_confidence: Option<String>,
}

/// The string fields of a finding that go into its `|`-joined canonical
/// hash string, as (name, value, optional?) triples in canonical order.
fn finding_string_fields<'a>(
    finding_id: &'a str,
    agent_id: &'a str,
    entity_id: &'a str,
    leak_category: &'a str,
    value_classification: &'a Option<String>,
    decision_confidence: &'a Option<String>,
) -> [(&'static str, Option<&'a str>, bool); 6] {
    [
        ("finding_id", Some(finding_id), false),
        ("agent_id", Some(agent_id), false),
        ("entity_id", Some(entity_id), false),
        ("leak_category", Some(leak_category), false),
        ("value_classification", value_classification.as_deref(), true),
        ("decision_confidence", decision_confidence.as_deref(), true),
    ]
}

/// AEGIS F7. The finding canonical string (unchanged since before Sep 24
/// 2026, so existing chains verify) joins fields with `|` and renders a
/// missing optional field as `null`. It is unambiguous — exactly one stored
/// entry per canonical string — if and only if no field contains `|` and no
/// optional field holds the literal string "null". Returns the first
/// violation. Applied to EVERY finding on load and in `verify_chain`,
/// legacy or new: an ambiguous entry cannot be told apart from a forged
/// re-split of it, so it is never trusted (docs/adr/0003 section 4).
fn finding_ambiguity(fields: &[(&'static str, Option<&str>, bool); 6]) -> Option<String> {
    for (name, value, optional) in fields {
        match value {
            Some(v) if v.contains('|') => {
                return Some(format!(
                    "ambiguous canonical form: finding field {name} contains '|' (the canonical \
                     field separator), so the entry cannot be distinguished from a re-split forgery"
                ))
            }
            Some("null") if *optional => {
                return Some(format!(
                    "ambiguous canonical form: optional finding field {name} holds the string \
                     \"null\", which hashes identically to a missing value"
                ))
            }
            _ => {}
        }
    }
    None
}

/// Full rule set for findings written from Sep 24 2026 on (fix wave 1):
/// the ambiguity rules above, plus no control characters (C0, DEL, C1) in
/// any string field.
fn finding_field_violation(fields: &[(&'static str, Option<&str>, bool); 6]) -> Option<String> {
    if let Some(reason) = finding_ambiguity(fields) {
        return Some(reason);
    }
    for (name, value, _) in fields {
        if value.is_some_and(|v| v.chars().any(char::is_control)) {
            return Some(format!("finding field {name} must not contain control characters"));
        }
    }
    None
}

impl LedgerRecordInput {
    /// Rejects any record whose canonical hash string would be ambiguous
    /// (a `|` in any field, the literal "null" in an optional field) or
    /// that carries control characters. `amount_usd` is already strict by
    /// type (money.rs).
    pub fn validate(&self) -> Result<(), String> {
        let fields = finding_string_fields(
            &self.finding_id,
            &self.agent_id,
            &self.entity_id,
            &self.leak_category,
            &self.value_classification,
            &self.decision_confidence,
        );
        match finding_field_violation(&fields) {
            None => Ok(()),
            Some(reason) => Err(reason),
        }
    }
}

/// A persisted finding. Unknown fields are refused on load (fix wave 3,
/// AEGIS N8): a field that is not part of the hashed canonical form (e.g. an
/// injected `"approved_by":"andre"`) used to be silently dropped, so a log
/// carrying unhashed, unverifiable "evidence" opened and verified. The field
/// set here is exactly what every real writer produced (legacy 9531fc2
/// lines, v2 negatives, v3 over-bound strings — all fixtures under
/// tests/fixtures); `kind` is removed by `LedgerEntry`'s deserializer first.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct FindingEntry {
    pub seq: u64,
    pub finding_id: String,
    pub agent_id: String,
    pub entity_id: String,
    pub leak_category: String,
    #[serde(deserialize_with = "deserialize_persisted_amount", default)]
    pub amount_usd: Option<Money>,
    pub value_classification: Option<String>,
    pub decision_confidence: Option<String>,
    pub recorded_at: DateTime<Utc>,
    pub prev_hash: String,
    pub hash: String,
}

/// A persisted event. Unknown fields are refused on load (see `FindingEntry`).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct EventEntry {
    pub seq: u64,
    pub event_id: String,
    pub department: String,
    pub event_type: String,
    pub actor: String,
    pub subject_id: String,
    pub payload_sha256: String,
    pub summary: String,
    pub recorded_at: DateTime<Utc>,
    pub prev_hash: String,
    pub hash: String,
}

impl FindingEntry {
    /// True when this stored finding carries exactly the same caller-supplied
    /// content as `input` (sweep F-7: the opt-in finding idempotency test,
    /// mirroring `EventEntry::same_content_as`). Amounts compare as their
    /// canonical money strings, so a legacy numeric amount loaded from an old
    /// log compares equal to the same amount sent as a string today.
    pub fn same_content_as(&self, input: &LedgerRecordInput) -> bool {
        self.finding_id == input.finding_id
            && self.agent_id == input.agent_id
            && self.entity_id == input.entity_id
            && self.leak_category == input.leak_category
            && self.amount_usd.as_ref().map(Money::as_str) == input.amount_usd.as_ref().map(Money::as_str)
            && self.value_classification == input.value_classification
            && self.decision_confidence == input.decision_confidence
    }

    fn string_fields(&self) -> [(&'static str, Option<&str>, bool); 6] {
        finding_string_fields(
            &self.finding_id,
            &self.agent_id,
            &self.entity_id,
            &self.leak_category,
            &self.value_classification,
            &self.decision_confidence,
        )
    }
}

impl EventEntry {
    /// The caller-supplied part of this stored event, for re-validation.
    fn to_input(&self) -> EventInput {
        EventInput {
            event_id: self.event_id.clone(),
            department: self.department.clone(),
            event_type: self.event_type.clone(),
            actor: self.actor.clone(),
            subject_id: self.subject_id.clone(),
            payload_sha256: self.payload_sha256.clone(),
            summary: self.summary.clone(),
        }
    }

    /// True when this stored event carries exactly the same caller-supplied
    /// content as `input` (the idempotent-retry test).
    pub fn same_content_as(&self, input: &EventInput) -> bool {
        self.event_id == input.event_id
            && self.department == input.department
            && self.event_type == input.event_type
            && self.actor == input.actor
            && self.subject_id == input.subject_id
            && self.payload_sha256 == input.payload_sha256
            && self.summary == input.summary
    }
}

/// One ledger entry. Serialized with a `kind` tag (`"finding"` / `"event"`).
/// Deserialization treats a missing `kind` as a finding, so log lines
/// written before events existed still load.
///
/// Fix wave 1 (AEGIS F7): deserialization only accepts lines that one of the
/// two real writers could have produced:
///   - no `kind` (legacy, 9531fc2 binary): `amount_usd` present and a JSON
///     number or null (the old `Option<f64>`); the finding must be
///     unambiguous (no `|`, no literal "null" optional). The old binary did
///     no validation, so control characters are tolerated here — they do
///     not make the canonical string ambiguous.
///   - `kind: "finding"` (current binary): `amount_usd` present and a JSON
///     string or null; every current append rule holds (no `|`, no control
///     characters, no literal "null" optional).
///   - `kind: "event"`: every `EventInput::validate` rule holds (the event
///     canonical string relies on them to be unambiguous).
#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum LedgerEntry {
    Finding(FindingEntry),
    Event(EventEntry),
}

impl<'de> Deserialize<'de> for LedgerEntry {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        use serde::de::Error;
        let mut v = serde_json::Value::deserialize(d)?;
        let obj = v
            .as_object_mut()
            .ok_or_else(|| D::Error::custom("ledger entry must be a JSON object"))?;
        let had_kind = obj.contains_key("kind");
        let kind = match obj.remove("kind") {
            None => "finding".to_string(), // legacy (pre-Sep 24 2026) entry
            Some(serde_json::Value::String(k)) => k,
            Some(other) => return Err(D::Error::custom(format!("ledger entry kind must be a string, got {other}"))),
        };
        let legacy = kind == "finding" && !had_kind;
        match kind.as_str() {
            "finding" => {
                match (legacy, obj.get("amount_usd")) {
                    (_, None) => return Err(D::Error::custom("finding entry has no amount_usd field")),
                    (_, Some(serde_json::Value::Null)) => {}
                    (true, Some(serde_json::Value::Number(_))) => {}
                    (false, Some(serde_json::Value::String(_))) => {}
                    (true, Some(other)) => {
                        return Err(D::Error::custom(format!(
                            "legacy finding (no kind) must have a numeric or null amount_usd, got {other}"
                        )))
                    }
                    (false, Some(other)) => {
                        return Err(D::Error::custom(format!(
                            "finding amount_usd must be a money string or null, got {other}"
                        )))
                    }
                }
                let f: FindingEntry = serde_json::from_value(v).map_err(D::Error::custom)?;
                let fields = f.string_fields();
                let violation = if legacy {
                    finding_ambiguity(&fields)
                } else {
                    finding_field_violation(&fields)
                };
                match violation {
                    None => Ok(LedgerEntry::Finding(f)),
                    Some(reason) => Err(D::Error::custom(reason)),
                }
            }
            "event" => {
                let e: EventEntry = serde_json::from_value(v).map_err(D::Error::custom)?;
                e.to_input()
                    .validate()
                    .map_err(|err| D::Error::custom(format!("invalid event entry: {err}")))?;
                Ok(LedgerEntry::Event(e))
            }
            other => Err(D::Error::custom(format!("unknown ledger entry kind {other:?}"))),
        }
    }
}

impl LedgerEntry {
    pub fn seq(&self) -> u64 {
        match self {
            LedgerEntry::Finding(f) => f.seq,
            LedgerEntry::Event(e) => e.seq,
        }
    }

    pub fn hash(&self) -> &str {
        match self {
            LedgerEntry::Finding(f) => &f.hash,
            LedgerEntry::Event(e) => &e.hash,
        }
    }

    pub fn prev_hash(&self) -> &str {
        match self {
            LedgerEntry::Finding(f) => &f.prev_hash,
            LedgerEntry::Event(e) => &e.prev_hash,
        }
    }

    pub fn as_finding(&self) -> Option<&FindingEntry> {
        match self {
            LedgerEntry::Finding(f) => Some(f),
            LedgerEntry::Event(_) => None,
        }
    }

    pub fn as_event(&self) -> Option<&EventEntry> {
        match self {
            LedgerEntry::Event(e) => Some(e),
            LedgerEntry::Finding(_) => None,
        }
    }

    /// Recomputes this entry's hash from its own stored fields.
    pub fn recompute_hash(&self) -> String {
        match self {
            LedgerEntry::Finding(f) => compute_finding_hash(
                f.seq,
                &f.finding_id,
                &f.agent_id,
                &f.entity_id,
                &f.leak_category,
                f.amount_usd.as_ref(),
                &f.value_classification,
                &f.decision_confidence,
                &f.recorded_at,
                &f.prev_hash,
            ),
            LedgerEntry::Event(e) => sha256_hex(&event_canonical(e)),
        }
    }
}

#[derive(Debug, PartialEq)]
pub enum LedgerError {
    /// The chain is broken at the given sequence number — either the
    /// stored hash doesn't match its own recomputed content, or its
    /// prev_hash doesn't match the prior entry's hash. Either way: the
    /// ledger from this point onward can no longer be trusted as-is.
    ChainBroken { at_seq: u64, reason: String },
}

#[derive(Debug, Default)]
pub struct Ledger {
    entries: Vec<LedgerEntry>,
}

fn sha256_hex(canonical: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(canonical.as_bytes());
    hex::encode(hasher.finalize())
}

/// The finding canonical string. UNCHANGED from before Sep 24 2026 except
/// that `amount` is now the stored money string instead of
/// `format!("{:.2}", f64)` — byte-identical for every two-decimal amount,
/// and byte-identical for legacy entries by construction (see money.rs).
#[allow(clippy::too_many_arguments)]
pub fn finding_canonical(
    seq: u64,
    finding_id: &str,
    agent_id: &str,
    entity_id: &str,
    leak_category: &str,
    amount_usd: Option<&Money>,
    value_classification: &Option<String>,
    decision_confidence: &Option<String>,
    recorded_at: &DateTime<Utc>,
    prev_hash: &str,
) -> String {
    // Canonical, explicit field ordering — never derive this from a struct's
    // in-memory field order, which is not a stable contract across Rust
    // versions or refactors.
    format!(
        "{seq}|{finding_id}|{agent_id}|{entity_id}|{leak_category}|{amount}|{vc}|{dc}|{ts}|{prev}",
        seq = seq,
        finding_id = finding_id,
        agent_id = agent_id,
        entity_id = entity_id,
        leak_category = leak_category,
        amount = amount_usd.map(Money::as_str).unwrap_or("null"),
        vc = value_classification.as_deref().unwrap_or("null"),
        dc = decision_confidence.as_deref().unwrap_or("null"),
        ts = recorded_at.to_rfc3339(),
        prev = prev_hash,
    )
}

#[allow(clippy::too_many_arguments)]
fn compute_finding_hash(
    seq: u64,
    finding_id: &str,
    agent_id: &str,
    entity_id: &str,
    leak_category: &str,
    amount_usd: Option<&Money>,
    value_classification: &Option<String>,
    decision_confidence: &Option<String>,
    recorded_at: &DateTime<Utc>,
    prev_hash: &str,
) -> String {
    sha256_hex(&finding_canonical(
        seq,
        finding_id,
        agent_id,
        entity_id,
        leak_category,
        amount_usd,
        value_classification,
        decision_confidence,
        recorded_at,
        prev_hash,
    ))
}

/// The event canonical string: `event|` domain prefix, then every field in
/// a fixed order. Every field before `summary` is restricted by validation
/// to a charset without `|`, and the two fields after it (an RFC3339
/// timestamp and a hex hash) cannot contain `|` either, so the string
/// parses back unambiguously even though `summary` may contain `|`.
pub fn event_canonical(e: &EventEntry) -> String {
    format!(
        "{EVENT_DOMAIN_PREFIX}{seq}|{event_id}|{department}|{event_type}|{actor}|{subject_id}|{payload}|{summary}|{ts}|{prev}",
        seq = e.seq,
        event_id = e.event_id,
        department = e.department,
        event_type = e.event_type,
        actor = e.actor,
        subject_id = e.subject_id,
        payload = e.payload_sha256,
        summary = e.summary,
        ts = e.recorded_at.to_rfc3339(),
        prev = e.prev_hash,
    )
}

pub fn genesis_hash() -> String {
    sha256_hex(GENESIS_HASH_SEED)
}

impl Ledger {
    pub fn new() -> Self {
        Ledger { entries: Vec::new() }
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn entries(&self) -> &[LedgerEntry] {
        &self.entries
    }

    fn last_hash(&self) -> String {
        match self.entries.last() {
            Some(e) => e.hash().to_string(),
            None => genesis_hash(),
        }
    }

    /// Builds a new finding entry chained to the current last hash (or the
    /// genesis hash if the ledger is empty), WITHOUT mutating the ledger.
    /// Exists so callers that need durability-before-visibility (see
    /// `persistence::PersistentLedger::append`) can compute the entry,
    /// persist it, and only then commit it to memory via `push_entry` —
    /// keeping the in-memory chain from ever advancing past what's on disk.
    pub fn build_entry(&self, record: LedgerRecordInput) -> LedgerEntry {
        let seq = self.entries.len() as u64;
        let prev_hash = self.last_hash();
        let recorded_at = Utc::now();

        let hash = compute_finding_hash(
            seq,
            &record.finding_id,
            &record.agent_id,
            &record.entity_id,
            &record.leak_category,
            record.amount_usd.as_ref(),
            &record.value_classification,
            &record.decision_confidence,
            &recorded_at,
            &prev_hash,
        );

        LedgerEntry::Finding(FindingEntry {
            seq,
            finding_id: record.finding_id,
            agent_id: record.agent_id,
            entity_id: record.entity_id,
            leak_category: record.leak_category,
            amount_usd: record.amount_usd,
            value_classification: record.value_classification,
            decision_confidence: record.decision_confidence,
            recorded_at,
            prev_hash,
            hash,
        })
    }

    /// Builds a new event entry on the same shared chain, without mutating
    /// the ledger. The input must already have been validated
    /// (`EventInput::validate`).
    pub fn build_event_entry(&self, input: EventInput) -> LedgerEntry {
        let mut entry = EventEntry {
            seq: self.entries.len() as u64,
            event_id: input.event_id,
            department: input.department,
            event_type: input.event_type,
            actor: input.actor,
            subject_id: input.subject_id,
            payload_sha256: input.payload_sha256,
            summary: input.summary,
            recorded_at: Utc::now(),
            prev_hash: self.last_hash(),
            hash: String::new(),
        };
        entry.hash = sha256_hex(&event_canonical(&entry));
        LedgerEntry::Event(entry)
    }

    /// Appends an already-built entry as-is, trusting the caller. Used by
    /// `append` (below) and by replay-from-disk on startup. Does NOT
    /// recompute or validate the hash — call `verify_chain` afterward if
    /// the entries did not originate from this process's own hashing.
    pub fn push_entry(&mut self, entry: LedgerEntry) {
        self.entries.push(entry);
    }

    /// Builds a new finding entry chaining it to the previous entry's hash
    /// (or the genesis hash if this is the first entry) and appends it
    /// in-memory. In-memory only — for a durable ledger, use
    /// `persistence::PersistentLedger` instead.
    pub fn append(&mut self, record: LedgerRecordInput) -> &LedgerEntry {
        let entry = self.build_entry(record);
        self.push_entry(entry);
        self.entries.last().expect("just pushed")
    }

    /// In-memory event append (no idempotency index — see
    /// `PersistentLedger::append_event` for the served behavior).
    pub fn append_event(&mut self, input: EventInput) -> &LedgerEntry {
        let entry = self.build_event_entry(input);
        self.push_entry(entry);
        self.entries.last().expect("just pushed")
    }

    /// Reconstructs a Ledger directly from a sequence of already-hashed
    /// entries (e.g. replayed from a persisted log). Does not verify the
    /// chain itself — callers must call `verify_chain()` afterward before
    /// trusting the result.
    pub fn from_entries(entries: Vec<LedgerEntry>) -> Self {
        Ledger { entries }
    }

    /// Recomputes every entry's hash (findings AND events) from its own
    /// fields and checks it against the stored hash, checks that each
    /// entry's prev_hash matches the previous entry's actual hash, and
    /// checks that `seq` is exactly the entry's position. Returns the first
    /// break found, if any — a real integrity check, not a format validator.
    ///
    /// An empty ledger is a valid chain (fix wave 3): nothing in it can have
    /// been altered. It used to be `Err(LedgerError::Empty)`, which
    /// GET /ledger/verify reported as 409 {"valid":false}.
    pub fn verify_chain(&self) -> Result<(), LedgerError> {
        let mut expected_prev = genesis_hash();
        for (i, entry) in self.entries.iter().enumerate() {
            verify_entry(entry, i as u64, &expected_prev)?;
            expected_prev = entry.hash().to_string();
        }
        Ok(())
    }

    /// Clones the entries at positions `from..to` (clamped to the ledger's
    /// length). Entries are immutable once pushed and the ledger only grows,
    /// so a reader that copies `0..len` in several short chunks gets exactly
    /// the snapshot it would have got in one long copy (sweep F-2: readers
    /// copy under the lock in chunks and serialize/verify outside it).
    pub fn clone_range(&self, from: usize, to: usize) -> Vec<LedgerEntry> {
        let to = to.min(self.entries.len());
        let from = from.min(to);
        self.entries[from..to].to_vec()
    }
}

/// One step of `Ledger::verify_chain`: checks that `entry` sits at position
/// `expected_seq`, chains to `expected_prev`, has an unambiguous canonical
/// form and a hash that matches its own fields. `verify_chain` is exactly
/// this applied from the genesis hash to the last entry; the server's
/// incremental and chunked verification (sweep F-2) applies the same step to
/// a range, starting from a hash it verified earlier.
pub fn verify_entry(entry: &LedgerEntry, expected_seq: u64, expected_prev: &str) -> Result<(), LedgerError> {
    if entry.seq() != expected_seq {
        return Err(LedgerError::ChainBroken {
            at_seq: entry.seq(),
            reason: format!("seq {} found at position {expected_seq}", entry.seq()),
        });
    }

    if entry.prev_hash() != expected_prev {
        return Err(LedgerError::ChainBroken {
            at_seq: entry.seq(),
            reason: "prev_hash does not match the actual previous entry's hash".into(),
        });
    }

    // AEGIS F7: a hash only means something if exactly one entry
    // maps to its canonical string. Checked here as well as on load
    // so an in-memory ledger and /ledger/verify enforce it too.
    let ambiguity = match entry {
        LedgerEntry::Finding(f) => finding_ambiguity(&f.string_fields()),
        LedgerEntry::Event(e) => e
            .to_input()
            .validate()
            .err()
            .map(|err| format!("ambiguous canonical form: invalid event field: {err}")),
    };
    if let Some(reason) = ambiguity {
        return Err(LedgerError::ChainBroken { at_seq: entry.seq(), reason });
    }

    if entry.recompute_hash() != entry.hash() {
        return Err(LedgerError::ChainBroken {
            at_seq: entry.seq(),
            reason: "stored hash does not match recomputed hash — entry was altered".into(),
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn money(s: &str) -> Money {
        Money::parse(s).unwrap()
    }

    fn sample_record(finding_id: &str, entity_id: &str, amount: &str) -> LedgerRecordInput {
        LedgerRecordInput {
            finding_id: finding_id.to_string(),
            agent_id: "affiliate-coupon-extension-v1".to_string(),
            entity_id: entity_id.to_string(),
            leak_category: "affiliate_coupon_extension".to_string(),
            amount_usd: Some(money(amount)),
            value_classification: Some("attributed".to_string()),
            decision_confidence: Some("high".to_string()),
        }
    }

    fn sample_event(event_id: &str) -> EventInput {
        EventInput {
            event_id: event_id.to_string(),
            department: "onboarding".to_string(),
            event_type: "compliance_ruling".to_string(),
            actor: "intel_15_compliance".to_string(),
            subject_id: "client_123".to_string(),
            payload_sha256: "a".repeat(64),
            summary: "Activation blocked: 2 requirements unmet".to_string(),
        }
    }

    fn finding_mut(l: &mut Ledger, i: usize) -> &mut FindingEntry {
        match &mut l.entries[i] {
            LedgerEntry::Finding(f) => f,
            _ => panic!("not a finding"),
        }
    }

    fn event_mut(l: &mut Ledger, i: usize) -> &mut EventEntry {
        match &mut l.entries[i] {
            LedgerEntry::Event(e) => e,
            _ => panic!("not an event"),
        }
    }

    #[test]
    fn append_grows_the_ledger_and_chains_hashes() {
        let mut ledger = Ledger::new();
        ledger.append(sample_record("aff-ord_1002", "ord_1002", "120.00"));
        ledger.append(sample_record("disc-ord_1003", "ord_1003", "128.00"));

        assert_eq!(ledger.len(), 2);
        let entries = ledger.entries();
        assert_eq!(entries[0].prev_hash(), genesis_hash());
        assert_eq!(entries[1].prev_hash(), entries[0].hash());
        assert_ne!(entries[0].hash(), entries[1].hash());
    }

    #[test]
    fn verify_chain_passes_on_an_untampered_ledger() {
        let mut ledger = Ledger::new();
        for i in 0..5 {
            ledger.append(sample_record(&format!("f-{i}"), &format!("ord_{i}"), &format!("{}.00", 10 * i + 1)));
        }
        assert_eq!(ledger.verify_chain(), Ok(()));
    }

    /// Fix wave 3: an empty ledger is a valid chain (nothing has been
    /// altered). This test used to assert `Err(LedgerError::Empty)`, which
    /// made GET /ledger/verify answer 409 {"valid":false} on a fresh
    /// ledger and forced every caller to special-case "invalid but fine".
    #[test]
    fn verify_chain_accepts_empty_ledger() {
        let ledger = Ledger::new();
        assert_eq!(ledger.verify_chain(), Ok(()));
    }

    #[test]
    fn tampering_with_a_past_entrys_amount_breaks_the_chain() {
        let mut ledger = Ledger::new();
        ledger.append(sample_record("f-0", "ord_0", "100.00"));
        ledger.append(sample_record("f-1", "ord_1", "200.00"));
        ledger.append(sample_record("f-2", "ord_2", "300.00"));

        assert_eq!(ledger.verify_chain(), Ok(()));

        // Simulate tampering: directly mutate a past entry's amount without
        // recomputing hashes, the way a compromised DB write might.
        finding_mut(&mut ledger, 0).amount_usd = Some(money("999999.00"));

        let result = ledger.verify_chain();
        assert!(matches!(result, Err(LedgerError::ChainBroken { at_seq: 0, .. })));
    }

    #[test]
    fn tampering_with_one_cent_is_caught() {
        let mut ledger = Ledger::new();
        ledger.append(sample_record("f-0", "ord_0", "49.99"));
        finding_mut(&mut ledger, 0).amount_usd = Some(money("50.00"));
        assert!(matches!(ledger.verify_chain(), Err(LedgerError::ChainBroken { at_seq: 0, .. })));
    }

    #[test]
    fn tampering_with_prev_hash_pointer_is_also_caught() {
        let mut ledger = Ledger::new();
        ledger.append(sample_record("f-0", "ord_0", "50.00"));
        ledger.append(sample_record("f-1", "ord_1", "75.00"));

        // Simulate someone splicing in a fabricated prev_hash on entry 1.
        finding_mut(&mut ledger, 1).prev_hash =
            "0000000000000000000000000000000000000000000000000000000000000000".to_string();

        let result = ledger.verify_chain();
        assert!(matches!(result, Err(LedgerError::ChainBroken { at_seq: 1, .. })));
    }

    #[test]
    fn empty_ledger_len_and_is_empty() {
        let ledger = Ledger::new();
        assert_eq!(ledger.len(), 0);
        assert!(ledger.is_empty());
    }

    // --- events on the shared chain ---------------------------------------

    #[test]
    fn events_and_findings_share_one_chain_and_verify() {
        let mut ledger = Ledger::new();
        ledger.append(sample_record("f-0", "ord_0", "10.00"));
        ledger.append_event(sample_event("onb-1"));
        ledger.append(sample_record("f-2", "ord_2", "20.00"));
        ledger.append_event(sample_event("onb-3"));
        assert_eq!(ledger.verify_chain(), Ok(()));
        let e = ledger.entries();
        assert_eq!(e[1].prev_hash(), e[0].hash());
        assert_eq!(e[2].prev_hash(), e[1].hash());
        assert_eq!(e[3].prev_hash(), e[2].hash());
        assert_eq!(e[1].seq(), 1);
    }

    #[test]
    fn tampering_with_an_event_summary_breaks_the_chain() {
        let mut ledger = Ledger::new();
        ledger.append(sample_record("f-0", "ord_0", "10.00"));
        ledger.append_event(sample_event("onb-1"));
        ledger.append(sample_record("f-2", "ord_2", "20.00"));
        event_mut(&mut ledger, 1).summary = "Activation approved".to_string();
        assert!(matches!(ledger.verify_chain(), Err(LedgerError::ChainBroken { at_seq: 1, .. })));
    }

    #[test]
    fn tampering_with_an_event_payload_hash_breaks_the_chain() {
        let mut ledger = Ledger::new();
        ledger.append_event(sample_event("onb-1"));
        event_mut(&mut ledger, 0).payload_sha256 = "b".repeat(64);
        assert!(matches!(ledger.verify_chain(), Err(LedgerError::ChainBroken { at_seq: 0, .. })));
    }

    #[test]
    fn deleting_an_entry_breaks_the_chain() {
        let mut ledger = Ledger::new();
        ledger.append(sample_record("f-0", "ord_0", "10.00"));
        ledger.append_event(sample_event("onb-1"));
        ledger.append(sample_record("f-2", "ord_2", "20.00"));
        ledger.entries.remove(1);
        assert!(ledger.verify_chain().is_err());
    }

    #[test]
    fn event_canonical_is_domain_separated_from_findings() {
        let mut ledger = Ledger::new();
        ledger.append(sample_record("f-0", "ord_0", "10.00"));
        ledger.append_event(sample_event("onb-1"));
        let f = ledger.entries()[0].as_finding().unwrap();
        let e = ledger.entries()[1].as_event().unwrap();
        let fc = finding_canonical(
            f.seq, &f.finding_id, &f.agent_id, &f.entity_id, &f.leak_category,
            f.amount_usd.as_ref(), &f.value_classification, &f.decision_confidence,
            &f.recorded_at, &f.prev_hash,
        );
        let ec = event_canonical(e);
        assert!(ec.starts_with("event|1|onb-1|onboarding|"), "{ec}");
        assert!(fc.as_bytes()[0].is_ascii_digit(), "{fc}");
        assert!(!fc.starts_with(EVENT_DOMAIN_PREFIX));
    }

    #[test]
    fn a_finding_crafted_to_mimic_an_event_still_has_a_different_canonical_form() {
        // Even a finding whose free-text fields spell out an event's fields
        // cannot produce an event canonical string: it starts with the seq.
        let mut ledger = Ledger::new();
        ledger.append_event(sample_event("onb-1"));
        let e = ledger.entries()[0].as_event().unwrap().clone();
        let fc = finding_canonical(
            0, "event", &e.event_id, &e.department, &e.event_type, None, &None, &None,
            &e.recorded_at, &e.prev_hash,
        );
        assert_ne!(fc, event_canonical(&e));
        assert_ne!(sha256_hex(&fc), e.hash);
    }

    #[test]
    fn entries_serialize_with_kind_and_string_amount() {
        let mut ledger = Ledger::new();
        ledger.append(sample_record("f-0", "ord_0", "49.99"));
        ledger.append_event(sample_event("onb-1"));
        let f: serde_json::Value = serde_json::to_value(&ledger.entries()[0]).unwrap();
        let e: serde_json::Value = serde_json::to_value(&ledger.entries()[1]).unwrap();
        assert_eq!(f["kind"], "finding");
        assert_eq!(f["amount_usd"], "49.99");
        assert_eq!(e["kind"], "event");
        assert_eq!(e["event_id"], "onb-1");
        for key in [
            "seq", "kind", "event_id", "department", "event_type", "actor", "subject_id",
            "payload_sha256", "summary", "recorded_at", "prev_hash", "hash",
        ] {
            assert!(e.get(key).is_some(), "event entry missing {key}");
        }
        assert_eq!(e.as_object().unwrap().len(), 12, "event entry has unexpected fields: {e}");
    }

    #[test]
    fn entries_round_trip_through_json() {
        let mut ledger = Ledger::new();
        ledger.append(sample_record("f-0", "ord_0", "0.30"));
        ledger.append_event(sample_event("onb-1"));
        let text = serde_json::to_string(ledger.entries()).unwrap();
        let back: Vec<LedgerEntry> = serde_json::from_str(&text).unwrap();
        assert_eq!(back, ledger.entries());
        assert_eq!(Ledger::from_entries(back).verify_chain(), Ok(()));
    }

    #[test]
    fn unknown_kind_is_rejected() {
        let r = serde_json::from_str::<LedgerEntry>(r#"{"kind":"memo","seq":0}"#);
        assert!(r.is_err());
    }

    #[test]
    fn record_input_rejects_numeric_amount() {
        let r = serde_json::from_str::<LedgerRecordInput>(
            r#"{"finding_id":"f","agent_id":"a","entity_id":"e","leak_category":"c","amount_usd":120.0,"value_classification":null,"decision_confidence":null}"#,
        );
        assert!(r.unwrap_err().to_string().contains("must be a JSON string"));
    }

    // --- AEGIS F7: unambiguous canonical strings --------------------------

    #[test]
    fn record_validation_rejects_pipes_control_chars_and_literal_null() {
        type Mutator = Box<dyn Fn(&mut LedgerRecordInput)>;
        let cases: Vec<(&str, Mutator)> = vec![
            ("finding_id pipe", Box::new(|r| r.finding_id = "a|b".into())),
            ("agent_id pipe", Box::new(|r| r.agent_id = "|".into())),
            ("entity_id pipe", Box::new(|r| r.entity_id = "ord|1".into())),
            ("leak_category pipe", Box::new(|r| r.leak_category = "x|".into())),
            ("vc pipe", Box::new(|r| r.value_classification = Some("a|b".into()))),
            ("dc pipe", Box::new(|r| r.decision_confidence = Some("a|b".into()))),
            ("vc literal null", Box::new(|r| r.value_classification = Some("null".into()))),
            ("dc literal null", Box::new(|r| r.decision_confidence = Some("null".into()))),
            ("finding_id newline", Box::new(|r| r.finding_id = "a\nb".into())),
            ("agent_id NUL", Box::new(|r| r.agent_id = "a\u{0}".into())),
            ("entity_id DEL", Box::new(|r| r.entity_id = "a\u{7f}".into())),
            ("leak_category C1", Box::new(|r| r.leak_category = "a\u{85}".into())),
            ("vc tab", Box::new(|r| r.value_classification = Some("a\tb".into()))),
        ];
        for (name, mutate) in cases {
            let mut r = sample_record("f-0", "ord_0", "10.00");
            mutate(&mut r);
            assert!(r.validate().is_err(), "{name} should be rejected");
        }
        let mut ok = sample_record("f-0", "ord_0", "10.00");
        assert_eq!(ok.validate(), Ok(()));
        ok.value_classification = None;
        ok.decision_confidence = None;
        ok.finding_id = "NULL-ish: null, Null, é ✓".into(); // only the exact optional "null" is ambiguous
        assert_eq!(ok.validate(), Ok(()));
    }

    /// The AEGIS forgery, reproduced in memory: two different findings with
    /// the SAME canonical string (a `|` moved from entity_id into agent_id,
    /// and a missing decision_confidence turned into the string "null").
    /// Before the fix both verified with the same hash; now both are refused.
    #[test]
    fn aegis_resplit_and_null_forgery_is_refused_by_verify_and_load() {
        let mut ledger = Ledger::new();
        let mut r = sample_record("f-1", "ord_1|ord_999", "10.00");
        r.agent_id = "agent-A".into();
        r.decision_confidence = None;
        ledger.append(r); // in-memory Ledger::append does not validate; verify must catch it
        let original = ledger.entries()[0].as_finding().unwrap().clone();

        let mut forged = original.clone();
        forged.agent_id = "agent-A|ord_1".into();
        forged.entity_id = "ord_999".into();
        forged.decision_confidence = Some("null".into());
        assert_ne!(forged, original);
        let canon = |f: &FindingEntry| {
            finding_canonical(
                f.seq, &f.finding_id, &f.agent_id, &f.entity_id, &f.leak_category, f.amount_usd.as_ref(),
                &f.value_classification, &f.decision_confidence, &f.recorded_at, &f.prev_hash,
            )
        };
        assert_eq!(canon(&forged), canon(&original), "the legacy canonical form really is ambiguous");
        assert_eq!(LedgerEntry::Finding(forged.clone()).recompute_hash(), original.hash);

        for f in [original, forged] {
            let l = Ledger::from_entries(vec![LedgerEntry::Finding(f.clone())]);
            match l.verify_chain() {
                Err(LedgerError::ChainBroken { at_seq: 0, reason }) => assert!(reason.contains("ambiguous"), "{reason}"),
                other => panic!("expected ambiguity refusal, got {other:?}"),
            }
            // Both on-disk shapes are refused at deserialization too.
            let new_line = serde_json::to_string(&LedgerEntry::Finding(f.clone())).unwrap();
            assert!(serde_json::from_str::<LedgerEntry>(&new_line).unwrap_err().to_string().contains("ambiguous"));
            let mut legacy: serde_json::Value = serde_json::from_str(&new_line).unwrap();
            legacy.as_object_mut().unwrap().remove("kind");
            legacy["amount_usd"] = serde_json::json!(10.0);
            assert!(serde_json::from_value::<LedgerEntry>(legacy).unwrap_err().to_string().contains("ambiguous"));
        }
    }

    /// Same class of bug in EVENTS (found in the F7 sweep): event fields were
    /// validated on append but never on load, and `summary` may contain `|`,
    /// so a forger could shift every field one slot to the right into the
    /// summary and keep the hash. Now refused on load and by verify_chain.
    #[test]
    fn event_resplit_forgery_is_refused_by_verify_and_load() {
        let mut ledger = Ledger::new();
        let mut input = sample_event("onb-1");
        input.summary = "Blocked | reason: missing W-9".into();
        ledger.append_event(input);
        let original = ledger.entries()[0].as_event().unwrap().clone();

        let mut forged = original.clone();
        forged.department = format!("{}|{}", original.department, original.event_type);
        forged.event_type = original.actor.clone();
        forged.actor = original.subject_id.clone();
        forged.subject_id = original.payload_sha256.clone();
        forged.payload_sha256 = "Blocked ".into();
        forged.summary = " reason: missing W-9".into();
        assert_eq!(event_canonical(&forged), event_canonical(&original), "the collision is real");
        assert_eq!(LedgerEntry::Event(forged.clone()).recompute_hash(), original.hash);

        let l = Ledger::from_entries(vec![LedgerEntry::Event(forged.clone())]);
        match l.verify_chain() {
            Err(LedgerError::ChainBroken { at_seq: 0, reason }) => assert!(reason.contains("ambiguous"), "{reason}"),
            other => panic!("expected refusal, got {other:?}"),
        }
        let line = serde_json::to_string(&LedgerEntry::Event(forged)).unwrap();
        assert!(serde_json::from_str::<LedgerEntry>(&line).is_err());
        // The genuine entry still verifies and round-trips.
        assert_eq!(ledger.verify_chain(), Ok(()));
        let line = serde_json::to_string(&ledger.entries()[0]).unwrap();
        assert!(serde_json::from_str::<LedgerEntry>(&line).is_ok());
    }

    #[test]
    fn persisted_shape_must_match_a_real_writer() {
        let mut ledger = Ledger::new();
        ledger.append(sample_record("f-0", "ord_0", "10.00"));
        let v: serde_json::Value = serde_json::to_value(&ledger.entries()[0]).unwrap();
        // kind:"finding" (current writer) never wrote a numeric amount...
        let mut x = v.clone();
        x["amount_usd"] = serde_json::json!(10.0);
        assert!(serde_json::from_value::<LedgerEntry>(x).is_err());
        // ...a legacy line (no kind) never had a string amount...
        let mut x = v.clone();
        x.as_object_mut().unwrap().remove("kind");
        assert!(serde_json::from_value::<LedgerEntry>(x).is_err());
        // ...neither writer ever omitted amount_usd...
        let mut x = v.clone();
        x.as_object_mut().unwrap().remove("amount_usd");
        assert!(serde_json::from_value::<LedgerEntry>(x).is_err());
        // ...and the current writer never wrote control characters.
        let mut x = v.clone();
        x["entity_id"] = serde_json::json!("ord\n0");
        assert!(serde_json::from_value::<LedgerEntry>(x).is_err());
        // Legacy lines may (old binary did no validation; not ambiguous).
        let mut x = v.clone();
        x.as_object_mut().unwrap().remove("kind");
        x["amount_usd"] = serde_json::json!(10.0);
        x["entity_id"] = serde_json::json!("ord\n0");
        assert!(serde_json::from_value::<LedgerEntry>(x).is_ok());
        // The genuine line loads.
        assert!(serde_json::from_value::<LedgerEntry>(v).is_ok());
    }

    // --- backward compatibility with the pre-Decimal hash -----------------

    /// Verbatim reproduction of the hash function as it existed before this
    /// change (commit 9531fc2, src/lib.rs `compute_hash`), with `amount_usd:
    /// Option<f64>` formatted via `format!("{:.2}", a)`.
    #[allow(clippy::too_many_arguments)]
    fn old_compute_hash(
        seq: u64,
        finding_id: &str,
        agent_id: &str,
        entity_id: &str,
        leak_category: &str,
        amount_usd: Option<f64>,
        value_classification: &Option<String>,
        decision_confidence: &Option<String>,
        recorded_at: &DateTime<Utc>,
        prev_hash: &str,
    ) -> String {
        let canonical = format!(
            "{seq}|{finding_id}|{agent_id}|{entity_id}|{leak_category}|{amount}|{vc}|{dc}|{ts}|{prev}",
            seq = seq,
            finding_id = finding_id,
            agent_id = agent_id,
            entity_id = entity_id,
            leak_category = leak_category,
            amount = amount_usd.map(|a| format!("{:.2}", a)).unwrap_or_else(|| "null".to_string()),
            vc = value_classification.as_deref().unwrap_or("null"),
            dc = decision_confidence.as_deref().unwrap_or("null"),
            ts = recorded_at.to_rfc3339(),
            prev = prev_hash,
        );
        let mut hasher = Sha256::new();
        hasher.update(canonical.as_bytes());
        hex::encode(hasher.finalize())
    }

    /// Builds a chain entirely with the OLD hashing and the OLD JSON shape
    /// (numeric amount_usd, no `kind`), then loads and verifies it with the
    /// new code, then extends it with new findings and events.
    #[test]
    fn chain_built_with_old_hashing_verifies_with_new_code() {
        // Includes amounts the old f64 API accepted that are NOT two-decimal
        // (12.345, 2.675, 1e20): the old hash only ever covered their
        // `{:.2}` rendering, and the new loader reproduces that rendering,
        // so those chains verify too.
        let amounts: [Option<f64>; 12] = [
            Some(120.0), Some(54.38), Some(89.99), Some(0.1), Some(2.01),
            Some(49.99), Some(0.30000000000000004), None, Some(1234567.89),
            Some(12.345), Some(2.675), Some(1e20),
        ];
        let mut prev = genesis_hash();
        let mut lines = Vec::new();
        for (i, amt) in amounts.iter().enumerate() {
            let ts: DateTime<Utc> = "2026-09-22T04:00:00.123456789Z".parse().unwrap();
            let (vc, dc) = if amt.is_some() {
                (Some("observed".to_string()), Some("high".to_string()))
            } else {
                (None, None)
            };
            let hash = old_compute_hash(
                i as u64, &format!("f-{i}"), "agent", &format!("ord_{i}"), "discount_misuse",
                *amt, &vc, &dc, &ts, &prev,
            );
            lines.push(serde_json::json!({
                "seq": i, "finding_id": format!("f-{i}"), "agent_id": "agent",
                "entity_id": format!("ord_{i}"), "leak_category": "discount_misuse",
                "amount_usd": amt, "value_classification": vc, "decision_confidence": dc,
                "recorded_at": ts, "prev_hash": prev, "hash": hash,
            }));
            prev = hash;
        }

        let entries: Vec<LedgerEntry> = lines
            .iter()
            .map(|v| serde_json::from_value(v.clone()).expect("legacy entry must deserialize"))
            .collect();
        let mut ledger = Ledger::from_entries(entries);
        assert_eq!(ledger.verify_chain(), Ok(()));

        // Legacy numeric amounts load as the canonical string.
        let amounts_now: Vec<Option<String>> = ledger
            .entries()
            .iter()
            .map(|e| e.as_finding().unwrap().amount_usd.as_ref().map(|m| m.to_string()))
            .collect();
        assert_eq!(amounts_now[0].as_deref(), Some("120.00"));
        assert_eq!(amounts_now[3].as_deref(), Some("0.10"));
        assert_eq!(amounts_now[6].as_deref(), Some("0.30"));
        assert_eq!(amounts_now[7], None);
        assert_eq!(amounts_now[9].as_deref(), Some(format!("{:.2}", 12.345_f64).as_str()));
        assert_eq!(amounts_now[10].as_deref(), Some(format!("{:.2}", 2.675_f64).as_str()));
        assert_eq!(amounts_now[11].as_deref(), Some("100000000000000000000.00"));

        ledger.append(sample_record("new-f", "ord_new", "12.30"));
        ledger.append_event(sample_event("onb-after-legacy"));
        assert_eq!(ledger.verify_chain(), Ok(()));
    }

    /// For every two-decimal amount string, the new hash of a finding equals
    /// the old hash of the same finding with that amount as an f64.
    #[test]
    fn new_finding_hash_equals_old_hash_for_two_decimal_amounts() {
        let ts: DateTime<Utc> = "2026-09-24T12:00:00Z".parse().unwrap();
        let vc = Some("observed".to_string());
        let dc = Some("high".to_string());
        for s in ["0.01", "0.10", "0.30", "2.01", "12.30", "49.99", "54.38", "89.99", "900.00", "1234567.89", "99999999.99"] {
            let m = money(s);
            let new = compute_finding_hash(7, "f", "a", "e", "c", Some(&m), &vc, &dc, &ts, "p");
            let old = old_compute_hash(7, "f", "a", "e", "c", Some(s.parse::<f64>().unwrap()), &vc, &dc, &ts, "p");
            assert_eq!(new, old, "hash diverged for {s}");
        }
        let new_null = compute_finding_hash(7, "f", "a", "e", "c", None, &None, &None, &ts, "p");
        let old_null = old_compute_hash(7, "f", "a", "e", "c", None, &None, &None, &ts, "p");
        assert_eq!(new_null, old_null);
    }
}
