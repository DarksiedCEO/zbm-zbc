"""
Theo — Conversation Mining (ADR 0049). ONE job: track multi-turn conversation chains (opening question -> follow-ups
-> final AI recommendation) for the client's topics, and record per turn whether and where the client and its
competitors appear, and what the final turn recommends. A brand can be invisible at turn one and win by turn three;
an opening-prompt probe (Callum, the audit) cannot see that.

Theo does not classify citation sources (Callum), size demand (Naomi), cross-check anything (Zara), write, publish,
or call a model except through the multi-engine adapter (answer_adapter.py).

Follow-up generation (deterministic; ``FOLLOWUPS_VERSION``). The whole chain is planned BEFORE the first question is
asked, from the topic and Priya's emerging queries only — never from an engine's answer, so text inside an answer
(a prompt injection included) cannot add, change or reorder a follow-up:
  turn 1  the topic as written;
  turn 2  ``compare``  (fixed template);
  turn 3  for a prompt-set topic, at most ONE of Priya's emerging items (kind ``constraint`` or ``question``) that
          shares at least ``PRIYA_FOLLOWUP_MIN_SHARED`` content words with the topic, first by key, as
          ``PRIYA_FOLLOWUP``; otherwise nothing;
  last    ``decide``   (fixed template): the final-recommendation turn.
A planned turn whose normalised text repeats an earlier turn is not asked (``LOOP_PREVENTED``); a chain whose
answer repeats an earlier answer of the same chain stops (``LOOP_STOPPED``); at most ``MAX_TURNS`` turns. LLM-
generated follow-ups would go only through the adapter and are NOT BUILT (``SEO_THEO_LLM_FOLLOWUPS`` refuses start).

Context mode ``user_turns_only`` (the honest limit). The adapter is single-question and stateless, so turn k is ONE
request carrying the earlier USER questions of the chain as a numbered list, then the new question. The engine's
own earlier answers are not sent back: a consumer chat app would include them, so these chains approximate a real
conversation, they do not replay one. Native multi-turn (each engine's message array) is not built.

Per turn, from the answer (in memory; only a bounded, PII-screened excerpt is kept, and none for a Priya topic):
the client mentioned (agents/brand_mention.py, with its confidence) and cited (the cited HOST is the client's: exact,
www. twin or subdomain via agents/domain_match.py; a lookalike never; a redirector host makes it UNKNOWN — Theo does
not resolve URLs, Callum does), the client's rank among the named entities, each competitor term mentioned, the
competitor domains cited. The final recommendation (complete chains only) is LEXICAL: the entity named first in the
first sentence of the last answer that holds a recommendation cue (``CUES``), else the entity named first; never
higher than MEDIUM confidence, LOW when its mention is unconfirmed. An incomplete chain (engine failure, refusal,
budget, loop) has an UNKNOWN final recommendation — never the last turn's answer passed off as the final one.

No agent touches service state, the ledger or the log: the service records what this module returns.
"""

from __future__ import annotations

import hashlib
import re
from typing import Callable, Optional

from agents import brand_mention, domain_match, priya_handoff, split_url
from agents.callum import excerpt
from agents.probes import MIN_USABLE, REDIRECTOR_HOSTS, _switch_code, extract_citations, wilson
from agents.prompt_guard import personal_data
from answer_adapter import PROTOCOL_ERROR_STOP_AFTER, STOP_CODES, EngineError
from envelope import envelope, finding
from ports import NOT_CONNECTED, ProviderAnswer
from primitives import Killed

AGENT, TASK = "theo", "conversation_chains"
VERSION = "theo-rules/2026-11-08.1"
FOLLOWUPS_VERSION = "theo-followups/2026-11-08.1"
CONTRACT = "theo.conversation_chains/1"
CONTEXT_MODE = "user_turns_only"
MAX_TURNS = 4
MAX_ROWS = 2000
PRIYA_FOLLOWUP_MIN_SHARED = 2
FOLLOWUPS = {"compare": "What are the main options to compare for this, and how do they differ?",
             "decide": "Which one would you recommend, and why?"}
