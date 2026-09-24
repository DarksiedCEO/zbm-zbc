//! Generic event records (build contract section 2, docs/adr/0003).
//!
//! `POST /ledger/events` lets any department record a hash-chained,
//! tamper-evident fact ("Onboarding blocked activation for client_123")
//! without the ledger knowing that department's schema. The ledger stores
//! only small, validated identifiers, a SHA-256 of the caller's full
//! payload, and a short human summary — never the payload itself.

use serde::{Deserialize, Serialize};

/// Request body for `POST /ledger/events`. Every field is required and
/// unknown fields are rejected (`deny_unknown_fields`).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct EventInput {
    pub event_id: String,
    pub department: String,
    pub event_type: String,
    pub actor: String,
    pub subject_id: String,
    pub payload_sha256: String,
    pub summary: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EventValidationError(pub String);

impl std::fmt::Display for EventValidationError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for EventValidationError {}

/// `[A-Za-z0-9._:-]`, 1..=max chars.
fn check_id(field: &str, v: &str, max: usize) -> Result<(), EventValidationError> {
    let ok_chars = v
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b':' | b'-'));
    if v.is_empty() || v.len() > max || !ok_chars {
        return Err(EventValidationError(format!(
            "{field} must be 1-{max} characters of [A-Za-z0-9._:-]"
        )));
    }
    Ok(())
}

/// `[a-z0-9_]`, 1..=max chars.
fn check_slug(field: &str, v: &str, max: usize) -> Result<(), EventValidationError> {
    let ok_chars = v
        .bytes()
        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_');
    if v.is_empty() || v.len() > max || !ok_chars {
        return Err(EventValidationError(format!(
            "{field} must be 1-{max} characters of [a-z0-9_]"
        )));
    }
    Ok(())
}

impl EventInput {
    /// Enforces every contract rule. Charset checks are ASCII-only, so byte
    /// length equals character length for the id/slug fields; `summary`
    /// length is counted in Unicode scalar values (1-280) and may not
    /// contain any control character (C0, DEL or C1).
    pub fn validate(&self) -> Result<(), EventValidationError> {
        check_id("event_id", &self.event_id, 128)?;
        check_slug("department", &self.department, 64)?;
        check_slug("event_type", &self.event_type, 64)?;
        check_slug("actor", &self.actor, 64)?;
        check_id("subject_id", &self.subject_id, 128)?;

        let h = &self.payload_sha256;
        if h.len() != 64 || !h.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
            return Err(EventValidationError(
                "payload_sha256 must be exactly 64 lowercase hex characters".into(),
            ));
        }

        let n = self.summary.chars().count();
        if n == 0 || n > 280 {
            return Err(EventValidationError(format!(
                "summary must be 1-280 characters, got {n}"
            )));
        }
        if self.summary.chars().any(char::is_control) {
            return Err(EventValidationError(
                "summary must not contain control characters".into(),
            ));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn valid() -> EventInput {
        EventInput {
            event_id: "onb-01J8ZQ.x:y_z".into(),
            department: "onboarding".into(),
            event_type: "compliance_ruling".into(),
            actor: "intel_15_compliance".into(),
            subject_id: "client_123".into(),
            payload_sha256: "0123456789abcdef".repeat(4),
            summary: "Activation blocked: 2 requirements unmet".into(),
        }
    }

    #[test]
    fn valid_event_passes() {
        assert_eq!(valid().validate(), Ok(()));
    }

    #[test]
    fn boundary_lengths_pass() {
        let mut e = valid();
        e.event_id = "a".repeat(128);
        e.subject_id = "b".repeat(128);
        e.department = "c".repeat(64);
        e.event_type = "d".repeat(64);
        e.actor = "e".repeat(64);
        e.summary = "é".repeat(280); // 280 chars, 560 bytes
        assert_eq!(e.validate(), Ok(()));
    }

    #[test]
    fn each_rule_rejects() {
        type Mutator = Box<dyn Fn(&mut EventInput)>;
        let cases: Vec<(&str, Mutator)> = vec![
            ("empty event_id", Box::new(|e| e.event_id.clear())),
            ("long event_id", Box::new(|e| e.event_id = "a".repeat(129))),
            ("event_id space", Box::new(|e| e.event_id = "a b".into())),
            ("event_id pipe", Box::new(|e| e.event_id = "a|b".into())),
            ("event_id unicode", Box::new(|e| e.event_id = "é".into())),
            ("uppercase department", Box::new(|e| e.department = "Onboarding".into())),
            ("department dash", Box::new(|e| e.department = "on-boarding".into())),
            ("long department", Box::new(|e| e.department = "a".repeat(65))),
            ("empty event_type", Box::new(|e| e.event_type.clear())),
            ("actor dot", Box::new(|e| e.actor = "intel.15".into())),
            ("long subject_id", Box::new(|e| e.subject_id = "a".repeat(129))),
            ("subject_id slash", Box::new(|e| e.subject_id = "a/b".into())),
            ("short sha", Box::new(|e| e.payload_sha256 = "ab".into())),
            ("upper sha", Box::new(|e| e.payload_sha256 = "A".repeat(64))),
            ("non-hex sha", Box::new(|e| e.payload_sha256 = "g".repeat(64))),
            ("empty summary", Box::new(|e| e.summary.clear())),
            ("long summary", Box::new(|e| e.summary = "a".repeat(281))),
            ("newline summary", Box::new(|e| e.summary = "a\nb".into())),
            ("tab summary", Box::new(|e| e.summary = "a\tb".into())),
            ("DEL summary", Box::new(|e| e.summary = "a\u{7f}b".into())),
            ("C1 summary", Box::new(|e| e.summary = "a\u{85}b".into())),
            ("NUL summary", Box::new(|e| e.summary = "a\u{0}b".into())),
        ];
        for (name, mutate) in cases {
            let mut e = valid();
            mutate(&mut e);
            assert!(e.validate().is_err(), "{name} should be rejected");
        }
    }

    #[test]
    fn summary_may_contain_pipes_and_unicode() {
        let mut e = valid();
        e.summary = "Blocked | reason: “missing W-9” ✓".into();
        assert_eq!(e.validate(), Ok(()));
    }

    #[test]
    fn unknown_and_missing_fields_are_rejected_by_deserialization() {
        let ok = r#"{"event_id":"e1","department":"d","event_type":"t","actor":"a","subject_id":"s","payload_sha256":"x","summary":"y"}"#;
        assert!(serde_json::from_str::<EventInput>(ok).is_ok());
        let extra = r#"{"event_id":"e1","department":"d","event_type":"t","actor":"a","subject_id":"s","payload_sha256":"x","summary":"y","extra":1}"#;
        assert!(serde_json::from_str::<EventInput>(extra).is_err());
        let missing = r#"{"event_id":"e1","department":"d","event_type":"t","actor":"a","subject_id":"s","payload_sha256":"x"}"#;
        assert!(serde_json::from_str::<EventInput>(missing).is_err());
        let null = r#"{"event_id":null,"department":"d","event_type":"t","actor":"a","subject_id":"s","payload_sha256":"x","summary":"y"}"#;
        assert!(serde_json::from_str::<EventInput>(null).is_err());
    }
}
