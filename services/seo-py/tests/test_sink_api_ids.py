"""C2-2f (lead ruling F2e-1; core-spec/2 T-3, T-5, CL-7, O bar): an id-bearing field written from API input takes
EVERY value its route's own validator accepts - a 422 or a write, never a 503 at the sink. Each test generates the
accepted set from the route's pattern + validator (seeded) and checks it against every SINK_FIELDS entry with that
leaf. The unions this needed are listed with their reasons in sink_fields.UNIONS (C2-2f)."""

import random
import re
import string

import pytest

import models
import sink_fields as sf
import sink_guard as sg
import svc_audits
import svc_tenants
from agents import secret_guard

SEED = 20261114
LOW, DIG, HEX = string.ascii_lowercase, string.digits, "0123456789abcdef"


def gen(rng, first, rest, lo, hi, n):
    subs = [rest] + [s for s in (DIG, HEX, DIG + ".", DIG + "-", LOW, DIG + "._-", LOW + DIG) if set(s) <= set(rest)]
    lens = list(range(lo, hi + 1)) + [L for L in (16, 32, 40, 64) if lo <= L <= hi] * 4
    out = []
    for i in range(n):
        L = rng.choice(lens) if i % 2 else rng.choice([L for L in lens if L <= 12] or lens)
        sub = rng.choice(subs)
        f = [c for c in first if c in sub] or first
        out.append(rng.choice(f) + "".join(rng.choice(sub) for _ in range(L - 1)))
    return out


def domains(rng):
    return gen(rng, LOW + DIG + ".-", LOW + DIG + ".-", 4, 80, 6000) + [
        ".".join("".join(rng.choice(LOW + DIG + "-") for _ in range(rng.randint(1, 12)))
                 for _ in range(rng.randint(2, 4))) for _ in range(6000)]


def ident_ok(x):
    return not svc_tenants.identifier_refused(x, prose=True)


def reg_domain_ok(x):
    """C2-2g: the registration route's own validator (svc_tenants.set_domains): DOMAIN_INVALID, then
    IDENTIFIER_WITHHELD. (The C2-2f probe left out valid_domain / the host grammar, so it counted non-hostnames.)"""
    return svc_tenants.valid_domain(x) and not svc_tenants.domain_invalid(x) and not svc_tenants.domain_refused(x)


def competitor_ok(x):
    """C2-2g: svc_audits._strict_prompt_set - DOMAIN_INVALID for a non-hostname, then IDENTIFIER_WITHHELD."""
    return not svc_tenants.domain_invalid(x) and not svc_audits._domain_refused(x)


def name_ok(x):
    return not svc_audits._identifier_refused(x) and secret_guard.check(x) is None


ULID = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CASES = {   # field -> (route pattern, corpus(rng), route validator, leaves written to the sinks)
    "tenant_id": (models.TENANT, lambda r: gen(r, LOW, LOW + DIG + "-", 2, 40, 8000), ident_ok, {"tenant_id", "arg"}),
    "finance_client_id": (models.FINANCE_CLIENT, lambda r: gen(r, string.ascii_letters + DIG,
                          string.ascii_letters + DIG + "._-", 1, 64, 8000), ident_ok, {"finance_client_id"}),
    "domains": (models.DOMAIN, domains, reg_domain_ok,
                {"domains[]", "domain", "property_host"}),
    "competitor_domains": (models.DOMAIN, domains, competitor_ok,
                           {"competitor_domains[]"}),
    "name": (r"^[a-z0-9][a-z0-9_-]{1,63}$", lambda r: gen(r, LOW + DIG, LOW + DIG + "_-", 2, 64, 8000), name_ok,
             {"name"}),
    "invoice_id": (models.INVOICE, lambda r: ["fin-inv-" + "".join(r.choice(a) for _ in range(26))
                                              for a in [ULID, DIG, "ABCDEF0123456789"] * 1000], None, {"invoice_id"}),
    "prompt_set_id": (r"^seo-pst-[0-9a-f]{40}$", lambda r: ["seo-pst-" + "".join(r.choice(a) for _ in range(40))
                                                            for a in [HEX, DIG] * 1000], None, {"prompt_set_id"}),
}


def stuck_values(field):
    rx, corpus, ok, leaves = CASES[field]
    rxc = re.compile(rx)
    accepted = sorted({x for x in corpus(random.Random(SEED)) if rxc.fullmatch(x) and (ok is None or ok(x))})
    entries = [(k, f) for k, f in sf.SINK_FIELDS.items() if k[2].rsplit("/", 1)[-1] in leaves]
    assert entries, field
    stuck = {(k, x) for x in accepted for k, f in entries if not sf.matches(f, x, key=sf.leaf_key(k[2]))}
    return accepted, entries, stuck


@pytest.mark.parametrize("field", ["tenant_id", "finance_client_id", "domains", "competitor_domains", "invoice_id",
                                   "prompt_set_id"])
