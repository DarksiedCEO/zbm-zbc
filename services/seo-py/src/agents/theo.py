"""
Theo — Conversation Mining (ADR 0049). ONE job: track multi-turn conversation chains (opening question -> follow-ups
-> final AI recommendation) for the client's topics, and record per turn whether and where the client and its
competitors appear, and what the final turn recommends. A brand can be invisible at turn one and recommended by turn
three; an opening-prompt probe (Callum, the audit) cannot see that.

Theo does not classify citation sources (Callum), size demand (Naomi), cross-check anything (Zara), write, publish,
or call a model except through the multi-engine adapter (answer_adapter.py).

Follow-up generation (deterministic; ``FOLLOWUPS_VERSION``). The whole USER side of the chain is planned BEFORE the
first question is asked, from the topic and Priya's emerging queries only — never from an engine's answer, so text
inside an answer (a prompt injection included) cannot add, change or reorder a follow-up or Theo's logic:
  turn 1  the topic as written;
  turn 2  ``compare``  (fixed template);
  turn 3  for a prompt-set topic, at most ONE of Priya's emerging items (kind ``constraint`` or ``question``) that
          shares at least ``PRIYA_FOLLOWUP_MIN_SHARED`` content words with the topic, first by key, as
          ``PRIYA_FOLLOWUP``; otherwise nothing;
  last    ``decide``   (fixed template): the final-recommendation turn.
A planned turn whose normalised text repeats an earlier turn is not asked (``LOOP_PREVENTED``); a chain whose answer
repeats an earlier answer of the same chain stops (``LOOP_STOPPED``); at most ``MAX_TURNS`` turns. LLM-generated
follow-ups would go only through the adapter and are NOT BUILT (``SEO_THEO_LLM_FOLLOWUPS`` refuses start).

Two context modes, chosen per engine and named in every output (review round 1, MED 5):
  ``native_multi_turn``  the engine's own earlier answers go back to it as assistant messages, in the provider's
                         documented multi-turn shape (the adapter's ``history``; Anthropic Messages and Gemini
                         generateContent, whose multi-turn docs were read: ``MULTI_TURN_DOCS`` in each module). Only
                         these chains may be called "invisible at turn one, recommended by the last turn"
                         (``INVISIBLE_THEN_RECOMMENDED``).
  ``stacked_questions``  the fallback for an engine whose multi-turn shape was not read (OpenAI, Perplexity): ONE
                         request per turn carrying the earlier USER questions only. It approximates a conversation and
                         its pattern says so (``INVISIBLE_AT_OPENING_RECOMMENDED_STACKED``).
Injection (review round 2): in native mode an earlier answer is re-sent, so text injected into it may steer the
engine's later answers, the classified final one included; Theo's plan and logic cannot be altered by it. The BASIS of
the defence is structural (review round 5): the native "invisible at turn one, recommended later" pattern is claimed
only when every complete chain's final question was re-asked WITHOUT history (``_stacked_check``, opt-in
``SEO_THEO_STACKED_CHECK``, default OFF) and gave the same pick; otherwise it is
``INVISIBLE_AT_OPENING_RECOMMENDED_UNCHECKED``, reported apart. With the check OFF, native chains report per-turn data
but never that pattern. agents/injection_guard.py is an AUXILIARY flag only (INJECTION_SUSPECTED: reported, kept in
the rates, and it too keeps a chain from supporting the native pattern). Every content block sent back must be an
allowed type that this engine returned earlier in this chain (the adapter's ``Conversation`` provenance record;
anything else is refused). An earlier answer goes back as the provider's content blocks exactly as received
(Anthropic: server_tool_use / web_search_tool_result / cited text blocks; Gemini: the parts, PROVISIONAL), never
truncated: a history over the adapter's cap ends the chain INCOMPLETE (``HISTORY_TOO_LARGE``).
Budget: a native turn k carries k-1 earlier answers, so it is reserved and charged as k reserve units (a stacked turn
as 1); a whole chain is reserved before its first turn, so a cap never cuts a chain in half.

Per turn, from the answer (in memory; only a bounded, PII-screened excerpt is kept, and none for a Priya topic):
the client mentioned (agents/brand_mention.py, with its confidence) and cited (the cited HOST is the client's: exact,
www. twin or subdomain via agents/domain_match.py; a lookalike never; a redirector host makes it UNKNOWN — Theo does
not resolve URLs, Callum does), the client's rank among the named entities, each competitor term mentioned, the
competitor domains cited.

The final answer (complete chains only) is read LEXICALLY (``recommendation``): the first entity named, and not
negated or contrasted away (``not``, ``n't``, ``avoid``, ``instead of``, ``rather than``, ``over``, ``<x>er than``,
``... is not recommended``; within the clause; ``not only``, "would not hesitate to recommend", "don't overlook",
"can't recommend X enough" are positive, and "not only X but also Y" / "both X and Y" is a shared pick with no
winner), in the first sentence holding a recommendation cue is ``recommended`` — counted as a recommendation only
when its mention is confirmed (HIGH), the same threshold the opening turn uses; otherwise the first such entity in
the answer is only ``named first in the final answer``, reported apart and never counted as a win. An incomplete
chain's final answer is UNKNOWN.

No agent touches service state, the ledger or the log: the service records what this module returns.
"""