PRIYA_FOLLOWUP = "What about this: {query}"
CONTEXT_TEMPLATE = "Earlier in this conversation I asked:\n{earlier}\nMy next question: {question}"
TURN_KINDS = ("opening", "compare", "priya_followup", "decide")
CHAIN_STATUS = ("COMPLETE", "INCOMPLETE", "LOOP_STOPPED", "NOT_ASKED")
PATTERNS = ("INVISIBLE_THEN_RECOMMENDED", "VISIBLE_AND_RECOMMENDED", "VISIBLE_THEN_NOT_RECOMMENDED",
            "NOT_RECOMMENDED", "INCONCLUSIVE")
STRENGTHS = ("STRONG", "WEAK", "NONE")
WINNERS = ("client", "competitor", "none", "unknown")
CUES = re.compile(r"\b(recommend|recommended|recommendation|best choice|best option|top pick|go with|i would choose|"
                  r"i'd choose|my pick|the winner)\b", re.IGNORECASE)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_CONTENT = re.compile(r"[^\W\d_]{3,}")
_STOP = frozenset({"the", "and", "for", "with", "what", "which", "who", "how", "why", "when", "where", "does", "are",
                   "can", "best", "near", "about", "this", "that", "your", "from", "into", "than"})
LIMITS = [
    "Context mode user_turns_only: each turn re-sends the earlier USER questions of the chain, not the engine's own "
    "earlier answers (the adapter is single-question). A consumer chat app keeps its answers in context, so these "
    "chains approximate a conversation; they are not a replay of one.",
    "Follow-ups are fixed templates plus at most one of Priya's emerging queries; real users ask other follow-ups. "
    "No LLM-generated follow-up exists (not built).",
    "The final recommendation is lexical (a cue word and the first entity named in that sentence): it does not "
    "understand the answer, so it is never more than MEDIUM confidence.",
    "Cited means the engine's structured citation names the client's host. Cited URLs are not resolved here (that is "
    "Callum's job): a citation on a redirector host leaves 'cited' UNKNOWN.",
    "One account, one time, N samples per chain: observations, not search truth. Nothing here is a visibility score "
    "or calibrated against traffic or revenue.",
]


# ---------------------------------------------------------------------------------------------- planning

def _content_words(text: str) -> set:
    return {w for w in _CONTENT.findall(priya_handoff.norm(text)) if w not in _STOP}


def plan_turns(topic: dict, handoff_items: list) -> dict:
    """The chain planned for a topic, before anything is asked: ``turns`` [{kind, source, priya_key, _text}] and
    ``flags``. Deterministic; reads the topic and Priya's items only."""
    flags: list = []
    cand = [{"kind": "opening", "source": topic["source"], "priya_key": topic["priya_key"], "_text": topic["_text"]},
            {"kind": "compare", "source": "template", "priya_key": None, "_text": FOLLOWUPS["compare"]}]
    if topic["source"] == "prompt_set":
        words = _content_words(topic["_text"])
        for it in sorted(handoff_items or [], key=lambda i: i["key"]):
            if it["key"] == topic["priya_key"] or not set(it["emerging_kinds"]) & {"constraint", "question"}:
                continue
            q = it["query"]["text"]
            if len(words & _content_words(q)) >= PRIYA_FOLLOWUP_MIN_SHARED:
                if personal_data(q) is not None:
                    flags.append("PRIYA_FOLLOWUP_REFUSED_PERSONAL_DATA")
                    continue
                cand.append({"kind": "priya_followup", "source": "priya", "priya_key": it["key"],
                             "_text": PRIYA_FOLLOWUP.format(query=" ".join(q.split()))})
                break
    cand.append({"kind": "decide", "source": "template", "priya_key": None, "_text": FOLLOWUPS["decide"]})
    turns: list = []
    seen: set = set()
    for c in cand:
        n = priya_handoff.norm(c["_text"])
        if n in seen:
            flags.append(f"LOOP_PREVENTED:{c['kind']}")
            continue
        seen.add(n)
        turns.append(c)
    if len(turns) > MAX_TURNS:
        flags.append("MAX_TURNS")
        turns = turns[:MAX_TURNS - 1] + [turns[-1]]
    return {"turns": turns, "flags": flags}