def test_every_route_accepted_id_is_writable_at_every_sink_entry(field):
    accepted, entries, stuck = stuck_values(field)
    if field == "finance_client_id":
        # C2-6g3 rewrite (OWNER DECISION 2026-11-15 02:00 PT (E-COMP), lead ruling 02:40 PT, G-7; T-2, O-4 I): the route accepts every valid reference, and every sink entry of
        # the reference is a T-2 vault reference field: it holds the CODE, never the raw value
        import vault as vault_mod
        coder = vault_mod.Coder(vault_mod.ephemeral_vault_key(), "acme")
        assert entries and all(f.ref for _k, f in entries)
        assert stuck == {(k, x) for x in accepted for k, _f in entries}           # every raw value refused
        coded = {(k, x) for x in accepted for k, f in entries
                 if not sf.matches(f, "c:text:" + coder.code("text", x), key=sf.leaf_key(k[2]))}
        print(f"{field}: accepted={len(accepted)} entries={len(entries)} code_refused={len(coded)}")
        assert len(accepted) >= 1000 and coded == set()
        return
    print(f"{field}: accepted={len(accepted)} entries={len(entries)} refused_at_the_sink={len(stuck)}")
    # C2-2g: only ~740 of the generated domains are hostnames (the corpus is mostly non-hostnames, now 422)
    assert len(accepted) >= (700 if "domains" in field else 1000) and stuck == set()


def test_a_prompt_set_name_is_refused_at_the_sink_only_when_it_has_no_class():
    """Residual F2f-1 (lead): accepted names with no T-1 class (e.g. ``0-``) cannot be typed (CL-7: no free-text
    type); every name that HAS a class is writable."""
    accepted, entries, stuck = stuck_values("name")
    # Re-pinned (G-7; lead ruling F3c-3, applied by owner decision 19:20 PT / card C2-OV, FCL-IDS #16): a closed enum
    # constant of the code base (``F.values``, e.g. the outbound Anthropic tool name "web_search") is never route
    # input, so a route-accepted name it refuses is not stuck; the enum is NOT widened.
    stuck = {(k, x) for k, x in stuck if sf.SINK_FIELDS[k].values is None}
    assert {sg.classify(x) for _k, x in stuck} <= {None}
    assert len(accepted) >= 1000


EXPECTED = {   # C2-2f unions: (sink, kind, path) leaf -> declared type set after the widening
    "finance_client_id": {"amount", "code", "hash", "number", "version"},
    "name": {"code", "hash", "number"},
}


def test_the_c22f_unions_are_exact_listed_and_never_free_text():
    widened = {k: v for k, v in sf.UNIONS.items() if v.startswith("C2-2f")}
    assert len(widened) == 44     # C2-2g: 57 - 13 domain entries reverted (lead ruling F2f-3)
    for k in widened:
        f = sf.SINK_FIELDS[k]
        leaf = k[2].rsplit("/", 1)[-1]
        if leaf == "finance_client_id" and f.ref:
            # C2-6g3 re-pin (OWNER DECISION 2026-11-15 02:00 PT (E-COMP), lead ruling 02:40 PT, G-7; T-2): the C2-2f open union is replaced by the T-2 reference type
            assert set(f.types) == {"code"} and f.ref is True, k
            continue
        assert set(f.types) <= set(sg.CLASSES) and len(f.types) >= 2
        if leaf in EXPECTED:
            assert set(f.types) == EXPECTED[leaf], k
        elif leaf in ("tenant_id", "arg"):
            assert set(f.types) == {"code", "hash"}, k
        else:
            assert leaf in ("tenant_id", "arg"), k


# ---- C2-2g (lead ruling F2f-3): a domain is a hostname; a non-hostname is 422 DOMAIN_INVALID before any write ----
DOMAIN_LEAVES = {"domains[]", "domain", "property_host"}
NON_HOSTNAMES = ["-0231", "0.154", "000778", ".3cdc", "0i.pqo.x", "0-zc.ly4wrad4w.3zm", "--l0cmyr.0bj3l",
                 "40d8b97e21ceaaa8", "03.3", "a..b.com", "ab-.com"]
HOSTNAMES = ["site.test", "example.com", "www.example.co.uk", "xn--80ak6aa92e.com", "shop.xn--p1ai", "1password.com"]


def test_domain_sink_fields_are_hostname_again():
    ents = {k: set(f.types) for k, f in sf.SINK_FIELDS.items() if k[2].rsplit("/", 1)[-1] in DOMAIN_LEAVES}
    assert len(ents) >= 12 and all(t == {"hostname"} for t in ents.values()), ents
    comp = {k: set(f.types) for k, f in sf.SINK_FIELDS.items() if k[2].endswith("/competitor_domains[]")}
    assert comp == {("seo_log", "prompt_set_version", "/inline/competitor_domains[]"): {"code", "hostname"}}
    assert not any(v.startswith("C2-2f") for k, v in sf.UNIONS.items() if k[2].rsplit("/", 1)[-1] in
                   DOMAIN_LEAVES | {"competitor_domains[]"})


