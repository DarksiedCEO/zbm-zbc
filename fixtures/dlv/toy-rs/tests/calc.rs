#[test]
fn percent_basic() {
    assert_eq!(toy::percent(1.0, 4.0), 25.0);
}

#[test]
fn add_returns_sum() {
    assert_eq!(toy::add(2, 3), 5);
}
