"""Guardrails G1-G7 (spec §F) plus config refusals."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import re
import typing
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

import config as config_mod
import models
import ports
from adapters import http_adapters as ha
from adapters.base import CallRefused, Http
from helpers import Harness, base_env
from store import encode
from test_cert_scenarios import codes, run_to_day

SRC = Path(__file__).resolve().parents[1] / "src"
ROOT = Path(__file__).resolve().parents[3]


# --- G1 --------------------------------------------------------------------------------------------------------------

def _annotations(tp) -> list:
    out = [tp]
    for a in typing.get_args(tp):
        out += _annotations(a)
    return out


def test_g1_no_float_or_money_field_in_any_model_or_port():
    from decimal import Decimal
    money = re.compile(r"amount|currency|balance|usd|price|payout_rate|cpm|earning", re.I)
    classes = [c for c in vars(models).values() if isinstance(c, type) and issubclass(c, BaseModel) and c is not BaseModel]
    assert len(classes) > 10
    for cls in classes:
        for name, f in cls.model_fields.items():
            assert not money.search(name), (cls.__name__, name)
            for t in _annotations(f.annotation):
                assert t not in (float, Decimal), (cls.__name__, name)
    for cls in [c for c in vars(ports).values() if dataclasses.is_dataclass(c)]:
        for f in dataclasses.fields(cls):
            assert not money.search(f.name), (cls.__name__, f.name)
            assert "float" not in str(f.type), (cls.__name__, f.name)


def test_g1_no_money_key_in_any_output(hr):
    hr.clean_clip("g1", views=9000, likes=900)
    run_to_day(hr, 20)
    text = hr.all_text()
    keys = set(re.findall(r'"([A-Za-z_]+)":', text))
    assert not [k for k in keys if re.search(r"amount|currency|balance|usd|price|cpm|earning|payout_rate", k, re.I)]


# --- G2 --------------------------------------------------------------------------------------------------------------

def test_g2_no_llm_sdk_import():
    banned = {"anthropic", "openai", "langchain", "llama_index", "transformers", "cohere", "google.generativeai",
              "mistralai", "litellm"}
    for p in SRC.rglob("*.py"):
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for n in names:
                assert n.split(".")[0] not in banned and n not in banned, (p.name, n)


def test_g2_no_network_in_tests():
    import socket
    with pytest.raises(RuntimeError, match="network"):
        socket.create_connection(("127.0.0.1", 9))


# --- G3 --------------------------------------------------------------------------------------------------------------

def test_g3_every_stand_in_answers_unavailable():
    v = ports.NotWiredTokenVault()
    assert v.authorization_url("tiktok", "s", "c", "https://x", ()) is None
    assert v.exchange_and_store("tiktok", "code", "ver", "https://x").available is False
    with pytest.raises(ports.VaultUnavailable):
        v.with_token("ref", "p", lambda t: t)
    assert v.destroy("ref") is False and v.identity_hmac_key() is None
    for p in ("youtube", "tiktok", "instagram", "x"):
        a = ports.NotWiredAdapter(p)
        assert a.account(v, "r").available is False
        f = a.fetch(v, "r", "acct", "ref", ("views",), 0)
        assert f.available is False and f.live_state == "unknown" and f.values == {}
    assert ports.NotWiredOEmbed().check("https://www.tiktok.com/@a/video/1") == "unknown"
    hs = ports.NotWiredPerceptualHasher()
    assert hs.video_signature("m") is None and hs.image_pdq(b"x") is None and hs.match({}, {}) is None
    with pytest.raises(RuntimeError):
        hs.distance("a", "b")
    assert ports.NotWiredMediaIntake().sha256("m") is None
    assert ports.NotWiredAgeAssuranceProvider().check("photo_id_match", "s", "1990-01-01").result == "unavailable"
    assert ports.NotWiredCompliance().row("HR-13").available is False
    assert ports.NotBuiltLegal37().takedown_notices("x").available is False
    assert ports.NotBuiltFinance31().payout_identity_hmac("c").available is False
    assert ports.NotBuiltPeople43().delegate_active("rev") is None
    assert ports.NotBuiltClipperNetwork().view_cap("c").available is False
    assert set(ports.STAND_INS) == {c for c in vars(ports).values() if isinstance(c, type)
                                    and c.__name__.startswith(("NotWired", "NotBuilt"))}


def test_g3_no_passing_fake_in_src():
    for p in SRC.rglob("*.py"):
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                assert not node.name.startswith(("Fake", "Passing", "Stub", "Mock")), (p.name, node.name)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
                assert "fakes" not in (mod or ""), p.name


def test_g3_production_wiring_is_all_stand_ins():
    import api
    s = config_mod.load(base_env())
    p = api.build_ports(s)
    assert isinstance(p.vault, ports.NotWiredTokenVault) and isinstance(p.age, ports.NotWiredAgeAssuranceProvider)
    assert all(isinstance(a, ports.NotWiredAdapter) for a in p.adapters.values())
    assert isinstance(p.compliance, ports.NotWiredCompliance) and isinstance(p.oembed, ports.NotWiredOEmbed)


def test_g3_day_one_with_stand_ins_nothing_certifies():
    h = Harness(passing=False)
    h.approve_rules()
    s = h.connect("clip-a")
    assert s["started"] is False and codes(s) == ["DEPENDENCY_UNAVAILABLE"]
    h.ok(h.register("d1", "clip-a", post_ref="https://www.tiktok.com/@c/video/1"), 201)
    run_to_day(h, 17)
    c = h.cert("d1")
    assert c["status"] == "not_certified" and c["settlement_source"] == "default"
    assert {"NOT_CONNECTED", "DEPENDENCY_UNAVAILABLE", "AGE_NOT_ASSURED"} <= set(codes(c))


# --- G4 --------------------------------------------------------------------------------------------------------------

def test_g4_every_negative_ruling_cites_a_rule_in_force(hr):
    hr.clean_clip("g4a")
    hr.clean_clip("g4b", clipper="clip-b", views=50000, likes=1)
    hr.ok(hr.register("g4c", "clip-c", "snapchat", post_ref="https://snap.example/1"), 201)
    run_to_day(hr, 3)
    hr.adapters["tiktok"].videos["https://www.tiktok.com/@c/video/g4a"]["gone"] = True
    run_to_day(hr, 17, 4)
    ids = {r["rule_id"] for r in hr.svc.current.rows}
    negatives = 0
    for rec in hr.svc.log.records:
        for kind, r in rec["data"]["ops"]:
            reasons = None
            if kind == "certification" and r["status"] in ("not_certified", "pending", "revised", "voided"):
                reasons = r["reasons"]
            elif kind == "attestation" and not (r["response"].get("verified") or r["response"].get("verified_views")):
                reasons = r["response"]["reasons"]
            elif kind == "connection" and r["status"] == "refused":
                reasons = r["reasons"]
            elif kind in ("hold", "fingerprint", "fetch") and r.get("reasons") is not None and not r.get("available"):
                reasons = r["reasons"]
            if reasons is None:
                continue
            if rec["data"]["rules_version"] is None:
                assert all(x["rule_id"] == "VI-00" for x in reasons)
                continue
            negatives += 1
            assert reasons, (kind, r)
            for x in reasons:
                assert x["rule_id"] in ids and x["code"] in models.__dict__.get("CATALOG", __import__("reasons").CATALOG)
    assert negatives > 10


# --- G5 --------------------------------------------------------------------------------------------------------------

def test_g5_seed_hash_pinned_everywhere():
    seed = (SRC.parent / "seed" / "vi_rules_seed.json").read_bytes()
    sha = hashlib.sha256(seed).hexdigest()
    assert sha == config_mod.PINNED_SEED_SHA256
    adr = (ROOT / "docs" / "adr" / "0007-verification-integrity-architecture.md").read_text()
    assert sha in adr
    assert len(json.loads(seed)["rows"]) == 28


def test_g5_other_seed_refuses_unless_explicitly_unpinned(tmp_path):
    doc = json.loads((SRC.parent / "seed" / "vi_rules_seed.json").read_bytes())
    doc["rows"][1]["statement"] += " (edited)"
    p = tmp_path / "seed.json"
    p.write_bytes(json.dumps(doc).encode())
    with pytest.raises(RuntimeError, match="does not match"):
        Harness(env={"VI_SEED_PATH": str(p)})
    with pytest.raises(RuntimeError):
        config_mod.load(base_env(VI_SEED_SHA256="0" * 64))
    other = hashlib.sha256(p.read_bytes()).hexdigest()
    h = Harness(env={"VI_SEED_PATH": str(p), "VI_ALLOW_UNPINNED_SEED": "1", "VI_SEED_SHA256": other})
    hl = h.ok(h.get("/health"))
    assert hl["rules_pinned"] is False and hl["production"] is False


# --- G6 --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("attack", ["edit", "delete", "reorder", "truncate_tail", "torn", "rewrite_consistent"])
def test_g6_log_tampering_refuses_start(tmp_path, attack):
    from fakes import FakeLedgerClient
    ledger = FakeLedgerClient()
    h = Harness(data_dir=str(tmp_path), ledger=ledger)
    h.approve_rules()
    h.clean_clip("g6")
    path = tmp_path / "vi_log.jsonl"
    lines = path.read_bytes().splitlines()
    assert len(lines) >= 6
    if attack == "edit":
        i = next(i for i, x in enumerate(lines) if b'"metric":"views"' in x)
        lines[i] = lines[i].replace(b'"metric":"views"', b'"metric":"VIEWS"')
    elif attack == "delete":
        del lines[2]
    elif attack == "reorder":
        lines[2], lines[3] = lines[3], lines[2]
    elif attack == "truncate_tail":
        lines = lines[:-2]
    elif attack == "torn":
        path.write_bytes(b"\n".join(lines) + b"\n" + lines[-1][:20])
    if attack == "rewrite_consistent":        # a whole-file rewrite with recomputed hashes: caught by the anchors
        recs, prev, out = [json.loads(x) for x in lines], "0" * 64, []
        recs[-1]["at"] = "2026-01-01T00:00:00Z"
        for r in recs:
            r["prev_line_sha256"] = prev
            r.pop("record_sha256")
            r["record_sha256"] = hashlib.sha256(encode(r)).hexdigest()
            line = encode(r)
            prev = hashlib.sha256(line).hexdigest()
            out.append(line)
        lines = out
    if attack != "torn":
        path.write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(Exception):
        Harness(data_dir=str(tmp_path), ledger=ledger)


# --- G7 --------------------------------------------------------------------------------------------------------------

class _Vault:
    def with_token(self, ref, purpose, fn):
        return fn("tok-g7")


def test_g7_adapters_only_make_documented_calls():
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request):
        seen.append((req.method, str(req.url)))
        assert req.headers.get("cookie") is None
        u = str(req.url)
        if "youtube/v3/channels" in u:
            return httpx.Response(200, json={"items": [{"id": "UC1"}]})
        if "youtube/v3/videos" in u:
            return httpx.Response(200, json={"items": [{"id": "abcdefghijk", "statistics": {"viewCount": "12"},
                                                        "snippet": {"channelId": "UC1",
                                                                    "publishedAt": "2026-10-01T09:00:00Z"}}]})
        if "youtubeanalytics" in u:
            return httpx.Response(200, json={"rows": [[5, 40]]})
        if "user/info" in u:
            return httpx.Response(200, json={"data": {"user": {"open_id": "oid"}}})
        if "video/list" in u:
            return httpx.Response(200, json={"data": {"videos": [{"id": "1234567", "create_time": 1790845200,
                                                                   "view_count": 9}], "has_more": False}})
        if "graph.instagram.com" in u and "/insights" in u:
            return httpx.Response(200, json={"data": [{"name": "views", "values": [{"value": 4}]}]})
        if "graph.instagram.com" in u and u.split("?")[0].endswith("/me"):
            return httpx.Response(200, json={"user_id": "17", "account_type": "MEDIA_CREATOR", "followers_count": 500})
        if "graph.instagram.com" in u:
            return httpx.Response(200, json={"id": "178900001", "timestamp": "2026-10-01T09:00:00+0000",
                                             "owner": {"id": "17"}})
        if "users/me" in u:
            return httpx.Response(200, json={"data": {"id": "44"}})
        if "api.x.com/2/tweets/" in u:
            return httpx.Response(200, json={"data": {"id": "1234567", "author_id": "44",
                                                      "created_at": "2026-10-01T09:00:00Z",
                                                      "public_metrics": {"impression_count": 7}}})
        return httpx.Response(404)
    t = httpx.MockTransport(handler)
    v = _Vault()
    yt = ha.YouTubeAdapter(t)
    assert yt.account(v, "r").account_id == "UC1"
    a = yt.fetch(v, "r", "UC1", "https://www.youtube.com/shorts/abcdefghijk", ("views", "avg_view_percentage",
                                                                               "engaged_views"), None)
    assert a.available and a.values["views"] == 12 and a.values["avg_view_percentage"] == 40
    tt = ha.TikTokAdapter(t)
    assert tt.account(v, "r").account_id == "oid"
    a = tt.fetch(v, "r", "oid", "https://www.tiktok.com/@u/video/1234567", ("views",), 1790845200)
    assert a.available and a.values["views"] == 9 and a.video.create_time == 1790845200
    ig = ha.InstagramAdapter(t)
    acc = ig.account(v, "r")
    assert acc.account_id == "17" and acc.is_professional and acc.followers == 500
    xa = ha.XAdapter(t)
    assert xa.account(v, "r").account_id == "44"
    assert xa.fetch(v, "r", "44", "https://x.com/u/status/1234567", ("impressions",), None).values["impressions"] == 7
    allowed = ha.YouTubeAdapter.ALLOWED + ha.TikTokAdapter.ALLOWED + ha.InstagramAdapter.ALLOWED + ha.XAdapter.ALLOWED
    for method, url in seen:
        assert any(method == m and url.startswith(p) for m, p in allowed), (method, url)
        assert method in ("GET", "POST") and (method == "GET" or url.startswith(ha.TT_API + "video/list/"))
    http = Http(ha.YouTubeAdapter.ALLOWED, t)
    for method, url in (("POST", ha.YT_DATA + "videos"), ("GET", "https://www.youtube.com/watch?v=abc"),
                        ("GET", "http://www.googleapis.com/youtube/v3/videos"), ("DELETE", ha.YT_DATA + "videos")):
        with pytest.raises(CallRefused):
            http.call(method, url)
    with pytest.raises(CallRefused):
        Http(ha.TikTokAdapter.ALLOWED, t).call("GET", "https://www.tiktok.com/@user")      # an HTML page: refused


def test_g7_adapter_failures_never_carry_the_token():
    def handler(req):
        raise httpx.ConnectError(f"refused, header was {req.headers.get('authorization')}")
    t = httpx.MockTransport(handler)
    for ad in (ha.YouTubeAdapter(t), ha.TikTokAdapter(t), ha.InstagramAdapter(t), ha.XAdapter(t)):
        a = ad.fetch(_Vault(), "r", "acct", "1234567", ("views",), 1790845200)
        assert a.available is False and "tok-g7" not in repr(a)
        assert ad.account(_Vault(), "r").available is False


def test_g7_rate_limit_and_oembed():
    t = httpx.MockTransport(lambda r: httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}}))
    a = ha.TikTokAdapter(t).fetch(_Vault(), "r", "oid", "1234567", ("views",), 1790845200)
    assert a.available is False and a.rate_limited is True
    o = ha.TikTokOEmbed(httpx.MockTransport(lambda r: httpx.Response(200, json={"title": "x"})))
    assert o.check("https://www.tiktok.com/@u/video/1234567") == "live"
    assert o.check("https://evil.example/x") == "unknown"
    o = ha.TikTokOEmbed(httpx.MockTransport(lambda r: httpx.Response(404)))
    assert o.check("https://www.tiktok.com/@u/video/1234567") == "gone"


# --- config refusals --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("over,msg", [
    ({"VI_SERVICE_TOKEN": "__unset__"}, "VI_SERVICE_TOKEN"),
    ({"VI_VAULT": "hashicorp"}, "VI_VAULT"),
    ({"VI_HASHER": "pdq"}, "VI_HASHER"),
    ({"VI_AGE_PROVIDER": "yoti"}, "VI_AGE_PROVIDER"),
    ({"VI_MEDIA_INTAKE": "s3"}, "VI_MEDIA_INTAKE"),
    ({"VI_TIKTOK_APP_CREDENTIALS_REF": "ref"}, "CREDENTIALS_REF"),
    ({"VI_DEVICE_SIGNALS_ENABLED": "1"}, "DEVICE"),
    ({"VI_COUNT_SOURCE": "scrape"}, "owner_oauth_api"),
    ({"VI_PLATFORMS_ENABLED": "tiktok,twitch"}, "twitch"),
    ({"VI_YT_IDS_PER_CALL": "50"}, "only 1"),
    ({"VI_COMPLIANCE_URL": "http://c"}, "together"),
    ({"VI_YT_RESERVE_UNITS": "20000"}, "RESERVE"),
    ({"VI_CALLER_TOKENS": json.dumps({"hacker": "x" * 40})}, "unknown name"),
    ({"VI_CALLER_TOKENS": json.dumps({"scheduler": "short"})}, ">= 32"),
    ({"VI_ANOM_MIN_LIKE_RATE": "abc"}, "decimal"),
])
def test_config_refuses_what_it_cannot_honor(over, msg):
    with pytest.raises(RuntimeError, match=msg):
        config_mod.load(base_env(**over))
