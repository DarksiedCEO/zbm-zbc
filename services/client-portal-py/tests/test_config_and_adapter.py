"""ADR 0028: start-up refusals, and the typed Legal adapter's distrust of Legal's answers."""

from __future__ import annotations

import json

import httpx
import pytest

import config as config_mod
from helpers import ENV, MSA_TEXT, SOW_TEXT, sha
from legal_client import Gone, LegalClient, LegalUnavailable, parse_accepted, parse_view


@pytest.mark.parametrize("over", [
    {"PORTAL_LEGAL_SERVICE_TOKEN": None}, {"PORTAL_LEGAL_CALLER_TOKEN": None}, {"PORTAL_EVIDENCE_KEY": None},
    {"PORTAL_EVIDENCE_KEY": "short"}, {"PORTAL_LEGAL_CALLER_TOKEN": "${{legal.LEGAL_PORTAL_CALLER_TOKEN}}xxxxxxxxxx"},
    {"PORTAL_EVIDENCE_KEY": ENV["PORTAL_LEGAL_SERVICE_TOKEN"]},
    {"PORTAL_PUBLIC_ORIGIN": None}, {"PORTAL_PUBLIC_ORIGIN": "http://accept.zbm.example"},
    {"PORTAL_PUBLIC_ORIGIN": "https://accept.zbm.example/path"}, {"PORTAL_PUBLIC_ORIGIN": "https://u:p@host.example"},
    {"PORTAL_LEGAL_URL": None}, {"PORTAL_LEGAL_URL": "http://legal.example.com:8420"},
    {"PORTAL_LEGAL_URL": "ftp://legal.railway.internal"}, {"PORTAL_TRUSTED_PROXY_HOPS": "9"},
    {"PORTAL_RATE_IP_PER_MINUTE": "0"}, {"PORTAL_DISPLAY_TZ": "Mars/Base"}, {"PORTAL_PORT": "-1"},
    {"PORTAL_RATE_LEGAL_PER_MINUTE": "0"}, {"PORTAL_CLIENT_IP_HEADER": "x-forwarded-for"},
    {"PORTAL_CLIENT_IP_HEADER": "bad header!"},
])
def test_a_bad_configuration_refuses_to_start(over):
    env = {k: v for k, v in {**ENV, **over}.items() if v is not None}
    with pytest.raises(RuntimeError) as exc:
        config_mod.load(env)
    for secret in (ENV["PORTAL_LEGAL_SERVICE_TOKEN"], ENV["PORTAL_EVIDENCE_KEY"], ENV["PORTAL_LEGAL_CALLER_TOKEN"]):
        assert secret not in str(exc.value)                       # the message names the variable, never a value


def test_defaults_and_secret_reprs():
    s = config_mod.load(dict(ENV))
    assert (s.bind_addr, s.port, s.trusted_proxy_hops, s.form_max_age_s) == ("127.0.0.1", 8480, 1, 4 * 3600)
    assert s.public_origin == "https://accept.zbm.example" and len(s.key_id) == 16
    assert ENV["PORTAL_EVIDENCE_KEY"] not in repr(s) and ENV["PORTAL_LEGAL_SERVICE_TOKEN"] not in repr(s)
    assert config_mod.load({**ENV, "PORTAL_PUBLIC_ORIGIN": "http://127.0.0.1:9"}).public_origin == "http://127.0.0.1:9"
    # AEGIS L-7: an explicit default port is the same origin a browser sends (no port)
    assert config_mod.load({**ENV, "PORTAL_PUBLIC_ORIGIN": "https://Accept.ZBM.example:443"}).public_origin == \
        "https://accept.zbm.example"
    assert (s.legal_per_minute, s.client_ip_header) == (300, "x-real-ip")


def good_view(**over):
    v = {"invitation_id": "lg-cin-" + "A" * 26, "page": "first_engagement", "expires_at": "2026-10-15T17:00:00Z",
         "display": {"client_legal_name": "Acme LLC", "notice_email": "a@b.example", "contact_first_name": None,
                     "sow_number": "S-1", "sow_title": "T"},
         "documents": [{"doc_id": "client_msa", "version": "1.0", "doc_sha256": sha(MSA_TEXT), "text": MSA_TEXT,
                        "title": "Client MSA"},
                       {"doc_id": "sow", "version": "1.1", "doc_sha256": sha(SOW_TEXT), "text": SOW_TEXT}],
         "msa_acceptance": None}
    v.update(over)
    return v


def test_a_good_view_parses():
    v = parse_view(good_view())
    assert v.doc("msa").text == MSA_TEXT and v.doc("sow").version == "1.1" and v.display.sow_number == "S-1"


@pytest.mark.parametrize("bad", [
    {"page": "other"}, {"documents": []}, {"invitation_id": "x"}, {"expires_at": "tomorrow"}, {"display": None},
    {"documents": [{"doc_id": "client_msa", "version": "1.0", "doc_sha256": "0" * 64, "text": MSA_TEXT}]},
    {"documents": [{"doc_id": "dpa", "version": "1.0", "doc_sha256": sha(MSA_TEXT), "text": MSA_TEXT}]},
    {"documents": [{"doc_id": "client_msa", "version": "1.0", "doc_sha256": sha(MSA_TEXT), "text": MSA_TEXT}] * 2},
    {"msa_acceptance": {"version": "x"}},
])
def test_a_view_this_adapter_does_not_accept_is_legal_unavailable(bad):
    with pytest.raises(LegalUnavailable):
        parse_view(good_view(**bad))


