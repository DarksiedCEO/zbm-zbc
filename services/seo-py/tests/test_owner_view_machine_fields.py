"""
Owner decision 19:20 PT "Show machine fields" (card C2-OV): in the owner's view of their own vaulted reports, fields
our code writes (report_fields.REPORT_FIELDS) are shown as typed machine values; free text goes through the CV. Both
directions per field family: typed + on-grammar -> shown; the same value in a free-text field, a typed field off its
grammar, an unlisted path and an unlisted document kind -> the CV (masked). Synthetic values only.
"""

import hashlib

import pytest

import report_fields as rf
from agents import logs as logs_mod
import sink_fields as sf
from svc_vaulted import owner_view

MASK = "[hidden: looks like a credential]"
HEX = hashlib.sha256(b"synthetic c2-ov").hexdigest()
FAKE_KEY = "AKIA" + "QZXW" * 4                      # synthetic K-1 shape, never a real key


def audit(**over):
    eng = {"model_versions": [over.get("mv", "perplexity/sonar@fast@2026-10-16.0")],
           "prompts": [{"answers_sha256": [over.get("h", HEX)], "prompt": {"sha256": HEX, "text": over.get("pt", "q")}}]}
    return {"agents": [{"facts": {"engines": {"perplexity": eng},
                                  "robots": {"text_sha256": HEX,
                                             "fetch": {"content_type": over.get("ct", "text/plain; charset=utf-8")}}},
                        "methodology": over.get("free", "plain words")}],
            "pages": {"https://site.test/": {"fingerprint": {"text": over.get("fp", HEX), "title": HEX}}},
            "limitations": [over.get("free", "plain words")]}


def log(t):
    return {"envelope": {"facts": {"families": {"Googlebot": {"top_path_templates": [{"template": t}]}},
                                   "uncrawled_important_paths": [t]},
                         "limitations": [t]}}


def test_the_table_is_closed_separate_from_sink_fields_and_has_no_free_text_type():
    assert set(rf.REPORT_FIELDS.values()) <= rf.TYPES and "text" not in rf.TYPES
    assert rf.DOC_KINDS == {rf.AUDIT_REPORT, rf.DRIFT_REPORT, rf.LOG_REPORT}
    assert not set(rf.REPORT_FIELDS) & set(sf.SINK_FIELDS)
    assert not {doc for doc, _ in rf.REPORT_FIELDS} & {k[0] for k in sf.SINK_FIELDS}


# ------------------------------------------------------------------------------------------------ hash family

def test_typed_hashes_are_shown():
    v = owner_view(audit(), doc=rf.AUDIT_REPORT)
    eng = v["agents"][0]["facts"]["engines"]["perplexity"]
    assert eng["prompts"][0]["answers_sha256"] == [HEX] and eng["prompts"][0]["prompt"]["sha256"] == HEX
    assert v["agents"][0]["facts"]["robots"]["text_sha256"] == HEX
    assert v["pages"]["https://site.test/"]["fingerprint"] == {"text": HEX, "title": HEX}


def test_the_same_hash_in_free_text_is_masked():
    v = owner_view(audit(free=HEX, pt=HEX), doc=rf.AUDIT_REPORT)
    assert v["limitations"] == [MASK] and v["agents"][0]["methodology"] == MASK
    assert v["agents"][0]["facts"]["engines"]["perplexity"]["prompts"][0]["prompt"]["text"] == MASK
    v = owner_view(audit(free=f"the value {HEX} here"), doc=rf.AUDIT_REPORT)
    assert HEX not in v["limitations"][0] and MASK in v["limitations"][0]


@pytest.mark.parametrize("bad", [FAKE_KEY, HEX.upper(), HEX + "0", f"{HEX} x"])
def test_a_typed_hash_field_off_its_grammar_falls_back_to_the_cv(bad):
    v = owner_view(audit(h=bad, fp=bad), doc=rf.AUDIT_REPORT)
    assert v["agents"][0]["facts"]["engines"]["perplexity"]["prompts"][0]["answers_sha256"][0] != bad
    assert v["pages"]["https://site.test/"]["fingerprint"]["text"] != bad


def test_no_document_kind_and_an_unlisted_kind_take_the_cv():
    for doc in (None, "seo_log", "unknown_doc"):
        v = owner_view(audit(), doc=doc)
        assert v["agents"][0]["facts"]["robots"]["text_sha256"] == MASK


# ------------------------------------------------------------------------------------------------ template family

@pytest.mark.parametrize("t", ["/about", "/", "/orders/{number}", "/reset-password/{token}", "/users/{email}/profile",
                               "/a/{uuid}", "/blog/how-to-rank", "/catalog/v2",
                               # lead ruling 19:50 PT OV-1 (card C2-OV2): every placeholder template_path writes
                               "/orders/{id}", "/f/{file}.pdf", "/img/{file}.jpeg", "/x/{file}.mp3"])
def test_typed_templates_are_shown_and_stay_masked_as_free_text(t):
    v = owner_view(log(t), doc=rf.LOG_REPORT)
    assert v["envelope"]["facts"]["families"]["Googlebot"]["top_path_templates"][0]["template"] == t
    plain = owner_view(t)
    assert v["envelope"]["limitations"] == [plain] and v["envelope"]["facts"]["uncrawled_important_paths"] == [plain]