from __future__ import annotations

import hashlib
import re
from typing import Callable, Optional

from agents import brand_mention, domain_match, injection_guard, priya_handoff, split_url
from agents.callum import excerpt
from agents.probes import MIN_USABLE, REDIRECTOR_HOSTS, _switch_code, extract_citations, wilson
from agents.prompt_guard import personal_data
from answer_adapter import PROTOCOL_ERROR_STOP_AFTER, STOP_CODES, EngineError, history_bytes
from envelope import envelope, finding
from ports import NOT_CONNECTED, ProviderAnswer
from primitives import Killed

AGENT, TASK = "theo", "conversation_chains"
VERSION = "theo-rules/2026-11-08.2"
# .2 (review round 1): native multi-turn mode, negation / contrast in the final answer, one confidence threshold at
#    both ends, the client's own outcome decides pattern and strength, context-weighted reservations
FOLLOWUPS_VERSION = "theo-followups/2026-11-08.1"
CONTRACT = "theo.conversation_chains/2"
NATIVE, STACKED = "native_multi_turn", "stacked_questions"
CONTEXT_MODES = (NATIVE, STACKED)
MAX_TURNS = 4
MAX_ROWS = 2000
# review round 2: an earlier answer goes back as the provider's content blocks exactly as received, never cut; a
# history over the adapter's MAX_HISTORY_BYTES makes the chain INCOMPLETE (HISTORY_TOO_LARGE) before any request
HISTORY_BYTES_DEFAULT = 256 * 1024
# instruction-like text aimed at the model (review rounds 2-5): agents/injection_guard.py scans EVERY text an answer
# would carry back (answer text, cited_text, result titles, Gemini parts); a flagged row flags its whole chain
# (INJECTION_SUSPECTED, an AUXILIARY flag since round 5: reported, never the basis of a claim)
PRIYA_FOLLOWUP_MIN_SHARED = 2
FOLLOWUPS = {"compare": "What are the main options to compare for this, and how do they differ?",
             "decide": "Which one would you recommend, and why?"}
PRIYA_FOLLOWUP = "What about this: {query}"
CONTEXT_TEMPLATE = "Earlier in this conversation I asked:\n{earlier}\nMy next question: {question}"
TURN_KINDS = ("opening", "compare", "priya_followup", "decide")
CHAIN_STATUS = ("COMPLETE", "INCOMPLETE", "LOOP_STOPPED", "NOT_ASKED")
OUTCOMES = ("RECOMMENDED", "NOT_RECOMMENDED", "MIXED")
PATTERNS = ("INVISIBLE_THEN_RECOMMENDED", "INVISIBLE_AT_OPENING_RECOMMENDED_UNCHECKED",
            "INVISIBLE_AT_OPENING_RECOMMENDED_STACKED", "VISIBLE_AND_RECOMMENDED", "RECOMMENDED_OPENING_MIXED",
            "VISIBLE_THEN_NOT_RECOMMENDED", "NOT_RECOMMENDED", "MIXED", "INCONCLUSIVE")
RECOMMENDED_PATTERNS = PATTERNS[:5]
NOT_RECOMMENDED_PATTERNS = PATTERNS[5:7]
STRENGTHS = ("STRONG", "WEAK", "NONE")
CUES = re.compile(r"\b(recommend|recommended|recommendation|best choice|best option|top pick|go with|i would choose|"
                  r"i'd choose|my pick|the winner)\b", re.IGNORECASE)
# negation / contrast before an entity, within its clause; and after it ("Acme is not recommended")
_NEG_BEFORE = re.compile(r"(\bnot\b(?!\s+only\b)|n['\u2019]t\b|\bcannot\b|\bnever\b|\bavoid\w*|\binstead of\b|"
                         r"\brather than\b|"
                         r"\bover\b|\bunlike\b|"
                         r"\bstay away from\b|\bskip\b|\bno longer\b|\b\w+er than\b|\b(?:more|less) \w+ than\b|"
                         r"\bsuperior to\b|\bexcept\b)", re.IGNORECASE)
_NEG_AFTER = re.compile(r"^[^.;:!?]{0,30}?\b(is not|isn't|are not|aren't|was not|wasn't|would not|wouldn't|"
                        r"should not|shouldn't|not recommended|falls short|is worse)\b", re.IGNORECASE)
# round 3: "and" followed by a new imperative ("skip X and go with Y") and "then" also start a clause
_CLAUSE = re.compile(r"[.!?\n;,:()—]|\s-\s|\bbut\b|\bhowever\b|\bwhereas\b|\bwhile\b|\bthen\b|"
                     r"\band\s+(?=(?:go|choose|pick|use|try|opt|stick|select|hire|consider|switch|call)\b)",
                     re.IGNORECASE)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_CONTENT = re.compile(r"[^\W\d_]{3,}")
