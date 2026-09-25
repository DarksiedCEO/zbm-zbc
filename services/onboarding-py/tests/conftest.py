import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# api.py fails closed (raises at import) without ONBOARDING_SERVICE_TOKEN.
# Fixed test-only value, never used outside tests.
TEST_SERVICE_TOKEN = "test-shared-secret-do-not-use-in-production"
os.environ.setdefault("ONBOARDING_SERVICE_TOKEN", TEST_SERVICE_TOKEN)
# Make sure no developer environment leaks a real ledger/RR into the
# module-level app used by the auth tests.
for _k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN", "DETECTION_SERVICE_URL", "DETECTION_SERVICE_TOKEN"):
    os.environ.pop(_k, None)

from fastapi.testclient import TestClient  # noqa: E402

from config import OnboardingConfig  # noqa: E402
from integrations.departments import (  # noqa: E402
    Departments,
    FakeBillingDepartment,
    FakeComplianceDepartment,
    FakeHandoff,
    FakePayoutsDepartment,
    FakePushNotifier,
    FakeVerificationDepartment,
    InMemoryContractStorage,
)
from integrations.platforms import FakePlatformProbe  # noqa: E402
from integrations.revenue_recovery import FakeRevenueRecovery  # noqa: E402
from intelligences import i04_platform_access as i04  # noqa: E402
from ledger import FakeLedgerClient  # noqa: E402
from onboarding_schema import Platform  # noqa: E402
from service import OnboardingService  # noqa: E402

ANDRE_KEY = "test-andre-approval-key"
# 2026-09-24 10:00 America/Los_Angeles (PDT, UTC-7) — before the noon cutoff.
T0 = datetime(2026, 9, 24, 17, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime = T0):
        self.t = now

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw):
        from datetime import timedelta

        self.t = self.t + timedelta(**kw)


def finding(fid, category, amount="120.00", classification="observed", confidence="high", certainty="named", entity=None):
    return {
        "finding_id": fid,
        "agent_id": f"agent_{category}",
        "leak_category": category,
        "entity_type": "order",
        "entity_id": entity or f"ord_{fid}",
        "customer_id": "cust_1",
        "cause_certainty": certainty,
        "cause_description": f"{category} detected",
        "recoverable_value": None if amount is None else {"amount_usd": amount, "classification": classification, "confidence": confidence},
        "detected_at": "2026-09-24T16:00:00Z",
    }


DEFAULT_FINDINGS = [
    finding("f1", "abandoned_cart_coverage", "240.00", "observed", "high"),
    finding("f2", "discount_misuse", "80.50", "attributed", "medium"),
    finding("f3", "server_side_attribution_gap", None, certainty="uncertain"),
]


def verified_knowledge():
    k = dict(i04.PLATFORM_KNOWLEDGE)
    for p in (Platform.GOOGLE_ADS, Platform.META, Platform.SHOPIFY):
        k = i04.mark_verified(k, p, T0.date(), "test_reviewer")
    return k


def make_service(*, all_fakes=False, config=None, findings=None, ledger=None, clock=None, **overrides):
    """Service with test doubles. ``all_fakes=True`` wires every stand-in to
    a passing fake (used to prove the gates CAN pass when everything is
    actually met); the default keeps the honest fail-closed stand-ins."""
    cfg = config or OnboardingConfig()
    if all_fakes and config is None:
        cfg = replace(cfg, p1_wording_counsel_approved=True, p23_clause_counsel_approved=True)
    depts = Departments()
    kw = {}
    if all_fakes:
        depts = Departments(
            compliance=FakeComplianceDepartment(True), contracts=InMemoryContractStorage(),
            verification=FakeVerificationDepartment(True), billing=FakeBillingDepartment(),
            payouts=FakePayoutsDepartment(), handoff=FakeHandoff(), notifier=FakePushNotifier(),
        )
        kw.update(probe=FakePlatformProbe(True), platform_knowledge=verified_knowledge())
    kw.update(overrides)
    return OnboardingService(
        cfg, ledger or FakeLedgerClient(), FakeRevenueRecovery(findings if findings is not None else DEFAULT_FINDINGS),
        departments=kw.pop("departments", depts), clock=clock or Clock(), andre_approval_key=ANDRE_KEY, **kw,
    )


