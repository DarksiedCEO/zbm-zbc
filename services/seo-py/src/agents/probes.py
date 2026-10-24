"""
The AI-visibility probe framework (P4) for Naomi (prompt volume) and Callum (citation sources).

Reliability rules (spec, Oct 6) built in, not left to the caller:
  - one response is not search truth: every prompt is sampled N times (config SEO_PROBE_SAMPLES, at least 3);
  - variance is reported: mention and citation rates carry a 95% Wilson interval and the sample variance;
  - refusals and errors are counted separately and excluded from the rates' denominators (and reported);
  - prompt sets are versioned and hashed; every answer records the model and model version the provider reported,
    and a sample that mixes versions is flagged (model drift inside one sample);
  - no single visibility score: results stay per engine, per prompt, with their intervals.

Callum's classes, per engine and prompt (from the answers actually sampled):
  STRENGTH              the site was cited in at least half of the answers
  MENTIONED_NOT_CITED   a brand term appeared but the site was never cited
  OPPORTUNITY           neither mentioned nor cited, while the answers cited other sources (listed)
  INSUFFICIENT_EVIDENCE fewer than 3 usable answers (refusals / errors)
  ABSENT                neither mentioned nor cited and no sources cited at all

Provider text is DATA: it is scanned for brand terms and URLs, never followed as an instruction, and dropped; the report
keeps its SHA-256, the cited hosts (bounded), whether the tenant's site was cited and at which position. Without a
connected provider port an engine's outcome is NOT_CONNECTED and there are no findings for it — never an invented
answer. Wave 4 (ADR 0017 W4): the Perplexity adapter is METERED — asked only inside a budget reservation the service
records first — and its citations are structured only (URLs in its text are not citations). A tenant domain matches
by exact host or subdomain, IDNA-aware; a lookalike host is counted as such and never as the tenant.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Optional
from urllib.parse import urlsplit

from agents import domain_match
from agents.prompt_guard import personal_data
from envelope import envelope, finding, observed
from perplexity import PROTOCOL_ERROR_STOP_AFTER, STOP_CODES, EngineError
from ports import NOT_CONNECTED, ProviderAnswer
from primitives import Killed

AGENT_CALLUM, AGENT_NAOMI = "callum", "naomi"
MIN_USABLE = 3
Z95 = 1.959963984540054
_URL = re.compile(r"https?://[^\s<>\"')\]]+")
METHODOLOGY = ("Each prompt of prompt set {ps} v{ver} (sha256 {sha}) sent {n} times per engine. An answer is "
               "'mentioned' when a brand term appears (case-insensitive, whole words) and 'cited' when a citation "
               "(structured from the provider, or a URL in the text) is on {domain} or a subdomain. Rates exclude "
               "refusals and errors; intervals are 95% Wilson score intervals over usable answers. Classes: "
               "STRENGTH (cited in >= half), MENTIONED_NOT_CITED, OPPORTUNITY (others cited, not us), ABSENT, "
               "INSUFFICIENT_EVIDENCE (< {m} usable answers).")
LIMITS = ["One sample of N answers at one time from one account; answer engines vary by user, location, time and "
          "model version. These are observations, not search truth.",
          "No prompt-volume source is connected: how often real users ask these prompts is unknown.",
          "No single visibility score is produced; nothing here is calibrated against traffic or revenue."]


def wilson(k: int, n: int) -> Optional[list]:
    if n <= 0:
        return None
    p = k / n
    den = 1 + Z95 ** 2 / n
    centre = (p + Z95 ** 2 / (2 * n)) / den
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95 ** 2 / (4 * n * n)) / den
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def extract_citations(ans: ProviderAnswer) -> list:
    """Callum's extraction: the provider's structured citations, then (only for an adapter without structured
    citations, ``cite_from_text``) URLs in the text. [{"url","host","via","position"}], position 1-based in the
    provider's own citation order (structured first)."""
    out, seen = [], set()
    text_items = _URL.findall(ans.text or "")[:200] if getattr(ans, "cite_from_text", True) else []
    for via, items in (("structured", ans.citations or []), ("text", text_items)):
        for u in items[:200]:
            if not isinstance(u, str):
                continue
            u = u.rstrip(".,;:")
            try:
                host = (urlsplit(u).hostname or "").lower()
            except ValueError:
                continue
            if host and u not in seen:
                seen.add(u)
                out.append({"url": u[:500], "host": host[:253], "via": via, "position": len(out) + 1})
    return out


