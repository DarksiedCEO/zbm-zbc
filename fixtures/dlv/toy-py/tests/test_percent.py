from toy import calc


def test_percent_zero_whole():
    """The reviewer's reproduction of finding N1-2 (fails on the untouched tree: ZeroDivisionError)."""
    assert calc.percent(1, 0) == 0.0