_STOP = frozenset({"the", "and", "for", "with", "what", "which", "who", "how", "why", "when", "where", "does", "are",
                   "can", "best", "near", "about", "this", "that", "your", "from", "into", "than"})
LIMITS = [
    "Context mode is per engine: native_multi_turn (the engine's earlier answers sent back as assistant messages, "
    "as the provider's content blocks exactly as received; Anthropic, and Gemini PROVISIONALLY: its thought-"
    "signature rule was not confirmed in the pages read) or stacked_questions (OpenAI and Perplexity: earlier USER "
    "questions re-sent in one request). A stacked chain approximates a conversation.",
    "In native multi-turn, text injected into an engine's answer is re-sent to that engine and may steer its later "
    "answers, the classified final one included. Theo's plan and logic cannot be altered by it (the user turns are "
    "fixed before turn one), but the engine can. The basis of the defence is structural: the native 'invisible at "
    "turn one, recommended later' pattern is claimed only when every complete chain was re-asked without history "
    "(SEO_THEO_STACKED_CHECK, default OFF) and gave the same pick; with the check OFF native chains report per-turn "
    "data but never that pattern. The lexical injection flag (INJECTION_SUSPECTED) is auxiliary only: an "
    "independent fresh set measured it at recall 0.246 / precision 0.789.",
    "Follow-ups are fixed templates plus at most one of Priya's emerging queries; real users ask other follow-ups. "
    "No LLM-generated follow-up exists (not built).",
    "The final answer is read lexically (a cue word, the first entity named and not negated or contrasted away in "
    "that clause): it does not understand the answer, so a recommendation is never more than MEDIUM confidence, and "
    "an entity merely named first is reported as 'named first in the final answer', never as a recommendation.",
    "Cited means the engine's structured citation names the client's host. Cited URLs are not resolved here (that is "
    "Callum's job): a citation on a redirector host leaves 'cited' UNKNOWN.",
    "Spend on engines that report no cost is a reserve estimate; a native turn carrying k-1 earlier answers is booked "
    "as k reserves, an estimate of its larger context, not a measured cost.",
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

def _host(url: str) -> str:
    p = split_url(url)
    return (p.hostname or "").lower() if p is not None else ""


def _mentions(text: str, ctx: dict) -> list:
    """Every entity mention: (position, kind, name, end, confidence), in text order (whitespace-normalised text)."""
    t = brand_mention._ws_lines(text)
    out = []
    cm = brand_mention.mention(text, ctx["terms"], ctx["rules"], ctx["domain"], client_cited=ctx.get("_cited", False))
    if cm["mentioned"]:
        for r in brand_mention.normalise(ctx["terms"], ctx["rules"]):
            for w, at in brand_mention._matches(t, r):
                out.append((at, "client", None, at + len(w), cm["confidence"]))
    for name in ctx["competitor_terms"]:
        m = brand_mention.mention(text, [name])
        if m["mentioned"]:
            for r in brand_mention.normalise([name], None):
                for w, at in brand_mention._matches(t, r):
                    out.append((at, "competitor", name, at + len(w), m["confidence"]))
    return sorted(out, key=lambda e: (e[0], e[1], e[2] or "")), t


# idioms that look negated and are not (review round 2): "would not hesitate to recommend X", "don't overlook X",
# "can't recommend X enough"
_POSITIVE_BEFORE = re.compile(r"(?:\b(?:not|never)|n['\u2019]t)\s+hesitate\s+to\s+(?:recommend|choose|use|suggest|"
                              r"go\s+with)\s*$|(?:\bdo\s+not|\bdon['\u2019]t|\bnever)\s+(?:overlook|ignore|forget|"
                              r"miss|underestimate|rule\s+out|sleep\s+on)\s*$", re.IGNORECASE)
_CANT_RECOMMEND = re.compile(r"(?:\bcannot|\bcan\s+not|\bcan['\u2019]t|\bcouldn['\u2019]t)\s+recommend\s*$",
                             re.IGNORECASE)
_ENOUGH = re.compile(r"^\s*(?:highly\s+|strongly\s+)?enough\b", re.IGNORECASE)
# round 3: a shared pick only when the construction itself holds an entity on EACH side ("not only X but also Y",
# "both X and Y"); "I recommend X, and both Y and others are weaker" is X's win
_TOGETHER = re.compile(r"\bnot only\b(.{0,80}?)\bbut (?:also|as well)\b(.{0,80})|\bboth\b(.{0,60}?)\band\b(.{0,60})",
                       re.IGNORECASE)


def _together(t: str, start: int, end: int, inside: list) -> list:
    """The distinct entities a shared-pick construction in t[start:end] names on both sides ([] when none)."""
    for m in _TOGETHER.finditer(t, start, end):
        g = (1, 2) if m.group(1) is not None else (3, 4)
        left = {(e[1], e[2]) for e in inside if m.start(g[0]) <= e[0] < m.end(g[0])}
        right = {(e[1], e[2]) for e in inside if m.start(g[1]) <= e[0] < m.end(g[1])}
        if left and right and left | right != left:
            return sorted(left | right, key=lambda x: (x[0], x[1] or ""))
    return []


def negated(t: str, start: int, end: int) -> bool:
    """The mention at [start, end) is negated or contrasted away within its clause (lexical; module docstring)."""
    pre = t[max(0, start - 80):start]
    cut = None
    for cut in _CLAUSE.finditer(pre):
        pass
    if cut is not None:
        pre = pre[cut.end():]
    if _POSITIVE_BEFORE.search(pre) or (_CANT_RECOMMEND.search(pre) and _ENOUGH.match(t[end:end + 30])):
        return False
    return bool(_NEG_BEFORE.search(pre) or _NEG_AFTER.search(t[end:end + 60]))


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
    found, _t = _mentions(text, {**ctx, "_cited": on_client})
    first: dict = {}
    for e in found:
        first.setdefault((e[1], e[2]), e)
    order = sorted(first.values(), key=lambda e: e[0])
    rank = next((i + 1 for i, e in enumerate(order) if e[1] == "client"), None)
    comps = []
    for name in ctx["competitor_terms"]:
        cm = brand_mention.mention(text, [name])
        comps.append({"name": name, "mentioned": cm["mentioned"], "confidence": cm["confidence"]})
    comp_cited = sorted({d for d in ctx["competitor_domains"] for h in hosts if domain_match.on_site(h, d)})
    return {"client": {"mentioned": m["mentioned"], "confidence": m["confidence"], "cited": cited,
                       "rank": rank, "lookalike_cited": lookalike},
            "competitors": comps, "competitor_domains_cited": comp_cited,
            "order": [e[1] if e[1] == "client" else f"competitor:{e[2]}" for e in order],
            "citations": len(cites), "_on_client": on_client}


def recommendation(a: ProviderAnswer, ctx: dict) -> dict:
    """The final answer, lexically (module docstring). ``counts`` is True only for a recommendation the opening
    turn's threshold would also accept: a cue sentence and a confirmed (HIGH) mention."""
    text = a.text or ""
    on_client = observe(a, ctx)["_on_client"]
    found, t = _mentions(text, {**ctx, "_cited": on_client})
    live = [e for e in found if not negated(t, e[0], e[3])]
    for sm in _SENTENCE.finditer(t):
        if not CUES.search(sm.group(0)):
            continue
        inside = [e for e in live if sm.start() <= e[0] < sm.end()]
        shared = _together(t, sm.start(), sm.end(), inside)
        if shared:
            # "not only X but also Y", "both X and Y": several picks, no single decisive one (review round 2)
            return {"winner": "none", "name": None, "basis": "recommended_together", "counts": False,
                    "confidence": "LOW", "together": sorted(k if k == "client" else f"competitor:{n}"
                                                            for k, n in shared),
                    "wording": "several recommended together in the final answer (no single pick)"}
        if inside:
            e = inside[0]
            high = e[4] == brand_mention.HIGH
            return {"winner": e[1], "name": e[2], "basis": "cue_sentence", "counts": high,
                    "confidence": "MEDIUM" if high else "LOW",
                    "wording": "recommended in the final answer" if high else "named first in the final answer"}
    if live:
        e = live[0]
        return {"winner": e[1], "name": e[2], "basis": "named_first", "counts": False, "confidence": "LOW",
                "wording": "named first in the final answer"}
    return {"winner": "none", "name": None, "basis": "none_named", "counts": False, "confidence": "LOW",
            "wording": "no entity named (or every one negated) in the final answer"}


# ---------------------------------------------------------------------------------------------- collection

class CallCap:
    """Theo's own per-run cap on the reserve units ONE conversation run may reserve across every engine
    (``SEO_THEO_RUN_MAX_CALLS``; 0 = OFF: nothing is asked). A whole chain is reserved before its first turn, so the
    cap never cuts a chain in half: a chain that does not fit is not started. A native turn k counts k units."""

    def __init__(self, limit: int):
        self.limit, self.used = limit, 0

    def take(self, calls: int) -> Optional[str]:
        if self.limit <= 0:
            return "BUDGET_OFF"
        if self.used + calls > self.limit:
            return "THEO_RUN_CALL_CAP"
        self.used += calls
        return None


class _ContextMeter:
    """The adapter's meter for one native turn that carries earlier answers: the request must fit ``weight``
    reserves, and a request of unknown cost is charged ``weight`` reserves (fail closed)."""

    def __init__(self, inner, weight: int):
        self.inner, self.weight = inner, weight

    @property
    def per_call(self):
        return getattr(self.inner, "per_call", None)

    @property
    def over_reserve(self):
        return getattr(self.inner, "over_reserve", False)

    def before(self) -> None:
        self.inner.before()
        pc, spent, cap = self.per_call, getattr(self.inner, "spent", None), getattr(self.inner, "reserved_spend", None)
        if pc is not None and spent is not None and cap is not None and spent() + pc * self.weight > cap:
            raise EngineError("ENGINE_BUDGET_EXHAUSTED", "RESERVATION_SPENT")

    def after(self, cost) -> None:
        self.inner.after(cost)
        try:
            unknown = cost is None or float(cost) <= 0
        except (TypeError, ValueError):
            unknown = True
        if unknown and hasattr(self.inner, "charge_context"):
            self.inner.charge_context(self.weight - 1)


def mode_of(port) -> str:
    return NATIVE if getattr(port, "multi_turn", False) is True else STACKED


def units(turns: list, mode: str) -> int:
    n = len(turns)
    return n * (n + 1) // 2 if mode == NATIVE else n


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
             "protocol_run": 0, "chains_asked": 0, "chains_not_asked": 0, "run": None, "mode": mode_of(port)}
        docs = getattr(port, "MULTI_TURN_DOCS", None)
        s["native_status"] = None if s["mode"] != NATIVE else \
            (docs.get("status", "documented") if isinstance(docs, dict) else "unknown")
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
                topic_chains.append(_chains_for(t, ti, plans[t["topic_key"]], e, st[e], n, guard, cap, ctx, clock,
                                                iso, rows))
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
                   "chains_asked": s["chains_asked"], "chains_not_asked": s["chains_not_asked"],
                   "context_mode": s["mode"], "native_status": s["native_status"]}
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
    return _summary(t, e, turns, plan["flags"], samples, s["mode"], s["native_status"])


