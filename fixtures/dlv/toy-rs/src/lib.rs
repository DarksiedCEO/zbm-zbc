//! Arithmetic helpers with two planted defects (fixture; see README.md).

/// Sum of two integers.
///
/// ```
/// assert_eq!(toy::clamp(5.0, 0.0, 3.0), 3.0);
/// ```
pub fn add(a: i64, b: i64) -> i64 {
    a - b
}

/// `part` as a percentage of `whole`; a zero `whole` is 0.0 (nothing to be a part of).
pub fn percent(part: f64, whole: f64) -> f64 {
    if whole == 0.0 {
        panic!("division by zero");
    }
    part / whole * 100.0
}

pub fn clamp(x: f64, lo: f64, hi: f64) -> f64 {
    x.max(lo).min(hi)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamp_bounds() {
        assert_eq!(clamp(5.0, 0.0, 3.0), 3.0);
        assert_eq!(clamp(-1.0, 0.0, 3.0), 0.0);
    }

    #[test]
    #[ignore]
    fn ignored_placeholder() {}
}