def render(turns: list, k: int) -> str:
    """The text sent at turn ``k`` (0-based): the earlier user questions, then the new one (CONTEXT_MODE)."""
    if k == 0:
        return turns[0]["_text"]
    earlier = "\n".join(f"{i + 1}. {' '.join(t['_text'].split())}" for i, t in enumerate(turns[:k]))
    return CONTEXT_TEMPLATE.format(earlier=earlier, question=turns[k]["_text"])


# ---------------------------------------------------------------------------------------------- per-turn observation

def _positions(text: str, terms: list, rules: Optional[list]) -> Optional[int]:
    t = brand_mention._ws_lines(text)
    best = None
    for r in brand_mention.normalise(terms, rules):
        for _w, at in brand_mention._matches(t, r):
            best = at if best is None or at < best else best
    return best


def _host(url: str) -> str:
    p = split_url(url)
    return (p.hostname or "").lower() if p is not None else ""


def observe(a: ProviderAnswer, ctx: dict) -> dict:
    """What one answer says about the client and the competitors. Reads the text in memory; keeps no text."""
    text = a.text or ""
    cites = extract_citations(a)
    hosts = [c["host"] for c in cites if c.get("host")]
    on_client = any(domain_match.match(h, ctx["domain"]) in (domain_match.EXACT, domain_match.SUBDOMAIN)
                    for h in hosts)
    lookalike = any(domain_match.match(h, ctx["domain"]) == domain_match.LOOKALIKE for h in hosts)
    redirector = any(h in REDIRECTOR_HOSTS for h in hosts)
    cited = True if on_client else (None if redirector else False)
    m = brand_mention.mention(text, ctx["terms"], ctx["rules"], ctx["domain"], client_cited=on_client)
    entities = []
    if m["mentioned"]:
        entities.append(("client", None, _positions(text, ctx["terms"], ctx["rules"]), m["confidence"]))
    comps = []
    for name in ctx["competitor_terms"]:
        cm = brand_mention.mention(text, [name])
        pos = _positions(text, [name], None) if cm["mentioned"] else None
        comps.append({"name": name, "mentioned": cm["mentioned"], "confidence": cm["confidence"]})
        if cm["mentioned"]:
            entities.append(("competitor", name, pos, cm["confidence"]))
    order = sorted(entities, key=lambda e: (e[2] if e[2] is not None else 10 ** 9, e[0], e[1] or ""))
    rank = next((i + 1 for i, e in enumerate(order) if e[0] == "client"), None)
    comp_cited = sorted({d for d in ctx["competitor_domains"] for h in hosts if domain_match.on_site(h, d)})
    return {"client": {"mentioned": m["mentioned"], "confidence": m["confidence"], "cited": cited,
                       "rank": rank, "lookalike_cited": lookalike},
            "competitors": comps, "competitor_domains_cited": comp_cited,
            "order": [e[0] if e[0] == "client" else f"competitor:{e[1]}" for e in order],
            "citations": len(cites), "_entities": order}


def recommendation(a: ProviderAnswer, ctx: dict) -> dict:
    """The final turn's recommendation, lexically (module docstring)."""
    text = " ".join((a.text or "").split("\n"))
    for s in _SENTENCE.findall(a.text or ""):
        if not CUES.search(s):
            continue
        o = observe(ProviderAnswer("ANSWER", s), ctx)["_entities"]
        if o:
            return _winner(o[0], "cue_sentence")
    o = observe(ProviderAnswer("ANSWER", text), ctx)["_entities"]
    if o:
        return _winner(o[0], "first_mention")
    return {"winner": "none", "name": None, "basis": "none_named", "confidence": "LOW"}


