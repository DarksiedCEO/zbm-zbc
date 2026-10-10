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

Provider text is DATA: it is scanned for brand terms and URLs, never followed as an instruction. Without a connected
provider port (Wave 1: none is built) an engine's outcome is NOT_CONNECTED and there are no findings for it — never
an invented answer.
"""

from __future__ import annotations

import math
import re
from typing import Optional
from urllib.parse import urlsplit

from envelope import envelope, finding, observed
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
    """Callum's extraction: the provider's structured citations, then URLs in the text. [{"url","host","via"}]."""
    out, seen = [], set()
    for via, items in (("structured", ans.citations or []), ("text", _URL.findall(ans.text or "")[:200])):
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
                out.append({"url": u[:500], "host": host, "via": via})
    return out


def _on_site(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain) or ("www." + host) == domain


def _mentioned(text: str, terms: list) -> bool:
    t = text or ""
    return any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", t, re.IGNORECASE) for term in terms)


def _valid_answer(a) -> bool:
    return isinstance(a, ProviderAnswer) and a.status in ("ANSWER", "REFUSAL", "ERROR", NOT_CONNECTED) \
        and isinstance(a.text, str) and isinstance(a.citations, list)


def probe_engine(engine: str, port, prompt_set: dict, domain: str, n: int, guard, osei) -> dict:
    """All prompts of the set on one engine. Returns {"outcome", "prompts": [...], "model_versions": [...]}."""
    if port is None or not getattr(port, "connected", False):
        return {"engine": engine, "outcome": NOT_CONNECTED, "prompts": []}
    results, versions = [], set()
    competitors = set(prompt_set.get("competitor_domains") or [])
    for pi, prompt in enumerate(prompt_set["prompts"]):
        tally = {"answers": 0, "refusals": 0, "errors": 0, "malformed": 0, "mentioned": 0, "cited": 0}
        other_hosts: dict = {}
        sample_versions = set()
        injection_like = 0
        for _ in range(n):
            guard(capability="ai_probe", provider=engine)
            try:
                a = port.ask(prompt)
            except Killed:
                raise
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
            if re.search(r"ignore (all |any )?(previous|prior) instructions|system prompt", a.text or "", re.I):
                injection_like += 1                   # reported as data; changes nothing
            cites = extract_citations(a)
            if _mentioned(a.text, prompt_set["brand_terms"]):
                tally["mentioned"] += 1
            if any(_on_site(c["host"], domain) for c in cites):
                tally["cited"] += 1
            for c in cites:
                if not _on_site(c["host"], domain):
                    other_hosts[c["host"]] = other_hosts.get(c["host"], 0) + 1
        versions |= sample_versions
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
        results.append({
            "prompt_index": pi, "prompt": observed(prompt, 500), "samples": n, **tally,
            "mention_rate": None if rate_m is None else round(rate_m, 4), "mention_ci95": wilson(tally["mentioned"], k),
            "citation_rate": None if rate_c is None else round(rate_c, 4), "citation_ci95": wilson(tally["cited"], k),
            "citation_variance": None if rate_c is None else round(rate_c * (1 - rate_c), 4),
            "class": cls, "other_sources": [{"host": h, "answers": c, "competitor": h in competitors or any(
                _on_site(h, d) for d in competitors)} for h, c in top],
            "model_versions": sorted(sample_versions), "mixed_model_versions": len(sample_versions) > 1,
            "injection_like_answers": injection_like})
    return {"engine": engine, "outcome": "OK", "prompts": results, "model_versions": sorted(versions)}


def run_callum(engines: dict, prompt_set: Optional[dict], domain: str, n: int, guard, osei) -> dict:
    if prompt_set is None:
        return envelope(AGENT_CALLUM, "citation_sources", "INSUFFICIENT_EVIDENCE", [],
                        methodology="No prompt set was given for this audit; no engine was asked anything.",
                        limitations=LIMITS, reason="NO_PROMPT_SET")
    methodology = METHODOLOGY.format(ps=prompt_set["name"], ver=prompt_set["version"], sha=prompt_set["sha256"][:16],
                                     n=n, domain=domain, m=MIN_USABLE)
    per_engine, findings, not_connected = {}, [], set()
    try:
        for eng in prompt_set["engines"]:
            r = probe_engine(eng, engines.get(eng), prompt_set, domain, n, guard, osei)
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
    connected = [e for e, r in per_engine.items() if r["outcome"] == "OK"]
    if not connected:
        return envelope(AGENT_CALLUM, "citation_sources", "NOT_CONNECTED", [], methodology=methodology,
                        limitations=LIMITS, not_connected=not_connected,
                        reason="no answer-engine port is connected; no engine was asked anything",
                        facts={"prompt_set": _ps_ref(prompt_set), "engines": {e: "NOT_CONNECTED" for e in per_engine}})
    outcome = "OK" if len(connected) == len(per_engine) else "PARTIAL"
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
