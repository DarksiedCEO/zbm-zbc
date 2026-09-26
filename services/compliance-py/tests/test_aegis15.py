"""
AEGIS round 15 findings on Compliance (38): reproductions per finding
(N15-1 ... N15-8), written to FAIL on the code at 5bb820b and pass after the
fix. The probe each one mirrors is named in its docstring
(review15/p/*.py, review15/ev/*.txt).
"""

from __future__ import annotations

import hashlib
import json
import shutil

import pytest

from clock import iso
from helpers import (ANDRE_TOKEN, NOW, SEED_ROWS, Harness, brand_facts, client_facts, clip_facts, creator_facts, publish_facts,
                     rid, unmet_codes)
from intelligences import i09_disclosure

RECONCILE = "/compliance/v1/reconcile"


def ready(h: Harness | None = None) -> Harness:
    h = h or Harness()
    h.approve_seed()
    h.run_controls()
    h.approve(*[h.memo_supersede(c) for c in ("CQ-01", "CQ-03", "CQ-11")])
    h.run_controls()
    return h


def seed_row(oid: str) -> dict:
    return dict(next(r for r in SEED_ROWS if r["id"] == oid))


def pay(h, **over):
    return h.review("zbc_clip", rid("sub"), clip_facts(**over)).json()


def _durable(tmp_path, name="d"):
    x = Harness(data_dir=str(tmp_path / name))
    ready(x)
    return x


def _restart(x, tmp_path, name="d", **env):
    return Harness(data_dir=str(tmp_path / name), ledger=x.ledger, clock=x.clock, ports=x.ports, env=env or None)


def _lines(path):
    return [ln for ln in path.read_bytes().split(b"\n") if ln]


def _epoch(x):
    return x.svc.log.epoch


def _forge(ledger, event_id, event_type, subject="compliance-log", actor="intel_11_evidence_audit"):
    """A ledger-token holder posts an event by hand (no Compliance token, no local log access)."""
    ledger.record_event(event_id, "compliance", event_type, actor, subject, {"forged": event_id}, "forged by hand")


def _reconcile_plan(y):
    r = y.client.get(RECONCILE, headers=y.headers(andre=ANDRE_TOKEN))
    assert r.status_code == 200, r.text
    return r.json()


def _reconcile(y, plan=None):
    plan = plan or _reconcile_plan(y)
    return y.post(RECONCILE, {"request_id": rid("rec"), "head_sha256": plan["head_sha256"],
                              "void_lines": plan["voidable"]["lines"], "void_event_ids": plan["voidable"]["event_ids"]},
                  andre=ANDRE_TOKEN)


# ---------------------------------------------------------------- N15-1 anchors are never withdrawn by a ledger event

def test_n15_1_failed_commit_is_not_withdrawn_by_the_services_own_marker(tmp_path):
    """pk2_anchor K4b / i11_evidence_audit.py:77-84: the local_commit_failed marker withdrew the anchor."""
    x = _durable(tmp_path)
    x.svc.log.fail_next_append = True
    assert x.rule("client-z", "client", client_facts()).status_code == 503
    with pytest.raises(RuntimeError, match="refusing to start"):
        _restart(x, tmp_path)


def test_n15_1_forged_markers_do_not_hide_a_truncation(tmp_path):
    """pk_anchor K3: truncate the tail and post local_commit_failed markers for it with the ledger token only."""
    x = _durable(tmp_path)
    logf = tmp_path / "d" / "compliance_log.jsonl"
    n = len(_lines(logf))
    x.ports.sanctions.result = "potential_match"
    x.screen("clipper-1")                                  # a screen + a hold: the lines an attacker wants gone
    lines = _lines(logf)
    ep = _epoch(x)
    for seq, ln in enumerate(lines[n:], start=n + 1):
        sha = hashlib.sha256(ln).hexdigest()
        _forge(x.ledger, f"cmp-lcf-{ep}-{seq}-{sha[:40]}", "local_commit_failed")
    logf.write_bytes(b"\n".join(lines[:n]) + b"\n")
    with pytest.raises(RuntimeError, match="refusing to start"):
        _restart(x, tmp_path)


