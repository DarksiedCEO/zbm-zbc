

def test_health_open(h):
    r = h.client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}      # nothing else unauthenticated (AEGIS L8)
    assert r.headers["cache-control"] == "no-store"


def test_enroll_store_use(hk):
    hk.andre_store("finance_31", "stripe_secret", readers=["finance_31"], purposes=["stripe_api"])
    r = hk.ok(hk.use("finance_31", "vault:finance_31.stripe_secret", "stripe_api"))
    assert r["value"] == "sk_value_123"
