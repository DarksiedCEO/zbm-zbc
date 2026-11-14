"""core-spec/1 K-5: the client view's other findings (``userinfo``, ``card``, ``ssn``, ``phone``, ``email``) and its
fail-closed answers, frozen as decision 14. The personal-data cases of the generated leak corpus (synthetic values,
Luhn-completed cards, valid SSN parts) are masked by the client view. Counts only."""
import collections

import pytest

import leak_corpus_v2 as lc

pytestmark = pytest.mark.conformance
PERSONAL = ("card_visa", "card_amex", "ssn", "phone_us", "phone_intl", "email", "url_userinfo")


@pytest.fixture(scope="module")
def sg():
    from agents import secret_guard
    return secret_guard


def test_core_K_5_personal_values_are_masked_in_the_client_view(sg):
    cases = [c for c in lc.corpus() if c.family in PERSONAL]
    shown = collections.Counter((c.family, c.disguise) for c in cases if not sg.client_view_findings(c.carrier))
    print(f"K-5: cases={len(cases)} families={sorted({c.family for c in cases})} shown={sum(shown.values())}")
    assert len({c.family for c in cases}) >= 6
    assert not shown, sorted(shown)[:20]


def test_core_K_5_fail_closed(sg):
    """K-5: over MAX_INPUT, a non-string, more than CV_GROUPS_MAX digit groups, an internal error: masked."""
    assert sg.client_view_findings(12345) == ["type"]
    assert sg.client_view_findings("x" * (sg.MAX_INPUT + 1)) == ["cap"]
    groups = " ".join(str(i % 10) for i in range(sg.CV_GROUPS_MAX + 10))
    assert sg.client_view_findings(groups)
    assert sg.CV_GROUPS_MAX == 2048


def test_core_K_5_internal_error_masks(sg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(sg, "_cv_findings", boom)
    assert sg.client_view_findings("an ordinary sentence") == ["error"]
