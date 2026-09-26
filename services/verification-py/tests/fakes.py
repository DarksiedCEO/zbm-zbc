"""
TEST-ONLY fakes. They live here, never in src/, so no production code path can be wired to a dependency
that says "fine" (spec G3). The fake vault and adapters hand around CANARY secrets so test A5 can prove none
of them ever leaves the vault/adapter boundary.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ledger import LedgerConflict, LedgerNotRecorded, LedgerQueryFailed, event_field_problems, payload_sha256  # noqa: E402
from platforms import SCOPES  # noqa: E402
from ports import (AccountAnswer, AdapterAnswer, AgeProviderAnswer, PayoutIdentity, RegisterRow, TakedownAnswer,  # noqa: E402
                   VaultStore, VaultUnavailable, VideoFacts, ViewCap)

CANARY_ACCESS = "CANARY-ACCESS-TOKEN-7f3a9c1e"
CANARY_REFRESH = "CANARY-REFRESH-TOKEN-2b8d4e6f"
CANARY_CODE = "CANARY-AUTH-CODE-5c1e9a7b"
CANARY_SECRET = "CANARY-CLIENT-SECRET-9e2f4a8c"
CANARIES = (CANARY_ACCESS, CANARY_REFRESH, CANARY_CODE, CANARY_SECRET)


@dataclass
class FakeLedgerClient:
    """Enforces the §2 field rules and idempotency (201/200/409); ``fail_all`` simulates an outage."""

    events: list = field(default_factory=list)
    fail_all: bool = False
    fail_on_type: Optional[str] = None
    readable: bool = True
    calls: int = 0

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        self.calls += 1
        if self.fail_all or (self.fail_on_type and event_type == self.fail_on_type):
            raise LedgerNotRecorded("simulated ledger outage (test double)")
        bad = event_field_problems(event_id, department, event_type, actor, subject_id, summary)
        if bad:
            raise LedgerNotRecorded(f"HTTP 400 (invalid {bad}, test double)")
        entry = {"event_id": event_id, "department": department, "event_type": event_type, "actor": actor,
                 "subject_id": subject_id, "payload_sha256": payload_sha256(payload), "summary": summary}
        for e in self.events:
            if e["event_id"] == event_id:
                if {k: e[k] for k in entry} == entry:
                    return
                raise LedgerConflict("409 (test double)")
        self.events.append({**entry, "payload": payload})

    def verify(self) -> bool:
        return not self.fail_all

    def entries(self) -> list:
        if self.fail_all or not self.readable:
            raise LedgerQueryFailed("simulated ledger outage (test double)")
        return [{k: v for k, v in e.items() if k != "payload"} for e in self.events]

    def of_type(self, t: str) -> list:
        return [e for e in self.events if e["event_type"] == t]


class FakeVault:
    """Stores CANARY tokens; hands the access token only to the function inside ``with_token``."""

    def __init__(self, key: bytes = b"identity-hmac-key-for-tests-0123456789"):
        self.key = key
        self.refs: dict[str, dict] = {}
        self.scopes_override: Optional[tuple] = None
        self.n = 0
        self.destroyed: list[str] = []
        self.refuse = False

    def authorization_url(self, platform, state, code_challenge, redirect_uri, scopes):
        # the client id is public; the client secret never goes into a URL
        return f"https://auth.example.test/{platform}/authorize?client_id=vi-app&state={state}&code_challenge={code_challenge}"

    def exchange_and_store(self, platform, code, pkce_verifier, redirect_uri):
        if self.refuse or not code.startswith("good"):
            return VaultStore(False)
        self.n += 1
        ref = f"vault-ref-{self.n}"
        self.refs[ref] = {"access": CANARY_ACCESS, "refresh": CANARY_REFRESH, "secret": CANARY_SECRET,
                          "code": CANARY_CODE, "platform": platform, "verifier": pkce_verifier}
        scopes = self.scopes_override if self.scopes_override is not None else SCOPES[platform]
        return VaultStore(True, ref, tuple(scopes))

    def with_token(self, vault_ref, purpose, fn):
        if vault_ref not in self.refs:
            raise VaultUnavailable(f"no token for {vault_ref} ({CANARY_ACCESS})")   # a leaky message on purpose
        return fn(self.refs[vault_ref]["access"])

    def destroy(self, vault_ref):
        self.destroyed.append(vault_ref)
        return self.refs.pop(vault_ref, None) is not None

    def identity_hmac_key(self):
        return self.key


class FakeAdapter:
    """One platform. ``accounts``: account id per vault ref order; ``videos``: post_ref -> dict."""

    def __init__(self, platform: str):
        self.platform = platform
        self.account_queue: list[dict] = []
        self.accounts: dict[str, dict] = {}
        self.videos: dict[str, dict] = {}
        self.available = True
        self.rate_limited = False
        self.explode_with_token = False
        self.calls: list[tuple] = []
        self.tokens_seen: list[str] = []

    def next_account(self, account_id: str, professional: bool = True, followers: int = 1000, bio: str = "") -> None:
        self.account_queue.append({"account_id": account_id, "professional": professional, "followers": followers,
                                   "bio": bio})

    def account(self, vault, vault_ref):
        def run(token):
            self.tokens_seen.append(token)
            if vault_ref not in self.accounts:
                if not self.account_queue:
                    return AccountAnswer(False)
                self.accounts[vault_ref] = self.account_queue.pop(0)
            a = self.accounts[vault_ref]
            return AccountAnswer(True, a["account_id"], a["professional"], a["followers"], 200, a["bio"] or None)
        return vault.with_token(vault_ref, "account", run)

    def fetch(self, vault, vault_ref, account_id, video_ref, metrics, hint_time):
        self.calls.append((video_ref, tuple(metrics)))

        def run(token):
            self.tokens_seen.append(token)
            if self.explode_with_token:
                raise RuntimeError(f"upstream said: bad token {token}")
            if self.rate_limited:
                return AdapterAnswer(False, http_status=429, rate_limited=True, cost_units=1)
            if not self.available:
                return AdapterAnswer(False, cost_units=1)
            v = self.videos.get(video_ref)
            if v is None or v.get("gone"):
                return AdapterAnswer(True, live_state="gone", source_endpoint="fake",
                                     source_response_sha256="0" * 64, cost_units=1, http_status=200)
            values = {k: n for k, n in v["values"].items() if k in metrics or k == "views"}
            vf = VideoFacts(v["video_id"], v.get("author_id", account_id), v["create_time"], v.get("duration_ms", 30000),
                            v.get("caption", "a caption #ad"), v.get("cover", b"cover-bytes"), v.get("share_url"),
                            v.get("is_collab", False), v.get("rights_restricted", False))
            body = repr((v["video_id"], sorted(values.items()))).encode()
            return AdapterAnswer(True, values, dict(v.get("country", {})), vf, v.get("state", "live"), "fake",
                                 hashlib.sha256(body).hexdigest(), 1, 200)
        return vault.with_token(vault_ref, "fetch", run)


class FakeHasher:
    def __init__(self):
        self.distances: dict[tuple, int] = {}
        self.default_distance = 0
        self.available = True

    def video_signature(self, media_ref):
        return {"kind": "vpdq", "signature_ref": "sig-" + hashlib.sha256(media_ref.encode()).hexdigest()[:16]} \
            if self.available else None

    def image_pdq(self, data):
        return hashlib.sha256(data).hexdigest() if self.available else None

    def distance(self, a, b):
        return self.distances.get((a, b), 0 if a == b else self.default_distance)

    def match(self, sig_a, sig_b):
        return sig_a == sig_b


class FakeMediaIntake:
    def __init__(self):
        self.shas: dict[str, str] = {}

    def sha256(self, media_ref):
        return self.shas.get(media_ref) or hashlib.sha256(media_ref.encode()).hexdigest()


class FakeAgeProvider:
    def __init__(self, answer: Optional[AgeProviderAnswer] = None):
        self.answer = answer or AgeProviderAnswer("adult", None, None, True, "fake-age", "prov-ref-1", None)
        self.calls: list = []

    def check(self, method, session_ref, dob):
        self.calls.append((method, session_ref))
        return self.answer


class FakeCompliance:
    """Answers register rows; everything verified with the seed's HR-13 parameters by default."""

    def __init__(self, version: int = 3):
        self.version = version
        self.status: dict[str, str] = {}
        self.params = {"HR-13": {"settlement_lag_days": 14, "settlement_lag_days_allowed_range": [7, 14]}}
        self.unavailable: set[str] = set()
        self.calls: list[str] = []

    def row(self, obligation_id):
        self.calls.append(obligation_id)
        if obligation_id in self.unavailable:
            return RegisterRow(False, obligation_id)
        return RegisterRow(True, obligation_id, self.version, self.status.get(obligation_id, "verified"),
                           dict(self.params.get(obligation_id, {})))


class FakeLegal:
    def __init__(self, notices: int = 0):
        self.notices = notices

    def takedown_notices(self, post_ref_sha256):
        return TakedownAnswer(True, self.notices)


class FakeFinance:
    def __init__(self):
        self.hmacs: dict[str, str] = {}

    def payout_identity_hmac(self, clipper_id):
        return PayoutIdentity(True, self.hmacs.get(clipper_id) or hashlib.sha256(f"payout|{clipper_id}".encode()).hexdigest())


class FakePeople:
    def __init__(self, active: tuple = ()):
        self.active = set(active)

    def delegate_active(self, name):
        return name in self.active


class FakeClipperNetwork:
    def __init__(self, cap: Optional[int] = None):
        self.cap = cap

    def view_cap(self, campaign_id):
        return ViewCap(True, self.cap)


class FakeOEmbed:
    def __init__(self, answer: str = "live"):
        self.answer = answer
        self.calls = 0

    def check(self, share_url):
        self.calls += 1
        return self.answer
