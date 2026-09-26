"""
Contract runner (AEGIS N16-6): Clipper Network's REAL V&I thin client (``src/httpclients.py``
``HttpVerificationIntegrity``) against Verification and Integrity's REAL FastAPI app, in-process: the client's
httpx transport is V&I's Starlette TestClient transport, so every request goes through V&I's auth, caller
identity, body models and routes exactly as over the wire, with V&I's own test fakes behind its ports.

Both services use the same top-level module names (config, ports, service, ...), so V&I is imported first, its
modules are set aside while CN's client is imported, then V&I's are restored (CN's client keeps references to its
own modules). Run by ``tests/test_contract_vi.py`` in a subprocess; prints one JSON line
``{"checks": [...], "methods": [...]}``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CN_ROOT = HERE.parent
VI_ROOT = CN_ROOT.parent / "verification-py"

for k in list(os.environ):
    if k.startswith(("VI_", "CN_")) or k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
        os.environ.pop(k, None)
os.environ["VI_SERVICE_TOKEN"] = "test-vi-service-token-do-not-use-0123"

# --- 1. V&I, with its test harness and fakes
sys.path[:0] = [str(VI_ROOT / "src"), str(VI_ROOT / "tests")]
import helpers as vh  # noqa: E402  (verification-py/tests/helpers.py)

vi_mods = {n: m for n, m in list(sys.modules.items()) if str(VI_ROOT) in (getattr(m, "__file__", None) or "")}
for n in vi_mods:
    del sys.modules[n]
sys.path[:] = [p for p in sys.path if str(VI_ROOT) not in p]

# --- 2. CN's client (and the discipline check that consumes its answers)
sys.path.insert(0, str(CN_ROOT / "src"))
import httpclients as cn_http  # noqa: E402
from intelligences import i08_discipline as cn_i08  # noqa: E402
import ports as cn_ports  # noqa: E402

cn_mods = {n: m for n, m in list(sys.modules.items()) if str(CN_ROOT) in (getattr(m, "__file__", None) or "")}
for n in cn_mods:
    del sys.modules[n]
sys.path[:] = [p for p in sys.path if str(CN_ROOT) not in p]
sys.path[:0] = [str(VI_ROOT / "src"), str(VI_ROOT / "tests")]
sys.modules.update(vi_mods)

CHECKS: list[dict] = []
METHODS: set[str] = set()


def check(name: str, ok: bool, detail=None) -> None:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": repr(detail)[:600]})


class Tracking:
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        fn = getattr(self.inner, name)
        if callable(fn):
            METHODS.add(name)
        return fn


def main() -> None:
    h = vh.Harness()
    h.approve_rules()
    vi = Tracking(cn_http.HttpVerificationIntegrity("http://testserver", vh.SERVICE_TOKEN, vh.CALLERS["clipper_network"],
                                                    transport=h.client._transport))
    rid = vh.rid
    ctx: dict = {}
    sections = []

    def _s2():  # age: check (relay) and subject read, adult / minor / none
        a = vi.age_check(rid("rq"), "cn-clp-1", "1990-01-01", True, "photo_id_match", "sess-1")
        want = h.svc.latest_age.get(("clipper_network", "cn-clp-1"))
        check("age_check adult", a.available and a.status == "adult" and a.attestation_id == want, a)
        s = vi.age_subject("cn-clp-1")
        check("age_subject adult", s.available and s.status == "adult" and s.attestation_id == want, s)
        m = vi.age_check(rid("rq"), "cn-clp-m", "2012-01-01", True, "photo_id_match", "sess-2")
        check("age_check minor", m.available and m.status == "minor", m)
        check("age_subject minor", vi.age_subject("cn-clp-m").status == "minor")
        n = vi.age_subject("cn-clp-none")
        check("age_subject none", n.available and n.status == "unknown" and n.attestation_id is None, n)

    sections.append(('age: check (relay) and subject read, adult / minor / none', _s2))

    def _s3():  # connections: start, complete, list
        st = vi.connection_start(rid("rq"), "cn-clp-1", "tiktok", "https://hub.example/cb")
        check("connection_start", st.available and st.started and st.connection_id, st)
        state = (st.authorization_url or "").split("state=")[1].split("&")[0] if st.started else "none"
        h.adapters["tiktok"].next_account("acct-cn-clp-1")
        cc = vi.connection_complete(rid("rq"), state, "good-code")
        check("connection_complete", cc.available and cc.status == "active" and cc.connection_id == st.connection_id, cc)
        ls = vi.connections("cn-clp-1")
        check("connections", ls.available and [(c.connection_id, c.status) for c in ls.connections]
              == [(st.connection_id, "active")], ls)
        bad = vi.connection_start(rid("rq"), "cn-clp-1", "snapchat", "https://hub.example/cb")
        check("connection_start refused carries reasons", not bad.available or (not bad.started and bad.reasons), bad)
        ctx["st"] = st

    sections.append(('connections: start, complete, list', _s3))

    def _s4():  # identity: clear, then a duplicate e-mail under another clipper id; the finding resolves
        i1 = vi.identity_check(rid("rq"), "cn-clp-1", "one@example.com")
        check("identity clear", i1.available and i1.status == "clear" and i1.finding_ids == (), i1)
        i2 = vi.identity_check(rid("rq"), "cn-clp-2", "one@example.com")
        vi_fids = sorted(f["finding_id"] for f in h.svc.findings.values() if f["clipper_id"] == "cn-clp-2")
        check("identity duplicate", i2.available and i2.status == "duplicate" and sorted(i2.finding_ids) == vi_fids, i2)
        fid = vi_fids[0] if vi_fids else "vi-fnd-" + "0" * 26
        ctx["fid"] = fid
        f = vi.finding(fid)
        check("finding", f.available and f.finding and f.finding.clipper_id == "cn-clp-2" and f.finding.status == "open"
              and f.finding.evidence_ids, f)
        unk = vi.finding("vi-fnd-" + "0" * 26)
        check("finding unknown", unk.available and unk.finding is None, unk)

    sections.append(('identity: clear, then a duplicate e-mail under another clipper id; the finding resolves', _s4))

    def _s5():  # integrity: the duplicate holds cn-clp-2; cn-clp-1 is clear
        g2, g1 = vi.integrity("cn-clp-2"), vi.integrity("cn-clp-1")
        check("integrity held", g2.available and g2.clear is False and g2.reasons, g2)
        check("integrity clear", g1.available and g1.clear is True, g1)

    sections.append(('integrity: the duplicate holds cn-clp-2; cn-clp-1 is clear', _s5))

    def _s6():  # strikes: Andre upholds the duplicate finding at V&I -> S3; CN's discipline accepts its evidence
        fid = ctx.get("fid") or "vi-fnd-" + "0" * 26
        r = h.post(f"/vi/v1/findings/{fid}/decision", {"request_id": rid("fd"), "decision": "uphold", "reason": "same person"},
                   andre=vh.ANDRE_TOKEN)
        check("vi uphold (setup)", r.status_code == 200, r.text[:300])
        feed = vi.strikes(None)
        mine = [x for x in feed.strikes if x.clipper_id == "cn-clp-2"]
        check("strikes feed", feed.available and mine and mine[-1].strike_class == "S3" and mine[-1].evidence_ids, feed)
        if mine:
            answers = {x: vi.finding(x) for x in mine[-1].finding_ids}
            check("discipline accepts the strike evidence", cn_i08.evidence_problem(mine[-1], answers) is None,
                  cn_i08.evidence_problem(mine[-1], answers))
        nxt = vi.strikes(feed.next_cursor) if feed.next_cursor else feed
        check("strikes paging", nxt.available, nxt)

    sections.append(("strikes: Andre upholds the duplicate finding at V&I -> S3; CN's discipline accepts its evidence", _s6))

    def _s7():  # certifications of a clipper
        vh_post_ref = "https://www.tiktok.com/@c/video/sub-c1"
        h.post_video("tiktok", vh_post_ref, views=5000, likes=500)
        reg = h.register("sub-c1", "cn-clp-1", "tiktok", post_ref=vh_post_ref)
        check("vi register (setup)", reg.status_code == 201, reg.text[:300])
        cs = vi.certifications("cn-clp-1")
        check("certifications", cs.available and [c.submission_id for c in cs.certifications] == ["sub-c1"]
              and cs.certifications[0].revision_watch_end is None and cs.certifications[0].status == "pending", cs)

    sections.append(('certifications of a clipper', _s7))

    def _s8():  # revoke
        st = ctx.get("st")
        rv = vi.connection_revoke(rid("rq"), (st and st.connection_id) or "vi-con-" + "0" * 26)
        check("connection_revoke", rv.available and rv.ok, rv)
        after = {c.connection_id: c.status for c in vi.connections("cn-clp-1").connections}
        check("connections after revoke", after.get(st.connection_id) == "revoked" and "active" not in after.values(), after)

    sections.append(('revoke', _s8))

    def _s9():  # ban: never without Andre's token; with it V&I blocks the identity
        nb = vi.ban(rid("rq"), "cn-clp-2", "cn-band-1", "2026-10-01T09:00:00Z", None)
        check("ban without token refused", not nb.ok and "cn-clp-2" not in h.svc.bans, nb)
        wrong = vi.ban(rid("rq"), "cn-clp-2", "cn-band-1", "2026-10-01T09:00:00Z", "not-andres-token-0000000000")
        check("ban with a wrong token refused", not wrong.ok and "cn-clp-2" not in h.svc.bans, wrong)
        ok = vi.ban(rid("rq"), "cn-clp-2", "cn-band-1", "2026-10-01T09:00:00Z", vh.ANDRE_TOKEN)
        check("ban with Andre's token", ok.available and ok.ok and "cn-clp-2" in h.svc.bans, ok)
        i3 = vi.identity_check(rid("rq"), "cn-clp-3", "one@example.com")
        check("banned identity blocked", i3.available and i3.status == "duplicate", i3)

    sections.append(("ban: never without Andre's token; with it V&I blocks the identity", _s9))

    for name, fn in sections:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a drift that raises is a failed check, not a crash
            check(f"section '{name}' raised", False, f"{type(exc).__name__}: {exc}")

    protocol = sorted(n for n in dir(cn_ports.VerificationIntegrityPort) if not n.startswith("_"))
    print(json.dumps({"checks": CHECKS, "methods": sorted(METHODS), "protocol": protocol}))




if __name__ == "__main__":
    main()
