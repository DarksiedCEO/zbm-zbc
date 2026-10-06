"""Government bids fail closed: the baseline checklist is always there, nothing is ever auto-attested, every item is
attested by Andre one at a time by its hash, and conflict-of-interest / gift / lobbying items are flagged to him."""

from intelligences import i05_gov_checklist
from helpers import rid


def _gov(h, checklist=None, **kw):
    return h.pursuit(kind="government_bid", ref="gov:lacounty-rfp-12", name="County of Los Angeles",
                     domain="lacounty.gov", checklist=checklist, **kw)


def test_baseline_always_and_nothing_attested(h):
    p = _gov(h)
    codes = [i["code"] for i in p["checklist"]]
    assert codes == list(i05_gov_checklist.BASELINE)
    assert all(i["attested_at"] is None for i in p["checklist"])


def test_sensitive_items_open_tasks_for_andre(h):
    p = _gov(h)
    tasks = [t for t in h.ok(h.get("/tasks?status=open")) if t["kind"] == "checklist_sensitive"]
    assert sorted(t["code"] for t in tasks) == sorted(i05_gov_checklist.SENSITIVE)
    assert all(t["target"] == f"pursuit:{p['pursuit_id']}" for t in tasks)


def test_requested_items_and_custom_labels(h):
    p = _gov(h, checklist=[{"code": "insurance_certificate"}, {"code": "custom", "label": "Local hire plan"},
                           {"code": "insurance_certificate"}])
    assert [i["code"] for i in p["checklist"]][-2:] == ["insurance_certificate", "custom"]
    h.refused(h.post("/pursuits", {"request_id": rid(), "brand": "zbm", "kind": "government_bid", "title": "T",
                                   "counterparty": {"ref": "gov:x", "name": "City X", "domain": "x.gov"},
                                   "value": "1.00", "deadline": p["deadline"],
                                   "checklist": [{"code": "custom"}]}), 422, "CHECKLIST_LABEL_REQUIRED")
    h.refused(h.post("/pursuits", {"request_id": rid(), "brand": "zbm", "kind": "government_bid", "title": "T",
                                   "counterparty": {"ref": "gov:x", "name": "City X", "domain": "x.gov"},
                                   "value": "1.00", "deadline": p["deadline"],
                                   "checklist": [{"code": "auto_attest_everything"}]}), 422, "CHECKLIST_CODE_UNKNOWN")
    h.refused(h.post("/pursuits", {"request_id": rid(), "brand": "zbm", "kind": "government_bid", "title": "T",
                                   "counterparty": {"ref": "gov:x", "name": "City X", "domain": "x.gov"},
                                   "value": "1.00", "deadline": p["deadline"],
                                   "checklist": [{"code": "buy_american", "label": "Already attested"}]}), 422,
              "CHECKLIST_LABEL_REFUSED")


def test_submission_refused_until_every_item_attested(h):
    p = _gov(h)
    h.bid(p["pursuit_id"])
    r = h.ready_response(p["pursuit_id"])
    h.refused(h.submit(r), 409, "CHECKLIST_INCOMPLETE")
    items = p["checklist"]
    for item in items[:-1]:
        h.ok(h.post(f"/pursuits/{p['pursuit_id']}/checklist/{item['item_id']}/attest",
                    {"request_id": rid(), "item_sha256": item["item_sha256"]}, andre=True))
    h.refused(h.submit(r), 409, "CHECKLIST_INCOMPLETE")
    last = items[-1]
    h.ok(h.post(f"/pursuits/{p['pursuit_id']}/checklist/{last['item_id']}/attest",
                {"request_id": rid(), "item_sha256": last["item_sha256"]}, andre=True))
    assert h.ok(h.submit(r), 201)["status"] == "queued"
    assert len(h.ledger.of_type("checklist_attested")) == len(items)
    assert not [t for t in h.ok(h.get("/tasks?status=open")) if t["kind"] == "checklist_sensitive"]


def test_attest_is_andre_only_exact_and_once(h):
    p = _gov(h)
    item = p["checklist"][0]
    url = f"/pursuits/{p['pursuit_id']}/checklist/{item['item_id']}/attest"
    h.refused(h.post(url, {"request_id": rid(), "item_sha256": item["item_sha256"]}), 403)
    h.refused(h.post(url, {"request_id": rid(), "item_sha256": item["item_sha256"]}, caller="dashboard"), 403)
    other = p["checklist"][1]["item_sha256"]
    h.refused(h.post(url, {"request_id": rid(), "item_sha256": other}, andre=True), 409, "CHECKLIST_HASH_MISMATCH")
    h.ok(h.post(url, {"request_id": rid(), "item_sha256": item["item_sha256"]}, andre=True))
    h.refused(h.post(url, {"request_id": rid(), "item_sha256": item["item_sha256"]}, andre=True), 409,
              "CHECKLIST_ALREADY_ATTESTED")
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/checklist/nb-chk-{'0' * 40}/attest",
                     {"request_id": rid(), "item_sha256": item["item_sha256"]}, andre=True), 404,
              "CHECKLIST_ITEM_NOT_FOUND")