@pytest.mark.parametrize("t", [f"/x/{FAKE_KEY}/{{token}}", "/a/{secret}", "/a/{token}x", "/a/{ID}", "/a/{id}x",
                               "/f/{file}.PDF", "/f/{file}.abcdef", "/f/{file}.", "/f/{file}", "/f/{file}.p-f",
                               "/f/{file}.p f", "/f/x{file}.pdf", "/f/{file}.pdf.gz", "/f/{files}.pdf",
                               "orders/{number}", "/a b/{token}", "/" + "a" * 41 + "/{token}"])
def test_a_template_off_its_grammar_or_with_a_valueish_literal_takes_the_cv(t):
    v = owner_view(log(t), doc=rf.LOG_REPORT)
    assert v["envelope"]["facts"]["families"]["Googlebot"]["top_path_templates"][0]["template"] == owner_view(t)
    if FAKE_KEY in t:
        assert FAKE_KEY not in str(v)



def test_the_template_placeholders_are_read_from_the_template_code():
    # lead ruling 19:50 PT OV-1: the grammar's set IS agents/logs' closed constant, not a copy
    assert rf._PLACEHOLDERS is logs_mod.PLACEHOLDERS
    assert logs_mod.PLACEHOLDERS == {"{email}", "{uuid}", "{number}", "{id}", "{token}"}
    assert logs_mod.FILE_PLACEHOLDER == "{file}." and logs_mod.FILE_EXT.pattern == "[a-z0-9]{1,5}"


@pytest.mark.parametrize("raw", ["/orders/12345", "/u/jane.doe@example.test/x", "/f/Report.PDF",
                                 "/a/123e4567-e89b-42d3-a456-426614174000", "/p/555-123-4567", "/k/" + "ab12" * 6,
                                 "/blog/how-to-rank", "/static/app.css", "/img/photo.jpeg"])
def test_what_template_path_writes_is_shown_on_the_typed_path_and_masked_nowhere_else(raw):
    t = logs_mod.template_path(raw)
    v = owner_view(log(t), doc=rf.LOG_REPORT)
    assert v["envelope"]["facts"]["families"]["Googlebot"]["top_path_templates"][0]["template"] == t
    assert v["envelope"]["limitations"] == [owner_view(t)]
    assert owner_view(log(t))["envelope"]["facts"]["families"]["Googlebot"]["top_path_templates"][0]["template"] \
        == owner_view(t)

# ------------------------------------------------------------------------------------------------ content type

def test_typed_content_types_are_shown_and_masked_as_free_text():
    ct = "text/plain; charset=utf-8"
    v = owner_view(audit(free=ct), doc=rf.AUDIT_REPORT)
    assert v["agents"][0]["facts"]["robots"]["fetch"]["content_type"] == ct
    assert v["limitations"] == [owner_view(ct)] != [ct]


@pytest.mark.parametrize("ct", [f"text/html; charset={FAKE_KEY}", "".join(("text/html; token=", "Ab12Cd", "34Ef56")), "text", "a/b/c",
                                "text/html; charset", "text/html charset=utf-8"])
def test_a_content_type_off_its_grammar_takes_the_cv(ct):
    v = owner_view(audit(ct=ct), doc=rf.AUDIT_REPORT)
    assert v["agents"][0]["facts"]["robots"]["fetch"]["content_type"] == owner_view(ct)
    assert FAKE_KEY not in str(v)


# ------------------------------------------------------------------------------------------------ model version

def test_typed_model_versions_are_shown_and_masked_as_free_text():
    mv = "perplexity/sonar@fast@2026-10-16.0"
    v = owner_view(audit(free=mv), doc=rf.AUDIT_REPORT)
    assert v["agents"][0]["facts"]["engines"]["perplexity"]["model_versions"] == [mv]
    assert v["limitations"] == [MASK]
    d = owner_view({"changes": [{"evidence": {"models_before": [mv], "models_after": ["sonar@?"]}}]},
                   doc=rf.DRIFT_REPORT)
    assert d["changes"][0]["evidence"] == {"models_before": [mv], "models_after": ["sonar@?"]}


@pytest.mark.parametrize("mv", [f"perplexity/{FAKE_KEY}@1", f"sonar@{FAKE_KEY}", "sonar", "a b@1", "a@b@c@d@e",
                                "sonar@fast<x>"])
def test_a_model_version_off_its_grammar_takes_the_cv(mv):
    v = owner_view(audit(mv=mv), doc=rf.AUDIT_REPORT)
    assert v["agents"][0]["facts"]["engines"]["perplexity"]["model_versions"] == [owner_view(mv)]
    assert FAKE_KEY not in str(v)


def test_a_typed_path_only_matches_its_own_shape():
    assert rf.field_type(rf.AUDIT_REPORT, ("pages", "https://site.test/", "fingerprint", "text")) == "hash"
    assert rf.field_type(rf.AUDIT_REPORT, ("pages", "[]", "fingerprint", "text")) is None
    assert rf.field_type(rf.AUDIT_REPORT, ("pages", "x", "fingerprint")) is None
    assert rf.field_type(rf.LOG_REPORT, ("pages", "x", "fingerprint", "text")) is None
