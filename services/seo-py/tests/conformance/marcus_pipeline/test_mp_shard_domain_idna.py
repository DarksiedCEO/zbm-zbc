"""MP-C4a2: a manifest's shard domain must be a canonical ASCII A-label host (marcus-pipeline-spec/1 B-MP-22 "host
lower-cased, IDNA"; B-MP-33 manifest; CB-19 refused whole). Lead ruling MP-C4a 23:25 PT: accepted only exactly as
agents/domain_match's strict IDNA round trip produces it (IDNA2003 codec, refuse-if-changed); a U-label, upper case,
a label the round trip changes, a surrogate are refused, never converted. Synthetic values only."""

from __future__ import annotations

import pytest

from agents import domain_match
from conformance.marcus_pipeline import gen_cc
from conformance.marcus_pipeline.util import build, watch
from marcus import index_builder as B

H40, S64 = "a" * 40, "b" * 64
BUECHER_A = "xn--bcher-kva.com"                       # the A-label of "b\u00fccher.com" (synthetic)


def _manifest(domain) -> dict:
    return {"contract": B.CONTRACT, "snapshot_id": "seo-snp-" + H40, "crawls": ["CC-MAIN-2024-10"],
            "built_at": "2026-11-14T00:00:00Z",
            "builder_version": {"version": B.BUILDER_VERSION, "rel_capture": "UNVERIFIED", "spike_ref": None},
            "shards": [{"shard_id": "seo-shd-" + H40, "sha256": S64, "domain": domain, "rows": 1}],
            "counts": {"rows": 1}, "licence_ack": B.LICENCE_ACK, "complete": True, "previous_snapshot_id": None,
            "crawled_sources": {"previous_snapshot_id": None, "sha256": S64, "crawled": 0, "not_crawled": 0},
            "key_id": "k1"}


def _refused(domain) -> None:
    with pytest.raises(B.ContractInvalid):
        B.validate_manifest(_manifest(domain))


@pytest.mark.parametrize("domain", ("example.com", "example.co.uk", BUECHER_A, "a" * 63 + ".com"))
def test_B_MP_22_a_canonical_ascii_shard_domain_is_accepted(domain):
    assert B.canonical_domain(domain) is True
    assert B.validate_manifest(_manifest(domain))["shards"][0]["domain"] == domain


def test_B_MP_22_a_valid_u_label_is_refused_its_a_label_accepted_never_converted():
    u = "b\u00fccher.com"
    assert domain_match._ascii(u) == BUECHER_A                 # the U-label is a valid IDNA name ...
    assert B.canonical_domain(u) is False                      # ... but not canonical: refused, not converted
    _refused(u)
    assert B.validate_manifest(_manifest(BUECHER_A))["shards"][0]["domain"] == BUECHER_A


@pytest.mark.parametrize("domain", (
    "stra\u00dfe.com",            # sharp s: the IDNA2003 round trip changes it (-> strasse)
    "\u03c3\u03bf\u03c2.com",     # final sigma
    "ab\u200dc.com",              # zero width joiner
    "xn--strae-oqa.com",          # A-label of sharp-s name: decodes to a label the round trip changes
    "xn--zz.com",                 # not valid punycode
    "\ud800.com", "a\udfff.com",  # lone surrogates
))
def test_B_MP_22_an_invalid_u_label_or_surrogate_is_refused_whole(domain):
    assert B.canonical_domain(domain) is False
    _refused(domain)


@pytest.mark.parametrize("domain", ("Example.com", "EXAMPLE.COM", "XN--BCHER-KVA.COM", "Xn--bcher-kva.com"))
def test_B_MP_22_upper_case_is_refused_not_normalised(domain):
    # B-MP-22 "host lower-cased" is the builder's normalisation (link_extractor / index_builder via domain_match);
    # the manifest is a contract document (CB-19), so non-canonical case is refused, not lower-cased (ruling 23:25).
    assert domain_match._ascii(domain) == domain.lower()
    assert B.canonical_domain(domain) is False
    _refused(domain)


@pytest.mark.parametrize("domain", ("example.com.", " example.com", "example.com ", "example..com", ".example.com",
                                    "", "ex ample.com", "example.com/x", None, 7, b"example.com"))
def test_B_MP_22_trailing_dot_space_empty_label_and_non_strings_are_refused(domain):
    assert B.canonical_domain(domain) is False
    _refused(domain)


def test_B_MP_22_label_length_boundary_63_accepted_64_refused():
    assert B.canonical_domain("a" * 63 + ".com") is True
    assert B.validate_manifest(_manifest("a" * 63 + ".com"))
    assert B.canonical_domain("a" * 64 + ".com") is False
    _refused("a" * 64 + ".com")


def test_B_MP_22_total_length_boundary_253_canonical_254_refused():
    n253 = ".".join(["a" * 63] * 3 + ["b" * 61])
    n254 = ".".join(["a" * 63] * 3 + ["b" * 62])
    assert (len(n253), len(n254)) == (253, 254)
    assert B.canonical_domain(n253) is True                    # the canonical-form rule's own boundary ...
    assert B.canonical_domain(n254) is False
    _refused(n254)
    _refused(n253)                                             # ... a 253-char name is still not registrable (B-MP-32)


def test_B_MP_22_every_builder_produced_manifest_has_canonical_shard_domains(mp):
    p = [gen_cc.Page(f"s{i}.example", ("a",), [gen_cc.Link(t, ("p", f"x{i}")) for t in ("acme.test", "globex.test")])
         for i in range(3)]
    gen_cc.write_crawl(mp.fx, gen_cc.CRAWLS[0], [p])
    watch(mp)
    man = mp.svc.index_store.read_manifest(build(mp)["snapshot_id"])
    doms = [s["domain"] for s in man["shards"]]
    assert sorted(set(doms)) == ["acme.test", "globex.test"]
    assert all(B.canonical_domain(d) and domain_match._ascii(d) == d for d in doms)
    assert B.validate_manifest(man) is not None
