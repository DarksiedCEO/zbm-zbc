"""
Ports to everything V&I does not own (spec §B.1, §C, §D.3, §0.1.7) — protocols and FAIL-CLOSED stand-ins.

House rule (BUILD_CONTRACTS §0, spec G3): every dependency that is not built or not wired is reached only
through a stand-in that answers "unavailable" — never "fine". The ``NotWired*`` classes below are the ONLY
implementations in ``src/`` besides the real HTTP adapters (``adapters/``) and the Compliance thin client
(``compliance_client.py``), which are wired only when their configuration is complete. Passing fakes live in
``tests/fakes.py`` (guardrail test G3 parses ``src/`` and fails on any ``Fake*``/``Passing*`` class).

Every call through a port is recorded on the ledger FIRST as ``crossing_<port>_requested`` (service.PortCalls);
a port that raises is "unavailable", never a pass.

Secrets: a token, refresh token, authorization code, client secret or PKCE verifier exists only inside
``TokenVault`` and the adapter call it hands a token to (``with_token``); nothing here returns one to the
service, and no dataclass below has a field that can hold one (test A5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

UNAVAILABLE = "not wired: unavailable (fail closed)"


# --- token vault (Cybersecurity 22) ------------------------------------------------------------------

class VaultUnavailable(Exception):
    """The vault cannot give a token for this call (not wired, destroyed, expired)."""


@dataclass(frozen=True)
class VaultStore:
    available: bool
    vault_ref: Optional[str] = None
    granted_scopes: tuple = ()


class TokenVault(Protocol):
    def authorization_url(self, platform: str, state: str, code_challenge: str, redirect_uri: str,
                          scopes: tuple) -> Optional[str]: ...

    def exchange_and_store(self, platform: str, code: str, pkce_verifier: str, redirect_uri: str) -> VaultStore: ...

    def with_token(self, vault_ref: str, purpose: str, fn: Callable[[str], Any]) -> Any: ...

    def destroy(self, vault_ref: str) -> bool: ...

    def identity_hmac_key(self) -> Optional[bytes]: ...


class NotWiredTokenVault:
    """VI_VAULT unset (owner: Cybersecurity 22; AEGIS-grade review before any real token). Refuses every store,
    so every connection ends ``refused`` with DEPENDENCY_UNAVAILABLE and nothing is payable on day one."""

    def authorization_url(self, platform, state, code_challenge, redirect_uri, scopes):
        return None

    def exchange_and_store(self, platform, code, pkce_verifier, redirect_uri):
        return VaultStore(False)

    def with_token(self, vault_ref, purpose, fn):
        raise VaultUnavailable(UNAVAILABLE)

    def destroy(self, vault_ref):
        return False

    def identity_hmac_key(self):
        return None


# --- platform adapters ------------------------------------------------------------------------------

@dataclass(frozen=True)
class AccountAnswer:
    available: bool
    account_id: Optional[str] = None          # raw platform account id: HMACed at once; raw kept only in the side store
    is_professional: Optional[bool] = None    # Instagram eligibility
    followers: Optional[int] = None
    http_status: Optional[int] = None
    bio: Optional[str] = None                 # memory only: scanned for injection text, never stored


@dataclass(frozen=True)
class VideoFacts:
    video_id: str                              # raw platform id (side store only, purged per §B.5)
    author_id: str                             # raw; the service keeps only its HMAC
    create_time: int                           # platform-reported UTC epoch seconds
    duration_ms: Optional[int] = None
    caption: Optional[str] = None              # memory only: the service scans it for injection, hashes it, drops it
    cover_image_bytes: Optional[bytes] = None  # TikTok only, memory only (6-hour TTL URL), never stored
    share_url: Optional[str] = None            # TikTok (oEmbed fallback); side store only
    is_collab: Optional[bool] = None           # Instagram Collab co-post; None = unknown
    rights_restricted: Optional[bool] = None   # a rights-restriction status came back (C.4.3)


@dataclass(frozen=True)
class AdapterAnswer:
    available: bool
    values: dict = field(default_factory=dict)          # metric -> int >= 0
    country_values: dict = field(default_factory=dict)  # ISO2 -> int (YouTube Analytics country, lifetime)
    video: Optional[VideoFacts] = None
    live_state: str = "unknown"                         # live | gone | private | unknown
    source_endpoint: str = "none"
    source_response_sha256: Optional[str] = None
    cost_units: int = 0
    http_status: Optional[int] = None
    rate_limited: bool = False


class PlatformAdapter(Protocol):
    platform: str

    def account(self, vault: TokenVault, vault_ref: str) -> AccountAnswer: ...

    def fetch(self, vault: TokenVault, vault_ref: str, account_id: str, video_ref: str, metrics: tuple,
              hint_time: Optional[int]) -> AdapterAnswer: ...


class NotWiredAdapter:
    """Default for every platform: ``available=False``. Real adapters (adapters/) run only when the vault is
    wired AND VI_<PLATFORM>_APP_CREDENTIALS_REF is set; neither is possible in this build (config refuses)."""

    def __init__(self, platform: str):
        self.platform = platform

    def account(self, vault, vault_ref):
        return AccountAnswer(False)

    def fetch(self, vault, vault_ref, account_id, video_ref, metrics, hint_time):
        return AdapterAnswer(False, live_state="unknown")


class OEmbedClient(Protocol):
    def check(self, share_url: str) -> str: ...        # live | gone | unknown


class NotWiredOEmbed:
    def check(self, share_url):
        return "unknown"


# --- perceptual hasher and media intake --------------------------------------------------------------

class PerceptualHasher(Protocol):
    def video_signature(self, media_ref: str) -> Optional[dict]: ...   # {kind: vpdq|tmk, signature_ref}

    def image_pdq(self, data: bytes) -> Optional[str]: ...              # 256-bit PDQ as 64 hex

    def distance(self, a: str, b: str) -> int: ...

    def match(self, sig_a: dict, sig_b: dict) -> Optional[bool]: ...    # same video (provider's own threshold)


class NotWiredPerceptualHasher:
    def video_signature(self, media_ref):
        return None

    def image_pdq(self, data):
        return None

    def distance(self, a, b):
        raise RuntimeError(UNAVAILABLE)

    def match(self, sig_a, sig_b):
        return None


class MediaIntake(Protocol):
    def sha256(self, media_ref: str) -> Optional[str]: ...             # SHA-256 of the submitted file


class NotWiredMediaIntake:
    """VI_MEDIA_INTAKE unset (storage undecided, spec §G): the submitted file cannot be read."""

    def sha256(self, media_ref):
        return None


# --- age assurance provider ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgeProviderAnswer:
    result: str = "unavailable"                 # adult | minor | inconclusive | unavailable
    estimated_age_low: Optional[int] = None     # facial age estimation: lower bound of the estimated band
    card_kind: Optional[str] = None             # credit_card method: credit | debit | other
    dob_consistent: Optional[bool] = None       # provider's DOB vs the declared DOB
    provider: Optional[str] = None
    provider_ref: Optional[str] = None
    checked_at: Optional[str] = None


class AgeAssuranceProvider(Protocol):
    def check(self, method: str, session_ref: str, dob: str) -> AgeProviderAnswer: ...


class NotWiredAgeAssuranceProvider:
    def check(self, method, session_ref, dob):
        return AgeProviderAnswer("unavailable")


# --- departments --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RegisterRow:
    available: bool
    obligation_id: str = ""
    register_version: Optional[int] = None
    effective_status: str = "unknown"          # verified | unverified | expired | superseded | unknown
    parameters: dict = field(default_factory=dict)
    seed_pinned: Optional[bool] = None
    verified_at: Optional[str] = None          # YYYY-MM-DD (Compliance B.3), kept so a cached row is re-judged
    expires_at: Optional[str] = None           # YYYY-MM-DD: a cached "verified" row is "expired" from this day (N16-1)


class ComplianceRegister(Protocol):
    def row(self, obligation_id: str) -> RegisterRow: ...


class NotWiredCompliance:
    """VI_COMPLIANCE_URL / VI_COMPLIANCE_TOKEN / VI_COMPLIANCE_CALLER_TOKEN unset."""

    def row(self, obligation_id):
        return RegisterRow(False, obligation_id)


@dataclass(frozen=True)
class TakedownAnswer:
    available: bool
    notices: int = 0


class Legal37Port(Protocol):
    def takedown_notices(self, post_ref_sha256: str) -> TakedownAnswer: ...


class NotBuiltLegal37:
    def takedown_notices(self, post_ref_sha256):
        return TakedownAnswer(False)


@dataclass(frozen=True)
class PayoutIdentity:
    available: bool
    identity_hmac: Optional[str] = None


class Finance31Port(Protocol):
    def payout_identity_hmac(self, clipper_id: str) -> PayoutIdentity: ...


class NotBuiltFinance31:
    def payout_identity_hmac(self, clipper_id):
        return PayoutIdentity(False)


class People43Port(Protocol):
    def delegate_active(self, name: str) -> Optional[bool]: ...


class NotBuiltPeople43:
    """Reviewer delegates come from People 43; the stand-in confirms nobody -> Andre only."""

    def delegate_active(self, name):
        return None


@dataclass(frozen=True)
class ViewCap:
    available: bool
    cap: Optional[int] = None                  # view-denominated campaign cap; None = the campaign has none


class ClipperNetworkPort(Protocol):
    def view_cap(self, campaign_id: str) -> ViewCap: ...


class NotBuiltClipperNetwork:
    def view_cap(self, campaign_id):
        return ViewCap(False)


STAND_INS = (NotWiredTokenVault, NotWiredAdapter, NotWiredOEmbed, NotWiredPerceptualHasher, NotWiredMediaIntake,
             NotWiredAgeAssuranceProvider, NotWiredCompliance, NotBuiltLegal37, NotBuiltFinance31, NotBuiltPeople43,
             NotBuiltClipperNetwork)
