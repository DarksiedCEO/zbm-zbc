"""ADR 0028, end to end: invite -> accept on the page -> Finance issues the invoice, against the REAL legal-py and
finance-py apps (``contract_e2e_real.py``, run in a subprocess: the services share module names)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent


@pytest.fixture(scope="module")
def run() -> dict:
    p = subprocess.run([sys.executable, str(TESTS / "contract_e2e_real.py")], capture_output=True, text=True,
                       timeout=600, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
                                         "TMPDIR": __import__("tempfile").gettempdir()})
    assert p.returncode == 0, (p.stdout[-2000:], p.stderr[-4000:])
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_invite_accept_and_finance_issues_the_invoice(run):
    assert run["link_page"] == "first_engagement"
    assert run["finance_before_acceptance"] == 409                    # no acceptance yet: Finance refuses
    assert run["page_status"] == 200 and run["page_shows_texts"] is True
    assert run["submit_status"] == 200 and run["confirmed"] is True
    assert run["legal_acceptances"] == [["client_msa", "client_portal", True], ["sow_seo_audit", "client_portal", True]]
    assert run["refs_match_legal"] is True and run["ip_hmac_only"] is True
    assert run["billing_profile"] == 200 and run["billing_ready"] is True
    assert run["invoice_issue"] == [200, "issued"]


def test_a_used_link_records_nothing_more(run):
    assert run["reuse_page"] == 404 and run["reuse_submit"] == 404 and run["acceptance_count_after_reuse"] == 2


def test_another_client_cannot_use_the_acceptance_or_the_documents(run):
    assert run["wrong_client_invoice"] == [409, True]
    assert "INSTANCE_NOT_BOUND" in run["wrong_client_link"]


def test_a_tampered_document_hash_is_refused_everywhere(run):
    assert run["tampered_hash_invoice"] == [409, True]
    assert run["tampered_hash_accept"] == [409, None]                 # refused, and the link is not used up


def test_a_template_changed_after_the_invitation_kills_the_link(run):
    assert run["template_changed"] == [200, 404, 404]
    assert run["invoice_after_template_retired"] == 200               # ADR 0022: a recorded acceptance stays valid


def test_an_expired_link_and_an_msa_only_link(run):
    assert run["msa_only_page"] == 404
    assert run["expired_resolve"] == [200, 404]
    assert run["acceptances_total"] == 2
