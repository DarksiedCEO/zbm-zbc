"""
ZBC intelligence 1a — Rulebook Writer.

Job: DRAFT a campaign rulebook from the client's goal and source material.
Decides: nothing final. It turns the goal into written, numbered rules and
lists every blocking issue it can see. Campaign Rulebook (1) approves; a
different actor is enforced. Andre then signs.

Every rule gets a stable id (`XX-N`, prefix = kind; see zbc.rulebook). Platform-dependent
rules cite the registry rows they rest on (`rationale_row_ids`); a target
platform with no usable length row or no usable originality row is a
BLOCKING issue (e.g. TikTok today: its only row is unverified).

Revision (`revise`): after go-live the rulebook is frozen, so a change is a
NEW version drafted from a full updated goal. Rule identity is by content
(kind + parameters): an unchanged rule keeps its id; a new or changed rule
gets the next unused number for its prefix; ids that disappear are
retired and never reused in this campaign.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.registry import PlatformRulesRegistry
from shared.text import SHORT_ENTRY_LETTERS, canonical
from shared.types import CampaignId, NonEmptyStr, SafeId
from zbc.platform_rules import length_rows, originality_rows
from zbc.rulebook import (
    PREFIX,
    TRANSFORMATION_ELEMENTS,
    Angle,
    PlatformTarget,
    Rule,
    Rulebook,
    RuleKind,
    RulebookStatus,
    format_rule_id,
)

WRITER_ACTOR = "zbc_rulebook_writer"


class AngleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: NonEmptyStr
    description: NonEmptyStr
    keywords: list[NonEmptyStr] = Field(min_length=1, max_length=100)
    hook_lines: list[NonEmptyStr] = Field(default_factory=list, max_length=100)


class NeverSayEntry(BaseModel):
    """A never-say phrase with options (fix wave 6, N3). `fuzzy`: opt a short
    entry (<= shared.text.SHORT_ENTRY_LETTERS letters, e.g. "cure", "scam")
    into the similarity gate, which otherwise matches it exactly only —
    one edit from a 4-letter word is a tenth of ordinary English."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    phrase: NonEmptyStr
    fuzzy: bool = False