@pytest.mark.parametrize("d", NON_HOSTNAMES)
def test_a_non_hostname_is_domain_invalid(d):
    assert svc_tenants.domain_invalid(d) and sg.classify(d) != "hostname"


@pytest.mark.parametrize("d", HOSTNAMES)
def test_a_hostname_is_not_domain_invalid(d):
    assert not svc_tenants.domain_invalid(d) and sg.classify(d) == "hostname"


class _NoWrite:
    """Any attribute access (lock, tenants, _commit, ...) fails: the refusal must come before any state is touched."""
    def __getattr__(self, name):
        raise AssertionError(f"touched {name} before refusing")


def _reason(fn):
    from errors import Invalid
    with pytest.raises(Invalid) as e:
        fn()
    assert e.value.status_code == 422
    return e.value.reason


@pytest.mark.parametrize("d", NON_HOSTNAMES)
def test_every_api_entry_refuses_a_non_hostname_422_before_any_write(d):
    assert _reason(lambda: svc_tenants.TenantsMixin.set_domains(_NoWrite(), "t1", {"domains": [d]})) == "DOMAIN_INVALID"
    body = {"name": "set-one", "brand_terms": ["acme"], "competitor_domains": ["site.test", d], "engines": ["openai"],
            "prompts": ["best shoes"]}
    assert _reason(lambda: svc_audits._strict_prompt_set(body)) == "DOMAIN_INVALID"
    t = {"domains": ["site.test"], "kind": "own"}
    assert _reason(lambda: svc_audits._authorize(t, {"domain": d}, True)) == "DOMAIN_INVALID"


def test_controls_hostnames_pass_the_domain_entries():
    body = {"name": "set-one", "brand_terms": ["acme"], "competitor_domains": HOSTNAMES[:5], "engines": ["openai"],
            "prompts": ["best shoes"]}
    svc_audits._strict_prompt_set(body)
    from errors import Forbidden
    with pytest.raises(Forbidden):       # a hostname that is not registered keeps its 403
        svc_audits._authorize({"domains": ["site.test"], "kind": "own"}, {"domain": "example.com"}, True)


def test_c22g_count_of_generated_accepted_domains_that_change(monkeypatch):
    """The C2-2f corpus (seed 20261114): which formerly accepted domains now get 422. Registration already refused
    them (valid_domain), so 0 change there; competitor domains (no valid_domain before) lose the non-hostnames.
    Re-pinned by C2-6c per G-7 (lead ruling 22:50 PT F6b-1): the C2-6b code-shape rule (T-5 step 3) makes the sink
    refuse 5,787 more generated junk strings, so 8566 -> 2779, 7824 -> 2037, code 7135 -> 1348. The assertion the
    re-pin rests on is checked below, not assumed: every value whose verdict changed was already refused 422
    (domain_invalid) before, and no value the API accepts is newly refused (F2e-1 holds)."""
    rx = re.compile(models.DOMAIN)
    corpus = sorted({x for x in domains(random.Random(SEED)) if rx.fullmatch(x)})
    before = [x for x in corpus if not svc_audits._domain_refused(x)]
    assert len(before) == 2779                                   # C2-6b + ruling 22:50 PT F6b-1 (was 8566)
    reg_before = [x for x in before if svc_tenants.valid_domain(x)]
    assert sum(svc_tenants.domain_invalid(x) for x in reg_before) == 0
    changed = [x for x in before if svc_tenants.domain_invalid(x)]
    from collections import Counter
    assert len(changed) == 2037                                  # was 7824
    assert Counter(sg.classify(x) for x in changed) == {"code": 1348, "number": 587, "version": 87, "amount": 9,
                                                         "hash": 6}   # code was 7135; the other classes unchanged
    with monkeypatch.context() as m:                             # the guard without the C2-6b shape rule
        m.setattr(sg, "code_shape_refusal", lambda s: None)
        before_c26b = [x for x in corpus if not svc_audits._domain_refused(x)]
    assert len(before_c26b) == 8566                              # the former pin, reproduced
    assert set(before) <= set(before_c26b)                       # the rule only refuses, never newly accepts
    newly = sorted(set(before_c26b) - set(before))
    assert len(newly) == 8566 - 2779
    assert all(svc_tenants.domain_invalid(x) for x in newly)     # every changed verdict was 422 before
    api_ok = [x for x in corpus if not svc_tenants.domain_invalid(x)]
    assert not set(api_ok) & set(newly)                          # no API-accepted id is newly refused
    assert {sg.classify(x) for x in before if not svc_tenants.domain_invalid(x)} == {"hostname"}