def test_attest_refused_on_non_government(h):
    p = h.pursuit()
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/checklist/nb-chk-{'0' * 40}/attest",
                     {"request_id": rid(), "item_sha256": "0" * 64}, andre=True), 409, "NOT_A_GOVERNMENT_BID")


def test_no_route_attests_in_bulk_and_no_field_auto_attests(h):
    p = _gov(h)
    h.refused(h.post("/pursuits", {"request_id": rid(), "brand": "zbm", "kind": "government_bid", "title": "T",
                                   "counterparty": {"ref": "gov:y", "name": "City Y", "domain": "y.gov"},
                                   "value": "1.00", "deadline": p["deadline"],
                                   "checklist": [{"code": "buy_american", "attested": True}]}), 422)
    paths = {r.path for r in h.client.app.app.app.routes}
    assert any("attest" in x for x in paths)
    assert not any("attest" in x and "{item_id}" not in x for x in paths)


def test_flagged_notes_raise_tasks_and_response_flags(h):
    p = _gov(h, notes="Buyer's procurement officer is a cousin of our account lead.")
    assert p["flags"] == ["CONFLICT_OF_INTEREST"]
    assert any(t["kind"] == "sensitivity_flag" and t["code"] == "CONFLICT_OF_INTEREST"
               for t in h.ok(h.get("/tasks")))
    h.bid(p["pursuit_id"])
    r = h.response(p["pursuit_id"], [{"custom": "Our plan."}])
    assert r["versions"][0]["flags"] == ["CONFLICT_OF_INTEREST"]
    h.refused(h.post(f"/responses/{r['response_id']}/approve",
                     {"request_id": rid(), "version": 1, "content_sha256": r["versions"][0]["content_sha256"],
                      "acknowledged_flags": []}, andre=True), 409, "FLAGS_NOT_ACKNOWLEDGED")


def test_close_flag_task_needs_andre(h):
    _gov(h, notes="lobbying contact list")
    t = next(t for t in h.ok(h.get("/tasks")) if t["kind"] == "sensitivity_flag")
    h.refused(h.post(f"/tasks/{t['task_id']}/close", {"request_id": rid()}, caller="dashboard"), 403)
    assert h.ok(h.post(f"/tasks/{t['task_id']}/close", {"request_id": rid()}, andre=True))["status"] == "closed"


def test_addendum_items_can_be_added_never_pre_attested(h):
    p = _gov(h)
    h.bid(p["pursuit_id"])
    r = h.ready_response(p["pursuit_id"])
    h.attest_all(p["pursuit_id"])
    p2 = h.ok(h.post(f"/pursuits/{p['pursuit_id']}/checklist",
                     {"request_id": rid(), "items": [{"code": "custom", "label": "Addendum 2 insurance rider"},
                                                     {"code": "conflict_of_interest_disclosure"}]}))
    new = [i for i in p2["checklist"] if i["attested_at"] is None]
    assert [i["code"] for i in new] == ["custom"]                  # a baseline item is never duplicated
    h.refused(h.submit(r), 409, "CHECKLIST_INCOMPLETE")
    h.ok(h.post(f"/pursuits/{p['pursuit_id']}/checklist/{new[0]['item_id']}/attest",
                {"request_id": rid(), "item_sha256": new[0]["item_sha256"]}, andre=True))
    assert h.ok(h.submit(r), 201)["status"] == "queued"


def test_addendum_sensitive_item_opens_task(h):
    p = _gov(h, checklist=[])
    before = len(h.ok(h.get("/tasks")))
    h.ok(h.post(f"/pursuits/{p['pursuit_id']}/checklist", {"request_id": rid(), "items": [
        {"code": "custom", "label": "Gift disclosure form"}]}))
    tasks = h.ok(h.get("/tasks"))
    assert len(tasks) == before + 1 and any(t["code"] == "custom" for t in tasks)   # a flagged label is sensitive
    h.refused(h.post(f"/pursuits/{h.pursuit()['pursuit_id']}/checklist",
                     {"request_id": rid(), "items": [{"code": "buy_american"}]}), 409, "NOT_A_GOVERNMENT_BID")
