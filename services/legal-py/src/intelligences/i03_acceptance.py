"""
Intelligence 3 — Acceptance & E-Sign Evidence (Legal spec §B.2, §C.3). Decides evidence records and their
sufficiency; never stores an IP address, user agent or device data, and never accepts a hash that differs from
the register.

- Clickwrap: ``doc_sha256`` must equal the register's SHA-256 for that version and the version must be the
  current (approved, effective, not past review) version at ``accepted_at`` (Legal's clock); ``presented_sha256``
  must equal ``doc_sha256``; ``affirmative_act`` must be true; clipper agreements need the ESIGN §7001(c)
  consent block while ``LEGAL_ESIGN_CONSENT_REQUIRED=1``. ``evidence_sufficient`` is false until CQ-19 is
  verified by a counsel memo, then true when every §B.2 field is present (L1: attribution rests on identity ref
  + document hash + timestamp + presentation + affirmative act, never on an IP).
- Envelopes: sufficient only when the provider's ``signed_document_sha256`` equals ``doc_sha256``; the provider's
  certificate is stored by hash in Legal's own blob store. The UETA §1633.3 exclusions are UNVERIFIED, so only
  ordinary commercial contract types may go out for e-sign.
"""

from __future__ import annotations

from typing import Any

NUMBER, NAME, ACTOR = 3, "Acceptance & E-Sign Evidence", "intel_03_acceptance"
METHODS = ("clickwrap_unticked_box", "esign_envelope")
PRESENTATIONS = ("scroll_to_accept", "link", "inline")
# Keys that must never appear anywhere in a Legal request body (spec G6: no IP, device, DOB, government id or
# payment data). A body carrying one is refused 422 before it is parsed.
FORBIDDEN_KEYS = frozenset({
    "ip", "ip_address", "ipaddress", "ip_addr", "remote_addr", "remote_ip", "client_ip", "x_forwarded_for",
    "user_agent", "useragent", "ua", "device", "device_id", "device_fingerprint", "fingerprint", "browser",
    "dob", "date_of_birth", "birth_date", "birthdate", "ssn", "tin", "ein", "tax_id", "government_id", "passport",
    "drivers_license", "national_id", "card_number", "pan", "cvv", "cvc", "account_number", "routing_number", "iban",
})


def forbidden_keys(obj: Any, path: str = "", depth: int = 0) -> list[str]:
    out: list[str] = []
    if depth > 40:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if str(k).lower().replace("-", "_") in FORBIDDEN_KEYS:
                out.append(p[:120])
            out += forbidden_keys(v, p, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:10_000]:
            out += forbidden_keys(v, path, depth + 1)
    return out


def clickwrap_sufficient(record: dict, cq19_verified: bool, consent_required: bool, bound_instance: bool) -> bool:
    """``bound_instance``: the accepted version is a filled instance bound to the accepting party, or a standard
    form (AEGIS N17-4); a template or another party's instance is never sufficient."""
    if not cq19_verified or bound_instance is not True:
        return False
    need = ("party_ref", "signer_identity_ref", "doc_id", "version", "doc_sha256", "presented_sha256", "accepted_at",
            "method", "presentation")
    if any(not record.get(k) for k in need) or record.get("affirmative_act") is not True:
        return False
    if record["presented_sha256"] != record["doc_sha256"]:
        return False
    if consent_required and record.get("doc_id") == "clipper_agreement" and not record.get("esign_consent"):
        return False
    return True