def test_n15_1_forged_markers_do_not_hide_a_register_rollback(tmp_path):
    """pk_anchor K3 (register v2 rolled back to v1 with forged markers carrying register:v2)."""
    x = _durable(tmp_path)
    logf = tmp_path / "d" / "compliance_log.jsonl"
    n = len(_lines(logf))
    v = x.get("/health").json()["register_version_in_force"]
    x.approve(x.memo_supersede("CQ-02"))
    lines = _lines(logf)
    ep = _epoch(x)
    for seq, ln in enumerate(lines[n:], start=n + 1):
        sha = hashlib.sha256(ln).hexdigest()
        _forge(x.ledger, f"cmp-lcf-{ep}-{seq}-{sha[:40]}", "local_commit_failed", subject=f"register:v{v + 1}")
    logf.write_bytes(b"\n".join(lines[:n]) + b"\n")
    with pytest.raises(RuntimeError, match="refusing to start"):
        _restart(x, tmp_path)
    # AEGIS N16-7 (changed): the rolled-back version event is VOIDABLE, never honoured by forged markers; reconcile
    # mode starts and lists it, and only Andre's recorded reconcile can void it (a deliberate rollback)
    y = _restart(x, tmp_path, COMPLIANCE_RECONCILE_MODE="1")
    plan = _reconcile_plan(y)
    assert any(e.startswith("cmp-ver-") for e in plan["voidable"]["event_ids"]) and not plan["fatal"]
    assert not any(e.startswith("cmp-lcf-") for e in plan["voidable"]["event_ids"])


def test_n15_1_operator_reconcile_after_a_failed_commit(tmp_path):
    """The only way to void a failed commit's anchor: Andre's POST /compliance/v1/reconcile (recorded, bound)."""
    x = _durable(tmp_path)
    x.svc.log.fail_next_append = True
    assert x.rule("client-z", "client", client_facts()).status_code == 503
    y = _restart(x, tmp_path, COMPLIANCE_RECONCILE_MODE="1")
    assert y.get("/health").json()["reconcile_required"] is True
    # nothing else runs while a reconcile is pending
    assert y.rule("client-q", "client", client_facts()).status_code == 503
    plan = _reconcile_plan(y)
    assert plan["voidable"]["lines"] and not plan["fatal"]
    # the service token alone (no Andre token) cannot reconcile
    r = y.post(RECONCILE, {"request_id": rid("rec"), "head_sha256": plan["head_sha256"],
                           "void_lines": plan["voidable"]["lines"], "void_event_ids": plan["voidable"]["event_ids"]})
    assert r.status_code == 403
    # a void list that is not exactly what the ledger shows is refused
    bad = y.post(RECONCILE, {"request_id": rid("rec"), "head_sha256": plan["head_sha256"], "void_lines": [],
                             "void_event_ids": plan["voidable"]["event_ids"]}, andre=ANDRE_TOKEN)
    assert bad.status_code == 409
    r = _reconcile(y, plan)
    assert r.status_code == 200, r.text
    assert x.ledger.of_type("reconcile")
    z = _restart(x, tmp_path)
    assert z.get("/health").json()["reconcile_required"] is False
    c11 = z.run_controls()["results"]["C-11"]
    assert c11["result"] == "pass", c11


def test_n15_1_reconcile_honoured_only_when_the_ledger_event_matches(tmp_path):
    x = _durable(tmp_path)
    x.svc.log.fail_next_append = True
    assert x.rule("client-z", "client", client_facts()).status_code == 503
    y = _restart(x, tmp_path, COMPLIANCE_RECONCILE_MODE="1")
    assert _reconcile(y).status_code == 200
    ev = x.ledger.of_type("reconcile")[0]
    ev["payload_sha256"] = "0" * 64                        # the ledger's reconcile no longer matches the local record
    with pytest.raises(RuntimeError, match="refusing to start"):
        _restart(x, tmp_path)
    x.ledger.events.remove(ev)                            # no reconcile event on the ledger at all
    with pytest.raises(RuntimeError, match="refusing to start"):
        _restart(x, tmp_path)