def _winner(e: tuple, basis: str) -> dict:
    conf = "MEDIUM" if basis == "cue_sentence" and e[3] == brand_mention.HIGH else "LOW"
    return {"winner": e[0], "name": e[1], "basis": basis, "confidence": conf}


# ---------------------------------------------------------------------------------------------- collection

class CallCap:
    """Theo's own per-run cap on the calls ONE conversation run may reserve across every engine
    (``SEO_THEO_RUN_MAX_CALLS``; 0 = OFF: nothing is asked). A whole chain is reserved before its first turn, so the
    cap never cuts a chain in half: a chain that does not fit is not started."""

    def __init__(self, limit: int):
        self.limit, self.used = limit, 0

    def take(self, calls: int) -> Optional[str]:
        if self.limit <= 0:
            return "BUDGET_OFF"
        if self.used + calls > self.limit:
            return "THEO_RUN_CALL_CAP"
        self.used += calls
        return None


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "surrogatepass")).hexdigest()


def run_chains(topics: list, plans: dict, engines: dict, engine_names: list, n: int, guard: Callable, budget,
               cap: CallCap, ctx: dict, clock, emit: Optional[Callable] = None) -> dict:
    """Ask every planned chain ``n`` times on every engine, topic by topic (so each topic's chains are emitted to
    Zara as soon as they are done: ``emit(topic_key, chains)``). Returns {"engines", "chains", "rows"}.
    The run rules are Callum's: a metered port only inside a recorded reservation (one per chain sample), the stop
    codes, two protocol errors with no answer between stop the engine, the 4x cost guard; prompts with personal data
    are never sent. ``Killed`` propagates (the service discards the run)."""
    from clock import iso
    st: dict = {}
    for e in engine_names:
        port = engines.get(e)
        s = {"port": port, "outcome": None, "stop": None, "stop_detail": None, "answers": 0, "errors": 0,
             "protocol_run": 0, "chains_asked": 0, "chains_not_asked": 0, "run": None}
        if port is None or not getattr(port, "connected", False):
            s["outcome"] = NOT_CONNECTED
        elif getattr(port, "metered", False):
            if budget is None:
                s["stop"], s["stop_detail"] = "ENGINE_BUDGET_EXHAUSTED", "NO_BUDGET_HOOK"
            else:
                s["run"] = budget.open(e, getattr(port, "model", None))
        st[e] = s
    chains: list = []
    rows: list = []
    try:
        for ti, t in enumerate(topics):
            topic_chains = []
            for e in engine_names:
                s = st[e]
                summary = _chains_for(t, ti, plans[t["topic_key"]], e, s, n, guard, cap, ctx, clock, iso, rows)
                topic_chains.append(summary)
            chains += topic_chains
            if emit is not None:
                emit(t["topic_key"], topic_chains)
    finally:
        for e, s in st.items():
            s["usage"] = s["run"].settle(s["answers"], s["stop"]) if s["run"] is not None else None
    view = {}
    for e, s in st.items():
        if s["outcome"] == NOT_CONNECTED:
            out = NOT_CONNECTED
        elif s["stop"] is None:
            out = "OK" if s["answers"] or not s["chains_asked"] else "FAILED"
        elif s["answers"]:
            out = "PARTIAL"
        else:
            out = {"ENGINE_BUDGET_EXHAUSTED": "BLOCKED", "THEO_RUN_CALL_CAP": "BLOCKED", "BUDGET_OFF": "BLOCKED",
                   "ENGINE_NOT_CONNECTED": NOT_CONNECTED}.get(s["stop"], "FAILED")
        view[e] = {"outcome": out, "reason": s["stop"], "reason_detail": s["stop_detail"], "usage": s["usage"],
                   "chains_asked": s["chains_asked"], "chains_not_asked": s["chains_not_asked"]}
    return {"engines": view, "chains": chains, "rows": rows[:MAX_ROWS], "rows_truncated": len(rows) > MAX_ROWS}


