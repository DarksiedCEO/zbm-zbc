"""core-spec/1 O-4 H and H-1: the six host lanes (sink class, tenant registration, competitor registration,
inspect_url host, check, checked_host) on the CI host draw and the real hosts, against the frozen baselines:
  vs ffd28ca8   newly refused only the label's own words (O-2 oracle) and residual (a) (Thai) in the inspect / check /
                checked_host lanes; 0 in the sink, registration and competitor lanes
  vs 2b26d980   (P1 of review 98b76cde) 0 newly refused in all six lanes
plus the H-1 shapes: the P2 shapes refused at registration (0 accepted), the hash-label exemption, a credential word
in a label is a name, a Unicode host label holding a script label is refused in inspect / check / checked_host."""
import random

import pytest

from conformance import baseline as B
from conformance import core_forms as F
from conformance import corpora as C
from conformance.oracle.core_own_words import own_words

pytestmark = pytest.mark.conformance
STRICT_LANES = ("inspect_url", "check", "checked_host")


@pytest.fixture(scope="module")
def mods():
    from agents import callum
    from agents import secret_guard

    import sink_guard
    import svc_audits
    import svc_tenants
    return {"sg": secret_guard, "sink": sink_guard, "tenants": svc_tenants, "audits": svc_audits, "callum": callum}


def unicode_form(h: str) -> str:
    out = []
    for label in h.split("."):
        if label.startswith("xn--"):
            try:
                label = label[4:].encode("ascii").decode("punycode")
            except UnicodeError:
                pass
        out.append(label)
    return ".".join(out)


def _compare(mods, items, release, name):
    base = B.read(name, release, C.stream_hash(items))
    new: dict = {}
    allowed = 0
    for h, b in zip((x[1] if isinstance(x, tuple) else x for x in items), base):
        got = C.lanes_host(mods, h)
        for lane, g, x in zip(C.HOST_LANES, got, b.split("|")):
            if x == "1" and not g:
                if release == "2b26d980" and p2_class(h):
                    allowed += 1                       # (H-1: a 9+ digit run with < 4 hex letters is the P2 class)
                    continue
                u = unicode_form(h)
                if release == "ffd28ca8" and lane in STRICT_LANES and (own_words(u) or any(
                        "\u0e00" <= c <= "\u0e7f" for c in u)):
                    allowed += 1
                    continue
                new[lane] = new.get(lane, 0) + 1
    return new, allowed


@pytest.mark.parametrize("release", ["ffd28ca8", "2b26d980"])
@pytest.mark.parametrize("name", ["hosts", "real_hosts"])
def test_core_O_4_H_no_host_newly_refused(mods, release, name):
    items = C.hosts() if name == "hosts" else C.real_hosts()
    new, allowed = _compare(mods, items, release, name)
    print(f"O-4 H {name} vs {release}: hosts={len(items)} newly refused={new} allowed (own words / Thai)={allowed}")
    assert not new


def p2_class(h: str) -> bool:
    """A label holding a run of 9+ digits with fewer than 4 hex letters around it (decision 64): the telephone / SSN
    shapes that H-1 requires refused at registration, which 2b26d980 took."""
    import re
    for label in h.split("."):
        if re.search(r"[0-9]{9,}", label.replace("a", "").replace("b", "").replace("c", "").replace("d", "")
                     .replace("e", "").replace("f", "")) and sum(c in "abcdef" for c in label) < 4:
            return True
    return False


def test_core_H_1_p2_shapes_refused_at_registration(mods):
    import test_review_98b76cde as r98
    accepted = {}
    for name, gen in sorted(r98.P2_SHAPES.items()):
        r = random.Random(f"{C.SEED}:p2:{name}")
        hosts = [gen(r) for _ in range(200)]
        accepted[name] = sum(1 for h in hosts if mods["tenants"].valid_domain(h) and not mods["tenants"].domain_refused(h))
    print(f"H-1 P2 shapes accepted at registration: {accepted}")
    assert all(v == 0 for v in accepted.values()), accepted


def test_core_H_1_hash_labels_and_credential_words_are_names(mods):
    r = random.Random(f"{C.SEED}:h1")
    hashes = ["".join(r.choice("0123456789abcdef") for _ in range(12)) + "abcd"[: r.randint(0, 4)] for _ in range(200)]
    hashes = [h for h in hashes if sum(c in "abcdef" for c in h) >= 4]
    hosts = [f"shop-{x}.webflow.io" for x in hashes] + ["pass" + "word-manager.example", "token-shop.example",
                                                          "my-secret-garden.example", "pin-art.example"]
    bad = [h for h in hosts if mods["sink"].classify(h) != "hostname" or mods["tenants"].domain_refused(h)]
    print(f"H-1 names: hosts={len(hosts)} refused in sink / registration={len(bad)}")
    assert not bad, bad[:5]


def test_core_H_1_a_unicode_label_holding_a_script_label_is_refused(mods):
    """A Unicode host label holding a script label (as listed): R in inspect / check / checked_host."""
    sg, callum = mods["sg"], mods["callum"]
    bad = []
    for eid in ("S00", "S12", "S16", "S21", "S22"):
        lab = F.by_id()[eid][0].replace(" ", "-")
        h = f"{lab}.example.com"
        u = sg.inspect_url(f"https://{h}/x")
        took = (sg.check(h) is None, bool(u) and u.get("host_kind") is None,
                callum.checked_host(h) == {"host": h, "host_withheld": None})
        if any(took):
            bad.append((eid, took))
    assert not bad, bad