def test_n15_1_c11_red_while_a_failed_commit_is_unreconciled(tmp_path):
    x = _durable(tmp_path)
    assert x.run_controls()["results"]["C-11"]["result"] == "pass"
    x.svc.log.fail_next_append = True
    assert x.rule("client-z", "client", client_facts()).status_code == 503
    c11 = x.run_controls()["results"]["C-11"]
    assert c11["result"] == "fail", c11
    assert _reconcile(x).status_code == 200               # a running service can be reconciled too
    assert x.run_controls()["results"]["C-11"]["result"] == "pass"


def test_n15_1_forged_anchor_beyond_head_needs_andre_reconcile(tmp_path):
    """pk2_anchor K4/K4b: a forged anchor is a DoS until Andre reconciles; a forged marker does not lift it."""
    x = _durable(tmp_path)
    n = len(x.svc.log)
    ep = _epoch(x)
    _forge(x.ledger, f"cmp-log-{ep}-{n + 1}-{'e' * 40}", "local_log_appended")
    with pytest.raises(RuntimeError):
        _restart(x, tmp_path)
    _forge(x.ledger, f"cmp-lcf-{ep}-{n + 1}-{'e' * 40}", "local_commit_failed")
    with pytest.raises(RuntimeError):
        _restart(x, tmp_path)
    y = _restart(x, tmp_path, COMPLIANCE_RECONCILE_MODE="1")
    assert _reconcile(y).status_code == 200
    _restart(x, tmp_path)


def test_n15_1_decision_whose_version_reached_the_ledger_is_restored_from_the_unwritten_line(tmp_path):
    """A published version whose decision line never reached the log is not honoured (N16-7: voidable only by
    Andre); the exact unwritten line is the recovery that keeps it."""
    x = _durable(tmp_path)
    v = x.get("/health").json()["register_version_in_force"]
    p = x.memo_supersede("CQ-02")
    x.svc.log.fail_next_append = True
    r = x.decide([{"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve"}])
    assert r.status_code == 503
    side = list((tmp_path / "d").glob("compliance_log.jsonl.unwritten-*"))
    assert len(side) == 1
    with pytest.raises(RuntimeError, match="match no decision in the local log"):
        _restart(x, tmp_path)
    logf = tmp_path / "d" / "compliance_log.jsonl"
    logf.write_bytes(logf.read_bytes() + side[0].read_bytes())
    y = _restart(x, tmp_path)
    assert y.get("/health").json()["register_version_in_force"] == v + 1


def test_n15_1_ledger_failure_on_the_version_event_leaves_only_a_voidable_anchor(tmp_path):
    x = _durable(tmp_path)
    v = x.get("/health").json()["register_version_in_force"]
    p = x.memo_supersede("CQ-02")
    x.ledger.fail_on_type = "register_version_published"
    r = x.decide([{"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve"}])
    assert r.status_code == 503
    x.ledger.fail_on_type = None
    y = _restart(x, tmp_path, COMPLIANCE_RECONCILE_MODE="1")      # not a rollback: reconcilable
    assert _reconcile(y).status_code == 200
    z = _restart(x, tmp_path)
    assert z.get("/health").json()["register_version_in_force"] == v


# ---------------------------------------------------------------- N15-2 a copied data dir is a second instance

def test_n15_2_copied_data_dir_turns_c11_red_on_the_original(tmp_path):
    """pk2_anchor K5b: C from a copy of A's data dir; both issue rulings; A's C-11 stayed green."""
    x = _durable(tmp_path, "a")
    shutil.copytree(tmp_path / "a", tmp_path / "c")
    c = Harness(data_dir=str(tmp_path / "c"), ledger=x.ledger, clock=x.clock, ports=x.ports)
    assert c.rule("cl-c", "client", client_facts()).status_code == 200
    assert x.rule("cl-a", "client", client_facts()).status_code == 200
    c11 = x.run_controls()["results"]["C-11"]
    assert c11["result"] == "fail", c11
    assert "lease" in c11["detail"] or "ruling" in c11["detail"]