def _unknown() -> dict:
    return {"winner": "unknown", "name": None, "basis": "chain_incomplete", "counts": False, "confidence": None,
            "wording": "unknown: the chain did not complete"}


def _one_chain(t, ti, si, n, turns, e, s, guard, cap, ctx, clock, iso, rows) -> dict:
    port, mode = s["port"], s["mode"]
    attempts = int(getattr(port, "attempts", 1) or 1)
    check = mode == NATIVE and bool(ctx.get("stacked_check"))
    need = (units(turns, mode) + (1 if check else 0)) * attempts        # the structural check's one extra request
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
    history: list = []
    answers_seen: set = set()
    injected = False
    too_long = False
    # round 3: the adapter's provenance record for this chain (only blocks it returned here may be sent back)
    conv = port.conversation() if mode == NATIVE and callable(getattr(port, "conversation", None)) else None
    status, reason, final = "COMPLETE", None, None
    takes_guard = bool(getattr(port, "accepts_guard", False))
    for k, turn in enumerate(turns):
        if mode == NATIVE and history and history_bytes(history) > int(getattr(port, "MAX_HISTORY_BYTES",
                                                                                 HISTORY_BYTES_DEFAULT)):
            status, reason = "INCOMPLETE", "HISTORY_TOO_LARGE"       # never truncated: what was seen would change
            break
        guard(capability="ai_probe", provider=e)
        row = {"topic_key": t["topic_key"], "engine": e, "sample": si, "turn": k + 1, "kind": turn["kind"],
               "turn_source": turn["source"], "priya_key": turn["priya_key"], "context_mode": mode,
               "observed_at": iso(clock.now())}
        err_code = err_detail = None
        try:
            if mode == NATIVE:
                m = meter if meter is None or k == 0 else _ContextMeter(meter, k + 1)
                a = port.ask(turn["_text"], guard=guard, meter=m, history=list(history) if history else None,
                             conversation=conv)
            else:
                prompt = render(turns, k)
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
        obs.pop("_on_client")
        ex = excerpt(a.text, ctx["terms"]) if t["source"] == "prompt_set" else \
            {"basis": None, "excerpt": None, "withheld": "PRIYA_QUERY_CONTEXT"}
        label = injection_guard.scan_all(injection_guard.resent_texts(a))
        injected = injected or label == "INJECTION_SUSPECTED"   # one flagged row flags the whole chain (round 3)
        too_long = too_long or label == "TOO_LONG_TO_SCAN"       # round 6: its own label, never "injection"
        rows.append({**row, **meta, "status": "ANSWER", **obs, "evidence": ex,
                     **({"injection_suspected": True} if label == "INJECTION_SUSPECTED" else {}),
                     **({"too_long_to_scan": True} if label == "TOO_LONG_TO_SCAN" else {})})
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
        elif mode == NATIVE:
            if not a.text.strip():                      # nothing to send back as the assistant's turn
                status, reason = "INCOMPLETE", "EMPTY_ANSWER_MID_CHAIN"
                break
            blocks = getattr(a, "turn_content", None)
            history += [{"role": "user", "text": turn["_text"]},
                        {"role": "assistant", "text": a.text,
                         **({"content": blocks} if isinstance(blocks, list) and blocks else {})}]
    if status != "COMPLETE":
        final = _unknown()
    divergent = None
    # round 6: no re-ask once the run's cost guard has tripped (the chain still counts; the check stays unchecked)
    over = meter is not None and getattr(meter, "over_reserve", False)
    if check and status == "COMPLETE" and not over:
        divergent, ev = _stacked_check(port, turns, guard, meter, ctx, final)
        if ev is not None:
            rows.append({"topic_key": t["topic_key"], "engine": e, "sample": si, "turn": len(turns) + 1,
                         "kind": "structural_recheck", "turn_source": "template", "priya_key": None,
                         "context_mode": STACKED, "observed_at": iso(clock.now()), **ev})
            injected = injected or ev.get("injection_suspected", False)
            too_long = too_long or ev.get("too_long_to_scan", False)
    return {"sample": si, "status": status, "reason": reason, "turns": done, "final": final,
            "injection_suspected": injected, "too_long_to_scan": too_long, "divergent_under_history": divergent}


