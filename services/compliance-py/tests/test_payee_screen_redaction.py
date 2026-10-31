"""Payee and owner screens through the provider path (POST /compliance/v1/sanctions/screen): the name and aliases go
to the screening provider only and are never stored (ADR 0027; red-team wave C finding). The 10-year screening record
(31 CFR 501.601, MSA 15.5(a)) still proves what was screened: the SHA-256 of the normalized name (and of each alias),
the country and region, the list version, the provider's reference and the result."""

from __future__ import annotations

import hashlib
import json
import os

import helpers as H
from clock import FixedClock
from helpers import NOW, Harness, rid
from intelligences import i08_sanctions

NAME, ALIAS = "Zelphira  Q. Quorvanth-Smith", "Z. Quorvanth Trading"


def screen(h, name=NAME, aliases=(ALIAS,)):
    body = {"request_id": rid("scr"), "subject_id": "payee-77", "role": "payee", "owner_of": None,
            "legal_name": name, "aliases": list(aliases), "dob": "1990-01-01", "country": "US", "region": "US-CA"}
    r = h.post("/compliance/v1/sanctions/screen", body, caller="onboarding")
    assert r.status_code == 200, r.text
    return r.json()


def test_the_name_reaches_the_provider_but_never_the_log_or_the_ledger(tmp_path):
    clock = FixedClock(NOW)
    h = Harness(ports=H.passing_ports(clock), clock=clock, data_dir=str(tmp_path))
    out = screen(h)
    assert h.ports.sanctions.seen[-1][0] == NAME                       # the provider got it
    raw = "".join(open(os.path.join(tmp_path, f), encoding="utf-8", errors="replace").read()
                  for f in os.listdir(tmp_path) if os.path.isfile(os.path.join(tmp_path, f)))
    blob = raw + json.dumps(h.ledger.events) + json.dumps(list(h.svc.screens.values()))
    for word in ("Zelphira", "Quorvanth", "zelphira", "quorvanth", "1990-01-01"):
        assert word not in blob, word
    rec = h.svc.screens[out["screen_id"]]
    assert rec["name_sha256"] == hashlib.sha256(i08_sanctions.normalize_name(NAME).encode()).hexdigest()
    assert rec["aliases_sha256"] == [hashlib.sha256(i08_sanctions.normalize_name(ALIAS).encode()).hexdigest()]
    assert rec["list_version"] and rec["result"] == out["result"] and rec["country"] == "US"
    assert "legal_name" not in rec and "aliases" not in rec


def test_the_normalized_name_hash_ignores_case_spacing_and_unicode_form():
    n = i08_sanctions.normalize_name
    assert n("  JANE q.   doe-smith ") == n("Jane Q. Doe-Smith") == n("Ｊａｎｅ Q. Doe-Smith")
    assert n("Jane Doe") != n("Jane Dough")


def test_the_status_finance_reads_still_works():
    h = Harness()
    screen(h)
    r = h.get("/compliance/v1/sanctions/status", caller="finance_31", subject_id="payee-77", role="payee")
    assert r.status_code == 200 and r.json()["screen_id"]