def test_n15_2_newer_lease_from_another_instance_refuses_start(tmp_path):
    """K5c: after a copy took over the lineage, the original (or any older copy) refuses to start."""
    x = _durable(tmp_path, "a")
    shutil.copytree(tmp_path / "a", tmp_path / "c")
    shutil.copytree(tmp_path / "a", tmp_path / "e")
    Harness(data_dir=str(tmp_path / "c"), ledger=x.ledger, clock=x.clock, ports=x.ports)   # C writes its lease
    with pytest.raises(RuntimeError, match="lease"):
        Harness(data_dir=str(tmp_path / "e"), ledger=x.ledger, clock=x.clock, ports=x.ports)
    with pytest.raises(RuntimeError, match="refusing to start"):
        Harness(data_dir=str(tmp_path / "a"), ledger=x.ledger, clock=x.clock, ports=x.ports)


def test_n15_2_restart_of_the_same_lineage_still_starts(tmp_path):
    x = _durable(tmp_path)
    y = _restart(x, tmp_path)
    z = _restart(y, tmp_path)
    assert z.run_controls()["results"]["C-11"]["result"] == "pass"
    assert len(x.ledger.of_type("instance_lease")) == 3


def test_n15_2_ruling_on_the_ledger_not_in_the_local_log_turns_c11_red(tmp_path):
    x = _durable(tmp_path)
    assert x.run_controls()["results"]["C-11"]["result"] == "pass"
    _forge(x.ledger, "cmp-rul-" + "a" * 26, "activation_ruling", subject="cl-ghost", actor="intel_02_activation_gate")
    c11 = x.run_controls()["results"]["C-11"]
    assert c11["result"] == "fail" and "ruling" in c11["detail"], c11


# ---------------------------------------------------------------- N15-3 weakening recomputed at approval time

def _amend(h, tid, mut, caller="legal_37"):
    row = dict(next(r for r in h.svc.current.rows if r["id"] == tid))
    new = {**row, **mut, "expires_at": None}
    ev = {"source_url": row["source_url"], "fetched_at": iso(NOW), "snapshot_sha256": "a" * 64,
          "normalized_text_sha256": "b" * 64, "quoted_excerpt": "x", "doc_number": None}
    if new["status"] == "verified":
        new["verified_at"] = NOW.date().isoformat()
    r = h.propose({"kind": "amend", "target_id": tid, "proposed_row": new, "evidence": ev}, caller=caller)
    assert r.status_code == 201, r.text
    return r.json()["proposal"]


def test_n15_3_stale_base_harmless_amend_cannot_silently_revert_a_strengthening():
    """pn_r15 R5 stale-base (i01_register.py:361-366)."""
    h = ready()
    tid = "US-FTC-D101-02"
    row = next(r for r in h.svc.current.rows if r["id"] == tid)
    harmless = _amend(h, tid, {"title": row["title"] + " (typo fixed)"})
    strong = _amend(h, tid, {"gates": sorted(set(row["gates"]) | {"publish", "activation"})})
    assert harmless["weakening"] is False and strong["weakening"] is False
    h.approve(strong)
    gates = next(r for r in h.svc.current.rows if r["id"] == tid)["gates"]
    d = h.decide([{"proposal_id": harmless["proposal_id"], "content_sha256": harmless["content_sha256"],
                   "decision": "approve"}])
    assert d.status_code == 422, d.text
    body = d.json()
    assert "gates_removed" in body["detail"] and body["recheck"][0]["diff"]["gates"]["old"] == gates
    assert next(r for r in h.svc.current.rows if r["id"] == tid)["gates"] == gates
    # with the explicit acknowledgment Andre may still approve it (he saw the diff)
    d2 = h.decide([{"proposal_id": harmless["proposal_id"], "content_sha256": harmless["content_sha256"],
                    "decision": "approve", "acknowledge_weakening": True}])
    assert d2.status_code == 200, d2.text


def test_n15_3_base_changed_in_a_touched_field_is_409_stale():
    h = ready()
    tid = "US-FTC-D101-02"
    row = next(r for r in h.svc.current.rows if r["id"] == tid)
    a = _amend(h, tid, {"title": row["title"] + " A"})
    b = _amend(h, tid, {"title": row["title"] + " B"})
    h.approve(a)
    d = h.decide([{"proposal_id": b["proposal_id"], "content_sha256": b["content_sha256"], "decision": "approve",
                   "acknowledge_weakening": True}])
    assert d.status_code == 409, d.text


