"""Partners: Andre approves each rate; tax information as references only; agreements through Legal (37), refused
LEGAL_UNAVAILABLE while Legal is a stand-in; a partner deal is won only by Andre with an agreement in force."""

from intelligences import i12_tax_refs
from helpers import FakeLegal, Harness, rid, wired_ports


def test_rate_proposed_then_approved_by_andre(h):
    p = h.partner()
    p = h.rate(p["partner_id"], "12.50", approve=False)
    assert p["rate_proposed"]["rate_pct"] == "12.50" and p["rate_approved"] is None
    body = {"request_id": rid(), "version": 1, "binding_sha256": p["rate_proposed"]["binding_sha256"]}
    h.refused(h.post(f"/partners/{p['partner_id']}/rate/approve", body), 403)
    h.refused(h.post(f"/partners/{p['partner_id']}/rate/approve", {**body, "binding_sha256": "0" * 64}, andre=True),
              409, "RATE_VERSION_STALE")
    p = h.ok(h.post(f"/partners/{p['partner_id']}/rate/approve", body, andre=True))
    assert p["rate_approved"]["rate_pct"] == "12.50"
    h.refused(h.post(f"/partners/{p['partner_id']}/rate/approve", {**body, "request_id": rid()}, andre=True), 409,
              "RATE_ALREADY_APPROVED")
    assert "12.50" not in str(h.ledger.of_type("rate_approved")[0]["_payload"])


def test_rate_bounds_and_types(h):
    p = h.partner()
    for bad in ("0.00", "50.01", "100.00", "12.5", "12", 12.5, 12):
        r = h.post(f"/partners/{p['partner_id']}/rate", {"request_id": rid(), "version": 1, "rate_pct": bad})
        assert r.status_code == 422, (bad, r.text)
    assert h.ok(h.post(f"/partners/{p['partner_id']}/rate", {"request_id": rid(), "version": 1, "rate_pct": "50.00"}))
    h.refused(h.post(f"/partners/{p['partner_id']}/rate", {"request_id": rid(), "version": 1, "rate_pct": "5.00"}),
              409, "RATE_VERSION_STALE")


def test_new_proposal_does_not_unseat_approved_rate_until_approved(h):
    p = h.partner()
    h.rate(p["partner_id"], "10.00")
    p = h.rate(p["partner_id"], "20.00", approve=False)
    assert p["rate_approved"]["rate_pct"] == "10.00" and p["rate_proposed"]["rate_pct"] == "20.00"


def test_raw_tax_ids_refused_everywhere_in_partner_bodies(h):
    p = h.partner()
    for ref in ("vault:tax:123-45-6789-abcdefgh", "vault:tax:123456789abcdefgh", "vault:tax:12-3456789abcdefghij",
                "tok:ab_123_45_6789_cdefghij"):
        h.refused(h.payee(p["partner_id"], ref), 422, "TAX_ID_RAW_REFUSED")
    for name in ("SSN 123-45-6789", "EIN: 12-3456789", "Co 987654321", "taxpayer id 1 23 45 6789", "A 123|45|6789"):
        r = h.post("/partners", {"request_id": rid(), "partner_key": "p-" + rid()[2:12], "kind": "referral",
                                 "brands": ["zbm"], "name": name, "domain": "n.test"})
        assert r.status_code == 422 and "6789" not in r.text and "3456789" not in r.text, name
    r = h.post("/partners", {"request_id": rid(), "partner_key": "p-notes", "kind": "referral", "brands": ["zbm"],
                             "name": "N", "domain": "n.test", "notes": "anything"})
    assert r.status_code == 422                               # partner records carry no free text at all
    for key in ("tin", "ssn", "ein", "itin", "tax_id", "taxpayer_id", "social_security_number"):
        r = h.post(f"/partners/{p['partner_id']}/payee", {"request_id": rid(), "finance_payee_ref": "fin:payee-1",
                                                          "tax_info_ref": "vault:tax:abcdefghijklmnop", key: "x"},
                   andre=True)
        h.refused(r, 422)


def test_tax_ref_shape(h):
    p = h.partner()
    for ref in ("123-45-6789", "vault:abc", "vault:tax:short", "https://vault/x", "tok:" + "a" * 65,
                "vault:tax:1a2b3c4d5e6f7g8h9i", "tok:12345678abcdefgh9"):
        r = h.payee(p["partner_id"], ref)
        assert r.status_code == 422, ref
    p2 = h.ok(h.payee(p["partner_id"], "tok:Ab_cd-EF_gh-IJ_kl12"))
    assert p2["payee"]["set"] is True and "tax_info_ref" not in str(p2)


def test_payee_ref_digit_rule(h):
    p = h.partner()
    r = h.post(f"/partners/{p['partner_id']}/payee", {"request_id": rid(), "finance_payee_ref": "fin:p1a2b3c4d5e6f7g8h9",
                                                      "tax_info_ref": "vault:tax:abcdefghijklmnop"}, andre=True)
    h.refused(r, 422)                     # nine digits cannot fit a Finance payee ref (models and i12)
    assert not i12_tax_refs.payee_ref_ok("fin:p1a2b3c4d5e6f7g8h9")


