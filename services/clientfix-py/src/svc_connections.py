"""
Connections, revocation and client sessions (founder decision 4; ADR 0017 decisions 5-7).

A connection is what the hub records after the client finished an OFFICIAL app connection (Shopify OAuth, Google OAuth):
the connector, the platform account it reaches (a shop domain, a GA4 property, a GTM container, a Business Profile
location, a Yelp business), the scopes granted, and a VAULT REFERENCE to the token the hub stored in the Cybersecurity
(22) vault. The token itself never reaches this service. A vault reference is bound to one connection forever; an
account is bound to at most one ACTIVE connection, so one client's connection can never be another's (tenant
isolation, decision 7).

Revocation (client, platform uninstall or Andre) is never refused for being late, repeated or ill-timed: the kill
switch (``revoked_now``) is set BEFORE the revocation is committed, so a running apply stops at its next request even
if the commit itself fails (it is then retried by the caller). Revoking a connection also stops every in-flight apply
of that client (the client's revocation epoch moves) and cancels every not-yet-applied item planned on it.

A client session is opened by the hub for a client it has authenticated; the token is returned once and only its
SHA-256 is kept. The client's quote acceptance and fix-plan approval must arrive through the hub WITH a live session
of exactly that client (expiry on the service clock).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Optional

from clock import iso, parse_iso
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from ledger import derived_id
from reasons import R

ACTIVE_JOB = ("quoted", "accepted", "paid", "planned", "approved")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ConnectionsMixin:
    # ------------------------------------------------------------------ connections

    def register_connection(self, actor: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("connection", body["client_id"], body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.connection_view(prev[1])
            conn = self.connectors.get(body["connector"])
            if conn is None:
                raise Invalid(R("CONNECTOR_UNKNOWN"))
            if conn.status == "not_built":
                raise Invalid(R("CONNECTOR_NOT_BUILT"))
            account = body["account_ref"]
            if not conn.account_ref.fullmatch(account):
                raise Invalid(R("ACCOUNT_REF_INVALID"))
            token_ref = body.get("token_ref")
            if conn.manual:
                if token_ref is not None:          # we never hold a client credential for a guided-manual platform
                    raise Invalid(R("TOKEN_REF_INVALID"))
            elif token_ref is None:
                raise Invalid(R("TOKEN_REF_INVALID"))
            if token_ref is not None and token_ref in self.token_index:
                raise Conflict(R("TOKEN_REF_IN_USE"))
            bound = self.account_index.get((conn.name, account))
            if bound is not None:
                if self.connections[bound]["client_id"] == body["client_id"]:
                    raise Conflict(R("CONNECTION_EXISTS"))
                raise Conflict(R("ACCOUNT_BOUND_ELSEWHERE"))
            scopes = sorted(set(body.get("scopes") or []))
            if not conn.manual and not set(conn.scopes) <= set(scopes):
                raise Invalid(R("SCOPES_INSUFFICIENT"))
            cid = derived_id("con", body["client_id"], conn.name, account, rk)
            data = {"connection_id": cid, "client_id": body["client_id"], "connector": conn.name,
                    "account_ref": account, "token_ref": token_ref, "scopes": scopes}
            self._commit("connection_registered", self._req(data, actor, rk, body, cid), actor,
                         evidence=("connection_registered", f"connection:{cid}",
                                   {"connection_id": cid, "client_id": body["client_id"], "connector": conn.name,
                                    "account_sha256": _sha(account),
                                    "token_ref_sha256": _sha(token_ref) if token_ref else None}, (actor, rk)))
            return self.connection_view(cid)

    def _a_connection_registered(self, d, at):
        c = {k: d[k] for k in ("connection_id", "client_id", "connector", "account_ref", "token_ref", "scopes")}
        c.update(status="active", created_at=at, revoked_at=None, revoked_by=None)
        self.connections[c["connection_id"]] = c
        self.account_index[(c["connector"], c["account_ref"])] = c["connection_id"]
        if c["token_ref"] is not None:
            self.token_index[c["token_ref"]] = c["connection_id"]

    def connection_view(self, cid: str) -> dict:
        with self.lock:
            c = self._get(self.connections, cid, "CONNECTION_NOT_FOUND")
            out = {k: v for k, v in c.items() if k != "token_ref"}
            out["has_token_ref"] = c["token_ref"] is not None       # the reference itself is never shown back
            out["status"] = "revoked" if cid in self.revoked_now else c["status"]
            return out

    def connections_view(self, client_id: Optional[str]) -> list:
        with self.lock:
            ids = sorted(self.connections, key=lambda k: (self.connections[k]["created_at"], k))
            return [self.connection_view(k) for k in ids
                    if client_id is None or self.connections[k]["client_id"] == client_id][:2000]

    def revoke_connection(self, actor: str, cid: str, body: dict) -> dict:
        """Never refused for being late, repeated or ill-timed (house rule: opt-outs are never refused)."""
        with self.lock:
            c = self._get(self.connections, cid, "CONNECTION_NOT_FOUND")
            self.revoked_now.add(cid)                     # the kill switch first: running work stops now
            if c["status"] == "revoked":
                return self.connection_view(cid)
            rk = self.rk("revoke", cid, body)
            if self._idem(actor, rk, body):
                return self.connection_view(cid)
            cancelled = []
            for j in self.jobs.values():
                if j["client_id"] != c["client_id"] or j["status"] not in ACTIVE_JOB:
                    continue
                for it in j["items"].values():
                    if it["status"] in ("open", "planned") and cid in (it.get("connection_id"),
                                                                       it["resource"]["connection_id"]):
                        cancelled.append([j["job_id"], it["item_id"]])
            data = {"connection_id": cid, "client_id": c["client_id"], "origin": body.get("origin", "client"),
                    "cancelled": cancelled}
            try:
                self._gate()
                self._commit("connection_revoked", self._req(data, actor, rk, body, cid), actor,
                             evidence=("connection_revoked", f"connection:{cid}",
                                       {"connection_id": cid, "client_id": c["client_id"], "origin": data["origin"],
                                        "cancelled_items": len(cancelled)}, (actor, rk)))
            except Unavailable as exc:
                exc.body = {**exc.body, "work_stopped": True}    # the kill switch holds in this process regardless
                raise
            for job_id in sorted({x[0] for x in cancelled}):     # a job left with nothing to do settles now
                self._settle_if_done(job_id)
            return self.connection_view(cid)

    def _a_connection_revoked(self, d, at):
        c = self.connections[d["connection_id"]]
        c.update(status="revoked", revoked_at=at, revoked_by=d.get("origin"))
        if self.account_index.get((c["connector"], c["account_ref"])) == c["connection_id"]:
            del self.account_index[(c["connector"], c["account_ref"])]
        self.revocation_epoch[c["client_id"]] = self.revocation_epoch.get(c["client_id"], 0) + 1
        for job_id, item_id in d.get("cancelled") or ():
            it = self.jobs[job_id]["items"][item_id]
            if it["status"] in ("open", "planned"):
                it["status"] = "cancelled_revoked"
                it["settled_at"] = at

    # ------------------------------------------------------------------ client sessions (hub-confirmed)

    def open_session(self, actor: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("session", body["client_id"], body)
            prev = self._idem(actor, rk, body)
            if prev:
                return {**prev[1], "session_token": None}      # the token is shown once; a retry opens nothing new
            token = secrets.token_hex(32)
            expires = iso(self.now() + timedelta(minutes=self.settings.client_session_minutes))
            obj = {"client_id": body["client_id"], "expires_at": expires}
            sha = _sha(token)
            self._commit("session_opened", self._req({"session_sha256": sha, **obj}, actor, rk, body, obj), actor,
                         evidence=("session_opened", f"client:{body['client_id']}",
                                   {"session_sha256": sha, "client_id": body["client_id"], "expires_at": expires},
                                   (actor, rk)))
            return {**obj, "session_token": token}

    def _a_session_opened(self, d, at):
        self.sessions[d["session_sha256"]] = {"client_id": d["client_id"], "expires_at": d["expires_at"],
                                              "status": "open"}

    def client_of_session(self, token: Optional[str], client_id: str) -> str:
        """The hub's client session must be live and be exactly this client's. Reads only (under the lock)."""
        if not token or not isinstance(token, str) or len(token) > 128:
            raise Forbidden(R("CLIENT_SESSION_REQUIRED"))
        s = self.sessions.get(_sha(token))
        if s is None or s["status"] != "open":
            raise Forbidden(R("CLIENT_SESSION_INVALID"))
        if self.now() >= parse_iso(s["expires_at"]):
            raise Forbidden(R("CLIENT_SESSION_EXPIRED"))
        if s["client_id"] != client_id:
            raise Forbidden(R("CLIENT_MISMATCH"))
        return s["client_id"]

    # ------------------------------------------------------------------ Andre: client freeze

    def freeze_client(self, body: dict, freeze: bool) -> dict:
        with self.lock:
            self._gate()
            cid = body["client_id"]
            rk = self.rk("freeze" if freeze else "unfreeze", cid, body)
            if self._idem("andre", rk, body):
                return {"client_id": cid, "frozen": cid in self.frozen_clients}
            if not freeze and cid not in self.frozen_clients:
                raise Conflict(R("NOT_FROZEN"))
            kind = "client_frozen" if freeze else "client_unfrozen"
            self._commit(kind, self._req({"client_id": cid}, "andre", rk, body, cid), "andre",
                         evidence=(kind, f"client:{cid}", {"client_id": cid}, ("andre", rk)))
            return {"client_id": cid, "frozen": freeze}

    def _a_client_frozen(self, d, at):
        self.frozen_clients[d["client_id"]] = {"at": at}

    def _a_client_unfrozen(self, d, at):
        self.frozen_clients.pop(d["client_id"], None)

    def _conn_for(self, client_id: str, cid: str) -> dict:
        """A connection, only if it is this client's (tenant isolation: never ``None == None`` — both ids are real)."""
        c = self.connections.get(cid)
        if c is None:
            raise NotFound(R("CONNECTION_NOT_FOUND"))
        if not client_id or c["client_id"] != client_id:
            raise Conflict(R("TENANT_MISMATCH"))
        return c
