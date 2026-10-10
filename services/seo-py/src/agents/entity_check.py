"""
The on-site entity consistency check (P6 local presence, read side of the propagation spine; ADR 0017 decision 10).

Compares what the audited pages SAY about the business — Organization / LocalBusiness JSON-LD and the phone number in
the visible text — with the tenant's canonical entity record (svc_entity.py), field by field. The canonical record
is the authority: a difference is reported against it, never "corrected" from the page.

Normalisation before comparing (and only this): case, whitespace and punctuation; the street words listed in
STREET_WORDS; "California" = "CA"; phone numbers by their ten NANP digits. Anything else that differs counts as
different, and the finding says so.
"""

from __future__ import annotations

import re
from typing import Optional

from envelope import envelope, finding, observed
from primitives.parse import ORG_TYPES
from svc_entity import phone_digits

AGENT, TASK = "entity_check", "on_site_entity_consistency"
STREET_WORDS = {"street": "st", "avenue": "ave", "boulevard": "blvd", "east": "e", "west": "w", "north": "n",
                "south": "s", "suite": "ste", "road": "rd", "drive": "dr", "second": "2nd"}
REGIONS = {"california": "ca"}
_PHONE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
METHODOLOGY = ("Organization / LocalBusiness JSON-LD nodes on the audited pages compared with the canonical entity "
               "record field by field (name, street address, locality, region, telephone) after normalising case, "
               "whitespace, punctuation, common street words and 'California' = 'CA'; phone numbers compared by "
               "their ten digits; the visible text searched for the canonical phone number.")
LIMITS = ["Only the audited pages were read; NAP elsewhere on the site, and on listings (GBP, Apple Business "
          "Connect, Bing Places), is not checked in Wave 1.",
          "Markup inserted by JavaScript was not seen (rendering not connected)."]


def norm(field: str, v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    if field == "telephone":
        return phone_digits(v) or None
    s = re.sub(r"[^\w\s]", " ", v.casefold())
    words = [STREET_WORDS.get(w, w) for w in s.split()]
    s = " ".join(words)
    return REGIONS.get(s, s) or None


def run(ctx, pages: dict, entity: Optional[dict]) -> dict:
    if entity is None:
        return envelope(AGENT, TASK, "INSUFFICIENT_EVIDENCE", [], methodology=METHODOLOGY, limitations=LIMITS,
                        reason="this tenant has no canonical entity record")
    canon = {k: f["value"] for k, f in entity["fields"].items()}
    findings, compared, nodes_seen = [], {}, 0
    phone_in_text = False
    read = 0
    for path, pg in pages.items():
        x = pg.get("extract")
        if x is None:
            continue
        read += 1
        url = ctx.url(path)
        if canon.get("telephone") and any(phone_digits(m) == phone_digits(canon["telephone"])
                                           for m in _PHONE.findall(x["text_sample"])):
            phone_in_text = True
        for block in x["jsonld"]:
            for n in block["nodes"]:
                if not set(n["types"]) & set(ORG_TYPES):
                    continue
                nodes_seen += 1
                props = n["props"]
                for field, cval in canon.items():
                    site = props.get(field)
                    key = f"{path}#{field}"
                    if site is None:
                        if field in ("street_address", "locality", "region") and "LocalBusiness" not in \
                                " ".join(n["types"]) and not any(t in ("ProfessionalService", "Store")
                                                                 for t in n["types"]):
                            continue                  # an Organization node need not carry an address
                        compared[key] = "missing"
                        findings.append(finding("ENTITY_FIELD_MISSING_IN_MARKUP", "medium", "measured", "ACT",
                                                url=url, capability="P6",
                                                detail={"field": field, "canonical": cval}))
                    elif norm(field, site) == norm(field, cval):
                        compared[key] = "match"
                    else:
                        compared[key] = "mismatch"
                        findings.append(finding("ENTITY_FIELD_MISMATCH", "high", "measured", "ACT", url=url,
                                                capability="P6",
                                                detail={"field": field, "canonical": cval,
                                                        "on_site": observed(site),
                                                        "canonical_source": entity["fields"][field]["source"],
                                                        "canonical_authority": entity["fields"][field]["authority"]}))
    if read and nodes_seen == 0:
        findings.append(finding("ENTITY_MARKUP_ABSENT", "medium", "measured", "ACT", url=ctx.url("/"), capability="P6",
                                detail={"note": "no Organization / LocalBusiness node to compare with the record"}))
    if read and canon.get("telephone") and not phone_in_text:
        findings.append(finding("PHONE_NOT_IN_VISIBLE_TEXT", "low", "measured", "TEST", url=ctx.url("/"),
                                capability="P6", detail={"canonical": canon["telephone"]}))
    outcome = "OK" if read == len(pages) and read else ("PARTIAL" if read else "INSUFFICIENT_EVIDENCE")
    return envelope(AGENT, TASK, outcome, findings, methodology=METHODOLOGY, limitations=LIMITS, not_connected={"render"},
                    facts={"entity_id": entity["entity_id"], "entity_version": entity["version"],
                           "nodes_compared": nodes_seen, "fields": compared, "phone_in_visible_text": phone_in_text})
