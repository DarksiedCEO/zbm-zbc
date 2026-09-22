//! ZBM Revenue Recovery — tamper-evident evidence ledger.
//!
//! Every detection Finding that clears the correlation/valuation layer gets
//! appended here. Each entry's hash is computed over its own fields PLUS
//! the previous entry's hash, so altering or deleting any past entry breaks
//! every hash after it — the same structural idea as a blockchain, applied
//! narrowly to an audit trail rather than to consensus/currency.
//!
//! This is the AEGIS-adjacent trust boundary named in Decision 6: Rust was
//! chosen here specifically for memory safety and mature crypto primitives
//! (RustCrypto's `sha2`), because tamper-evidence is exactly the property
//! that must not have a subtle bug.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const GENESIS_HASH_SEED: &str = "ZBM-REVENUE-RECOVERY-LEDGER-GENESIS-2026";

/// The caller-supplied facts for one ledger entry. Deliberately narrow —
/// this crate does not know about detection logic, confidence bands, or
/// leak categories; it only knows how to hash-chain whatever record it's
/// handed. Keeping it decoupled from `zbm_schema` (Python) is intentional:
/// the ledger's integrity guarantee should not depend on any other
/// service's schema evolving in lockstep.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LedgerRecordInput {
    pub finding_id: String,
    pub agent_id: String,
    pub entity_id: String,
    pub leak_category: String,
    pub amount_usd: Option<f64>,
    pub value_classification: Option<String>,
    pub decision_confidence: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct LedgerEntry {
    pub seq: u64,
    pub finding_id: String,
    pub agent_id: String,
    pub entity_id: String,
    pub leak_category: String,
    pub amount_usd: Option<f64>,
    pub value_classification: Option<String>,
    pub decision_confidence: Option<String>,
    pub recorded_at: DateTime<Utc>,
    pub prev_hash: String,
    pub hash: String,
}

#[derive(Debug, PartialEq)]
pub enum LedgerError {
    /// The chain is broken at the given sequence number — either the
    /// stored hash doesn't match its own recomputed content, or its
    /// prev_hash doesn't match the prior entry's hash. Either way: the
    /// ledger from this point onward can no longer be trusted as-is.
    ChainBroken { at_seq: u64, reason: String },
    Empty,
}

#[derive(Debug, Default)]
pub struct Ledger {
    entries: Vec<LedgerEntry>,
}

