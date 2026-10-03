"""Spec §G guardrails G1-G8."""

from __future__ import annotations

import hashlib
import inspect
import itertools
import random
import re
import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel

import config as config_mod
import models
import ports as P
import reasons as R
from advice import default_guard
from builders import executed_msa
from helpers import Harness, rid

SRC = Path(__file__).resolve().parents[1] / "src"
SEED = Path(__file__).resolve().parents[1] / "seed"


# --- G1 ----------------------------------------------------------------------------------------------------------

def test_g1_no_llm_sdk_and_no_network_in_tests():
    banned = re.compile(r"^\s*(import|from)\s+(openai|anthropic|langchain|transformers|llama|cohere|google\.generativeai|"
                        r"vertexai|mistralai|litellm|torch|tensorflow|requests)\b", re.M)
    for p in SRC.rglob("*.py"):
        assert not banned.search(p.read_text()), p.name
    # the socket guard is active (conftest)
    import socket
    with pytest.raises(RuntimeError, match="network access"):
        socket.create_connection(("127.0.0.1", 9))


# --- G2 ----------------------------------------------------------------------------------------------------------

def test_g2_every_stand_in_answers_unavailable_and_no_fake_in_src():
    ports = P.Ports()
    assert ports.compliance.create_proposal("r", {}).status == "unavailable"
    assert ports.compliance.row("CQ-01").available is False
    assert ports.esign.create_envelope("d", "1.0", "a" * 64, ["s"]).available is False
    assert ports.esign.certificate("e") is None and ports.esign.status("e") is None
    assert ports.counsel.deliver({}).delivered is False
    assert ports.cybersecurity_22.freeze("h", ["email"], []).delivered is False
    for n in ("people_43", "clipper_network", "creative_production", "finance_31", "onboarding",
              "verification_integrity", "push"):
        assert getattr(ports, n).notify("k", "s", {}).delivered is False
    for p in SRC.rglob("*.py"):
        assert not re.search(r"^class Fake", p.read_text(), re.M), p.name
    assert not (SRC / "fakes.py").exists()


def test_g2_production_wiring_is_all_stand_ins(h):
    import api
    ports = api.build_ports(h.settings)
    assert all(type(getattr(ports, f)).__name__.startswith(("NotWired", "NotBuilt"))
               for f in ("compliance", "esign", "counsel", "cybersecurity_22", "people_43", "push"))
    assert h.ok(h.client.get("/health"))["counsel_channel_wired"] is False


@pytest.mark.parametrize("var,val", [("LEGAL_COUNSEL_CHANNEL", "email"), ("LEGAL_ESIGN_PROVIDER", "docusign"),
                                     ("LEGAL_PORTAL_FAQ", "1"), ("LEGAL_AGENT_MAX_FALLBACK", "2"),
                                     ("LEGAL_MEMO_REVIEW_DAYS", "365"), ("LEGAL_DISPUTE_THRESHOLD", "5000"),
                                     ("LEGAL_COMPLIANCE_URL", "http://127.0.0.1:1"), ("LEGAL_BUSINESS_TZ", "Mars/Olympus"),
                                     ("LEGAL_SERVICE_TOKEN", "")])
def test_g2_unbuilt_or_unsafe_configuration_refuses_to_start(var, val):
    from helpers import base_env
    env = base_env(**{var: val}) if val else {k: v for k, v in base_env().items() if k != var}
    with pytest.raises(RuntimeError):
        config_mod.load(env)


# --- G3 ----------------------------------------------------------------------------------------------------------

