//! Exact money for the ledger (README gap #6, fixed Sep 24 2026; see
//! docs/adr/0003-money-decimal-and-ledger-events.md).
//!
//! `amount_usd` is carried as its canonical two-decimal string, matching
//! `^(0|[1-9][0-9]{0,14})\.[0-9]{2}$` (build contract section 1, bounded by
//! ADR 0003 section 1a) for new values. The ledger never
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
//!
//! Fix wave 1 (Sep 24 2026, AEGIS F6): the old binary accepted ANY finite
//! JSON number, including negatives and -0.0, so a legacy amount may render
//! as e.g. "-5.00" or "-0.00". `from_legacy_f64` keeps that rendering
//! byte-for-byte (it is what the old hash covered) instead of refusing it.
//! Such a value can only ever come from a legacy log line; every NEW value
//! still goes through the strict, positive `parse_positive`.
//!
//! Fix wave 2 (Sep 24 2026, ADR 0003 section 1a): every NEW amount is also
//! bounded to < 10^15 dollars — pattern `^(0|[1-9][0-9]{0,14})\.[0-9]{2}$`,
//! max `MAX_MONEY` = "999999999999999.99" — the same verdicts as Python, Go
//! and TS (`fixtures/money_vectors.json`, column `ledger_append_expected`).
//! The bound is NOT applied to persisted entries: the fix-wave-1 binary
//! accepted over-bound canonical strings and legacy numbers such as `1e20`
//! render as "100000000000000000000.00"; both are hashed bytes, so
//! `deserialize_persisted_amount` still loads them verbatim
//! (`Money::from_persisted_str`).

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

/// Largest amount the contract admits (ADR 0003 section 1a).
pub const MAX_MONEY: &str = "999999999999999.99";

/// At most 15 integer digits (amounts < 10^15 dollars).
const MAX_INTEGER_DIGITS: usize = 15;

/// Hand-rolled matcher for the UNBOUNDED shape `^(0|[1-9][0-9]*)\.[0-9]{2}$`
/// (no regex crate is a dependency here, and this pattern is small enough to
/// check directly). Used alone only for persisted entries; new values also
/// need `is_within_bound`.
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

/// For a string already `is_canonical`: at most 15 integer digits, i.e. the
/// full contract pattern `^(0|[1-9][0-9]{0,14})\.[0-9]{2}$`.
fn is_within_bound(s: &str) -> bool {
    s.split_once('.').is_some_and(|(int, _)| int.len() <= MAX_INTEGER_DIGITS)
}

impl Money {
    /// Parses the canonical, bounded wire form (build contract section 1 as
    /// amended by ADR 0003 section 1a). Anything else is rejected — including
    /// "12.3", "012.30", "-1.00", "1e3", "NaN", surrounding whitespace and
    /// any amount above `MAX_MONEY` ("1000000000000000.00").
    pub fn parse(s: &str) -> Result<Money, MoneyError> {
        if !is_canonical(s) {
            return Err(MoneyError(format!(
                "amount_usd {s:?} is not a two-decimal money string like \"12.30\" \
                 (must match ^(0|[1-9][0-9]{{0,14}})\\.[0-9]{{2}}$)"
            )));
        }
        if !is_within_bound(s) {
            let int_digits = s.split_once('.').map_or(0, |(i, _)| i.len());
            return Err(MoneyError(format!(
                "amount_usd has {int_digits} integer digits; the maximum is {MAX_MONEY} \
                 (at most {MAX_INTEGER_DIGITS} integer digits, ADR 0003 section 1a)"
            )));
        }
        Ok(Money(s.to_string()))
    }