fn compute_hash(
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
    // Canonical, explicit field ordering — never derive this from a struct's
    // in-memory field order, which is not a stable contract across Rust
    // versions or refactors.
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

fn genesis_hash() -> String {
    let mut hasher = Sha256::new();
    hasher.update(GENESIS_HASH_SEED.as_bytes());
    hex::encode(hasher.finalize())
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
            Some(e) => e.hash.clone(),
            None => genesis_hash(),
        }
    }

    /// Appends a new entry, chaining it to the previous entry's hash (or
    /// the genesis hash if this is the first entry). Returns the newly
    /// appended entry.
    pub fn append(&mut self, record: LedgerRecordInput) -> &LedgerEntry {
        let seq = self.entries.len() as u64;
        let prev_hash = self.last_hash();
        let recorded_at = Utc::now();

        let hash = compute_hash(
            seq,
            &record.finding_id,
            &record.agent_id,
            &record.entity_id,
            &record.leak_category,
            record.amount_usd,
            &record.value_classification,
            &record.decision_confidence,
            &recorded_at,
            &prev_hash,
        );

        let entry = LedgerEntry {
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
        };

        self.entries.push(entry);
        self.entries.last().expect("just pushed")
    }

    /// Recomputes every entry's hash from its own fields and checks it
    /// against the stored hash, AND checks that each entry's prev_hash
    /// matches the previous entry's actual hash. Returns the first break
    /// found, if any — a real integrity check, not a format validator.
    pub fn verify_chain(&self) -> Result<(), LedgerError> {
        if self.entries.is_empty() {
            return Err(LedgerError::Empty);
        }

        let mut expected_prev = genesis_hash();

        for entry in &self.entries {
            if entry.prev_hash != expected_prev {
                return Err(LedgerError::ChainBroken {
                    at_seq: entry.seq,
                    reason: "prev_hash does not match the actual previous entry's hash".into(),
                });
            }

            let recomputed = compute_hash(
                entry.seq,
                &entry.finding_id,
                &entry.agent_id,
                &entry.entity_id,
                &entry.leak_category,
                entry.amount_usd,
                &entry.value_classification,
                &entry.decision_confidence,
                &entry.recorded_at,
                &entry.prev_hash,
            );

            if recomputed != entry.hash {
                return Err(LedgerError::ChainBroken {
                    at_seq: entry.seq,
                    reason: "stored hash does not match recomputed hash — entry was altered".into(),
                });
            }

            expected_prev = entry.hash.clone();
        }

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_record(finding_id: &str, entity_id: &str, amount: f64) -> LedgerRecordInput {
        LedgerRecordInput {
            finding_id: finding_id.to_string(),
            agent_id: "affiliate-coupon-extension-v1".to_string(),
            entity_id: entity_id.to_string(),
            leak_category: "affiliate_coupon_extension".to_string(),
            amount_usd: Some(amount),
            value_classification: Some("attributed".to_string()),
            decision_confidence: Some("high".to_string()),
        }
    }

    #[test]
    fn append_grows_the_ledger_and_chains_hashes() {
        let mut ledger = Ledger::new();
        ledger.append(sample_record("aff-ord_1002", "ord_1002", 120.00));
        ledger.append(sample_record("disc-ord_1003", "ord_1003", 128.00));

        assert_eq!(ledger.len(), 2);
        let entries = ledger.entries();
        assert_eq!(entries[0].prev_hash, genesis_hash());
        assert_eq!(entries[1].prev_hash, entries[0].hash);
        assert_ne!(entries[0].hash, entries[1].hash);
    }

    #[test]
    fn verify_chain_passes_on_an_untampered_ledger() {
        let mut ledger = Ledger::new();
        for i in 0..5 {
            ledger.append(sample_record(&format!("f-{i}"), &format!("ord_{i}"), 10.0 * i as f64 + 1.0));
        }
        assert_eq!(ledger.verify_chain(), Ok(()));
    }

    #[test]
    fn verify_chain_rejects_empty_ledger() {
        let ledger = Ledger::new();
        assert_eq!(ledger.verify_chain(), Err(LedgerError::Empty));
    }

    #[test]
    fn tampering_with_a_past_entrys_amount_breaks_the_chain() {
        let mut ledger = Ledger::new();
        ledger.append(sample_record("f-0", "ord_0", 100.0));
        ledger.append(sample_record("f-1", "ord_1", 200.0));
        ledger.append(sample_record("f-2", "ord_2", 300.0));

        assert_eq!(ledger.verify_chain(), Ok(()));

        // Simulate tampering: directly mutate a past entry's amount without
        // recomputing hashes, the way a compromised DB write might.
        ledger.entries[0].amount_usd = Some(999_999.0);

        let result = ledger.verify_chain();
        assert!(matches!(result, Err(LedgerError::ChainBroken { at_seq: 0, .. })));
    }

    #[test]
    fn tampering_with_prev_hash_pointer_is_also_caught() {
        let mut ledger = Ledger::new();
        ledger.append(sample_record("f-0", "ord_0", 50.0));
        ledger.append(sample_record("f-1", "ord_1", 75.0));

        // Simulate someone splicing in a fabricated prev_hash on entry 1.
        ledger.entries[1].prev_hash = "0000000000000000000000000000000000000000000000000000000000000000".to_string();

        let result = ledger.verify_chain();
        assert!(matches!(result, Err(LedgerError::ChainBroken { at_seq: 1, .. })));
    }

    #[test]
    fn empty_ledger_len_and_is_empty() {
        let ledger = Ledger::new();
        assert_eq!(ledger.len(), 0);
        assert!(ledger.is_empty());
    }
}