class CampaignGoal(BaseModel):
    """What the client funds and supplies. The writer never invents any of it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: CampaignId
    client_id: SafeId
    vertical: NonEmptyStr
    objective: NonEmptyStr
    source_asset_ids: list[SafeId] = Field(min_length=1, max_length=200)
    cleared_asset_ids: list[SafeId] = Field(default_factory=list, max_length=500)
    angles: list[AngleInput] = Field(min_length=1, max_length=20)
    must_say: list[NonEmptyStr] = Field(default_factory=list, max_length=100)
    never_say: list[NonEmptyStr | NeverSayEntry] = Field(default_factory=list, max_length=1000)
    disclosure_any_of: list[NonEmptyStr] = Field(min_length=1, max_length=50)
    platforms: list[PlatformTarget] = Field(min_length=1, max_length=50)
    min_days_live: int = Field(ge=1, le=365)
    min_transformation_elements: int = Field(default=2, ge=1, le=len(TRANSFORMATION_ELEMENTS))
    min_resolution_height_px: int = Field(default=720, ge=240, le=4320)
    campaign_max_length_seconds: int | None = Field(default=None, ge=3, le=3600)
    # Language of the campaign's customer-facing text. Only English rulebooks
    # exist in this build; Clip Review sends any clip whose text contains a
    # letter outside the Latin script to a human (N3).
    language: Literal["en"] = "en"


def _rule(kind: RuleKind, n: int, text: str, params: dict, rows: tuple[str, ...] = ()) -> Rule:
    return Rule(rule_id=format_rule_id(PREFIX[kind], n), kind=kind, text=text, params=params, rationale_row_ids=rows)


def never_say_entries(goal: CampaignGoal) -> list[NeverSayEntry]:
    """The goal's never-say list as entries, de-duplicated by phrase (the
    first spelling of a phrase wins; `fuzzy` if any spelling said so)."""
    out: dict[str, NeverSayEntry] = {}
    for e in goal.never_say:
        e = e if isinstance(e, NeverSayEntry) else NeverSayEntry(phrase=e)
        prev = out.get(e.phrase)
        out[e.phrase] = e if prev is None else prev.model_copy(update={"fuzzy": prev.fuzzy or e.fuzzy})
    return list(out.values())


# Fix wave 10 (AEGIS round 9 N9-3): the most never-say phrases one rulebook may carry. Clip Review's
# cost grows with the list (every phrase is judged by every similarity signal over every text field);
# measured on the reference host (2 vCPU Intel Xeon @ 2.80 GHz, Python 3.11) with the AEGIS round-9
# worst-case generator (probes/ns_cost8b.py's construction, three phrase sets, every field at its
# maximum, the clip routed to a human so no rejection cuts the signals short, a regional indicator in
# every field so the regional-indicator reading runs too; wall clock, every cache cold, one process
# per measurement): 30 phrases 1.47-1.56 s per review (0.88-0.89 s without a regional indicator), 35
# phrases 1.52-1.66 s, 40 1.60-1.79 s, 50 1.80-1.85 s, 100 2.28-2.33 s. Inside the full test run (a
# large heap: more collector time) the same case at 50 phrases measured 2.05 s CPU against 1.69 s
# alone, about 21% more. One review must stay under 2 s with that margin, so 30 (1.56 s x 1.21 =
# 1.89 s; measured inside the full test run at 30: 1.77 s CPU, tests/test_fix_wave_10.py). Most of the cost is fixed (every per-text view built over ~65 KB, twice when the
# regional-indicator reading runs: 10 phrases already cost 1.23 s), so a longer list needs a faster
# gate, not a higher cap. A draft keeps every phrase (nothing is ever dropped silently) and says so;
# Campaign Rulebook refuses to approve it (422) until the list is at most this long. See
# docs/adr/0005 (decision 44, gap 16).
MAX_NEVER_SAY = 30
# Fix wave 11 (AEGIS round 10 N10-4): the count cap alone did not bound the cost — 30 phrases of 60 words
# cost 37.9 s CPU per review, 30 phrases of eight two-letter words 6.9 s. A phrase is also capped in
# words and characters, and the whole list in characters. Chosen from measurement on the reference host
# (fix11 probes/cost11.py: every field at its maximum, the phrases' own words mutated — 1 in 8 spelled
# in regional indicators, 1 in 8 in currency / math symbols — a regional indicator in every field so the
# regional-indicator reading runs, routed to a human, one fresh process per measurement, thread CPU, two
# seeds, two text modes):
#   within the caps  15 x 3 words (<= 20 c) 1.34-1.49 s; 16 x 3 (<= 18 c) 0.86-1.37 s; 20-21 x 3 (<= 14-15 c)
#                    0.91-1.70 s; 10 x 3 long words (<= 30 c) 0.74-1.25 s; 15 x 3 English words 0.83-1.39 s;
#                    30 x 3 two-letter words 0.41-0.62 s; 30 x 2 words 0.46-0.64 s  -> worst 1.70 s alone;
#                    1.94-1.98 s CPU inside the full test run (a large heap; tests/test_fix_wave_11.py)
#   over a cap       4 words a phrase: 16 x 4 (382 c) 1.47-1.93 s, 12 x 4 (<= 25 c, 290 c) 1.62-1.72 s,
#                    11 x 4 English words 1.74-1.77 s; 400 c of 3 words (22 x 3) 1.45-1.74 s; 30 x 3
#                    (588 c) 1.76-1.93 s; 20 x 6 two-letter words 1.56-2.20 s; 30 x 8 two-letter words
#                    5.6-7.2 s; 30 x 60 words 37.9 s (AEGIS round 10)
# Four-word phrases were measured and left out: 1.6-1.9 s alone is 1.9-2.1 s inside a long-running
# process, no margin; a 240-character budget measured 1.65 s worst, barely below 300's (the cost
# saturates). About half of the cost is the regional-indicator reading (a second pass over
# the text); most of the rest is fixed per text, so a longer list needs a faster gate, not a higher cap.
# See docs/adr/0005 (decision 49, gap 16).
MAX_NEVER_SAY_WORDS = 3  # words per phrase (canonical words)
MAX_NEVER_SAY_PHRASE_CHARS = 30  # characters per phrase
MAX_NEVER_SAY_CHARS = 300  # characters over the whole never-say list


def never_say_over_caps(phrases: list[str]) -> list[str]:
    """Every way `phrases` (a never-say list) breaks the review-cost caps, one line each; [] if none."""
    out: list[str] = []
    if len(phrases) > MAX_NEVER_SAY:
        out.append(f"{len(phrases)} never-say phrases: at most {MAX_NEVER_SAY} per rulebook")
    for p in phrases:
        words = len(canonical(p).split())
        if words > MAX_NEVER_SAY_WORDS or len(p) > MAX_NEVER_SAY_PHRASE_CHARS:
            out.append(f"never-say {p[:40] + ('...' if len(p) > 40 else '')!r} is too long ({words} words, {len(p)} "
                       f"characters): at most {MAX_NEVER_SAY_WORDS} words and {MAX_NEVER_SAY_PHRASE_CHARS} "
                       "characters per phrase")
    total = sum(len(p) for p in phrases)
    if total > MAX_NEVER_SAY_CHARS:
        out.append(f"never-say phrases total {total} characters: at most {MAX_NEVER_SAY_CHARS} per rulebook")
    return out


def never_say_cap_warnings(entries: list[NeverSayEntry]) -> list[str]:
    """N9-3 / N10-4: a draft whose never-say list is over a review-cost cap cannot be approved — say so."""
    return [f"{w} can be approved (review cost); nothing was dropped — shorten the list before review"
            for w in never_say_over_caps([e.phrase for e in entries])]


def short_entry_warnings(entries: list[NeverSayEntry], rule_ids: dict[str, str] | None = None) -> list[str]:
    """N3: a never-say entry of <= SHORT_ENTRY_LETTERS letters is matched
    exactly (on canonical text, lookalikes folded, split or run together)
    but gets NO edit budget unless `fuzzy` — say so, either way."""
    out: list[str] = []
    for e in entries:
        letters = len(canonical(e.phrase).replace(" ", ""))
        if letters <= SHORT_ENTRY_LETTERS:
            rid = f"{rule_ids[e.phrase]} " if rule_ids and e.phrase in rule_ids else ""
            if e.fuzzy:
                out.append(f"never-say {rid}{e.phrase!r} has {letters} letters and is opted into fuzzy matching: "
                           "expect ordinary words one letter away from it to reach the human queue")
            else:
                out.append(f"never-say {rid}{e.phrase!r} has {letters} letters (<= {SHORT_ENTRY_LETTERS}): matched "
                           "exactly only, misspellings are not caught; set \"fuzzy\": true on the entry to opt in")
    return out


def _warnings(goal: CampaignGoal) -> list[str]:
    entries = never_say_entries(goal)
    return never_say_cap_warnings(entries) + short_entry_warnings(entries)


def _build(goal: CampaignGoal, registry: PlatformRulesRegistry, today: date) -> tuple[list[Angle], list[Rule], list[str]]:
    blocking: list[str] = []
    angles = [
        Angle(angle_id=f"A{i:02d}", name=a.name, description=a.description,
              keywords=tuple(a.keywords), approved_hook_lines=tuple(a.hook_lines))
        for i, a in enumerate(goal.angles, start=1)
    ]
    counters: dict[RuleKind, int] = {}

    def nxt(kind: RuleKind) -> int:
        counters[kind] = counters.get(kind, 0) + 1
        return counters[kind]

    rules: list[Rule] = []
    rules.append(_rule(RuleKind.ON_BRIEF, nxt(RuleKind.ON_BRIEF),
                       "Every clip uses exactly one approved angle: "
                       + ", ".join(f"{a.angle_id} {a.name}" for a in angles) + ".",
                       {"angle_ids": [a.angle_id for a in angles]}))
    for phrase in dict.fromkeys(goal.must_say):
        rules.append(_rule(RuleKind.MUST_SAY, nxt(RuleKind.MUST_SAY), f"The clip must say: \"{phrase}\".", {"phrase": phrase}))
    for e in never_say_entries(goal):
        params = {"phrase": e.phrase, **({"fuzzy": True} if e.fuzzy else {})}
        rules.append(_rule(RuleKind.NEVER_SAY, nxt(RuleKind.NEVER_SAY), f"The clip must never say: \"{e.phrase}\".", params))
    rules.append(_rule(RuleKind.DISCLOSURE, nxt(RuleKind.DISCLOSURE),
                       "The caption carries a paid-partnership disclosure (one of: "
                       + ", ".join(goal.disclosure_any_of) + ") or the platform's paid-partnership label is on.",
                       {"any_of": list(goal.disclosure_any_of), "or_platform_label": True}))
    # a target listed twice is one target (fix wave 8, N7-3 fuzz: two identical spec rules
    # collapsed onto one id at revision time, a duplicate-id 500)
    platforms = list(dict.fromkeys(goal.platforms))
    targets = [{"platform": p.platform, "placement": p.placement} for p in platforms]
    rules.append(_rule(RuleKind.PLATFORM, nxt(RuleKind.PLATFORM),
                       "Post only to: " + ", ".join(f"{t['platform']}/{t['placement']}" for t in targets) + ".",
                       {"targets": targets}))

    originality_row_ids: list[str] = []
    watermark_row_ids: list[str] = []
    for p in platforms:
        lr = length_rows(registry, p.platform, p.placement, today)
        if not lr.usable:
            why = "; ".join(lr.blocked) or "no length row in the Platform Rules Registry"
            blocking.append(f"{p.platform}/{p.placement}: no usable length rule ({why})")
        else:
            cap = min(int(r.value) for r in lr.usable)
            if goal.campaign_max_length_seconds is not None:
                cap = min(cap, goal.campaign_max_length_seconds)
            rules.append(_rule(RuleKind.SPEC_LENGTH, nxt(RuleKind.SPEC_LENGTH),
                               f"Clips for {p.platform}/{p.placement} are at most {cap} seconds.",
                               {"platform": p.platform, "placement": p.placement, "max_seconds": cap},
                               tuple(sorted(r.row_id for r in lr.usable))))
        orr = originality_rows(registry, p.platform, p.placement, today)
        if not orr.usable:
            why = "; ".join(orr.blocked) or "no originality row in the Platform Rules Registry"
            blocking.append(f"{p.platform}/{p.placement}: no usable originality rule ({why})")
        for r in orr.usable:
            originality_row_ids.append(r.row_id)
            if r.rule_key == "originality_repost_watermark":
                watermark_row_ids.append(r.row_id)

    rules.append(_rule(RuleKind.ORIGINALITY_TRANSFORM, nxt(RuleKind.ORIGINALITY_TRANSFORM),
                       f"Transform, don't repost: every clip adds at least {goal.min_transformation_elements} of: "
                       + ", ".join(sorted(TRANSFORMATION_ELEMENTS)) + ". A raw repost is always rejected.",
                       {"min_elements": goal.min_transformation_elements, "allowed": sorted(TRANSFORMATION_ELEMENTS)},
                       tuple(sorted(set(originality_row_ids)))))
    rules.append(_rule(RuleKind.ORIGINALITY_WATERMARK, nxt(RuleKind.ORIGINALITY_WATERMARK),
                       "No noticeable third-party watermark.", {}, tuple(sorted(set(watermark_row_ids)))))
    rules.append(_rule(RuleKind.QUALITY_FLOOR, nxt(RuleKind.QUALITY_FLOOR),
                       f"Minimum vertical resolution {goal.min_resolution_height_px}px.",
                       {"min_height_px": goal.min_resolution_height_px}))
    allowed_assets = sorted(set(goal.source_asset_ids) | set(goal.cleared_asset_ids))
    rules.append(_rule(RuleKind.RIGHTS_CLEARED_ONLY, nxt(RuleKind.RIGHTS_CLEARED_ONLY),
                       "Use only the campaign's licensed source footage and cleared assets (no other music, "
                       "likeness or footage).", {"allowed_asset_ids": allowed_assets}))
    rules.append(_rule(RuleKind.MIN_DAYS_LIVE, nxt(RuleKind.MIN_DAYS_LIVE),
                       f"Each clip stays live at least {goal.min_days_live} days.", {"days": goal.min_days_live}))
    return angles, rules, blocking


def draft(goal: CampaignGoal, registry: PlatformRulesRegistry, today: date, version: int = 1,
          drafted_by: str = WRITER_ACTOR) -> Rulebook:
    angles, rules, blocking = _build(goal, registry, today)
    return Rulebook(
        campaign_id=goal.campaign_id, client_id=goal.client_id, vertical=goal.vertical, version=version,
        status=RulebookStatus.DRAFT, objective=goal.objective, source_asset_ids=tuple(goal.source_asset_ids),
        approved_angles=tuple(angles), platforms=tuple(goal.platforms), rules=tuple(rules),
        blocking_issues=tuple(blocking), warnings=tuple(_warnings(goal)),
        drafted_by=drafted_by, language=goal.language,
    )


def _content_key(rule: Rule) -> tuple[str, str]:
    return rule.kind.value, json.dumps(rule.params, sort_keys=True)


def revise(previous: Rulebook, goal: CampaignGoal, registry: PlatformRulesRegistry, today: date, version: int,
           drafted_by: str = WRITER_ACTOR) -> Rulebook:
    if goal.campaign_id != previous.campaign_id:
        raise ValueError("a revision must be for the same campaign")
    angles, fresh, blocking = _build(goal, registry, today)
    old_by_key = {_content_key(r): r for r in previous.rules}  # each old rule is matched at most once
    # the next unused number per prefix: one above the campaign's high-water mark (fix wave 9, L2: the
    # marks replace the stored list of every retired id; fix wave 8, N7-3: computed once per revision)
    counters: dict[str, int] = {k: v + 1 for k, v in previous.rule_number_high_water.items()}
    out: list[Rule] = []
    for r in fresh:
        old = old_by_key.pop(_content_key(r), None)
        if old is not None:
            out.append(r.model_copy(update={"rule_id": old.rule_id}))
        else:
            prefix = PREFIX[r.kind]
            n = counters.get(prefix, 1)
            counters[prefix] = n + 1
            out.append(Rule(rule_id=format_rule_id(prefix, n), kind=r.kind, text=r.text, params=r.params,
                            rationale_row_ids=r.rationale_row_ids))
    return Rulebook(
        campaign_id=goal.campaign_id, client_id=goal.client_id, vertical=goal.vertical, version=version,
        status=RulebookStatus.DRAFT, objective=goal.objective, source_asset_ids=tuple(goal.source_asset_ids),
        approved_angles=tuple(angles), platforms=tuple(goal.platforms), rules=tuple(out),
        rule_number_high_water=dict(previous.rule_number_high_water), blocking_issues=tuple(blocking),
        warnings=tuple(_warnings(goal)), drafted_by=drafted_by,
        supersedes_version=previous.version, language=goal.language,
    )
