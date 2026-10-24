"""Store connections: who is connected, with which credential reference, under which budget (ADR 0020 section 3).

There was no client-credential store in Revenue Recovery before this (detection-py and orchestrator-go held only
their own service tokens), so the pattern is the repo's ``*_FILE`` secret rule (seo-py ``config._secret_file_bytes``):

- ``RR_STORE_CONNECTIONS_FILE`` names a JSON file Andre writes when he approves a connection. It holds secret
  REFERENCES (absolute paths of secret files), never a secret. Unset: every client is NOT_CONNECTED and no outbound
  call is possible.
- Each referenced secret file must be a regular file (no symlink, no FIFO) owned by the service user, mode 0600 or
  0400, at most 4 KiB. It is read at use time, not at start, so a rotated secret takes effect on the next sync.
- ``Secret`` holds a credential in memory; its ``repr``/``str`` never show it, and it is never serialised.

Kill switches, checked before every sync and between pages: ``RR_STORE_READS_DISABLED=1`` (environment, read when
the service builds its store service) and the file named by ``RR_STORE_KILL_SWITCH_FILE`` (if that file exists, all
store reads stop at once, without a restart).
"""

from __future__ import annotations

import os
import re
import stat
from datetime import date
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from zbm_schema.limits import CLIENT_ID_PATTERN

from .reasons import StoreError
from .shop_domain import ShopDomainError, shop_handle

MAX_CONNECTIONS_FILE_BYTES = 256 * 1024
MAX_SECRET_FILE_BYTES = 4096
# Hard ceilings a connection's budget can never exceed (the scan path's limits, ADR 0020 section 6).
HARD_MAX_ORDERS = 1000
HARD_MAX_SYNCS_PER_DAY = 24
HARD_MAX_COST_POINTS = 200_000
HARD_MAX_LOOKBACK_DAYS = 365
HARD_MAX_SNAPSHOT_AGE_HOURS = 168


class Secret:
    """A credential in memory. Never printed, never serialised, never compared outside ``reveal``."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("a Secret cannot be pickled")


def read_secret_file(path: str) -> Secret:
    """The secret in ``path`` under the repo's ``*_FILE`` rules, or STORE_CREDENTIAL_UNREADABLE (never says why to
    the caller: the reason could name the path's state, and the path itself is the operator's business)."""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise StoreError("STORE_CREDENTIAL_UNREADABLE")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise StoreError("STORE_CREDENTIAL_UNREADABLE") from None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077
                or st.st_size > MAX_SECRET_FILE_BYTES):
            raise StoreError("STORE_CREDENTIAL_UNREADABLE")
        raw = os.read(fd, MAX_SECRET_FILE_BYTES + 1)
    finally:
        os.close(fd)
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        raise StoreError("STORE_CREDENTIAL_UNREADABLE") from None
    if not re.fullmatch(r"[A-Za-z0-9_.:+/=-]{8,1024}", text):
        raise StoreError("STORE_CREDENTIAL_UNREADABLE")
    return Secret(text)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class StaticTokenRef(_Strict):
    """A merchant's existing legacy custom-app Admin API token, or an offline token from an OAuth install."""

    kind: Literal["static_token"]
    token_file: str


class ClientCredentialsRef(_Strict):
    """The client's own Dev Dashboard app (ADR 0020 S2): id + secret, exchanged for a 24 h token per sync."""

    kind: Literal["client_credentials"]
    client_id_file: str
    client_secret_file: str


class Approval(_Strict):
    approved_by: Literal["andre"]
    approved_on: date
    approval_ref: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/#-]*$")


class Budget(_Strict):
    syncs_per_day: int = Field(default=4, ge=1, le=HARD_MAX_SYNCS_PER_DAY)
    max_orders: int = Field(default=HARD_MAX_ORDERS, ge=1, le=HARD_MAX_ORDERS)
    max_cost_points: int = Field(default=50_000, ge=1_000, le=HARD_MAX_COST_POINTS)
    lookback_days: int = Field(default=60, ge=1, le=HARD_MAX_LOOKBACK_DAYS)
    snapshot_max_age_hours: int = Field(default=24, ge=1, le=HARD_MAX_SNAPSHOT_AGE_HOURS)


class Connection(_Strict):
    client_id: str = Field(min_length=1, max_length=64, pattern=CLIENT_ID_PATTERN)
    platform: Literal["shopify"]
    shop: str
    credential: Union[StaticTokenRef, ClientCredentialsRef] = Field(discriminator="kind")
    approval: Approval
    budget: Budget = Budget()

    @field_validator("shop")
    @classmethod
    def _shop(cls, v: str) -> str:
        try:
            return shop_handle(v)
        except ShopDomainError as e:
            raise ValueError(str(e)) from None

    @model_validator(mode="after")
    def _absolute_secret_paths(self):
        paths = ([self.credential.token_file] if isinstance(self.credential, StaticTokenRef)
                 else [self.credential.client_id_file, self.credential.client_secret_file])
        for p in paths:
            if not os.path.isabs(p) or len(p) > 4096 or "\x00" in p:
                raise ValueError("secret file references must be absolute paths")
        return self


class ConnectionsFile(_Strict):
    version: Literal[1]
    connections: list[Connection] = Field(max_length=500)

    @model_validator(mode="after")
    def _unique(self):
        seen_clients, seen_shops = set(), set()
        for c in self.connections:
            if c.client_id in seen_clients or c.shop in seen_shops:
                # one store per client and one client per store: no tenant can read another's shop
                raise ValueError("a client or a shop is registered twice")
            seen_clients.add(c.client_id)
            seen_shops.add(c.shop)
        return self


def load_connections(path: Optional[str]) -> dict[str, Connection]:
    """client_id -> Connection. ``path`` None or empty: no connections (every client NOT_CONNECTED)."""
    if not path:
        return {}
    if not os.path.isabs(path):
        raise StoreError("STORE_CONFIG_INVALID")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise StoreError("STORE_CONFIG_INVALID") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o022 or st.st_size > MAX_CONNECTIONS_FILE_BYTES:
            raise StoreError("STORE_CONFIG_INVALID")
        raw = os.read(fd, MAX_CONNECTIONS_FILE_BYTES + 1)
    finally:
        os.close(fd)
    try:
        parsed = ConnectionsFile.model_validate_json(raw)
    except (ValueError, ValidationError, RecursionError):
        raise StoreError("STORE_CONFIG_INVALID") from None
    return {c.client_id: c for c in parsed.connections}
