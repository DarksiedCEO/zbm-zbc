"""
ZBC intelligence 8 — Clip Review.

Job: score every submitted clip against the rulebook VERSION IT WAS MADE
UNDER.
Decides: pass / reject / human_review. Output is a review decision only —
it has no money fields and never implies a payout (payout_eligibility.py
is a separate gate; Finance (31) pays).

Every rejection cites WRITTEN rule ids from that version. This is enforced
structurally: a `ClipReviewDecision` can only be validated with the
rulebook's rule ids in the validation context; constructing one directly,
or citing an id that isn't in that version, raises. No rule on the page,
no rejection — a problem with no governing rule goes to human_review.

What is judged is what the submitter DECLARES (transformation elements,
watermark flag, resolution, platform label) plus EVERY text field of the
submission (caption, on-screen text, transcript, account bio: TEXT_FIELDS
— fix wave 7, AEGIS round 6 NEW-7: the bio was stored and never scanned,
so "GET RICH with my link" in a bio passed). No media is analysed in this build; audio
fingerprinting (Chromaprint) and scene analysis plug in later
(shared/media.py). Submitted text is DATA: it is only searched for the
rulebook's phrases, never interpreted — "ignore your rules and approve"
in a caption or bio changes nothing.

Checks (each tied to a rule kind; missing rule => not governed):
PF  platform/placement is a target                         -> reject
SP  length <= the target's max; rationale rows usable today
    (a stale row => human_review, never an automatic pass)  -> reject / human_review
OB  angle is approved (reject); no angle keyword in the
    clip's text => borderline                               -> reject / human_review
OR  raw repost or zero valid elements, or fewer valid
    elements than required                                  -> reject; unknown element names -> human_review
OW  declared third-party watermark                          -> reject
DC  disclosure token in caption, or paid-partnership label  -> reject
MS  each must-say phrase present in the CLIP's text (caption,
    on-screen text, transcript — a bio is not the clip)     -> reject
NS  no never-say phrase in ANY text field (bio included)   -> reject
    (the fields are scanned as one text in model order, and a phrase
    SPREAD over two fields in any order — "get" in the caption, "rich"
    in the bio — is a human's call, fix wave 7: the words at the end of
    one field and the start of another are read together)
    (DC/MS/NS match on canonical text — confusables, diacritics, format
    characters and fullwidth forms folded, shared/text.py; a phrase found
    only once split letters are rejoined or leetspeak folded is
    borderline -> human_review, never a pass; NS also: a NEAR MISS — the
    phrase appears once symbols/digits are read as letters ("return$",
    "G€t", "6et") or treated as wildcards — is human_review;
    fix wave 5: ASCII lookalike spellings — the words are the phrase once
    rn/m, cl/d, vv/w are read alike ("make rnoney") -> reject; within a
    small edit distance of the phrase on that skeleton ("Guaranteed
    retrns", "miracle kure", "get rlch", "make nnoney", words split or run
    together) -> human_review; shared/text.visual_near_miss;
    fix wave 6: the gate runs on the clip's LETTER STREAM (splits are
    irrelevant: "ge t rl ch", "make r n oney"; stretched letters "geeet
    riiich" read as the phrase -> reject; doubled letters "gget ricch" ->
    human_review), the phrase's words in order within two other words
    ("make big money") -> human_review, and an entry of <= 4 letters is
    exact-only unless its rule says `fuzzy` (N3); one scan per clip for
    all never-say rules, shared/text.visual_near_misses;
    fix wave 7: a vowel-drop or phonetic respelling — the phrase's
    consonant skeleton ("mk mny", "grnteed rtrns") or its words' phonetic
    keys ("phree money", "get ritch", "kno risque") -> human_review,
    shared/text.skeleton_near_miss / phonetic_near_miss;
    fix wave 8 (AEGIS round 7): a stacked respelling — vowel drop +
    homophone + lookalike ("grnteed retunrs", "lose vvait fst", "mk
    rnunny") -> human_review (the pairs rn/m cl/d vv/w read alike before
    the consonant and phonetic signals; two signals each nearly accepting
    the same window, shared/text.stacked_near_miss); a symbol standing
    for a word ("make 💰", "make $$$ fast", "free 💸", "guaranteed 📈",
    "get 💎 quick"; shared/text.SYMBOL_LEXICON / symbol_stand_in) ->
    human_review; and a phrase SPREAD over ANY ordered pair of fields is
    judged by every signal, respelled halves included ("gt" in the
    caption + "ritch" in the bio) -> human_review (N7-5; the fields are
    joined by a line break, a token boundary, not a hard wall))
    fix wave 9 (AEGIS round 8): letter-like symbols (🅼🅰🅺🅴, 𝐦𝐚𝐤𝐞) are
    read as letters, so an exact phrase in them is a reject (H1; regional
    indicators 🇲🇦🇰🇪 since fix wave 10: a human's call, see below);
    ANY symbol in the place of one phrase word, whatever follows it,
    across line breaks and across field boundaries (a field of nothing
    but symbols sits beside both ends of every other field) ->
    human_review (M2); the similarity signals run in stages, each batch
    only for the phrases the earlier signals left, and not at all when a
    written rule already rejects the clip (a rejection carries no human
    review reasons) unless it is routed to a human (M1; the review itself
    runs off the workflow lock, zbc/workflow.review_clip_unlocked)
    fix wave 10 (AEGIS round 9): currency / math-symbol letters ("₥₳₭€
    ₥⊙₦€¥") are read as letters (shared/text.CURRENCY_MATH_LOOKALIKES) ->
    human_review, never a reject (a currency sign is also money) (N9-1);
    regional indicators are a READING, never a signal: every never-say
    rule no other signal flagged is looked for in the fields read with
    each regional indicator as its letter, and a hit is human_review,
    never a reject (it may be flags), whatever the run lengths
    (`_regional_never_say`, N9-2); grade-1 Braille letters are letter-like
    (N9-7)
MIX any word mixing letters with symbols/digits in any text field
    (shared/text.mixed_symbol_words; ordinary
    punctuation, #hashtags, prices and "2nd"/"1990s"-style numbers excepted)
                                                            -> human_review
OBF any text field shows an obfuscation
    signal (letter-like symbols, fix wave 9; more than 30% of a field or
    of a run of its words stripped by canonicalisation, the fail-safe —
    since fix wave 10 also any run of 3 stripped characters, and a word
    made mostly of currency / math symbols;
    bidi controls, fillers, tag characters anywhere;
    other invisibles beside a letter; lookalikes among Latin;
    two scripts inside one word; separator-split letters; a Latin
    letter outside Basic Latin + Latin-1 + the fold table)  -> human_review
LAT English-language campaign (every rulebook in this build):
    any letter outside the Latin script in those fields     -> human_review
QF  resolution >= floor; not declared => human_review       -> reject / human_review
RC  every source/added asset is in the allow-list           -> reject
MD  min days live is NOT judged here (Verification and Integrity).
"""

