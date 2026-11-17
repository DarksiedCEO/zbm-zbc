"""F-HARN-1: the leak harness removes ONLY the vault records' ciphertext and nonce (random bytes) before the probes are
searched; a PIN anywhere else (another field of the vault file, a line of another shape, another file, a value that is
not strict base64url of the record's size) is still found (G-7: tighter, never weaker). Synthetic values only."""

from __future__ import annotations

import base64
import json
import os
from types import SimpleNamespace

import pytest

import leak_harness_v2 as lh

PIN = "2567"  # synthetic
CASE = SimpleNamespace(probes=(PIN,), value=PIN, family="pin", name="pin/synthetic")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _record(**over) -> dict:
    # id / t without digits (no chance match outside c and n); k is a fixed synthetic key id
    r = {"v": 1, "id": _b64(b"\xff" * 16), "t": _b64(b"\xfb" * 32), "k": _b64(b"key-1"),
         "n": _b64(os.urandom(12)), "c": "Ab" + PIN + "xY" + _b64(os.urandom(48))}  # PIN as a chance digit run in c
    r.update(over)
    return r


def _line(r: dict) -> str:
    return json.dumps(r, sort_keys=True, separators=(",", ":"))


def _found(name: str, raw: str) -> bool:
    return lh.leaked(lh._scrub(lh.scrub_vault_file(name, raw)), CASE) is not None


@pytest.mark.parametrize("name", sorted(lh.VAULT_FILES))
def test_chance_digits_in_vault_ciphertext_or_nonce_are_not_a_leak(name):
    raw = _line(_record()) + "\n" + _line(_record(n="Ab" + PIN + "xY" + _b64(os.urandom(6)))) + "\n"
    assert lh.leaked(raw, CASE) == PIN              # the old harness reported this (F-HARN-1)
    out = lh.scrub_vault_file(name, raw)
    assert not _found(name, raw)
    assert out.count('"c":"~"') == 2 and out.count('"n":"~"') == 2 and out.endswith("\n")


@pytest.mark.parametrize("field", ["id", "t", "k", "v"])
def test_pin_in_any_other_vault_field_is_found(field):
    r = _record(c=_b64(os.urandom(48)))
    r[field] = PIN if field == "v" else r[field][:4] + "-" + PIN
    assert _found("citation_vault.jsonl", _line(r))


@pytest.mark.parametrize("bad", [
    {"x": "pin " + PIN},                                    # an extra field: not the record shape, nothing removed
    {"c": "pin=" + PIN + "=here"},                          # not strict base64url
    {"c": "Ab" + PIN + "x"},                                # too short for AES-GCM (< 16 bytes)
    {"n": "Ab" + PIN + "xY"},                               # not a 12-byte nonce
    {"c": 2567},                                            # not a string
])
def test_pin_in_a_line_not_of_the_exact_record_shape_is_found(bad):
    r = _record()
    r.update(bad)
    assert _found("operator_text_vault.jsonl", _line(r))


def test_pin_in_a_non_record_line_of_a_vault_file_is_found():
    raw = _line(_record(c=_b64(os.urandom(48)))) + "\nnote pin " + PIN + "\n{not json " + PIN + "\n"
    assert _found("citation_vault.jsonl", raw)


@pytest.mark.parametrize("name", ["seo_log.jsonl", "audit_vault.jsonl", "citation_vault.jsonl.bak", "x.jsonl"])
def test_vault_shaped_line_in_any_other_file_is_searched_whole(name):
    raw = _line(_record())
    assert lh.scrub_vault_file(name, raw) == raw
    assert _found(name, raw)
