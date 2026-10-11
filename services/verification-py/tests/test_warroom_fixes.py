"""
War room fixes (ADR 0018; devtools/warroom/findings.md): WR-F005, the under-18 lock and lookalike / invisible
characters in a minor's e-mail address; WR-F007, a ban recorded before WR-F005 and the mailbox variants it covered. Each test fails on the war room's base (b467f24) except the no-churn and
legacy-record tests, which pin that the fix changes nothing that was already folded.
"""

from __future__ import annotations

import pytest

import lookalikes
from intelligences import i07_duplicate_identity as i07
from clock import iso
from helpers import ANDRE_TOKEN, Harness, rid
from ports import AgeProviderAnswer


def _minor(hr, sid="kid", email="kid.name@gmail.com"):
    hr.identity(sid, email)
    hr.age.answer = AgeProviderAnswer("minor", None, None, True, "p", "r")
    hr.ok(hr.age_check(sid, dob="2012-01-01"))
    hr.age.answer = AgeProviderAnswer("adult", None, None, True, "p", "r2")


@pytest.mark.parametrize("variant", [
    "kiԁ.ηame@gmail.com",                 # the war room's replay (Cyrillic komi de, Greek eta)
    "kid.ηame@gmail.com",                 # Greek eta alone
    "kiԁ.nαme@gmail.com",
    "k\u00adid.name@gmail.com",           # soft hyphen
    "kid\u200b.name@gmail.com",           # zero-width space
    "kid.na\u2060me@gmail.com",           # word joiner
    "\ufeffkid.name@gmail.com",           # BOM
    "kid.name\u200d@gmail.com",           # zero-width joiner
    "ｋｉｄ.ｎａｍｅ@gmail.com",
    "kiԁ.ηame+x@googlemail.com",
])
def test_wr_f005_minor_lock_folds_lookalikes_and_invisible_characters(hr, variant):
    _minor(hr)
    hr.identity("kid2", variant)
    assert hr.ok(hr.age_check("kid2"))["status"] == "minor"


def test_wr_f005_zeta_and_final_sigma_fold():
    assert i07.mailbox_base("ζoe@example.com") == i07.mailbox_base("zoe@example.com")
    assert i07.mailbox_base("luςy@example.com") == i07.mailbox_base("lucy@example.com")
    assert i07.mailbox_base("LUΣY@example.com") != i07.mailbox_base("lucy@example.com")   # capital sigma is not c


def test_wr_f005_an_unrelated_adult_is_not_locked(hr):
    _minor(hr)
    hr.identity("other", "kid.names@gmail.com")
    assert hr.ok(hr.age_check("other"))["status"] == "adult"


def test_wr_f005_no_churn_for_any_address_the_old_fold_already_folded():
    """Every code point the pre-fix fold turned into ASCII gives the same canonical mailbox now, so every
    email_base HMAC stored for such an address still matches."""
    changed = []
    for cp in range(0x80, 0x30000):
        if 0xD800 <= cp < 0xE000:
            continue
        addr = f"x{chr(cp)}y@example.com"
        old = i07.mailbox_base_v0(addr)
        if old.isascii() and i07.mailbox_base(addr) != old:
            changed.append(hex(cp))
    assert changed == []
    for addr in ("kid@example.com", "Kid.Name+tag@GoogleMail.com.", "kіd-alt@example.com", "José@example.com"):
        assert i07.mailbox_base(addr) == i07.mailbox_base_v0(addr)


def test_wr_f005_v0_hmac_kept_only_when_the_fold_changed(hr):
    hr.identity("plain", "kid.name@gmail.com")
    assert "email_base_v0" not in hr.svc.identities["plain"]["hmacs"]
    hr.identity("eta", "kiη@example.com")
    assert "email_base_v0" in hr.svc.identities["eta"]["hmacs"]


