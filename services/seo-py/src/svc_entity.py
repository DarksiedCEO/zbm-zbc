"""
The canonical entity record — the propagation spine's READ side (ADR 0017 decision 10; spec: one canonical entity
record -> listings -> on-site structured data -> AI entity representation).

Every field carries its value plus source, provenance, authority, freshness and history. Wave 1 holds the record and
compares a site against it (agents/entity_check.py); it pushes nothing to any listing (GBP, Apple Business Connect,
Bing Places are Wave 0, Andre's job; writing to them is a later wave).

The first record is Andre's own NAP exactly as he stated it (spec, Oct 6): Z Best Media, 5318 East 2nd Street,
Long Beach, CA; (562) 248-6617. Nothing is added that he did not state (no postal code, no country, no hours).
"""

from __future__ import annotations

import re
from datetime import timedelta

from clock import parse_iso
from errors import Conflict, Invalid
from reasons import R

ZBM_ENTITY = "ent-zbm"
FIELDS = ("name", "street_address", "locality", "region", "telephone")
FRESH_DAYS = 180            # a field not re-confirmed for this long is reported "stale" (a review prompt, not an error)
SEED_PROVENANCE = "Department 2 merged spec, founder-approved 2026-10-06 (Andre's own NAP)"
SEED = {"name": "Z Best Media", "street_address": "5318 East 2nd Street", "locality": "Long Beach", "region": "CA",
        "telephone": "(562) 248-6617"}
SOURCE = re.compile(r"[a-z][a-z0-9_]{1,39}")
_VALUE = re.compile(r"[\x20-\x7e]{1,200}")
AUTHORITIES = ("first_party", "first_party_platform", "third_party", "inferred")


def phone_digits(s: str) -> str:
    d = re.sub(r"\D", "", s or "")
    return d[1:] if len(d) == 11 and d.startswith("1") else d


class EntityMixin:
    def _seed_entity(self) -> None:
        if ZBM_ENTITY in self.entities:
            return
        data = {"entity_id": ZBM_ENTITY, "tenant_id": "zbm", "fields": {
            k: {"value": v, "source": "founder_statement", "provenance": SEED_PROVENANCE, "authority": "first_party"}
            for k, v in SEED.items()}}
        self._commit("entity_created", data, "seo",
                     evidence=("entity_created", f"entity:{ZBM_ENTITY}", {"entity_id": ZBM_ENTITY,
                               "fields_sha256": self._fields_sha(data["fields"])}, ("seed", ZBM_ENTITY)))

    @staticmethod
    def _fields_sha(fields: dict) -> str:
        from ledger import payload_sha256
        return payload_sha256(fields)

    def _a_entity_created(self, d, at):
        self.entities[d["entity_id"]] = {
            "entity_id": d["entity_id"], "tenant_id": d["tenant_id"], "version": 1,
            "fields": {k: {**f, "observed_at": at, "history": []} for k, f in d["fields"].items()}}

    def update_entity_field(self, entity_id: str, body: dict) -> dict:
        field, value = body["field"], body["value"]
        if field not in FIELDS:
            raise Invalid(R("ENTITY_FIELD_UNKNOWN"))
        if not _VALUE.fullmatch(value) or not SOURCE.fullmatch(body["source"]) \
                or body["authority"] not in AUTHORITIES or not _VALUE.fullmatch(body["provenance"]):
            raise Invalid(R("ENTITY_VALUE_INVALID"))
        if field == "telephone" and len(phone_digits(value)) != 10:
            raise Invalid(R("ENTITY_VALUE_INVALID"))
        with self.lock:
            self._gate()
            e = self._get(self.entities, entity_id, "ENTITY_NOT_FOUND")
            self.check_kill(tenant=e["tenant_id"], capability="entity_write", write=True)
            rk = self.rk("entity", entity_id, body)
            if self._idem("andre", rk, body):
                return self.entity_view(entity_id)
            if body["expected_version"] != e["version"]:
                raise Conflict(R("ENTITY_VERSION_STALE"))
            data = {"entity_id": entity_id, "field": field, "value": value, "source": body["source"],
                    "provenance": body["provenance"], "authority": body["authority"], "version": e["version"] + 1}
            self._commit("entity_field_set", self._req(data, "andre", rk, body, entity_id), "andre",
                         evidence=("entity_field_set", f"entity:{entity_id}",
                                   {"entity_id": entity_id, "field": field, "version": data["version"],
                                    "value_sha256": self._fields_sha({"v": value})}, ("andre", rk)))
            return self.entity_view(entity_id)

    def _a_entity_field_set(self, d, at):
        e = self.entities[d["entity_id"]]
        old = e["fields"].get(d["field"])
        hist = (old.get("history", []) + [{k: v for k, v in old.items() if k != "history"} | {"replaced_at": at}]
                if old else [])
        e["fields"][d["field"]] = {"value": d["value"], "source": d["source"], "provenance": d["provenance"],
                                   "authority": d["authority"], "observed_at": at, "history": hist}
        e["version"] = d["version"]

    def entity_view(self, entity_id: str, tenant: str | None = None) -> dict:
        with self.lock:
            e = self._get(self.entities, entity_id, "ENTITY_NOT_FOUND")
            if tenant is not None and e["tenant_id"] != tenant:
                self._get({}, entity_id, "ENTITY_NOT_FOUND")
            now = self.now()
            fields = {}
            for k, f in e["fields"].items():
                age = now - parse_iso(f["observed_at"])
                fields[k] = {**f, "history": [dict(h) for h in f["history"]],
                             "freshness": "fresh" if age <= timedelta(days=FRESH_DAYS) else "stale"}
            return {"entity_id": e["entity_id"], "tenant_id": e["tenant_id"], "version": e["version"],
                    "fields": fields, "fresh_days": FRESH_DAYS}

    def entity_for_tenant(self, tenant: str):
        with self.lock:
            for e in self.entities.values():
                if e["tenant_id"] == tenant:
                    return self.entity_view(e["entity_id"])
        return None