def test_n15_3_sweep_control_proposal_cannot_silently_revert_a_later_change():
    h = ready()
    c0 = dict(h.svc.control_defs["C-01"])
    title_only = h.propose({"kind": "control", "target_id": "C-01", "proposed_row": {**c0, "title": c0["title"] + " x"},
                            "evidence": None}).json()["proposal"]
    stricter = h.propose({"kind": "control", "target_id": "C-01", "proposed_row": {**c0, "sla_hours": 12},
                          "evidence": None}).json()["proposal"]
    h.approve(stricter)
    d = h.decide([{"proposal_id": title_only["proposal_id"], "content_sha256": title_only["content_sha256"],
                   "decision": "approve"}])
    assert d.status_code == 422 and "control_sla_longer" in d.json()["detail"], d.text
    assert h.svc.control_defs["C-01"]["sla_hours"] == 12


# ---------------------------------------------------------------- N15-4 label negation: sentence-level rule

R4_ALLOWED = ["Sponsored, not affiliated with YouTube", "#fun #summer #travel #beach #ad", "Advertisement",
              "ADVERTISEMENT: paid by Acme", "Not affiliated. #ad", "No purchase necessary. Sponsored by Acme"]
R4_BLOCKED = ["This is not in any way sponsored", "not at all an ad", "Sponsored? Not really.", "#ad (just kidding)",
              "Is this an ad? No.", "n0t an ad", "not really really really sponsored", "#notanad", "sponsored free",
              "#ad-free", "not　sponsored", "not sponsored", "not sponsored", "nicht Werbung #ad", "(#ad)",
              "#ad jk", "#ad lol", "Sponsored?", "ad, not", "#ad, not really"]


@pytest.mark.parametrize("text", R4_ALLOWED)
def test_n15_4_r4_real_disclosures_pass(text):
    h = ready()
    h.activate_creator()
    h.activate_brand()
    j = pay(h, disclosure={"in_video_label_text": text})
    assert j["allowed"] is True, (text, j["unmet"])


@pytest.mark.parametrize("text", R4_BLOCKED)
def test_n15_4_r4_negated_questioned_or_retracted_labels_fail(text):
    h = ready()
    h.activate_creator()
    h.activate_brand()
    j = pay(h, disclosure={"in_video_label_text": text})
    assert j["allowed"] is False and ("US-FTC-D101-02", "label_vocabulary") in unmet_codes(j), text


def test_n15_4_the_response_says_why():
    h = ready()
    h.activate_creator()
    h.activate_brand()
    j = pay(h, disclosure={"in_video_label_text": "#ad (just kidding)"})
    line = next(u for u in j["unmet"] if u["code"] == "label_vocabulary")["message"]
    assert "retract" in line or "kidding" in line, line
    j = pay(h, disclosure={"in_video_label_text": "Is this an ad? No."})
    line = next(u for u in j["unmet"] if u["code"] == "label_vocabulary")["message"]
    assert "question" in line, line


HR06 = seed_row("HR-06")["parameters"]["local_labels"]
R4B = [("DE", "#ad Werbung", True), ("DE", "#ad #Werbung", True), ("DE", "#ad Anzeige", True),
       ("DE", "#ad #anzeige", True), ("DE", "#ad keine Werbung", False), ("DE", "#ad Werbung? Nein.", False),
       ("DE", "#ad Werbefrei", False), ("IT", "#ad Pubblicità", True), ("IT", "#ad #ADV", True),
       ("IT", "#ad #pubblicità", True), ("IT", "#ad senza pubblicità", False), ("IT", "#ad Sponsorizzato", True),
       ("NL", "#ad Reclame", True), ("NL", "#ad geen reclame", False), ("NL", "#ad Advertentie", True),
       ("ES", "#ad Publicidad", True), ("ES", "#ad #publicidad", True), ("ES", "#ad sin publicidad", False),
       ("ES", "#ad #publi", False)]   # '#publi' is not in the seed's ES list (HR-06): only Andre can add it


