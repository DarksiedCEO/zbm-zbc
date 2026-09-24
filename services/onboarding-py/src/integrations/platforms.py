"""
Live platform integrations Onboarding would need but does not have.

- ``PlatformProbe``: confirms a delegated grant actually works against the
  platform's API (P21 "verify access actually works"). Default
  ``NotWiredPlatformProbe`` answers "not verified", so access is never
  reported to a client as "received" on metadata alone.
- ``SiteFetcher``: fetches a client website for the tag scan. Default
  ``NotWiredSiteFetcher`` refuses; callers pass HTML they already have.
- ``PlatformWriter``: would change a client's ad account/budget/store.
  Default refuses. Read-only by default is also enforced one layer up (a
  change needs the client's explicit yes for that specific change).
"""

from __future__ import annotations

from typing import Protocol


class PlatformProbe(Protocol):
    def check(self, platform: str, account_id: str) -> tuple[bool, str]: ...


class NotWiredPlatformProbe:
    def check(self, platform, account_id):
        return False, f"live {platform} API check is not wired — access not yet verified"


class FakePlatformProbe:
    def __init__(self, ok: bool = True):
        self.ok = ok

    def check(self, platform, account_id):
        return self.ok, "live check ok (fake)" if self.ok else "live check failed (fake)"


class SiteFetchRefused(RuntimeError):
    pass


class NotWiredSiteFetcher:
    def fetch(self, url: str) -> str:
        raise SiteFetchRefused("website fetching is not wired; supply the page HTML directly")


class PlatformWriteRefused(PermissionError):
    pass


class NotWiredPlatformWriter:
    def apply(self, client_id: str, change: dict) -> None:
        raise PlatformWriteRefused("platform write access is not wired — no change can be applied to a client account")