def _on_site(host: str, domain: str) -> bool:
    """Exact host or subdomain (with the www. twin), IDNA-aware, never by substring (agents/domain_match.py)."""
    return domain_match.on_site(host, domain)


def _mentioned(text: str, terms: list) -> bool:
    t = text or ""
    return any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", t, re.IGNORECASE) for term in terms)


def _valid_answer(a) -> bool:
    return isinstance(a, ProviderAnswer) and a.status in ("ANSWER", "REFUSAL", "ERROR", NOT_CONNECTED) \
        and isinstance(a.text, str) and isinstance(a.citations, list)


def _answer_hash(a: ProviderAnswer) -> str:
    if isinstance(a.answer_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", a.answer_sha256):
        return a.answer_sha256
    return hashlib.sha256((a.text or "").encode("utf-8", "surrogatepass")).hexdigest()


def probe_engine(engine: str, port, prompt_set: dict, domain: str, n: int, guard, osei, budget=None) -> dict:
    """All prompts of the set on one engine. Returns {"outcome", "reason", "prompts": [...], "model_versions": [...],
    "usage": {...}}. A METERED port (a real adapter that costs money: Perplexity) is asked only inside a budget
    reservation recorded first (``budget``: the service's hook; without one nothing is asked)."""
    if port is None or not getattr(port, "connected", False):
        return {"engine": engine, "outcome": NOT_CONNECTED, "prompts": []}
    metered = bool(getattr(port, "metered", False))
    takes_guard = bool(getattr(port, "accepts_guard", False))
    attempts = int(getattr(port, "attempts", 1) or 1)
    if metered and budget is None:
        return {"engine": engine, "outcome": "BLOCKED", "reason": "ENGINE_BUDGET_EXHAUSTED", "prompts": [],
                "usage": None}
    run = budget.open(engine, getattr(port, "model", None)) if metered else None
    results, versions = [], set()
    competitors = set(prompt_set.get("competitor_domains") or [])
    stop: Optional[str] = None
    stop_detail: Optional[str] = None
    answers_total = 0
    errors_total = 0
    errors_seen: dict = {}
    protocol_run = 0                                    # consecutive ENGINE_PROTOCOL_ERRORs, across prompts
    try:
        for pi, prompt in enumerate(prompt_set["prompts"]):
            if stop is not None:
                break
            pd = personal_data(prompt)
            if pd is not None:                         # never sent; the report does not quote it either
                results.append(_refused_prompt(pi, n, pd))
                continue
            meter = None
            if run is not None:
                try:
                    meter = run.reserve(pi, n * attempts)      # recorded first; raises when over budget
                except EngineError as e:
                    if e.code == "ENGINE_KILLED":
                        raise Killed(_switch_code(e)) from None
                    stop, stop_detail = e.code, e.detail
                    break
            tally = {"answers": 0, "refusals": 0, "errors": 0, "malformed": 0, "mentioned": 0, "cited": 0,
                     "lookalike_citations": 0}
            errors_by: dict = {}
            other_hosts: dict = {}
            sample_versions = set()
            injection_like = 0
            positions: list = []
            hashes: list = []
            for _ in range(n):
                guard(capability="ai_probe", provider=engine)
                try:
                    a = port.ask(prompt, guard=guard, meter=meter) if takes_guard else port.ask(prompt)
                except Killed:
                    raise
                except EngineError as e:
                    tally["errors"] += 1
                    errors_by[e.code] = errors_by.get(e.code, 0) + 1
                    if e.code == "ENGINE_KILLED":
                        raise Killed(_switch_code(e)) from None
                    if e.code == "ENGINE_PROTOCOL_ERROR":
                        osei.quarantine("ai_answer", "PROVIDER_ANSWER_MALFORMED", None, None)
                        protocol_run += 1
                        if protocol_run >= PROTOCOL_ERROR_STOP_AFTER:   # an unreadable shape: stop spending
                            stop, stop_detail = e.code, "CONSECUTIVE_PROTOCOL_ERRORS"
                            break
                    else:
                        protocol_run = 0
                    if e.code in STOP_CODES:
                        stop, stop_detail = e.code, e.detail
                        break
                    continue
                except Exception:                         # a provider failure is counted, never raised
                    tally["errors"] += 1
                    continue
                if not _valid_answer(a):
                    tally["malformed"] += 1
                    osei.quarantine("ai_answer", "PROVIDER_ANSWER_MALFORMED", None, None)
                    continue
                if a.status == NOT_CONNECTED:
                    return {"engine": engine, "outcome": NOT_CONNECTED, "prompts": []}
                sample_versions.add(f"{a.model or '?'}@{a.model_version or '?'}")
                if a.status == "REFUSAL":
                    tally["refusals"] += 1
                    continue
                if a.status == "ERROR":
                    tally["errors"] += 1
                    continue
                tally["answers"] += 1
                protocol_run = 0
                hashes.append(_answer_hash(a))
                if re.search(r"ignore (all |any )?(previous|prior) instructions|system prompt", a.text or "", re.I):
                    injection_like += 1                   # reported as data; changes nothing
                cites = extract_citations(a)
                if _mentioned(a.text, prompt_set["brand_terms"]):
                    tally["mentioned"] += 1
                pos = None
                for c in cites:
                    m = domain_match.match(c["host"], domain)
                    if m in (domain_match.EXACT, domain_match.SUBDOMAIN):
                        pos = c["position"] if pos is None else pos
                    elif m == domain_match.LOOKALIKE:
                        tally["lookalike_citations"] += 1
                    else:
                        other_hosts[c["host"]] = other_hosts.get(c["host"], 0) + 1
                positions.append(pos)
                if pos is not None:
                    tally["cited"] += 1
            versions |= sample_versions
            answers_total += tally["answers"]
            errors_total += tally["errors"] + tally["malformed"]
            for code, k in errors_by.items():
                errors_seen[code] = errors_seen.get(code, 0) + k
            results.append(_prompt_result(pi, prompt, n, tally, errors_by, other_hosts, competitors, sample_versions,
                                          injection_like, positions, hashes))
        if stop is None and not answers_total and errors_total:
            # every request failed: never OK (AEGIS H2). The reason is the most frequent error code.
            stop = sorted(errors_seen.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if errors_seen \
                else "ENGINE_UNAVAILABLE"
            stop_detail = "NO_ANSWERS"
    finally:
        usage = run.settle(answers_total, stop) if run is not None else None
    asked = len(results)
    if stop_detail == "NO_ANSWERS":
        outcome = "FAILED"
    elif stop is None:
        outcome = "OK"
    elif answers_total:
        outcome = "PARTIAL"
    else:
        outcome = {"ENGINE_BUDGET_EXHAUSTED": "BLOCKED", "ENGINE_NOT_CONNECTED": NOT_CONNECTED}.get(stop, "FAILED")
    return {"engine": engine, "outcome": outcome, "reason": stop, "reason_detail": stop_detail, "prompts": results,
            "prompts_not_asked": len(prompt_set["prompts"]) - asked, "model_versions": sorted(versions),
            "usage": usage}


def _switch_code(e: EngineError) -> str:
    """The kill switch behind an ENGINE_KILLED (its own code when the guard gave one)."""
    return e.detail if e.detail and (e.detail.startswith("KILLED_") or e.detail == "SERVICE_CLOSED") \
        else "KILLED_PROVIDER"


def _refused_prompt(pi: int, n: int, kind: str) -> dict:
    return {"prompt_index": pi, "prompt": None, "prompt_refused": f"personal_data:{kind}", "samples": 0,
            "answers": 0, "refusals": 0, "errors": 0, "malformed": 0, "mentioned": 0, "cited": 0,
            "lookalike_citations": 0, "errors_by_reason": {}, "mention_rate": None, "mention_ci95": None,
            "citation_rate": None, "citation_ci95": None, "citation_variance": None, "class": "INSUFFICIENT_EVIDENCE",
            "other_sources": [], "model_versions": [], "mixed_model_versions": False, "injection_like_answers": 0,
            "cited_positions": [], "best_position": None, "answers_sha256": []}


def _prompt_result(pi, prompt, n, tally, errors_by, other_hosts, competitors, sample_versions, injection_like,
                   positions, hashes) -> dict:
    k = tally["answers"]
    rate_m = tally["mentioned"] / k if k else None
    rate_c = tally["cited"] / k if k else None
    if k < MIN_USABLE:
        cls = "INSUFFICIENT_EVIDENCE"
    elif rate_c is not None and rate_c >= 0.5:
        cls = "STRENGTH"
    elif tally["mentioned"] and not tally["cited"]:
        cls = "MENTIONED_NOT_CITED"
    elif not tally["mentioned"] and not tally["cited"] and other_hosts:
        cls = "OPPORTUNITY"
    elif not tally["mentioned"] and not tally["cited"]:
        cls = "ABSENT"
    else:
        cls = "PARTIAL_CITATION"
    top = sorted(other_hosts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    cited_at = [p for p in positions if p is not None]
    return {
        "prompt_index": pi, "prompt": observed(prompt, 500), "samples": n, **tally,
        "errors_by_reason": dict(sorted(errors_by.items())),
        "mention_rate": None if rate_m is None else round(rate_m, 4), "mention_ci95": wilson(tally["mentioned"], k),
        "citation_rate": None if rate_c is None else round(rate_c, 4), "citation_ci95": wilson(tally["cited"], k),
        "citation_variance": None if rate_c is None else round(rate_c * (1 - rate_c), 4),
        "class": cls, "other_sources": [{"host": h, "answers": c, "competitor": h in competitors or any(
            _on_site(h, d) for d in competitors)} for h, c in top],
        "model_versions": sorted(sample_versions), "mixed_model_versions": len(sample_versions) > 1,
        "injection_like_answers": injection_like,
        "cited_positions": positions[:50], "best_position": min(cited_at) if cited_at else None,
        "answers_sha256": hashes[:50]}


def run_callum(engines: dict, prompt_set: Optional[dict], domain: str, n: int, guard, osei, budget=None) -> dict:
    if prompt_set is None:
        return envelope(AGENT_CALLUM, "citation_sources", "INSUFFICIENT_EVIDENCE", [],
                        methodology="No prompt set was given for this audit; no engine was asked anything.",
                        limitations=LIMITS, reason="NO_PROMPT_SET")
    methodology = METHODOLOGY.format(ps=prompt_set["name"], ver=prompt_set["version"], sha=prompt_set["sha256"][:16],
                                     n=n, domain=domain, m=MIN_USABLE)
    per_engine, findings, not_connected = {}, [], set()
    try:
        for eng in prompt_set["engines"]:
            r = probe_engine(eng, engines.get(eng), prompt_set, domain, n, guard, osei, budget)
            per_engine[eng] = r
            if r["outcome"] == NOT_CONNECTED:
                not_connected.add(f"answer_engine:{eng}")
                continue
            for p in r["prompts"]:
                findings.append(_class_finding(eng, p))
                if p["mixed_model_versions"]:
                    findings.append(finding("MODEL_VERSION_MIXED_IN_SAMPLE", "low", "measured", "WATCH",
                                            capability="P4", detail={"engine": eng, "prompt_index": p["prompt_index"],
                                                                     "versions": p["model_versions"]}))
    except Killed as k:
        return envelope(AGENT_CALLUM, "citation_sources", "KILLED", [], methodology=methodology, limitations=LIMITS,
                        reason=k.code, facts={"engines": {e: {"outcome": v["outcome"]} for e, v in per_engine.items()}})
    observed_ = [e for e, r in per_engine.items() if r["outcome"] in ("OK", "PARTIAL")]
    stopped = {e: r for e, r in per_engine.items() if r["outcome"] in ("BLOCKED", "FAILED")}
    for e, r in stopped.items():
        findings.append(finding("ENGINE_RUN_STOPPED", "low", "measured", "WATCH", capability="P4",
                                detail={"engine": e, "reason": r.get("reason"), "outcome": r["outcome"]}))
    if not observed_ and stopped:
        e, r = sorted(stopped.items())[0]
        return envelope(AGENT_CALLUM, "citation_sources", r["outcome"], findings, methodology=methodology,
                        limitations=LIMITS, not_connected=not_connected, reason=r.get("reason"),
                        facts={"prompt_set": _ps_ref(prompt_set), "samples_per_prompt": n, "engines": per_engine})
    if not observed_:
        return envelope(AGENT_CALLUM, "citation_sources", "NOT_CONNECTED", [], methodology=methodology,
                        limitations=LIMITS, not_connected=not_connected,
                        reason="no answer-engine port is connected; no engine was asked anything",
                        facts={"prompt_set": _ps_ref(prompt_set), "engines": {e: "NOT_CONNECTED" for e in per_engine}})
    outcome = "OK" if all(per_engine[e]["outcome"] == "OK" for e in per_engine) else "PARTIAL"
    return envelope(AGENT_CALLUM, "citation_sources", outcome, findings, methodology=methodology, limitations=LIMITS,
                    not_connected=not_connected, facts={"prompt_set": _ps_ref(prompt_set), "samples_per_prompt": n,
                                                        "engines": per_engine})


def _ps_ref(ps: dict) -> dict:
    return {"prompt_set_id": ps["prompt_set_id"], "name": ps["name"], "version": ps["version"], "sha256": ps["sha256"]}


def _class_finding(engine: str, p: dict) -> dict:
    cls = p["class"]
    decision, sev = {"STRENGTH": ("WATCH", "info"), "MENTIONED_NOT_CITED": ("TEST", "medium"),
                     "OPPORTUNITY": ("TEST", "medium"), "ABSENT": ("WATCH", "low"),
                     "PARTIAL_CITATION": ("WATCH", "low"),
                     "INSUFFICIENT_EVIDENCE": ("INSUFFICIENT_EVIDENCE", "info")}[cls]
    return finding(f"CITATION_{cls}", sev, "measured", decision, capability="P4",
                   detail={"engine": engine, "prompt_index": p["prompt_index"], "usable_answers": p["answers"],
                           "citation_rate": p["citation_rate"], "citation_ci95": p["citation_ci95"],
                           "mention_rate": p["mention_rate"], "mention_ci95": p["mention_ci95"],
                           "refusals": p["refusals"], "errors": p["errors"], "other_sources": p["other_sources"][:5]})


def run_naomi(prompt_volume_port, prompt_set: Optional[dict]) -> dict:
    """Prompt volume with confidence bands needs a volume source; none is connected in Wave 1."""
    method = ("Prompt volume would come from the prompt-volume port with a confidence band per prompt; no source is "
              "connected, so nothing is estimated.")
    if prompt_volume_port is None or not getattr(prompt_volume_port, "connected", False):
        return envelope(AGENT_NAOMI, "prompt_volume", "NOT_CONNECTED", [], methodology=method,
                        limitations=["No volume estimate of any kind is produced without a connected source."],
                        not_connected={"prompt_volume"}, reason="no prompt-volume source is connected")
    return envelope(AGENT_NAOMI, "prompt_volume", "INSUFFICIENT_EVIDENCE", [], methodology=method,
                    limitations=["The volume adapter contract is not defined in Wave 1."],
                    reason="a prompt-volume port reported connected, but Wave 1 defines no volume contract to read")