def test_g3_every_negative_answer_cites_a_rule_in_the_version_in_force(he):
    in_force = set(he.svc.current.by_id())
    assert set(R.CATALOG.values()) <= in_force
    seen = []
    orig = he.client.request

    def spy(*a, **k):
        r = orig(*a, **k)
        try:
            seen.append(r.json())
        except ValueError:
            pass
        return r
    he.client.request = spy
    executed_msa(he, verify_cq19=False)
    he.post("/legal/v1/music/rulings", {"request_id": rid(), "subject_kind": "zbc_clip", "subject_id": "c",
                                        "platform": "youtube", "paid": True, "music": {"present": True,
                                        "source": "licensed", "track_or_license_id": None},
                                        "reposted_or_reedited_by_zbc": True, "music_changed_since_approval": True},
            caller="creative_production")
    he.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "x", "kind": "question"},
            caller="hub")
    he.apost("/legal/v1/memos", {"request_id": rid(), "counsel_ref": "eng-counsel-1", "memo_date": "2026-10-01",
                                 "content_b64": "bQ==", "cites": {"cq_ids": ["CQ-21"]},
                                 "answers": [{"cq_id": "CQ-19", "resolution": "verified_rule"}]})
    he.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "x", "kind": "demand_letter",
                                   "facts": {"class_action_threat": "unknown"}}, caller="hub")
    he.post("/legal/v1/signoffs", {"request_id": rid(), "topic": "agpl_code", "subject_id": "s", "facts": {}},
            caller="creative_production")
    items = []

    def walk(o):
        if isinstance(o, dict):
            if {"code", "rule_id", "message"} <= set(o):
                items.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(seen)
    assert len(items) >= 8
    for it in items:
        assert it["rule_id"] in in_force and R.CATALOG[it["code"]] == it["rule_id"]
        assert set(it) == {"code", "rule_id", "cq_id", "message", "evidence_ids"} and len(it["message"]) <= 200


def test_g3_no_rule_the_catalog_cites_can_be_retired_and_founder_rules_never_weaken(hr):
    r = hr.apost("/legal/v1/rules/proposals", {"request_id": rid(), "kind": "retire", "target_id": "LG-12"})
    assert r.status_code == 422
    row = dict(hr.svc.current.by_id()["LG-01"], statement="Relaxed.")
    r = hr.apost("/legal/v1/rules/proposals", {"request_id": rid(), "kind": "amend", "target_id": "LG-01",
                                               "proposed_row": row})
    assert r.status_code == 422 and "founder-locked" in r.text
    lg10 = dict(hr.svc.current.by_id()["LG-10"])
    lg10["parameters"] = {**lg10["parameters"], "restore_min_business_days": 5}
    r = hr.apost("/legal/v1/rules/proposals", {"request_id": rid(), "kind": "amend", "target_id": "LG-10",
                                               "proposed_row": lg10})
    assert r.status_code == 422 and "10..14" in r.text