def test_wr_f005_a_minor_recorded_before_the_fix_keeps_every_match_it_had(hr):
    """A minor whose address had a letter the old fold left alone ("kiη@") was stored with the old email_base only.
    Its variants that the old fold unified ("kiη+2@") are still locked; that a NEW-fold-only variant ("kin@") is not
    is the documented limit (V&I keeps no raw address; ADR 0007 "War room fixes")."""
    _minor(hr, "old-kid", "kiη@example.com")
    hm = hr.svc.clipper_hmacs["old-kid"]
    v0 = next(h for k, h in hm if k == "email_base_v0")
    for k in [k for k in hm if k[0] in ("email_base", "email_base_v0")]:
        hm.discard(k)
    hm.add(("email_base", v0))                       # exactly what a pre-fix identity check stored
    hr.identity("kid-new-only", "kin@example.com")                       # the documented limit
    assert hr.ok(hr.age_check("kid-new-only"))["status"] == "adult"
    hr.identity("kid-plus", "kiη+2@example.com")
    assert hr.ok(hr.age_check("kid-plus"))["status"] == "minor"


def test_wr_f005_the_shared_table_is_the_top_layer_below_this_services_own():
    t = lookalikes.Table({chr(k): v for k, v in i07._LOOKALIKE.items()})
    for ch, v in lookalikes.SHARED.items():
        if ord(ch) not in i07._LOOKALIKE:
            assert t.mapping[ch] == v
    for k, v in i07._LOOKALIKE.items():
        assert t.mapping[chr(k)] == v


def _pre_fix_identity(hr, sid, email):
    """What a pre-WR-F005 identity check stored for ``email``: the old fold's email_base, no email_base_v0."""
    hr.identity(sid, email)
    hm = hr.svc.clipper_hmacs[sid]
    v0 = next(h for k, h in hm if k == "email_base_v0")
    for k in [k for k in hm if k[0] in ("email_base", "email_base_v0")]:
        hm.discard(k)
    hm.add(("email_base", v0))


def _ban(hr, sid):
    hr.ok(hr.post("/vi/v1/bans", {"request_id": rid(), "clipper_id": sid, "cn_decision_id": f"d-{sid}",
                                  "approved_at": iso(hr.clock.now())}, caller="clipper_network", andre=ANDRE_TOKEN))


def test_wr_f007_a_ban_recorded_before_the_fix_still_blocks_a_variant_the_old_fold_unified(hr):
    """AEGIS H2: the ban stored the old ``email_base`` HMAC; the new identity check carries that value as
    ``email_base_v0``, and the ban lookup compared kinds literally, so ``kiη+2@`` re-entered clear."""
    _pre_fix_identity(hr, "evil", "kiη@example.com")
    _ban(hr, "evil")
    assert ["email_base", next(h for k, h in hr.svc.clipper_hmacs["evil"] if k == "email_base")] \
        in hr.svc.bans["evil"]["blocked"]
    r = hr.identity("evil2", "kiη+2@example.com")
    assert r["status"] == "finding" and r["findings"]
    assert hr.svc.findings[r["findings"][0]]["code"] == "DUPLICATE_IDENTITY"


def test_wr_f007_a_ban_after_the_fix_blocks_both_folds(hr):
    hr.identity("evil", "kiη@example.com")
    _ban(hr, "evil")
    assert hr.identity("evil2", "kiη+2@example.com")["status"] == "finding"
    assert hr.identity("evil3", "kin+3@example.com")["status"] == "finding"    # the new fold reads kiη as kin


def test_wr_f007_a_pre_fix_ban_still_blocks_after_a_restart(tmp_path):
    x = Harness(data_dir=str(tmp_path / "d"))
    x.approve_rules()
    _pre_fix_identity(x, "evil", "kiη@example.com")
    _ban(x, "evil")
    y = Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock, fakes=x.fakes)
    assert y.identity("evil2", "kiη+2@example.com")["status"] == "finding"


def test_wr_f007_an_unrelated_address_is_not_blocked_by_a_ban(hr):
    _pre_fix_identity(hr, "evil", "kiη@example.com")
    _ban(hr, "evil")
    assert hr.identity("someone", "kim@example.com")["status"] == "clear"
