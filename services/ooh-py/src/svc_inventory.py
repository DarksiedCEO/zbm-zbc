"""
OOH-2 (ADR 0020): the inventory and operator registry — the Southern California markets and their four readiness
flags, the named sources inventory facts come from, the canonical face (one object for every format), the supplier
profile, and vendor avails. Global, not per tenant: it is ZBM's knowledge of the market, not a client's data, and the
client hub never reads it (it carries vendor cost).

Rules enforced here, each with its own reason code and test:

* Nothing is invented. A market starts with every flag NOT_MET; a face attribute or supplier attribute nobody has
  sourced is UNKNOWN; there are no defaults.
* Every fact names its source and carries that source's timestamp: a face's identity, each attribute value, each
  avails answer. A source is a vendor quote or a rate card, recorded as received (``dated`` not in the future), of a
  registered vendor; a fact about one vendor's face or profile from another vendor's source is SOURCE_VENDOR_MISMATCH.
  A vendor API or platform feed is a port that is NOT_CONNECTED (SOURCE_NOT_CONNECTED; the port itself answers
  INVENTORY_PORT_NOT_CONNECTED).
* An attribute's current value is the one whose source is the most recent (``as_of``; a tie goes to the later
  record). An older source never displaces a newer one: it is kept in the history, marked ``current: false``.
* Raising a readiness flag to MET is Andre's, with an evidence reference; lowering it (the safe direction) is open to
  any preparer and to Compliance. The flags are independent: none is derived from another, and there is no "active".
* Every change commits through ``_commit`` (evidence first, then the anchored log line, then memory through the
  same ``_a_*`` handler replay uses).
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

import policy as P
from clock import parse_iso
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from ledger import derived_id
from reasons import R
from svc_campaigns import observed, unit_ref_key

_LAT = re.compile(r"-?[0-9]{1,2}(\.[0-9]{1,7})?")
_LON = re.compile(r"-?[0-9]{1,3}(\.[0-9]{1,7})?")
_FEET = re.compile(r"[0-9]{1,4}(\.[0-9]{1,2})?")
_INT = re.compile(r"[1-9][0-9]{0,6}")
_MONEY = re.compile(r"(0|[1-9][0-9]{0,11})\.[0-9]{2}")
_REF = re.compile(r"[A-Za-z0-9._:-]{1,100}")
from pii import has_personal_data as personal_data_in   # noqa: E402  (AEGIS wave 2 F-2, OOH-3/4 F3)


class PortNotConnected(Exception):
    """A port that exists as an interface only."""


class NotConnectedInventoryPort:
    """The vendor-API / platform inventory port (founder, Oct 16 round 2): an interface with no implementation. No
    supplier, platform or programmatic DOOH provider is chosen; every call refuses."""

    name = "inventory_supply"
    status = "NOT_CONNECTED"

    def fetch_faces(self, vendor: str) -> list:
        raise PortNotConnected("no inventory supply port is connected (no provider chosen)")


def _csv(value: str, allowed: tuple) -> Optional[list]:
    items = [x.strip() for x in value.split(",")]
    if not items or any(not x for x in items) or len(set(items)) != len(items) or any(x not in allowed for x in items):
        return None
    return sorted(items)


def parse_value(kind: str, value: str):
    """The typed value of an attribute (a string in the body), or raises Invalid. Text is vendor data: untrusted."""
    def bad():
        return Invalid(R("ATTRIBUTE_VALUE_INVALID"))
    if kind == "text" and personal_data_in(value):      # references cannot hold "@"; dates in them are not phones
        raise Invalid(R("ATTRIBUTE_VALUE_PERSONAL_DATA"))
    if kind in ("lat", "lon"):
        if not (_LAT if kind == "lat" else _LON).fullmatch(value):
            raise bad()
        v = Decimal(value)
        if abs(v) > (90 if kind == "lat" else 180):
            raise bad()
        return value
    if kind == "feet":
        if not _FEET.fullmatch(value) or Decimal(value) <= 0:
            raise bad()
        return value
    if kind == "int":
        if not _INT.fullmatch(value):
            raise bad()
        return int(value)
    if kind == "bool":
        if value not in ("true", "false"):
            raise bad()
        return value == "true"
    if kind == "facing":
        if value not in P.FACINGS:
            raise bad()
        return value
    if kind == "money":
        if not _MONEY.fullmatch(value):
            raise bad()
        try:
            if Decimal(value) <= 0:
                raise bad()
        except InvalidOperation:
            raise bad() from None
        return value
    if kind == "ref":
        if not _REF.fullmatch(value):
            raise bad()
        return value
    if kind == "text":
        return observed(value)
    if kind == "operator_type":
        if value not in P.OPERATOR_TYPES:
            raise bad()
        return value
    if kind == "formats":
        out = _csv(value, P.FORMATS)
    elif kind == "markets":
        out = _csv(value, P.MARKET_IDS)
    elif kind == "payment_methods":
        items = [x.strip() for x in value.split(",")]
        if "card" in items:
            raise Invalid(R("VENDOR_PAYMENT_METHOD_REFUSED"))       # vendors are never paid by card (locked)
        out = _csv(value, P.VENDOR_PAYMENT_METHODS)
    else:
        raise bad()
    if out is None:
        raise bad()
    return out


class InventoryMixin:
    # ================================================================================================ markets

    def _init_registry(self) -> None:
        self.markets: dict[str, dict] = {
            mid: {"market_id": mid, "name": name, "scope": scope,
                  "flags": {f: {"value": P.READINESS_DEFAULT, "evidence_ref": None, "set_by": None, "set_at": None,
                                "history": []} for f in P.READINESS_FLAGS}}
            for mid, name, scope in P.MARKETS}
        self.sources: dict[str, dict] = {}
        self.faces: dict[str, dict] = {}
        self.face_keys: dict[tuple, str] = {}           # (vendor, unit key) -> face id: one face, one record
        self.inventory_port = NotConnectedInventoryPort()

    def flag_met(self, market: str, flag: str) -> bool:
        m = self.markets.get(market)
        return m is not None and m["flags"][flag]["value"] == "MET"

    def require_market(self, market: str) -> None:
        if market not in self.markets:
            raise Invalid(R("MARKET_UNKNOWN"))

    def set_readiness(self, actor: str, mid: str, body: dict, andre: bool) -> dict:
        with self.lock:
            self._gate()
            if mid not in self.markets:
                raise NotFound(R("MARKET_UNKNOWN"))
            actor = "andre" if andre else actor
            rk = self.rk("readiness", mid, body)
            if self._idem(actor, rk, body):
                return self.market_view(mid)
            raising = body["value"] == "MET"
            if raising:
                if not andre:
                    raise Forbidden(R("ANDRE_APPROVAL_REQUIRED"))     # only Andre raises a flag
                self.check_kill(capability="registry")              # lowering is never blocked: it is the safe way
            data = {"market_id": mid, "flag": body["flag"], "value": body["value"], "evidence_ref": body["evidence_ref"],
                    "from": self.markets[mid]["flags"][body["flag"]]["value"]}
            self._commit("readiness_set", self._req(data, actor, rk, body, mid), actor,
                         evidence=(f"market_readiness_{'raised_by_andre' if raising else 'lowered'}",
                                   f"market:{mid}", data, (actor, rk)))
            return self.market_view(mid)

    def _a_readiness_set(self, d, at):
        f = self.markets[d["market_id"]]["flags"][d["flag"]]
        f["history"].append({"from": f["value"], "to": d["value"], "evidence_ref": d["evidence_ref"],
                             "by": d["actor"], "at": at})
        f.update(value=d["value"], evidence_ref=d["evidence_ref"], set_by=d["actor"], set_at=at)

    def market_view(self, mid: str) -> dict:
        with self.lock:
            m = self.markets[mid]
            return {"market_id": mid, "name": m["name"], "scope": m["scope"],
                    "flags": {k: {**v, "history": [dict(h) for h in v["history"]]} for k, v in m["flags"].items()},
                    "gates": dict(P.READINESS_GATES)}

    def markets_view(self) -> list:
        with self.lock:
            return [self.market_view(mid) for mid in P.MARKET_IDS]

    # ================================================================================================ sources

    def record_source(self, actor: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("source", "registry", body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.source_view(prev[1])
            self.check_kill(capability="registry")
            if body["kind"] in P.SOURCE_KINDS_NOT_CONNECTED:
                raise Conflict(R("SOURCE_NOT_CONNECTED"))           # a vendor API / platform feed: port not connected
            self.require_vendor(body["vendor"])
            try:
                dated = parse_iso(body["dated"])
            except ValueError:
                raise Invalid(R("INVALID"), field="dated") from None
            if dated > self.now():
                raise Invalid(R("SOURCE_DATED_IN_FUTURE"))
            sid = self._fresh(self.sources, derived_id("src", actor, rk))
            data = {"source_id": sid, "kind": body["kind"], "vendor": body["vendor"], "doc_ref": body["doc_ref"],
                    "dated": body["dated"], "evidence_class": "VENDOR_ASSERTED",
                    "vendor_text": observed(body.get("vendor_text"))}
            ev = {k: data[k] for k in ("source_id", "kind", "vendor", "doc_ref", "dated", "evidence_class")}
            ev["vendor_text_sha256"] = (data["vendor_text"] or {}).get("sha256")
            self._commit("source_recorded", self._req(data, actor, rk, body, sid), actor,
                         evidence=("inventory_source_recorded", f"source:{sid}", ev, (actor, rk)))
            return self.source_view(sid)

    def _a_source_recorded(self, d, at):
        self.sources[d["source_id"]] = {k: d[k] for k in ("source_id", "kind", "vendor", "doc_ref", "dated",
                                                          "evidence_class", "vendor_text")} \
            | {"recorded_by": d["actor"], "recorded_at": at}

    def _source(self, sid: str, vendor: str) -> dict:
        s = self.sources.get(sid)
        if s is None:
            raise NotFound(R("SOURCE_NOT_FOUND"))
        if s["vendor"] != vendor:
            raise Conflict(R("SOURCE_VENDOR_MISMATCH"))             # one vendor's document never describes another's
        return s

    def source_view(self, sid: str) -> dict:
        with self.lock:
            s = self.sources.get(sid)
            if s is None:
                raise NotFound(R("SOURCE_NOT_FOUND"))
            return {**s, "vendor_text": dict(s["vendor_text"]) if s["vendor_text"] else None}

    # ================================================================================================ faces

    def _fact(self, src: dict, value, actor: str, at: str) -> dict:
        return {"value": value, "source_id": src["source_id"], "source_kind": src["kind"], "as_of": src["dated"],
                "evidence_class": src["evidence_class"], "recorded_by": actor, "recorded_at": at}

    def register_face(self, actor: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("face", "registry", body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.face_view(prev[1])
            self.check_kill(capability="registry")
            src = self.sources.get(body["source_id"])
            if src is None:
                raise NotFound(R("SOURCE_NOT_FOUND"))
            self.require_market(body["market"])
            key = unit_ref_key(body["vendor_face_ref"])
            if not key:
                raise Invalid(R("INVALID"), field="vendor_face_ref")
            if (src["vendor"], key) in self.face_keys:
                raise Conflict(R("FACE_EXISTS"))                   # one physical face, one registry record
            fid = self._fresh(self.faces, derived_id("fce", src["vendor"], key))
            data = {"face_id": fid, "vendor": src["vendor"], "vendor_face_ref": body["vendor_face_ref"], "unit_key": key,
                    "format": body["format"], "market": body["market"], "source_id": src["source_id"]}
            self._commit("face_registered", self._req(data, actor, rk, body, fid), actor,
                         evidence=("inventory_face_registered", f"face:{fid}", data, (actor, rk)))
            return self.face_view(fid)

    def _a_face_registered(self, d, at):
        src = self.sources[d["source_id"]]
        self.faces[d["face_id"]] = {
            "face_id": d["face_id"], "vendor": d["vendor"], "vendor_face_ref": d["vendor_face_ref"],
            "unit_key": d["unit_key"], "format": d["format"], "market": d["market"],
            "proof_requirement": P.PROOF_REQUIREMENTS[d["format"]],
            "identity": self._fact(src, {k: d[k] for k in ("vendor", "vendor_face_ref", "format", "market")},
                                   d["actor"], at),
            "attributes": {}, "avails": [], "version": 1}
        self.face_keys[(d["vendor"], d["unit_key"])] = d["face_id"]

    def _face(self, fid: str) -> dict:
        f = self.faces.get(fid)
        if f is None:
            raise NotFound(R("FACE_NOT_FOUND"))
        return f

    def set_face_attribute(self, actor: str, fid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            f = self._face(fid)
            rk = self.rk("face_attr", fid, body)
            if self._idem(actor, rk, body):
                return self.face_view(fid)
            self.check_kill(capability="registry")
            if f["version"] != body["expected_version"]:
                raise Conflict(R("VERSION_STALE"))
            spec = P.FACE_ATTRIBUTES.get(body["attribute"])
            if spec is None:
                raise Invalid(R("ATTRIBUTE_UNKNOWN"))
            if f["format"] not in spec[1]:
                raise Invalid(R("ATTRIBUTE_NOT_FOR_FORMAT"))     # a mobile billboard has no fixed latitude
            value = parse_value(spec[0], body["value"])
            src = self._source(body["source_id"], f["vendor"])
            data = {"face_id": fid, "attribute": body["attribute"], "value": value, "source_id": src["source_id"]}
            ev = {**data, "value": value["sha256"] if isinstance(value, dict) else value}
            self._commit("face_attribute_set", self._req(data, actor, rk, body, fid), actor,
                         evidence=("inventory_face_attribute_sourced", f"face:{fid}", ev, (actor, rk)))
            return self.face_view(fid)

    def _sourced_set(self, table: dict, name: str, fact: dict) -> None:
        """Append a fact to an attribute's history; the current value is the one from the most recent source."""
        entry = table.setdefault(name, {"history": []})
        entry["history"].append(fact)
        best = None
        for i, h in enumerate(entry["history"]):
            k = (parse_iso(h["as_of"]), i)
            if best is None or k >= best[0]:
                best = (k, i)
        for i, h in enumerate(entry["history"]):
            h["current"] = i == best[1]

    def _a_face_attribute_set(self, d, at):
        f = self.faces[d["face_id"]]
        self._sourced_set(f["attributes"], d["attribute"], self._fact(self.sources[d["source_id"]], d["value"],
                                                                      d["actor"], at))
        f["version"] += 1

    def record_avails(self, actor: str, fid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            f = self._face(fid)
            rk = self.rk("avails", fid, body)
            if self._idem(actor, rk, body):
                return self.face_view(fid)
            self.check_kill(capability="registry")
            flight = self._flight(body)
            if flight is None:
                raise Invalid(R("FLIGHT_INVALID"))
            src = self._source(body["source_id"], f["vendor"])
            if body.get("valid_until") is not None:
                self._future(body["valid_until"], "AVAILS_VALIDITY_INVALID")
            aid = self._fresh({a["avails_id"] for a in f["avails"]}, derived_id("avl", fid, actor, rk))
            data = {"face_id": fid, "avails_id": aid, "source_id": src["source_id"], "flight": flight,
                    "answer": body["answer"], "valid_until": body.get("valid_until")}
            self._commit("avails_recorded", self._req(data, actor, rk, body, fid), actor,
                         evidence=("inventory_avails_recorded", f"face:{fid}", data, (actor, rk)))
            return self.face_view(fid)

    def _a_avails_recorded(self, d, at):
        f = self.faces[d["face_id"]]
        f["avails"].append({"avails_id": d["avails_id"], "flight": list(d["flight"]), "answer": d["answer"],
                            "valid_until": d["valid_until"], **self._fact(self.sources[d["source_id"]], d["answer"],
                                                                          d["actor"], at)})
        f["version"] += 1

    def avails_block(self, fid: str, flight: Optional[list]) -> Optional[dict]:
        """The vendor's unexpired UNAVAILABLE answer on this face for a flight overlapping ``flight`` that no later
        AVAILABLE answer covering the WHOLE of ``flight`` has overtaken (AEGIS wave 2 F-1: an AVAILABLE for a merely
        overlapping flight says nothing about the other days), or None. Later = the source's date, then the record."""
        live = []
        for i, a in enumerate(self.faces[fid]["avails"]):
            if a["valid_until"] is not None and parse_iso(a["valid_until"]) <= self.now():
                continue
            live.append(((parse_iso(a["as_of"]), i), a))
        for k, a in live:
            if a["answer"] != "UNAVAILABLE" or not self._overlap(a["flight"], flight):
                continue
            overtaken = flight is not None and any(
                k2 > k and b["answer"] == "AVAILABLE" and b["flight"][0] <= flight[0] and b["flight"][1] >= flight[1]
                for k2, b in live)
            if not overtaken:
                return a
        return None

    def face_view(self, fid: str) -> dict:
        with self.lock:
            f = self._face(fid)
            attrs = {}
            for name, (kind, formats) in P.FACE_ATTRIBUTES.items():
                if f["format"] not in formats:
                    continue
                e = f["attributes"].get(name)
                cur = next((h for h in e["history"] if h["current"]), None) if e else None
                attrs[name] = ({"status": "SOURCED", **_copy(cur)} if cur else {"status": "UNKNOWN", "value": None})
            return {**{k: f[k] for k in ("face_id", "vendor", "vendor_face_ref", "unit_key", "format", "market",
                                         "proof_requirement", "version")},
                    "identity": _copy(f["identity"]), "attributes": attrs,
                    "attribute_history": {k: [_copy(h) for h in v["history"]] for k, v in f["attributes"].items()},
                    "avails": [_copy(a) for a in f["avails"]]}

    def faces_view(self, market: Optional[str], fmt: Optional[str], vendor: Optional[str],
                   vendor_face_ref: Optional[str]) -> list:
        with self.lock:
            key = unit_ref_key(vendor_face_ref) if vendor_face_ref else None
            return [self.face_view(fid) for fid, f in sorted(self.faces.items())
                    if (market is None or f["market"] == market) and (fmt is None or f["format"] == fmt)
                    and (vendor is None or f["vendor"] == vendor) and (key is None or f["unit_key"] == key)]

    def sync_from_port(self, vendor: str) -> dict:
        """The vendor-API / platform port: NOT_CONNECTED. Nothing is fetched, nothing is recorded."""
        self.require_vendor(vendor)
        try:
            self.inventory_port.fetch_faces(vendor)
        except PortNotConnected:
            pass
        # whatever a port returns, nothing from it is recorded in this wave: a connected port needs its own wave
        raise Unavailable(R("INVENTORY_PORT_NOT_CONNECTED"))

    # ================================================================================================ suppliers

    def set_supplier_attribute(self, actor: str, vid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            v = self.vendors.get(vid)
            if v is None:
                raise NotFound(R("VENDOR_UNKNOWN"))
            rk = self.rk("supplier_attr", vid, body)
            if self._idem(actor, rk, body):
                return self.supplier_view(vid)
            self.check_kill(capability="registry")
            if v.get("version", 1) != body["expected_version"]:
                raise Conflict(R("VERSION_STALE"))
            kind = P.SUPPLIER_ATTRIBUTES.get(body["attribute"])
            if kind is None:
                raise Invalid(R("ATTRIBUTE_UNKNOWN"))
            value = parse_value(kind, body["value"])
            src = self._source(body["source_id"], vid)
            data = {"vendor_id": vid, "attribute": body["attribute"], "value": value, "source_id": src["source_id"]}
            self._commit("supplier_attribute_set", self._req(data, actor, rk, body, vid), actor,
                         evidence=("supplier_attribute_sourced", f"vendor:{vid}", data, (actor, rk)))
            return self.supplier_view(vid)

    def _a_supplier_attribute_set(self, d, at):
        v = self.vendors[d["vendor_id"]]
        self._sourced_set(v.setdefault("profile", {}), d["attribute"],
                          self._fact(self.sources[d["source_id"]], d["value"], d["actor"], at))
        v["version"] = v.get("version", 1) + 1

    def supplier_view(self, vid: str) -> dict:
        with self.lock:
            v = self.vendors.get(vid)
            if v is None:
                raise NotFound(R("VENDOR_UNKNOWN"))
            prof = v.get("profile", {})
            attrs = {}
            for name in P.SUPPLIER_ATTRIBUTES:
                e = prof.get(name)
                cur = next((h for h in e["history"] if h["current"]), None) if e else None
                attrs[name] = ({"status": "SOURCED", **_copy(cur)} if cur else {"status": "UNKNOWN", "value": None})
            return {"vendor_id": vid, "legal_name": v.get("legal_name"), "version": v.get("version", 1),
                    "data_port": self.inventory_port.status, "attributes": attrs,
                    "attribute_history": {k: [_copy(h) for h in e["history"]] for k, e in prof.items()}}


def _copy(x):
    return json.loads(json.dumps(x))
