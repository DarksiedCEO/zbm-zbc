"""
Intelligence 10 — Escalation Briefing.

Decides: the briefing pack Andre reads, BEFORE he engages, delivered by
push notification. Contents are exactly the locked list: who the client
is; what the account pull found; what the client said they care about;
exactly where the snag is; what the agent already tried; a 60-second
summary; the ONE decision he must make; a recommended action.

Rules:
- The 60-second summary is capped at 150 words (roughly 60 seconds of
  reading), built from the other fields — never free-written.
- The one decision and the recommended action come from a fixed table per
  trigger (below). Nothing a client wrote can change them.
- Dollar figures appear only as LabeledValue renders (the baseline totals
  are shown per classification, labeled).
- No credential, token or secret can be in the pack: every text field is
  passed through ``redaction.scrub``.
"""

from __future__ import annotations

from onboarding_schema import Baseline, BriefingPack, TriggerKind
from redaction import scrub

from ._status import PHASE1_STATUS

NUMBER = 10
NAME = "Escalation Briefing"
PHASE = 1
STATUS = PHASE1_STATUS

SUMMARY_MAX_WORDS = 150

DECISIONS: dict[TriggerKind, tuple[str, str]] = {
    TriggerKind.HUMAN_REQUESTED: (
        "Will you take this conversation yourself within the time the client was promised?",
        "Contact the client directly within the committed time; the agent pauses on this thread until you do.",
    ),
    TriggerKind.DEAL_SIZE: (
        "Approve onboarding this deal as scoped, or review the terms first?",
        "Review the scope against the signed contract; also set a deal-size threshold so only deals above it come to you.",
    ),
    TriggerKind.STUCK: (
        "How should this blocked step be unblocked?",
        "Reach the person who owns the blocked step directly; the agent's one resolution attempt did not clear it.",
    ),
    TriggerKind.FRICTION: (
        "How should this friction be cleared?",
        "Talk to the client directly; the agent's one resolution attempt did not clear it.",
    ),
    TriggerKind.AUDIT_ANOMALY: (
        "Proceed, pause, or ask the client to explain the anomaly?",
        "Pause client-facing numbers on this account until the anomaly is explained.",
    ),
    TriggerKind.LOW_RECOMMEND_SCORE: (
        "How do you want to recover this client relationship?",
        "Call the client personally this week and ask what would make it a 9 or 10.",
    ),
}


def _baseline_text(b: Baseline | None) -> str:
    if b is None:
        return "No account pull yet."
    parts = [f"{b.finding_count} finding(s)"]
    if b.totals_by_classification:
        parts.append("totals by label: " + ", ".join(f"{k} {v:.2f} USD" for k, v in sorted(b.totals_by_classification.items())))
    if b.double_count_entities:
        parts.append(f"{len(b.double_count_entities)} double-count risk(s) excluded")
    if b.uncertain_findings:
        parts.append(f"{len(b.uncertain_findings)} uncertain-cause finding(s)")
    return "; ".join(parts) + "."


def _cap_words(text: str, n: int) -> str:
    words = text.split()
    return text if len(words) <= n else " ".join(words[:n]) + " ..."


def build_briefing(
    client_line: str,
    baseline: Baseline | None,
    client_priorities: list[str],
    trigger: TriggerKind,
    snag: str,
    attempted: str | None,
    extra_context: str = "",
) -> BriefingPack:
    decision, recommended = DECISIONS[trigger]
    found = _baseline_text(baseline)
    cares = ", ".join(p.replace("_", " ") for p in client_priorities) or "Not stated yet."
    tried = attempted or "Nothing: this is a hard trigger, so it escalates immediately with no agent discretion."
    summary = _cap_words(
        f"{client_line}. Trigger: {trigger.value.replace('_', ' ')}. Snag: {snag}. {extra_context} "
        f"Account pull: {found} Client cares about: {cares}. Already tried: {tried} "
        f"Your decision: {decision} Recommended: {recommended}",
        SUMMARY_MAX_WORDS,
    )
    return BriefingPack(
        who_the_client_is=scrub(client_line),
        what_the_account_pull_found=scrub(found),
        what_the_client_said_they_care_about=scrub(cares),
        exactly_where_the_snag_is=scrub(snag),
        what_the_agent_already_tried=scrub(tried),
        sixty_second_summary=scrub(" ".join(summary.split())),
        the_one_decision=decision,
        recommended_action=recommended,
    )
