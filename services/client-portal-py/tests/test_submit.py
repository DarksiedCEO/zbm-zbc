"""ADR 0028: POST /accept/{token}: CSRF, validation, what reaches Legal, idempotency, confirmation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

import render as V
from helpers import (ENV, MSA_TEXT, ORIGIN, SOW_TEXT, TOKEN, FakeLegal, call, form_post, hidden, make, sha, view)
from legal_client import Document


def test_a_valid_submission_records_through_legal_and_confirms():
    app, legal, *_ = make()
    r = form_post(app)
    assert r.status == 200, r.text[:800]
    assert "Thank you. Your acceptance is recorded." in r.text
    for n, (doc_id, text) in enumerate((("client_msa", MSA_TEXT), ("sow_seo_audit", SOW_TEXT)), start=1):
        assert f"SHA-256 fingerprint: {sha(text)}" in r.text
        assert f"Acceptance reference: lg-acc-{str(n) * 26}" in r.text
    assert "(Pacific Time) by Patricia Q. Example, Managing Member" in r.text
    # nothing claims an email was sent: no mailer is wired
    assert "We have emailed" not in r.text and "Z Best Media will email a copy of each document to legal@acme.example" \
        in r.text
    body = legal.accepts[0]
    assert body["token"] == TOKEN and body["presentation"] == "inline"
    assert [(d["doc_id"], d["version"], d["doc_sha256"], d["presented_sha256"], d["affirmative_act"])
            for d in body["documents"]] == [("client_msa", "1.0", sha(MSA_TEXT), sha(MSA_TEXT), True),
                                            ("sow_seo_audit", "1.1", sha(SOW_TEXT), sha(SOW_TEXT), True)]
    page = base64.b64decode(body["page_text_b64"])
    assert hashlib.sha256(page).hexdigest() == body["page_sha256"]
    assert page.decode() == V.acceptance_page(view(), TOKEN, 0, "", None).text
    assert body["signatory"] == {"full_name": "Patricia Q. Example", "title": "Managing Member", "company": "Acme LLC"}


def test_legal_never_receives_the_ip_address_or_the_user_agent():
    app, legal, *_ = make()
    form_post(app, headers={"user-agent": "Mozilla/5.0 SecretAgent"}, client=("203.0.113.42", 1))
    sent = json.dumps(legal.accepts[0])
    assert "203.0.113.42" not in sent and "SecretAgent" not in sent
    key = ENV["PORTAL_EVIDENCE_KEY"].encode()
    want_ip = hmac.new(key, "ip-v1\x1f203.0.113.42".encode(), hashlib.sha256).hexdigest()
    want_ua = hmac.new(key, "ua-v1\x1fMozilla/5.0 SecretAgent".encode(), hashlib.sha256).hexdigest()
    assert legal.accepts[0]["ip_hmac_sha256"] == want_ip and legal.accepts[0]["user_agent_hmac_sha256"] == want_ua
    assert legal.accepts[0]["hmac_key_id"] == hashlib.sha256(key).hexdigest()[:16]


@pytest.mark.parametrize("headers,code", [
    ({"origin": None}, 403),
    ({"origin": "https://evil.example"}, 403),
    ({"origin": "https://accept.zbm.example.evil.example"}, 403),
    ({"origin": "null"}, 403),
    ({"sec-fetch-site": "cross-site"}, 403),
    ({"sec-fetch-site": "same-site"}, 403),
    ({"content-type": "text/plain"}, 415),
    ({"content-type": "multipart/form-data; boundary=x"}, 415),
    ({"content-type": "application/json"}, 415),
])
def test_csrf_and_content_type_are_refused_before_anything_is_recorded(headers, code):
    app, legal, *_ = make()
    r = form_post(app, headers=headers)
    assert r.status == code and "Nothing was recorded" in r.text
    assert legal.accepts == []


def test_a_missing_sec_fetch_site_is_accepted_with_a_matching_origin():
    app, legal, *_ = make()
    assert form_post(app, headers={"sec-fetch-site": None}).status == 200


@pytest.mark.parametrize("change", ["sig", "issued", "page_sha256", "drop_sig", "token_swap"])
def test_a_forged_or_replayed_form_is_refused(change):
    other = "O" * 43
    app, legal, *_ = make(FakeLegal(views={TOKEN: view(), other: view()}))
    page = call(app, "GET", f"/accept/{TOKEN}")
    h = hidden(page.text)
    fields = {"sig": {"form_sig": "0" * 64}, "issued": {"issued": str(int(h["issued"]) - 1)},
              "page_sha256": {"page_sha256": "1" * 64}}.get(change, {})
    if change == "token_swap":                    # a form signed for one link is not valid for another
        r = form_post(app, token=other, get=page)
    else:
        r = form_post(app, fields=fields, drop=("form_sig",) if change == "drop_sig" else (), get=page)
    assert r.status == 403 and legal.accepts == []


def test_a_form_older_than_the_limit_or_from_the_future_is_refused():
    app, legal, clock, _ = make()
    page = call(app, "GET", f"/accept/{TOKEN}")
    clock.t += 4 * 3600 + 1
    assert form_post(app, get=page).status == 403
    app2, legal2, clock2, _ = make()
    page2 = call(app2, "GET", f"/accept/{TOKEN}")
    clock2.t -= 120
    assert form_post(app2, get=page2).status == 403
    assert legal.accepts == [] and legal2.accepts == []


@pytest.mark.parametrize("fields,drop,errors", [
    ({}, ("accept_doc_1",), ["accept_doc_1"]),
    ({}, ("accept_doc_2",), ["accept_doc_2"]),
    ({"accept_doc_1": "on"}, (), ["accept_doc_1"]),
    ({"full_name": "  "}, (), ["full_name"]),
    ({"title": ""}, (), ["title"]),
    ({"company": "Globex Corp"}, (), ["company"]),
    ({"full_name": "x" * 121}, (), ["full_name"]),
    ({"title": "CEO Evil"}, (), ["title"]),
    ({}, ("accept_doc_1", "accept_doc_2", "full_name"), ["accept_doc_1", "accept_doc_2", "full_name"]),
])
def test_an_incomplete_form_comes_back_with_accessible_errors_and_nothing_recorded(fields, drop, errors):
    app, legal, *_ = make()
    r = form_post(app, fields=fields, drop=drop)
    assert r.status == 422 and legal.accepts == []
    assert 'role="alert"' in r.text and "There is a problem" in r.text
    for f in errors:
        assert f'href="#{f}"' in r.text and f'id="{f}-error"' in r.text
        assert f'aria-describedby="{f}-error"' in r.text or f'aria-describedby="{f}-hint {f}-error"' in r.text
    assert len([x for x in r.text.split('<input type="checkbox"')[1:] if "checked" in x.split(">")[0]]) == 0
    # the page text is unchanged by the errors: the same hash is signed again
    assert hidden(r.text)["page_sha256"] == hashlib.sha256(
        V.acceptance_page(view(), TOKEN, 0, "", None).text.encode()).hexdigest()


def test_the_typed_values_come_back_escaped():
    app, legal, *_ = make()
    r = form_post(app, fields={"full_name": '"><script>x</script>', "company": "Nope"})
    assert r.status == 422 and "<script>x" not in r.text and "&quot;&gt;&lt;script&gt;" in r.text


def test_company_matching_ignores_case_spaces_and_punctuation():
    app, legal, *_ = make()
    assert form_post(app, fields={"company": "Acme Inc"}).status == 422
    app2, legal2, *_ = make()
    assert form_post(app2, fields={"company": "  acme, l.l.c. "}).status == 200


def test_documents_changed_between_the_page_and_the_submission():
    legal = FakeLegal(views={TOKEN: view()})
    app, *_ = make(legal)
    page = call(app, "GET", f"/accept/{TOKEN}")
    changed = SOW_TEXT.replace("1,500.00", "2,500.00")
    legal.views[TOKEN] = view(docs=[Document("client_msa", "1.0", sha(MSA_TEXT), MSA_TEXT),
                                    Document("sow_seo_audit", "1.2", sha(changed), changed)])
    r = form_post(app, get=page)
    assert r.status == 409 and "The documents changed after you opened this page" in r.text
    assert legal.accepts == []


def test_a_refresh_or_a_lost_response_shows_the_confirmation_again():
    """AEGIS M-2: the same form submitted again after it was accepted (the response was lost, or the client
    refreshed the confirmation) gets the confirmation from Legal's receipt; nothing is recorded twice."""
    app, legal, *_ = make()
    page = call(app, "GET", f"/accept/{TOKEN}")
    first = form_post(app, get=page)
    assert first.status == 200
    assert call(app, "GET", f"/accept/{TOKEN}").status == 404          # the page itself is gone once used
    again = form_post(app, get=page)
    assert again.status == 200 and "Thank you. Your acceptance is recorded." in again.text
    assert "Acceptance reference: lg-acc-" + "1" * 26 in again.text and len(legal.accepts) == 1
    assert legal.receipts[-1][1] == legal.accepts[0]["request_id"]


