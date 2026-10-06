from helpers import rid


def test_starts_verified_and_healthy(h):
    st = h.ok(h.get("/status"))
    assert st["integrity"]["ok"] is True and st["status"] == "ok"
    assert h.client.get("/health").json() == {"status": "ok"}


def test_full_pursuit_path_with_stand_ins(h):
    p = h.pursuit()
    p = h.bid(p["pursuit_id"])
    assert p["stage"] == "responding"
    r = h.ready_response(p["pursuit_id"])
    s = h.ok(h.submit(r), 201)
    assert s["status"] == "queued"
    out = h.ok(h.job("submission-queue"))
    assert out["not_wired"] == 1
    assert h.ok(h.get(f"/pursuits/{p['pursuit_id']}"))["stage"] == "responding"
    assert h.ok(h.job("integrity"))["ledger_valid"] is True
    assert rid()
