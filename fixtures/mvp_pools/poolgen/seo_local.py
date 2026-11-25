"""SEO local layer (test-set spec v0, "SEO corpus ... plus a local-business layer"): a citation network for SEO-L4
(NAP / citation audit) and listing pairs for SEO-L5 (duplicate listing suppression).

L4: 200 businesses, each with a canonical NAP record (what the owner confirmed) and 15-45 listings across 40 fictional
directories. Each listing is either correct, correct with a formatting-only difference (NOT a fault: "St." vs
"Street", suite formats, phone punctuation, a toll-free number the owner lists too, case, http/https/www), or carries
one planted fault: wrong name, wrong address, wrong phone, or marked closed while the business is open.

L5: 200 listing pairs: a true duplicate, a business that moved (old listing should be marked moved), and three that are
NOT duplicates: a practitioner listing inside a practice, a department listing, and two different businesses at the
same address.
"""

from __future__ import annotations

from . import common
from .common import Stream

POOL_ID = "seo-local"
BUSINESSES = 200
DIRECTORY_COUNT = 40
L5_PAIRS = 200
CITIES = (("Phoenix", "AZ", "85004", "602"), ("Mesa", "AZ", "85201", "480"), ("Las Vegas", "NV", "89101", "702"),
          ("Portland", "OR", "97204", "503"), ("Seattle", "WA", "98101", "206"),
          ("Los Angeles", "CA", "90012", "213"), ("Long Beach", "CA", "90802", "562"),
          ("San Diego", "CA", "92101", "619"))
STREET_NAMES = ("Fixture", "Sample", "Testwell", "Mockridge", "Example", "Placeholder", "Synthetic", "Demo")
STREET_TYPES = (("Street", "St"), ("Avenue", "Ave"), ("Boulevard", "Blvd"), ("Road", "Rd"), ("Drive", "Dr"))
TRADES = ("Plumbing", "Dental", "Auto Repair", "Med Spa", "Law Group", "Heating & Air", "Insurance", "Bistro")
FAULTS = ("wrong_name", "wrong_address", "wrong_phone", "closed_listing")
FORMAT_VARIANTS = ("street_abbrev", "suite_format", "phone_punctuation", "toll_free_alt", "case", "website_form",
                   "ampersand")


def _directories() -> list[dict]:
    kinds = ("data_aggregator", "map", "review_site", "vertical_directory", "local_chamber")
    return [{"directory_id": f"dir-{i:02d}", "host": f"directory-{i:02d}.test", "kind": kinds[i % len(kinds)]}
            for i in range(DIRECTORY_COUNT)]


def _phone_fmt(e164: str, style: int) -> str:
    a, b, c = e164[2:5], e164[5:8], e164[8:]
    return (f"({a}) {b}-{c}", f"{a}-{b}-{c}", f"{a}.{b}.{c}", e164, f"1-{a}-{b}-{c}")[style % 5]


def _canonical(s: Stream, i: int) -> dict:
    city, state, zip_, area = CITIES[i % len(CITIES)]
    st_full, st_abbr = s.pick(STREET_TYPES)
    has_suite = s.chance(1, 2)
    return {
        "name": f"{s.pick(STREET_NAMES)} {s.pick(TRADES)} {s.between(100, 999)}",
        "street_number": str(s.between(100, 9899)), "street_name": s.pick(STREET_NAMES),
        "street_type": st_full, "street_type_abbrev": st_abbr,
        "suite": str(s.between(1, 40)) if has_suite else None,
        "city": city, "state": state, "zip": zip_,
        "phone": common.fixture_phone(area, s.below(50)),
        "toll_free": common.fixture_phone("888", s.below(50)) if s.chance(1, 3) else None,
        "website": f"https://www.{common.slugify(s.pick(STREET_NAMES))}-{s.between(1000, 9999)}.test/",
        "open": True,
    }


def _render_listing(c: dict, s: Stream, variants: set[str], fault: str | None) -> dict:
    """A directory listing as the directory shows it: one flat NAP block of strings."""
    name = c["name"]
    if "ampersand" in variants and "&" in name:
        name = name.replace("&", "and")
    if "case" in variants:
        name = name.upper()
    st = c["street_type_abbrev"] + "." if "street_abbrev" in variants else c["street_type"]
    street = f"{c['street_number']} {c['street_name']} {st}"
    if c["suite"]:
        style = s.pick(["Suite {}", "Ste {}", "Ste. {}", "#{}", "Unit {}"]) if "suite_format" in variants else "Suite {}"
        street += ", " + style.format(c["suite"])
    phone = c["phone"]
    if "toll_free_alt" in variants and c["toll_free"]:
        phone = c["toll_free"]
    phone = _phone_fmt(phone, s.below(5) if "phone_punctuation" in variants else 0)
    website = c["website"]
    if "website_form" in variants:
        website = s.pick([website.replace("https://", "http://"), website.rstrip("/"),
                          website.replace("www.", "")])
    status = "open"
    if fault == "wrong_name":
        name = name + " " + s.pick(["Express", "Pros", "Group", "LLC Old Name"])
    elif fault == "wrong_address":
        street = f"{int(c['street_number']) + s.between(10, 900)} {c['street_name']} {st}"
    elif fault == "wrong_phone":
        alt = (int(c["phone"][-2:]) + s.between(1, 49)) % 50
        phone = _phone_fmt(common.fixture_phone(c["phone"][2:5], alt), 0)
    elif fault == "closed_listing":
        status = "permanently_closed"
    return {"name": name, "street": street, "city": c["city"], "state": c["state"], "zip": c["zip"],
            "phone": phone, "website": website, "status": status}


