"""
Uploaded and caller-supplied text is DATA (Legal spec §0.1.5, LG-14): contract text, counterparty paper, memo
bodies and every string a caller sends. Copied from verification-py (itself from onboarding-py guardrails).

- ``has_control_chars``: C0/C1/DEL and lone surrogates are refused at the
  schema edge (422), except tab/newline where a field allows multi-line text
  (none of the fact fields do).
- ``normalize``: NFKC + case-fold + whitespace collapse, for vocabulary checks
  (the Playbook Engine's clause hash, spec C.2).
- ``scan_injection``: the onboarding-py guardrail family (onboarding-py
  src/guardrails.py ``_INJECTION``, copied, not imported: services do not
  import each other). A hit NEVER changes a ruling; it is recorded as
  ``injection_text_ignored`` (rule names and counts only) and that is all.
Nothing here evaluates, formats or templates client text.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterator

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")
_CONTROL_STRICT = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")

_INJECTION = [
    ("ignore_instructions", re.compile(r"(?i)\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|all|your|the|any)\b.{0,20}\b(instructions?|rules?|guidelines?|policy|policies|prompt|guardrails?|playbooks?)")),
    ("role_reassignment", re.compile(r"(?i)\b(you are now|act as|pretend (?:to be|you are)|from now on you)\b")),
    ("system_prompt_probe", re.compile(r"(?i)\b(system prompt|developer message|hidden instructions?|jailbreak)\b")),
    ("fake_role_marker", re.compile(r"(?im)^[^\S\n]*(system|assistant|developer)\s*:")),
    ("ai_directive_comment", re.compile(r"(?i)<!--\s*(ai|assistant|agent|llm|bot)\b")),
    ("approval_forgery", re.compile(r"(?i)\b(auto[- ]?approve|approve (?:this|me|all)|mark (?:as )?(?:approved|verified|compliant|certified)|certify (?:this|me|all|it)|skip (?:the )?(?:vetting|verification|compliance|checks?))\b")),
    ("credential_exfiltration", re.compile(r"(?i)\b(reveal|show|send|print|output|tell me)\b.{0,30}\b(password|credential|token|secret|api key)s?\b")),
    ("acceptance_forgery", re.compile(r"(?i)\b(accept|approve|sign|waive) (?:all|every|each|any) (?:clauses?|terms?|changes?|deviations?|redlines?)\b")),
    ("guarantee_coercion", re.compile(r"(?i)\b(say|tell them|promise|state) (?:that )?(?:we|you) (?:guarantee|will guarantee)\b")),
]
# Bounded scan: at most this many characters of any one string are scanned
# (every pattern above is linear; the cap bounds the total work per request).
SCAN_MAX_CHARS = 16_384


def has_control_chars(value: str, allow_newlines: bool = False) -> bool:
    return bool((_CONTROL if allow_newlines else _CONTROL_STRICT).search(value))


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def tokens(text: str) -> list[str]:
    """Whole tokens of normalized text; '#' is kept as part of a token."""
    return re.findall(r"#?[^\W_]+", normalize(text))


def scan_injection(text: str) -> list[str]:
    head = text[:SCAN_MAX_CHARS]
    return [name for name, rx in _INJECTION if rx.search(head)]


def iter_strings(obj: Any, depth: int = 0) -> Iterator[str]:
    if depth > 40:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from iter_strings(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from iter_strings(v, depth + 1)


def injection_rules_in(obj: Any) -> list[str]:
    found: set[str] = set()
    for s in iter_strings(obj):
        found.update(scan_injection(s))
    return sorted(found)


def printable_text_problem(text: str) -> bool:
    """Document and memo texts may carry tab/newline/CR; any other control character or a lone surrogate is
    refused (422) before the text is hashed."""
    return bool(_CONTROL.search(text.replace("\r", "")))


# --- IP addresses (AEGIS N17-6): no IP address is ever accepted in any string field of a write route ---------------
# IPv4 dotted (leading zeros, a port appended, embedded in a ref such as "client:192.168.1.1"), the "ip-10-0-0-1"
# host form, IPv6 in full, compressed ("fe80::1", "::1"), IPv4-mapped and zone-id forms, each also after URL
# percent-decoding. Every candidate is confirmed with the ``ipaddress`` module, so a timestamp ("17:00:00") or a
# two-part version ("1.2") is never a hit. Integer ("3232235777") and hex ("0a000001") encodings are NOT detected
# (they are indistinguishable from ordinary numbers and hashes; ADR amendment, AEGIS round 17).
_IPV4 = re.compile(r"(?<![0-9.])([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})(?![0-9]|\.[0-9])")
_IPV4_DASHED = re.compile(r"(?i)(?<![0-9a-z])ip-([0-9]{1,3})-([0-9]{1,3})-([0-9]{1,3})-([0-9]{1,3})(?![0-9])")
_V6_RUN = re.compile(r"[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*(?:%[0-9A-Za-z._-]{1,32})?")
IP_SCAN_MAX_CHARS = 65_536


def _octets_ok(groups) -> bool:
    return all(int(g) <= 255 for g in groups)


def ip_in(value: str) -> bool:
    """True when ``value`` (or its percent-decoded form) contains an IPv4 or IPv6 address."""
    if not isinstance(value, str) or not value:
        return False
    import ipaddress
    from urllib.parse import unquote
    head = value[:IP_SCAN_MAX_CHARS]
    views = [head]
    if "%" in head:
        dec = unquote(head)
        views += [dec, unquote(dec)]
    for v in views:
        for m in _IPV4.finditer(v):
            if _octets_ok(m.groups()):
                return True
        for m in _IPV4_DASHED.finditer(v):
            if _octets_ok(m.groups()):
                return True
        for m in _V6_RUN.finditer(v):
            run = m.group(0)
            base, _, zone = run.partition("%")
            tries = {base, base.strip("."), base.rstrip(":."), base.lstrip(":")}
            if base.startswith("::"):
                tries.add(base.rstrip("."))
            for t in list(tries):
                if t.startswith(":") and not t.startswith("::"):
                    tries.add(t[1:])
                if t.endswith(":") and not t.endswith("::"):
                    tries.add(t[:-1])
            for t in tries:
                if t.count(":") < 2:
                    continue
                try:
                    ipaddress.IPv6Address(t)
                    return True
                except ValueError:
                    continue
    return False


def ip_fields(obj: Any, path: str = "body", depth: int = 0) -> list[str]:
    """Paths (keys included) of every string that carries an IP address, anywhere in ``obj``."""
    out: list[str] = []
    if depth > 40:
        return out
    if isinstance(obj, str):
        if ip_in(obj):
            out.append(path)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and ip_in(k):
                out.append(f"{path}.<key>")
            out += ip_fields(v, f"{path}.{str(k)[:40]}", depth + 1)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj[:10_000]):
            out += ip_fields(v, f"{path}[{i}]", depth + 1)
    return out[:20]