@pytest.mark.parametrize("cc,text,want", R4B)
def test_n15_4_r4b_local_labels_both_directions(cc, text, want):
    """R4b: the local label rule is the same rule (the DE/NL/ES rows are unverified in the seed, so the whole
    gate stays blocked there; the label decision itself is asserted here)."""
    assert i09_disclosure.local_label_present(text, HR06[cc]) is want, (cc, text)


def test_n15_4_r4b_italian_hashtag_label_passes_the_gate():
    h = ready()
    h.activate_creator()
    kit = seed_row("HR-06")["parameters"]["eu_kit_version"]
    h.activate_brand("camp-it", targets=("US", "IT"), eu_kit_version_acknowledged=kit, msa_eu_clause=True)
    j = pay(h, campaign="camp-it", disclosure={"in_video_label_text": "#ad #ADV"})
    assert ("IT-AGCOM", "local_label") not in unmet_codes(j), j["unmet"]
    j = pay(h, campaign="camp-it", disclosure={"in_video_label_text": "#ad (#ADV)"})
    assert ("IT-AGCOM", "local_label") in unmet_codes(j)


# ---------------------------------------------------------------- N15-5 empty targets / platforms

@pytest.mark.parametrize("targets,platforms", [((), ()), ((), ("web",)), (("US",), ())])
def test_n15_5_publish_with_empty_targets_or_platforms_is_422(targets, platforms):
    """pn_r15 R2 (i04_publish_gate.py:91-93)."""
    h = ready()
    h.activate_client(platforms=("web",))
    r = h.review("zbm_work", rid("w"), publish_facts(targets=targets, platforms=platforms))
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("lane", ["client", "zbc_brand"])
@pytest.mark.parametrize("key", ["target_jurisdictions", "platforms"])
def test_n15_5_activation_with_empty_targets_or_platforms_is_422(lane, key):
    h = ready()
    f = client_facts() if lane == "client" else brand_facts()
    f[key] = []
    assert h.rule("s-1", lane, f).status_code == 422


def test_n15_5_creator_with_no_accounts_is_422():
    h = ready()
    s = h.screen("clipper-9")
    assert h.rule("clipper-9", "zbc_creator", creator_facts(s["screen_id"], accounts=[])).status_code == 422


def test_n15_5_sweep_engine_never_passes_an_empty_platform_or_jurisdiction_set():
    """Defence in depth behind the 422: a context with no platforms or no jurisdictions blocks."""
    from intelligences.engine import Ctx, evaluate
    for plats, juris in ((set(), {"US"}), ({"web"}, set())):
        ctx = Ctx(gate="publish", subject_id="w", subject_kind="zbm_work", asset_type="site", rows={},
                  platforms=plats, jurisdictions=juris, flags={}, now=NOW)
        codes = {(u["obligation_id"], u["code"]) for u in evaluate(ctx)}
        assert any(c[0] == "HR-03" and c[1].startswith("fact_missing:") for c in codes), (plats, juris)


def test_n15_5_sweep_control_wildcard_matching_no_row_is_red():
    from controls import control_status
    d = {"control_id": "C-99", "title": "t", "owner_department": "compliance", "owner_intelligence": None, "test": "t",
         "evidence": "e", "sla_hours": 24, "blocks_gates": [], "obligation_ids": ["ZZ-NOPE-*"]}
    st = control_status(d, {"last_result": "pass", "last_passed_at": iso(NOW)}, {}, NOW)
    assert st["status"] == "red"


# ---------------------------------------------------------------- N15-6 payout platform vs the clipper's accounts

def test_n15_6_payout_platform_must_be_a_declared_clipper_account():
    """pn_r15 R2f (i03_payout_gate.py)."""
    h = ready()
    h.activate_creator(accounts=[{"platform": "tiktok", "handle_sha256": "c" * 64}])
    h.activate_brand()
    j = pay(h, platform="youtube")
    assert j["allowed"] is False
    assert ("HR-03", "fact_missing:clipper_account_scope") in unmet_codes(j)


# ---------------------------------------------------------------- N15-7 more weakening classes

def _unverified_row(h):
    return next(r for r in h.svc.current.rows if r["status"] == "unverified" and r["source_kind"] != "counsel-question"
                and not r["counsel_flag"] and r["source_url"] and "publish" in r["gates"]
                and r["source_kind"] in ("statute", "reg", "guidance", "platform-policy"))