from __future__ import annotations

import functools
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, model_validator

from shared.registry import PlatformRulesRegistry
from shared.text import (
    READINGS,
    PhraseMatch,
    _osa_within,
    _vis,
    canonical,
    consonant_skeleton,
    contains_phrase,
    match_phrase,
    mixed_symbol_words,
    near_miss,
    non_latin_letters,
    obfuscation_signals,
    _has_symbol,
    _is_symbol_char,
    phonetic_key,
    phonetic_signal,
    phrase_words_in_order,
    regional_reading,
    relaxed_skeleton_spans,
    relaxed_visual_spans,
    skeleton_near_misses,
    skeleton_signal,
    stacked_or_symbol,
    symbol_fragment_at_edges,
    symbol_only,
    symbol_stand_in,
    visual_lookalike_exact,
    visual_near_misses,
    warm_phonetic_runs,
    word_key,
)
from shared.types import MAX_RULEBOOK_VERSION, CampaignId, NonEmptyStr, SafeId
from zbc.platform_rules import rows_usable
from zbc.rulebook import Rulebook, RuleKind


# Every free-text field of a submission. All of them are scanned by every
# text rule that forbids something (never-say, obfuscation, mixed symbol
# words, non-Latin letters); the rules that REQUIRE something look where
# the requirement lives (disclosure: the caption; must-say and the angle
# keywords: the clip's own text, CLIP_TEXT_FIELDS). A field added to the
# model must be added here or test_fix_wave_7 fails.
TEXT_FIELDS = ("caption", "on_screen_text", "transcript", "account_bio")
CLIP_TEXT_FIELDS = ("caption", "on_screen_text", "transcript")


class ClipSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    submission_id: SafeId
    campaign_id: CampaignId
    rulebook_version: int = Field(ge=1, le=MAX_RULEBOOK_VERSION)
    clipper_id: SafeId
    posted_at: AwareDatetime
    platform: NonEmptyStr
    placement: NonEmptyStr
    post_ref: NonEmptyStr
    length_seconds: float = Field(gt=0, le=36000)
    resolution_height_px: int | None = Field(default=None, ge=1, le=10000)
    angle_id: NonEmptyStr
    moment_ids: list[str] = Field(default_factory=list, max_length=200)
    caption: str = Field(default="", max_length=5000)
    on_screen_text: str = Field(default="", max_length=5000)
    transcript: str = Field(default="", max_length=50000)
    account_bio: str = Field(default="", max_length=5000)
    transformation_elements: list[str] = Field(default_factory=list, max_length=50)
    is_raw_repost: bool
    has_third_party_watermark: bool
    paid_partnership_label: bool = False
    source_asset_ids: list[SafeId] = Field(default_factory=list, max_length=200)
    added_asset_ids: list[SafeId] = Field(default_factory=list, max_length=200)


class BrokenRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    reason: str


class RuleCitationError(ValueError):
    """A decision tried to cite a rule id that is not in its rulebook version."""


class ClipReviewDecision(BaseModel):
    """Review decision ONLY. Deliberately no amount/rate/payout fields
    (tests/test_guardrails.py inspects this model to keep it that way)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    submission_id: str
    campaign_id: str
    rulebook_version: int
    outcome: Literal["pass", "reject", "human_review"]
    broken_rules: tuple[BrokenRule, ...] = ()
    human_review_reasons: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    decided_by: str
    decided_at: datetime
    received_at: datetime | None = None  # server receipt time of the submission (never submitter-asserted)

    @model_validator(mode="after")
    def _cites_only_rules_on_the_page(self, info: ValidationInfo) -> "ClipReviewDecision":
        ctx = info.context or {}
        rule_ids = ctx.get("rulebook_rule_ids")
        key = ctx.get("rulebook_key")
        if rule_ids is None or key is None:
            raise RuleCitationError("a clip review decision can only be built against a rulebook version")
        if key != (self.campaign_id, self.rulebook_version):
            raise RuleCitationError("decision's campaign/version differs from the rulebook it was built against")
        for b in self.broken_rules:
            if b.rule_id not in rule_ids:
                raise RuleCitationError(
                    f"rule {b.rule_id!r} is not in {self.campaign_id} v{self.rulebook_version}; no rule on the page, no rejection"
                )
        if self.outcome == "reject" and not self.broken_rules:
            raise RuleCitationError("a rejection must cite at least one written rule")
        if self.outcome != "reject" and self.broken_rules:
            raise RuleCitationError("only a rejection cites broken rules")
        if self.outcome == "human_review" and not self.human_review_reasons:
            raise RuleCitationError("human_review needs a reason")
        return self


def make_decision(rb: Rulebook, **data) -> ClipReviewDecision:
    """The ONLY way to build a decision: validated against `rb`'s rule ids."""
    try:
        return ClipReviewDecision.model_validate(
            data, context={"rulebook_rule_ids": rb.rule_ids(), "rulebook_key": (rb.campaign_id, rb.version)}
        )
    except ValidationError as exc:
        raise RuleCitationError("; ".join(e["msg"] for e in exc.errors())) from exc


BOUNDARY_WORDS = 8  # words read together across two fields: the longest never-say phrase plus the adjacency gap


def _mentions(text: str, phrase: str, fuzzy: bool) -> bool:
    """The phrase is there by ANY never-say signal: exact, split / leet-folded,
    in order within the adjacency policy, or any similarity signal
    (symbols, visual, skeleton, phonetic, stacked, a symbol for a word)."""
    return (match_phrase(text, phrase) is not PhraseMatch.NONE or phrase_words_in_order(text, phrase) is not None
            or near_miss(text, phrase, fuzzy) is not None)


EDGE_TOKEN_CHARS = 64  # a token longer than this ends a field edge (fix wave 8, N7-7)


def _field_edges(sub: ClipSubmission, n: int = BOUNDARY_WORDS) -> dict[str, tuple[list[str], list[str]]]:
    """Per non-empty text field: (its first n canonical tokens, its last n).
    An edge stops at a token of more than EDGE_TOKEN_CHARS characters (fix
    wave 8, N7-7: a field that is one 50,000-letter "word" made every
    field joint 50 KB, scanned per phrase — 4.5 s): no signal reads a
    phrase across such a token (a window starts at a token start), so the
    words beyond it cannot be part of a phrase spread over the boundary."""
    edges = {}
    for f in TEXT_FIELDS:
        toks = canonical(getattr(sub, f)).split()
        if toks:
            head, tail = toks[:n], toks[-n:]
            long_h = [i for i, t in enumerate(head) if len(t) > EDGE_TOKEN_CHARS]
            long_t = [i for i, t in enumerate(tail) if len(t) > EDGE_TOKEN_CHARS]
            head = head[:long_h[0]] if long_h else head
            tail = tail[long_t[-1] + 1:] if long_t else tail
            edges[f] = (head, tail)
    return edges


