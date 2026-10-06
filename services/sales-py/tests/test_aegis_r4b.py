"""AEGIS round 4b (Oct 5 2026, NOT BLOCKING, fixed anyway): regressions with the reviewer's cases
(scratchpad aegis-sales-r4b/test_r4b.py)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from clock import FixedClock
from helpers import Harness, rid, wired_ports
from intelligences import i07_quiet_hours as q


def q_sms(h, cid, t):
    return h.post("/sales/v1/outreach/sms", {"request_id": rid(), "contact_id": cid, "template_id": t["template_id"],
                                             "version": 1}, "sales_agent")


# ------------------------------------------------------------------ S5-M1

def test_s5_m1_creighton_saskatchewan_keeps_manitoba_time(tmp_path):
    utc = datetime(2026, 10, 7, 2, 30, tzinfo=timezone.utc)          # 21:30 CDT Creighton, 20:30 Regina
    w = Harness(tmp_path, clock=FixedClock(utc), ports=wired_ports())
    lead = w.vlead(tz="America/Regina", phone="+13065550100")
    w.ok(w.consent(lead["contact_id"]), 201)
    t = w.template(channel="sms", name="s", subject=None, body="Hi {{first_name}}")
    w.refused(q_sms(w, lead["contact_id"], t), 403, "QUIET_HOURS")
    w.ok(w.job("send-queue"))
    assert w.ports.sms.sent == []


@pytest.mark.parametrize("code,zone", [("306", "America/Winnipeg"), ("639", "America/Winnipeg"),
                                       ("474", "America/Winnipeg"), ("306", "America/Edmonton"),
                                       ("418", "America/Blanc-Sablon"), ("250", "America/Creston"),
                                       ("250", "America/Fort_Nelson"), ("867", "America/Iqaluit"),
                                       ("867", "America/Rankin_Inlet"), ("867", "America/Cambridge_Bay"),
                                       ("807", "America/Atikokan"), ("709", "America/Goose_Bay"),
                                       ("334", "America/New_York"), ("775", "America/Denver")])
def test_s5_m1_every_community_zone_offset_is_covered(code, zone):
    """Each community zone's UTC offsets (summer and winter) appear among the zones the code lists."""
    def offs(zs):
        return {ZoneInfo(z).utcoffset(datetime(2026, m, 15)) for z in zs for m in (1, 7)}
    for m in (1, 7):
        assert ZoneInfo(zone).utcoffset(datetime(2026, m, 15)) in offs(q.AREA_ZONES[code])


# ------------------------------------------------------------------ S5-L1

@pytest.mark.parametrize("code", ["231", "269"])
def test_s5_l1_michigan_codes_are_eastern(code):
    assert q.AREA_ZONES[code] == ("America/New_York",)
    assert q.phone_problem(f"+1{code}5550100") is None


def test_s5_l1_only_456_was_dropped_from_the_round_3_table():
    """Every geographic code of the 48cf35f table is still present; 456 (non-geographic) is the only removal."""
    round3 = set("""201 202 203 207 212 215 216 220 223 234 239 240 248 267 272 276 301 302 304 305 313 315 321 324
        330 332 336 339 347 351 352 380 386 401 404 407 410 412 413 419 434 440 443 445 470 475 478 484 508 513 516
        517 518 540 551 561 567 570 571 585 586 603 607 609 610 614 616 617 631 646 656 678 680 689 703 704 706 716
        717 718 724 727 732 734 740 754 757 762 770 772 774 781 786 802 803 804 810 813 814 828 835 838 843 845 848
        854 856 857 860 862 863 864 878 904 908 910 912 914 917 919 929 934 937 941 947 954 959 973 978 980 984 989
        205 210 214 217 218 219 224 225 228 251 254 256 262 269 281 309 312 314 316 318 319 320 325 331 334 337 346
        361 402 405 409 414 417 430 432 456 469 479 501 504 507 512 515 563 573 580 601 605 608 612 615 618 620 630
        636 641 651 660 662 682 701 708 712 713 715 726 731 737 763 769 773 779 785 806 815 816 817 830 832 847 850
        870 872 901 903 913 918 920 931 936 940 952 956 972 979 303 307 385 406 435 505 575 719 720 801 970 983 480
        520 602 623 928 206 209 213 253 279 310 323 341 360 408 415 424 425 442 503 509 510 530 541 559 562 564 619
        626 628 650 657 661 669 702 707 714 725 747 760 775 805 818 820 831 840 858 909 916 925 949 951 971 907 808
        236 250 257 604 672 778 368 403 587 780 825 306 474 639 204 431 584 226 249 263 289 343 354 365 367 382 387
        416 418 437 438 450 468 514 519 548 579 581 613 647 683 705 742 753 819 873 905 942 428 506 782 902 787 939
        340 671 670 684 242 246 264 268 284 345 441 473 649 658 876 664 721 758 767 784 809 829 849 868 869 807 709
        879 867""".split())
    assert round3 - set(q.AREA_ZONES) == {"456"}


# ------------------------------------------------------------------ S5-L2

def test_s5_l2_a_stranger_cannot_opt_someone_else_out(w):
    lead = w.vlead()
    w.ok(w.consent(lead["contact_id"]), 201)
    t = w.template(channel="sms", name="s", subject=None, body="Hi {{first_name}}")
    r = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "from_email": "prankster@evil.test",
                                          "text": "unsubscribe. stop texting 3105550100"}, "provider_events"), 201)
    assert r["class"] == "unsubscribe" and r["held"] is True and r["task_id"]
    c = w.ok(w.get(f"/sales/v1/contacts/{lead['contact_id']}"))
    assert c["phone_suppressed"] is False and c["consent"]["sms:zbm"] is True and c["phone_held"] is True
    w.refused(q_sms(w, lead["contact_id"], t), 403, "PHONE_HOLD")                  # held for a person
    assert len(w.svc.suppression) == 1                                             # the prankster's own address
    w.ok(w.andre(f"/sales/v1/tasks/{r['task_id']}/decision", {"request_id": rid(), "decision": "not_an_opt_out"}))
    w.ok(q_sms(w, lead["contact_id"], t), 201)


def test_s5_l2_the_senders_own_number_is_still_opted_out_automatically(w):
    r = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "sms", "from_phone": "+12125550199",
                                          "text": "STOP. also 3105550100"}, "provider_events"), 201)
    assert r["suppressed"] is True
    from intelligences import i02_identity
    sender = i02_identity.keyed(w.svc.pii_key, "phone", "+12125550199")
    named = i02_identity.keyed(w.svc.pii_key, "phone", "+13105550100")
    assert sender in w.svc.suppression and named not in w.svc.suppression and w.svc._held(named)