def test_a_different_submission_on_a_used_link_is_the_404():
    app, legal, *_ = make()
    page = call(app, "GET", f"/accept/{TOKEN}")
    assert form_post(app, get=page).status == 200
    r = form_post(app, get=page, fields={"title": "CEO"})               # someone else's form for the same link
    assert r.status == 404 and "Thank you" not in r.text and len(legal.accepts) == 1


def test_a_double_submit_racing_the_first_gets_the_receipt():
    """Legal answered the accept with 404 because the first submission used the link a moment earlier."""
    from legal_client import AcceptOutcome
    app, legal, *_ = make()
    page = call(app, "GET", f"/accept/{TOKEN}")
    form_post(app, get=page)
    legal.used.discard(TOKEN)                                         # the second resolve ran before the first commit
    orig = legal.accept

    def late(body):
        legal.used.add(TOKEN)
        return AcceptOutcome("not_found")
    legal.accept = late
    r = form_post(app, get=page)
    legal.accept = orig
    assert r.status == 200 and "Thank you. Your acceptance is recorded." in r.text


@pytest.mark.parametrize("name", ["Pat\u202e CLL emcA", "Pat\u200bQ", "Pat\u2066x\u2069", "Pat\u00adQ"])
def test_bidi_and_invisible_characters_are_refused_before_legal(name):
    app, legal, *_ = make()
    r = form_post(app, fields={"full_name": name})
    assert r.status == 422 and legal.accepts == [] and 'href="#full_name"' in r.text