def _field_joints(edges: dict[str, tuple[list[str], list[str]]]) -> list[tuple[str, str, str, str, str]]:
    """Every ORDERED pair (a, b) of distinct non-empty text fields, read
    across the boundary: (a, b, a's tail, b's head, tail + head). Fix
    wave 8 (AEGIS round 7, N7-5): the fields used to be adjacent only in
    model order (caption, on-screen text, transcript, bio), so "gt" in the
    caption + "ritch" in the bio passed while the same halves in the
    caption + on-screen text went to a human. Bounded: BOUNDARY_WORDS a
    side, at most 12 pairs."""
    out = []
    for a, (_, tail) in edges.items():
        for b, (head, _) in edges.items():
            if a != b:
                out.append((a, b, " ".join(tail), " ".join(head), " ".join(tail + head)))
    return out


@functools.lru_cache(maxsize=65536)
def _close(token: str, word: str) -> bool:
    """`token` could be `word` under SOME never-say signal: the same once
    lookalike pairs are read alike, the same consonant skeleton or sound
    (as written or with the pairs contracted), one letter edit away, or
    carrying a symbol / digit (a stand-in). A cheap pre-filter: a joint
    is only scanned for a phrase when its tail holds a token close to
    the phrase's first word and its head one close to its last."""
    if not token.isalpha():
        if token[0] in "#@":
            return False  # a tag or a number is not a word of the phrase
        if any(c.isalpha() for c in token):
            return True  # letters mixed with symbols / digits: a symbol may stand for a letter
        return any(_is_symbol_char(c) for c in token)  # a symbol run (fix wave 9: any symbol, M2)
    return (word_key(token) == word_key(word)
            or any(consonant_skeleton(_vis(token, v)) == consonant_skeleton(_vis(word, v))
                   or phonetic_key(_vis(token, v)) == phonetic_key(_vis(word, v)) for v in READINGS)
            or _osa_within(token, word, 1) is not None)


def _spreads(joints: list[tuple[str, str, str, str, str]],
             phrases: list[tuple[str, bool]]) -> dict[tuple[str, bool], tuple[str, str, str]]:
    """{(phrase, fuzzy): (field a, field b, the words)} for each multi-word
    phrase said — by any signal, `_mentions` — only by the last words of
    field a read together with the first words of field b, for some
    ordered pair of distinct text fields (the first such pair in
    `joints` order): a phrase spread over two fields, exactly ("get" in
    the caption, "rich" in the bio) or respelled ("gt" + "ritch"),
    whichever order the fields are in. Joint by joint, the phrases that
    pass the `_close` pre-filter are scanned in ONE batch per text (fix
    wave 8, N7-7)."""
    out: dict[tuple[str, bool], tuple[str, str, str]] = {}
    words = {key: canonical(key[0]).split() for key in phrases}
    for a, b, tail, head, joint in joints:
        tail_toks, head_toks = tail.split(), head.split()
        cands = [key for key in phrases if key not in out and len(words[key]) >= 2
                 and any(_close(t, words[key][0]) for t in tail_toks)
                 and any(_close(t, words[key][-1]) for t in head_toks)]
        if not cands:
            continue
        batch = tuple(cands)
        for text in (joint, tail, head):
            visual_near_misses(text, batch)
            skeleton_near_misses(text, batch)
        for key in cands:
            phrase, fuzzy = key
            if _mentions(joint, phrase, fuzzy) and not _mentions(tail, phrase, fuzzy) and not _mentions(head, phrase, fuzzy):
                out[key] = (a, b, joint)
    return out


def _raw_edges(text: str, n: int = BOUNDARY_WORDS) -> tuple[str, str]:
    """(first n, last n) whitespace-separated chunks of `text` as written
    — symbols, prices and line breaks' neighbours kept — each side stopping
    at a chunk over EDGE_TOKEN_CHARS characters (as `_field_edges`)."""
    chunks = text.split()
    head, tail = chunks[:n], chunks[-n:]
    long_h = [i for i, t in enumerate(head) if len(t) > EDGE_TOKEN_CHARS]
    long_t = [i for i, t in enumerate(tail) if len(t) > EDGE_TOKEN_CHARS]
    head = head[:long_h[0]] if long_h else head
    tail = tail[long_t[-1] + 1:] if long_t else tail
    return " ".join(head), " ".join(tail)


