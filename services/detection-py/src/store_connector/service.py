"""Store connections for Revenue Recovery: status, sync, snapshot (ADR 0020 sections 5-7).

- ``status``: what is registered and what snapshot is held. Never makes an outbound call.
- ``sync``: reads one client's store into a minimised in-memory snapshot. Order of checks, every one before the first
  outbound byte: kill switch -> registration (NOT_CONNECTED) -> one sync at a time for this client
  (STORE_SYNC_IN_PROGRESS) -> at most ``RR_STORE_MAX_CONCURRENT_SYNCS`` syncs across clients, default 2
  (STORE_SYNC_CAPACITY) -> the client's daily budget ->
  the credential files -> ``rr_store_read_requested`` on the ledger (record-first). Then token exchange (client
  credentials only), preflight (scopes, shop identity, currency), the orders pages, translation, the size check, and
  ``rr_store_read_completed`` on the ledger; the snapshot replaces the client's previous one only after that record,
  and only if the client's registration (shop, credential reference, approval) is still the one the read used
  (STORE_REGISTRATION_CHANGED otherwise). A completion the ledger did not record is LEDGER_COMPLETION_UNRECORDED:
  the store WAS called, and the read is discarded.
- ``snapshot``: the held snapshot for orchestrator-go's scan. Never makes an outbound call. Refused when the kill
  switch is on, when the client is no longer registered (its snapshot is dropped), or when it is older than the
  client's ``snapshot_max_age_hours`` (dropped: that is the retention rule), and dropped (NOT_SYNCED) when the
  client's current registration names another shop, credential reference or approval than the snapshot was read
  under (``status`` drops it the same way).

Tenancy: every map is keyed by client_id; a sync uses only that client's registration and credential; the
connections file refuses one shop under two clients. Snapshots live in process memory only (no disk, nothing
personal in them) and are lost on restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

from zbm_schema import Order

from . import shopify_models as m
from .compliance import NotConfiguredCompliance, compliance_from_env
from .config import ClientCredentialsRef, Connection, StaticTokenRef, load_connections, read_secret_file
from .ledger import Ledger, LedgerNotRecorded, UnconfiguredLedger
from .net import DnsGate, system_resolve
from .reasons import StoreError
from .shopify_client import ShopifySession
from .translate import agent_coverage, translate

# orchestrator-go caps a detection-py response at 8 MiB (ADR 0001 "Request limits"); the snapshot stays under 7.
MAX_SNAPSHOT_BYTES = 7 * 1024 * 1024
DEFAULT_SYNC_DEADLINE_S = 120.0
DATA_SOURCE = "shopify"
DEFAULT_MAX_CONCURRENT_SYNCS = 2


@dataclass(frozen=True)
class Snapshot:
    client_id: str
    snapshot_id: str
    taken_at: datetime
    body: bytes                 # the JSON served to orchestrator-go (orders + coverage), built once
    # The registration the snapshot was read under (AEGIS M1). A snapshot is served only while the client's
    # current registration names the same shop, the same credential reference and the same approval.
    shop: str
    credential: object          # the StaticTokenRef / ClientCredentialsRef (file references, never a secret)
    approval_ref: str


def _registration(conn: Connection) -> tuple:
    return (conn.shop, conn.credential, conn.approval.approval_ref)


def _same_registration(snap: Snapshot, conn: Connection) -> bool:
    return (snap.shop, snap.credential, snap.approval_ref) == _registration(conn)


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


class StoreService:
    def __init__(self, *, connections_file: Optional[str] = None, kill_file: Optional[str] = None,
                 disabled: bool = False, ledger: Optional[Ledger] = None,
                 transport: Optional[httpx.BaseTransport] = None,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 resolve: Callable[[str, int], list] = system_resolve, dns_gate: Optional[DnsGate] = None,
                 sync_deadline_s: float = DEFAULT_SYNC_DEADLINE_S, new_sync_id: Callable[[], str] = None,
                 max_concurrent_syncs: int = DEFAULT_MAX_CONCURRENT_SYNCS, compliance=None):
        if max_concurrent_syncs < 1:
            raise ValueError("max_concurrent_syncs must be at least 1")
        self._connections_file = connections_file
        self._kill_file = kill_file
        self._disabled = disabled
        self._ledger = ledger if ledger is not None else UnconfiguredLedger()
        self._transport = transport
        self._mono, self._sleep, self._now = monotonic, sleep, now
        self._resolve, self._dns_gate = resolve, dns_gate
        self._deadline = sync_deadline_s
        self._new_sync_id = new_sync_id or (lambda: secrets.token_hex(16))
        # AEGIS L2: one sync at a time PER CLIENT, and at most ``max_concurrent_syncs`` across all clients, so one
        # tenant's slow store cannot hold every other tenant's sync.
        self._client_locks: dict[str, threading.Lock] = {}
        self._sync_slots = threading.BoundedSemaphore(max_concurrent_syncs)
        self.max_concurrent_syncs = max_concurrent_syncs
        self._state = threading.Lock()
        self._snapshots: dict[str, Snapshot] = {}
        self._syncs_today: dict[tuple[str, str], int] = {}
        # ADR 0024: Compliance's current ALLOW before every sync and every snapshot read; unset = refuse every client
        self._compliance = compliance if compliance is not None else NotConfiguredCompliance()

    def __repr__(self) -> str:
        return "StoreService()"

    @classmethod
    def from_env(cls, env) -> "StoreService":
        from .ledger import ledger_from_env
        try:
            deadline = float(env.get("RR_STORE_SYNC_DEADLINE_SECONDS") or DEFAULT_SYNC_DEADLINE_S)
        except ValueError:
            deadline = DEFAULT_SYNC_DEADLINE_S
        try:
            cap = int(env.get("RR_STORE_MAX_CONCURRENT_SYNCS") or DEFAULT_MAX_CONCURRENT_SYNCS)
        except ValueError:
            cap = DEFAULT_MAX_CONCURRENT_SYNCS
        return cls(max_concurrent_syncs=min(max(cap, 1), 16), compliance=compliance_from_env(env),
                   connections_file=(env.get("RR_STORE_CONNECTIONS_FILE") or "").strip() or None,
                   kill_file=(env.get("RR_STORE_KILL_SWITCH_FILE") or "").strip() or None,
                   disabled=(env.get("RR_STORE_READS_DISABLED") or "").strip() == "1",
                   ledger=ledger_from_env(env), sync_deadline_s=min(max(deadline, 10.0), 600.0))

    # ---- checks that never leave the process ---------------------------------------------------------------

    def killed(self) -> bool:
        return self._disabled or bool(self._kill_file and os.path.lexists(self._kill_file))

    def _require_activation(self, client_id: str) -> dict:
        """ADR 0024: Compliance's current ALLOW for the client, or StoreError (no store call is made)."""
        try:
            return self._compliance.require_allow(client_id)
        except StoreError:
            raise
        except Exception:  # noqa: BLE001 - any other failure of the port is "unavailable", never a pass
            raise StoreError("COMPLIANCE_UNAVAILABLE") from None

    def _connection(self, client_id: str) -> Connection:
        conns = load_connections(self._connections_file)
        conn = conns.get(client_id)
        if conn is None:
            with self._state:
                self._snapshots.pop(client_id, None)       # deregistered: its data goes too
            raise StoreError("NOT_CONNECTED")
        return conn

    def status(self, client_id: str) -> dict:
        out = {"client_id": client_id, "data_source": DATA_SOURCE, "kill_switch": self.killed()}
        try:
            conn = self._connection(client_id)
        except StoreError as e:
            return {**out, "connected": False, "reason": e.code}
        with self._state:
            snap = self._current(client_id, conn)
        return {**out, "connected": True, "reason": None, "shop": conn.shop, "credential_kind": conn.credential.kind,
                "budget": conn.budget.model_dump(), "snapshot_id": snap.snapshot_id if snap else None,
                "snapshot_taken_at": snap.taken_at.isoformat() if snap else None}

    def snapshot(self, client_id: str) -> bytes:
        if self.killed():
            raise StoreError("STORE_READS_DISABLED")
        conn = self._connection(client_id)
        self._require_activation(conn.client_id)
        with self._state:
            snap = self._current(client_id, conn)
            if snap is None:
                raise StoreError("NOT_SYNCED")
            age_h = (self._now() - snap.taken_at).total_seconds() / 3600.0
            if age_h > conn.budget.snapshot_max_age_hours or age_h < -0.02:
                self._snapshots.pop(client_id, None)
                raise StoreError("SNAPSHOT_STALE")
            return snap.body

    def _current(self, client_id: str, conn: Connection) -> Optional[Snapshot]:
        """The held snapshot, if it was read under the client's CURRENT registration; otherwise it is dropped (the
        shop, credential or approval changed: what was read belongs to the old registration). Caller holds _state."""
        snap = self._snapshots.get(client_id)
        if snap is not None and not _same_registration(snap, conn):
            self._snapshots.pop(client_id, None)
            return None
        return snap

    def _registration_unchanged(self, conn: Connection) -> bool:
        try:
            now = load_connections(self._connections_file).get(conn.client_id)
        except StoreError:
            return False
        return now is not None and _registration(now) == _registration(conn)

    # ---- the sync --------------------------------------------------------------------------------------------

    def sync(self, client_id: str) -> dict:
        if self.killed():
            raise StoreError("STORE_READS_DISABLED")
        conn = self._connection(client_id)
        self._require_activation(conn.client_id)
        with self._state:
            lock = self._client_locks.setdefault(conn.client_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise StoreError("STORE_SYNC_IN_PROGRESS")
        try:
            if not self._sync_slots.acquire(blocking=False):
                raise StoreError("STORE_SYNC_CAPACITY")
            try:
                return self._sync_locked(conn)
            finally:
                self._sync_slots.release()
        finally:
            lock.release()

    def _sync_locked(self, conn: Connection) -> dict:
        now = self._now()
        day = (conn.client_id, now.date().isoformat())
        with self._state:
            if self._syncs_today.get(day, 0) >= conn.budget.syncs_per_day:
                raise StoreError("STORE_BUDGET_EXHAUSTED")
        cred = conn.credential
        if isinstance(cred, StaticTokenRef):
            secrets_held = (read_secret_file(cred.token_file),)
        else:
            assert isinstance(cred, ClientCredentialsRef)
            secrets_held = (read_secret_file(cred.client_id_file), read_secret_file(cred.client_secret_file))
        sync_id = self._new_sync_id()
        base = {"sync_id": sync_id, "client_id": conn.client_id, "platform": "shopify", "shop": conn.shop,
                "api_version": m.API_VERSION, "credential_kind": cred.kind,
                "approval_ref": conn.approval.approval_ref, "requested_at": now.isoformat()}
        try:
            self._ledger.record(f"rrs.{sync_id}.requested", "rr_store_read_requested", conn.client_id, base,
                                f"rrs1 requested client={conn.client_id} shop={conn.shop} v={m.API_VERSION}")
        except LedgerNotRecorded:
            raise StoreError("LEDGER_UNAVAILABLE") from None
        with self._state:
            self._syncs_today[day] = self._syncs_today.get(day, 0) + 1
            for k in [k for k in self._syncs_today if k[1] != day[1]]:
                del self._syncs_today[k]

        session = ShopifySession(conn.shop, deadline_s=self._deadline, transport=self._transport,
                                 monotonic=self._mono, sleep=self._sleep, resolve=self._resolve,
                                 dns_gate=self._dns_gate, kill_check=self.killed)
        try:
            if isinstance(cred, StaticTokenRef):
                session.use_static_token(secrets_held[0])
            else:
                session.exchange_client_credentials(secrets_held[0], secrets_held[1])
            session.preflight()
            read = session.read_orders(now=now, lookback_days=conn.budget.lookback_days,
                                       max_orders=conn.budget.max_orders,
                                       max_cost_points=conn.budget.max_cost_points)
            orders, counts = translate(read.orders)
            snap = self._build(conn, now, orders, counts, read)
        except StoreError as e:
            self._record_failed(sync_id, conn, e.code, session)
            raise
        except Exception:
            self._record_failed(sync_id, conn, "STORE_RESPONSE_INVALID", session)
            raise StoreError("STORE_RESPONSE_INVALID") from None
        if not self._registration_unchanged(conn):
            # AEGIS M1: re-registered (another shop, credential or approval) or deregistered while reading
            self._record_failed(sync_id, conn, "STORE_REGISTRATION_CHANGED", session)
            raise StoreError("STORE_REGISTRATION_CHANGED")
        st = session.stats
        done = {**base, "snapshot_id": snap.snapshot_id, "snapshot_sha256": hashlib.sha256(snap.body).hexdigest(),
                "orders_read": counts["orders_read"], "orders_analysed": counts["orders_analysed"],
                "requests": st.requests, "pages": st.pages, "cost_points": round(st.cost_points, 2),
                "throttle_waits": st.throttle_waits, "granted_scopes": st.granted_scopes,
                "truncated_by": read.truncated_by}
        try:
            self._ledger.record(f"rrs.{sync_id}.completed", "rr_store_read_completed", conn.client_id, done,
                                f"rrs1 completed client={conn.client_id} snap={snap.snapshot_id} "
                                f"read={counts['orders_read']} analysed={counts['orders_analysed']} "
                                f"pages={st.pages} cost={int(st.cost_points)}")
        except LedgerNotRecorded:
            # AEGIS L1: requests WERE made; the read is discarded because its completion is not on the ledger
            self._record_failed(sync_id, conn, "LEDGER_COMPLETION_UNRECORDED", session)
            raise StoreError("LEDGER_COMPLETION_UNRECORDED") from None
        with self._state:
            stored = self._registration_unchanged(conn)
            if stored:
                self._snapshots[conn.client_id] = snap
        if not stored:
            # the completion is on the ledger but the snapshot was discarded: say so (best effort, outside the lock)
            self._record_failed(sync_id, conn, "STORE_REGISTRATION_CHANGED", session)
            raise StoreError("STORE_REGISTRATION_CHANGED")
        return {"client_id": conn.client_id, "sync_id": sync_id, "snapshot_id": snap.snapshot_id,
                "orders_read": counts["orders_read"], "orders_analysed": counts["orders_analysed"],
                "orders_excluded": counts["orders_excluded"], "truncated_by": read.truncated_by,
                "pages": st.pages, "cost_points": round(st.cost_points, 2), "throttle_waits": st.throttle_waits}

    def _record_failed(self, sync_id: str, conn: Connection, code: str, session: ShopifySession) -> None:
        payload = {"sync_id": sync_id, "client_id": conn.client_id, "reason": code,
                   "requests": session.stats.requests, "pages": session.stats.pages}
        try:
            self._ledger.record(f"rrs.{sync_id}.failed", "rr_store_read_failed", conn.client_id, payload,
                                f"rrs1 failed client={conn.client_id} reason={code} "
                                f"requests={session.stats.requests}")
        except Exception:  # noqa: BLE001, S110 - best effort: the sync already failed; its requested record stands
            pass

    def _build(self, conn: Connection, now: datetime, orders: list[Order], counts: dict, read) -> Snapshot:
        created = [o.placed_at for o in orders]
        window = {
            "requested_start": read.window_start.isoformat(),
            "lookback_days": read.lookback_days,
            "lookback_limited_by_scope": read.lookback_limited_by_scope,
            "truncated_by": read.truncated_by,
            "oldest_order_read": min(n.createdAt for n in read.orders).isoformat() if read.orders else None,
            "newest_order_read": max(n.createdAt for n in read.orders).isoformat() if read.orders else None,
            "oldest_order_analysed": min(created).isoformat() if created else None,
            "newest_order_analysed": max(created).isoformat() if created else None,
        }
        coverage = {"data_source": DATA_SOURCE, "shop": conn.shop, "api_version": m.API_VERSION,
                    "taken_at": now.isoformat(), "window": window, "counts": counts, "agents": agent_coverage(),
                    "customer_data": "none collected: no customer, name, email, phone, address or IP was requested"}
        order_json = [o.model_dump(mode="json") for o in orders]
        digest = hashlib.sha256(_canonical({"client_id": conn.client_id, "orders": order_json,
                                            "coverage": coverage})).hexdigest()
        snapshot_id = f"snap-{digest[:24]}"
        body = _canonical({"client_id": conn.client_id, "snapshot_id": snapshot_id, "data_source": DATA_SOURCE,
                           "orders": order_json, "coverage": coverage})
        if len(body) > MAX_SNAPSHOT_BYTES:
            raise StoreError("STORE_SNAPSHOT_TOO_LARGE")
        return Snapshot(client_id=conn.client_id, snapshot_id=snapshot_id, taken_at=now, body=body, shop=conn.shop,
                        credential=conn.credential, approval_ref=conn.approval.approval_ref)