def _chains_for(t, ti, plan, e, s, n, guard, cap, ctx, clock, iso, rows) -> dict:
    turns = plan["turns"]
    samples = []
    for si in range(n):
        if t["refused"] is not None:
            samples.append({"sample": si, "status": "NOT_ASKED", "reason": "PROMPT_REFUSED", "turns": [],
                            "final": _unknown()})
            continue
        if s["outcome"] == NOT_CONNECTED or s["stop"] is not None:
            s["chains_not_asked"] += 1
            samples.append({"sample": si, "status": "NOT_ASKED", "reason": s["stop"] or NOT_CONNECTED, "turns": [],
                            "final": _unknown()})
            continue
        samples.append(_one_chain(t, ti, si, n, turns, e, s, guard, cap, ctx, clock, iso, rows))
    return _summary(t, e, turns, plan["flags"], samples)


def _unknown() -> dict:
    return {"winner": "unknown", "name": None, "basis": "chain_incomplete", "confidence": None}


def _one_chain(t, ti, si, n, turns, e, s, guard, cap, ctx, clock, iso, rows) -> dict:
    port = s["port"]
    attempts = int(getattr(port, "attempts", 1) or 1)
    need = len(turns) * attempts
    meter = None
    why = cap.take(need)
    if why is not None:
        s["stop"], s["stop_detail"] = why, why
        s["chains_not_asked"] += 1
        return {"sample": si, "status": "NOT_ASKED", "reason": why, "turns": [], "final": _unknown()}
    if s["run"] is not None:
        try:
            meter = s["run"].reserve(ti * n + si, need)
        except EngineError as err:
            if err.code == "ENGINE_KILLED":
                raise Killed(_switch_code(err)) from None
            s["stop"], s["stop_detail"] = err.code, err.detail
            s["chains_not_asked"] += 1
            return {"sample": si, "status": "NOT_ASKED", "reason": err.code, "turns": [], "final": _unknown()}
    s["chains_asked"] += 1
    done: list = []
    answers_seen: set = set()
    status, reason, final = "COMPLETE", None, None
    takes_guard = bool(getattr(port, "accepts_guard", False))
    for k, turn in enumerate(turns):
        prompt = render(turns, k)
        guard(capability="ai_probe", provider=e)
        row = {"topic_key": t["topic_key"], "engine": e, "sample": si, "turn": k + 1, "kind": turn["kind"],
               "turn_source": turn["source"], "priya_key": turn["priya_key"], "observed_at": iso(clock.now())}
        err_code = err_detail = None
        try:
            a = port.ask(prompt, guard=guard, meter=meter) if takes_guard else port.ask(prompt)
        except Killed:
            raise
        except EngineError as err:
            if err.code == "ENGINE_KILLED":
                raise Killed(_switch_code(err)) from None
            a, err_code, err_detail = None, err.code, err.detail
        except Exception:                                   # a provider failure is counted, never raised
            a, err_code, err_detail = None, "ENGINE_UNAVAILABLE", "ADAPTER_FAILED"
        if a is not None and (not isinstance(a, ProviderAnswer) or not isinstance(a.text, str)
                              or not isinstance(a.citations, list)
                              or a.status not in ("ANSWER", "REFUSAL", "ERROR", NOT_CONNECTED)):
            a, err_code, err_detail = None, "ENGINE_PROTOCOL_ERROR", "MALFORMED_ANSWER"
        if a is not None and a.status == NOT_CONNECTED:
            a, err_code, err_detail = None, "ENGINE_NOT_CONNECTED", None
        if a is not None and a.status == "ERROR":
            a, err_code, err_detail = None, "ENGINE_UNAVAILABLE", "ENGINE_REPORTED_ERROR"
        if a is None:
            s["errors"] += 1
            rows.append({**row, "status": "FAILED", "error_code": err_code, "error_detail": err_detail})
            done.append({"turn": k + 1, "kind": turn["kind"], "status": "FAILED", "error_code": err_code})
            status, reason = "INCOMPLETE", "ENGINE_FAILED_AT_OPENING" if k == 0 else "ENGINE_FAILED_MID_CHAIN"
            if err_code == "ENGINE_PROTOCOL_ERROR":
                s["protocol_run"] += 1
                if s["protocol_run"] >= PROTOCOL_ERROR_STOP_AFTER:
                    s["stop"], s["stop_detail"] = err_code, "REPEATED_PROTOCOL_ERRORS"
            if err_code in STOP_CODES:
                s["stop"], s["stop_detail"] = err_code, err_detail
            break
        meta = {"model": a.model, "model_version": a.model_version, "provider_asked_at": a.asked_at,
                "answer_sha256": a.answer_sha256 or _sha(a.text)}
        if a.status == "REFUSAL":
            rows.append({**row, **meta, "status": "REFUSAL"})
            done.append({"turn": k + 1, "kind": turn["kind"], "status": "REFUSAL"})
            status, reason = "INCOMPLETE", "REFUSED_AT_OPENING" if k == 0 else "REFUSED_MID_CHAIN"
            break
        s["answers"] += 1
        s["protocol_run"] = 0
        obs = observe(a, ctx)
        obs.pop("_entities")
        ex = excerpt(a.text, ctx["terms"]) if t["source"] == "prompt_set" else \
            {"basis": None, "excerpt": None, "withheld": "PRIYA_QUERY_CONTEXT"}
        rows.append({**row, **meta, "status": "ANSWER", **obs, "evidence": ex})
        done.append({"turn": k + 1, "kind": turn["kind"], "status": "ANSWER", "client": obs["client"],
                     "competitors_mentioned": [c["name"] for c in obs["competitors"] if c["mentioned"]]})
        h = meta["answer_sha256"]
        if h in answers_seen:
            status, reason = "LOOP_STOPPED", "ANSWER_REPEATED"
            break
        answers_seen.add(h)
        if meter is not None and getattr(meter, "over_reserve", False):
            s["stop"], s["stop_detail"] = "ENGINE_BUDGET_EXHAUSTED", "COST_ABOVE_RESERVE"
            if k + 1 < len(turns):
                status, reason = "INCOMPLETE", "BUDGET_EXHAUSTED_MID_CHAIN"
                break
        if k + 1 == len(turns):
            final = recommendation(a, ctx)
    if status != "COMPLETE":
        final = _unknown()
    return {"sample": si, "status": status, "reason": reason, "turns": done, "final": final}


