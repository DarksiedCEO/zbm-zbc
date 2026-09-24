"""Drive the API to a given stage (used by end-to-end, guardrail and attack tests)."""

from __future__ import annotations

from conftest import TEST_FOUNDER_TOKEN
from samples import (
    CAMPAIGN,
    ZBC_ASSETS,
    zbc_goal,
    zbc_kit_request,
    zbc_license,
    zbc_music_clearance,
    zbm_clearance,
    zbm_requirements,
    zbm_work,
)

C = f"/zbc/campaigns/{CAMPAIGN}"


def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text)
    return r.json()


# --- ZBC --------------------------------------------------------------------------------

def zbc_rights_on_file(api):
    ok(api.post("/rights/licenses", {"actor_id": "rights_desk", "license": zbc_license()}), 201)
    ok(api.post("/rights/clearances", {"actor_id": "rights_desk", "record": zbc_music_clearance()}), 201)


def zbc_live(api, goal=None):
    zbc_rights_on_file(api)
    rb = ok(api.post(f"{C}/rulebooks", {"actor_id": "zbc_rulebook_writer", "goal": goal or zbc_goal()}), 201)
    v = rb["version"]
    ok(api.post(f"{C}/rulebooks/{v}/review", {"actor_id": "zbc_campaign_rulebook"}))
    ok(api.post(f"{C}/rulebooks/{v}/sign", andre=TEST_FOUNDER_TOKEN))
    ok(api.post(f"{C}/rights-check", {"assets": ZBC_ASSETS}))
    return ok(api.post(f"{C}/rulebooks/{v}/go-live"))


def zbc_open(api, goal=None):
    from samples import zbc_source

    live = zbc_live(api, goal)
    ok(api.post(f"{C}/moment-map", zbc_source()))
    ok(api.post(f"{C}/hook-sheets"))
    ok(api.post(f"{C}/kit", zbc_kit_request()), 201)
    ok(api.post(f"{C}/kit/sign", andre=TEST_FOUNDER_TOKEN))
    return live


# --- ZBM --------------------------------------------------------------------------------

def zbm_approved_brief(api, **req_over):
    brief = ok(api.post("/zbm/briefs", {"requirements": zbm_requirements(**req_over)}), 201)
    return ok(api.post(f"/zbm/briefs/{brief['brief_id']}/review", {"actor_id": "zbm_creative_lead"}))


def zbm_work_at_quality(api, work=None):
    ok(api.post("/rights/clearances", {"actor_id": "rights_desk", "record": zbm_clearance()}), 201)
    brief = zbm_approved_brief(api)
    job = ok(api.post(f"/zbm/briefs/{brief['brief_id']}/jobs"), 201)
    w = ok(api.post(f"/zbm/jobs/{job['job_id']}/work", work or zbm_work()), 201)
    ok(api.post(f"/zbm/work/{w['work_id']}/export-validation"))
    ok(api.post(f"/zbm/work/{w['work_id']}/rights"))
    return brief, job, w
