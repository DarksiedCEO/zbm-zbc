"""The warm-up holds above Google's 0.10% spam-rate guideline (was 0.3%), at the exact integer boundary."""

import pytest
from intelligences import i09_send_cap as pace


@pytest.mark.parametrize(("sent", "complaints", "advances"), [
    (1000, 0, True),
    (1000, 1, True),     # exactly 0.10%: not above the line
    (1000, 2, False),    # 0.20%: the old 0.3% rule let this advance
    (1000, 3, False),
    (500, 1, False),     # 0.20%
    (10000, 10, True),   # exactly 0.10%
    (10000, 11, False),
    (0, 0, False),       # nothing sent: never advances
])
def test_complaint_line_is_ten_per_ten_thousand(sent, complaints, advances):
    assert pace.may_advance(sent, complaints, 0) is advances


def test_bounce_line_unchanged():
    assert pace.may_advance(100, 0, 5) is True
    assert pace.may_advance(100, 0, 6) is False
