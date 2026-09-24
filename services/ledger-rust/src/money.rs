//! Exact money for the ledger (README gap #6, fixed Sep 24 2026; see
//! docs/adr/0003-money-decimal-and-ledger-events.md).
//!
//! `amount_usd` is carried as its canonical two-decimal string, matching
//! `^(0|[1-9][0-9]*)\.[0-9]{2}$` (build contract section 1). The ledger never
//! does arithmetic on money, so there is no numeric representation at all —
//! the string that was validated is the string that is stored, returned and
//! hashed.
//!
//! Backward compatibility: before this change `amount_usd` was an `f64` and
//! the finding hash included `format!("{:.2}", amount)`. Legacy persisted
//! entries carry a JSON number; `Money::from_legacy_f64` converts one with
//! that exact same `format!("{:.2}", f64)` call, so the canonical hash string
//! of every legacy entry is byte-identical to what the old code hashed and
//! existing chains still verify. For every amount that has exactly two
//! decimals, the new string form and the old formatted form are the same
//! bytes (proven by `two_decimal_strings_match_old_f64_formatting` below).

use serde::{Deserialize, Deserializer, Serialize, Serializer};

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Money(String);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MoneyError(pub String);

impl std::fmt::Display for MoneyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for MoneyError {}

/// Hand-rolled matcher for `^(0|[1-9][0-9]*)\.[0-9]{2}$` (no regex crate is a
/// dependency here, and this pattern is small enough to check directly).
fn is_canonical(s: &str) -> bool {
    let Some((int, frac)) = s.split_once('.') else {
        return false;
    };
    let int_ok = int == "0"
        || (!int.is_empty()
            && int.as_bytes()[0] != b'0'
            && int.bytes().all(|b| b.is_ascii_digit()));
    let frac_ok = frac.len() == 2 && frac.bytes().all(|b| b.is_ascii_digit());
    int_ok && frac_ok
}

impl Money {
    /// Parses the canonical wire form. Anything else is rejected — including
    /// "12.3", "012.30", "-1.00", "1e3", "NaN" and surrounding whitespace.
    pub fn parse(s: &str) -> Result<Money, MoneyError> {
        if is_canonical(s) {
            Ok(Money(s.to_string()))
        } else {
            Err(MoneyError(format!(
                "amount_usd {s:?} is not a two-decimal money string like \"12.30\" \
                 (must match ^(0|[1-9][0-9]*)\\.[0-9]{{2}}$)"
            )))
        }
    }

    /// Like `parse`, but additionally rejects "0.00" (positive-only fields).
    pub fn parse_positive(s: &str) -> Result<Money, MoneyError> {
        let m = Money::parse(s)?;
        if m.0 == "0.00" {
            return Err(MoneyError("amount_usd must be positive, got \"0.00\"".into()));
        }
        Ok(m)
    }

    /// Converts a LEGACY persisted `f64` amount using exactly the formatting
    /// the old hash function used (`format!("{:.2}", f64)`), so the hash
    /// canonical string is unchanged. Only used when reading old log lines.
    pub fn from_legacy_f64(v: f64) -> Result<Money, MoneyError> {
        if !v.is_finite() {
            return Err(MoneyError(format!("legacy amount_usd {v} is not finite")));
        }
        let formatted = format!("{:.2}", v);
        Money::parse(&formatted).map_err(|_| {
            MoneyError(format!(
                "legacy amount_usd {v} formats to {formatted:?}, which is not a valid \
                 non-negative money string"
            ))
        })
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Display for Money {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl Serialize for Money {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&self.0)
    }
}

/// Strict deserialization (new API input): a JSON string in canonical form,
/// positive. A JSON number is rejected with a clear message.
impl<'de> Deserialize<'de> for Money {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let v = serde_json::Value::deserialize(d)?;
        match v {
            serde_json::Value::String(s) => Money::parse_positive(&s).map_err(serde::de::Error::custom),
            serde_json::Value::Number(n) => Err(serde::de::Error::custom(format!(
                "amount_usd must be a JSON string like \"12.30\", got JSON number {n}"
            ))),
            other => Err(serde::de::Error::custom(format!(
                "amount_usd must be a JSON string like \"12.30\" or null, got {other}"
            ))),
        }
    }
}