def test_payee_is_andre_only(h):
    p = h.partner()
    h.refused(h.post(f"/partners/{p['partner_id']}/payee", {"request_id": rid(), "finance_payee_ref": "fin:payee-1",
                                                            "tax_info_ref": "vault:tax:abcdefghijklmnop"}), 403)


def test_agreement_refused_while_legal_is_a_stand_in(h):
    p = h.partner()
    h.refused(h.post(f"/partners/{p['partner_id']}/agreements", {"request_id": rid(), "kind": "referral_agreement"}),
              503, "LEGAL_UNAVAILABLE")
    assert not any(e["event_type"] == "agreement_handoff" for e in h.ledger.events)
    pur = h.pursuit()
    h.refused(h.post(f"/pursuits/{pur['pursuit_id']}/agreements", {"request_id": rid(), "kind": "nda"}), 503,
              "LEGAL_UNAVAILABLE")


def test_agreement_kinds_match_partner_kind(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    p = h.partner(kind="white_label", key="wl-co")
    h.refused(h.post(f"/partners/{p['partner_id']}/agreements", {"request_id": rid(), "kind": "referral_agreement"}),
              422, "AGREEMENT_KIND_INVALID")
    out = h.ok(h.post(f"/partners/{p['partner_id']}/agreements", {"request_id": rid(),
                                                                   "kind": "white_label_agreement"}), 201)
    assert out["status"] == "delivered" and h.ports.legal.sent[0].kind == "white_label_agreement"
    pur = h.pursuit()
    h.refused(h.post(f"/pursuits/{pur['pursuit_id']}/agreements", {"request_id": rid(), "kind": "alliance_agreement"}),
              422, "AGREEMENT_KIND_INVALID")


def test_deal_won_needs_andre_rate_and_legal(h):
    p = h.partner()
    d = h.deal(p["partner_id"])
    body = {"request_id": rid(), "agreement_kind": "referral_agreement"}
    h.refused(h.post(f"/partner-deals/{d['deal_id']}/won", body), 403)
    h.refused(h.post(f"/partner-deals/{d['deal_id']}/won", body, andre=True), 409, "RATE_NOT_APPROVED")
    h.rate(p["partner_id"])
    h.refused(h.post(f"/partner-deals/{d['deal_id']}/won", body, andre=True), 503, "LEGAL_UNAVAILABLE")
    assert h.ok(h.get(f"/partner-deals/{d['deal_id']}"))["status"] == "registered"


def test_deal_won_refused_when_agreement_not_in_force(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(legal=FakeLegal(in_force="not_in_force")))
    p = h.partner()
    h.rate(p["partner_id"])
    d = h.deal(p["partner_id"])
    h.refused(h.post(f"/partner-deals/{d['deal_id']}/won", {"request_id": rid(), "agreement_kind":
                                                            "referral_agreement"}, andre=True), 409,
              "AGREEMENT_NOT_IN_FORCE")


def test_deal_won_snapshots_rate(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    d = h.won_deal(rate="10.00")
    assert d["status"] == "won" and d["rate"]["rate_pct"] == "10.00"
    h.rate(d["partner_id"], "30.00")
    assert h.ok(h.get(f"/partner-deals/{d['deal_id']}"))["rate"]["rate_pct"] == "10.00"


def test_partner_deal_brand_must_be_partnered(h):
    p = h.partner(brands=("zbc",), key="clips-only")
    r = h.post("/partner-deals", {"request_id": rid(), "partner_id": p["partner_id"], "brand": "zbm",
                                  "counterparty": {"ref": "org:z", "name": "Zed", "domain": "zed.test"},
                                  "deal_value": "10.00"})
    h.refused(r, 422, "PARTNER_BRAND_MISMATCH")


def test_deal_value_positive_and_canonical(h):
    p = h.partner()
    for bad in ("0.00", 100.0, "100", "1e3"):
        r = h.post("/partner-deals", {"request_id": rid(), "partner_id": p["partner_id"], "brand": "zbm",
                                      "counterparty": {"ref": "org:z", "name": "Zed", "domain": "zed.test"},
                                      "deal_value": bad})
        assert r.status_code == 422, bad


def test_partner_over_threshold_needs_deal_approval_before_win(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    p = h.partner()
    h.rate(p["partner_id"])
    d = h.deal(p["partner_id"], value="15000.00")
    body = {"request_id": rid(), "agreement_kind": "referral_agreement"}
    h.refused(h.post(f"/partner-deals/{d['deal_id']}/won", body, andre=True), 409, "DEAL_APPROVAL_REQUIRED")
    h.ok(h.post(f"/partner-deals/{d['deal_id']}/deal-approval",
                {"request_id": rid(), "binding_sha256": d["deal_gate"]["binding_sha256"]}, andre=True))
    assert h.ok(h.post(f"/partner-deals/{d['deal_id']}/won", body, andre=True))["status"] == "won"
