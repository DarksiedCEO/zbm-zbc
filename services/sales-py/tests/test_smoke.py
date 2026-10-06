def test_health_open(h):
    r = h.client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}      # nothing else unauthenticated
    assert r.headers["cache-control"] == "no-store"


def test_status_shows_every_port_not_wired(h):
    s = h.ok(h.get("/sales/v1/status"))
    assert s["integrity"]["ok"] is True and s["in_memory"] is True
    assert s["ports_wired"] == {k: False for k in s["ports_wired"]}
    assert s["send_cap_today"] == 20 and s["warmup_step"] == 1


def test_intelligences_listed(h):
    reg = h.ok(h.get("/sales/v1/intelligences", "sales_agent"))
    assert [x["number"] for x in reg] == list(range(1, 13))
