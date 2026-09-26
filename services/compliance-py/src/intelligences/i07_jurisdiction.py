"""
Intelligence 7 — Jurisdiction Resolver (spec C.5).

Decides operate / conditional / refuse for a person (declared country and
region + attestation, never IP alone) or a campaign target. Rules, in order:
  1. not attested, or country missing -> fact_missing;
  2. region required (HR-05 ``region_required``: US, CA, UA) but null:
     CA -> treated as CA-QC (refuse); US/UA -> fact_missing;
  3. the code, or its country, is in HR-07 ``refuse`` -> refuse;
  4. in HR-05 ``operate`` and not in ``operate_excludes`` -> operate;
  5. in HR-06 ``conditional`` -> conditional;
  6. otherwise refuse (HR-07 ``unlisted: refuse``, default deny).
Before any of that, a code that is not on the shipped ISO 3166-1 / 3166-2
list (``jurisdictions.py``) is REFUSED as unknown (AEGIS N14-3): an alias
such as ``CA-PQ`` / ``CA-QUE`` for Quebec, or a made-up ``US-XX``, never
reaches the operate list by its country prefix.
Targets: CA must be province-level; bare CA counts as including CA-QC.
Each answer names the rows responsible (``cites``), so the
``jurisdiction_class`` check can cite exactly HR-05/06/07 and, when they
match, US-OFAC-04, FR-LOI-2023-451 and CA-QC-LAW25.
Never does: use network location alone; guess a region.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from jurisdictions import is_known

NUMBER, NAME, ACTOR = 7, "Jurisdiction Resolver", "intel_07_jurisdiction"


@dataclass(frozen=True)
class Params:
    operate: tuple[str, ...]
    operate_excludes: tuple[str, ...]
    region_required: tuple[str, ...]
    conditional: tuple[str, ...]
    refuse: tuple[str, ...]
    unlisted: str

    @classmethod
    def from_rows(cls, rows_by_id: dict[str, dict]) -> "Params":
        hr05 = (rows_by_id.get("HR-05") or {}).get("parameters", {})
        hr06 = (rows_by_id.get("HR-06") or {}).get("parameters", {})
        hr07 = (rows_by_id.get("HR-07") or {}).get("parameters", {})

        def tup(d, k):
            v = d.get(k, [])
            return tuple(x for x in v if isinstance(x, str)) if isinstance(v, list) else ()

        unlisted = hr07.get("unlisted", "refuse")
        return cls(tup(hr05, "operate"), tup(hr05, "operate_excludes"), tup(hr05, "region_required"),
                   tup(hr06, "conditional"), tup(hr07, "refuse"),
                   unlisted if unlisted in ("refuse", "conditional") else "refuse")

    @property
    def ofac_codes(self) -> tuple[str, ...]:
        """OFAC comprehensively sanctioned jurisdictions = HR-07 refuse minus
        the non-sanctions refusals (France, Quebec). ADR 0006, choice 9."""
        return tuple(c for c in self.refuse if c not in ("FR", "CA-QC"))


@dataclass(frozen=True)
class Resolution:
    who: str                     # "subject" | "target" | "clipper" | "campaign" | ...
    code: Optional[str]          # resolved ISO code, or None when facts are missing
    cls: str                     # operate | conditional | refuse | missing
    reason: str
    cites: tuple[str, ...] = ()
    missing: tuple[str, ...] = field(default=())   # fact keys when cls == missing


def _country(code: str) -> str:
    return code.split("-", 1)[0]


def classify(code: str, p: Params, who: str) -> Resolution:
    country = _country(code)
    cites: list[str] = []
    if not is_known(code) or not is_known(country):
        # unknown ISO code (alias, typo, reserved or made up): refused, never guessed
        if country == "FR":
            cites.append("FR-LOI-2023-451")
        if country == "CA":
            cites.append("CA-QC-LAW25")  # an unknown Canadian code may be Quebec (e.g. CA-PQ, CA-QUE)
        return Resolution(who, code, "refuse", "unknown ISO 3166 code (not on the shipped list): refused",
                          tuple(["HR-07", *cites]))
    if code in p.ofac_codes or country in p.ofac_codes:
        cites.append("US-OFAC-04")
    if country == "FR":
        cites.append("FR-LOI-2023-451")
    if code == "CA-QC":
        cites.append("CA-QC-LAW25")
    if code in p.refuse or country in p.refuse:
        if code == "CA-QC":
            cites.append("HR-05")  # operate_excludes names CA-QC too
        return Resolution(who, code, "refuse", "refused jurisdiction", tuple(["HR-07", *cites]))
    if (code in p.operate or country in p.operate) and code not in p.operate_excludes:
        return Resolution(who, code, "operate", "operate jurisdiction", ("HR-05", *cites))
    if code in p.operate_excludes:
        return Resolution(who, code, "refuse", "excluded from operate", ("HR-05", "HR-07", *cites))
    if code in p.conditional or country in p.conditional:
        return Resolution(who, code, "conditional", "conditional: EU kit required", ("HR-06", *cites))
    if p.unlisted == "refuse":
        return Resolution(who, code, "refuse", "unlisted jurisdiction (default deny)", ("HR-07", *cites))
    return Resolution(who, code, "conditional", "unlisted jurisdiction treated as conditional", ("HR-06", *cites))


def resolve_person(j: Optional[dict], p: Params, who: str, prefix: str = "jurisdiction") -> Resolution:
    """``j`` = the validated ``jurisdiction`` facts (keys absent when missing or wrong)."""
    j = j or {}
    missing = [f"{prefix}.{k}" for k in ("declared_country", "attested", "attestation_ref") if k not in j]
    if "declared_country" in j and j.get("attested") is False:
        missing.append(f"{prefix}.attested")
    if missing:
        return Resolution(who, None, "missing", "jurisdiction not declared and attested", ("HR-05", "HR-07"),
                          tuple(sorted(set(missing))))
    country = j["declared_country"]
    region = j.get("declared_region")
    if "declared_region" not in j:
        return Resolution(who, None, "missing", "declared_region missing", ("HR-05",), (f"{prefix}.declared_region",))
    if region is not None and _country(region) != country:
        return Resolution(who, None, "missing", "declared_region is not in declared_country", ("HR-05",),
                          (f"{prefix}.declared_region",))
    if region is None and country in p.region_required:
        if country == "CA":
            r = classify("CA-QC", p, who)
            return Resolution(who, "CA", r.cls, "Canada without a province counts as including Quebec",
                              tuple(sorted(set(r.cites + ("HR-05",)))))
        return Resolution(who, None, "missing", f"region required for {country}", ("HR-05",),
                          (f"{prefix}.declared_region",))
    return classify(region or country, p, who)


def resolve_target(code: str, p: Params, who: str = "target") -> Resolution:
    if code == "CA":
        r = classify("CA-QC", p, who)
        return Resolution(who, "CA", r.cls, "bare CA target counts as including Quebec (province-level required)",
                          tuple(sorted(set(r.cites + ("HR-05",)))))
    return classify(code, p, who)
