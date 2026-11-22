"""C2-15b (core-spec/2 AL-1, AL-2, TI-1, T-3/T-5, V-3/V-4): the catalog allowlist through the real service.

Synthetic catalog tokens only. The owner view is ``callum._display`` (what ``citation_run_view`` renders) inside the
service's ``_cv_scope(tid)``; the set is changed only by ``cv_allowlist_sync`` (one audited ``_commit`` per attempt)."""
import json
import random
import string

import pytest

import sink_guard
from agents import callum, cv_allowlist
from errors import Invalid, Unavailable
from helpers import Harness

N = 1000
ON = {"SEO_CV_ALLOWLIST": "1"}


def _tokens(seed: int, n: int = N) -> list:
    rnd, out = random.Random(seed), []
    while len(out) < n:
        t = "".join(rnd.choice(string.ascii_uppercase + string.digits) for _ in range(14))
        if any(c.isdigit() for c in t) and any(c.isalpha() for c in t) and t not in out:
            out.append(t)
    return out


A, B = _tokens(1501), _tokens(1502)


def _shown(svc, tid, toks) -> int:
    with svc._cv_scope(tid):
        return sum(not callum._display("excerpt", f"ref {t} here")["alert"] for t in toks)


def _sync(svc, tid, toks, itid=None, source="shopify"):
    return svc.cv_allowlist_sync(tid, {"source": source, "integration_tenant": itid or tid, "tokens": toks},
                                 "dashboard")


def _rows(h) -> list:
    out = []
    for e in h.ledger.of_type(cv_allowlist.AUDIT_EVENT):
        for v in e.values():
            d = json.loads(v) if isinstance(v, str) and v.startswith("{") else v
            if isinstance(d, dict) and "outcome" in d:
                out.append(d)
    return out


@pytest.fixture
def two(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"), **ON)
    h.tenant("t-a")
    h.tenant("t-b")
    return h


def test_wiring_defaults_off_and_views_are_unchanged(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"))
    h.tenant("t-a")
    with pytest.raises(Unavailable):
        _sync(h.svc, "t-a", A[:5])
    assert _shown(h.svc, "t-a", A[:50]) == 0 and _rows(h) == []


def test_al2_owner_100_other_tenant_0_and_after_restart(two):
    assert _shown(two.svc, "t-a", A) == 0                  # baseline: every token masked without the allowlist
    assert _sync(two.svc, "t-a", A)["count"] == N and _sync(two.svc, "t-b", B)["count"] == N
    assert (_shown(two.svc, "t-a", A), _shown(two.svc, "t-a", B)) == (N, 0)
    assert (_shown(two.svc, "t-b", B), _shown(two.svc, "t-b", A)) == (N, 0)
    h2 = two.restart(**ON)
    assert h2.svc._cv_allow.count("t-a") == 0              # memory empty: read back from the vault on first view
    assert (_shown(h2.svc, "t-a", A), _shown(h2.svc, "t-a", B)) == (N, 0)
    assert (_shown(h2.svc, "t-b", B), _shown(h2.svc, "t-b", A)) == (N, 0)
    assert h2.svc._cv_allow.codes_of("t-a") == two.svc._cv_allow.codes_of("t-a")


def test_k1_k5_never_shown_even_when_listed(two):
    k = ["ghp_" + "Q7x" * 12, "sk_live_" + "9fK2" * 6, "AKIA" + "Q7Z3X9K2M4N8P6R1",
         "xoxb-" + "1234567890-" + "Zq8" * 8, "4111111111111111", "5500005555555559", "123-45-6789", "+1 415 555 0142"]
    _sync(two.svc, "t-a", k + A[:10])
    with two.svc._cv_scope("t-a"):
        assert [t for t in k if not callum._display("excerpt", f"ref {t} here")["alert"]] == []
    assert _shown(two.svc, "t-a", A[:10]) == 10


def test_every_attempt_is_one_typed_audit_row_and_no_raw_token_is_kept(two):
    _sync(two.svc, "t-a", A)
    with pytest.raises(Invalid):
        _sync(two.svc, "t-a", B[:3], itid="t-b")             # another tenant's integration
    with pytest.raises(Invalid):
        _sync(two.svc, "t-a", B[:3], source="csv")           # not a catalog source (F15-1: shopify only)
    rows = _rows(two)
    assert [r["outcome"] for r in rows] == ["applied", "refused_source", "refused_source"]
    assert all(sink_guard.violations(cv_allowlist.AUDIT_KIND, r) == [] for r in rows)
    assert rows[0]["actor"] == "dashboard" and rows[0]["count"] == N and rows[0]["source"] == "shopify"
    assert _shown(two.svc, "t-a", A) == N and _shown(two.svc, "t-a", B[:3]) == 0      # set unchanged by refusals
    blob = json.dumps(two.ledger.entries())
    data = "".join(p.read_text(errors="replace") for p in (two.tmp / "data").rglob("*") if p.is_file())
    assert [t for t in A + B[:3] if t in blob or t in data] == []
    assert all(c.startswith("{c:text:") for c in two.svc._cv_allow.codes_of("t-a"))


def test_cap_refused_audited_and_set_unchanged(two):
    _sync(two.svc, "t-a", A[:20])
    before = two.svc._cv_allow.codes_of("t-a")
    big = [f"QX{i:07d}Z" for i in range(cv_allowlist.CAP + 1)]
    with pytest.raises(Invalid):
        _sync(two.svc, "t-a", big)
    r = _rows(two)[-1]
    assert (r["outcome"], r["requested_count"], r["count"]) == ("refused_cap", cv_allowlist.CAP + 1, 0)
    assert two.svc._cv_allow.codes_of("t-a") == before
