"""AEGIS r9 (Oct 30 2026) on 14f4d9a: the rejected exit, Andre's live read of a hold, and the two surviving mutants.

R9-3  ``ofac_rejected_return`` asks Compliance AT RELEASE TIME: if the refusal now rests on a sanctions MATCH the hold
      is upgraded and the release refused; if Compliance cannot answer, refused (fail closed). Andre can read a hold's
      stored ground and Compliance's current one (``GET /fin/v1/invoices/{id}/compliance-hold``, Andre's token only)
      immediately before he refunds at Stripe (runbook step G).
g3    A sanctions hold recorded before AEGIS r8 (``sanctions: true``, no ground) is a MATCH: never a rejected return.
g6    The Compliance client reads only an exact ``"place"`` as place; a missing, null, upper-case or listed ground on a
      sanctions refusal is a match.
"""

from __future__ import annotations

from helpers import ANDRE_TOKEN
from test_compliance_gate_adr0024 import _stripe_hr, activation_doc, cmp_client
from test_compliance_hold_r4 import _f7r, _hold
from test_compliance_hold_r5 import _pay_then
from test_compliance_hold_r7 import _andre_get
from test_compliance_hold_r8 import _ground, _refund, _rejected


def _place_hold_refunded(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _ground(hr, "place"))
    h = _hold(hr, inv)
    _refund(hr, pi)
    return hr, inv, _f7r(hr, h["receipt_id"])[0]["entry_id"]


# --- R9-3: the rejected exit re-reads Compliance ------------------------------------------------------------------------

def test_r9_3_a_rejected_return_is_refused_when_compliance_now_says_match(tmp_path):
    hr, inv, entry = _place_hold_refunded(tmp_path)
    _ground(hr, "match")                                    # an SDN match recorded after the payment was held
    r = _rejected(hr, inv, entry)
    assert r.status_code == 409, r.text
    assert _hold(hr, inv)["sanctions_ground"] == "match"    # and the hold now says so (sticky)
    _ground(hr, "place")
    assert _rejected(hr, inv, entry).status_code == 409     # never back to place


def test_r9_3_a_rejected_return_is_refused_when_compliance_cannot_answer(tmp_path):
    hr, inv, entry = _place_hold_refunded(tmp_path)
    hr.f["compliance"].activation_available = False
    r = _rejected(hr, inv, entry)
    assert r.status_code == 409 and "compliance_38" in r.text, r.text
    assert _hold(hr, inv) is not None


def test_r9_3_a_rejected_return_still_works_for_a_place_compliance_confirms(tmp_path):
    hr, inv, entry = _place_hold_refunded(tmp_path)
    assert _rejected(hr, inv, entry).status_code == 200


def test_r9_3_andre_reads_a_holds_stored_and_current_ground(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _ground(hr, "place"))
    path = f"/fin/v1/invoices/{inv['invoice_id']}/compliance-hold"
    r = _andre_get(hr, path)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["held"] is True and d["sanctions_ground"] == "place"
    assert d["compliance_now"] == {"available": True, "allowed": False, "sanctions_ground": "place"}
    assert d["ground_now"] == "place" and d["rejected_return_possible"] is True
    _ground(hr, "match")
    d = _andre_get(hr, path).json()
    assert d["compliance_now"]["sanctions_ground"] == "match"
    assert d["ground_now"] == "match" and d["rejected_return_possible"] is False
    hr.f["compliance"].activation_available = False
    d = _andre_get(hr, path).json()
    assert d["compliance_now"] == {"available": False} and d["rejected_return_possible"] is False
    # Andre's token only: every other caller is refused
    for caller in ("onboarding", "scheduler", "seo_02"):
        assert hr.get(path, caller=caller).status_code in (401, 403)


def test_r9_3_the_live_read_of_an_invoice_without_a_hold(tmp_path):
    hr = _stripe_hr(tmp_path)
    from test_stripe_incoming import rr_invoice
    inv = rr_invoice(hr, "1200.00")
    d = _andre_get(hr, f"/fin/v1/invoices/{inv['invoice_id']}/compliance-hold").json()
    assert d["held"] is False and d["rejected_return_possible"] is False


# --- g3: a legacy sanctions hold (no ground) is a match -----------------------------------------------------------------

def test_g3_a_legacy_sanctions_hold_is_never_a_rejected_return(tmp_path):
    hr, inv, entry = _place_hold_refunded(tmp_path)
    i = hr.svc.db["invoices"][inv["invoice_id"]]
    legacy = {k: v for k, v in i["held_compliance"].items() if k != "sanctions_ground"}
    hr.svc.db["invoices"][inv["invoice_id"]] = {**i, "held_compliance": legacy}     # as recorded before AEGIS r8
    r = _rejected(hr, inv, entry)
    assert r.status_code == 409, r.text
    assert hr.svc.db["invoices"][inv["invoice_id"]].get("held_compliance")


# --- g6: the HTTP client reads only an exact "place" as place -----------------------------------------------------------

def test_g6_the_client_reads_a_missing_or_odd_ground_as_match():
    base = dict(client_id="zbm-client-1", decision="REFUSE", sanctions_grounded=True, reasons=["x"])
    path = "/compliance/v1/clients/zbm-client-1/activation"
    for raw, want in (("place", "place"), (None, "match"), ("PLACE", "match"), (["place"], "match"),
                      ("match", "match"), ("", "match"), ("place ", "match")):
        doc = activation_doc(**base, sanctions_ground=raw)
        a = cmp_client({path: (200, doc)}).client_activation("zbm-client-1")
        assert a.available and not a.allowed and a.sanctions is True and a.ground == want, (raw, a)
    doc = activation_doc(**base)
    doc.pop("sanctions_ground", None)
    a = cmp_client({path: (200, doc)}).client_activation("zbm-client-1")
    assert a.ground == "match", a
