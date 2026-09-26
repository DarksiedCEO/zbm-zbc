"""Spec §G guardrail tests G1-G6, plus start-up refusals (config that cannot be honored)."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import typing
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

import api
import config as config_mod
import models
import ports as ports_mod
from helpers import ANDRE_TOKEN, SEED_PATH, Harness, base_env, codes, rid

SRC = Path(__file__).resolve().parents[1] / "src"
MONEYISH = re.compile(r"(amount|price|money|payout|balance|currency|fee|earning|usd|cents|salary|wage|cost)", re.I)


def _types(ann):
    yield ann
    for a in typing.get_args(ann):
        yield from _types(a)


def test_g1_no_float_or_money_field_in_any_model():
    seen = 0
    for model in models.REQUEST_MODELS:
        for name, f in model.model_fields.items():
            seen += 1
            assert not MONEYISH.search(name), (model.__name__, name)
            for t in _types(f.annotation):
                assert t not in (float, Decimal), (model.__name__, name)
                if isinstance(t, type) and issubclass(t, BaseModel):
                    assert t in models.REQUEST_MODELS
    assert seen > 60
    # templates have no money type either
    import templates
    assert not any(MONEYISH.search(t) for t in templates.TYPES)


def test_g2_no_llm_sdk_and_same_pinned_requirements():
    banned = {"openai", "anthropic", "langchain", "transformers", "cohere", "google", "vertexai", "llama_cpp", "mistralai",
              "groq", "torch", "tensorflow"}
    for py in SRC.rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            assert not (set(names) & banned), (py.name, names)
    here = (SRC.parent / "requirements.txt").read_text()
    assert here == (SRC.parents[1] / "compliance-py" / "requirements.txt").read_text()


def test_g3_no_passing_fake_in_src_and_every_stand_in_says_not_allowed():
    for py in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.ClassDef):
                assert not node.name.startswith(("Fake", "Passing", "Stub", "Mock")), (py.name, node.name)
    p = ports_mod.Ports()
    vi = p.vi
    for ans in (vi.age_subject("c"), vi.age_check("r", "c", "2000-01-01", True, "photo_id_match", "p"),
                vi.identity_check("r", "c", "a@b.co"), vi.connections("c"), vi.connection_start("r", "c", "youtube", "https://x"),
                vi.connection_complete("r", "s", "code"), vi.connection_revoke("r", "k"), vi.integrity("c"),
                vi.strikes(None), vi.finding("f"), vi.certifications("c"), vi.ban("r", "c", "d", "t", "andre-token"),
                p.compliance.resolve_person("r", "US", "US-CA", True, "a"), p.compliance.creator_activation("r", "c", {}),
                p.compliance.latest_activation("zbc_creator", "c"), p.compliance.review_email_campaign("r", "s", {}),
                p.creative.live_rulebook("c"), p.creative.kit("c"), p.finance.tax_status("c"), p.finance.rate_card("d", "v"),
                p.finance.open_items("c"), p.finance.notify_offboarding("c", "o"), p.legal.current_version("d"),
                p.people.delegate_active("maria"), p.hub.revoke_session("c"), p.push.push("t", {})):
        assert ans.available is False, ans
    assert p.messaging.send("m", "email", "a@b.co", "body").delivered is False
    # the default service (env only, no fakes) wires only stand-ins
    h = Harness(stand_ins=True)
    assert all(type(getattr(h.ports, f)).__name__.startswith(("NotBuilt", "NotWired")) for f in
               ("vi", "compliance", "creative", "finance", "legal", "people", "messaging", "hub", "push"))


def test_g4_every_adverse_item_cites_a_rule_in_force():
    h = Harness().ready()
    h.ports.finance.form = False
    h.ports.vi.default_connection = False
    cid = h.apply().json()["clipper_id"]
    j = h.admit(cid).json()
    rules = {r["rule_id"]: r for r in h.get("/cn/v1/rules").json()["rules"]}
    assert j["unmet"]
    for u in j["unmet"]:
        assert u["rule_id"] in rules and (rules[u["rule_id"]]["status"] in ("in_force", "open"))
        assert re.fullmatch(r"cn/CN-[0-9A-Z-]+/[A-Z_:0-9a-z]+: .+", "cn/" + u["rule_id"] + "/" + u["code"] + ": " + u["message"])
    # retire CN-06 (acknowledged) -> the check still runs but cites CN-00: the decision stays negative
    p = h.propose_rule({"kind": "retire", "target_id": "CN-06"}).json()["proposal"]
    assert p["weakening"] is True
    h.approve(p, ack=True)
    j2 = h.admit(cid).json()
    assert ("CN-00", "RULE_NOT_IN_FORCE:CN-06") in codes(j2)
    assert all(u["rule_id"] != "CN-06" for u in j2["unmet"])


def test_g5_seed_hash_pinned():
    data = SEED_PATH.read_bytes()
    assert hashlib.sha256(data).hexdigest() == config_mod.PINNED_SEED_SHA256
    adr = (SRC.parents[2] / "docs" / "adr" / "0008-clipper-network-architecture.md").read_text()
    assert config_mod.PINNED_SEED_SHA256 in adr
    with pytest.raises(RuntimeError, match="differs from the pinned seed hash"):
        config_mod.load(base_env(CN_RULES_SEED_SHA256="a" * 64))
    with pytest.raises(RuntimeError, match="CN_ALLOW_UNPINNED_SEED"):
        config_mod.load(base_env(CN_RULES_SEED_PATH="/tmp/other.json"))


def test_g5_tampered_seed_refuses_start(tmp_path):
    seed = json.loads(SEED_PATH.read_bytes())
    seed["rules"][1]["parameters"]["guardian_path"] = True
    p = tmp_path / "seed.json"
    p.write_bytes(json.dumps(seed).encode())
    s = config_mod.load(base_env())
    s.seed_path = str(p)
    with pytest.raises(RuntimeError, match="does not match the expected"):
        api.build_service(s, ledger=Harness().ledger)
    # explicitly unpinned: starts, but reports itself non-production (rules_pinned false)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    seed["rules"][1]["parameters"]["guardian_path"] = False
    p.write_bytes(json.dumps(seed).encode())
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    h = Harness(env={"CN_ALLOW_UNPINNED_SEED": "1", "CN_RULES_SEED_SHA256": sha, "CN_RULES_SEED_PATH": str(p)})
    assert h.client.get("/health").json()["rules_pinned"] is False


def test_g6_log_chain_tamper_refuses_start(tmp_path):
    h = Harness(data_dir=str(tmp_path / "d")).ready()
    h.admitted_clipper()
    log = tmp_path / "d" / "cn_log.jsonl"
    lines = log.read_bytes().split(b"\n")
    lines[3] = lines[3].replace(b'"applicant"', b'"active"', 1) if b'"applicant"' in lines[3] else lines[3][:-5] + b'0000}'
    log.write_bytes(b"\n".join(lines))
    with pytest.raises(Exception, match="(chain|hash|JSON|canonical)"):
        Harness(data_dir=str(tmp_path / "d"), ledger=h.ledger)


def test_health_shape_and_docs_off():
    h = Harness()
    assert h.client.get("/health").json() == {"status": "ok", "service": "clipper-network-py", "rules_version": None,
                                              "in_memory": True, "rules_pinned": True, "reconcile_mode": False,
                                              "reconcile_required": False}
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert h.client.get(p).status_code == 404


@pytest.mark.parametrize("env,match", [
    ({"CN_SERVICE_TOKEN": "__unset__"}, "CN_SERVICE_TOKEN"),
    ({"CN_IDENTITY_HMAC_KEY": "__unset__"}, "CN_IDENTITY_HMAC_KEY"),
    ({"CN_IDENTITY_HMAC_KEY": "short"}, "CN_IDENTITY_HMAC_KEY"),
    ({"CN_CALLER_TOKENS": json.dumps({"hub": "short"})}, "printable"),
    ({"CN_CALLER_TOKENS": json.dumps({"stranger": "x" * 40})}, "unknown name"),
    ({"CN_ANDRE_APPROVAL_TOKEN": "test-cn-service-token-do-not-use-0000"}, "distinct"),
    ({"CN_MESSAGE_PROVIDER": "sendgrid"}, "no messaging adapter"),
    ({"CN_FINANCE_URL": "http://127.0.0.1:1"}, "no adapter"),
    ({"CN_CHANNELS": "email_opt_in,sms"}, "narrow"),
    ({"CN_VI_URL": "http://127.0.0.1:9"}, "must be set together"),
    ({"CN_OPT_OUT_URL": "http://insecure.example"}, "https"),
    ({"CN_RECONCILE_MODE": "yes"}, "0 or 1"),
])
def test_config_that_cannot_be_honored_refuses_start(env, match):
    with pytest.raises(RuntimeError, match=match):
        config_mod.load(base_env(**env))


@pytest.mark.parametrize("name,value", [("CN_APPEAL_WINDOW_DAYS", "3"), ("CN_QUIET_WINDOW", "06:00-23:00"),
                                        ("CN_S2_SUSPENSION_DAYS", "1"), ("CN_ADMISSION_REQUIRES_CONNECTION", "0"),
                                        ("CN_TIER_PLATFORM_ANCHORS", "1"), ("CN_MAX_ENROLMENTS_T0", "50")])
def test_rule_backed_env_values_must_go_through_andre(name, value):
    with pytest.raises(RuntimeError, match="only Andre changes a rule"):
        Harness(env={name: value})


def test_rule_backed_env_restating_the_seed_default_is_accepted():
    h = Harness(env={"CN_APPEAL_WINDOW_DAYS": "14", "CN_QUIET_WINDOW": "08:00-20:00"})
    assert h.client.get("/health").status_code == 200


def test_intelligences_registry_has_ten_and_no_llm():
    h = Harness()
    r = h.get("/intelligences").json()
    assert [x["number"] for x in r] == list(range(1, 11)) and not any(x["llm"] for x in r)
