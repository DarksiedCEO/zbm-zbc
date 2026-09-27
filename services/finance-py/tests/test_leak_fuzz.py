"""
FIN-28 / G7 / A9 of the brief: no bank details, TINs, SSNs or card data are stored anywhere. A leak fuzz puts
TIN / SSN / EIN / ITIN / routing / bank-account / card / IBAN shaped values into every caller-supplied text and id
field (and sensitive-looking keys into every body), then scans every byte Finance produced — ledger payloads, the
local log, the audit export — and every response for them.
"""

from __future__ import annotations

import json
import random

import pytest

from helpers import ANDRE_TOKEN, Harness, rid


def _luhn_complete(prefix: str, length: int, rnd) -> str:
    digits = prefix + "".join(str(rnd.randrange(10)) for _ in range(length - len(prefix) - 1))
    for check in range(10):
        cand = digits + str(check)
        total, alt = 0, False
        for ch in reversed(cand):
            d = int(ch)
            if alt:
                d = d * 2 - 9 if d * 2 > 9 else d * 2
            total += d
            alt = not alt
        if total % 10 == 0:
            return cand
    raise AssertionError


def _iban(rnd) -> str:
    bban = "NWBK" + "".join(str(rnd.randrange(10)) for _ in range(14))
    num = "".join(str(int(c, 36)) for c in bban + "GB00")
    check = 98 - int(num) % 97
    return f"GB{check:02d}{bban}"


def secrets(n=40):
    rnd = random.Random(2826)
    out = ["123-45-6789", "987-65-4320", "12-3456789", "912-70-1234", "021000021", "4111 1111 1111 1111",
           "4111-1111-1111-1111", "5500005555555559", "GB29NWBK60161331926819", "000123456789", "1234567890123"]
    for _ in range(n):
        out.append(f"{rnd.randrange(100, 900)}-{rnd.randrange(10, 99)}-{rnd.randrange(1000, 9999)}")
        out.append(f"{rnd.randrange(10, 99)}-{rnd.randrange(1000000, 9999999)}")
        out.append(str(rnd.randrange(10**8, 10**9)))
        out.append(_luhn_complete(rnd.choice(["4", "51", "37", "6011"]), rnd.choice([15, 16]), rnd))
        out.append(_iban(rnd))
        out.append(str(rnd.randrange(10**11, 10**12)))
    return out


SECRETS = secrets()


def _bodies(x, s):
    legal = {"doc_id": "m", "version": 1, "doc_sha256": "b" * 64, "acceptance_id": "a"}
    facts = {"submission_id": "sub-1", "eligible": True, "blockers": [], "clip_review_outcome": "pass",
             "verification": {"verified": True, "reason": s}, "compliance": {"allowed": True, "reference": "r"}}
    return [
        ("/fin/v1/payees", {"request_id": rid(), "payee_id": s, "kind": "clipper", "declared_country": "US"},
         {"caller": "onboarding"}),
        ("/fin/v1/payees", {"request_id": rid(), "payee_id": "p1", "kind": "clipper", "declared_country": "US",
                            "callback_contact_ref": f"vault:{s}"}, {"caller": "onboarding"}),
        ("/fin/v1/payees", {"request_id": s, "payee_id": "p2", "kind": "clipper", "declared_country": "US"},
         {"caller": "onboarding"}),
        ("/fin/v1/payout-handoffs", {"request_id": rid(), "submission_id": "sub-1", "facts": facts},
         {"caller": "creative_production"}),
        ("/fin/v1/payout-handoffs", {"request_id": rid(), "submission_id": s, "facts": {**facts, "submission_id": s}},
         {"caller": "creative_production"}),
        ("/fin/v1/invoices", {"request_id": rid(), "entity": "zbm", "client_id": "c", "kind": "service",
                              "lines": [{"line_code": "creative_services", "quantity": 1, "unit_price": "1.00",
                                         "description": f"pay to {s}"}], "payment_methods": ["ach"],
                              "legal_ref": legal}, {"caller": "onboarding"}),
        ("/fin/v1/invoices", {"request_id": rid(), "entity": "zbm", "client_id": "c", "kind": "service",
                              "lines": [{"line_code": "creative_services", "quantity": 1, "unit_price": "1.00"}],
                              "payment_methods": ["ach"], "legal_ref": legal, "notes": f"account {s}",
                              "template_vars": {"account": s}}, {"caller": "onboarding"}),
        ("/fin/v1/invoices", {"request_id": rid(), "entity": "zbm", "client_id": s, "kind": "service",
                              "lines": [{"line_code": "creative_services", "quantity": 1, "unit_price": "1.00"}],
                              "payment_methods": ["ach"], "legal_ref": legal}, {"caller": "onboarding"}),
        ("/fin/v1/payees/clip-a/callbacks", {"request_id": rid(), "change_event_id": "e", "contact_ref":
                                             f"vault:{s}", "outcome": "confirmed", "notes": s}, {"andre": ANDRE_TOKEN}),
        ("/fin/v1/controls/FC-13/results", {"request_id": rid(), "result": "pass", "evidence_ref": s},
         {"andre": ANDRE_TOKEN}),
        ("/fin/v1/treasury/top-ups", {"request_id": rid(), "amount": "1.00", "notes": s}, {"andre": ANDRE_TOKEN}),
        ("/fin/v1/payees/clip-a/offboarding-notices", {"request_id": rid(), "offboarding_id": s},
         {"caller": "clipper_network"}),
    ]