def test_g3_a_rule_parameter_change_is_flagged_weakening_and_takes_effect_only_on_approval(he):
    lg12 = dict(he.svc.current.by_id()["LG-12"])
    lg12["parameters"] = {"verified_platform_libraries": ["tiktok", "youtube"]}
    p = he.ok(he.apost("/legal/v1/rules/proposals", {"request_id": rid(), "kind": "amend", "target_id": "LG-12",
                                                     "proposed_row": lg12}), 201)["proposal"]
    assert p["weakening"] and "parameters_changed" in p["weakening_reasons"]
    r = he.apost("/legal/v1/rules/decisions", {"request_id": rid(), "decisions": [
        {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve"}]})
    assert r.status_code == 422
    he.ok(he.apost("/legal/v1/rules/decisions", {"request_id": rid(), "decisions": [
        {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve",
         "acknowledge_weakening": True}]}))
    assert he.svc.rules_version == 2 and he.svc._param("LG-12", "verified_platform_libraries", []) == ["tiktok", "youtube"]


# --- G4 ----------------------------------------------------------------------------------------------------------

def test_g4_every_seed_file_is_pinned():
    for name, pin in config_mod.PINNED_SEEDS.items():
        assert hashlib.sha256((SEED / name).read_bytes()).hexdigest() == pin, name
    assert set(config_mod.PINNED_SEEDS) == {p.name for p in SEED.glob("*.json")}


@pytest.mark.parametrize("name", sorted(config_mod.PINNED_SEEDS))
def test_g4_a_changed_seed_refuses_start(tmp_path, name):
    d = tmp_path / "seed"
    shutil.copytree(SEED, d)
    (d / name).write_bytes((d / name).read_bytes().replace(b'"version": 1', b'"version": 2', 1))
    with pytest.raises(RuntimeError, match="does not match"):
        Harness(env={"LEGAL_SEED_DIR": str(d)})


def test_g4_unpinned_rules_seed_runs_only_non_production_and_says_so(tmp_path):
    d = tmp_path / "seed"
    shutil.copytree(SEED, d)
    orig = (d / "legal_rules_seed.json").read_bytes()
    # wave 25 (scout B Low): a no-op `.replace(b"LG-19", b"LG-19", 1)` stood in front of this one tamper
    raw = orig.replace(b'"source": "LEGAL_SPEC.md rev 1', b'"source": "LEGAL_SPEC.md rev 1 (test copy)', 1)
    assert raw != orig
    (d / "legal_rules_seed.json").write_bytes(raw)
    h = hashlib.sha256(raw).hexdigest()
    with pytest.raises(RuntimeError):
        Harness(env={"LEGAL_SEED_DIR": str(d), "LEGAL_SEED_SHA256": h})
    x = Harness(env={"LEGAL_SEED_DIR": str(d), "LEGAL_SEED_SHA256": h, "LEGAL_ALLOW_UNPINNED_SEED": "1"})
    assert x.ok(x.client.get("/health"))["rules_pinned"] is False
    assert x.ok(x.get("/legal/v1/documents/sow/current"))["rules_pinned"] is False


# --- G5 ----------------------------------------------------------------------------------------------------------

def _durable(tmp_path):
    x = Harness(data_dir=str(tmp_path / "d"))
    x.approve_rules()
    x.engage()
    x.approve_doc("clipper_agreement", "clipper agreement text")
    return x


def test_g5_log_tamper_refuses_start(tmp_path):
    x = _durable(tmp_path)
    p = tmp_path / "d" / "legal_log.jsonl"
    lines = p.read_bytes().splitlines()
    lines[2] = lines[2].replace(b'"anchored":true', b'"anchored":true ', 1)
    p.write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(Exception, match="chain|canonical|hash"):
        Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock)


def test_g5_blob_tamper_or_loss_refuses_start(tmp_path):
    x = _durable(tmp_path)
    blobs = sorted((tmp_path / "d" / "blobs").iterdir())
    blob = blobs[0]
    orig = blob.read_bytes()
    blob.write_bytes(orig + b"!")
    with pytest.raises(Exception, match="does not match its SHA-256"):
        Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock)
    blob.unlink()
    with pytest.raises(RuntimeError, match="missing from the blob store"):
        Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock)
    blob.write_bytes(orig)
    Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock)


# --- G6 ----------------------------------------------------------------------------------------------------------

FORBIDDEN_FIELD = re.compile(r"(^|_)(ip|ip_address|user_agent|device\w*|dob|date_of_birth|ssn|tax_id|government_id|"
                             r"passport|card_number|account_number|routing_number|iban|cvv)$")


def _all_models():
    return [c for _, c in inspect.getmembers(models, inspect.isclass) if issubclass(c, BaseModel) and c is not BaseModel]


def test_g6_no_model_has_an_ip_dob_government_id_or_payment_field():
    for cls in _all_models():
        for name in cls.model_fields:
            assert not FORBIDDEN_FIELD.search(name), (cls.__name__, name)


def test_g6_byte_scan_of_everything_legal_wrote(he):
    v = he.approve_doc("clipper_agreement", "clipper agreement text")
    he.verify_cq("CQ-19")
    he.clickwrap("clipper_agreement", "1.0", v["sha256"])
    he.post("/legal/v1/acceptances", {"request_id": rid(), "ip": "198.51.100.23"}, caller="clipper_network")
    text = he.all_text().lower()
    for bad in ("198.51.100.23", '"ip"', "ip_address", "user_agent", "date_of_birth", '"dob"'):
        assert bad not in text, bad


# --- G7 ----------------------------------------------------------------------------------------------------------

def test_g7_zero_false_negatives_on_the_pattern_list():
    g = default_guard()
    total = 0
    for p in g.patterns:
        assert p.examples, p.pid
        for e in p.examples:
            total += 1
            assert p.pid in g.scan(e), (p.pid, e)
            assert g.scan(e.upper()) and g.scan(" ".join(e)) , e          # case and letter-spacing variants
    assert total >= 40