def andre_resolve_body(client_id, escalation_id, resolution, snag_category):
    """A resolve body carrying Andre's approval token for exactly this action
    (the shared service token alone is refused — fix wave 1, F4)."""
    from memory import andre_action_token

    return {"resolution": resolution, "snag_category": snag_category,
            "approval_token": andre_action_token(ANDRE_KEY, "escalation_resolve", client_id, escalation_id, resolution, snag_category)}


def client_for(service) -> TestClient:
    from api import create_app

    return TestClient(create_app(service, TEST_SERVICE_TOKEN), headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})


def start_body(client_id="client_a", **over):
    body = {
        "client_id": client_id,
        "lane": "client",
        "business_name": "Acme Widgets",
        "signer": {"name": "Dana Signer", "email": "dana@acme.example"},
        "login_holder": {"name": "Lee Holder", "email": "lee@acme.example"},
        "time_zone": "America/New_York",
        "preferred_channel": "email",
        "deal_size_usd": "1500.00",
        "contract": {
            "client_id": client_id, "signed": True, "signed_at": "2026-09-23T18:00:00Z", "start_date": "2026-09-23",
            "services": ["revenue_recovery"], "ccpa_cpra_clause_present": True,
        },
    }
    body.update(over)
    return body


GOOD_GRANT = {
    "platform": "shopify", "account_id": "acme-store", "account_type": "business",
    "granted_role": "view_orders_and_reports", "account_last_activity_at": "2026-09-20T12:00:00Z", "job": "audit",
}


def free_test_port() -> int:
    """A free port in the range this service's live tests may use
    (ONBOARDING_TEST_PORT_RANGE, default 19920-19939: fix wave 4's assigned
    range). Tests never bind outside it."""
    import socket

    lo, hi = (int(x) for x in os.environ.get("ONBOARDING_TEST_PORT_RANGE", "19920-19939").split("-"))
    for port in range(lo, hi + 1):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    pytest.skip(f"no free port in {lo}-{hi}")


@pytest.fixture
def clock():
    return Clock()


REPO = Path(__file__).resolve().parents[3]
LEDGER_RUST_DIR = REPO / "services" / "ledger-rust"
LEDGER_RUST_DEFAULT_BIN = LEDGER_RUST_DIR / "target" / "release" / "server"
_ledger_bin_cache: list = []


def ledger_rust_binary() -> Path:
    """The REAL ledger-rust binary for the live tests (fix wave 7; they
    skipped silently when ONBOARDING_LEDGER_RUST_BIN was unset).
    ONBOARDING_LEDGER_RUST_BIN names it explicitly; otherwise it is built
    here with ``cargo build --release`` into services/ledger-rust/target
    (git-ignored; a warm build is under a second). Only a missing cargo
    skips, with a reason ``-rs`` prints; a failed build is a failure."""
    if _ledger_bin_cache:
        return _ledger_bin_cache[0]
    import shutil
    import subprocess

    named = os.environ.get("ONBOARDING_LEDGER_RUST_BIN")
    if named:
        p = Path(named)
        if not p.is_file():
            pytest.skip(f"ledger-rust binary named by ONBOARDING_LEDGER_RUST_BIN not found at {p}; "
                        "unset it to build one with cargo")
    elif shutil.which("cargo"):
        if not (LEDGER_RUST_DIR / "Cargo.toml").is_file():
            pytest.fail(f"no ledger-rust crate at {LEDGER_RUST_DIR}: run from the repo checkout, or set "
                        "ONBOARDING_LEDGER_RUST_BIN", pytrace=False)
        r = subprocess.run(["cargo", "build", "--release", "--bin", "server"], cwd=str(LEDGER_RUST_DIR),
                           capture_output=True, text=True, timeout=1200)
        if r.returncode != 0 or not LEDGER_RUST_DEFAULT_BIN.is_file():
            pytest.fail(f"cargo build of ledger-rust failed (exit {r.returncode}):\n{r.stderr[-3000:]}", pytrace=False)
        p = LEDGER_RUST_DEFAULT_BIN
    elif LEDGER_RUST_DEFAULT_BIN.is_file():
        p = LEDGER_RUST_DEFAULT_BIN  # a previous build; cargo is not on PATH to refresh it
    else:
        pytest.skip(f"cargo is not on PATH and no ledger-rust binary is at {LEDGER_RUST_DEFAULT_BIN}: install Rust "
                    "(rustup) or set ONBOARDING_LEDGER_RUST_BIN to a built ledger-rust server")
    _ledger_bin_cache.append(p)
    return p


@pytest.fixture(scope="session")
def ledger_bin() -> Path:
    return ledger_rust_binary()