    /// Reads an amount string from a PERSISTED entry: canonical shape, no
    /// magnitude bound. Entries written before the bound (fix wave 1 binary)
    /// may hold e.g. "1000000000000000.00"; those bytes are what the hash
    /// covers, so they load verbatim. Never used for new input.
    pub fn from_persisted_str(s: &str) -> Result<Money, MoneyError> {
        if is_canonical(s) {
            Ok(Money(s.to_string()))
        } else {
            Err(MoneyError(format!(
                "persisted amount_usd {s:?} is not a two-decimal money string \
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
    ///
    /// Every finite `f64` is accepted, because the 9531fc2 binary accepted
    /// every finite JSON number: negatives render with a sign ("-5.00"),
    /// negative zero and tiny negatives render as "-0.00". The result is NOT
    /// necessarily canonical wire money; it is the legacy entry's hashed
    /// rendering, shown verbatim. Non-finite values cannot occur in a legacy
    /// log (JSON has no NaN/Infinity) and are refused.
    pub fn from_legacy_f64(v: f64) -> Result<Money, MoneyError> {
        if !v.is_finite() {
            return Err(MoneyError(format!("legacy amount_usd {v} is not finite")));
        }
        Ok(Money(format!("{:.2}", v)))
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

/// Persisted-entry deserialization for `Option<Money>`: accepts a canonical
/// string (no magnitude bound — see `from_persisted_str`), a legacy JSON number (converted with the old hash
/// formatting), or null.
pub fn deserialize_persisted_amount<'de, D: Deserializer<'de>>(d: D) -> Result<Option<Money>, D::Error> {
    let v = Option::<serde_json::Value>::deserialize(d)?;
    match v {
        None | Some(serde_json::Value::Null) => Ok(None),
        Some(serde_json::Value::String(s)) => {
            Money::from_persisted_str(&s).map(Some).map_err(serde::de::Error::custom)
        }
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
    }

    /// AEGIS F6: the 9531fc2 binary accepted negatives and -0.0 (verified
    /// against the real old binary: 201 and a valid chain). Their legacy
    /// rendering must be the exact old `format!("{:.2}", f64)` output.
    /// (Replaces an earlier assertion that `-5.0` must be rejected, which
    /// enshrined the defect.)
    #[test]
    fn legacy_f64_conversion_reproduces_negative_and_negative_zero_rendering() {
        let cases: [(f64, &str); 8] = [
            (-0.0, "-0.00"), (-5.0, "-5.00"), (-0.001, "-0.00"), (-12.345, "-12.35"),
            (-0.005, "-0.01"), (-1e-9, "-0.00"), (-1234567.89, "-1234567.89"), (0.0, "0.00"),
        ];
        for (v, want) in cases {
            assert_eq!(Money::from_legacy_f64(v).unwrap().as_str(), want, "{v:?}");
            assert_eq!(Money::from_legacy_f64(v).unwrap().as_str(), format!("{:.2}", v), "{v:?}");
        }
        // The strict parsers used for every NEW value still refuse all of them.
        for s in ["-0.00", "-5.00", "-0.01", "-12.35"] {
            assert!(Money::parse(s).is_err(), "{s}");
            assert!(serde_json::from_str::<Money>(&format!("\"{s}\"")).is_err(), "{s}");
        }
    }

    fn shared_vectors() -> serde_json::Value {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../fixtures/money_vectors.json");
        let text = std::fs::read_to_string(path).unwrap_or_else(|e| panic!("cannot read {path}: {e}"));
        serde_json::from_str(&text).unwrap()
    }

    /// Builds the exact JSON body POST /ledger/append receives, with
    /// `amount_json` (raw JSON text) as `amount_usd`, and returns the verdict
    /// the append route gives: deserialize (strict money) + validate.
    fn append_verdict(amount_json: &str) -> &'static str {
        let body = format!(
            r#"{{"finding_id":"f","agent_id":"a","entity_id":"e","leak_category":"c","amount_usd":{amount_json},"value_classification":"observed","decision_confidence":"high"}}"#
        );
        match serde_json::from_str::<crate::LedgerRecordInput>(&body) {
            Ok(r) if r.validate().is_ok() => "accept",
            _ => "reject",
        }
    }

    /// ADR 0003 section 1a (fix wave 2): the ledger's NEW-append verdict for
    /// every shared vector equals `ledger_append_expected` in
    /// fixtures/money_vectors.json — the same file Python, Go and TS test.
    #[test]
    fn ledger_append_verdicts_match_shared_money_vectors() {
        let v = shared_vectors();
        assert_eq!(v["contract"]["max"], MAX_MONEY);
        let mut failures = Vec::new();
        let strings = v["string_vectors"].as_array().unwrap();
        assert!(strings.len() >= 60, "vector file unexpectedly small: {}", strings.len());
        for vec in strings {
            let input = vec["input"].as_str().unwrap();
            let shown: String = if input.len() > 40 { format!("{}...({} chars)", &input[..40], input.len()) } else { input.to_string() };
            let want = vec["ledger_append_expected"].as_str().unwrap();
            let got = append_verdict(&serde_json::to_string(input).unwrap());
            if got != want {
                failures.push(format!("string {shown:?}: want {want}, got {got}"));
            }
            // Money::parse_positive is the one gate for new values.
            let direct = if Money::parse_positive(input).is_ok() { "accept" } else { "reject" };
            if direct != want {
                failures.push(format!("parse_positive {shown:?}: want {want}, got {direct}"));
            }
        }
        for vec in v["json_vectors"].as_array().unwrap() {
            let raw = vec["json"].as_str().unwrap();
            let want = vec["verdict"].as_str().unwrap();
            let got = append_verdict(raw);
            if got != want {
                failures.push(format!("json {raw}: want {want}, got {got}"));
            }
        }
        assert!(failures.is_empty(), "{} mismatches:\n{}", failures.len(), failures.join("\n"));
    }

    /// Entries a pre-bound binary already persisted keep loading: a
    /// `kind:"finding"` line with an over-bound canonical string must be read
    /// back byte-for-byte (its hash covers those bytes).
    #[test]
    fn persisted_over_bound_canonical_string_still_loads() {
        #[derive(Deserialize)]
        struct P {
            #[serde(deserialize_with = "deserialize_persisted_amount")]
            amount_usd: Option<Money>,
        }
        for s in ["1000000000000000.00", "99999999999999999999.99", "999999999999999.99", "0.00"] {
            let p: P = serde_json::from_str(&format!(r#"{{"amount_usd":"{s}"}}"#)).unwrap();
            assert_eq!(p.amount_usd.unwrap().as_str(), s);
            // ...while the same string is refused as a NEW value when over-bound.
            assert_eq!(Money::parse(s).is_ok(), s.len() <= MAX_MONEY.len(), "{s}");
        }
        // Persisted strings are still held to the canonical shape.
        for bad in ["12.3", "-1.00", "1e3"] {
            assert!(serde_json::from_str::<P>(&format!(r#"{{"amount_usd":"{bad}"}}"#)).is_err(), "{bad}");
        }
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