SUBJECTS = ["The clipper agreement", "Document client_msa version 1.1", "The routing notice", "Matter lg-mat-01",
            "The hold on this matter", "Filing dmca_agent_designation", "The counter-notice", "The obligation",
            "Your request", "The campaign order form", "The privacy policy", "Takedown notice lg-tdn-2",
            "The engagement letter", "The playbook review", "Acceptance lg-acc-3", "The retention class",
            "The data sheet soi_zbc", "The music ruling", "The sign-off for camp-7", "Counsel question CQ-21"]
VERBS = ["was recorded on", "was received on", "is scheduled for", "was routed to counsel on", "expires on",
         "was forwarded on", "was acknowledged on", "was calendared for", "was filed on", "is due on"]
TAILS = ["2026-10-01.", "2026-12-07 at 09:00.", "the next business day.", "2027-01-15; reference lg-ref-9.",
         "the date shown in the portal.", "a date Andre entered.", "2029-10-01 (three years after filing)."]
EXTRA = ["Thank you; your submission was received.", "Status: routed. Reference number follows.",
         "This message contains dates and statuses of your own records.",
         "Your music track id is required for each clip.", "Please keep this reference for your records.",
         "The restore window opens on the tenth business day after receipt.",
         "Two thousand views were verified for clip 12.", "The legal department received your request.",
         "Our counsel reviews documents before they are published.",
         "This is not legal advice; our documents are reviewed by our counsel; obtain your own counsel."]


def _benign(n=1000) -> list[str]:
    rnd = random.Random(20260926)
    combos = list(itertools.product(SUBJECTS, VERBS, TAILS))
    rnd.shuffle(combos)
    out = [f"{s} {v} {t}" for s, v, t in combos[: n - len(EXTRA)]] + EXTRA
    assert len(out) == n
    return out


def test_g7_benign_fuzz_false_positives_recorded(capsys):
    g = default_guard()
    sentences = _benign()
    fps = [s for s in sentences if g.scan(s)]
    with capsys.disabled():
        print(f"\n[G7] advice guard: {len(fps)} false positive(s) on {len(sentences)} benign sentences"
              + (f"; first: {fps[:3]}" if fps else ""))
    assert len(fps) <= 10                    # recorded; a regression past 1% fails


# --- G8 ----------------------------------------------------------------------------------------------------------

def test_g8_no_float_field_anywhere_and_money_is_the_s1_string():
    for cls in _all_models():
        for name, f in cls.model_fields.items():
            assert "float" not in str(f.annotation).lower(), (cls.__name__, name)
    # launch_guard.py (fix wave 26b, C5-3) is the launchers' shared GIL switch-interval check, byte-identical in every
    # service: it parses that interval (seconds) with float() and touches no money, model or record
    src = "\n".join(p.read_text() for p in SRC.rglob("*.py") if p.name != "launch_guard.py")
    assert not re.search(r"\bfloat\(", src)
    assert "Decimal" not in (SRC / "launch_guard.py").read_text() and "money" not in (SRC / "launch_guard.py").read_text()
    for bad in ("12.3", "12", "-1.00", "1e3", 12.3):
        with pytest.raises(Exception):
            models.ContractTermsModel.model_validate({"client_id": "a", "signed": False, "start_date": "2026-10-01",
                                                      "services": [], "allowed_commitment_categories": [],
                                                      "ccpa_cpra_clause_present": False, "monthly_spend_cap_usd": bad})
    ok = models.ContractTermsModel.model_validate({"client_id": "a", "signed": False, "start_date": "2026-10-01",
                                                   "services": [], "allowed_commitment_categories": [],
                                                   "ccpa_cpra_clause_present": False, "monthly_spend_cap_usd": "12.30"})
    assert ok.monthly_spend_cap_usd == "12.30"
    for bad in ("5000", "5000.0", 5000.0):
        with pytest.raises(Exception):
            models.MatterIntake.model_validate({"request_id": "r", "channel": "email", "requester_ref": "x",
                                                "kind": "contract_dispute", "disputed_amount_usd": bad})