def test_typed_fields_are_nfkc_normalized():
    app, legal, *_ = make()
    assert form_post(app, fields={"full_name": "Ｐａｔ Example", "company": "Ａｃｍｅ LLC"}).status == 200
    assert legal.accepts[0]["signatory"]["full_name"] == "Pat Example"


def test_the_same_submission_always_has_the_same_request_id():
    a1, l1, *_ = make()
    a2, l2, *_ = make()
    form_post(a1)
    form_post(a2)
    assert l1.accepts[0]["request_id"] == l2.accepts[0]["request_id"]
    a3, l3, *_ = make()
    form_post(a3, fields={"title": "Owner"})
    assert l3.accepts[0]["request_id"] != l1.accepts[0]["request_id"]


@pytest.mark.parametrize("kind,code", [("not_found", 404), ("refused", 409)])
def test_legal_refusing_the_submission_records_nothing_and_says_so(kind, code):
    legal = FakeLegal(views={TOKEN: view()}, accept_kind=kind)
    app, *_ = make(legal)
    r = form_post(app)
    assert r.status == code and "Thank you" not in r.text


def test_legal_down_at_submission_is_503():
    legal = FakeLegal(views={TOKEN: view()})
    app, *_ = make(legal)
    page = call(app, "GET", f"/accept/{TOKEN}")
    legal.unavailable = True
    r = form_post(app, get=page)
    assert r.status == 503 and "Thank you" not in r.text


def test_body_limits_and_duplicate_fields():
    app, legal, *_ = make()
    h = {"content-type": "application/x-www-form-urlencoded", "origin": ORIGIN}
    big = b"full_name=" + b"x" * 9000
    assert call(app, "POST", f"/accept/{TOKEN}", {**h, "content-length": str(len(big))}, big).status == 413
    assert call(app, "POST", f"/accept/{TOKEN}", h, chunks=[b"a=" + b"x" * 5000, b"b=" + b"y" * 5000]).status == 413
    assert call(app, "POST", f"/accept/{TOKEN}", {**h, "content-length": "abc"}, b"").status == 413
    page = call(app, "GET", f"/accept/{TOKEN}")
    data = list(hidden(page.text).items()) + [("full_name", "A"), ("full_name", "B"), ("title", "t"),
                                              ("company", "Acme LLC"), ("accept_doc_1", "yes"), ("accept_doc_2", "yes")]
    assert call(app, "POST", f"/accept/{TOKEN}", h, urlencode(data).encode()).status == 400
    assert call(app, "POST", f"/accept/{TOKEN}", h, b"\xff\xfe=1").status == 400
    assert legal.accepts == []


def test_a_wired_mailer_gets_part_d_and_the_page_says_it_was_sent():
    sent = []

    class Mailer:
        def send(self, message):
            sent.append(message)
            return True
    app, legal, *_ = make(mailer=Mailer(), mail_postal_line="ZBM Example Co., 1 Example Way, Long Beach, California",
                          mail_next_step="we schedule the kickoff call.")
    r = form_post(app)
    assert r.status == 200 and "We have emailed a copy of each document to legal@acme.example." in r.text
    m = sent[0]
    assert m.to == "legal@acme.example" and m.subject == "Your agreement with Z Best Media: copy for your records"
    assert m.idempotency_key == view().invitation_id
    assert m.body.startswith("Hi Patricia,\n\nThank you. This email confirms what you accepted for Acme LLC.")
    assert [a.content for a in m.attachments] == [MSA_TEXT.encode(), SOW_TEXT.encode()]
    assert all(hashlib.sha256(a.content).hexdigest() in m.body for a in m.attachments)


def test_a_failing_mailer_never_undoes_the_recorded_acceptance():
    class Broken:
        def send(self, message):
            raise OSError("smtp down")
    app, legal, *_ = make(mailer=Broken(), mail_postal_line="x", mail_next_step="y")
    r = form_post(app)
    assert r.status == 200 and "Z Best Media will email a copy" in r.text


def test_a_mailer_without_the_part_d_facts_refuses_to_build():
    class M:
        def send(self, message):
            return True
    with pytest.raises(RuntimeError):
        make(mailer=M())


def test_a_submission_never_logs_the_signatory(caplog):
    app, *_ = make()
    with caplog.at_level("INFO", logger="client_portal"):
        form_post(app)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "Patricia" not in logged and "Managing Member" not in logged and TOKEN not in logged
    assert "accept_post 200" in logged