def test_leak_fuzz_no_sensitive_value_is_ever_stored_or_echoed():
    x = Harness().ready()
    x.fund_campaign()
    x.payee()
    accepted = 0
    for s in SECRETS:
        for path, body, kw in _bodies(x, s):
            r = x.post(path, body, **kw)
            assert r.status_code != 500, (path, s)
            assert s not in r.text and s.replace(" ", "") not in r.text.replace(" ", ""), (path, s, r.text[:300])
            accepted += r.status_code < 400
    blob = x.all_text()
    squashed = blob.replace(" ", "").replace("-", "")
    for s in SECRETS:
        assert s not in blob, s
        assert s.replace(" ", "").replace("-", "") not in squashed, s
    assert accepted == 0          # every one of them is refused at the edge (422), never stored


@pytest.mark.parametrize("key", ["tin", "ssn", "ein", "dob", "date_of_birth", "bank_account_number", "routing_number",
                                 "account_number", "card_number", "iban", "cvv", "pan", "itin", "passport"])
def test_sensitive_keys_refused_everywhere(hr, key):
    for path, body, kw in (("/fin/v1/payees", {"request_id": rid(), "payee_id": "p", "kind": "clipper"},
                            {"caller": "onboarding"}),
                           ("/fin/v1/invoices", {"request_id": rid()}, {"caller": "onboarding"}),
                           ("/fin/v1/treasury/top-ups", {"request_id": rid(), "amount": "1.00"}, {"andre": ANDRE_TOKEN})):
        r = hr.post(path, {**body, key: "x"}, **kw)
        assert r.status_code == 422 and "SENSITIVE_DATA_REFUSED" in r.text, (path, key)
        r = hr.post(path, {**body, "nested": {key: "x"}}, **kw)
        assert r.status_code == 422, (path, key)


def test_g7_no_model_or_record_field_is_named_like_sensitive_data():
    import models
    import re
    rx = re.compile(r"(?i)(account_number|routing|iban|card_?num|\bpan\b|^tin$|_tin$|^tin_(?!match)|ssn|ein_value|dob|birth)")
    for name in dir(models):
        cls = getattr(models, name)
        if isinstance(cls, type) and hasattr(cls, "model_fields"):
            for f in cls.model_fields:
                assert not rx.search(f), (name, f)
    x = Harness().ready()
    x.fund_campaign()
    x.payee()
    x.accrue()
    x.recon()
    b = x.run()["batch"]
    x.approve(b)

    def keys(o, out):
        if isinstance(o, dict):
            for k, v in o.items():
                out.add(k)
                keys(v, out)
        elif isinstance(o, list):
            for v in o:
                keys(v, out)
        return out
    ks = set()
    for rec in x.svc.log.records:
        keys(rec, ks)
    bad = [k for k in ks if rx.search(k)]
    assert not bad, bad
