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
from svc_vaulted import ENTITY_FIELD, normalised

ZBM_ENTITY = "ent-zbm"
FIELDS = ("name", "street_address", "locality", "region", "telephone")
FRESH_DAYS = 180            # a field not re-confirmed for this long is reported "stale" (a review prompt, not an error)
SEED_PROVENANCE = "Department 2 merged spec, founder-approved 2026-10-06 (Andre's own NAP)"
SEED = {"name": "Z Best Media", "street_address": "5318 East 2nd Street", "locality": "Long Beach", "region": "CA",
        "telephone": "(562) 248-6617"}
SEED_NAME = "zbm_nap_v1"
_CONTENT_KEYS = ("value", "source", "provenance", "authority")
SEEDS = {SEED_NAME: {k: {"value": v, "source": "founder_statement", "provenance": SEED_PROVENANCE,
                         "authority": "first_party"} for k, v in SEED.items()}}
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


def _flags(field: str, content: dict) -> dict:
    """The strict verdict names (or None) the log keeps for a field's value and provenance."""
    return {"value": value_flag(field, content["value"]), "provenance": secret_guard.check(content["provenance"])}


def unreadable(entity) -> bool:
    """True when a part of the entity record could not be read from the vault (a current value is ``None``)."""
    return isinstance(entity, dict) and (bool(entity.get("vault_unavailable")) or any(
        not isinstance(f, dict) or f.get("value") is None for f in (entity.get("fields") or {}).values()))


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
    """Sink job D: an entity's content (every field's value, source, provenance and authority) is ONE blob per entity
    version in the operator text vault (``ENTITY_FIELD``). The log keeps the ids, the field NAMES (the closed
    ``FIELDS`` schema), the strict verdict flags, the blob ref and its keyed HMAC. A blob that cannot be read leaves
    the field ``None`` and the entity ``vault_unavailable``: every dependant fails closed (never compares ``None``)."""

    def _seed_entity(self) -> None:
        if ZBM_ENTITY in self.entities:
            return
        # the founder's own record is a constant of this code base (``SEEDS``): the log names it, never copies it
        data = {"entity_id": ZBM_ENTITY, "tenant_id": "zbm", "seed": SEED_NAME}
        self._commit("entity_created", data, "seo",
                     evidence=("entity_created", f"entity:{ZBM_ENTITY}", {"entity_id": ZBM_ENTITY, "seed": SEED_NAME},
                               ("seed", ZBM_ENTITY)))

    @staticmethod
    def _fields_sha(fields: dict) -> str:
        from ledger import payload_sha256
        return payload_sha256(fields)

    def _entity_doc(self, tid: str, d: dict):
        """The content of an entity record: the named seed, the record's blob, or None (it cannot be read)."""
        if "seed" in d:
            return SEEDS.get(d["seed"])
        doc = self._doc_applied(tid, ENTITY_FIELD, d.get("content_ref"), d.get("content_hmac"), self.optext_vault)
        fields = doc.get("fields") if isinstance(doc, dict) else None
        if not isinstance(fields, dict) or not all(
                isinstance(f, dict) and all(isinstance(f.get(k), str) for k in _CONTENT_KEYS)
                for f in fields.values()):
            return None
        return fields

    def _a_entity_created(self, d, at):
        tid = d["tenant_id"]
        if isinstance(d.get("fields"), dict):
            # a line written before sink job D: plain values (the seed) or per-text stubs (review c0c09bd1)
            cache: dict = {}
            fields = {k: {**f, "value": self._optext_open(tid, f["value"], cache),
                          "provenance": self._optext_open(tid, f["provenance"], cache)}
                      for k, f in d["fields"].items()}
        else:
            doc = self._entity_doc(tid, d)
            names = list(SEEDS[d["seed"]]) if "seed" in d and doc is not None else list(d.get("fields") or [])
            fields = {k: (dict(doc[k]) if doc is not None and k in doc else dict.fromkeys(_CONTENT_KEYS))
                      for k in names}
        self.entities[d["entity_id"]] = {
            "entity_id": d["entity_id"], "tenant_id": tid, "version": 1,
            "fields": {k: {**f, "observed_at": at, "history": []} for k, f in fields.items()}}
        if not fields:
            self.entities[d["entity_id"]]["vault_unavailable"] = True     # nothing of the record could be read

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
        fields = {value["field"]: {k: value[k] for k in _CONTENT_KEYS} for value in supplied}
        # The route tenant is validated and at most40 characters; this is an existing entity-ID shape.
        eid = f"ent-{tid}"
        order = list(fields)
        with self.lock:
            self._gate()
            tenant = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, capability="entity_write", write=True)
            if tenant["kind"] != "client":
                raise Invalid(R("ENTITY_TENANT_KIND_INVALID"))
            self._optext_key_check()
        # the content is encrypted and HMAC-ed OUTSIDE the service lock (as every vaulted document)
        doc = normalised({"v": 1, "entity_id": eid, "version": 1, "fields": fields})
        sealed = self._doc_seal(tid, ENTITY_FIELD, doc, self.optext_vault)
        try:
            with self.lock:
                self._gate()
                tenant = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
                self.check_kill(tenant=tid, capability="entity_write", write=True)
                rk = self.rk("entity_create", tid, body)
                hbody = {**body, "fields": [{**v, **self._entity_hashed(tid, v)} for v in supplied]}
                if self._idem("andre", rk, hbody):
                    return self.entity_view(eid)
                if eid in self.entities or any(e["tenant_id"] == tid for e in self.entities.values()):
                    raise Conflict(R("ENTITY_EXISTS"))
                data = {"entity_id": eid, "tenant_id": tid, "fields": order,
                        "flags": {f: _flags(f, fields[f]) for f in order},
                        "content_ref": sealed.ref, "content_hmac": sealed.hmac}
                self._doc_commit(sealed, doc, lambda: self._commit(
                    "entity_created", self._req(data, "andre", rk, hbody, eid), "andre",
                    evidence=("entity_created", f"entity:{eid}",
                              {"entity_id": eid, "tenant_id": tid, "content_hmac": sealed.hmac}, ("andre", rk))))
                return self.entity_view(eid)
        finally:
            sealed.discard()

    def _entity_hashed(self, tid: str, v: dict) -> dict:
        """An entity field with its value and provenance as keyed HMACs (review c0c09bd1): the idempotency body."""
        return {**{k: v[k] for k in ("source", "authority") if k in v},
                "value": self._optext_hmac(tid, "entity", v["value"]),
                "provenance": self._optext_hmac(tid, "entity", v["provenance"])}

    def update_entity_field(self, entity_id: str, body: dict) -> dict:
        field = body["field"]
        validate_field(field, body)
        with self.lock:
            self._gate()
            e = self._get(self.entities, entity_id, "ENTITY_NOT_FOUND")
            tid = e["tenant_id"]
            self.check_kill(tenant=tid, capability="entity_write", write=True)
            self._optext_key_check()
        # the new version's content, encrypted and HMAC-ed OUTSIDE the lock; the version it names is the one the
        # caller expects to replace, checked under the lock below
        version = body["expected_version"] + 1
        content = {k: body[k] for k in _CONTENT_KEYS}
        doc = normalised({"v": 1, "entity_id": entity_id, "version": version, "fields": {field: content}})
        sealed = self._doc_seal(tid, ENTITY_FIELD, doc, self.optext_vault)
        try:
            with self.lock:
                self._gate()
                e = self._get(self.entities, entity_id, "ENTITY_NOT_FOUND")
                self.check_kill(tenant=tid, capability="entity_write", write=True)
                rk = self.rk("entity", entity_id, body)
                hbody = {**body, **self._entity_hashed(tid, body)}
                if self._idem("andre", rk, hbody):
                    return self.entity_view(entity_id)
                if body["expected_version"] != e["version"]:
                    raise Conflict(R("ENTITY_VERSION_STALE"))
                data = {"entity_id": entity_id, "field": field, "version": version,
                        "flags": _flags(field, content), "content_ref": sealed.ref, "content_hmac": sealed.hmac}
                self._doc_commit(sealed, doc, lambda: self._commit(
                    "entity_field_set", self._req(data, "andre", rk, hbody, entity_id), "andre",
                    evidence=("entity_field_set", f"entity:{entity_id}",
                              {"entity_id": entity_id, "field": field, "version": version,
                               "content_hmac": sealed.hmac}, ("andre", rk))))
                return self.entity_view(entity_id)
        finally:
            sealed.discard()

    def _a_entity_field_set(self, d, at):
        e = self.entities[d["entity_id"]]
        old = e["fields"].get(d["field"])
        hist = (old.get("history", []) + [{k: v for k, v in old.items() if k != "history"} | {"replaced_at": at}]
                if old else [])
        if "content_ref" in d:
            doc = self._entity_doc(e["tenant_id"], d)
            new = dict(doc[d["field"]]) if doc is not None and d["field"] in doc else dict.fromkeys(_CONTENT_KEYS)
        else:                                              # a line written before sink job D (per-text stubs)
            cache: dict = {}
            new = {"value": self._optext_open(e["tenant_id"], d["value"], cache), "source": d["source"],
                   "provenance": self._optext_open(e["tenant_id"], d["provenance"], cache),
                   "authority": d["authority"]}
        e["fields"][d["field"]] = {**new, "observed_at": at, "history": hist}
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
            out = {"entity_id": e["entity_id"], "tenant_id": e["tenant_id"], "version": e["version"],
                   "fields": fields, "fresh_days": FRESH_DAYS}
            if any(f.get("value") is None for f in e["fields"].values()):
                out["vault_unavailable"] = True           # sink job D: a field's blob cannot be read; dependants refuse
            return out

    def entity_for_tenant(self, tenant: str, raw: bool = False):
        with self.lock:
            for e in self.entities.values():
                if e["tenant_id"] == tenant:
                    return self.entity_view(e["entity_id"], raw=raw)
        return None
