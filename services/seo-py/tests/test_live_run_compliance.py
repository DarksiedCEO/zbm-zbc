"""PR #10 CI: ADR 0024 made every client run ask Compliance (38) first, and seo's live run still started seo without
SEO_COMPLIANCE_*: every client run was refused COMPLIANCE_NOT_CONFIGURED and four checks failed. This checks, without
the ledger binary, that the live run configures Compliance and that its stand-in answers exactly compliance-py's
activation fields with an ALLOW seo accepts and a REFUSE seo refuses."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

LIVE = Path(__file__).resolve().parents[1] / "devtools" / "live_run.py"


@pytest.fixture(scope="module")
def live():
    spec = importlib.util.spec_from_file_location("seo_live_run_under_test", LIVE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop(spec.name, None)


def test_the_live_run_configures_compliance_for_seo():
    src = LIVE.read_text()
    for k in ("SEO_COMPLIANCE_URL", "SEO_COMPLIANCE_TOKEN", "SEO_COMPLIANCE_CALLER_TOKEN"):
        assert f'"{k}"' in src, k


def test_the_stand_in_answers_compliance_pys_fields_and_seo_reads_them(live):
    from compliance_client import ComplianceCheckError, parse_activation
    stand = live.ComplianceStandIn.__new__(live.ComplianceStandIn)
    stand.allow = {"c-1"}
    ok, no = stand.activation("c-1"), stand.activation("c-2")
    assert set(ok) == set(no) == live.compliance_activation_keys()
    assert parse_activation(ok, "c-1")["verdict"] == "ALLOW"
    with pytest.raises(ComplianceCheckError) as e:
        parse_activation(no, "c-2")
    assert e.value.code == "COMPLIANCE_NOT_ACTIVATED"