# ---------------------------------------------------------------------------------------------- summaries

def _rate(k: int, nn: int) -> dict:
    return {"value": None if nn == 0 else round(k / nn, 4), "k": k, "n": nn, "ci95": wilson(k, nn),
            "interval": "wilson_95", "directional": nn < MIN_USABLE}


def _summary(t: dict, e: str, turns: list, flags: list, samples: list) -> dict:
    complete = [c for c in samples if c["status"] == "COMPLETE"]
    by_turn = []
    for k, turn in enumerate(turns):
        ans = [c["turns"][k] for c in complete]
        by_turn.append({"turn": k + 1, "kind": turn["kind"], "source": turn["source"],
                        "client_mentioned": _rate(sum(1 for x in ans if x["client"]["mentioned"]
                                                      and x["client"]["confidence"] == brand_mention.HIGH), len(ans)),
                        "client_cited": _rate(sum(1 for x in ans if x["client"]["cited"] is True), len(ans)),
                        "client_cited_unknown": sum(1 for x in ans if x["client"]["cited"] is None),
                        "competitor_mentioned": _rate(sum(1 for x in ans if x["competitors_mentioned"]), len(ans))})
    finals: dict = {"client": 0, "competitor": {}, "none": 0}
    winners = []
    for c in complete:
        f = c["final"]
        if f["winner"] == "competitor":
            finals["competitor"][f["name"]] = finals["competitor"].get(f["name"], 0) + 1
            winners.append("competitor:" + f["name"])
        else:
            finals[f["winner"]] += 1
            winners.append(f["winner"])
    incomplete: dict = {}
    for c in samples:
        if c["status"] != "COMPLETE":
            key = c["reason"] or c["status"]
            incomplete[key] = incomplete.get(key, 0) + 1
    nc = len(complete)
    client_final = _rate(finals["client"], nc)
    modal = max((winners.count(w) for w in set(winners)), default=0)
    if nc == 0:
        pattern, strength = "INCONCLUSIVE", "NONE"
    else:
        first = by_turn[0]["client_mentioned"]["k"]
        won = finals["client"]
        pattern = ("INVISIBLE_THEN_RECOMMENDED" if not first and won else "VISIBLE_AND_RECOMMENDED" if first and won
                   else "VISIBLE_THEN_NOT_RECOMMENDED" if first else "NOT_RECOMMENDED")
        strength = "STRONG" if nc >= MIN_USABLE and modal * 3 >= nc * 2 else "WEAK"
    return {"topic_key": t["topic_key"], "engine": e, "chains": len(samples), "complete": nc,
            "loop_stopped": sum(1 for c in samples if c["status"] == "LOOP_STOPPED"),
            "not_complete": dict(sorted(incomplete.items())), "turns_planned": len(turns),
            "plan_flags": list(flags), "by_turn": by_turn,
            "final_recommendation": {"client": finals["client"],
                                     "competitor": dict(sorted(finals["competitor"].items())),
                                     "none": finals["none"], "unknown": len(samples) - nc,
                                     "basis": "lexical (CUES, first entity named)", "max_confidence": "MEDIUM"},
            "client_recommended": client_final, "unstable": len(set(winners)) > 1, "pattern": pattern,
            "strength": strength}


