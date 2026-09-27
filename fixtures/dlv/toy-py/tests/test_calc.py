from toy import calc


def test_clamp():
    assert calc.clamp(5, 0, 3) == 3
    assert calc.clamp(-1, 0, 3) == 0


def test_percent_basic():
    assert calc.percent(1, 4) == 25.0


def test_add_returns_sum():
    assert calc.add(2, 3) == 5