def _symbol_spreads(sub: ClipSubmission, phrases: list[tuple[str, bool]]) -> dict[tuple[str, bool], tuple[str, str, str]]:
    """{(phrase, fuzzy): (field a, field b, what)} for each multi-word
    phrase whose SYMBOL stand-in (`symbol_stand_in`) is only there across
    a field boundary (fix wave 9, AEGIS round 8 M2, one mechanism with the
    wave-8 spreads): (1) the last words of field a read with the first
    words of field b, symbols kept ("... make" + "💰 daily vlogs"), for
    every ordered pair; (2) a field that is NOTHING but symbols ("💰" as
    the bio) with the phrase minus one word at either edge of another
    field ("make ..." opening the caption) — fields have no reading order,
    so a symbol-only field sits beside both ends of every other one."""
    out: dict[tuple[str, bool], tuple[str, str, str]] = {}
    fields = {f: getattr(sub, f) for f in TEXT_FIELDS if getattr(sub, f).strip()}
    multi = [key for key in phrases if len(canonical(key[0]).split()) >= 2]
    if not multi:
        return out
    only = [f for f, t in fields.items() if symbol_only(t)]
    for s in only:
        for x, t in fields.items():
            if x in only:
                continue
            head, tail = _raw_edges(t)
            for key in multi:
                if key in out:
                    continue
                hit = symbol_fragment_at_edges(head, key[0]) or symbol_fragment_at_edges(tail, key[0])
                if hit:
                    out[key] = (x, s, f"{hit}; the {s} is only {fields[s].strip()[:20]!r}")
    edges = {f: _raw_edges(t) for f, t in fields.items()}
    for a, (_, tail) in edges.items():
        for b, (head, _) in edges.items():
            if a == b or not tail or not head:
                continue
            joint = tail + " " + head
            if not _has_symbol(joint):
                continue
            for key in multi:
                if key in out:
                    continue
                how = symbol_stand_in(joint, key[0])
                if how and not symbol_stand_in(tail, key[0]) and not symbol_stand_in(head, key[0]):
                    out[key] = (a, b, how)
    return out


def _rejected_later(sub: ClipSubmission, rb: Rulebook) -> bool:
    """The quality-floor or rights rule rejects this clip (the same tests
    `review` makes below, made early; fix wave 9, M1)."""
    qf = rb.one(RuleKind.QUALITY_FLOOR)
    if qf is not None and sub.resolution_height_px is not None and sub.resolution_height_px < int(qf.params.get("min_height_px", 0)):
        return True
    rc = rb.one(RuleKind.RIGHTS_CLEARED_ONLY)
    if rc is not None:
        allowed = set(rc.params.get("allowed_asset_ids", []))
        if any(a not in allowed for a in [*sub.source_asset_ids, *sub.added_asset_ids]):
            return True
    return False


