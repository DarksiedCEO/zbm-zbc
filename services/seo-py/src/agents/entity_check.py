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

from agents import split_url
from envelope import envelope, finding
from primitives.fetch import FetchResult
from primitives.parse import ORG_TYPES
from primitives.safe_url import digest, safe_url
from svc_entity import phone_digits

AGENT, TASK = "entity_check", "on_site_entity_consistency"
STREET_WORDS = {"street": "st", "avenue": "ave", "boulevard": "blvd", "east": "e", "west": "w", "north": "n",
                "south": "s", "suite": "ste", "road": "rd", "drive": "dr", "second": "2nd"}
REGIONS = {"california": "ca"}
_PHONE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
METHODOLOGY = ("Organization / LocalBusiness JSON-LD nodes on the audited pages compared with the canonical entity "
               "record field by field (name, street address, locality, region, telephone) after normalising case, "
               "whitespace, punctuation, common street words and 'California' = 'CA'; phone numbers compared by "
               "their ten digits. The homepage phone observation searches only the selected homepage raw-HTML text sample "
               "(first 20000 extracted characters), with exact requested/final URL attribution; it is not visual evidence.")
LIMITS = ["Only the audited pages were read; NAP elsewhere on the site, and on listings (GBP, Apple Business "
          "Connect, Bing Places), is not checked in Wave 1.",
          "Markup inserted by JavaScript was not seen (rendering not connected)."]


def comparison(field: str, site: str, cval: Optional[str]) -> dict:
    """What is stored about an on-site value that differs from the record (round 2: never the page's text): the
    result, whether it is equal after each normalisation step, the lengths, and a keyed digest of the normalised
    on-site value (``safe_url.digest``: HMAC with the run's per-tenant key, so the same wrong value can be
    recognised across runs without being kept)."""
    s, c = site or "", cval or ""
    ns = norm(field, s)
    return {"result": "mismatch", "case_folded_equal": s.casefold() == c.casefold(),
            "whitespace_folded_equal": " ".join(s.casefold().split()) == " ".join(c.casefold().split()),
            "normalised_equal": ns == norm(field, c), "on_site_length": len(s), "canonical_length": len(c),
            "on_site_digest": digest(ns or "", "entity-" + field)}