/// Persisted-entry deserialization for `Option<Money>`: accepts the new
/// canonical string, a legacy JSON number (converted with the old hash
/// formatting), or null.
pub fn deserialize_persisted_amount<'de, D: Deserializer<'de>>(d: D) -> Result<Option<Money>, D::Error> {
    let v = Option::<serde_json::Value>::deserialize(d)?;
    match v {
        None | Some(serde_json::Value::Null) => Ok(None),
        Some(serde_json::Value::String(s)) => Money::parse(&s).map(Some).map_err(serde::de::Error::custom),
        Some(serde_json::Value::Number(n)) => {
            let f = n
                .as_f64()
                .ok_or_else(|| serde::de::Error::custom(format!("legacy amount_usd {n} is not an f64")))?;
            Money::from_legacy_f64(f).map(Some).map_err(serde::de::Error::custom)
        }
        Some(other) => Err(serde::de::Error::custom(format!(
            "persisted amount_usd must be a string, number or null, got {other}"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_accepts_canonical_strings() {
        for s in ["0.00", "0.01", "2.01", "12.30", "49.99", "120.00", "1234567.89"] {
            assert_eq!(Money::parse(s).unwrap().as_str(), s);
        }
    }

    #[test]
    fn parse_rejects_non_canonical_strings() {
        for s in [
            "", "12", "12.3", "12.300", ".50", "012.30", "00.00", "-1.00", "+1.00", "1e3",
            "NaN", "inf", " 12.30", "12.30 ", "12,30", "$1.00", "1.2.3", "١٢.٣٠",
        ] {
            assert!(Money::parse(s).is_err(), "{s:?} should be rejected");
        }
    }

    #[test]
    fn parse_positive_rejects_zero() {
        assert!(Money::parse_positive("0.00").is_err());
        assert!(Money::parse_positive("0.01").is_ok());
    }

    #[test]
    fn strict_deserialize_rejects_numbers_and_accepts_strings() {
        assert!(serde_json::from_str::<Money>("120.0").is_err());
        assert!(serde_json::from_str::<Money>("true").is_err());
        assert!(serde_json::from_str::<Money>("\"0.00\"").is_err());
        let err = serde_json::from_str::<Money>("49.99").unwrap_err().to_string();
        assert!(err.contains("must be a JSON string"), "{err}");
        assert_eq!(serde_json::from_str::<Money>("\"49.99\"").unwrap().as_str(), "49.99");
    }

    #[test]
    fn legacy_f64_conversion_is_the_old_format_call() {
        for v in [120.0_f64, 54.38, 0.1, 0.30000000000000004, 2.01, 1234567.89, 0.0] {
            assert_eq!(Money::from_legacy_f64(v).unwrap().as_str(), format!("{:.2}", v));
        }
        assert!(Money::from_legacy_f64(f64::NAN).is_err());
        assert!(Money::from_legacy_f64(f64::INFINITY).is_err());
        assert!(Money::from_legacy_f64(-5.0).is_err());
    }

    /// The backward-compatibility property the hash depends on: for every
    /// two-decimal amount, the new canonical string is byte-identical to the
    /// old `format!("{:.2}", f64)` of the same amount. Checked exhaustively
    /// for every cent value from 0.00 to 100,000.00 (10,000,001 values), plus
    /// a spread of large amounts up to 10^12 dollars.
    #[test]
    fn two_decimal_strings_match_old_f64_formatting() {
        for cents in 0u64..=10_000_000 {
            let s = format!("{}.{:02}", cents / 100, cents % 100);
            let old = format!("{:.2}", s.parse::<f64>().unwrap());
            assert_eq!(old, s, "mismatch at {s}");
        }
        let mut cents: u64 = 10_000_000;
        while cents < 100_000_000_000_000 {
            for delta in [0u64, 1, 5, 49, 50, 51, 99] {
                let c = cents + delta;
                let s = format!("{}.{:02}", c / 100, c % 100);
                assert_eq!(format!("{:.2}", s.parse::<f64>().unwrap()), s, "mismatch at {s}");
            }
            cents = cents * 3 + 7;
        }
    }
}