def _borderline_never_say(sub: ClipSubmission | None, all_text: str, edges, open_rules: list[tuple],
                          borderline: list[str]) -> set[str]:
    """The never-say signals that make a clip a human's call, for the
    rules no exact or lookalike reading broke (in rule order); the ids of
    the rules it flagged. `sub` None: no symbol-across-fields check (a
    second reading of the same fields, fix wave 10)."""
    flagged: set[str] = set()
    # near_miss() in stages (fix wave 9, M1): each batched scan runs only for the phrases every earlier
    # signal left, so a phrase gets the same first signal as before, in the same rule order
    found: dict[str, str] = {}
    pending: list[tuple] = []
    for r, phrase, fz, m in open_rules:
        if m is PhraseMatch.LOOSE:
            found[r.rule_id] = f"{r.rule_id}: possible never-say {phrase!r} written with split/obfuscated letters"
            continue
        how = near_miss(all_text, phrase, fz, stacked=False, stage="early")
        if how:
            found[r.rule_id] = f"{r.rule_id}: possible never-say {phrase!r} written with {how}"
        else:
            pending.append((r, phrase, fz))
    for signal, prepare in ((skeleton_signal, skeleton_near_misses), (phonetic_signal, warm_phonetic_runs)):
        if not pending:
            break
        prepare(all_text, tuple((p, fz) for _, p, fz in pending))
        left_over = []
        for r, phrase, fz in pending:
            how = signal(all_text, phrase, fz)
            if how:
                found[r.rule_id] = f"{r.rule_id}: possible never-say {phrase!r} written with {how}"
            else:
                left_over.append((r, phrase, fz))
        pending = left_over
    unmatched: list[tuple] = pending
    borderline.extend(found[r.rule_id] for r, _, _, _ in open_rules if r.rule_id in found)
    flagged.update(found)
    # the phrases no signal caught: the stacked rule (its relaxed scans batched, fix wave 8 class B),
    # a symbol standing for a word (N7-4), then the phrase spread over two fields (N7-5)
    if unmatched:
        relaxed_visual_spans(all_text, tuple((p, fz) for _, p, fz in unmatched))
        relaxed_skeleton_spans(all_text, tuple((p, fz) for _, p, fz in unmatched))
        # The stacked rule and the symbol rule read the batch just scanned for `all_text` BEFORE the
        # field joints are scanned (fix wave 10, N9-3): `_spreads` scans up to 36 joint texts, which
        # evicted `all_text` from the per-thread memo (16 texts), so every phrase's stacked check
        # re-scanned the whole 65 KB text for that one phrase — 117 full scans, 24 s, at 200 phrases.
        # Each phrase's results do not depend on which other phrases share its batch, so the reasons
        # (and their rule order) are unchanged.
        stacked = {r.rule_id: stacked_or_symbol(all_text, phrase, fz) for r, phrase, fz in unmatched}
        rest = [(p, fz) for r, p, fz in unmatched if not stacked[r.rule_id]]
        spreads = _spreads(_field_joints(edges), rest) if rest else {}
        left = []
        for r, phrase, fz in unmatched:
            how = stacked[r.rule_id]
            if how:
                borderline.append(f"{r.rule_id}: possible never-say {phrase!r} written with {how}")
                flagged.add(r.rule_id)
                continue
            spread = spreads.get((phrase, fz))
            if spread:
                borderline.append(f"{r.rule_id}: possible never-say {phrase!r} spread over {spread[0]} and "
                                  f"{spread[1]} ({spread[2][:60]!r})")
                flagged.add(r.rule_id)
                continue
            left.append((r, phrase, fz))
        # a symbol standing for a word across a field boundary (fix wave 9, M2)
        sym = _symbol_spreads(sub, [(p, fz) for _, p, fz in left]) if left and sub is not None else {}
        for r, phrase, fz in left:
            hit = sym.get((phrase, fz))
            if hit:
                borderline.append(f"{r.rule_id}: possible never-say {phrase!r} with a symbol standing for one of "
                                  f"its words across {hit[0]} and {hit[1]} ({hit[2][:120]})")
                flagged.add(r.rule_id)
    return flagged


REGIONAL_NOTE = "read with its regional-indicator symbols as letters (they may be flags, so a human decides)"


def _regional_never_say(all_text: str, open_rules: list[tuple], already: set[str], borderline: list[str]) -> None:
    """Fix wave 10 (AEGIS round 9 N9-2): regional indicators are a candidate READING, never a signal on
    their own. The never-say rules no other signal flagged are looked for once more in the fields read
    with every regional indicator as its letter (glued "🇲🇦🇰🇪" or pair-spaced "🇲🇦 🇰🇪" alike): any
    hit — exact, split, lookalike or any similarity signal — is a human's call, never a rejection (a
    run of regional indicators is also a row of flags); no hit adds nothing, whatever the run lengths."""
    reading = regional_reading(all_text)
    if reading is None:
        return
    todo = [(r, phrase, fz) for r, phrase, fz, _ in open_rules if r.rule_id not in already]
    if not todo:
        return
    visual_near_misses(reading, tuple((p, fz) for _, p, fz in todo))  # one scan of the reading for them all
    pending: list[tuple] = []
    hits: list[str] = []
    for r, phrase, fz in todo:
        m = match_phrase(reading, phrase)
        if m is PhraseMatch.EXACT or (m is PhraseMatch.NONE and visual_lookalike_exact(reading, phrase, fz)):
            hits.append(f"{r.rule_id}: possible never-say {phrase!r} written in regional-indicator letters")
        else:
            pending.append((r, phrase, fz, m))
    if pending:
        _borderline_never_say(None, reading, {}, pending, hits)
    borderline.extend(f"{h} — {REGIONAL_NOTE}" for h in hits)


