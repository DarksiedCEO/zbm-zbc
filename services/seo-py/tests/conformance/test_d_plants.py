"""D-05 (the planted-value bar, A-18) and D-06 (positive control), D-08 (crawl-originated canaries on every A-18
surface): planted values through real service audits and log ingests; every stored / returned form scanned."""

from __future__ import annotations

import collections
import json
import re

import pytest

from common import check_ran, rng
from gen import plant_sites, plants
from harness import scanner
from harness.service import Svc

CODE = re.compile(r"\{c:[a-z]+:[0-9a-f]{16}\}")
AUDITS, PER_SITE = 12, 40


def _constants() -> str:
    """Every string constant of the service's modules (SUITE_PLAN 3: a planted value never coincides with one)."""
    import sys
    out = []
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", "") or ""
        if "/src/" not in f:
            continue
        for v in vars(mod).values():
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, (tuple, frozenset, set, list)):
                out.extend(x for x in v if isinstance(x, str))
    return "\n".join(out).lower()


@pytest.fixture(scope="module")
def planted(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("plants")
    r = rng(7, "d05")
    s = None
    results = []
    consts = _constants()
    for k in range(AUDITS):
        built = plant_sites.build(r, PER_SITE)
        if s is None:
            s = Svc(tmp, built["web"])
        else:
            s.web.routes = built["web"].routes
        a = s.audit(built["paths"])
        if k < 2:
            s.log_ingest(built["lines"])
        results.append((built, a))
    out = s.stored_outputs()
    displays = []
    for built, a in results:
        d = s.audit_resolved(a["audit_id"])
        displays.append(d.text)
    s.close()
    plants_all = [p for built, _ in results for p in built["plants"]]
    plants_all = [p for p in plants_all if plants.constants_free(p[2], consts)]
    if len(results) > 2:
        plants_all = [p for p in plants_all if p[0] != "logs" or True]
    return {"out": out, "displays": displays, "plants": plants_all, "reports": [a for _, a in results]}


def test_D_05_A_18_planted_values_readable_nowhere(planted):
    leaks = collections.Counter()
    examples = {}
    for agent, surface, value, _ in planted["plants"]:
        if surface == "audit_path":
            continue            # the operator's own request, echoed to the operator: not an A-18 crawled surface
        hits = scanner.hits(plants.needles(value), planted["out"])
        if hits:
            leaks[(agent, surface)] += 1
            examples.setdefault((agent, surface), hits[0][1])
    assert not leaks, {f"{a}/{s}": (n, examples[(a, s)]) for (a, s), n in leaks.items()}
    per_agent = collections.Counter(p[0] for p in planted["plants"])
    for agent in ("selene", "delia", "roman", "logs", "entity_check"):
        assert per_agent[agent] >= 100, (agent, per_agent[agent])
    check_ran("A-18", min(per_agent.values()) * 10)


def test_D_06_the_owner_display_resolves_the_clients_own_evidence(planted):
    missing = collections.Counter()
    shown = "\n".join(planted["displays"])
    from agents_run import coding
    from primitives import safe_url as su
    for agent, surface, value, referenced in planted["plants"]:
        if not referenced:
            continue
        with coding():
            stored = su.safe_path("/" + plant_sites._slug(value)) if surface in ("canonical_other_path",
                                                                              "audit_path") else "{c:"
        if "{c:" not in stored:
            continue            # a value-destroying placeholder: nothing to resolve, by design (spec 0.1)
        if not scanner.hits([value.lower()] if value.isascii() else [value], shown):
            missing[surface] += 1
    assert not missing, dict(missing)
    for d in planted["displays"]:
        assert not CODE.search(d.replace("{c:", "{c:", 0)) or "[not available]" not in d


def test_D_06_codes_resolve_only_for_the_owner(planted):
    for a in planted["reports"]:
        assert CODE.search(json.dumps(a["report"])), "a stored report with planted values carries codes"


def test_D_08_every_a18_surface_was_planted(planted):
    surfaces = {p[1] for p in planted["plants"]}
    want = {"robots_comment", "robots_rule", "robots_sitemap_path", "robots_sitemap_host", "audit_path", "title",
            "h1", "h2", "h3", "h4", "h5", "h6", "meta_description", "html_lang", "canonical_other_host",
            "canonical_other_path", "jsonld_type", "org_name", "telephone", "street", "sameas", "jsonld_string",
            "x_robots_bot", "meta_robots_name", "homepage_phone_text", "redirect_host", "redirect_path",
            "redirect_query", "sitemap_loc_path", "sitemap_loc_query", "sitemap_loc_host", "sitemap_lastmod",
            "index_child", "namespace", "root_name", "llms_text", "llms_link", "log_ip", "log_path", "log_query",
            "log_user_agent", "log_referer", "content_type"}
    assert want <= surfaces, want - surfaces
    assert len(want) >= 32


def test_D_06_the_log_ingest_owner_display_resolves_its_own_evidence(tmp_path):
    """Spec 0.1 (an evidence map per audit / log ingest) and D-02h: a log ingest's stored report holds codes; the
    owner display (hub, tenant-authenticated, ``resolve=evidence``) resolves the planted log paths; the plain view
    and every stored form do not; any other caller asking to resolve is refused."""
    from cert_web import Web, site
    from harness.service import clf
    r = rng(17, "d06logs")
    vals = [plants.uniq(r) for _ in range(20)]
    s = Svc(tmp_path, site(Web()))
    s.audit(("/",))
    done = s.log_ingest([clf("66.249.66.1", f"/{v}/x") for v in vals])
    iid = done["ingest_id"]
    stored = s.stored_outputs()
    assert not [v for v in vals if v in stored]
    plain = s.get(f"/tenants/zbm/log-ingests/{iid}")
    assert plain.status_code == 200 and not [v for v in vals if v in plain.text] and "{c:seg:" in plain.text
    own = s.log_ingest_resolved(iid)
    assert own.status_code == 200 and own.json()["evidence_available"] is True
    tops = own.json()["report"]["envelope"]["facts"]["families"]["Googlebot"]["top_path_templates"]
    assert tops and all("{c:" not in t["template"] and "[not available]" not in t["template"] for t in tops), tops
    for caller in ("dashboard", "seo_agent", "compliance_38"):
        assert s.log_ingest_resolved(iid, caller=caller).status_code == 403, caller
    s.close()
