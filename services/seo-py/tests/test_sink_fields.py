"""core-spec/2 T-1, T-2, T-3, T-5 (enforcement half): the frozen SINK_FIELDS table and its per-field type check.

Synthetic values only: every sample is built at run time from random hex / base32 (no literal secret, no real id).
Paths and reasons are compared, values are never printed."""
import re
import secrets
import uuid

import pytest

import reasons
import sink_fields as sf
import sink_guard as sg
from primitives import codes

_CROCK = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_B32 = "abcdefghijklmnopqrstuvwxyz234567"


def _hex(n: int) -> str:
    while True:
        v = secrets.token_hex(n // 2 + 1)[:n]
        if v.strip("0123456789"):              # at least one a-f letter (a hash, not a digit run)
            return v


def _pick(alphabet: str, n: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _samples() -> dict:
    """One synthetic sample per T-1 class that can be built without process configuration."""
    out = {
        "empty": "",
        "service_id": "seo-aud-" + _hex(40),
        "scoped_id": "audit:seo-aud-" + _hex(40),
        "invoice_id": "fin-inv-" + _pick(_CROCK, 26),
        "finance_id": "fin-pay-" + _pick(_CROCK, 26),
        "ruling_id": "cmp-rul-" + _pick(_B32, 26),
        "jurisdiction": "US-CA",
        "request_id": str(uuid.uuid4()),
        "amount": "12.500000",
        "request_key": "audit_requested|example.org|" + str(uuid.uuid4()),
        "hash": _hex(32),
        "timestamp": "2026-11-14T09:50:00Z",
        "date": "2026-11-14",
        "number": "1234",
        "version": "1.2.3",
        "boolean": "true",
        "actor": sorted(sg.ACTORS)[0],
        "hostname": "example.org",
        "code": "status_ok",
    }
    if sg.MODEL_IDS:
        out["model_id"] = sorted(sg.MODEL_IDS)[0]
    return out


SAMPLES = _samples()


def test_samples_are_what_they_claim():
    assert {c: sg.classify(v) for c, v in SAMPLES.items()} == {c: c for c in SAMPLES}


# ---------------------------------------------------------------------------------------------------- T-1 / T-3
def test_t1_closed_type_list_is_the_21_v1_classes():
    assert sf.TYPES == sg.CLASSES and len(sf.TYPES) == 21
    assert not {"string", "any", "text", "free_text"} & set(sf.TYPES)


def test_t3_table_is_frozen_and_typed_by_the_closed_list_only():
    with pytest.raises(TypeError):
        sf.SINK_FIELDS[("seo_log", "x", "/x")] = sf.F("code")       # type: ignore[index]
    for (sink, kind, path), field in sf.SINK_FIELDS.items():
        # C2-3a (lead ruling F3-1): a `logging` record kind is the logger name, dotted (seo.audits)
        name_ok = all(sg._is_name(p) for p in kind.split(".")) if sink == "logging" else sg._is_name(kind)
        # C2-3b re-pin (lead ruling F3-2): an `error` record kind is the reason code (reasons.CODES) or validation
        if sink == "error":
            # C2-14c re-pin (G-7, lead ruling 19:05 PT FCL-Q1; F6a-1 / F14b-1): the /health 503 body is the error
            # kind "health" (only /status typed; same closed set as test_error_body_typed CODES | {validation, health})
            name_ok = kind in reasons.CODES or kind in ("validation", "health")
            if kind == "health":
                assert path == "/status", path            # ruling 19:05: /status is its ONLY decided path
        # C2-3c2 re-pin (T-3 / lead ruling F3-4, G-7): an `outbound` record kind is the engine / protocol name of the
        # closed provider-host table (perplexity.sonar is dotted); nothing outside OUTBOUND_HOSTS
        # C2-3c4 re-pin (T-3 / F3-4 / C2-3c3, G-7): or a non-LLM client kind of OUTBOUND_CLIENT_HOSTS (gsc.query ...)
        if sink == "outbound":
            name_ok = ((kind in sf.OUTBOUND_HOSTS or kind in sf.OUTBOUND_CLIENT_HOSTS)
                       and all(sg._is_name(p) for p in kind.split(".")))
        assert sink in sf.SINKS and name_ok and path.startswith(("/", "[]"))
        assert field.types <= frozenset(sf.TYPES)                     # CL-7: a closed SET of T-1 types, nothing else
        assert field.types or field.nullable                          # a null-only field is declared nullable
        assert field.values is None or field.types == {"code"}        # an enum is a code with a closed constant set
        assert not field.ref or field.types == {"code"}
        assert not field.numeric or "number" in field.types           # CL-6: numeric widens number only
        if field.ref and (sink, kind, path) in sf.UNIONS:
            # C2-6h re-pin (G-7; OWNER DECISION 2026-11-15 02:00 PT (E-COMP), T-2): the finance_client_id keys keep
            # their C2-2f UNIONS entry (test_sink_api_ids counts 44) but hold the T-2 reference: single type by design
            assert (kind, path.rsplit("/", 1)[-1]) == ("tenant_finance_client_set", "finance_client_id"), (sink, kind, path)
            assert field.types == {"code"} and sf.UNIONS[(sink, kind, path)].startswith("C2-2f"), (sink, kind, path)
            continue
        assert (len(field.types) > 1) is ((sink, kind, path) in sf.UNIONS)   # CL-7: every union listed with a reason
    assert sum(1 for k, f in sf.SINK_FIELDS.items() if f.ref and k in sf.UNIONS) == 3   # C2-6h: exactly the 3 E-COMP keys


def test_t3_sink_of_a_kind():
    assert sf.sink_of("audit_requested") == ("seo_log", "audit_requested")
    assert sf.sink_of("ledger:log_anchor") == ("ledger", "log_anchor")


# ---------------------------------------------------------------------------------------------------------- T-2
@pytest.mark.parametrize("kind", codes.KINDS)
def test_t2_braceless_code_is_the_reference_form(kind):
    assert sf.is_ref(f"c:{kind}:{_hex(16)}")
    assert sf.matches(sf.F("code", ref=True), f"c:{kind}:{_hex(16)}")


def test_t2_braced_bare_and_malformed_references_are_refused():
    ref = sf.F("code", ref=True)
    h = _hex(16)
    bad = ["{c:text:" + h + "}",                       # Wave-1 D-02a: the braced form stays refused
           h,                                          # a bare 16-hex is not a reference
           "c:text:" + h.upper(), "c:text:" + h[:15], "c:text:" + h + "0", "c:ip:" + h, "c:nokind:" + h,
           "c:text:" + h + " ", "C:text:" + h, "c:text:" + h[:8] + "@" + h[9:]]
    assert [b for b in bad if sf.matches(ref, b)] == []
    assert not sf.matches(sf.F("code", ref=True), 7) and not sf.matches(sf.F("code", ref=True), None)


# ---------------------------------------------------------------------------------------------------------- T-5
_ENTRIES = sorted(sf.SINK_FIELDS.items())


@pytest.mark.parametrize("key,field", _ENTRIES, ids=["|".join(k) for k, _ in _ENTRIES])
def test_t5_each_table_field_accepts_its_grammar_and_refuses_every_other_class(key, field):
    sink, kind, path = key
    lk = sf.leaf_key(path)
    if field.grammar is not None:
        # C2-6h re-pin (G-7; lead ruling 01:20 PT (B), C2-6f): an entity-id field is checked by its EXACT code-base
        # grammar (ENTITY prefix + config.TENANT_ID) INSTEAD of the open-code shape rule, both directions: the exact
        # value is accepted; the open-code sample, the other entity form and near misses are refused
        assert key in sf.ENTITY_ID_FIELDS and field.types == {"code"} and not field.ref, key
        forms = ("ent-acme", "entity:ent-acme")
        own = [v for v in forms if field.grammar.fullmatch(v)]
        assert len(own) == 1 and sf.matches(field, own[0], key=lk), key
        for v in (SAMPLES["code"], *[f for f in forms if f != own[0]], "ent-", "ent-Acme", own[0] + " x",
                  "x" + own[0], own[0] + "@", own[0].replace("acme", "a")):
            assert not sf.matches(field, v, key=lk), key
    for cls in field.types:
        own = sorted(field.values)[0] if field.values else SAMPLES.get(cls)
        if own is not None and not field.ref and field.grammar is None:
            assert sf.matches(field, own, key=lk), (key, cls)
    for cls, sample in SAMPLES.items():
        if cls in field.types:
            continue
        assert not sf.matches(field, sample, key=lk), (key, cls)
    for prose in ("two words", "a@b", "{c:text:" + _hex(16) + "}"):    # no free-text type anywhere (CL-7)
        assert not sf.matches(field, prose, key=lk), key
    assert sf.matches(field, None) is field.nullable                  # CL-6: null only where declared nullable
    assert sf.matches(field, 0.25) is field.numeric                   # CL-6: finite float only where numeric
    for bad in (float("nan"), float("inf"), b"ab", ["x"]):
        assert not sf.matches(field, bad), key
    assert sf.matches(field, True) is ("boolean" in field.types and field.values is None and not field.ref)
    # F2b-4: all-digit fixed-length hex only in a field-scoped digit-hex field or under a global DIGIT_HEX_KEYS key
    assert sf.matches(field, "7" * 16, key=lk) is ("hash" in field.types and (field.digit_hex or lk in sg.DIGIT_HEX_KEYS))


@pytest.mark.parametrize("cls", sorted(SAMPLES))
def test_t5_a_field_of_one_class_refuses_the_others_and_prose(cls):
    field = sf.F(cls)
    for other, sample in SAMPLES.items():
        expect = other == cls or (cls == "code" and other in sf.CODE_GRAMMAR_OVERLAP)
        assert sf.matches(field, sample) is expect, (cls, other)
    for prose in ("two words", "a@b", "{c:text:" + _hex(16) + "}", " " + SAMPLES[cls], SAMPLES[cls] + "\n"):
        assert not sf.matches(field, prose), cls


def test_t5_enum_is_a_closed_constant_set():
    field = sf.F("code", values=frozenset(("queued", "done")))
    assert sf.matches(field, "done") and not sf.matches(field, "running") and not sf.matches(field, "Done")


def test_t5_untyped_path_and_wrong_type_are_refused_never_rewritten():
    k = next((k for k in sf.SINK_FIELDS if k[0] == "ledger"), None)
    assert k is not None
    kind = sf.LEDGER + k[1]
    assert ("/no_such_field", "untyped") in sf.field_violations(kind, {"no_such_field": "status_ok"})
    data = {k[2].strip("/"): "two words"}
    before = dict(data)
    assert sf.field_violations(kind, data) == [(k[2], "field_type")]
    assert data == before
    assert sf.field_violations("audit_requested_unknown_kind", {"a": "status_ok"}) == [("/a", "untyped")]


def test_t5_enforce_refuses_at_write_and_never_rewrites():
    """C2-R1 (F-B4): T-5 through the write entry itself (``sink_guard.enforce``), not via the T-4 recorder: a value
    off its field's grammar, an untyped path and a seo_log kind with no table row each raise ``SinkViolation``; the
    payload is left as written (never truncated or rewritten); a typed payload passes."""
    k = next((k for k in sf.SINK_FIELDS if k[0] == "ledger"), None)
    assert k is not None
    kind = sf.LEDGER + k[1]
    for knd, data in ((kind, {k[2].strip("/"): "two words"}), (kind, {"no_such_field": "status_ok"}),
                      ("seo_log_kind_with_no_table_row", {"a": "status_ok"})):
        before = dict(data)
        with pytest.raises(sg.SinkViolation):
            sg.enforce(knd, data)
        assert data == before
    assert sg.enforce("ledger:log_anchor", {"epoch": _hex(16), "seq": 3, "line_sha256": _hex(64)}) is None


def test_t5_log_anchor_payload_is_typed():
    ok = {"epoch": _hex(16), "seq": 3, "line_sha256": _hex(64)}
    assert sf.field_violations("ledger:log_anchor", ok) == []
    assert sf.field_violations("ledger:log_anchor", {**ok, "seq": "status_ok"}) == [("/seq", "field_type")]
    assert sf.field_violations("ledger:log_anchor", {**ok, "line_sha256": "c:text:" + _hex(16)}) == \
        [("/line_shaN", "field_type")]
    assert sf.field_violations("ledger:log_anchor", {**ok, "seq": 10 ** 9}) == [("/seq", "field_type")]


# ------------------------------------------------------------------------------- C2-2b: the frozen inventory table
def test_c22b_table_covers_both_sinks_and_every_writer_kind():
    kinds = {(k[0], k[1]) for k in sf.SINK_FIELDS}
    assert {s for s, _ in kinds} == set(sf.SINKS)
    assert len(sf.SINK_FIELDS) > 1000                                 # frozen from the C2-2b inventory, not a seed
    for kind in ("audit_requested", "citation_run_recorded", "job_ran", "tenant_created", "schedule_created"):
        assert ("seo_log", kind) in kinds, kind
    assert ("ledger", "log_anchor") in kinds and ("ledger", "audit_requested") in kinds


def test_cl6_numeric_and_nullable_lists_are_exactly_the_table_flags():
    assert sf.NUMERIC == frozenset(k for k, f in sf.SINK_FIELDS.items() if f.numeric)
    assert sf.NULLABLE == frozenset(k for k, f in sf.SINK_FIELDS.items() if f.nullable)
    assert sf.NUMERIC and sf.NULLABLE


def test_cl7_every_union_has_a_reason_and_no_free_text_member():
    assert sf.UNIONS
    for key, reason in sf.UNIONS.items():
        field = sf.SINK_FIELDS[key]
        if field.ref:   # C2-6h re-pin (G-7; E-COMP owner decision 02:00 PT, T-2): the 3 finance refs (see test_t3)
            assert key[1] == "tenant_finance_client_set" and key[2].endswith("/finance_client_id"), key
            assert field.types == {"code"} and isinstance(reason, str) and len(reason) > 20, key
            continue
        assert len(field.types) >= 2 and field.types <= frozenset(sf.TYPES)
        assert isinstance(reason, str) and len(reason) > 20


def test_cl7_union_field_accepts_each_member_only():
    field = sf.F("code", also=frozenset(("model_id",)))
    assert sf.matches(field, SAMPLES["code"]) and sf.matches(field, SAMPLES["model_id"])
    assert not sf.matches(field, SAMPLES["hostname"]) and not sf.matches(field, None)
    with pytest.raises(ValueError):
        sf.F("code", also=frozenset(("free_text",)))
    with pytest.raises(ValueError):
        sf.F("code", numeric=True)
    with pytest.raises(ValueError):
        sf.F(None)


def test_cl6_null_only_and_numeric_fields():
    assert sf.matches(sf.F(None, nullable=True), None) and not sf.matches(sf.F(None, nullable=True), "status_ok")
    num = sf.F("number", numeric=True)
    assert sf.matches(num, 0.5) and sf.matches(num, 3) and not sf.matches(num, float("inf"))
    assert not sf.matches(sf.F("number"), 0.5)


# ----------------------------------------------------------- C2-2b2: dynamic dict keys ({key}) + F2b-4 digit-only hex
def _key_paths() -> set:
    need = set()
    for sink, kind, path in sf.SINK_FIELDS:
        for m in re.finditer(r"/\*", path):
            need.add((sink, kind, path[:m.start()] + "/{key}"))
    return need


def test_c22b2_every_dynamic_dict_key_is_typed_or_listed_untyped():
    need = _key_paths()
    typed = {k for k in sf.SINK_FIELDS if k[2].endswith("/{key}")}
    untyped = set(sf.KEY_UNTYPED)
    assert typed | untyped == need and not typed & untyped
    assert (len(need), len(typed), len(untyped)) == (36, 24, 12)
    for key in typed:
        f = sf.SINK_FIELDS[key]
        assert f.types in ({"code"}, {"date"}) and not f.nullable and not f.numeric and not f.digit_hex, key
    for key, reason in sf.KEY_UNTYPED.items():
        assert key not in sf.SINK_FIELDS and isinstance(reason, str) and len(reason) > 20, key


def test_c22b2_key_types_follow_the_inventory():
    t = lambda *k: sf.SINK_FIELDS[k].types                              # noqa: E731
    assert t("seo_log", "engine_run_settled", "/by_day/{key}") == {"date"}
    assert t("ledger", "engine_run_settled", "/by_day/{key}") == {"date"}
    assert t("seo_log", "drift_recorded", "/facts/counts/{key}") == {"code"}
    assert t("ledger", "drift_recorded", "/counts/{key}") == {"code"}
    assert t("seo_log", "log_chunk", "/inline/families/*/status/{key}") == {"code"}
    assert ("seo_log", "audit_completed", "/report/agents[]/facts/fields/{key}") in sf.KEY_UNTYPED
    assert ("seo_log", "log_chunk", "/inline/families/*/templates/{key}") in sf.KEY_UNTYPED


def test_c22b2_field_violations_checks_each_dynamic_key_against_its_entry():
    assert sf.field_violations("engine_run_settled", {"by_day": {SAMPLES["date"]: {}}}) == []
    assert sf.field_violations("engine_run_settled", {"by_day": {"Two Words": {}}}) == [("/by_day/{key}", "field_type")]
    assert sf.field_violations("ledger:drift_recorded", {"counts": {SAMPLES["date"]: {}}}) == \
        [("/counts/{key}", "field_type")]
    data = {"report": {"agents": [{"facts": {"fields": {"Some Field": {}}}}]}}
    assert sf.field_violations("audit_completed", data) == [("/report/agents[]/facts/fields/{key}", "untyped")]


_F2B4 = (("seo_log", "audit_completed", "/report_hmac"), ("seo_log", "citation_run_recorded", "/result_hmac"),
         ("seo_log", "job_ran", "/job"))


def test_f2b4_digit_hex_fields_are_the_ruled_fields_each_with_a_reason():
    assert set(sf.DIGIT_HEX_FIELDS) == {k for k, f in sf.SINK_FIELDS.items() if f.digit_hex}
    assert set(_F2B4) <= set(sf.DIGIT_HEX_FIELDS)
    assert {sf.leaf_key(k[2]) for k in sf.DIGIT_HEX_FIELDS} == {"report_hmac", "result_hmac", "job"}
    for key, reason in sf.DIGIT_HEX_FIELDS.items():
        assert "hash" in sf.SINK_FIELDS[key].types and isinstance(reason, str) and len(reason) > 20, key
    assert sg.DIGIT_HEX_KEYS == frozenset(("epoch", "vault_id", "repair_id", "key_fp"))   # global rule unchanged
    with pytest.raises(ValueError):
        sf.F("code", digit_hex=True)


def test_f2b4_digit_hex_is_fixed_length_lowercase_hex_including_all_digits():
    f = sf.SINK_FIELDS[_F2B4[0]]
    for n in sorted(sg.HASH_LENGTHS):
        assert sf.matches(f, "7" * n) and sf.matches(f, _hex(n)) and not sf.matches(f, "A" * n), n
    for n in (15, 17, 31, 33, 39, 41, 63, 65):
        assert not sf.matches(f, "7" * n) and not sf.matches(f, _hex(n)), n
    assert not sf.matches(sf.F("hash"), "7" * 16)                       # field-scoped: a plain hash field still refuses
    assert sf.field_violations("audit_completed", {"report_hmac": "1" * 64}) == []
    assert sf.field_violations("citation_run_recorded", {"result_hmac": "2" * 64}) == []
    assert sf.field_violations("audit_completed", {"report_hmac": "1" * 63}) == [("/report_hmac", "field_type")]


def test_f2b4_job_keeps_its_observed_code_values():
    f = sf.SINK_FIELDS[_F2B4[2]]
    assert f.types == {"code", "hash"} and _F2B4[2] in sf.UNIONS
    assert sf.matches(f, SAMPLES["code"]) and sf.matches(f, "3" * 16) and sf.matches(f, _hex(64))
    assert not sf.matches(f, "two words") and not sf.matches(f, "3" * 15)