# ---------------------------------------------------------------------------------------------- the envelope

def output(tenant_id: str, run_id: str, topics: list, plans: dict, res: dict, n: int) -> dict:
    """The Theo -> Zara hand-off (``CONTRACT``)."""
    return {"contract": CONTRACT, "producer": AGENT, "tenant_id": tenant_id, "run_id": run_id, "rules": VERSION,
            "followups": FOLLOWUPS_VERSION, "context_mode": CONTEXT_MODE, "samples_per_chain": n,
            "topics": [{"topic_key": t["topic_key"], "source": t["source"], "priya_key": t["priya_key"],
                        "turns_planned": [{"kind": x["kind"], "source": x["source"], "priya_key": x["priya_key"]}
                                          for x in plans[t["topic_key"]]["turns"]]} for t in topics],
            "chains": res["chains"]}


def envelope_for(outcome: str, res: Optional[dict], contract: Optional[dict], topics: list, plans: dict,
                 reason: Optional[str] = None) -> dict:
    method = (f"Each topic asked as a {MAX_TURNS}-turn-at-most chain (opening, compare, at most one of Priya's "
              "emerging queries, decide), planned before the first question from the topic and Priya's items only "
              f"({FOLLOWUPS_VERSION}); context mode {CONTEXT_MODE}; each chain sampled N times per engine through the "
              "multi-engine adapter. Per turn: client and competitor mentions (whole-word, with confidence), client "
              "rank, client host cited. Final recommendation lexical on the last turn of a COMPLETE chain only. "
              f"Rates over complete chains, 95% Wilson intervals, directional under {MIN_USABLE}. Rules {VERSION}.")
    tv = [{**{k: v for k, v in t.items() if not k.startswith("_")},
           "turns_planned": [x["kind"] for x in plans.get(t["topic_key"], {}).get("turns", [])],
           "plan_flags": plans.get(t["topic_key"], {}).get("flags", [])} for t in topics]
    if res is None:
        return envelope(AGENT, TASK, outcome, [], methodology=method, limitations=LIMITS, reason=reason,
                        facts={"topics": tv})
    nc = {f"answer_engine:{e}" for e, v in res["engines"].items() if v["outcome"] == NOT_CONNECTED}
    facts = {"rules": VERSION, "topics": tv, "engines": res["engines"], "chains": res["chains"], "rows": res["rows"],
             "rows_truncated": res["rows_truncated"], "contract": contract}
    if outcome in ("NOT_CONNECTED", "KILLED", "RESTRICTED"):
        return envelope(AGENT, TASK, outcome, [], methodology=method, limitations=LIMITS, reason=reason,
                        not_connected=nc, facts=facts)
    return envelope(AGENT, TASK, outcome, _findings(res), methodology=method, limitations=LIMITS, reason=reason,
                    not_connected=nc, facts=facts)