def _stacked_check(port, turns, guard, meter, ctx, final) -> tuple:
    """Round 4, the structural second guard (opt-in, SEO_THEO_STACKED_CHECK): the final question asked once more as
    stacked questions, with NO earlier answers in context. True when the lexical final recommendation differs from the
    native chain's (DIVERGENT_UNDER_HISTORY: the history, an injected one included, may have moved the answer); None
    when the check could not be made. A heuristic: engines vary between samples anyway. Returns (divergent, evidence
    row fields or None); the re-ask's answer is scanned by the auxiliary guard too (round 6)."""
    try:
        guard(capability="ai_probe", provider=getattr(port, "name", None))
        a = port.ask(render(turns, len(turns) - 1), guard=guard, meter=meter)
    except Killed:                                     # a kill switch or a member cancellation stops the run
        raise
    except EngineError as err:
        if err.code == "ENGINE_KILLED":                # review round 5: never swallowed
            raise Killed(_switch_code(err)) from None
        return None, {"status": "FAILED", "error_code": err.code, "error_detail": err.detail}
    except Exception:  # noqa: BLE001 - a failed check is no finding, never a failed chain
        return None, {"status": "FAILED", "error_code": "ENGINE_UNAVAILABLE", "error_detail": "ADAPTER_FAILED"}
    if not isinstance(a, ProviderAnswer) or a.status != "ANSWER" or not isinstance(a.text, str):
        st = a.status if isinstance(a, ProviderAnswer) and a.status in ("REFUSAL", "ERROR") else "FAILED"
        return None, {"status": st}
    alt = recommendation(a, ctx)
    label = injection_guard.scan_all(injection_guard.resent_texts(a))
    divergent = (alt["winner"], alt["name"], alt["counts"]) != (final["winner"], final["name"], final["counts"])
    ev = {"status": "ANSWER", "model": a.model, "model_version": a.model_version, "provider_asked_at": a.asked_at,
          "answer_sha256": a.answer_sha256 or _sha(a.text),
          "recheck": {"winner": alt["winner"], "name": alt["name"], "counts": alt["counts"], "divergent": divergent},
          **({"injection_suspected": True} if label == "INJECTION_SUSPECTED" else {}),
          **({"too_long_to_scan": True} if label == "TOO_LONG_TO_SCAN" else {})}
    return divergent, ev


