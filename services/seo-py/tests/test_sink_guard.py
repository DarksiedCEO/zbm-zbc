"""Sink job A: the log and the ledger accept machine values only (sink_guard.py), and the vault blob API (vault.py).

Every credential-shaped, e-mail-shaped or random value here is BUILT AT RUN TIME (never a literal); assertions print
counts, paths and class names only."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import string
import time
import uuid

import pytest

import conftest
import ledger as ledger_mod
import sink_guard as sg
import vault
from errors import Unavailable
from helpers import Harness, invoice_id
from ledger import LedgerNotRecorded, derived_id
from service import SeoService
from store import PENDING_NAME

AT = chr(64)
ALNUM = string.ascii_letters + string.digits
TENANT_A, TENANT_B = "acme-corp", "globex-co"


def rnd(n: int, chars: str = ALNUM, seed: int = 0) -> str:
    r = random.Random(seed or n)
    return "".join(r.choice(chars) for _ in range(n))


def hexa(n: int, seed: int = 1) -> str:
    out = rnd(n, "0123456789abcdef", seed)
    return out if any(c in "abcdef" for c in out) else "a" + out[1:]


def marker() -> str:
    """Two words unique to this run: prose, and something to scan the disk for."""
    return "zq" + uuid.uuid4().hex[:10] + " wv" + uuid.uuid4().hex[:10]


# ------------------------------------------------------------------------------------------------ the grammar

def accepted_values() -> dict:
    sid = derived_id("aud", "t", 1)
    return {
        "empty": [""],
        "service_id": [sid, derived_id("evd", "x"), derived_id("anc", "e", 1, "s"), vault.ref(hexa(32)),
                       vault.blob_ref(hexa(32, 2))] + [f"seo-{p}-{hexa(40, i + 3)}"
                                                       for i, p in enumerate(sorted(sg.SERVICE_ID_PREFIXES))],
        "scoped_id": ["audit:" + sid, "citation_run:" + derived_id("cit", 2), "log:" + hexa(16),
                      "invoice:" + invoice_id(3), "request:" + str(uuid.uuid4()), derived_id("pst", 1) + ":v2:7",
                      derived_id("egr", 1) + ":p3"],
        "invoice_id": [invoice_id(1), invoice_id(2)],
        "request_id": [str(uuid.uuid4()), str(uuid.uuid4())],
        "request_key": [f"audit|acme|{uuid.uuid4()}", f"client_report|acme/{sid}|{uuid.uuid4()}",
                        f"domains|acme-corp|{uuid.uuid4()}", f"log_chunk|{derived_id('lgi', 1)}|{uuid.uuid4()}",
                        "seo|audit_interrupted"],
        "hash": [hexa(16), hexa(32), hexa(40), hexa(64), hashlib.sha256(b"x").hexdigest()],
        "timestamp": ["2026-10-09T18:00:00Z", "2026-10-09T18:00:00.123456Z", "2026-10-09T18:00Z"],
        "date": ["2026-10-09", "1999-01-31"],
        "number": ["0", "7", "12345678", "-3"],
        "version": ["1.0", "v2.3.1", "2026-10-01.1", "seo-client-report/2", "bot-families/2026-10-01.1",
                    "seo-audit-report/1"],
        "boolean": ["true", "false"],
        "actor": sorted(sg.ACTORS),
        "hostname": ["example.com", "shop.example.co.uk", "a-b.example.org"],
        "code": ["COMPLETED", "RUN_FAILED", "audit_requested", "client", "PROCESS_LOST", "svc_audits:run:12",
                 "engine.v2_rules", "k-test", "acme-corp", "pass", "GSC_AUTH_REFUSED", "Googlebot"]
        + sorted(sg.SERVICE_WORDS),
        "model_id": sorted(sg.MODEL_IDS),                    # sink job C: the adapters' declared model ids, exactly
    }


def test_every_class_is_accepted_and_the_set_is_closed():
    values = accepted_values()
    assert set(values) == set(sg.CLASSES)
    wrong = [(cls, i) for cls, vs in values.items() for i, v in enumerate(vs) if sg.classify(v) != cls]
    assert wrong == []
    assert all(sg.violations("job_ran", {"k": vs}) == [] for vs in values.values())


def refused_values() -> dict:
    digits = string.digits
    return {
        "prose": ("two words", "space"), "sentence": ("The audit ran on three pages.", "space"),
        "tab": ("a\tb", "space"), "newline": ("a\nb", "space"), "nbsp": ("a\u00a0b", "space"),
        "leading_space": (" ab", "space"), "trailing_space": ("ab ", "space"), "control": ("a\x00b", "space"),
        "at": ("ab" + AT + "cd", "at"), "email": ("pat" + AT + "example" + ".com", "at"),
        "email_tagged": ("pat.smith+x" + AT + "mail.example" + ".org", "at"),
        "key_sk": ("sk" + "-" + rnd(40), "secret"), "key_gh": ("gh" + "p_" + rnd(36), "secret"),
        "key_aws": ("AK" + "IA" + rnd(16, string.ascii_uppercase + digits), "secret"),
        "key_slack": ("xo" + "xb-" + rnd(8, digits) + "-" + rnd(24), "secret"),
        "random_24": (rnd(24), "secret"), "random_32": (rnd(32), "secret"), "random_48": (rnd(48), "secret"),
        "digits_9": ("1" + rnd(8, digits), "shape"), "digits_16": (rnd(16, "123456789"), "shape"),
        "digits_64": (rnd(64, digits), "shape"), "digits_in_token": ("acct-" + rnd(10, digits), "secret"),
        "digits_through_separators": ("1.555.010.0199", "secret"), "phone": ("555-010-0199", "shape"),
        "label_with_value": ("password_" + rnd(7, string.ascii_lowercase), "secret"),
        "label_colon_value": ("pw:" + rnd(7, string.ascii_lowercase), "secret"),
        "id_with_prose_part": (derived_id("pst", 1) + ":two words", "space"),
        "id_with_long_part": (derived_id("pst", 1) + ":" + rnd(14, string.ascii_lowercase), "secret"),
        "media_type": ("text/html", "shape"), "vendor_model": ("vendor/model-9", "shape"),
        "timestamp_offset": ("2026-10-09T18:00:00+00:00", "charset"),
        "long": ("a" * (sg.MAX_LEN + 1), "long"), "long_code": ("A" * (sg.CODE_MAX + 1), "shape"),
        "url": ("https://example.com/x", "shape"), "path": ("/about", "shape"), "non_ascii": ("caf\u00e9", "charset"),
        "brackets": ("[withheld]", "charset"), "host_key_label": (rnd(20).lower() + ".example.com", "host"),
        "upper_hex": (rnd(40, "ABCDEF" + digits), "secret"), "unknown_id_prefix": ("seo-zzz-" + hexa(40), "secret"),
        "request_key_bad_target": (f"audit|two words|{uuid.uuid4()}", "space"),
        "request_key_no_uuid": ("audit|acme|" + rnd(12), "shape"),
        "request_key_secret_target": (f"audit|{rnd(32)}|{uuid.uuid4()}", "shape"),
    }


@pytest.mark.parametrize("name", sorted(refused_values()))
def test_refused_values(name):
    value, reason = refused_values()[name]
    assert sg.classify(value) is None
    assert sg._judge(value)[1] == reason, name
    assert sg.violations("job_ran", {"note": value}) == [("/note", reason)]
    assert sg.violations("job_ran", {"rows": [{"x": [value]}]}) == [("/rows[]/x[]", reason)]
    assert [r for _p, r in sg.violations("job_ran", {value: 1})] == [reason]       # as a dict KEY


def test_non_strings_are_not_machine_values_and_other_objects_are_violations():
    assert all(sg.classify(v) is None for v in (None, 1, 1.5, True, b"ab", ["a"], {"a": 1}, object()))
    data = {"a": 1, "b": 2.5, "c": True, "d": None, "e": [1, 2, (3, None)], "f": {"g": []}}
    assert sg.violations("job_ran", data) == []
    bad = {"raw": b"ab", "s": {1, 2}, "o": object(), "t": (b"x",), 7: "seven", "n": {"deep": [bytearray(b"z")]}}
    assert sg.violations("job_ran", bad) == [("/n/deep[]", "type"), ("/o", "type"), ("/raw", "type"), ("/s", "type"),
                                             ("/t[]", "type"), ("/{key}", "key_type")]

    class Text(str):
        pass
    assert sg.violations("job_ran", {"x": Text("ok")}) == [("/x", "type")]
    assert sg.violations("not a kind", {}) == [("", "kind")]
    assert sg.violations("ledger:audit_requested", {"audit_id": derived_id("aud", 1)}) == []


EXPECTED_PATHS = [("/report/*", "space"), ("/report/pages/*/hN", "space"), ("/report/pages/*/title", "space"),
                  ("/report/pages/{key}", "charset"), ("/report/pages/{key}", "space"),
                  ("/report/rows[]/cellN", "secret"), ("/report/rows[]/cellN", "space"), ("/report/shaN", "space")]


def test_paths_are_normalised_and_never_carry_content():
    secret, prose = "sk" + "-" + rnd(40), marker()
    data = {"report": {"pages": {"/pricing?x=" + secret: {"title": prose}, prose: {"h1": prose}},
                       "sha256": prose, "rows": [{"cell9": secret}, {"cell9": prose}],
                       "Mixed-Key": prose, "x" * 41: prose}}
    found = sg.violations("audit_completed", data)
    assert found == EXPECTED_PATHS
    text = repr(found)
    assert secret not in text and prose not in text and "pricing" not in text and "Mixed" not in text
    assert all(set(p) <= set(string.ascii_lowercase + "_N/*[]{}") for p, _ in found)
    assert {r for _p, r in found} <= set(sg.REASONS)
    exc = sg.SinkViolation("audit_completed", [p for p, _ in found])
    assert exc.kind == "audit_completed" and secret not in str(exc) and prose not in str(exc)
    assert sg.SinkViolation(prose, ["/x"]).kind == "*" and prose not in str(sg.SinkViolation(prose, ["/x"]))


def test_depth_is_bounded_and_the_epoch_exemption_is_exact():
    deep = cur = {}
    for _ in range(sg.MAX_DEPTH + 5):
        cur["d"] = {}
        cur = cur["d"]
    assert [r for _p, r in sg.violations("job_ran", deep)] == ["depth"]
    digits16 = rnd(16, "123456789")
    assert sg.violations("ledger:log_anchor", {"epoch": digits16, "seq": 1, "line_sha256": hexa(64)}) == []
    assert sg.violations("ledger:log_anchor", {"other": digits16}) == [("/other", "shape")]
    assert sg.violations("ledger:log_anchor", {"epoch": rnd(17, "123456789")}) == [("/epoch", "shape")]
    assert sg.violations("ledger:log_anchor", {"epoch": [digits16]}) == [("/epoch[]", "shape")]
    digits32 = rnd(32, "123456789")
    assert sg.violations("prompt_set_version", {"p": [{"vault_id": digits32, "repair_id": digits32}]}) == []
    assert sg.violations("prompt_set_version", {"p": [{"hmac": digits32}]}) == [("/p[]/hmac", "shape")]
    assert sg.violations("prompt_set_version", {"vault_id": digits32 + "1"}) == [("/vault_id", "shape")]
    assert sg.DIGIT_HEX_KEYS == {"epoch", "vault_id", "repair_id"}


def test_the_guard_is_linear_on_a_two_thousand_row_record():
    rows = [{"engine": "openai", "prompt_id": derived_id("pst", i), "status": "CITED", "host": f"h{i % 50}.example.com",
             "evidence": {"vault_id": vault.ref(hexa(32, i + 1)),
                          "fields": {f: {"hmac_sha256": hexa(64, i * 7 + j + 1), "flag": None}
                                     for j, f in enumerate(("url", "excerpt", "title", "hint"))}},
             "position": i % 9, "cited": bool(i % 2), "model_version": "v1.2", "at": "2026-10-09T18:00:00Z"}
            for i in range(2000)]
    data = {"citation_run_id": derived_id("cit", 1), "result": {"facts": {"rows": rows}}, "result_sha256": hexa(64)}
    assert sg.violations("citation_run_recorded", data) == []
    best = min(_timed(lambda: sg.violations("citation_run_recorded", data)) for _ in range(5))
    assert best < 0.25, round(best, 4)                      # measured about 0.02 s; the bound only catches a blow-up


def _timed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


# ------------------------------------------------------------------------------------------------ the commit

def disk(h) -> bytes:
    out = b""
    for root, _dirs, files in os.walk(h.settings.data_dir):
        for f in sorted(files):
            with open(os.path.join(root, f), "rb") as fh:
                out += fh.read()
    return out


def state(h) -> tuple:
    return len(h.svc.log), len(h.ledger.events), len(h.svc.log._lines)


def needles(text: str) -> list:
    raw = text.encode()
    return [raw, text.split()[0].encode(), raw.hex().encode(), base64.b64encode(raw)[:-4],
            base64.urlsafe_b64encode(raw)[:-4], text.encode("utf-16-le"), json.dumps(text).encode()]


def refused_commit(h, kind, data, evidence=None):
    before = state(h)
    with pytest.raises(Unavailable) as e:
        h.svc._commit(kind, data, "scheduler", evidence=evidence)
    assert e.value.reason == "SINK_REFUSED" and e.value.status_code == 503 and e.value.body == {}
    assert state(h) == before
    assert h.svc.integrity["ok"] and h.svc._own_pending is None


def test_commit_refuses_a_non_legacy_value_and_writes_nothing(hd):
    h, text = hd, marker()
    h.svc._commit("job_ran", {"job": "nightly"}, "scheduler")           # the control: a machine value passes
    before_disk = disk(h)
    for data in ({"job": "nightly", "note": text}, {"job": text}, {"job": "nightly", text: 1},
                 {"job": "nightly", "x": [{"y": (text,)}]}, {"job": "nightly", "raw": text.encode()},
                 {"job": "nightly", "mail": "pat" + AT + "example" + ".com"},
                 {"job": "nightly", "key": "sk" + "-" + rnd(40)}, {"job": "nightly", "n": "1" + rnd(9, string.digits)}):
        refused_commit(h, "job_ran", data)
    now = disk(h)
    assert now == before_disk
    assert not any(n in now for n in needles(text))
    assert not os.path.exists(os.path.join(h.settings.data_dir, PENDING_NAME))
    assert not any(n in json.dumps(h.ledger.events).encode() for n in needles(text))
    h.svc._commit("job_ran", {"job": "nightly"}, "scheduler")           # and the service still writes
    assert h.svc.verify_integrity(force=True)["ok"]


def test_commit_refuses_an_evidence_payload_before_any_ledger_event(hd):
    h, text = hd, marker()
    good = ("audit_requested", "audit:" + derived_id("aud", 1), {"audit_id": derived_id("aud", 1)}, ("a", 1))
    bad = ("audit_requested", "audit:" + derived_id("aud", 2), {"audit_id": derived_id("aud", 2), "why": text},
           ("a", 2))
    before = disk(h)
    refused_commit(h, "job_ran", {"job": "nightly"}, evidence=bad)
    refused_commit(h, "job_ran", {"job": "nightly"}, evidence=[good, bad])    # the good event is not recorded either
    assert disk(h) == before and not any(n in json.dumps(h.ledger.events).encode() for n in needles(text))
    h.svc._commit("job_ran", {"job": "nightly"}, "scheduler", evidence=good)
    assert [e["event_type"] for e in h.ledger.events[-2:]] == ["audit_requested", "log_anchor"]


def test_the_ledger_recorder_refuses_payloads_and_anchors_itself(h):
    text, n = marker(), len(h.ledger.events)
    rec = h.svc.rec
    with pytest.raises(LedgerNotRecorded) as e:
        rec.record(derived_id("evd", 1), "audit_requested", "seo", "audit:" + derived_id("aud", 1),
                   {"audit_id": derived_id("aud", 1), "note": text}, "audit_requested x")
    assert e.value.took_effect is False and text not in str(e.value) and "/note" in str(e.value)
    with pytest.raises(LedgerNotRecorded):
        h.svc._anchor(derived_id("anc", 1), "seo", hexa(16), {"epoch": hexa(16), "seq": 1, "line_sha256": text},
                      "job_ran", 1)
    with pytest.raises(LedgerNotRecorded):
        rec.record(derived_id("evd", 2), "log_anchor", "seo", "log:" + hexa(16), {"epoch": hexa(16), "raw": b"x"}, "s")
    assert rec.try_record(derived_id("evd", 3), "audit_requested", "seo", "audit:x", {text: 1}, "s") is None
    assert len(h.ledger.events) == n
    h.svc._anchor(derived_id("anc", 2), "seo", hexa(16), {"epoch": hexa(16), "seq": 1, "line_sha256": hexa(64)},
                  "job_ran", 1)
    assert len(h.ledger.events) == n + 1


def test_a_legacy_path_passes_for_now_and_only_for_its_own_kind(h, monkeypatch):
    monkeypatch.setattr(sg, "OBSERVER", None)               # this test's own records are not the suite's
    simple = [(k, p) for k, p in sg.LEGACY_ROWS if not k.startswith(sg.LEDGER) and "[]" not in p
              and "*" not in p and "{" not in p and "N" not in p]
    if not simple:
        pytest.skip("no plain legacy path is left: nothing of this shape passes any more")
    kind, path = simple[0]
    text = marker()
    data = cur = {}
    parts = path.strip("/").split("/")
    for part in parts[:-1]:
        cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = text
    assert sg.refused(kind, data) == []
    other = "job_ran" if kind != "job_ran" else "switch_set"
    assert sg.refused(other, data) == [path]
    refused_commit(h, other, data)


def test_the_reason_code_is_in_the_catalogue_and_the_api_answers_it(h, monkeypatch):
    import reasons
    assert reasons.R("SINK_REFUSED") == "SINK_REFUSED"
    before = state(h)
    monkeypatch.setattr(sg, "violations", lambda kind, data: [("/x", "space")])
    r = h.post("/tenants", {"request_id": str(uuid.uuid4()), "tenant_id": "acme", "kind": "client"}, andre=True)
    assert r.status_code == 503 and r.json()["detail"] == "SINK_REFUSED"
    assert state(h) == before and "acme" not in h.svc.tenants


# ------------------------------------------------------------------------------------------------ the legacy list

def test_the_legacy_list_is_explicit_sorted_and_names_known_sinks():
    rows = sg.LEGACY_ROWS
    assert list(rows) == sorted(set(rows)) and sg.LEGACY_PROSE_PATHS == frozenset(rows)
    kinds = {name[3:] for name in dir(SeoService) if name.startswith("_a_")}
    assert len(kinds) == 27
    for kind, path in rows:
        assert kind.removeprefix(sg.LEDGER) and (kind in kinds or kind.startswith(sg.LEDGER)), kind
        assert path.startswith("/") and set(path) <= set(string.ascii_lowercase + "_N/*[]{}"), path
    assert sum(sg.LEGACY_COUNTS.values()) == len(rows)
    assert sg.LEGACY_COUNTS == {k: sum(1 for kk, _ in rows if kk == k) for k in {kk for kk, _ in rows}}


@pytest.mark.sink_last
def test_legacy_list_is_exactly_what_the_suite_needed_and_every_kind_passed():
    """Runs LAST. Every record the suite committed went through the guard (conftest's observer). A path outside the
    legacy list refuses its commit where it happens (the test above); here: no legacy entry is stale, and each of the
    27 kinds was committed. A partial run only checks that nothing outside the list was let through."""
    seen = conftest.SINK_SEEN
    assert seen["legacy_used"] <= sg.LEGACY_PROSE_PATHS
    if not seen["full_run"]:
        pytest.skip("a partial run: the legacy list is compared with a full run only")
    stale = sorted(sg.LEGACY_PROSE_PATHS - seen["legacy_used"])
    assert stale == [], f"{len(stale)} legacy entries are no longer needed: delete them from LEGACY_ROWS: {stale}"
    kinds = {name[3:] for name in dir(SeoService) if name.startswith("_a_")}
    assert kinds - seen["passed"] == set()
    assert len([k for k in seen["passed"] if k.startswith(sg.LEDGER)]) >= 32


# ------------------------------------------------------------------------------------------------ the blob API

def vault_key(kid: str = "k1"):
    return vault.parse_vault_key(base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"), kid)


def make_vault(kind: str, tmp_path, key=None):
    key = key or vault_key()
    if kind == "memory":
        return vault.MemoryCitationVault(key), key
    name = vault.OPERATOR_VAULT_NAME if kind == "operator" else vault.VAULT_NAME
    return vault.CitationVault(str(tmp_path / "data"), key, name=name), key


def reopen(v, key):
    if v.path is None:
        return v
    return vault.CitationVault(v.data_dir, key, name=os.path.basename(v.path))


def lines_of(v) -> list:
    if v.path is None:
        return v.lines()
    if not os.path.exists(v.path):
        return []
    with open(v.path, "rb") as fh:
        return fh.read().split(b"\n")[:-1]


def rewrite(v, lines: list) -> None:
    raw = b"".join(ln + b"\n" for ln in lines)
    if v.path is None:
        v._buf[:] = raw
        v._index.clear()
        offset = 0
        for ln in lines:
            parsed = vault._parse_line(ln)
            if parsed is not None:
                v._index[parsed["vault_id"]] = (offset, len(ln))
            offset += len(ln) + 1
        v._size = offset
    else:
        with open(v.path, "wb") as fh:
            fh.write(raw)


def big_object(mb: float = 1.3) -> dict:
    r = random.Random(11)
    rows = [{"i": i, "text": "".join(r.choice(ALNUM + " ") for _ in range(200))} for i in range(int(mb * 5000))]
    return {"rows": rows, "note": marker()}


def not_found(v, tenant, ref_text) -> None:
    with pytest.raises(vault.VaultNotFound) as e:
        v.get_blob(tenant, ref_text)
    assert str(e.value) == vault.NOT_FOUND and e.value.__cause__ is None and e.value.__context__ is None


VAULTS = ("memory", "file", "operator")


@pytest.mark.parametrize("kind", VAULTS)
def test_blob_round_trip_small_large_and_every_shape(kind, tmp_path):
    v, key = make_vault(kind, tmp_path)
    big = big_object()
    assert len(json.dumps(big)) > 1024 * 1024
    objs = [{}, [], {"a": 1}, [1, "two", None, {"k": [True]}], {"u": "caf\u00e9 \u4e2d\u6587 \U0001f600"}, big,
            {"edge": "x" * (vault.BLOB_CHUNK_BYTES - 11)}, {"edge": "x" * (2 * vault.BLOB_CHUNK_BYTES)}]
    refs = [v.put_blob(TENANT_A, o) for o in objs]
    assert len(set(refs)) == len(refs)
    for ref_text, o in zip(refs, objs):
        assert sg.classify(ref_text) == "service_id" and vault.parse_blob_ref(ref_text) is not None
        assert vault.parse_ref(ref_text) is None
        assert v.get_blob(TENANT_A, ref_text) == o
        not_found(v, TENANT_B, ref_text)
    v2 = reopen(v, key)
    assert all(v2.get_blob(TENANT_A, r) == o for r, o in zip(refs, objs))
    chunks = -(-len(vault._encode(big)) // vault.BLOB_CHUNK_BYTES)
    assert chunks >= 6 and all(len(ln) <= vault._MAX_LINE_BYTES for ln in lines_of(v))


@pytest.mark.parametrize("kind", VAULTS)
def test_blob_refuses_bad_input_and_fails_closed_without_a_key(kind, tmp_path):
    v, _key = make_vault(kind, tmp_path)
    for obj in ("text", 7, None, b"x", (1,), {"x": float("nan")}, {"x": {1, 2}}, {"x": b"y"}):
        with pytest.raises(vault.VaultRecordInvalid):
            v.put_blob(TENANT_A, obj)
    with pytest.raises(vault.VaultRecordInvalid):
        v.put_blob("Not A Tenant", {})
    assert len(v) == 0 and lines_of(v) == []
    ref_text = v.put_blob(TENANT_A, {"a": 1})
    for bad in (None, 7, "", ref_text[:-1], ref_text[:-1] + ("0" if ref_text[-1] != "0" else "1"), ref_text.upper(),
                vault.blob_ref(hexa(32, 5)), vault.ref(vault.parse_blob_ref(ref_text)), vault.parse_blob_ref(ref_text)):
        not_found(v, TENANT_A, bad)
    not_found(v, "Not A Tenant", ref_text)
    nokey = vault.MemoryCitationVault(None) if kind == "memory" else \
        vault.CitationVault(str(tmp_path / "nokey"), None)
    with pytest.raises(vault.VaultUnavailable):
        nokey.put_blob(TENANT_A, {"a": 1})
    with pytest.raises(vault.VaultUnavailable):
        nokey.get_blob(TENANT_A, ref_text)
    assert lines_of(nokey) == [] if kind == "memory" else not os.path.exists(nokey.path)


@pytest.mark.parametrize("kind", VAULTS)
def test_blob_lines_are_not_records_and_records_are_not_blobs(kind, tmp_path):
    v, _key = make_vault(kind, tmp_path)
    ref_text = v.put_blob(TENANT_A, big_object(0.3))
    for ln in lines_of(v):
        vid = vault._parse_line(ln)["vault_id"]
        with pytest.raises(vault.VaultNotFound):
            v.get(TENANT_A, vid)                           # a chunk or a manifest is never served as a record
        if vid != vault.parse_blob_ref(ref_text):
            not_found(v, TENANT_A, vault.blob_ref(vid))    # and a chunk is not a blob
    rid = v.put(TENANT_A, {"url": "x"})
    not_found(v, TENANT_A, vault.blob_ref(rid))
    assert v.get(TENANT_A, rid) == {"url": "x"} and v.get_blob(TENANT_A, ref_text)["note"]


def fields(line: bytes) -> dict:
    return json.loads(line.decode("ascii"))


def with_fields(line: bytes, **change) -> bytes:
    return json.dumps({**fields(line), **change}, sort_keys=True, separators=(",", ":")).encode("ascii")


def flip(text: str) -> str:
    raw = bytearray(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    raw[len(raw) // 2] ^= 0x01
    return base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")


def blob_attacks() -> dict:
    """name -> f(lines of blob 1 (chunks then manifest), lines of blob 2) -> the new line list."""
    def swap_cipher(a, b, i, j):
        a, b = list(a), list(b)
        fi, fj = fields(a[i]), fields(b[j])
        a[i] = with_fields(a[i], n=fj["n"], c=fj["c"])
        b[j] = with_fields(b[j], n=fi["n"], c=fi["c"])
        return a, b

    def reorder(one, two):
        a = list(one)
        fi, fj = fields(a[0]), fields(a[1])
        a[0], a[1] = with_fields(a[0], n=fj["n"], c=fj["c"]), with_fields(a[1], n=fi["n"], c=fi["c"])
        return a + two

    def reorder_ids(one, two):
        a = list(one)
        fi, fj = fields(a[0]), fields(a[1])
        a[0], a[1] = with_fields(a[0], id=fj["id"]), with_fields(a[1], id=fi["id"])
        return a + two
    return {
        "reorder_ciphertexts": reorder,
        "reorder_ids": reorder_ids,
        "drop_first_chunk": lambda one, two: one[1:] + two,
        "drop_middle_chunk": lambda one, two: one[:1] + one[2:] + two,
        "drop_last_chunk": lambda one, two: one[:-2] + one[-1:] + two,
        "drop_manifest": lambda one, two: one[:-1] + two,
        "swap_chunk_across_blobs": lambda one, two: sum(swap_cipher(one, two, 0, 0), []),
        "swap_manifests": lambda one, two: sum(swap_cipher(one, two, len(one) - 1, len(two) - 1), []),
        "tamper_chunk": lambda one, two: [with_fields(one[0], c=flip(fields(one[0])["c"]))] + one[1:] + two,
        "tamper_last_chunk": lambda one, two: one[:-2] + [with_fields(one[-2], c=flip(fields(one[-2])["c"]))]
        + one[-1:] + two,
        "tamper_manifest": lambda one, two: one[:-1] + [with_fields(one[-1], c=flip(fields(one[-1])["c"]))] + two,
        "tamper_nonce": lambda one, two: [with_fields(one[0], n=flip(fields(one[0])["n"]))] + one[1:] + two,
        "tamper_tenant_tag": lambda one, two: [with_fields(one[0], t=flip(fields(one[0])["t"]))] + one[1:] + two,
        "truncate_chunk": lambda one, two: [with_fields(one[0], c=fields(one[0])["c"][:-8])] + one[1:] + two,
        "duplicate_chunk_over_next": lambda one, two: one[:1] + [with_fields(one[1], n=fields(one[0])["n"],
                                                                             c=fields(one[0])["c"])] + one[2:] + two,
    }


@pytest.mark.parametrize("kind", VAULTS)
@pytest.mark.parametrize("attack", sorted(blob_attacks()))
def test_blob_chunks_cannot_be_reordered_dropped_swapped_or_changed(kind, attack, tmp_path):
    v, key = make_vault(kind, tmp_path)
    obj1, obj2 = big_object(0.5), big_object(0.52)
    r1 = v.put_blob(TENANT_A, obj1)
    n1 = len(lines_of(v))
    r2 = v.put_blob(TENANT_A, obj2)
    lines = lines_of(v)
    assert n1 >= 4 and len(lines) - n1 >= 4
    rewrite(v, blob_attacks()[attack](lines[:n1], lines[n1:]))
    v = reopen(v, key)
    not_found(v, TENANT_A, r1)
    if attack.startswith("swap"):
        not_found(v, TENANT_A, r2)
    else:
        assert v.get_blob(TENANT_A, r2) == obj2             # the untouched blob still reads


@pytest.mark.parametrize("kind", VAULTS)
def test_blob_cannot_be_moved_to_another_tenant_or_key(kind, tmp_path):
    v, key = make_vault(kind, tmp_path)
    ra = v.put_blob(TENANT_A, big_object(0.25))
    na = len(lines_of(v))
    rb = v.put_blob(TENANT_B, big_object(0.26))
    lines = lines_of(v)
    a, b = lines[:na], lines[na:]
    # every line of A's blob re-tagged as B's (the tag is public in the file): the associated data still names A
    moved = [with_fields(ln, t=fields(b[0])["t"]) for ln in a]
    rewrite(v, moved + b)
    v = reopen(v, key)
    not_found(v, TENANT_B, ra)
    not_found(v, TENANT_A, ra)
    assert v.get_blob(TENANT_B, rb)["note"]
    other = vault.MemoryCitationVault(vault_key("k1"))      # the same key ID, another key
    rewrite(other, lines)
    not_found(other, TENANT_A, ra)


@pytest.mark.parametrize("kind", VAULTS)
def test_blob_keeps_no_plaintext_at_rest_in_any_encoding(kind, tmp_path):
    v, _key = make_vault(kind, tmp_path)
    words = [marker() for _ in range(3)]
    obj = {"first": words[0], "rows": [{"t": "x" * 300, "i": i} for i in range(2500)], "mid": words[1],
           "tail": ["y" * 100] * 3000 + [words[2]]}
    ref_text = v.put_blob(TENANT_A, obj)
    raw = b"\n".join(lines_of(v))
    if v.path is not None:
        raw = b"".join(open(os.path.join(v.data_dir, f), "rb").read() for f in sorted(os.listdir(v.data_dir)))
    decoded = b"".join(base64.urlsafe_b64decode(fields(ln)["c"] + "=" * (-len(fields(ln)["c"]) % 4))
                       for ln in lines_of(v))
    hits = 0
    checked = 0
    for word in words + [TENANT_A + " " + TENANT_A, '"rows":[{"i":0,"t":"xxxxxxxx', "xxxxxxxxxxxxxxxx",
                         vault.parse_blob_ref(ref_text)]:
        for needle in needles(word) + [word.encode("utf-16-be"), word.encode("utf-32-le"), word[::-1].encode()]:
            if len(needle) >= 12:                           # a shorter needle can occur in base64 by chance
                checked += 1
                hits += needle in raw or needle in decoded
    assert hits == 0 and checked >= 50
    assert v.get_blob(TENANT_A, ref_text) == obj


def test_blob_is_one_append_and_one_fsync(tmp_path, monkeypatch):
    v, _key = make_vault("file", tmp_path)
    v.put(TENANT_A, {"warm": 1})                            # the file exists: no directory fsync is due
    calls = {"pwrite": 0, "fsync": 0}
    real_pwrite, real_fsync = os.pwrite, os.fsync
    monkeypatch.setattr(os, "pwrite", lambda *a: (calls.__setitem__("pwrite", calls["pwrite"] + 1), real_pwrite(*a))[1])
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.__setitem__("fsync", calls["fsync"] + 1), real_fsync(fd))[1])
    ref_text = v.put_blob(TENANT_A, big_object())
    assert calls == {"pwrite": 1, "fsync": 1}
    monkeypatch.undo()
    assert len(v.get_blob(TENANT_A, ref_text)["rows"]) == 6500


@pytest.mark.parametrize("kind", VAULTS)
def test_blob_joins_a_collecting_batch(kind, tmp_path):
    v, _key = make_vault(kind, tmp_path)
    batch = vault.VaultBatch()
    with v.collecting(batch):
        ref_text = v.put_blob(TENANT_A, big_object(0.3))
        rid = v.put(TENANT_A, {"a": 1})
    assert len(batch) >= 4 and lines_of(v) == []
    not_found(v, TENANT_A, ref_text)
    batch.discard()
    v.write_batch(batch)
    assert lines_of(v) == []
    with v.collecting(batch):
        ref_text = v.put_blob(TENANT_A, {"b": 2})
        rid = v.put(TENANT_A, {"a": 1})
    v.write_batch(batch)
    assert v.get_blob(TENANT_A, ref_text) == {"b": 2} and v.get(TENANT_A, rid) == {"a": 1}


def test_a_failed_blob_write_leaves_nothing_and_the_vault_still_works(tmp_path, monkeypatch):
    v, key = make_vault("file", tmp_path)
    kept = v.put_blob(TENANT_A, {"kept": 1})
    size, count = os.path.getsize(v.path), len(v)
    real = os.pwrite
    monkeypatch.setattr(os, "pwrite", lambda fd, data, off: real(fd, data[:len(data) // 2], off))   # a short write
    with pytest.raises(vault.VaultWriteError):
        v.put_blob(TENANT_A, big_object(0.5))
    monkeypatch.undo()
    assert os.path.getsize(v.path) == size and len(v) == count and v.fault is None
    again = v.put_blob(TENANT_A, {"again": 2})
    v2 = reopen(v, key)
    assert v2.get_blob(TENANT_A, kept) == {"kept": 1} and v2.get_blob(TENANT_A, again) == {"again": 2}


@pytest.mark.parametrize("cut", ["inside_first_chunk", "inside_middle_chunk", "inside_manifest", "before_manifest"])
def test_a_crash_mid_blob_leaves_nothing_readable_and_is_recoverable(cut, tmp_path):
    v, key = make_vault("file", tmp_path)
    kept = v.put_blob(TENANT_A, {"kept": 1})
    rid = v.put(TENANT_A, {"r": 1})
    size = os.path.getsize(v.path)
    obj = big_object(0.5)
    lost = v.put_blob(TENANT_A, obj)
    word = obj["note"]
    lines = lines_of(v)
    new = lines[3:]                                          # kept = 2 lines, the record = 1
    offsets = [size]
    for ln in new:
        offsets.append(offsets[-1] + len(ln) + 1)
    at = {"inside_first_chunk": offsets[0] + 1000, "inside_middle_chunk": offsets[1] + len(new[1]) // 2,
          "inside_manifest": offsets[-2] + 40, "before_manifest": offsets[-2]}[cut]
    with open(v.path, "r+b") as fh:                          # the process died with only this much on disk
        fh.truncate(at)
    v2 = vault.CitationVault(v.data_dir, key)
    torn = cut != "before_manifest"
    assert (v2.tail_repair or {}).get("torn_lines", 0) == (1 if torn else 0)
    not_found(v2, TENANT_A, lost)
    for ln in lines_of(v2)[3:]:                              # the chunks that did land are not readable by any route
        vid = vault._parse_line(ln)["vault_id"]
        with pytest.raises(vault.VaultNotFound):
            v2.get(TENANT_A, vid)
        not_found(v2, TENANT_A, vault.blob_ref(vid))
    everything = b"".join(open(os.path.join(v.data_dir, f), "rb").read() for f in sorted(os.listdir(v.data_dir)))
    assert not any(n in everything for n in needles(word))
    assert v2.get_blob(TENANT_A, kept) == {"kept": 1} and v2.get(TENANT_A, rid) == {"r": 1}
    again = v2.put_blob(TENANT_A, obj)                       # recoverable: the same blob is simply put again
    v3 = vault.CitationVault(v.data_dir, key)
    assert v3.get_blob(TENANT_A, again) == obj and v3.get_blob(TENANT_A, kept) == {"kept": 1}
    not_found(v3, TENANT_A, lost)


def test_blob_reads_under_the_previous_key_after_a_rotation(tmp_path):
    old_text = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    new_text = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    v = vault.CitationVault(str(tmp_path / "data"), vault.parse_vault_key(old_text, "k1"))
    obj = big_object(0.3)
    ref_text = v.put_blob(TENANT_A, obj)
    rotated = vault.CitationVault(v.data_dir, vault.parse_vault_key(new_text, "k2", old_text, "k1"))
    assert rotated.get_blob(TENANT_A, ref_text) == obj
    newer = rotated.put_blob(TENANT_A, {"n": 1})
    assert rotated.get_blob(TENANT_A, newer) == {"n": 1}
    alone = vault.CitationVault(v.data_dir, vault.parse_vault_key(new_text, "k2"))
    not_found(alone, TENANT_A, ref_text)
    assert alone.get_blob(TENANT_A, newer) == {"n": 1}


# ------------------------------------------------------------------------------------------------ mutation bite

def chunk_parts(v, key, ref_text) -> tuple:
    blob_id = vault.parse_blob_ref(ref_text)
    got = v._unseal(key, TENANT_A, blob_id, lambda kid: vault._blob_manifest_aad(TENANT_A, blob_id, kid))
    manifest = vault._blob_manifest(got[0])
    return blob_id, manifest["chunks"], manifest["n"], got[1]


def prop_commit_refuses_prose(tmp_path):
    h = Harness(tmp_path / "p1")
    before = state(h)
    with pytest.raises(Unavailable):
        h.svc._commit("job_ran", {"job": "nightly", "note": marker()}, "scheduler")
    assert state(h) == before


def prop_ledger_refuses_prose(tmp_path):
    h = Harness(tmp_path / "p2")
    before = state(h)
    with pytest.raises(LedgerNotRecorded):
        h.svc.rec.record(derived_id("evd", 1), "audit_requested", "seo", "audit:x", {"note": marker()}, "s")
    assert state(h) == before


def prop_at_refused(tmp_path):
    assert sg.classify("ab" + AT + "cd") is None and sg.violations("job_ran", {"x": "ab" + AT + "cd"}) != []


def prop_whitespace_refused(tmp_path):
    assert sg.classify("two words") is None and sg.violations("job_ran", {"x": "two words"}) != []


def prop_key_shapes_refused(tmp_path):
    for value in ("sk" + "-" + rnd(40), rnd(32), "acct-" + rnd(10, string.digits)):
        assert sg.classify(value) is None


def prop_host_keys_refused(tmp_path):
    assert sg.classify(rnd(20).lower() + ".example.com") is None


def prop_digit_hex_refused(tmp_path):
    assert sg.classify(rnd(16, "123456789")) is None


def prop_chunk_index_is_bound(tmp_path):
    key = vault_key()
    v = vault.MemoryCitationVault(key)
    ref_text = v.put_blob(TENANT_A, big_object(0.25))
    blob_id, chunks, n, kid = chunk_parts(v, key, ref_text)
    assert n >= 2 and v._blob_chunk(key, TENANT_A, blob_id, 0, n, chunks[0], kid) is not None
    assert v._blob_chunk(key, TENANT_A, blob_id, 1, n, chunks[0], kid) is None


def prop_chunk_count_and_blob_are_bound(tmp_path):
    key = vault_key()
    v = vault.MemoryCitationVault(key)
    ref_text = v.put_blob(TENANT_A, big_object(0.25))
    blob_id, chunks, n, kid = chunk_parts(v, key, ref_text)
    assert v._blob_chunk(key, TENANT_A, blob_id, 0, n + 1, chunks[0], kid) is None
    assert v._blob_chunk(key, TENANT_A, hexa(32, 9), 0, n, chunks[0], kid) is None


def prop_chunk_tenant_is_bound(tmp_path):
    key = vault_key()
    v = vault.MemoryCitationVault(key)
    ref_text = v.put_blob(TENANT_A, big_object(0.25))
    blob_id, chunks, n, kid = chunk_parts(v, key, ref_text)
    tag = base64.urlsafe_b64encode(key._cur.tenant_tag(TENANT_B)).rstrip(b"=").decode("ascii")
    rewrite(v, [with_fields(ln, t=tag) for ln in lines_of(v)])      # the public tag re-written as the other tenant's
    assert v._blob_chunk(key, TENANT_B, blob_id, 0, n, chunks[0], kid) is None
    assert v._blob_chunk(key, TENANT_A, blob_id, 0, n, chunks[0], kid) is None


def _aad_without(part: str):
    real = vault._blob_chunk_aad

    def aad(tenant_id, blob_id, index, count, key_id):
        return real(TENANT_A if part == "tenant" else tenant_id, "0" * 32 if part == "blob" else blob_id,
                    0 if part == "index" else index, 0 if part == "count" else count, key_id)
    return aad


BITES = {
    "guard_disabled_in_commit": (lambda m: m.setattr(sg, "enforce", lambda kind, data: None),
                                 (prop_commit_refuses_prose, prop_ledger_refuses_prose)),
    "legacy_bypass_widened_to_everything": (lambda m: m.setattr(sg, "_legacy", lambda kind, path: True),
                                            (prop_commit_refuses_prose, prop_ledger_refuses_prose)),
    "no_violations_reported": (lambda m: m.setattr(sg, "violations", lambda kind, data: []),
                               (prop_commit_refuses_prose, prop_ledger_refuses_prose)),
    "at_allowed": (lambda m: m.setattr(sg, "_ALLOWED", sg._ALLOWED + AT), (prop_at_refused,)),
    "whitespace_allowed": (lambda m: m.setattr(sg, "_ALLOWED", sg._ALLOWED + " "), (prop_whitespace_refused,)),
    "check_skipped_for_non_id_tokens": (lambda m: m.setattr(sg, "_check", lambda value: None),
                                        (prop_key_shapes_refused,)),
    "host_check_skipped": (lambda m: m.setattr(sg, "_host_ok", lambda host: True), (prop_host_keys_refused,)),
    "digit_only_hex_is_a_hash": (lambda m: m.setattr(sg, "_is_hash", lambda s, digits_ok=False: len(s) in
                                                     sg.HASH_LENGTHS and not s.strip("0123456789abcdef")),
                                 (prop_digit_hex_refused,)),
    "blob_aad_without_chunk_index": (lambda m: m.setattr(vault, "_blob_chunk_aad", _aad_without("index")),
                                     (prop_chunk_index_is_bound,)),
    "blob_aad_without_chunk_count": (lambda m: m.setattr(vault, "_blob_chunk_aad", _aad_without("count")),
                                     (prop_chunk_count_and_blob_are_bound,)),
    "blob_aad_without_blob_id": (lambda m: m.setattr(vault, "_blob_chunk_aad", _aad_without("blob")),
                                 (prop_chunk_count_and_blob_are_bound,)),
    "blob_aad_without_tenant": (lambda m: m.setattr(vault, "_blob_chunk_aad", _aad_without("tenant")),
                                (prop_chunk_tenant_is_bound,)),
}
PROPERTIES = sorted({p for _patch, props in BITES.values() for p in props}, key=lambda f: f.__name__)


@pytest.mark.parametrize("prop", PROPERTIES, ids=lambda f: f.__name__)
def test_properties_hold_unmutated(prop, tmp_path):
    prop(tmp_path)


@pytest.mark.parametrize("name", sorted(BITES))
def test_mutation_bites(name, tmp_path, monkeypatch):
    patch, props = BITES[name]
    monkeypatch.setattr(sg, "OBSERVER", None)               # a mutated guard's verdicts are not the suite's record
    sg._CACHE.clear()
    sg._KEYS.clear()
    patch(monkeypatch)
    try:
        for i, prop in enumerate(props):
            with pytest.raises(BaseException) as caught:
                prop(tmp_path / f"m{i}")
            assert caught.type in (AssertionError, pytest.fail.Exception), (name, prop.__name__, caught.type)
    finally:
        monkeypatch.undo()
        sg._CACHE.clear()
        sg._KEYS.clear()


def test_the_ledger_and_the_service_use_the_same_guard():
    import service
    assert ledger_mod.sink_guard is sg and service.sink_guard is sg
