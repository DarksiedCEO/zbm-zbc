"""AEGIS round 4 (Oct 5 2026, NOT BLOCKING, fixed anyway): regressions with the reviewer's cases
(scratchpad aegis-sales-r4/test_r4.py)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from clock import FixedClock
from helpers import Harness, rid, wired_ports
from intelligences import i07_quiet_hours as q


def q_sms(h, cid, t):
    return h.post("/sales/v1/outreach/sms", {"request_id": rid(), "contact_id": cid, "template_id": t["template_id"],
                                             "version": 1}, "sales_agent")


def texted(w):
    lead = w.vlead()
    w.ok(w.consent(lead["contact_id"]), 201)
    t = w.template(channel="sms", name="s", subject=None, body="Hi {{first_name}}, quick question?")
    w.ok(q_sms(w, lead["contact_id"], t), 201)
    w.ok(w.job("send-queue"))
    return lead, t


# ------------------------------------------------------------------ S4-M1 area codes spanning zones

@pytest.mark.parametrize("utc,phone,tz", [
    (datetime(2026, 10, 7, 1, 30, tzinfo=timezone.utc), "+18505550100", "America/Chicago"),     # 21:30 EDT Tallahassee
    (datetime(2026, 10, 7, 3, 30, tzinfo=timezone.utc), "+19285550100", "America/Phoenix"),     # 21:30 MDT Navajo
    (datetime(2026, 10, 7, 4, 30, tzinfo=timezone.utc), "+15415550100", "America/Los_Angeles"),  # 22:30 MDT Ontario OR
    (datetime(2026, 10, 7, 12, 30, tzinfo=timezone.utc), "+17015550100", "America/Chicago"),    # 06:30 MDT Dickinson
    (datetime(2026, 10, 7, 12, 30, tzinfo=timezone.utc), "+18505550100", "America/New_York"),   # 07:30 CDT Pensacola
    (datetime(2026, 10, 7, 12, 30, tzinfo=timezone.utc), "+18125550100", "America/New_York"),   # 07:30 CDT Evansville
])
def test_s4_m1_split_area_codes_use_the_stricter_zone(tmp_path, utc, phone, tz):
    w = Harness(tmp_path, clock=FixedClock(utc), ports=wired_ports())
    lead = w.vlead(tz=tz, phone=phone)
    w.ok(w.consent(lead["contact_id"]), 201)
    t = w.template(channel="sms", name="s", subject=None, body="Hi {{first_name}}")
    w.refused(q_sms(w, lead["contact_id"], t), 403, "QUIET_HOURS")
    w.ok(w.job("send-queue"))
    assert w.ports.sms.sent == []


SPANNING = ["850", "448", "928", "541", "458", "208", "986", "701", "605", "308", "785", "620", "915", "432", "812",
            "930", "219", "574", "906", "270", "364", "606", "423", "931", "334", "775", "580", "907", "867", "807",
            "709", "879", "418", "581", "367", "250", "236", "778", "672", "306", "639"]


def _offsets(zones, when):
    return {ZoneInfo(z).utcoffset(when) for z in zones}


@pytest.mark.parametrize("code", SPANNING)
def test_s4_m1_every_spanning_code_lists_zones_with_different_offsets(code):
    zones = q.AREA_ZONES[code]
    summer, winter = datetime(2026, 7, 1), datetime(2026, 1, 15)
    assert len(_offsets(zones, summer)) >= 2 or len(_offsets(zones, winter)) >= 2


# ------------------------------------------------------------------ S4-L1 the table is complete and geographic

NON_GEOGRAPHIC = {"456", "500", "521", "522", "523", "524", "525", "526", "527", "528", "529", "532", "533", "535",
                  "538", "542", "543", "544", "545", "546", "547", "549", "550", "552", "553", "554", "556", "558",
                  "566", "569", "577", "578", "588", "589", "600", "622", "700", "710", "800", "833", "844", "855",
                  "866", "877", "888", "880", "881", "882", "883", "884", "885", "886", "887", "889", "900"}


def test_s4_l1_every_code_is_a_geographic_npa():
    for code in q.AREA_ZONES:
        assert re.fullmatch(r"[2-9][0-8][0-9]", code), code           # NPA format; N9X is reserved
        assert code[1:] != "11", code                                  # N11 service codes
        assert code not in NON_GEOGRAPHIC, code
        assert not (code.startswith("37") or code.startswith("96")), code   # reserved blocks


@pytest.mark.parametrize("code", ["208", "812", "423", "270", "606", "308", "915", "458", "986", "906", "930"])
def test_s4_l1_codes_the_reviewer_found_missing_are_present(code):
    assert code in q.AREA_ZONES


def test_s4_l1_table_covers_the_geographic_plan():
    assert "456" not in q.AREA_ZONES and len(q.AREA_ZONES) >= 440


@pytest.mark.parametrize("phone,tz", [("+12085550100", "America/Boise"),
                                      ("+18125550100", "America/Indiana/Indianapolis")])
def test_s4_l1_formerly_unknown_codes_text_at_midday(tmp_path, phone, tz):
    w = Harness(tmp_path, clock=FixedClock(datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)), ports=wired_ports())
    lead = w.vlead(tz=tz, phone=phone)
    w.ok(w.consent(lead["contact_id"]), 201)
    t = w.template(channel="sms", name="s", subject=None, body="Hi {{first_name}}")
    assert q_sms(w, lead["contact_id"], t).status_code == 201


def test_s4_l1_toll_free_is_refused(tmp_path):
    assert q.phone_problem("+18005550100") == "AREA_CODE_UNKNOWN"


# ------------------------------------------------------------------ S4-M2 unattributed replies

def test_s4_m2_unattributed_opt_out_naming_a_number_suppresses_it_and_opens_a_task(w):
    lead, t = texted(w)
    r = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "sms", "from_phone": "+12125550199",
                                          "text": "this is jane, stop texting 3105550100"}, "provider_events"), 201)
    assert r["suppressed"] is True and r["task_id"]
    assert w.svc.tasks[r["task_id"]]["kind"] == "review_reply"
    w.refused(q_sms(w, lead["contact_id"], t), 403, "SUPPRESSED")
    w.ok(w.job("send-queue"))
    assert len(w.ports.sms.sent) == 1


def test_s4_m2_unattributed_reply_naming_a_number_holds_it(w):
    lead, t = texted(w)
    r = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "sms", "from_phone": "+12125550199",
                                          "text": "who is this? my wife's number is (310) 555-0100"},
                    "provider_events"), 201)
    assert r["held"] is True
    w.refused(q_sms(w, lead["contact_id"], t), 403, "PHONE_HOLD")


def test_s4_m2_unattributed_email_naming_an_address_holds_that_contacts_phones(w):
    lead, t = texted(w)
    r = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "from_email": "jane.home@gmail.com",
                                          "text": "I am jane@acme-shop.test - quit texting my cell"},
                    "provider_events"), 201)
    assert r["held"] is True and r["task_id"]
    w.refused(q_sms(w, lead["contact_id"], t), 403, "PHONE_HOLD")


def test_s4_m2_unattributed_opt_out_without_identifiers_still_opens_a_task(w):
    lead, t = texted(w)
    r = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "from_email": "jane.home@gmail.com",
                                          "text": "This is Jane from Acme - quit texting my cell"},
                    "provider_events"), 201)
    assert r["task_id"] and w.svc.tasks[r["task_id"]]["kind"] == "review_reply"


def test_s4_m2_unattributed_non_opt_out_without_identifiers_opens_a_task(w):
    r = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "from_email": "x@unknown.test",
                                          "text": "who is this"}, "provider_events"), 201)
    assert r["task_id"] and w.svc.tasks[r["task_id"]]["kind"] == "review_reply"


def test_s4_m2_body_numbers_are_extracted_conservatively():
    from intelligences import i02_identity
    assert i02_identity.phones_in("stop texting 3105550100 or (212) 555-0123, call 911, zip 90001") == \
        ["+13105550100", "+12125550123"]
    assert i02_identity.emails_in("I am Jane.Doe+x@Acme-Shop.test") == ["jane.doe@acme-shop.test"]


def test_s4_m2_unattributed_opt_out_naming_an_address_holds_that_contacts_phones(w):
    lead, t = texted(w)
    r = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "from_email": "jane.home@gmail.com",
                                          "text": "I am jane@acme-shop.test. Unsubscribe me and stop texting me."},
                    "provider_events"), 201)
    assert r["class"] == "unsubscribe" and r["suppressed"] is True and r["held"] is True
    w.refused(q_sms(w, lead["contact_id"], t), 403, "PHONE_HOLD")