# ---------------------------------------------------------------------------------------------- summaries

def _rate(k: int, nn: int) -> dict:
    return {"value": None if nn == 0 else round(k / nn, 4), "k": k, "n": nn, "ci95": wilson(k, nn),
            "interval": "wilson_95", "directional": nn < MIN_USABLE}


def client_outcome(rec: int, nc: int) -> Optional[str]:
    """The client's own outcome over complete chains: RECOMMENDED in at least 2/3, NOT_RECOMMENDED in none, else
    MIXED (a 1-of-5 win is MIXED, never a strong recommendation)."""
    if nc == 0:
        return None
    if rec * 3 >= nc * 2:
        return "RECOMMENDED"
    return "NOT_RECOMMENDED" if rec == 0 else "MIXED"


def pattern_of(outcome: Optional[str], visible: int, any_mention: int, nc: int, mode: str,
               structurally_agreed: bool = False) -> str:
    """One rule, used by Theo and re-checked by Zara. Visible at turn one = a confirmed mention in at least 2/3 of
    the complete chains; invisible = no mention of ANY confidence in any of them. Review round 5: the native claim
    "invisible at turn one, recommended later" needs the STRUCTURAL check on EVERY complete chain — re-asked without
    history, same pick, and no auxiliary injection flag (``structurally_agreed``); otherwise the chain set is
    ``INVISIBLE_AT_OPENING_RECOMMENDED_UNCHECKED``, reported apart."""
    if outcome is None:
        return "INCONCLUSIVE"
    if outcome == "MIXED":
        return "MIXED"
    vis = visible * 3 >= nc * 2
    if outcome == "RECOMMENDED":
        if any_mention == 0:
            if mode != NATIVE:
                return "INVISIBLE_AT_OPENING_RECOMMENDED_STACKED"
            return "INVISIBLE_THEN_RECOMMENDED" if structurally_agreed else \
                "INVISIBLE_AT_OPENING_RECOMMENDED_UNCHECKED"
        return "VISIBLE_AND_RECOMMENDED" if vis else "RECOMMENDED_OPENING_MIXED"
    return "VISIBLE_THEN_NOT_RECOMMENDED" if vis else "NOT_RECOMMENDED"


