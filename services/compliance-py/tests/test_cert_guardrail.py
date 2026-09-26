"""Certification guardrails, spec §H items 26-31 (run on every change)."""

import ast
import hashlib
import inspect
import json
import typing
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

import facts as facts_mod
import models
import ports
from fetcher import FetchFailed, NotWiredFetcher
from helpers import SEED_PATH, Harness, base_env, client_facts, clip_facts, publish_facts, rid
from register import SHELF_LIFE_DAYS, seed_checks
from store import StoreCorrupt

SRC = Path(__file__).resolve().parents[1] / "src"
SPEC_SHA = "4e3821d018a42be76ede1bf501004f0a8c5590327fa98ddd9f8c3f16845f584d"
MONEY_WORDS = ("amount", "usd", "price", "rate", "balance", "money", "fee", "payment", "cents", "currency")


def _annotations(model: type[BaseModel]):
    for name, f in model.model_fields.items():
        yield model.__name__, name, f.annotation


def _flatten(tp):
    yield tp
    for a in typing.get_args(tp):
        yield from _flatten(a)


# H.26 ------------------------------------------------------------------------------------

def test_h26_no_float_decimal_or_money_field_in_any_request_model():
    for model in models.REQUEST_MODELS:
        for mname, fname, ann in _annotations(model):
            assert not any(word in fname.lower() for word in MONEY_WORDS), (mname, fname)
            for t in _flatten(ann):
                assert t not in (float, Decimal), (mname, fname)


def test_h26_no_money_in_the_fact_schemas():
    numbers = []

    def walk(spec, path):
        if spec.kind == "obj":
            for k, sub in spec.fields.items():
                assert not any(w in k.lower() for w in MONEY_WORDS), f"{path}.{k}"
                walk(sub, f"{path}.{k}")
        elif spec.kind == "list":
            walk(spec.item, path + "[]")
        elif spec.kind == "number":
            numbers.append(path)
    for name, sch in [*facts_mod.ACTIVATION_SCHEMAS.items(), ("payout", facts_mod.PAYOUT_SCHEMA),
                      ("publish", facts_mod.PUBLISH_SCHEMA)]:
        walk(sch, name)
    # the only non-integer number is the label offset in seconds (not money)
    assert set(numbers) == {"payout.disclosure.in_video_label_start_s", "publish.disclosure.in_video_label_start_s"}


def test_h26_no_money_key_in_any_response(hs):
    hs.activate_creator()
    hs.activate_brand()
    responses = [hs.review("zbc_clip", rid(), clip_facts()).json(), hs.review("zbm_work", "w", publish_facts()).json(),
                 hs.rule("c", "client", client_facts()).json(), hs.get("/compliance/v1/controls").json(),
                 hs.get("/compliance/v1/trust-center").json(), hs.get("/compliance/v1/audit/export").json()]

    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)
    for r in responses:
        for k in keys(r):
            assert not any(w == k.lower() or k.lower().endswith("_" + w) for w in MONEY_WORDS), k


# H.27 ------------------------------------------------------------------------------------

LLM_MODULES = ("openai", "anthropic", "google.generativeai", "google.genai", "langchain", "transformers", "cohere",
               "mistralai", "llama_index", "litellm", "vertexai", "boto3", "ollama", "groq")


def test_h27_no_llm_sdk_import_anywhere_in_src():
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for n in names:
                assert not any(n == m or n.startswith(m + ".") for m in LLM_MODULES), (path.name, n)


def test_h27_tests_cannot_open_network_sockets():
    import socket
    with pytest.raises(RuntimeError, match="network"):
        socket.create_connection(("127.0.0.1", 9))


# H.28 ------------------------------------------------------------------------------------

def test_h28_every_stand_in_says_not_allowed():
    vi = ports.NotBuiltVerificationIntegrity()
    assert vi.age_attestation("a").available is False
    assert vi.attest_clip("s", "p", "youtube", "2026-01-01T00:00:00Z", 14).available is False
    fin = ports.NotBuiltFinance31()
    assert fin.tax_status("x").available is False and fin.rail_status("x").available is False
    assert ports.NotBuiltLegal37().current_version("pp").available is False
    s = ports.NotWiredSanctionsProvider()
    assert s.screen("n", [], None, "US", None).result == "unavailable" and s.current_list_version() is None
    assert ports.NotWiredAccessibilityChecker().check("a", "site", "0" * 64).available is False
    with pytest.raises(FetchFailed):
        NotWiredFetcher().fetch("https://www.ftc.gov/feeds/blog-business.xml")
    assert set(ports.STAND_INS) == {c for _, c in inspect.getmembers(ports, inspect.isclass)
                                    if c.__name__.startswith(("NotBuilt", "NotWired"))}


def test_h28_no_passing_fake_is_defined_in_src():
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                assert not node.name.startswith(("Fake", "Passing", "Allow", "Accepting")), (path.name, node.name)


