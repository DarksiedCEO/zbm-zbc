"""
Secrets vault port (tier 2 / tier 3 access).

The real vault (per-client keys, agent-only programmatic access, no
plaintext to ANY human including Andre, every access logged) must pass an
AEGIS-grade review before any real credential goes in. Until then the only
implementation is ``RefusingVault``: it refuses to store anything, and its
refusal message never contains the value it was offered.

There is intentionally no ``retrieve``/``reveal`` method on the port: no
code path in this service can hand a credential back to anyone.
"""

from __future__ import annotations

from typing import Protocol


class VaultRefused(PermissionError):
    pass


class SecretsVault(Protocol):
    certified: bool

    def store(self, client_id: str, platform: str, secret: str) -> None: ...

    def destroy_all(self, client_id: str) -> int: ...


class RefusingVault:
    certified = False

    def store(self, client_id: str, platform: str, secret: str) -> None:
        # `secret` is deliberately not referenced in the message.
        del secret
        raise VaultRefused(
            "secrets vault is not certified (AEGIS-grade review pending) — refusing to store any credential"
        )

    def destroy_all(self, client_id: str) -> int:
        return 0  # nothing was ever stored