def _summary(t: dict, e: str, turns: list, flags: list, samples: list, mode: str,
             native_status: Optional[str] = None) -> dict:
    # review round 5: the regex injection flag is AUXILIARY: flagged chains stay in the rates, are reported, and
    # (like an unchecked or divergent chain) cannot support the native "invisible -> recommended" pattern
    complete = [c for c in samples if c["status"] == "COMPLETE"]
    injected = [c for c in samples if c.get("injection_suspected")]
    agreed = [c for c in complete if c.get("divergent_under_history") is False and not c.get("injection_suspected")
              and not c.get("too_long_to_scan")]
    too_long = [c for c in samples if c.get("too_long_to_scan")]
    by_turn = []
    for k, turn in enumerate(turns):
        ans = [c["turns"][k] for c in complete]
        by_turn.append({"turn": k + 1, "kind": turn["kind"], "source": turn["source"],
                        "client_mentioned": _rate(sum(1 for x in ans if x["client"]["mentioned"]
                                                      and x["client"]["confidence"] == brand_mention.HIGH), len(ans)),
                        "client_mentioned_unconfirmed": sum(1 for x in ans if x["client"]["mentioned"]
                                                            and x["client"]["confidence"] != brand_mention.HIGH),
                        "client_cited": _rate(sum(1 for x in ans if x["client"]["cited"] is True), len(ans)),
                        "client_cited_unknown": sum(1 for x in ans if x["client"]["cited"] is None),
                        "competitor_mentioned": _rate(sum(1 for x in ans if x["competitors_mentioned"]), len(ans))})
    rec = {"client": 0, "competitor": {}}
    named = {"client": 0, "competitor": {}}
    none = 0
    winners = []
    for c in complete:
        f = c["final"]
        if f["winner"] == "none":
            none += 1
            winners.append("no_recommendation")
            continue
        box = rec if f["counts"] else named
        if f["winner"] == "client":
            box["client"] += 1
        else:
            box["competitor"][f["name"]] = box["competitor"].get(f["name"], 0) + 1
        winners.append(("client" if f["winner"] == "client" else "competitor:" + f["name"]) if f["counts"]
                       else "no_recommendation")
    incomplete: dict = {}
    for c in samples:
        if c["status"] != "COMPLETE":
            key = c["reason"] or c["status"]
            incomplete[key] = incomplete.get(key, 0) + 1
    nc = len(complete)
    outcome = client_outcome(rec["client"], nc)
    first = by_turn[0]
    pattern = pattern_of(outcome, first["client_mentioned"]["k"],
                         first["client_mentioned"]["k"] + first["client_mentioned_unconfirmed"], nc, mode,
                         nc > 0 and len(agreed) == nc)
    strength = "STRONG" if nc >= MIN_USABLE and outcome in ("RECOMMENDED", "NOT_RECOMMENDED") else \
        "WEAK" if nc else "NONE"
    return {"topic_key": t["topic_key"], "engine": e, "context_mode": mode, "native_status": native_status,
            "divergent_under_history": {"checked": sum(1 for c in samples if c.get("divergent_under_history")
                                                       is not None),
                                        "divergent": sum(1 for c in samples if c.get("divergent_under_history")),
                                        "agreed_clean": len(agreed),
                                        "note": "heuristic: the final question re-asked without the earlier answers "
                                                "(opt-in SEO_THEO_STACKED_CHECK); a different pick may be the "
                                                "history's influence, or ordinary sampling variation"},
            "too_long_to_scan": {"chains": len(too_long),
                                 "complete": sum(1 for c in too_long if c["status"] == "COMPLETE"),
                                 "note": "a re-sent text over the guard's scan limit was not scanned; not an "
                                         "injection finding, but it cannot support the native pattern"},
            "injection_suspected": {"chains": len(injected), "complete": sum(1 for c in injected
                                                                             if c["status"] == "COMPLETE"),
                                    "note": "AUXILIARY lexical flag (instruction-like text in a re-sent answer); "
                                            "not a reliable detector: flagged chains stay in the rates and cannot "
                                            "support the native invisible-then-recommended pattern"},
            "chains": len(samples), "complete": nc,
            "loop_stopped": sum(1 for c in samples if c["status"] == "LOOP_STOPPED"),
            "not_complete": dict(sorted(incomplete.items())), "turns_planned": len(turns),
            "plan_flags": list(flags), "by_turn": by_turn,
            "final_recommendation": {"recommended": {"client": rec["client"],
                                                     "competitor": dict(sorted(rec["competitor"].items()))},
                                     "named_first_only": {"client": named["client"],
                                                          "competitor": dict(sorted(named["competitor"].items()))},
                                     "none": none, "unknown": len(samples) - nc,
                                     "basis": "lexical: a cue sentence and a confirmed mention, negation-aware; "
                                              "'named first in the final answer' is reported apart, never a win",
                                     "max_confidence": "MEDIUM"},
            "client_recommended": _rate(rec["client"], nc), "client_outcome": outcome,
            "unstable": len(set(winners)) > 1, "pattern": pattern, "strength": strength}


# ---------------------------------------------------------------------------------------------- the envelope