def test_h28_default_service_uses_only_stand_ins():
    import api
    svc = api.app.state.service
    assert type(svc.ports.verification).__name__ == "NotBuiltVerificationIntegrity"
    assert type(svc.ports.finance).__name__ == "NotBuiltFinance31"
    assert type(svc.ports.legal).__name__ == "NotBuiltLegal37"
    assert type(svc.ports.sanctions).__name__ == "NotWiredSanctionsProvider"
    assert type(svc.ports.accessibility).__name__ == "NotWiredAccessibilityChecker"
    assert type(svc.ports.fetcher).__name__ == "NotWiredFetcher"
    assert type(svc.recorder.client).__name__ == "UnconfiguredLedgerClient"


# H.29 ------------------------------------------------------------------------------------

def test_h29_seed_file_hash_matches_the_spec():
    assert hashlib.sha256(SEED_PATH.read_bytes()).hexdigest() == SPEC_SHA


def test_h29_modified_seed_refuses_to_start(tmp_path):
    bad = tmp_path / "seed.json"
    bad.write_bytes(SEED_PATH.read_bytes().replace(b'"HR-02"', b'"HR-2X"', 1))
    with pytest.raises(RuntimeError, match="refusing to start"):
        Harness(env={"COMPLIANCE_SEED_PATH": str(bad)})
    with pytest.raises(RuntimeError, match="refusing to start"):
        Harness(env={"COMPLIANCE_SEED_SHA256": "0" * 64})


# H.30 ------------------------------------------------------------------------------------

def test_h30_tampered_log_refuses_to_start_and_runtime_tamper_turns_c11_red(tmp_path):
    x = Harness(data_dir=str(tmp_path))
    x.approve_seed()
    x.rule("client-1", "client", client_facts())
    log = tmp_path / "compliance_log.jsonl"
    lines = log.read_bytes().split(b"\n")
    lines[-2] = lines[-2].replace(b'"allowed":false', b'"allowed":true')
    log.write_bytes(b"\n".join(lines))
    with pytest.raises(StoreCorrupt):
        Harness(data_dir=str(tmp_path))
    # runtime tamper: C-11 goes red at the next internal run
    y_dir = tmp_path / "y"
    y = Harness(data_dir=str(y_dir))
    y.approve_seed()
    assert y.run_controls()["results"]["C-11"]["result"] == "pass"
    ylog = y_dir / "compliance_log.jsonl"
    data = ylog.read_bytes()
    ylog.write_bytes(data.replace(b'"approved_by":"andre"', b'"approved_by":"mallory"', 1))
    assert y.run_controls()["results"]["C-11"]["result"] == "fail"
    assert y.get("/compliance/v1/controls/C-11").json()["status"] == "red"


def test_h30_deleted_or_reordered_lines_refuse_to_start(tmp_path):
    x = Harness(data_dir=str(tmp_path))
    x.approve_seed()
    x.rule("client-1", "client", client_facts())
    log = tmp_path / "compliance_log.jsonl"
    lines = [ln for ln in log.read_bytes().split(b"\n") if ln]
    log.write_bytes(b"\n".join(lines[:1] + lines[2:]) + b"\n")
    with pytest.raises(StoreCorrupt):
        Harness(data_dir=str(tmp_path))
    log.write_bytes(b"\n".join(lines) + b"\n" + b'{"torn":')
    with pytest.raises(StoreCorrupt):
        Harness(data_dir=str(tmp_path))


def test_persistence_replays_versions_rulings_and_holds(tmp_path):
    x = Harness(data_dir=str(tmp_path))
    x.approve_seed()
    x.run_controls()
    a = x.activate_client()
    y = Harness(data_dir=str(tmp_path), clock=x.clock, ledger=x.ledger)
    assert y.get("/health").json() == {"status": "ok", "service": "compliance-py", "register_version_in_force": 1,
                                       "in_memory": False, "seed_pinned": True, "production": True}
    assert y.get(f"/compliance/v1/rulings/{a['ruling_id']}").json()["allowed"] is True
    assert y.svc.control_state["C-11"]["last_result"] == "pass"


def test_in_memory_restart_has_no_version_in_force():
    x = Harness()
    x.approve_seed()
    y = Harness(ledger=x.ledger)
    assert y.get("/health").json()["register_version_in_force"] is None


# H.31 ------------------------------------------------------------------------------------

def test_h31_seed_rows_source_urls_and_expiry_arithmetic():
    rows = json.loads(SEED_PATH.read_bytes())["rows"]
    assert seed_checks(rows) == []
    for r in rows:
        if r["status"] == "verified":
            assert r["source_url"] is not None or r["source_kind"] == "house-rule", r["id"]
            life = SHELF_LIFE_DAYS[r["source_kind"]]
            expect = None if life is None else (date.fromisoformat(r["verified_at"]) + timedelta(days=life)).isoformat()
            assert r["expires_at"] == expect, r["id"]
    assert len(rows) == 118
    assert sum(r["status"] == "verified" for r in rows) == 59
    assert sum(r["source_kind"] == "house-rule" for r in rows) == 13
    assert sum(r["counsel_flag"] for r in rows) == 14
    assert {r["id"] for r in rows if r["counsel_flag"]} == {f"CQ-{i:02d}" for i in range(1, 15)}
    assert all(r["check"] == "counsel_memo" and r["status"] == "unverified" for r in rows if r["counsel_flag"])


def test_h31_base_env_helper_is_test_only():
    assert "COMPLIANCE_SERVICE_TOKEN" in base_env()