def generate_l4(seed: str) -> tuple[list[dict], list[dict]]:
    root = Stream(f"{POOL_ID}|l4|{seed}")
    dirs = _directories()
    subjects, key = [], []
    for i in range(BUSINESSES):
        s = root.child(f"b{i}")
        biz_id = common.opaque_id("loc", seed, i)
        canon = _canonical(s.child("canon"), i)
        n_listings = s.between(15, 45)
        chosen = s.shuffled(dirs)[:n_listings]
        listings, truth = [], []
        control = i % 10 == 9            # 1 in 10 businesses: every listing correct (formatting variants allowed)
        for j, d in enumerate(chosen):
            ls = s.child(f"l{j}")
            fault = None if control or not ls.chance(1, 5) else ls.pick(FAULTS)
            variants = {v for v in FORMAT_VARIANTS if ls.chance(1, 4)}
            lid = common.opaque_id("cit", seed, i, j)
            listings.append({"citation_id": lid, "directory_id": d["directory_id"],
                             "url": f"https://{d['host']}/biz/{lid[-8:]}",
                             **_render_listing(canon, ls, variants, fault)})
            truth.append({"citation_id": lid, "verdict": fault or "correct"})
        subjects.append({"fixture": common.FIXTURE_LABEL, "pool": POOL_ID, "business_id": biz_id,
                         "canonical": canon, "listings": listings})
        key.append({"business_id": biz_id, "citations": truth})
    order = root.child("order").shuffled(list(range(len(subjects))))
    return [subjects[k] for k in order], [key[k] for k in order]


L5_KINDS = (("true_duplicate", True, 50), ("moved_business", True, 40), ("practitioner_listing", False, 40),
            ("department_listing", False, 35), ("different_business_same_address", False, 35))


def generate_l5(seed: str) -> tuple[list[dict], list[dict]]:
    root = Stream(f"{POOL_ID}|l5|{seed}")
    pairs, key = [], []
    n = 0
    for kind, dup, count in L5_KINDS:
        for _ in range(count):
            s = root.child(f"{kind}-{n}")
            c = _canonical(s.child("canon"), n)
            a = _render_listing(c, s.child("a"), set(), None)
            # formatting differences only (a second phone number would need the owner's record to judge)
            b = _render_listing(c, s.child("b"), {v for v in FORMAT_VARIANTS
                                                  if v != "toll_free_alt" and s.chance(1, 3)}, None)
            if kind == "moved_business":
                b["street"] = f"{int(c['street_number']) + s.between(1000, 5000)} {s.pick(STREET_NAMES)} Way"
                b["status"] = "open"
                a["status"] = "open"
                a["last_owner_update_days_ago"], b["last_owner_update_days_ago"] = s.between(400, 900), s.between(1, 60)
            elif kind == "practitioner_listing":
                b["name"] = f"Dr. {s.pick(('Avery', 'Jordan', 'Riley', 'Casey'))} {s.pick(STREET_NAMES)}, DDS"
            elif kind == "department_listing":
                b["name"] = a["name"] + " - " + s.pick(("Service Department", "Parts Department", "Billing Office"))
                b["phone"] = _phone_fmt(common.fixture_phone(c["phone"][2:5], (int(c["phone"][-2:]) + 17) % 50), 0)
            elif kind == "different_business_same_address":
                b["name"] = f"{s.pick(STREET_NAMES)} {s.pick([t for t in TRADES if t not in a['name']])} {s.between(100, 999)}"
                b["phone"] = _phone_fmt(common.fixture_phone(c["phone"][2:5], (int(c["phone"][-2:]) + 23) % 50), 0)
                b["website"] = f"https://www.other-{common.slugify(b['name'])}.test/"
            for x in (a, b):
                x.setdefault("last_owner_update_days_ago", s.between(1, 300))
            pid = common.opaque_id("pair", seed, n)
            pairs.append({"fixture": common.FIXTURE_LABEL, "pool": POOL_ID, "pair_id": pid, "listing_a": a,
                          "listing_b": b})
            key.append({"pair_id": pid, "kind": kind, "is_duplicate": dup,
                        "action": ("merge_or_remove_duplicate" if kind == "true_duplicate" else
                                   "mark_old_listing_moved" if kind == "moved_business" else "leave_both"),
                        "requires_owner_approval": dup})
            n += 1
    order = root.child("order").shuffled(list(range(len(pairs))))
    return [pairs[k] for k in order], [key[k] for k in order]