def output(tenant_id: str, run_id: str, topics: list, plans: dict, res: dict, n: int) -> dict:
    """The Theo -> Zara hand-off (``CONTRACT``)."""
    return {"contract": CONTRACT, "producer": AGENT, "tenant_id": tenant_id, "run_id": run_id, "rules": VERSION,
            "followups": FOLLOWUPS_VERSION,
            "context_modes": {e: v["context_mode"] for e, v in res["engines"].items()}, "samples_per_chain": n,
            "topics": [{"topic_key": t["topic_key"], "source": t["source"], "priya_key": t["priya_key"],
                        "turns_planned": [{"kind": x["kind"], "source": x["source"], "priya_key": x["priya_key"]}
                                          for x in plans[t["topic_key"]]["turns"]]} for t in topics],
            "chains": res["chains"]}


def envelope_for(outcome: str, res: Optional[dict], contract: Optional[dict], topics: list, plans: dict,
                 reason: Optional[str] = None) -> dict:
    method = (f"Each topic asked as a {MAX_TURNS}-turn-at-most chain (opening, compare, at most one of Priya's "
              "emerging queries, decide), the user side planned before the first question from the topic and "
              f"Priya's items only ({FOLLOWUPS_VERSION}); per engine native_multi_turn (earlier answers sent back "
              "as assistant messages) or stacked_questions (earlier questions only); each chain sampled N times. "
              "Per turn: client and competitor mentions (whole-word, with confidence), client rank, client host "
              "cited. Final answer read lexically on COMPLETE chains only (negation-aware; a recommendation needs a "
              "cue and a confirmed mention, as the opening does). Pattern and strength from the client's own "
              f"outcome. Rates over complete chains, 95% Wilson intervals, directional under {MIN_USABLE}. "
              f"Rules {VERSION}.")
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
             "context_mode": c["context_mode"], "pattern": c["pattern"],
             "client_recommended": c["client_recommended"]}
        dec = "INSUFFICIENT_EVIDENCE" if c["complete"] < MIN_USABLE else "TEST"
        if c["pattern"] == "INVISIBLE_THEN_RECOMMENDED":            # native, structurally checked on every chain
            out.append(finding("THEO_INVISIBLE_THEN_RECOMMENDED", "info", "measured", dec, capability="P4", detail=d))
        elif c["pattern"] == "INVISIBLE_AT_OPENING_RECOMMENDED_UNCHECKED":
            dv = c["divergent_under_history"]
            out.append(finding("THEO_NATIVE_PATTERN_NOT_STRUCTURALLY_CHECKED", "info", "measured",
                               "INSUFFICIENT_EVIDENCE", capability="P4",
                               detail={**d, "complete_chains": c["complete"], "checked": dv["checked"],
                                       "divergent": dv["divergent"], "agreed_clean": dv["agreed_clean"],
                                       "meaning": "the client was not named at turn one and was recommended later, "
                                                  "but not every chain was re-asked without history with the same "
                                                  "pick (check off, failed or divergent, or a re-sent text flagged "
                                                  "or too long to scan); no claim is made"}))
        elif c["pattern"] == "INVISIBLE_AT_OPENING_RECOMMENDED_STACKED":
            out.append(finding("THEO_RECOMMENDED_WHEN_QUESTIONS_STACKED", "info", "measured",
                               "INSUFFICIENT_EVIDENCE", capability="P4", detail=d))
        elif c["pattern"] == "VISIBLE_THEN_NOT_RECOMMENDED":
            out.append(finding("THEO_VISIBLE_THEN_NOT_RECOMMENDED", "medium", "measured", dec, capability="P4",
                               detail=d))
        elif c["pattern"] == "MIXED":
            out.append(finding("THEO_CHAIN_MIXED", "low", "measured", "WATCH", capability="P4", detail=d))
        if c["final_recommendation"]["recommended"]["competitor"]:
            out.append(finding("THEO_COMPETITOR_RECOMMENDED", "medium", "measured", dec, capability="P9",
                               detail={**d, "competitor": c["final_recommendation"]["recommended"]["competitor"]}))
        if c["unstable"]:
            out.append(finding("THEO_CHAIN_UNSTABLE", "low", "measured", "WATCH", capability="P4", detail=d))
        if c["loop_stopped"]:
            out.append(finding("THEO_CHAIN_LOOP_STOPPED", "low", "measured", "WATCH", capability="P4",
                               detail={**d, "loop_stopped": c["loop_stopped"]}))
        if c["divergent_under_history"]["divergent"]:
            out.append(finding("THEO_DIVERGENT_UNDER_HISTORY", "low", "measured", "WATCH", capability="P4",
                               detail={**d, **{k: c["divergent_under_history"][k] for k in ("checked", "divergent")}}))
    for e, v in sorted(res["engines"].items()):
        if v["outcome"] in ("BLOCKED", "FAILED") or (v["outcome"] == "PARTIAL" and v["reason"]):
            out.append(finding("ENGINE_RUN_STOPPED", "low", "measured", "WATCH", capability="P4",
                               detail={"engine": e, "reason": v["reason"], "outcome": v["outcome"]}))
    return out