def test_n15_7_amend_unverified_to_verified_is_refused():
    """pk_anchor R5x: an amend turned a blocking unverified rule into a verified one, unflagged."""
    h = ready()
    row = _unverified_row(h)
    new = {**row, "status": "verified", "source_quality": "primary", "verified_at": NOW.date().isoformat(),
           "expires_at": None}
    ev = {"source_url": row["source_url"], "fetched_at": iso(NOW), "snapshot_sha256": "a" * 64,
          "normalized_text_sha256": "b" * 64, "quoted_excerpt": "x", "doc_number": None}
    r = h.propose({"kind": "amend", "target_id": row["id"], "proposed_row": new, "evidence": ev}, caller="legal_37")
    assert r.status_code == 422, r.text


def test_n15_7_reverify_unverified_to_verified_is_flagged_and_bound_to_the_rows_source():
    h = ready()
    row = _unverified_row(h)
    new = {**row, "status": "verified", "source_quality": "primary", "verified_at": NOW.date().isoformat(),
           "expires_at": None}
    ev = {"source_url": row["source_url"], "fetched_at": iso(NOW), "snapshot_sha256": "a" * 64,
          "normalized_text_sha256": "b" * 64, "quoted_excerpt": "x", "doc_number": None}
    r = h.propose({"kind": "reverify", "target_id": row["id"], "proposed_row": new, "evidence": ev}, caller="legal_37")
    assert r.status_code == 201, r.text
    p = r.json()["proposal"]
    assert p["weakening"] is True and "unverified_to_verified" in p["weakening_reasons"]
    other = "https://example.gov/elsewhere"
    r = h.propose({"kind": "reverify", "target_id": row["id"], "proposed_row": {**new, "source_url": other},
                   "evidence": {**ev, "source_url": other}}, caller="legal_37")
    assert r.status_code == 422, r.text


def test_n15_7_counsel_memo_path_still_works():
    h = ready()
    p = h.memo_supersede("CQ-04")
    assert h.decide([{"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"],
                      "decision": "approve", "acknowledge_weakening": True}]).status_code == 200


@pytest.mark.parametrize("mut,reason", [({"owner_intelligence": "i05_control_monitor"}, "control_owner_intelligence_changed"),
                                        ({"evidence": "none required"}, "control_evidence_changed")])
def test_n15_7_control_owner_intelligence_or_evidence_change_is_weakening(mut, reason):
    h = ready()
    c0 = dict(h.svc.control_defs["C-01"])
    p = h.propose({"kind": "control", "target_id": "C-01", "proposed_row": {**c0, **mut}, "evidence": None}).json()["proposal"]
    assert p["weakening"] is True and reason in p["weakening_reasons"], p


# ---------------------------------------------------------------- N15-8 rulings bind request_id and facts

def test_n15_8_rulings_carry_request_id_facts_sha256_and_seed_pinned():
    h = ready()
    f = client_facts()
    r = h.rule("client-1", "client", f, request_id="rq-158").json()
    want = hashlib.sha256(json.dumps(f, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert r["request_id"] == "rq-158" and r["facts_sha256"] == want and r["seed_pinned"] is True
    r2 = h.get(f"/compliance/v1/rulings/{r['ruling_id']}").json()
    assert r2["request_id"] == "rq-158" and r2["facts_sha256"] == want
    p = h.review("zbc_clip", "sub-158", clip_facts(), request_id="rq-158b").json()
    assert p["request_id"] == "rq-158b" and p["facts_sha256"] == hashlib.sha256(
        json.dumps(clip_facts(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_n15_8_facts_sha256_formula_is_stable_for_non_ascii_and_numbers():
    h = ready()
    h.activate_creator()
    h.activate_brand()
    f = clip_facts(disclosure={"in_video_label_text": "#ad Pubblicità ＃ＡＤ", "in_video_label_start_s": 1.5})
    p = h.review("zbc_clip", "sub-na", f).json()
    assert p["facts_sha256"] == hashlib.sha256(json.dumps(f, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

