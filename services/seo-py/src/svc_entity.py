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

from agents import secret_guard
from clock import parse_iso
from errors import Conflict, Invalid
from reasons import R
from svc_optext import text_view

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


def validate_field(field: str, data: dict) -> None:
    """The same supplied-fact vocabulary and validation for creation and subsequent updates."""
    if field not in FIELDS:
        raise Invalid(R("ENTITY_FIELD_UNKNOWN"))
    if not all(type(data.get(k)) is str for k in ("value", "source", "authority", "provenance")):
        raise Invalid(R("ENTITY_VALUE_INVALID"))
    value = data["value"]
    if not _VALUE.fullmatch(value) or not SOURCE.fullmatch(data["source"]) \
            or data["authority"] not in AUTHORITIES or not _VALUE.fullmatch(data["provenance"]):
        raise Invalid(R("ENTITY_VALUE_INVALID"))
    if field == "telephone" and len(phone_digits(value)) != 10:
        raise Invalid(R("ENTITY_VALUE_INVALID"))
    # review c0c09bd1: the value and its provenance are VAULTED (svc_optext.py), not refused for failing the strict
    # check; refused only for a strong-key prefix, a PEM / PGP block or URL user info. The source is an identifier
    # (``SOURCE``): refused when it fails the identifier rules
    if secret_guard.operator_refusal(value) is not None or secret_guard.operator_refusal(data["provenance"]) is not None:
        raise Invalid(R("ENTITY_VALUE_WITHHELD"))
    if secret_guard.identifier_refusal(data["source"]) is not None:
        raise Invalid(R("IDENTIFIER_WITHHELD"))


def value_flag(field: str, value: str):
    """The strict flag kept with an entity value's stub. A business telephone number is the one waiver (check (e) on
    the telephone field ONLY: a card / SSN typed into any other field is flagged)."""
    return secret_guard.check(value, globals()["_waiver"](field))


def _waiver(field: str) -> frozenset:
    return frozenset(("e_digits",)) if field == "telephone" else frozenset()