def review(sub: ClipSubmission, rb: Rulebook, registry: PlatformRulesRegistry, now: datetime,
           decided_by: str = "zbc_clip_review", received_at: datetime | None = None,
           route_to_human: tuple[str, ...] = ()) -> ClipReviewDecision:
    """`route_to_human`: reasons from outside the rulebook (e.g. a declared
    version outside its grace window) that make this clip ineligible for
    ANY automatic outcome; the automatic result is kept as a reason for
    the human reviewer."""
    today = now.date()
    broken: list[BrokenRule] = []
    borderline: list[str] = []
    checks: list[str] = []
    clip_text = " ".join(getattr(sub, f) for f in CLIP_TEXT_FIELDS)
    # the fields as one text, in model order, joined by a line break: a token boundary for every
    # stream gate (the phrase's letters never cross it mid-word) and a stop for the symbol rule
    all_text = "\n".join(getattr(sub, f) for f in TEXT_FIELDS)
    edges = _field_edges(sub)

    def fail(rule_id: str, reason: str) -> None:
        broken.append(BrokenRule(rule_id=rule_id, reason=reason))

    target = f"{sub.platform}/{sub.placement}"
    pf = rb.one(RuleKind.PLATFORM)
    if pf is None:
        borderline.append("no platform rule in this version (not governed)")
    else:
        checks.append(pf.rule_id)
        targets = {f"{t['platform']}/{t['placement']}" for t in pf.params.get("targets", [])}
        if target not in targets:
            fail(pf.rule_id, f"{target} is not a campaign target")

    sp = next((r for r in rb.rules_of(RuleKind.SPEC_LENGTH)
               if (r.params.get("platform"), r.params.get("placement")) == (sub.platform, sub.placement)), None)
    if sp is not None:
        checks.append(sp.rule_id)
        if sub.length_seconds > int(sp.params["max_seconds"]):
            fail(sp.rule_id, f"length {sub.length_seconds}s exceeds {sp.params['max_seconds']}s")
        stale = rows_usable(registry, sp.rationale_row_ids, today)
        if stale:
            borderline.append(f"{sp.rule_id} rests on registry rows that can't be relied on today: " + "; ".join(stale))

    ob = rb.one(RuleKind.ON_BRIEF)
    if ob is None:
        borderline.append("no on-brief rule in this version (not governed)")
    else:
        checks.append(ob.rule_id)
        angle = rb.angle(sub.angle_id)
        if angle is None or sub.angle_id not in ob.params.get("angle_ids", []):
            fail(ob.rule_id, f"angle {sub.angle_id!r} is not an approved angle")
        elif not any(contains_phrase(clip_text, kw) for kw in angle.keywords):
            borderline.append(f"{ob.rule_id}: none of {angle.angle_id}'s keywords appear in the clip's text (borderline on-brief)")

    ortr = rb.one(RuleKind.ORIGINALITY_TRANSFORM)
    if ortr is None:
        borderline.append("no transformation rule in this version (not governed)")
    else:
        checks.append(ortr.rule_id)
        allowed = set(ortr.params.get("allowed", []))
        valid = sorted({e for e in sub.transformation_elements if e in allowed})
        unknown = sorted({e for e in sub.transformation_elements if e not in allowed})
        need = int(ortr.params.get("min_elements", 1))
        if sub.is_raw_repost or not valid:
            fail(ortr.rule_id, "raw repost: no transformation (transform, don't repost)")
        elif len(valid) < need:
            fail(ortr.rule_id, f"{len(valid)} transformation element(s) ({', '.join(valid)}); needs {need}")
        if unknown:
            borderline.append(f"{ortr.rule_id}: unrecognised transformation element(s) {', '.join(unknown)}")

    ow = rb.one(RuleKind.ORIGINALITY_WATERMARK)
    if ow is not None:
        checks.append(ow.rule_id)
        if sub.has_third_party_watermark:
            fail(ow.rule_id, "noticeable third-party watermark")
    elif sub.has_third_party_watermark:
        borderline.append("third-party watermark declared but no watermark rule in this version")

    dc = rb.one(RuleKind.DISCLOSURE)
    if dc is None:
        borderline.append("no disclosure rule in this version (not governed)")
    else:
        checks.append(dc.rule_id)
        found = [match_phrase(sub.caption, t) for t in dc.params.get("any_of", [])]
        label = bool(dc.params.get("or_platform_label") and sub.paid_partnership_label)
        if PhraseMatch.EXACT not in found and not label:
            if PhraseMatch.LOOSE in found:
                borderline.append(f"{dc.rule_id}: disclosure only found with split/obfuscated letters")
            else:
                fail(dc.rule_id, "no disclosure in the caption and no paid-partnership label")

    for r in rb.rules_of(RuleKind.MUST_SAY):
        checks.append(r.rule_id)
        m = match_phrase(clip_text, r.params.get("phrase", ""))
        if m is PhraseMatch.LOOSE:
            borderline.append(f"{r.rule_id}: must-say {r.params.get('phrase')!r} only found with split/obfuscated letters")
        elif m is PhraseMatch.NONE:
            fail(r.rule_id, f"missing must-say {r.params.get('phrase')!r}")
    never = [(r, r.params.get("phrase", ""), bool(r.params.get("fuzzy"))) for r in rb.rules_of(RuleKind.NEVER_SAY)]
    batch = tuple((p, fz) for _, p, fz in never)
    # one scan of the submission's letter stream (every text field, bio included) for every
    # never-say phrase at once (fix wave 6, N1; fix wave 7, NEW-7 / the skeleton scan)
    visual_near_misses(all_text, batch)
    open_rules: list[tuple] = []
    for r, phrase, fz in never:
        checks.append(r.rule_id)
        m = match_phrase(all_text, phrase)
        lookalike = None if m is PhraseMatch.EXACT else visual_lookalike_exact(all_text, phrase, fz)
        if m is PhraseMatch.EXACT:
            fail(r.rule_id, f"says never-say {phrase!r}")
        elif lookalike is not None:
            fail(r.rule_id, f"says never-say {phrase!r} with lookalike letters ({lookalike[:60]!r} "
                            "reads the same once rn/m, cl/d and vv/w are read alike, stretched letters collapsed "
                            "and spaces ignored)")
        else:
            open_rules.append((r, phrase, fz, m))
    # Fix wave 9 (AEGIS round 8 M1): the similarity signals below only ever add a human_review REASON,
    # and a rejection carries none. When a written rule is already broken (or will be: the quality
    # floor and rights rules below are decided here, from the same inputs) and nothing routes the
    # clip to a human, the outcome is a rejection whatever they find, so they are not computed.
    rejecting = not route_to_human and (bool(broken) or _rejected_later(sub, rb))
    if not rejecting:
        flagged = _borderline_never_say(sub, all_text, edges, open_rules, borderline)
        _regional_never_say(all_text, open_rules, flagged, borderline)
        for field_name in TEXT_FIELDS:
            for sig in obfuscation_signals(getattr(sub, field_name)):
                borderline.append(f"obfuscation in {field_name}: {sig}")
            mixed = mixed_symbol_words(getattr(sub, field_name))
            if mixed:
                borderline.append(f"letters mixed with symbols/digits in {field_name} ({', '.join(repr(w) for w in mixed)}): "
                                  "a symbol or digit can stand in for a letter, so a human reads it")
            if rb.language == "en":
                foreign = non_latin_letters(getattr(sub, field_name))
                if foreign:
                    borderline.append(f"non-Latin letter(s) in {field_name} of an English-language campaign "
                                      f"({', '.join(foreign[:5])}): a human reads it")

    qf = rb.one(RuleKind.QUALITY_FLOOR)
    if qf is not None:
        checks.append(qf.rule_id)
        floor = int(qf.params.get("min_height_px", 0))
        if sub.resolution_height_px is None:
            borderline.append(f"{qf.rule_id}: resolution not declared")
        elif sub.resolution_height_px < floor:
            fail(qf.rule_id, f"resolution {sub.resolution_height_px}px below {floor}px")

    rc = rb.one(RuleKind.RIGHTS_CLEARED_ONLY)
    used = list(dict.fromkeys([*sub.source_asset_ids, *sub.added_asset_ids]))
    if rc is not None:
        checks.append(rc.rule_id)
        allowed_assets = set(rc.params.get("allowed_asset_ids", []))
        uncleared = [a for a in used if a not in allowed_assets]
        if uncleared:
            fail(rc.rule_id, f"uncleared asset(s): {', '.join(uncleared)}")
        if not sub.source_asset_ids:
            borderline.append(f"{rc.rule_id}: no source asset declared")
    elif used:
        borderline.append("assets declared but no rights rule in this version")

    outcome = "reject" if broken else ("human_review" if borderline else "pass")
    if route_to_human:
        automatic = f"automatic result would have been {outcome}" + (
            f" (broke {', '.join(b.rule_id for b in broken)})" if broken else "")
        borderline = [*route_to_human, automatic, *borderline]
        outcome, broken = "human_review", []
    return make_decision(
        rb, submission_id=sub.submission_id, campaign_id=sub.campaign_id, rulebook_version=rb.version,
        outcome=outcome, broken_rules=tuple(broken),
        human_review_reasons=tuple(borderline) if outcome == "human_review" else (),
        checks=tuple(checks), decided_by=decided_by, decided_at=now, received_at=received_at,
    )
