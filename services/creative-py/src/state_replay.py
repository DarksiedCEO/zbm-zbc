"""
Wave F (M-1, ADR 0005 "Wave F fixes"): creative-py's consequential state is rebuilt from the local record log at start.

Briefs, jobs and work (ZBM), rulebooks, goals, rights checks, moment maps, hook sheets, kits (with Andre's signatures),
clip submissions and decisions (ZBC), the creative memories, the platform-rules registry rows and the rights records /
licences are carried by every decision line as a state delta (``shared.statelog``) and replayed in log order at start.

Andre's kit signature is never lost and never forged by a restart:

* never forged -- after replay, every kit that is ``signed`` must be signed by Andre AND named by a committed
  ``campaign_kit_signed_by_andre`` record (a committed line's evidence) or by a resolved signature intent; otherwise
  start-up is refused. A line whose record failed is not committed, so an unsigned kit cannot become signed;
* never lost -- ``sign_kit`` writes a ``kit_signature_intent`` line (anchored, after Andre's token is verified) BEFORE
  the ledger record. If the process stops before the decision's own line is written, the next start applies the
  signature exactly when the ledger holds that record and the kit is still the draft it was made for (read with the
  ledger's paged, filtered read), and closes the intent with a ``kit_signature_resolved`` line carrying the state.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from datetime import datetime, timezone

import shared
import zbc
import zbm
from shared.founder import FOUNDER_ACTOR
from shared.ledger import DEPARTMENT, LedgerQueryFailed, UnconfiguredLedgerClient
from shared.statelog import Codec, StateCodecError, StateReplayError, StateTracker

log = logging.getLogger("creative.replay")

SIGN_EVENT = "campaign_kit_signed_by_andre"
INTENT_KIND = "kit_signature_intent"
RESOLVED_KIND = "kit_signature_resolved"
LINE_KINDS = frozenset({"decision", "partial", "ids", INTENT_KIND, RESOLVED_KIND})
LINE_KEYS = frozenset({"op", "outside_calls", "counters", "partial", "rk", "evidence", "state", INTENT_KIND,
                       "kit_signatures_closed", "outcomes", "kit_signatures_applied"})


def _state_modules() -> list:
    mods = []
    for pkg in (shared, zbm, zbc):
        mods.append(pkg)
        mods += [importlib.import_module(f"{pkg.__name__}.{m.name}") for m in pkgutil.iter_modules(pkg.__path__)]
    return mods


def build_tracker(zbm_wf, zbc_wf, registry, rights) -> StateTracker:
    st = StateTracker(Codec(_state_modules()))
    for name, attr in (("briefs", "briefs"), ("jobs", "jobs"), ("work", "work")):
        st.add_keyed(f"zbm_{name}", zbm_wf, attr)
    st.add_whole("zbm_rounds_used", lambda: zbm_wf._rounds_used, lambda v: setattr(zbm_wf, "_rounds_used", v))
    st.add_whole("zbm_escalated_chains", lambda: zbm_wf._escalated_chains,
                 lambda v: setattr(zbm_wf, "_escalated_chains", v))
    st.add_whole("zbm_memory", lambda: zbm_wf.memory, lambda v: setattr(zbm_wf, "memory", v))
    st.add_keyed("zbc_rulebooks", zbc_wf.rulebooks, "_by_key")
    for attr in ("goals", "rights_checks", "moment_maps", "hook_sheets", "kits", "submissions", "decisions",
                 "_kit_sha", "_submission_content"):
        st.add_keyed(f"zbc_{attr.lstrip('_')}", zbc_wf, attr)
    st.add_whole("zbc_memory", lambda: zbc_wf.memory, lambda v: setattr(zbc_wf, "memory", v))
    st.add_keyed("registry_rows", registry, "rows")
    st.add_keyed("rights_records", rights, "records")
    st.add_keyed("rights_licenses", rights, "licenses")
    return st


def replay(recorder, state: StateTracker, zbc_wf, ledger) -> None:
    """Apply every line's state in log order, refuse start on a line this build cannot interpret, check every signed
    kit against its committed signature, then resolve open signature intents. Writes nothing unless an intent was
    open (idempotent: a second restart replays the same lines to the same state)."""
    committed: set[str] = set()
    signed_by_evidence: set[str] = set()
    intents: dict[str, dict] = {}
    closed: set[str] = set()
    for r in recorder.journal.log.iter_records():
        where = f"local log line {r.get('seq')} ({r.get('kind')!r})"
        d = r.get("data")
        if r.get("kind") not in LINE_KINDS or not isinstance(d, dict):
            raise StateReplayError(f"refusing to start: {where}: not a line kind this build writes; the log cannot be "
                                   "interpreted (inspect it; nothing was skipped)")
        unknown = sorted(set(d) - LINE_KEYS)
        if unknown:
            raise StateReplayError(f"refusing to start: {where}: unknown field(s) {unknown}; the log cannot be "
                                   "interpreted (inspect it; nothing was skipped)")
        if "state" in d:
            state.apply(d["state"], where)
        for n in d.get("evidence") or []:
            committed.add(n.get("event_id"))
            if n.get("event_type") == SIGN_EVENT:
                signed_by_evidence.add(n.get("subject_id"))
        if r["kind"] == INTENT_KIND:
            i = d.get(INTENT_KIND)
            if not isinstance(i, dict) or not {"campaign_id", "kit_id", "rulebook_version", "event_id"} <= set(i):
                raise StateReplayError(f"refusing to start: {where}: malformed kit signature intent")
            intents[i["event_id"]] = i
        closed.update(d.get("kit_signatures_closed") or [])
        signed_by_evidence.update(d.get("kit_signatures_applied") or [])
    try:
        state.baseline()
    except StateCodecError as exc:
        raise StateReplayError(f"refusing to start: the replayed state cannot be re-encoded ({exc})") from None
    for cid in state.keyed["zbc_kits"].raw_keys():
        kit = state.keyed["zbc_kits"].raw_get(cid)
        if kit.status == "signed" and (kit.signed_by != FOUNDER_ACTOR or kit.kit_id not in signed_by_evidence):
            raise StateReplayError(
                f"refusing to start: kit {kit.kit_id} ({cid}) is signed in the replayed state but no committed Andre "
                "signature names it; a restart never signs a kit (inspect the log)")
    open_ = {eid: i for eid, i in intents.items() if eid not in closed and eid not in committed}
    if open_:
        _resolve_intents(recorder, state, zbc_wf, ledger, open_)


def _resolve_intents(recorder, state: StateTracker, zbc_wf, ledger, open_: dict[str, dict]) -> None:
    try:
        if isinstance(ledger, UnconfiguredLedgerClient):
            raise LedgerQueryFailed("ledger not configured")
        if hasattr(ledger, "entries_filtered"):
            found = ledger.entries_filtered(DEPARTMENT, SIGN_EVENT, want=set(open_))
        else:
            found = ledger.entries()
        held = {e.get("event_id") for e in found if isinstance(e, dict) and e.get("event_type") == SIGN_EVENT}
    except (LedgerQueryFailed, AttributeError) as exc:
        raise StateReplayError(f"refusing to start: {len(open_)} Andre kit signature(s) may be on the ledger without a "
                               f"committed local line, and the ledger cannot be read ({exc}); a signature would be "
                               "lost") from None
    outcomes: dict[str, str] = {}
    applied: list[str] = []
    before: list = []
    for eid, i in sorted(open_.items()):
        kit = zbc_wf.kits.get(i["campaign_id"])
        if eid not in held:
            outcomes[eid] = "not_on_ledger"
        elif kit is None or kit.kit_id != i["kit_id"] or kit.rulebook_version != i["rulebook_version"]:
            outcomes[eid] = "kit_replaced"
        elif kit.status == "signed":
            outcomes[eid] = "already_signed"
        else:
            before.append((i["campaign_id"], kit))
            zbc_wf.kits[i["campaign_id"]] = kit.model_copy(update={"status": "signed", "signed_by": FOUNDER_ACTOR})
            outcomes[eid] = "applied"
            applied.append(kit.kit_id)
    delta, mark = state.delta()
    extra = {"kit_signatures_closed": sorted(open_), "outcomes": outcomes, "kit_signatures_applied": applied}
    if delta is not None:
        extra["state"] = delta
    try:
        recorder.journal.commit(RESOLVED_KIND, datetime.now(timezone.utc).isoformat(), RESOLVED_KIND, [], extra)
        mark()
    except Exception as exc:  # noqa: BLE001 - ledger or local log: resolved again at the next start
        # nothing the log does not hold is served: the kit stays a draft in this process (no later line may carry a
        # signature no committed line names); the next start resolves the intent again
        for cid, kit in before:
            state.keyed["zbc_kits"].raw_set(cid, kit)
        state.reset()
        log.warning("kit signature intents left open (%s); resolved again at the next start", type(exc).__name__)