def _shown(f: dict, field: str) -> dict:
    """The owner's view of an entity field (review c0c09bd1): value and provenance through the client-view masker (a
    business telephone number is shown: it is the field's purpose); a value that cannot be read is None."""
    out = dict(f)
    for k in ("value", "provenance"):
        if k == "value" and field == "telephone" and isinstance(f.get("value"), str) \
                and len(phone_digits(f["value"])) == 10:
            continue                                       # the business telephone number is the field's purpose
        shown, alert = text_view(f.get(k))
        out[k] = shown
        if alert:
            out[f"{k}_masked"] = True
    return out


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
        cache: dict = {}
        tid = d["tenant_id"]
        self.entities[d["entity_id"]] = {
            "entity_id": d["entity_id"], "tenant_id": tid, "version": 1,
            "fields": {k: {**f, "value": self._optext_open(tid, f["value"], cache),
                           "provenance": self._optext_open(tid, f["provenance"], cache),
                           "observed_at": at, "history": []} for k, f in d["fields"].items()}}

    def create_entity(self, tid: str, body: dict) -> dict:
        supplied = body["fields"]
        if not isinstance(supplied, list) or not 1 <= len(supplied) <= len(FIELDS):
            raise Invalid(R("ENTITY_VALUE_INVALID"))
        names = set()
        for value in supplied:
            field = value["field"]
            validate_field(field, value)
            if field in names:
                raise Invalid(R("ENTITY_VALUE_INVALID"))
            names.add(field)
        fields = {value["field"]: {k: value[k] for k in ("value", "source", "provenance", "authority")}
                  for value in supplied}
        with self.lock:
            self._gate()
            tenant = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, capability="entity_write", write=True)
            if tenant["kind"] != "client":
                raise Invalid(R("ENTITY_TENANT_KIND_INVALID"))
            # The route tenant is validated and at most40 characters; this is an existing entity-ID shape.
            eid = f"ent-{tid}"
            rk = self.rk("entity_create", tid, body)
            self._optext_key_check()
            hashed = {f: self._entity_hashed(tid, v) for f, v in fields.items()}
            hbody = {**body, "fields": [{**v, **self._entity_hashed(tid, v)} for v in supplied]}
            if self._idem("andre", rk, hbody):
                return self.entity_view(eid)
            if eid in self.entities or any(e["tenant_id"] == tid for e in self.entities.values()):
                raise Conflict(R("ENTITY_EXISTS"))
            order = list(fields)
            texts = [fields[f][k] for f in order for k in ("value", "provenance")]
            flags = [value_flag(f, fields[f]["value"]) if k == "value" else secret_guard.check(fields[f][k])
                     for f in order for k in ("value", "provenance")]
            stubs = iter(self._optext_seal(tid, "entity", texts, flags))
            data = {"entity_id": eid, "tenant_id": tid,
                    "fields": {f: {**fields[f], "value": next(stubs), "provenance": next(stubs)} for f in order}}
            try:
                self._commit("entity_created", self._req(data, "andre", rk, hbody, eid), "andre",
                             evidence=("entity_created", f"entity:{eid}",
                                       {"entity_id": eid, "tenant_id": tid,
                                        "fields_sha256": self._fields_sha(hashed)}, ("andre", rk)))
            except BaseException:
                self._optext_orphaned()
                raise
            self._optext_committed()
            return self.entity_view(eid)

    def _entity_hashed(self, tid: str, v: dict) -> dict:
        """An entity field with its value and provenance as keyed HMACs (review c0c09bd1)."""
        return {**{k: v[k] for k in ("source", "authority") if k in v},
                "value": self._optext_hmac(tid, "entity", v["value"]),
                "provenance": self._optext_hmac(tid, "entity", v["provenance"])}

    def update_entity_field(self, entity_id: str, body: dict) -> dict:
        field, value = body["field"], body["value"]
        validate_field(field, body)
        with self.lock:
            self._gate()
            e = self._get(self.entities, entity_id, "ENTITY_NOT_FOUND")
            self.check_kill(tenant=e["tenant_id"], capability="entity_write", write=True)
            rk = self.rk("entity", entity_id, body)
            tid = e["tenant_id"]
            self._optext_key_check()
            h = self._entity_hashed(tid, body)
            hbody = {**body, **h}
            if self._idem("andre", rk, hbody):
                return self.entity_view(entity_id)
            if body["expected_version"] != e["version"]:
                raise Conflict(R("ENTITY_VERSION_STALE"))
            sv, sp = self._optext_seal(tid, "entity", [value, body["provenance"]],
                                       [value_flag(field, value), secret_guard.check(body["provenance"])])
            data = {"entity_id": entity_id, "field": field, "value": sv, "source": body["source"],
                    "provenance": sp, "authority": body["authority"], "version": e["version"] + 1}
            try:
                self._commit("entity_field_set", self._req(data, "andre", rk, hbody, entity_id), "andre",
                             evidence=("entity_field_set", f"entity:{entity_id}",
                                       {"entity_id": entity_id, "field": field, "version": data["version"],
                                        "value_sha256": self._fields_sha({"v": h["value"]})}, ("andre", rk)))
            except BaseException:
                self._optext_orphaned()
                raise
            self._optext_committed()
            return self.entity_view(entity_id)

    def _a_entity_field_set(self, d, at):
        e = self.entities[d["entity_id"]]
        old = e["fields"].get(d["field"])
        hist = (old.get("history", []) + [{k: v for k, v in old.items() if k != "history"} | {"replaced_at": at}]
                if old else [])
        cache: dict = {}
        e["fields"][d["field"]] = {"value": self._optext_open(e["tenant_id"], d["value"], cache),
                                   "source": d["source"],
                                   "provenance": self._optext_open(e["tenant_id"], d["provenance"], cache),
                                   "authority": d["authority"], "observed_at": at, "history": hist}
        e["version"] = d["version"]

    def entity_view(self, entity_id: str, tenant: str | None = None, raw: bool = False) -> dict:
        """The owner's view (review c0c09bd1: values and provenance through the client-view masker); ``raw`` (the
        service's own comparisons only, never a response) keeps the decrypted values."""
        with self.lock:
            e = self._get(self.entities, entity_id, "ENTITY_NOT_FOUND")
            if tenant is not None and e["tenant_id"] != tenant:
                self._get({}, entity_id, "ENTITY_NOT_FOUND")
            now = self.now()
            fields = {}
            for k, f in e["fields"].items():
                age = now - parse_iso(f["observed_at"])
                fields[k] = {**(dict(f) if raw else _shown(f, k)),
                             "history": [dict(h) if raw else _shown(h, k) for h in f["history"]],
                             "freshness": "fresh" if age <= timedelta(days=FRESH_DAYS) else "stale"}
            return {"entity_id": e["entity_id"], "tenant_id": e["tenant_id"], "version": e["version"],
                    "fields": fields, "fresh_days": FRESH_DAYS}

    def entity_for_tenant(self, tenant: str, raw: bool = False):
        with self.lock:
            for e in self.entities.values():
                if e["tenant_id"] == tenant:
                    return self.entity_view(e["entity_id"], raw=raw)
        return None