def outcome_of(res: dict) -> tuple[str, Optional[str]]:
    eng = res["engines"]
    observed_ = [e for e, v in eng.items() if v["outcome"] in ("OK", "PARTIAL") and v["chains_asked"]]
    if not observed_:
        stopped = sorted((e, v) for e, v in eng.items() if v["outcome"] in ("BLOCKED", "FAILED"))
        if stopped:
            return stopped[0][1]["outcome"], stopped[0][1]["reason"]
        if all(v["outcome"] == NOT_CONNECTED for v in eng.values()):
            return "NOT_CONNECTED", "no answer-engine port is connected; no engine was asked anything"
        return "INSUFFICIENT_EVIDENCE", "NO_CHAIN_ASKED"
    if all(v["outcome"] == "OK" for v in eng.values()) and \
            all(c["complete"] == c["chains"] for c in res["chains"]):
        return "OK", None
    return "PARTIAL", None


def _findings(res: dict) -> list:
    out = []
    for c in res["chains"]:
        d = {"engine": c["engine"], "topic_key": c["topic_key"], "complete_chains": c["complete"],
             "pattern": c["pattern"], "client_recommended": c["client_recommended"]}
        directional = c["complete"] < MIN_USABLE
        dec = "INSUFFICIENT_EVIDENCE" if directional else "TEST"
        if c["pattern"] == "INVISIBLE_THEN_RECOMMENDED":
            out.append(finding("THEO_INVISIBLE_THEN_RECOMMENDED", "info", "measured", dec, capability="P4", detail=d))
        elif c["pattern"] == "VISIBLE_THEN_NOT_RECOMMENDED":
            out.append(finding("THEO_VISIBLE_THEN_NOT_RECOMMENDED", "medium", "measured", dec, capability="P4",
                               detail=d))
        if c["final_recommendation"]["competitor"]:
            out.append(finding("THEO_COMPETITOR_RECOMMENDED", "medium", "measured", dec, capability="P9",
                               detail={**d, "competitor": c["final_recommendation"]["competitor"]}))
        if c["unstable"]:
            out.append(finding("THEO_CHAIN_UNSTABLE", "low", "measured", "WATCH", capability="P4", detail=d))
        if c["loop_stopped"]:
            out.append(finding("THEO_CHAIN_LOOP_STOPPED", "low", "measured", "WATCH", capability="P4",
                               detail={**d, "loop_stopped": c["loop_stopped"]}))
    for e, v in sorted(res["engines"].items()):
        if v["outcome"] in ("BLOCKED", "FAILED") or (v["outcome"] == "PARTIAL" and v["reason"]):
            out.append(finding("ENGINE_RUN_STOPPED", "low", "measured", "WATCH", capability="P4",
                               detail={"engine": e, "reason": v["reason"], "outcome": v["outcome"]}))
    return out

