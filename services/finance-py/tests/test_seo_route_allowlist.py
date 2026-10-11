"""AEGIS SEO Wave 3 M2: Search & Answer Intelligence (``seo_02``) reaches exactly one route of this service, the
narrow invoice-status read. Every route the app serves is enumerated from the app itself (a new route is covered
without editing this file), called with seo_02's caller token and a valid service bearer, and must answer 403 —
except ``GET /fin/v1/invoices/{invoice_id}/status``. Other callers' reach is unchanged (checked on the routes that
admit any caller)."""

from __future__ import annotations

import re

from fastapi.routing import APIRoute

from helpers import Harness, rid

STATUS = ("GET", "/fin/v1/invoices/{invoice_id}/status")


def _routes(app):
    for r in app.routes:
        if isinstance(r, APIRoute):
            for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
                yield method, r.path


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x-1", path)


def test_every_route_but_the_status_read_is_403_for_seo_02():
    h = Harness().ready()
    seen = 0
    for method, path in _routes(h.app):
        if path == "/health":
            continue
        url = _concrete(path)
        body = {"request_id": rid()}
        r = h.client.request(method, url, headers=h.headers(caller="seo_02"),
                             json=body if method in ("POST", "PUT") else None)
        seen += 1
        if (method, path) == STATUS:
            assert r.status_code == 404, (method, path, r.status_code)           # reached: no such invoice
        else:
            assert r.status_code == 403, (method, path, r.status_code, r.text[:200])
    assert seen > 60                                                           # the enumeration really ran


def test_seo_02_with_andre_s_token_still_reaches_nothing_else():
    from helpers import ANDRE_TOKEN
    h = Harness().ready()
    r = h.client.get("/fin/v1/treasury", headers=h.headers(caller="seo_02", andre=ANDRE_TOKEN))
    assert r.status_code == 403


def test_other_callers_still_reach_the_routes_open_to_any_caller():
    h = Harness().ready()
    for caller in ("scheduler", "onboarding", "compliance_38"):
        assert h.get("/fin/v1/integrity", caller=caller).status_code == 200
        assert h.get("/fin/v1/intelligences", caller=caller).status_code == 200