def _client(status, payload, seen=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        return httpx.Response(status, json=payload)
    return LegalClient("http://legal.railway.internal:8420", "s" * 40, "c" * 40,
                       transport=httpx.MockTransport(handler))


def test_the_adapter_sends_the_token_in_the_body_with_the_portal_caller_token():
    seen = []
    assert _client(404, {"detail": "not found"}, seen).resolve("T" * 43) == Gone(final=False)
    req = seen[0]
    assert req.url.path == "/legal/v1/portal/invitations/resolve" and "T" * 43 not in str(req.url)
    assert json.loads(req.content) == {"token": "T" * 43}
    assert req.headers["authorization"] == "Bearer " + "s" * 40 and req.headers["x-legal-caller-token"] == "c" * 40


@pytest.mark.parametrize("status", [401, 403, 500, 503, 302])
def test_any_other_answer_is_unavailable(status):
    with pytest.raises(LegalUnavailable):
        _client(status, {}).resolve("T" * 43)


def test_unreachable_is_unavailable():
    def boom(req):
        raise httpx.ConnectError("no route")
    c = LegalClient("http://legal.railway.internal:8420", "s" * 40, "c" * 40, transport=httpx.MockTransport(boom))
    with pytest.raises(LegalUnavailable):
        c.resolve("T" * 43)


BODY = {"documents": [{"doc_id": "client_msa", "version": "1.0", "doc_sha256": "a" * 64}]}


def acc(**over):
    a = {"acceptance_id": "lg-acc-" + "B" * 26, "doc_id": "client_msa", "version": "1.0", "doc_sha256": "a" * 64,
         "accepted_at": "2026-10-01T17:00:00Z", "evidence_sufficient": True, "via": "client_portal",
         "basis_in_force": True}
    a.update(over)
    return a


def test_an_acceptance_answer_parses_only_when_every_record_counts_and_matches():
    assert parse_accepted({"acceptances": [acc()]}, BODY).kind == "accepted"
    for bad in ({"evidence_sufficient": False}, {"via": None}, {"basis_in_force": False}, {"doc_sha256": "b" * 64},
                {"acceptance_id": "nope"}, {"doc_id": "dpa"}):
        with pytest.raises(LegalUnavailable):
            parse_accepted({"acceptances": [acc(**bad)]}, BODY)
    with pytest.raises(LegalUnavailable):
        parse_accepted({"acceptances": []}, BODY)


def test_refusals_and_404_from_legal():
    assert _client(404, {"detail": "not found"}).accept(BODY).kind == "not_found"
    out = _client(409, {"detail": "X", "reasons": [{"code": "PRESENTED_TEXT_MISMATCH"}]}).accept(BODY)
    assert out.kind == "refused" and out.codes == ("PRESENTED_TEXT_MISMATCH",)


def _blocks_view(**over):
    import helpers as H
    v = H.view()
    d = good_view(documents=[{"doc_id": x.doc_id, "version": x.version, "doc_sha256": x.doc_sha256, "text": x.text,
                              "label": x.label} for x in v.documents],
                  display={"client_legal_name": "Acme LLC", "notice_email": "legal@acme.example",
                           "contact_first_name": "Pat", "sow_number": "SOW-2026-001",
                           "sow_title": "SEO and AI Visibility Audit"},
                  blocks=[dict(b) for b in v.blocks], page_sha256=v.page_sha256)
    d.update(over)
    return d


def test_blocks_parse_and_their_text_must_hash_to_legals_page_hash():
    v = parse_view(_blocks_view())
    assert v.blocks and v.page_sha256 and "Review and accept your agreement" in v.page_text
    with pytest.raises(LegalUnavailable):
        parse_view(_blocks_view(page_sha256="0" * 64))
    bad = _blocks_view()
    next(b for b in bad["blocks"] if b["k"] == "doc_text")["text"] = "not the document"
    with pytest.raises(LegalUnavailable):
        parse_view(bad)
    bad = _blocks_view()
    bad["blocks"].append({"k": "script", "text": "x"})
    with pytest.raises(LegalUnavailable):
        parse_view(bad)


def test_a_receipt_parses_only_counted_acceptances_of_its_documents():
    from legal_client import parse_receipt
    d = good_view()
    rec = {"invitation_id": d["invitation_id"], "accepted_at": "2026-10-01T17:05:00Z", "display": d["display"],
           "documents": d["documents"],
           "acceptances": [acc(doc_id=x["doc_id"], version=x["version"], doc_sha256=x["doc_sha256"],
                               acceptance_id="lg-acc-" + str(i) * 26) for i, x in enumerate(d["documents"], 1)]}
    assert len(parse_receipt(rec).acceptances) == 2
    rec["acceptances"][0]["via"] = None
    with pytest.raises(LegalUnavailable):
        parse_receipt(rec)
