"""AEGIS round 4 on b75a77b (not blocking; cleared for wiring): the Lows and Infos closed. Each test asserts the fix;
all fail on b75a77b except the control ``test_l2_a_well_formed_foreign_currency_*`` (a valid non-USD code is still
recorded and refunded). L1 is the live run's refund selection: devtools/live_run.py, run five times."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from helpers import GOOGLE_SCOPES, PRODUCT, SHOP_A, rid

CONT = "accounts/1/containers/2"
ADR = Path(__file__).resolve().parents[3] / "docs" / "adr" / "0017-client-fix-lane-architecture.md"


def _tasks(h):
    return h.ok(h.get("/tasks"))


def _codes(h):
    return [t["code"] for t in _tasks(h)]


def _accepted_job(h):
    conn = h.connection()
    h.t.shop(SHOP_A).product(PRODUCT)
    j = h.job([h.finding(conn, target=PRODUCT)])
    h.ok(h.accept(j))
    return j


def _body(j, **over):
    b = {"request_id": rid(), "finance_event_id": "fin-evt-" + "a" * 40, "job_id": j["job_id"],
         "kind": "payment_confirmed", "amount": j["quote"]["total"], "currency": "USD",
         "quote_sha256": j["quote_sha256"]}
    b.update(over)
    return b


# ============================================================================== L2 currency is an ISO 4217 shape

@pytest.mark.parametrize("cur", ["Jane Q. Client, 12 Elm St, DOB 1970-01-01", "usd", "US", "USDX", "€€€"])
def test_l2_a_currency_that_is_not_three_capitals_is_a_malformed_body_recorded_by_hash(h, cur):
    j = _accepted_job(h)
    h.refused(h.post("/finance/events", _body(j, currency=cur), caller="finance_31"), 422)
    assert h.ok(h.get("/refunds")) == []                              # never a refund carrying the text
    assert len(h.ledger.of_type("finance_event_malformed")) == 1
    if len(cur) > 5:                                                  # the free text is stored nowhere
        assert cur not in json.dumps(h.svc.log.records, default=str, ensure_ascii=False)
        assert cur not in json.dumps(h.ledger.events, default=str, ensure_ascii=False)
    assert h.ok(h.get(f"/jobs/{j['job_id']}"))["payment"] is None


def test_l2_a_well_formed_foreign_currency_is_still_recorded_and_refunded(h):
    j = _accepted_job(h)
    h.ok(h.post("/finance/events", _body(j, currency="EUR"), caller="finance_31"))
    assert [r["reason"] for r in h.ok(h.get("/refunds"))] == ["currency_not_supported"]


# ============================================================================== L3 malformed tasks are capped

def test_l3_malformed_bodies_beyond_the_cap_roll_into_one_digest_task(h):
    assert h.svc.settings.malformed_tasks_max == 5
    for i in range(9):
        h.refused(h.post("/finance/events", {"request_id": rid(), "junk": i}, caller="finance_31"), 422)
    codes = _codes(h)
    assert codes.count("FINANCE_EVENT_MALFORMED") == 5
    [digest] = [t for t in _tasks(h) if t["code"] == "FINANCE_EVENT_MALFORMED_DIGEST"]
    assert digest["count"] == 4 and digest["status"] == "open"
    assert len(h.ledger.of_type("finance_event_malformed")) == 9         # every body is still recorded
    # Andre closes the digest: the next overflow body opens a NEW digest task
    h.ok(h.post(f"/tasks/{digest['task_id']}/close", {"request_id": rid()}, andre=True))
    h.refused(h.post("/finance/events", {"request_id": rid(), "junk": 99}, caller="finance_31"), 422)
    digests = [t for t in _tasks(h) if t["code"] == "FINANCE_EVENT_MALFORMED_DIGEST"]
    assert len(digests) == 2 and [t["count"] for t in digests if t["status"] == "open"] == [1]


def test_l3_the_cap_is_a_documented_setting():
    import config
    assert config.load({"CFX_SERVICE_TOKEN": "s" * 40, "CFX_NON_PRODUCTION": "1",
                        "CFX_MALFORMED_TASKS_MAX": "2"}).malformed_tasks_max == 2
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "CFX_MALFORMED_TASKS_MAX" in readme


# ============================================================================== Info 1 a keyed digest

def test_info1_the_malformed_body_digest_is_keyed_not_a_plain_sha256(h):
    j = _accepted_job(h)
    body = {"request_id": "00000000-0000-4000-8000-000000000000", "finance_event_id": "fin-evt-x",
            "job_id": j["job_id"], "kind": "payment_confirmed", "amount": "10.00", "currency": "USD",
            "quote_sha256": "0" * 64, "card_number": "4111111111111111"}
    h.refused(h.post("/finance/events", body, caller="finance_31"), 422)
    plain = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                           .encode()).hexdigest()
    assert plain not in json.dumps(h.ledger.events, default=str)
    assert plain not in json.dumps(h.svc.log.records, default=str)
    assert len(h.ledger.of_type("finance_event_malformed")) == 1


# ============================================================================== Info 2 the conflict task names it all

def test_info2_the_rollback_failed_task_names_the_foreign_version_and_the_left_run_workspace(h):
    from connectors.tag_manager import RUN_PREFIX
    gtm = h.t.gtm
    gtm.tag("11", paused=True)
    gtm.tag("12", paused=False)
    gtm.publish_initial()
    conn = h.connection(connector="gtm", account=CONT, scopes=GOOGLE_SCOPES["gtm"])
    target = f"{CONT}/tags/11"
    f = h.finding(conn, check="gtm_tag_paused", target=target)
    j = h.job([f])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    it = h.item(j["job_id"])
    h.ok(h.plan(j, [{"item_id": it["item_id"], "connection_id": conn["connection_id"],
                     "ops": [{"op": "gtm.tag.update", "target": target, "field": "paused", "after": False}]}]))
    h.ok(h.approve(j["job_id"]))
    rel = {}

    def act(c, r, real):
        gtm.tags["11"].pop("paused", None)
        rel["v"] = gtm._version("client release", gtm.tags)
        gtm.live = rel["v"]
        raise TimeoutError("lost")                                    # our create_version never landed
    h.t.rules.append((lambda c, r: r.url.endswith(":create_version") and "v" not in rel, act))
    out = h.ok(h.apply(j["job_id"]))
    assert out["items"][0]["status"] == "rollback_failed"
    [t] = [t for t in _tasks(h) if t["code"] == "ROLLBACK_FAILED"]
    assert t["ref"] == rel["v"]
    left = [gtm.ws_path(k) for k, w in gtm.workspaces.items() if w["name"].startswith(RUN_PREFIX)]
    assert left and t["run_workspaces"] == left


# ============================================================================== Info 3 UTS #46 / IDNA confusables

def _puny(label: str) -> str:
    return "xn--" + label.encode("punycode").decode("ascii") + ".com"


@pytest.mark.parametrize("label,flagged", [
    ("ｇｏｏｇｌｅ", True),                    # ｇｏｏｇｌｅ fullwidth
    ("\U0001d420\U0001d428\U0001d428\U0001d420\U0001d425\U0001d41e", True),  # 𝐠𝐨𝐨𝐠𝐥𝐞 mathematical bold
    ("аpple", True),                                              # аpple: Cyrillic а + Latin
    ("аррӏе", True),                          # аррӏе: all Cyrillic look-alikes
    ("bücher", False),                                            # bücher: a real IDN
    ("café", False),
    ("中国", False),                                           # 中国
])
def test_info3_confusable_hosts(label, flagged):
    from svc_jobs import host_detail
    d = host_detail(_puny(label))
    assert d["unicode"] == label + ".com"
    assert d["confusable"] is flagged, d
    assert bool(d["reasons"]) is flagged


def test_info3_fullwidth_and_mathematical_are_flagged_as_idna_invalid():
    from svc_jobs import host_detail
    for label in ("ｇｏｏｇｌｅ", "\U0001d420\U0001d428\U0001d428\U0001d420\U0001d425\U0001d41e"):
        d = host_detail(_puny(label))
        assert {"idna_invalid", "not_uts46_mapped", "ascii_lookalike"} <= set(d["reasons"])


# ============================================================================== Info 4 the ADR states the limits

def test_info4_the_adr_states_what_l4_detection_does_not_cover_and_the_reaper_window():
    text = re.sub(r"\s+", " ", ADR.read_text())
    assert "re-publishes an existing older version" in text and "is NOT detected" in text
    assert "getStatus and the DELETE" in text and "one-request window" in text
