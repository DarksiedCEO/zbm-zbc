"""Shared reference data: registry seed, row usability, rights records, actors, stand-ins, media plug points."""

from datetime import date

import pytest

from samples import TODAY, zbm_clearance
from shared import departments, media
from shared.actors import ActorRegistry, Role
from shared.errors import GuardrailViolation, PreconditionFailed, RegistryRowBlocked
from shared.registry import SHELF_LIFE_DAYS, RowOwner, RowStatus, check_usable, seeded_registry
from shared.rights import ClearanceRecord, record_clearance


def test_seed_is_exactly_the_sourced_rows():
    reg = seeded_registry()
    verified = {r.row_id: r for r in reg.rows.values() if r.status is RowStatus.VERIFIED}
    assert {(r.platform, r.rule_key, r.source_url) for r in verified.values()} == {
        ("youtube", "max_length_seconds", "https://support.google.com/youtube/answer/15424877"),
        ("instagram", "recommended_max_length_seconds", "https://creators.instagram.com/blog/tips-for-improving-your-reach"),
        ("instagram", "originality_repost_watermark", "https://creators.instagram.com/blog/tips-for-improving-your-reach"),
        ("youtube", "originality_reused_content", "https://support.google.com/youtube/answer/1311392"),
    }
    assert SHELF_LIFE_DAYS == 30
    assert all(r.verified_at == date(2026, 9, 23) and r.expires_at == date(2026, 10, 23) for r in verified.values())
    assert not any(r.platform == "tiktok" and r.status is RowStatus.VERIFIED for r in reg.rows.values())
    owners = {r.row_id: r.owner for r in reg.rows.values()}
    assert owners["yt-shorts-max-length"] is RowOwner.ZBM_PLACEMENT_SPEC
    assert owners["yt-reused-content-monetization"] is RowOwner.ZBC_PLATFORM_RULES


def test_usability_boundaries():
    row = seeded_registry().get("yt-shorts-max-length")
    assert check_usable(row, date(2026, 9, 23)).usable
    assert check_usable(row, date(2026, 10, 22)).usable
    assert not check_usable(row, date(2026, 10, 23)).usable  # expires_at is the first blocked day
    assert not check_usable(row, date(2026, 9, 22)).usable   # verified in the future


def test_require_usable_blocks_with_reason():
    reg = seeded_registry()
    with pytest.raises(RegistryRowBlocked, match="unverified"):
        reg.require_usable("tiktok-originality-unverified", TODAY)
    with pytest.raises(RegistryRowBlocked, match="no such row"):
        reg.require_usable("nope", TODAY)
    assert reg.require_usable("yt-shorts-max-length", TODAY).value == 180


def test_rights_records_append_only_and_role_gated(rights, recorder, actors, ledger):
    rec = ClearanceRecord.model_validate(zbm_clearance())
    with pytest.raises(GuardrailViolation):
        record_clearance(rights, recorder, actors, "zbm_creative_lead", rec)
    record_clearance(rights, recorder, actors, "rights_desk", rec)
    with pytest.raises(PreconditionFailed):
        record_clearance(rights, recorder, actors, "rights_desk", rec)
    assert len(ledger.of_type("rights_record_added")) == 1


def test_rights_record_not_stored_when_ledger_fails(rights, recorder, actors, ledger):
    from shared.ledger import LedgerRecordError

    ledger.fail_next = True
    with pytest.raises(LedgerRecordError):
        record_clearance(rights, recorder, actors, "rights_desk", ClearanceRecord.model_validate(zbm_clearance()))
    assert rights.records == {}


def test_actor_registry_rules(monkeypatch):
    reg = ActorRegistry()
    with pytest.raises(ValueError):
        reg.add("andre", [Role.ZBM_CREATIVE_LEAD])
    with pytest.raises(ValueError):
        reg.add("Not Valid", [Role.ZBM_CREATIVE_LEAD])
    monkeypatch.setenv("CREATIVE_EXTRA_ACTORS", '{"jo": ["zbm_creative_lead"]}')
    assert Role.ZBM_CREATIVE_LEAD in ActorRegistry.from_env().get("jo").roles


def test_every_stand_in_fails_closed():
    d = departments.Departments()
    assert d.compliance.review("x", "1", {}).allowed is False
    assert d.verification.attest_clip("1", {}).verified is False
    assert d.verification.attest_result("1", {}).verified is False
    assert d.legal.signoff("t", "1", {}).allowed is False
    assert d.finance.accept_payout_handoff("1", {}).allowed is False
    assert d.clipper_network.announce_rulebook_version("c", 1, {}).allowed is False
    req = departments.CommissionRequest("r", "zbm", "enigma", {})
    assert d.creative_agents.commission(req).commissioned is False
    for gate in (d.compliance.review("x", "1", {}), d.legal.signoff("t", "1", {}), d.finance.accept_payout_handoff("1", {})):
        assert "not allowed yet" in gate.reason


def test_media_plug_points_are_not_integrated():
    for stand_in, call in ((media.PySceneDetectStandIn(), lambda s: s.detect("m")),
                           (media.WhisperStandIn(), lambda s: s.transcribe("m")),
                           (media.KinocutStandIn(), lambda s: s.cut("m", 0, 1)),
                           (media.ChromaprintStandIn(), lambda s: s.fingerprint("m")),
                           (media.C2paStandIn(), lambda s: s.stamp("m", {}))):
        out = call(stand_in)
        assert isinstance(out, media.NotIntegrated) and out.integrated is False


def test_no_video_library_imported_anywhere():
    import ast
    from pathlib import Path

    banned = {"scenedetect", "faster_whisper", "whisperx", "c2pa", "acoustid", "chromaprint", "cv2", "av", "moviepy",
              "kinocut", "ffmpeg"}
    for f in (Path(__file__).resolve().parents[1] / "src").glob("**/*.py"):
        tree = ast.parse(f.read_text())
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                names.add(n.module.split(".")[0])
        assert not (names & banned), f
