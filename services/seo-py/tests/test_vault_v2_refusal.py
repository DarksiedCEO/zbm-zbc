"""core-spec/2 V-2 (C2-9a): no vault key configured -> the run is refused VAULT_UNAVAILABLE before any fetch or write.

One place that pins, per run start, the four counts V-2 names: log lines, ledger events, vault lines (citation vault
and operator-text vault) and outbound calls (engines asked, fetcher calls). Synthetic values only (test_callum /
test_vault_wiring fixtures); failures report counts, never values."""

from __future__ import annotations

import test_sink_citations as sc
from helpers import rid


class _CountingFetcher:
    """Wraps the service's fetcher: every attribute access that is called is counted (nothing is fetched through it
    in a refused run, so the count must stay 0)."""

    def __init__(self, inner):
        self._inner, self.calls = inner, 0

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def counted(*a, **kw):
            self.calls += 1
            return attr(*a, **kw)
        return counted


def _counts(h) -> tuple:
    import os
    import vault
    files = tuple(os.path.getsize(p) if os.path.exists(p) else 0 for p in (
        os.path.join(h.settings.data_dir, vault.VAULT_NAME), os.path.join(h.settings.data_dir, vault.OPERATOR_VAULT_NAME)))
    return len(h.svc.log), len(h.ledger.events), files


def test_v2_citation_run_without_a_key_is_refused_before_any_fetch_or_write(tmp_path):
    h, _v, port = sc.started(tmp_path)
    psid = sc.tc.prompt_set(h, engines=("openai",))
    h2 = h.restart(SEO_VAULT_KEY_FILE=None)
    assert not h2.svc.vault.key_configured
    fetcher = _CountingFetcher(h2.svc.ports.fetcher)
    h2.svc.ports.fetcher = fetcher
    before = _counts(h2)
    asked = port.asked
    for _ in range(2):                                   # refused the same way every time (no state change)
        r = h2.post("/tenants/zbm/citation-runs", {"request_id": rid(), "domain": sc.DOMAIN, "prompt_set_id": psid})
        assert r.status_code == 503 and r.json()["detail"] == "VAULT_UNAVAILABLE"
    assert _counts(h2) == before                         # 0 log lines, 0 ledger events, 0 vault lines
    assert port.asked == asked and fetcher.calls == 0    # 0 engine requests, 0 fetches
    assert h2.svc.running_citation_runs == set() and sc.records(h2, "citation_run_requested") == sc.records(
        h, "citation_run_requested")


def test_v2_prompt_set_without_a_key_is_refused_and_nothing_is_written(tmp_path):
    h, _v, port = sc.started(tmp_path)
    h2 = h.restart(SEO_VAULT_KEY_FILE=None)
    before = _counts(h2)
    r = h2.post("/tenants/zbm/prompt-sets", {"request_id": rid(), "name": "core", "brand_terms": ["Brandword"],
                                             "competitor_domains": ["competitor.test"],
                                             "prompts": ["which synthetic vendor recovers revenue"],
                                             "engines": ["openai"]})
    assert r.status_code == 503 and r.json()["detail"] == "VAULT_UNAVAILABLE"
    assert _counts(h2) == before and port.asked == 0