def norm(field: str, v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    if field == "telephone":
        return phone_digits(v) or None
    s = re.sub(r"[^\w\s]", " ", v.casefold())
    words = [STREET_WORDS.get(w, w) for w in s.split()]
    s = " ".join(words)
    return REGIONS.get(s, s) or None


# Separate from the legacy all-page fact: no substring of a longer identifier counts.
_HOME_PHONE = re.compile(r"(?<![\w+])(?:\+?1[ .-]?)?(?:\([0-9]{3}\)|[0-9]{3})[ .-]?[0-9]{3}[ .-]?[0-9]{4}(?!\w)")
HOME_SCHEMA = "homepage-phone-sample/1"
HOME_REASONS = {"FOUND", "NOT_FOUND_IN_SAMPLE", "NO_CANONICAL_PHONE", "HOMEPAGE_NOT_SELECTED",
                "HOMEPAGE_UNREAD", "FETCH_PROVENANCE_UNKNOWN", "FINAL_URL_DIFFERS", "SAMPLE_UNKNOWN"}


def _home_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        return None
    u = split_url(value)
    if u is None or u.scheme not in ("https", "http") or not u.hostname or u.username or u.password \
            or u.query or u.fragment:
        return None
    return value


def homepage_phone(ctx, pages, telephone):
    """Observed sample only. Exact requested/final root equality; no alias or redirect inference."""
    configured = _home_url(ctx.url("/"))
    obs = {"schema": HOME_SCHEMA, "state": "unknown", "reason": "NO_CANONICAL_PHONE",
           "configured_url": configured, "requested_url": None, "final_url": None,
           "sample_limit_chars": 20000, "sample_chars": None, "extracted_text_chars": None,
           "sample_truncated": None}
    def unknown(reason):
        obs["reason"] = reason
        return obs
    if not isinstance(telephone, str) or len(telephone) > 100 \
            or not re.fullmatch(r"[0-9]{10}", phone_digits(telephone)):
        return obs
    if "/" not in ctx.paths:
        return unknown("HOMEPAGE_NOT_SELECTED")
    pg = pages.get("/")
    if not isinstance(pg, dict):
        return unknown("HOMEPAGE_UNREAD")
    f, x = pg.get("fetch"), pg.get("extract")
    if not isinstance(f, FetchResult):
        return unknown("FETCH_PROVENANCE_UNKNOWN")
    requested, final = _home_url(f.url), _home_url(f.final_url)
    # round 2 M5: compared raw, in memory; stored in the one safe form (a redirect target is crawled data)
    obs["requested_url"], obs["final_url"] = safe_url(requested), safe_url(final)
    if configured is None or requested != configured or final is None:
        return unknown("FETCH_PROVENANCE_UNKNOWN")
    if final != configured:
        return unknown("FINAL_URL_DIFFERS")
    if f.state != "OK" or type(f.status) is not int or not 200 <= f.status < 300 \
            or f.content_type not in ("text/html", "application/xhtml+xml") or not isinstance(f.body, bytes):
        return unknown("HOMEPAGE_UNREAD")
    if not isinstance(x, dict) or "parse_error" not in x or x["parse_error"] is not None:
        return unknown("SAMPLE_UNKNOWN")
    sample, length = x.get("text_sample"), x.get("text_length")
    if not isinstance(sample, str) or len(sample) > 20000 or type(length) is not int \
            or not 0 <= length <= 4 * 1024 * 1024 or len(sample) != min(length, 20000):
        return unknown("SAMPLE_UNKNOWN")
    truncated = length > len(sample)
    obs.update(sample_chars=len(sample), extracted_text_chars=length, sample_truncated=truncated)
    found = any(phone_digits(m.group()) == phone_digits(telephone)
                and not (truncated and m.end() == len(sample)) for m in _HOME_PHONE.finditer(sample))
    obs.update(state="found" if found else "not_found_in_sample", reason="FOUND" if found else "NOT_FOUND_IN_SAMPLE")
    return obs


def run(ctx, pages: dict, entity: Optional[dict]) -> dict:
    telephone = None if entity is None else entity.get("fields", {}).get("telephone", {}).get("value")
    home = homepage_phone(ctx, pages, telephone)
    if entity is None:
        return envelope(AGENT, TASK, "INSUFFICIENT_EVIDENCE", [], methodology=METHODOLOGY, limitations=LIMITS,
                        reason="this tenant has no canonical entity record", facts={"homepage_phone_observation": home})
    canon = {k: f["value"] for k, f in entity["fields"].items()}
    findings, compared, nodes_seen = [], {}, 0
    phone_in_text = False
    read = 0
    for path, pg in pages.items():
        x = pg.get("extract")
        if not isinstance(x, dict):
            continue
        read += 1
        url = ctx.url(path)
        sample = x.get("text_sample") if isinstance(x.get("text_sample"), str) else ""
        if isinstance(canon.get("telephone"), str) and canon["telephone"] and any(
                phone_digits(m) == phone_digits(canon["telephone"]) for m in _PHONE.findall(sample)):
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
                                                        "comparison": comparison(field, site, cval),
                                                        "canonical_source": entity["fields"][field]["source"],
                                                        "canonical_authority": entity["fields"][field]["authority"]}))
    if read and nodes_seen == 0:
        findings.append(finding("ENTITY_MARKUP_ABSENT", "medium", "measured", "ACT", url=ctx.url("/"), capability="P6",
                                detail={"note": "no Organization / LocalBusiness node to compare with the record"}))
    if home["state"] == "not_found_in_sample":
        findings.append(finding("HOMEPAGE_PHONE_NOT_IN_TEXT_SAMPLE", "low", "measured", "TEST", url=ctx.url("/"),
                                capability="P6", detail={"sample_limit_chars": 20000, "sample_chars": home["sample_chars"]}))
    outcome = "OK" if read == len(pages) and read else ("PARTIAL" if read else "INSUFFICIENT_EVIDENCE")
    if home["state"] == "unknown" and outcome == "OK":
        outcome = "PARTIAL"
    return envelope(AGENT, TASK, outcome, findings, methodology=METHODOLOGY, limitations=LIMITS, not_connected={"render"},
                    facts={"entity_id": entity["entity_id"], "entity_version": entity["version"],
                           "nodes_compared": nodes_seen, "fields": compared, "phone_in_visible_text": phone_in_text,
                           "homepage_phone_observation": home})
