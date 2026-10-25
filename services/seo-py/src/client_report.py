"""
The client-facing SEO audit report (ADR 0017, section "Client report"): a completed audit rendered as Markdown or as
one self-contained HTML page (inline CSS, no script, no external asset; printable to PDF from a browser).

Deterministic: no model call, no clock, no randomness, no network. The same audit view, tenant and branding give the
same bytes. Everything shown comes from the recorded report; nothing is added, scored or estimated here.

Rules (the ADR 0017 report rules carried to the client document):
  - no single overall score, grade or percentage, and no claim of superiority, guaranteed rankings, traffic or
    citations; every finding keeps its own severity, evidence class, rule id and decision;
  - an area that was not observed (NOT_CONNECTED, BLOCKED, FAILED, KILLED, RESTRICTED, INSUFFICIENT_EVIDENCE, or a
    check this audit did not include) is stated plainly as NOT CONNECTED / NOT COVERED / NOT CHECKED and never shown
    as clean;
  - every value taken from a crawled page, a sitemap, robots.txt or an answer engine is untrusted data: escaped for
    the output format (HTML entities; Markdown specials, autolink-shaped tokens in code spans), bounded, and withheld
    when it looks like personal data (agents.prompt_guard). Crawled URLs appear only as path templates
    (agents.logs.template_path plus the Search Console slug rule): a page the audit request named is shown as written;
    every other path is a template and another host is ``{offsite}``. The only clickable links are the audited pages
    on the tenant's registered domain (HTML only, built from the request, never from crawled text);
  - retention: no answer body, no prompt or query text and no per-query or per-URL Search Console row is ever
    rendered (prompts are "Prompt 1", "Prompt 2"); the Search Console section is rendered from the retained view
    (window totals only once its breakdowns expired);
  - ESTIMATED / INFERRED values say so, with the assumption or the rule they rest on;
  - white label: the preparer's name and accent colour are a ``Branding`` value (config); no third-party promotion.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import unquote

from agents import logs as logs_mod
from agents import search_performance as sp
from agents import split_url
from agents.prompt_guard import personal_data

RENDERER_VERSION = "seo-client-report/1"
SUPPORTED_REPORT_VERSIONS = ("seo-audit-report/1",)
FORMATS = {"markdown": "text/markdown; charset=utf-8", "html": "text/html; charset=utf-8"}

TEXT_MAX = 160                 # characters of one untrusted text value
LIST_MAX = 5                   # items of one list value
LOCATIONS_MAX = 20             # affected pages listed per finding group
INSTANCES_MAX = 5              # instances whose details are shown per finding group
TEMPLATES_MAX = 25             # Search Console page templates shown
DASH = "\u2014"
WITHHELD = "[withheld: may contain personal data]"
SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")
_TOKEN = re.compile(r"[A-Za-z0-9_.:/+-]{1,80}")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_HOST = re.compile(r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
_DOMAIN = re.compile(r"[a-z0-9.-]{4,253}")
_REQ_PATH = re.compile(r"/[\x21-\x7e]{0,500}")
_ACCENT = re.compile(r"#[0-9a-fA-F]{6}")
_BRAND = re.compile(r"[A-Za-z0-9 .,&'()-]{1,60}")
_SHA = re.compile(r"[0-9a-f]{64}")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ID = re.compile(r"seo-[a-z]{2,5}-[0-9a-f]{40}")
# ISO dates and the service's dated rule versions ("2026-10-10.1") are not personal data; they are taken out before
# the personal-data check (whose digit-run rule would otherwise withhold every methodology naming a version)
_DATES = re.compile(r"(?<![0-9])(?:19|20)[0-9]{2}-[01][0-9]-[0-3][0-9](?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?Z?)?"
                    r"(?:\.[0-9]{1,3})?(?![0-9])")


class ReportInputError(ValueError):
    """The audit cannot be rendered for a client (not completed, another tenant's, malformed). The message never
    quotes the input."""


@dataclass(frozen=True)
class Branding:
    """Who prepared the report (white label). Plain configuration: a name and an accent colour, nothing else."""
    name: str = "Z Best Media"
    accent: str = "#1f4e79"

    def __post_init__(self):
        if not isinstance(self.name, str) or not _BRAND.fullmatch(self.name) or self.name != self.name.strip():
            raise ValueError("brand name: 1-60 characters of letters, digits, spaces and . , & ' ( ) -")
        if not isinstance(self.accent, str) or not _ACCENT.fullmatch(self.accent):
            raise ValueError("brand accent: a #RRGGBB colour")

    def digest(self) -> str:
        return hashlib.sha256(json.dumps({"name": self.name, "accent": self.accent.lower()}, sort_keys=True)
                              .encode()).hexdigest()


DEFAULT_BRANDING = Branding()

# ------------------------------------------------------------------------------------------------ vocabulary

SECTIONS = (
    ("crawlability", "Crawlability", ("selene", "crawlability"),
     "Whether search and AI crawlers can reach and index the audited pages: robots.txt, status codes, redirects, "
     "noindex signals and canonical links."),
    ("sitemaps", "Sitemaps and llms.txt", ("delia", "sitemaps_and_llms_txt"),
     "Whether the XML sitemaps can be read and list the right URLs, and whether an llms.txt file is published (an "
     "emerging, non-standard convention)."),
    ("content", "Content structure", ("roman", "content_structure"),
     "How each audited page is structured for machines: title, description, headings, language, structured data "
     "(JSON-LD) and how much text is in the raw HTML."),
    ("entity", "Entity consistency", ("entity_check", "on_site_entity_consistency"),
     "Whether the business details in the pages' Organization / LocalBusiness markup match your canonical business "
     "record (name, address, phone)."),
    ("search", "Search performance (Google Search Console)", ("osei", "search_performance"),
     "Clicks, impressions, click-through rate and position from your own Google Search Console property, when it is "
     "connected."),
    ("ai", "AI visibility (answer engines)", ("callum", "citation_sources"),
     "Whether answer engines (Perplexity when connected) cite or mention your site for a fixed set of market "
     "questions, sampled several times each."),
)

EVIDENCE = {
    "measured": ("OBSERVED", "measured by this audit's own requests"),
    "estimated": ("ESTIMATED", "an estimate"),
    "modeled": ("MODELED", "produced by a model"),
    "inferred": ("INFERRED", "drawn by a stated rule from what was observed, not measured directly"),
    "unknown": ("UNKNOWN", "how this was obtained is not known"),
}
DECISION_TEXT = {
    "ACT": "Fix", "TEST": "Test a change", "WATCH": "Monitor (may be intentional)", "DEFER": "Defer",
    "STOP": "Stop", "DO_NOT_BUILD": "Do not build", "DO_NOT_PUBLISH": "Do not publish", "DO_NOT_SPEND": "Do not spend",
    "INSUFFICIENT_EVIDENCE": "Not enough evidence to recommend a change",
}
STATUS = {
    "OK": ("CHECKED", None),
    "PARTIAL": ("PARTLY CHECKED", "Part of this area could not be checked; the findings cover only what was observed."),
    "NOT_CONNECTED": ("NOT CONNECTED", "The data source for this area is not connected for this audit."),
    "INSUFFICIENT_EVIDENCE": ("NOT COVERED", "Not enough could be observed to report on this area."),
    "BLOCKED": ("NOT CHECKED (blocked)", "This check could not run."),
    "FAILED": ("NOT CHECKED (failed)", "This check failed before it could observe anything reliable."),
    "KILLED": ("NOT CHECKED (stopped)", "This check was stopped by a safety switch."),
    "QUARANTINED": ("NOT CHECKED (input set aside)", "The input for this check was malformed and was set aside."),
    "RESTRICTED": ("NOT CHECKED (suspended)", "This check is suspended by the department manager."),
    None: ("NOT COVERED", "This audit did not include this check."),
}
NOT_CLEAN = ("Nothing is reported here because this area was not checked. This is not a clean result.")
REASON_TEXT = {
    "GSC_NOT_CONNECTED": "Google Search Console is not connected to this service.",
    "GSC_PROPERTY_UNBOUND": "No Search Console property is linked to your account here yet.",
    "GSC_PROPERTY_NOT_GRANTED": "The Search Console property has not granted this service read access.",
    "GSC_AUTH_REFUSED": "Google refused this service's credentials.",
    "GSC_BUDGET_EXHAUSTED": "The daily Search Console read allowance was used up.",
    "GSC_RATE_LIMITED": "Google asked this service to slow down; the read was not completed.",
    "GSC_UNAVAILABLE": "Google Search Console did not answer in time.",
    "GSC_PROTOCOL_ERROR": "Google's answer could not be read safely, so none of it was used.",
    "GSC_KILLED": "The Search Console read was stopped by a safety switch.",
    "NO_PROMPT_SET": "No set of market questions was chosen for this audit, so no answer engine was asked anything.",
    "ENGINE_BUDGET_EXHAUSTED": "The answer-engine budget did not allow (more) questions.",
    "ENGINE_AUTH_REFUSED": "The answer engine refused this service's key.",
    "ENGINE_RATE_LIMITED": "The answer engine asked this service to slow down.",
    "ENGINE_UNAVAILABLE": "The answer engine did not answer.",
    "ENGINE_PROTOCOL_ERROR": "The answer engine's replies could not be read safely, so none of them were used.",
    "ENGINE_KILLED": "Questions to the answer engine were stopped by a safety switch.",
    "ENGINE_NOT_CONNECTED": "The answer engine is not connected.",
    "AGENT_RESTRICTED": "This check is suspended by the department manager.",
    "KILLED_GLOBAL": "A service-wide safety switch was engaged.",
    "KILLED_TENANT": "A safety switch for your account was engaged.",
    "KILLED_CAPABILITY": "A safety switch for this kind of check was engaged.",
    "KILLED_PROVIDER": "A safety switch for this data source was engaged.",
    "KILLED_WRITE": "A safety switch on recording was engaged.",
}
NOT_CONNECTED_TEXT = {
    "render": "JavaScript rendering: pages were read as raw HTML; content added by JavaScript was not seen.",
    "prompt_volume": "Prompt volume: how often real people ask the market questions is not known.",
    "first_party:search_console": "Google Search Console (your own search data).",
    "first_party:bing_webmaster": "Bing Webmaster Tools.",
    "first_party:analytics": "Web analytics (traffic, conversions).",
    "first_party:crm": "CRM (leads, revenue).",
    "first_party:server_logs": "Server logs (only when you upload them).",
    "answer_engine:openai": "OpenAI answer engine.",
    "answer_engine:anthropic": "Anthropic answer engine.",
    "answer_engine:google": "Google answer engine.",
    "answer_engine:perplexity": "Perplexity answer engine.",
    "zero_day": "Zero-Day (not part of this audit).",
    "orca_publish": "ORCA Publish (not part of this audit).",
    "clientfix": "Fix execution: this audit recommends fixes; it does not make changes to your site.",
    "fetch": "Web fetching: no page could be read.",
}
INTERNAL_CODES = frozenset({"ENGINE_COST_ABOVE_RESERVE"})   # operational, about our spend: in the audit record only

# Per rule: (title, plain-English fix, detail keys shown with their kind). A key not listed is never rendered.
# Kinds: num, bool, nums (list of numbers), text (untrusted text), texts, url (crawled URL -> template),
# urls, path (audited path as written, else template), paths, token (code-shaped), issues (structured-data issues),
# rate (0..1 with a 95% interval in "<key>_ci95"), sources (answer-engine cited hosts), prompt (index -> "Prompt n").
_R = {
    # Selene: crawlability
    "ROBOTS_UNREACHABLE": ("robots.txt could not be read", "Make /robots.txt answer 200 (or 404 if you want no "
                           "rules). Crawlers that follow RFC 9309 treat an unreachable robots.txt as 'everything "
                           "disallowed'.", (("status", "num"), ("fetch_state", "token"))),
    "ROBOTS_ABSENT": ("No robots.txt", "Optional: publish a robots.txt with a Sitemap line. Without one every crawler "
                      "may crawl everything, which is often fine.", (("status", "num"),)),
    "ROBOTS_BLOCKS_SEARCH_CRAWLER": ("robots.txt blocks a search crawler", "Remove or narrow the Disallow rule for "
                                     "this crawler unless blocking it is intended.",
                                     (("token", "token"), ("blocked_paths", "paths"), ("rules", "texts"))),
    "ROBOTS_BLOCKS_AI_OR_DATASET_CRAWLER": ("robots.txt blocks an AI or dataset crawler", "No fix needed if this is "
                                            "your policy. If you want this operator to read your site, allow its "
                                            "user-agent token.", (("token", "token"), ("purpose", "token"),
                                                                  ("blocked_paths", "paths"), ("rules", "texts"))),
    "ROBOTS_LINES_IGNORED": ("robots.txt has lines crawlers ignore", "Correct or remove the malformed lines so the "
                             "rules mean what you intend.", (("lines_ignored", "num"),)),
    "PAGE_BLOCKED_FOR_OUR_CRAWLER": ("Page not read: robots.txt disallows our crawler", "If you want this page "
                                     "audited, allow the ZBM-SEO-Audit user-agent for it.", ()),
    "PAGE_UNREACHABLE": ("Page could not be reached", "Check that the page is online and reachable from the public "
                         "internet, then re-run the audit.", (("fetch_state", "token"),)),
    "PAGE_HTTP_ERROR": ("Page answers with an error status", "Make the page answer 200, or redirect it permanently "
                        "(301) to its replacement.", (("status", "num"),)),
    "REDIRECT_CHAIN": ("Redirect chain", "Point links and redirects straight at the final URL (one hop), using a "
                       "permanent (301/308) redirect.", (("statuses", "nums"), ("hops", "num"),
                                                          ("temporary", "bool"), ("final_url", "url"))),
    "REDIRECTED": ("Page redirects", "Link to the final URL directly; use a permanent redirect if the move is "
                   "permanent.", (("statuses", "nums"), ("temporary", "bool"), ("final_url", "url"))),
    "PAGE_NOT_HTML": ("Page is not HTML", "Check that this URL is meant to be a web page; serve it as text/html if "
                      "so.", (("content_type", "token"),)),
    "NOINDEX": ("Page asks not to be indexed (noindex)", "Remove the noindex (meta robots or X-Robots-Tag header) if "
                "this page should appear in search; keep it if hiding it is intended.", ()),
    "CANONICAL_INVALID": ("Canonical link is not a valid URL", "Give the page one absolute https canonical URL.",
                          (("count", "num"),)),
    "CANONICAL_MULTIPLE": ("More than one canonical link", "Keep exactly one rel=canonical on the page.",
                           (("count", "num"), ("invalid", "num"), ("canonicals", "urls"))),
    "CANONICAL_OTHER_HOST": ("Canonical points to another host", "Confirm the other host is really the preferred "
                             "copy; otherwise point the canonical at this page.", (("canonical", "url"),)),
    "NOINDEX_CANONICAL_CONFLICT": ("noindex and a canonical elsewhere", "Use one signal: either noindex, or a "
                                   "canonical to the preferred page, not both.", (("canonical", "url"),)),
    "CANONICAL_TARGET_NOT_200": ("Canonical target does not answer 200", "Point the canonical at a URL that answers "
                                 "200 without redirecting.", (("canonical", "url"), ("status", "num"),
                                                             ("redirected", "bool"))),
    "CANONICAL_MISSING": ("No canonical link", "Add a self-referencing rel=canonical to the page.", ()),
    "HTML_PARSE_ERROR": ("HTML could not be fully parsed", "Validate the page's HTML and fix structural errors.", ()),
    "BOT_DIFFERENTIAL": ("Crawlers are served different content than browsers", "Serve crawlers and visitors the "
                         "same content (check CDN, bot-protection and cloaking rules).", ()),
    # Delia: sitemaps and llms.txt
    "SITEMAP_MISSING": ("No sitemap found", "Publish an XML sitemap and name it in robots.txt with a Sitemap line.",
                        (("status", "num"),)),
    "SITEMAP_UNREADABLE": ("Sitemap could not be read", "Make the sitemap URL answer 200 with XML.",
                           (("status", "num"), ("fetch_state", "token"))),
    "SITEMAP_DECOMPRESS_FAILED": ("Compressed sitemap is broken", "Re-generate the .gz sitemap or serve it "
                                  "uncompressed.", ()),
    "SITEMAP_QUARANTINED": ("Sitemap was set aside as unsafe or malformed", "Serve a plain sitemap without a "
                            "DOCTYPE or entities, valid XML, within size limits.", (("why", "token"),)),
    "SITEMAP_NAMESPACE": ("Sitemap uses an unexpected XML namespace", "Use the sitemaps.org 0.9 namespace.", ()),
    "SITEMAP_ROOT_UNKNOWN": ("Sitemap root element not recognised", "Use <urlset> or <sitemapindex> as the root.",
                             ()),
    "SITEMAP_TOO_MANY_URLS": ("Sitemap file lists too many URLs", "Split it into files of at most 50,000 URLs "
                              "under a sitemap index.", (("urls", "num"), ("limit", "num"))),
    "SITEMAP_LOC_NOT_ABSOLUTE": ("Sitemap URLs are not absolute", "List full https URLs in <loc>.",
                                 (("count", "num"),)),
    "SITEMAP_LOC_OTHER_HOST": ("Sitemap lists URLs on another host", "List only URLs of this site in its sitemap.",
                               (("count", "num"), ("examples", "urls"))),
    "SITEMAP_LISTS_ROBOTS_BLOCKED_URL": ("Sitemap lists URLs robots.txt blocks", "Remove blocked URLs from the "
                                         "sitemap, or allow them in robots.txt.",
                                         (("count", "num"), ("examples", "urls"))),
    "SITEMAP_LASTMOD_INVALID": ("Invalid lastmod dates in the sitemap", "Use W3C datetime values (e.g. "
                                "2026-10-01) for <lastmod>.", (("count", "num"),)),
    "SITEMAP_LASTMOD_FUTURE": ("lastmod dates in the future", "Set <lastmod> to the page's real last change.",
                               (("count", "num"),)),
    "SITEMAP_LASTMOD_ALL_IDENTICAL": ("Every sitemap URL has the same lastmod", "Set <lastmod> per URL from the "
                                      "page's real last change, or leave it out.", (("count", "num"),)),
    "LLMS_TXT_UNREADABLE": ("llms.txt could not be read", "Nothing required: llms.txt is optional and "
                            "non-standard.", (("fetch_state", "token"),)),
    "LLMS_TXT_ABSENT": ("No llms.txt", "Nothing required: llms.txt is an emerging, non-standard convention and its "
                        "absence is not a defect.", (("status", "num"),)),
    "LLMS_TXT_NOT_UTF8": ("llms.txt is not UTF-8", "Save llms.txt as UTF-8 text.", ()),
    "LLMS_TXT_FORMAT": ("llms.txt does not follow the proposed format", "Optional: follow the llmstxt.org proposal "
                        "(a # title, a > summary, sections of absolute links).", (("issues", "issues"),)),
    "LLMS_TXT_CONTENT_TYPE": ("llms.txt served with an unexpected content type", "Serve llms.txt as text/plain or "
                              "text/markdown.", (("content_type", "token"),)),
    # Roman: content structure
    "TITLE_MISSING": ("Page has no title", "Add a unique, descriptive <title>.", ()),
    "TITLE_LENGTH": ("Title length outside the usual range", "Consider a title of roughly 15-65 characters that "
                     "describes the page (a heuristic, not a rule).", (("length", "num"),
                                                                        ("heuristic_range", "nums"))),
    "META_DESCRIPTION_MISSING": ("No meta description", "Add a short meta description summarising the page.", ()),
    "H1_COUNT": ("Not exactly one H1 heading", "Give the page one H1 that states its topic.", (("h1_count", "num"),)),
    "HEADING_LEVEL_SKIP": ("Heading levels skip", "Nest headings in order (H1, then H2, then H3).",
                           (("skips", "texts"),)),
    "LANG_MISSING": ("No language declared", "Add lang to the <html> element (e.g. lang=\"en\").", ()),
    "STRUCTURED_DATA_INVALID_JSON": ("Structured data is not valid JSON", "Fix the JSON-LD block so it parses.",
                                     (("blocks", "num"),)),
    "STRUCTURED_DATA_ERRORS": ("Structured data has errors", "Add the required properties listed below to the "
                               "JSON-LD.", (("issues", "issues"),)),
    "STRUCTURED_DATA_RECOMMENDED_MISSING": ("Structured data lacks recommended properties", "Consider adding the "
                                            "properties listed below.", (("issues", "issues"),)),
    "STRUCTURED_DATA_ABSENT": ("No structured data", "Consider adding JSON-LD that describes the page and your "
                               "organisation.", ()),
    "ENTITY_SAMEAS_MISSING": ("Organisation markup has no sameAs links", "Add sameAs links to your official "
                              "profiles in the Organization markup.", ()),
    "LITTLE_TEXT_IN_RAW_HTML": ("Little text in the raw HTML", "Make sure the main content is in the HTML the "
                                "server sends, not only added by JavaScript.", (("characters", "num"),
                                                                                ("heuristic_min", "num"))),
    "ENTITY_MARKUP_ABSENT": ("No Organization / LocalBusiness markup", "Add Organization or LocalBusiness JSON-LD "
                             "with your business name, address and phone exactly as in your business record.", ()),
    # entity check
    "ENTITY_FIELD_MISSING_IN_MARKUP": ("Business detail missing from the markup", "Add this field to the "
                                       "Organization / LocalBusiness markup exactly as in your business record.",
                                       (("field", "token"),)),
    "ENTITY_FIELD_MISMATCH": ("Business detail differs from your record", "Change the markup to match your "
                              "business record exactly (the record is the authority). The values are in the audit "
                              "record.", (("field", "token"),)),
    "PHONE_NOT_IN_VISIBLE_TEXT": ("Business phone number not in the visible text", "Show the phone number from your "
                                  "business record as text on the home page.", ()),
    # Callum: AI visibility
    "CITATION_STRENGTH": ("Cited in at least half of the answers", "Keep the cited pages current and accurate.",
                          (("engine", "token"), ("prompt_index", "prompt"), ("usable_answers", "num"),
                           ("citation_rate", "rate"), ("mention_rate", "rate"))),
    "CITATION_MENTIONED_NOT_CITED": ("Mentioned but not cited", "Publish a clear, crawlable page that answers this "
                                     "question, so engines can link to it.",
                                     (("engine", "token"), ("prompt_index", "prompt"), ("usable_answers", "num"),
                                      ("mention_rate", "rate"), ("other_sources", "sources"))),
    "CITATION_OPPORTUNITY": ("Not mentioned or cited; other sources were", "Test a page that answers this question "
                             "directly; see which kinds of sources were cited instead.",
                             (("engine", "token"), ("prompt_index", "prompt"), ("usable_answers", "num"),
                              ("other_sources", "sources"))),
    "CITATION_ABSENT": ("Not mentioned or cited (no sources cited at all)", "Monitor: the answers cited no sources "
                        "for this question.", (("engine", "token"), ("prompt_index", "prompt"),
                                               ("usable_answers", "num"))),
    "CITATION_PARTIAL_CITATION": ("Cited in some answers", "Monitor and keep the cited pages current.",
                                  (("engine", "token"), ("prompt_index", "prompt"), ("usable_answers", "num"),
                                   ("citation_rate", "rate"), ("mention_rate", "rate"))),
    "CITATION_INSUFFICIENT_EVIDENCE": ("Too few usable answers to judge", "Nothing to change yet; re-sample later.",
                                       (("engine", "token"), ("prompt_index", "prompt"), ("usable_answers", "num"),
                                        ("refusals", "num"), ("errors", "num"))),
    "MODEL_VERSION_MIXED_IN_SAMPLE": ("The engine changed model during sampling", "Treat this question's rates with "
                                      "extra caution.", (("engine", "token"), ("prompt_index", "prompt"))),
    "ENGINE_RUN_STOPPED": ("Answer-engine sampling stopped early", "Nothing to change on your site; the remaining "
                           "questions were not asked.", (("engine", "token"), ("reason", "token"))),
    "ENGINE_ANSWERS_WITHOUT_SOURCES": ("Answers came without any sources", "Nothing to change on your site; the "
                                       "engine returned no citations to compare.", (("engine", "token"),
                                                                                     ("answers", "num"))),
}
_INFERRED_RULE = {
    "SITEMAP_LASTMOD_ALL_IDENTICAL": "every URL carries the same lastmod, so it likely does not reflect real changes",
}


# ------------------------------------------------------------------------------------------------ safe values

def _clean(s: Any) -> str:
    t = "".join(ch if ch.isprintable() else " " for ch in str(s))
    return " ".join(t.split())


def safe_text(s: Any, limit: int = TEXT_MAX) -> str:
    """Untrusted text as a bounded plain string; withheld when it looks like personal data."""
    t = _clean(str(s)[: limit * 4])
    if len(t) > limit:
        t = t[: limit - 1] + "…"
    if personal_data(_DATES.sub(" ", t)) is not None:     # judged on exactly what would be shown
        return WITHHELD
    return t


def safe_token(s: Any) -> str:
    """A code-shaped value (a state, a media type, a rule token); judged for personal data like any text (a crawled
    ``Content-Type`` of ``tel:+15550100199`` is code-shaped too)."""
    t = _clean(s)
    if not _TOKEN.fullmatch(t):
        return safe_text(t, 60)
    return WITHHELD if personal_data(_DATES.sub(" ", t)) is not None else t


def safe_host(h: Any) -> Optional[str]:
    """A plain LDH host name that does not look like personal data, else None."""
    h = str(h or "").lower().rstrip(".")
    return h if _HOST.fullmatch(h) and personal_data(h) is None else None


def _templated(p: str) -> str:
    t = logs_mod.template_path(p) if isinstance(p, str) else None
    if t is None:
        return "{unreadable}"
    segs = [sp.slug_segment(x) for x in t.split("/")[1:]]
    return ("/" + "/".join(segs)).rstrip("/") or "/"


def shown_path(p: str) -> tuple[str, bool]:
    """(what is shown, whether it may be a link) for a path the audit request named: the query string and fragment
    are never shown (AEGIS L-4), and a path that still looks like personal data is shown as its template, unlinked."""
    base = re.split(r"[?#]", p, maxsplit=1)[0] or "/"
    # judged decoded too (AEGIS N-4: /u/555%2D010%2D0199)
    if any(personal_data(_DATES.sub(" ", x)) is not None for x in (base, unquote(base))):
        return _templated(base), False
    return base, True


class _Ctx:
    def __init__(self, scheme: str, domain: str, paths: list):
        self.scheme, self.domain = scheme, domain
        self.paths = list(paths)
        self.hosts = {domain.removeprefix("www."), "www." + domain.removeprefix("www.")}
        self.pages = {f"{scheme}://{domain}{p}": p for p in self.paths}
        self.shown = {p: shown_path(p) for p in self.paths}

    def page_url(self, path: str) -> str:
        return f"{self.scheme}://{self.domain}{path}"

    def location(self, url: Any) -> dict:
        """{"kind": "page", "path"} for a page the request named; else {"kind": "template", "text"}."""
        if isinstance(url, str) and url in self.pages:
            return {"kind": "page", "path": self.shown[self.pages[url]][0]}
        return {"kind": "template", "text": self.template(url)}

    def template(self, url: Any) -> str:
        if not isinstance(url, str) or not url:
            return "{unreadable}"
        p = split_url(url)
        if p is None or p.scheme not in ("http", "https"):
            return "{unreadable}"
        t = sp.page_template(url, self.hosts)
        if t == "{offsite}":
            host = safe_host(p.hostname)
            return "{offsite}" + (f" ({host})" if host else "")
        return t

    def path(self, p: Any) -> str:
        if isinstance(p, str) and p in self.paths:
            return self.shown[p][0]
        return _templated(p)


def _num(v: Any) -> Optional[str]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and v != v:
        return None
    return f"{v:,}" if isinstance(v, int) else f"{v:,.4g}"


def _pct(v: Any) -> Optional[str]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
        return None
    return f"{v * 100:.1f}%"


def _detail_items(code: str, detail: dict, ctx: _Ctx) -> list:
    """[(label, value-model)] for the keys this rule shows. value-model: {"t": "text"|"code"|"list", ...}."""
    spec = _R.get(code, (None, None, ()))[2]
    out = []
    if not isinstance(detail, dict):
        return out
    for key, kind in spec:
        if key not in detail:
            continue
        v = detail[key]
        label = key.replace("_", " ")
        model = None
        if kind == "num":
            n = _num(v)
            model = None if n is None else {"t": "text", "v": n}
        elif kind == "bool":
            model = {"t": "text", "v": "yes" if v is True else "no"} if isinstance(v, bool) else None
        elif kind == "nums" and isinstance(v, list):
            nums = [n for n in (_num(x) for x in v[:LIST_MAX]) if n is not None]
            model = {"t": "text", "v": ", ".join(nums) + (" …" if len(v) > LIST_MAX else "")} if nums else None
        elif kind == "token":
            model = None if v is None else {"t": "code", "v": safe_token(v)}
        elif kind == "url":
            model = None if v is None else {"t": "code", "v": ctx.template(v)}
        elif kind == "urls" and isinstance(v, list):
            model = {"t": "list", "v": sorted({ctx.template(x) for x in v[:LIST_MAX]}), "more": len(v) > LIST_MAX}
        elif kind == "paths" and isinstance(v, list):
            model = {"t": "list", "v": sorted({ctx.path(x) for x in v[:LIST_MAX]}), "more": len(v) > LIST_MAX}
        elif kind == "texts" and isinstance(v, list):
            model = {"t": "list", "v": [safe_text(x, 80) for x in v[:LIST_MAX]], "more": len(v) > LIST_MAX}
        elif kind == "issues" and isinstance(v, list):
            items = []
            for i in v[:LIST_MAX]:
                if isinstance(i, dict):
                    parts = [safe_token(i[k]) for k in ("code", "type", "property") if isinstance(i.get(k), str)]
                    items.append(" ".join(parts) or "issue")
                else:
                    items.append(safe_token(i))
            model = {"t": "list", "v": items, "more": len(v) > LIST_MAX}
        elif kind == "rate":
            p = _pct(v)
            if p is not None:
                ci = detail.get(key.replace("_rate", "_ci95"))
                if isinstance(ci, list) and len(ci) == 2 and all(_pct(x) for x in ci):
                    p += f" (95% interval {_pct(ci[0])} to {_pct(ci[1])})"
                model = {"t": "text", "v": p}
        elif kind == "prompt":
            model = {"t": "text", "v": f"Question {v + 1}"} if isinstance(v, int) and not isinstance(v, bool) \
                and 0 <= v < 10_000 else None
            label = "question"
        elif kind == "sources" and isinstance(v, list):
            items = []
            for s in v[:LIST_MAX]:
                if not isinstance(s, dict):
                    continue
                host = safe_host(s.get("host")) or "{host}"
                n = _num(s.get("answers"))
                items.append(host + (f" ({n} answers)" if n else "") + (" (a competitor you named)"
                                                                         if s.get("competitor") is True else ""))
            model = {"t": "list", "v": items, "more": len(v) > LIST_MAX} if items else None
            label = "cited instead"
        if model is not None and (model["t"] != "list" or model["v"]):
            out.append((label, model))
    return out


# ------------------------------------------------------------------------------------------------ the model

def _ts(s: Any) -> str:
    if not isinstance(s, str):
        raise ReportInputError("a timestamp is not a string")
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ReportInputError("a timestamp is not ISO 8601") from None
    if d.tzinfo is None:
        raise ReportInputError("a timestamp has no UTC offset")
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _ts_plus(s: str, days: int) -> str:
    d = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc) + timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def _status(env: Optional[dict]) -> dict:
    outcome = None if env is None else env.get("outcome")
    label, why = STATUS.get(outcome, STATUS["FAILED"])
    reason = None if env is None else env.get("reason")
    rtext = None
    if isinstance(reason, str) and _CODE.fullmatch(reason):
        rtext = REASON_TEXT.get(reason)
    checked = outcome in ("OK", "PARTIAL")
    return {"outcome": outcome or "NOT_COVERED", "label": label, "why": rtext or why, "checked": checked,
            "reason_code": reason if isinstance(reason, str) and _CODE.fullmatch(reason) else None}


def _groups(findings: list, ctx: _Ctx) -> list:
    groups: dict = {}
    for f in findings:
        if not isinstance(f, dict) or not isinstance(f.get("code"), str) or not _CODE.fullmatch(f["code"]):
            continue
        if f["code"] in INTERNAL_CODES:
            continue
        sev, dc, dec = f.get("severity"), f.get("data_class"), f.get("decision")
        if sev not in SEVERITY_ORDER or dc not in EVIDENCE or dec not in DECISION_TEXT:
            # refused, never downgraded (AEGIS L-10): the recorded report holds a value outside the closed vocabularies
            raise ReportInputError("a finding outside the closed vocabularies")
        cap = f.get("capability") if isinstance(f.get("capability"), str) and _TOKEN.fullmatch(f["capability"]) \
            else None
        key = (SEVERITY_ORDER.index(sev), f["code"], dc, dec)
        g = groups.setdefault(key, {"code": f["code"], "severity": sev, "data_class": dc, "decision": dec,
                                    "capability": cap, "locations": {}, "instances": []})
        loc = ctx.location(f.get("url")) if f.get("url") is not None else None
        if loc is not None:
            g["locations"][json.dumps(loc, sort_keys=True)] = loc
        items = _detail_items(f["code"], f.get("detail") or {}, ctx)
        if items:
            g["instances"].append({"location": loc, "items": items})
    out = []
    for key in sorted(groups):
        g = groups[key]
        title, fix, _ = _R.get(g["code"], (None, None, ()))
        locs = [g["locations"][k] for k in sorted(g["locations"])]
        inst = sorted(g["instances"], key=lambda i: json.dumps(i, sort_keys=True))
        label, how = EVIDENCE[g["data_class"]]
        if g["data_class"] == "inferred":
            how += ": " + _INFERRED_RULE.get(g["code"], "the rule is recorded with the finding")
        elif g["data_class"] in ("estimated", "modeled"):
            how += "; the assumption is not part of this finding's record, so treat it as unverified"
        out.append({"code": g["code"], "title": title or g["code"].replace("_", " ").capitalize(),
                    "fix": fix or "No standard fix is written for this rule yet; ask us about it (the details are in "
                           "the audit record).",
                    "severity": g["severity"], "evidence": label, "evidence_how": how,
                    "decision": DECISION_TEXT[g["decision"]], "capability": g["capability"],
                    "locations": locs[:LOCATIONS_MAX], "locations_more": max(0, len(locs) - LOCATIONS_MAX),
                    "instances": inst[:INSTANCES_MAX], "instances_more": max(0, len(inst) - INSTANCES_MAX)})
    return out


def _note(g: dict) -> str:
    """A finding group of an area that was not checked, as one plain line (values already made safe)."""
    bits = []
    for inst in g["instances"][:INSTANCES_MAX]:
        bits.append("; ".join(f"{lbl}: " + (", ".join(v["v"]) if v["t"] == "list" else str(v["v"]))
                              for lbl, v in inst["items"]))
    return (f"Run note: {g['title']} (rule {g['code']})" + (f": {' | '.join(bits)}" if bits else "")
            + ". This area was not checked; the note explains why, it is not a finding.")[:600]


def _search_block(env: Optional[dict], block: Optional[dict]) -> Optional[dict]:
    """The Search Console figures (window, totals, query classes, page templates) when the read succeeded."""
    if env is None or env.get("outcome") != "OK" or not isinstance(block, dict):
        return None
    s = block.get("section")
    if not isinstance(s, dict) or not isinstance(s.get("totals"), dict) or not isinstance(s.get("window"), dict):
        return None
    w, t = s["window"], s["totals"]
    out = {"property": safe_text(s.get("property"), 120), "window_start": safe_token(w.get("start")),
           "window_end": safe_token(w.get("end")), "days": _num(w.get("days")),
           "clicks": _num(t.get("clicks")) or "0", "impressions": _num(t.get("impressions")) or "0",
           "ctr": _pct(t.get("ctr")), "position": _num(t.get("position")), "expired": bool(s.get("expired")),
           "classes": [], "unattributed": None, "templates": [], "templates_more": 0}
    qc = s.get("by_query_class")
    if isinstance(qc, dict) and isinstance(qc.get("classes"), dict):
        names = {"pii_suspect": "set aside (looked like personal data; dropped)", "brand": "brand", "question":
                 "questions", "local": "local intent", "commercial": "commercial intent", "other": "other"}
        for k in sp.QUERY_CLASSES:
            v = qc["classes"].get(k)
            if isinstance(v, dict):
                out["classes"].append({"name": names[k], "clicks": _num(v.get("clicks")) or "0",
                                       "impressions": _num(v.get("impressions")) or "0", "ctr": _pct(v.get("ctr"))})
        u = qc.get("unattributed")
        if isinstance(u, dict):
            out["unattributed"] = {"clicks": _num(u.get("clicks")) or "0",
                                   "impressions": _num(u.get("impressions")) or "0"}
    pt = s.get("by_page_template")
    if isinstance(pt, dict) and isinstance(pt.get("templates"), list):
        rows = [r for r in pt["templates"] if isinstance(r, dict) and isinstance(r.get("template"), str)]
        for r in rows[:TEMPLATES_MAX]:
            tpl = r["template"]
            if tpl not in ("{other}", "{offsite}", "{unreadable}"):
                tpl = sp_path(tpl)
            out["templates"].append({"template": tpl, "clicks": _num(r.get("clicks")) or "0",
                                     "impressions": _num(r.get("impressions")) or "0", "ctr": _pct(r.get("ctr"))})
        out["templates_more"] = max(0, len(rows) - TEMPLATES_MAX)
    return out


def sp_path(tpl: str) -> str:
    """A recorded Search Console template, re-checked: templated again and every segment re-judged."""
    t = logs_mod.template_path(tpl)
    if t is None:
        return "{unreadable}"
    segs = [sp.slug_segment(x) for x in t.split("/")[1:]]
    return ("/" + "/".join(segs)).rstrip("/") or "/"


def _ai_block(env: Optional[dict]) -> Optional[dict]:
    if env is None:
        return None
    facts = env.get("facts") if isinstance(env.get("facts"), dict) else {}
    engines = facts.get("engines") if isinstance(facts.get("engines"), dict) else {}
    rows = []
    n = facts.get("samples_per_prompt")
    samples = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else None
    for name in sorted(engines):
        if not _TOKEN.fullmatch(str(name)):
            continue
        e = engines[name]
        if isinstance(e, str):
            rows.append({"engine": name, "status": STATUS.get(e, STATUS["FAILED"])[0], "asked": None,
                         "not_asked": None, "sampling": "Nothing was asked."})
            continue
        if not isinstance(e, dict):
            continue
        prompts = e.get("prompts") if isinstance(e.get("prompts"), list) else []
        asked = sum(1 for p in prompts if isinstance(p, dict) and p.get("prompt_refused") is None)
        na = e.get("prompts_not_asked")
        not_asked = na if isinstance(na, int) and not isinstance(na, bool) and na >= 0 else None
        rows.append({"engine": name, "status": STATUS.get(e.get("outcome"), STATUS["FAILED"])[0],
                     "asked": _num(asked), "not_asked": _num(not_asked),
                     "sampling": _sampling(e.get("outcome"), asked, len(prompts) - asked, not_asked, samples)})
    ps = facts.get("prompt_set") if isinstance(facts.get("prompt_set"), dict) else None
    return {"engines": rows, "samples": _num(samples),
            "prompt_set_version": _num(ps.get("version")) if ps else None,
            "prompt_set_sha": ps.get("sha256")[:16] if ps and isinstance(ps.get("sha256"), str)
            and _SHA.fullmatch(ps["sha256"]) else None}


def _sampling(outcome: Any, asked: int, refused: int, not_asked: Optional[int], samples: Optional[int]) -> str:
    """What was asked of one engine, stated only as far as the record shows it (AEGIS M-1): a per-question count is
    claimed only for an engine that finished every question (outcome OK, nothing left unasked)."""
    skipped = f" {refused} question(s) were not sent because they looked like personal data." if refused else ""
    if asked == 0:
        return "Nothing was asked." + skipped
    if outcome == "OK" and not_asked == 0 and samples:
        return f"Each of the {asked} question(s) asked was asked {samples} times.{skipped}"
    rest = (f" {not_asked} question(s) were not asked." if not_asked else
            " How many questions were left unasked is not recorded." if not_asked is None else "")
    up_to = f", each up to {samples} times" if samples else ""
    return f"Sampling did not finish: {asked} question(s) were asked{up_to}.{rest}{skipped}"


def _freshness(key: str, report: dict, env: Optional[dict], tiers: dict) -> str:
    if env is None or env.get("outcome") not in ("OK", "PARTIAL"):
        return "not observed in this audit"
    fin = report["finished_at"]
    if key == "search":
        sb = (report.get("search_performance") or {}).get("section") or {}
        w = sb.get("window") if isinstance(sb.get("window"), dict) else {}
        start, end = w.get("start"), w.get("end")
        if not (isinstance(start, str) and _DAY.fullmatch(start) and isinstance(end, str) and _DAY.fullmatch(end)):
            return "no Search Console data in this report"
        return f"Google data for {start} to {end} (Pacific Time, final data; Google's figures lag about 3 days)"
    tier = {"crawlability": ("robots", "page"), "sitemaps": ("sitemap", "llms_txt"), "content": ("page",),
            "entity": ("page",), "ai": ("ai_probe",)}[key]
    days = min(int(tiers.get(t, 7)) for t in tier)
    return f"observed during the run; refresh recommended by {_ts_plus(fin, days)}"


def build(audit: dict, tenant_id: str, branding: Branding = DEFAULT_BRANDING, preview: bool = False) -> dict:
    """The neutral report model from one audit view (``SeoService.audit_view``). Refuses anything that is not a
    completed audit of ``tenant_id``."""
    if not isinstance(audit, dict) or audit.get("tenant_id") != tenant_id:
        raise ReportInputError("not an audit of this tenant")
    if audit.get("status") != "completed":
        raise ReportInputError("the audit is not completed")
    report = audit.get("report")
    if not isinstance(report, dict) or report.get("report_version") not in SUPPORTED_REPORT_VERSIONS:
        raise ReportInputError("no report of a supported version")
    if report.get("tenant_id") != tenant_id or report.get("audit_id") != audit.get("audit_id"):
        raise ReportInputError("the report belongs to another audit or tenant")
    aid = report["audit_id"]
    if not isinstance(aid, str) or not _ID.fullmatch(aid):
        raise ReportInputError("audit id malformed")
    domain, scheme, paths = report.get("domain"), report.get("scheme"), report.get("paths")
    if not isinstance(domain, str) or not _DOMAIN.fullmatch(domain) or scheme not in ("http", "https") \
            or not isinstance(paths, list) or not paths \
            or not all(isinstance(p, str) and _REQ_PATH.fullmatch(p) for p in paths):
        raise ReportInputError("target malformed")
    rsha = audit.get("report_sha256")
    if not isinstance(rsha, str) or not _SHA.fullmatch(rsha):
        raise ReportInputError("report hash missing")
    started, finished = _ts(report.get("started_at")), _ts(report.get("finished_at"))
    ctx = _Ctx(scheme, domain, paths)
    envs = {}
    for e in report.get("agents") or []:
        if isinstance(e, dict) and isinstance(e.get("agent"), str) and isinstance(e.get("task"), str):
            envs.setdefault((e["agent"], e["task"]), e)
    hygiene = report.get("data_hygiene") if isinstance(report.get("data_hygiene"), dict) else {}
    tiers = hygiene.get("refresh_tiers_days") if isinstance(hygiene.get("refresh_tiers_days"), dict) else {}
    tiers = {k: v for k, v in tiers.items() if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 10_000}
    sections = []
    for key, title, at, what in SECTIONS:
        env = envs.get(at)
        st = _status(env)
        sec = {"key": key, "title": title, "what": what, "status": st,
               "freshness": _freshness(key, report, env, tiers),
               "groups": _groups(env.get("findings") or [], ctx) if env and st["outcome"] != "NOT_COVERED" else [],
               "dropped": _num(env.get("findings_dropped")) if env and env.get("findings_dropped") else None,
               "search": _search_block(env, report.get("search_performance")) if key == "search" else None,
               "ai": _ai_block(env) if key == "ai" else None}
        if key == "ai":
            naomi = envs.get(("naomi", "prompt_volume"))
            sec["volume"] = _status(naomi)
        # AEGIS L-6: an area that was not checked lists no findings; what its run recorded (an engine run stopped,
        # a page blocked) is a note under its methodology and limits
        sec["notes"] = [] if st["checked"] else [_note(g) for g in sec["groups"]]
        if not st["checked"]:
            sec["groups"] = []
        sections.append(sec)
    nc = sorted({x for x in (report.get("not_connected") or []) if isinstance(x, str)})
    not_connected = [NOT_CONNECTED_TEXT[x] for x in nc if x in NOT_CONNECTED_TEXT]
    unknown_nc = [x for x in nc if x not in NOT_CONNECTED_TEXT and _TOKEN.fullmatch(x)]
    methods = []
    for key, title, at, _what in SECTIONS:
        env = envs.get(at)
        if env is None:
            continue
        lims = env.get("limitations") if isinstance(env.get("limitations"), list) else []
        sec = next(x for x in sections if x["key"] == key)
        notes = list(sec["notes"])
        if key == "ai" and sec["ai"] and any(not e["sampling"].startswith("Each of") for e in sec["ai"]["engines"]):
            notes.append("The method above is the plan. Not every engine was asked every question that many times; "
                         "the per-engine lines in the AI visibility section state what was actually asked.")
        methods.append({"title": title, "method": safe_text(env.get("methodology") or "", 1500),
                        "limits": [safe_text(x, 600) for x in lims[:20] if isinstance(x, str)] + notes})
    quarantined = hygiene.get("quarantined") if isinstance(hygiene.get("quarantined"), list) else []
    q_counts: dict = {}
    for q in quarantined:
        if isinstance(q, dict) and isinstance(q.get("reason"), str) and _CODE.fullmatch(q["reason"]):
            q_counts[q["reason"]] = q_counts.get(q["reason"], 0) + 1
    versions = report.get("versions") if isinstance(report.get("versions"), dict) else {}
    return {
        "renderer_version": RENDERER_VERSION, "preview": bool(preview),
        "brand": {"name": branding.name, "accent": branding.accent},
        "audit_id": aid, "report_sha256": rsha, "domain": domain, "scheme": scheme,
        "pages": [{"path": ctx.shown[p][0], "url": ctx.page_url(ctx.shown[p][0]) if ctx.shown[p][1] else None}
                  for p in paths],
        "started": started, "finished": finished,
        "run_outcome": report.get("outcome") if isinstance(report.get("outcome"), str)
        and _CODE.fullmatch(report["outcome"]) else "UNKNOWN",
        "sections": sections, "not_connected": not_connected, "not_connected_other": unknown_nc,
        "global_limits": [safe_text(x, 600) for x in (report.get("limitations") or [])[:40] if isinstance(x, str)],
        "methods": methods, "quarantined": sorted(q_counts.items()),
        "versions": {k: safe_token(v) for k, v in sorted(versions.items()) if isinstance(k, str) and _TOKEN.fullmatch(k)
                     and isinstance(v, str)},
    }


# ------------------------------------------------------------------------------------------------ Markdown

_MD_SPECIAL = set("\\`*_[]<>|~&#!")
_LINKISH = re.compile(r"[^\s|]*(?:://|www\.|@|\.[a-z]{2,})[^\s|]*", re.IGNORECASE)


def _code_span(tok: str) -> str:
    fence = "`" * (max((len(r) for r in re.findall(r"`+", tok)), default=0) + 1)
    return f"{fence} {tok} {fence}"


def md(text: Any) -> str:
    """Text as literal Markdown: no link, autolink, image, HTML, emphasis, heading or table cell can be smuggled in.
    A token GitHub-flavoured Markdown could autolink (scheme://, www., e-mail, a dotted host) goes in a code span,
    since escapes do not stop autolinks."""
    text = _clean(text)
    out, pos = [], 0
    for m_ in _LINKISH.finditer(text):
        tok = m_.group(0).rstrip(".,;:!?)")           # sentence punctuation stays outside the code span
        if not _LINKISH.fullmatch(tok):
            tok = m_.group(0)
        out += ["".join("\\" + ch if ch in _MD_SPECIAL else ch for ch in text[pos:m_.start()]),
                _code_span(tok.replace("|", "∣"))]
        pos = m_.start() + len(tok)
    out.append("".join("\\" + ch if ch in _MD_SPECIAL else ch for ch in text[pos:]))
    return "".join(out)


def md_code(text: Any) -> str:
    return _code_span(_clean(text).replace("|", "∣"))


def _md_val(model: dict) -> str:
    if model["t"] == "text":
        return md(model["v"])
    if model["t"] == "code":
        return md_code(model["v"])
    items = ", ".join(md_code(x) for x in model["v"])
    return items + (", …" if model.get("more") else "")


def _md_loc(loc: Optional[dict]) -> str:
    if loc is None:
        return "site-wide"
    return md_code(loc["path"] if loc["kind"] == "page" else loc["text"])


def render_markdown(m: dict) -> str:
    L = []
    if m["preview"]:
        L += ["> **PREVIEW.** Rendered offline and not recorded on the ledger. Not for delivery to a client.", ""]
    L += [f"# SEO technical audit: {md_code(m['domain'])}", "",
          f"Prepared by {md(m['brand']['name'])}.", "",
          "## Cover", "",
          f"- **Website audited:** {md_code(m['scheme'] + '://' + m['domain'])}",
          f"- **Audit ID:** {md_code(m['audit_id'])}",
          f"- **Run:** {md(m['started'])} to {md(m['finished'])}",
          f"- **Run outcome:** {md(m['run_outcome'])} (whether every check could run; not a score)",
          f"- **Audit record:** report SHA-256 {md_code(m['report_sha256'])}, recorded on a tamper-evident ledger "
          "when the audit completed.",
          f"- **Pages audited ({len(m['pages'])}):** " + ", ".join(md_code(p["path"]) for p in m["pages"]), "",
          "### What was checked", "",
          "| Area | Status | Findings | Data freshness |", "|---|---|---|---|"]
    for s in m["sections"]:
        n = sum(1 for _ in s["groups"])
        L.append(f"| {md(s['title'])} | **{md(s['status']['label'])}** | "
                 f"{n if s['status']['checked'] else '—'} | {md(s['freshness'])} |")
    L += ["", "Findings are counted per rule (one rule may affect several pages). An area marked NOT CONNECTED, NOT "
          "COVERED or NOT CHECKED was not observed: it has no findings because it could not be looked at, not because "
          "there is nothing there.", ""]
    if m["not_connected"] or m["not_connected_other"]:
        L += ["### Not connected for this audit", ""]
        L += [f"- {md(x)}" for x in m["not_connected"]]
        L += [f"- {md_code(x)}" for x in m["not_connected_other"]]
        L.append("")
    L += ["### How to read this report", "",
          "- Every finding states its **severity** (critical, high, medium, low, info), its **evidence class** "
          "(OBSERVED: measured by this audit's own requests; INFERRED: drawn by a stated rule; ESTIMATED: an estimate "
          "with its assumption), the **rule** that produced it, and a **recommended fix**.",
          "- There is no overall score and no grade. Findings of different kinds are not added together.",
          "- This report does not promise rankings, traffic or citations by any search or answer engine, and it makes "
          "no comparison claims about any other provider.",
          "- Text from your pages, sitemaps and robots.txt is quoted as data only. Other URLs are shown as templates "
          "(identifiers replaced by placeholders such as {slug} or {number}); the exact URLs are in the audit record.",
          ""]
    for s in m["sections"]:
        L += _md_section(s)
    L += ["## Methodology and limits", ""]
    L += ["### Applies to the whole audit", ""] + [f"- {md(x)}" for x in m["global_limits"]] + [""]
    for meth in m["methods"]:
        L += [f"### {md(meth['title'])}", "", f"**Method.** {md(meth['method'])}", ""]
        if meth["limits"]:
            L += ["**Limits.**", ""] + [f"- {md(x)}" for x in meth["limits"]] + [""]
    if m["quarantined"]:
        L += ["### Inputs set aside", "", "Malformed or unsafe inputs were set aside unread (never parsed further):",
              ""] + [f"- {md_code(r)}: {n}" for r, n in m["quarantined"]] + [""]
    if m["versions"]:
        L += ["### Versions", ""] + [f"- {md(k.replace('_', ' '))}: {md_code(v)}" for k, v in m["versions"].items()]
        L += [f"- report renderer: {md_code(m['renderer_version'])}", ""]
    L += ["---", "", f"Prepared by {md(m['brand']['name'])}. This report recommends changes; it does not make any "
          "change to your site.", ""]
    return "\n".join(L)


def _no_findings(st: dict) -> str:
    if st["outcome"] == "OK":
        return "No findings in this area."
    return ("No findings in the part that could be checked. The rest of this area was not checked, so this is not a "
            "clean result.")


def _md_section(s: dict) -> list:
    st = s["status"]
    L = [f"## {md(s['title'])}", "", f"**Status: {md(st['label'])}.** {md(s['what'])}", "",
         f"Data freshness: {md(s['freshness'])}.", ""]
    if not st["checked"]:
        L += [f"{md(st['why'] or '')} {NOT_CLEAN}".strip()]
        if st["reason_code"]:
            L[-1] += f" (reason {md_code(st['reason_code'])})"
        L.append("")
    elif st["why"]:
        L += [md(st["why"]), ""]
    if s["key"] == "search" and s["search"]:
        L += _md_search(s["search"])
    if s["key"] == "ai":
        L += _md_ai(s)
    if not st["checked"] and s["notes"]:
        L += ["Notes from the run are listed under Methodology and limits.", ""]
    if st["checked"] and not s["groups"]:
        L += [md(_no_findings(st)), ""]
    if s["dropped"] and st["checked"]:                 # AEGIS N-1: never under an area that was not checked
        L += [f"{md(s['dropped'])} more findings are in the audit record (the list is bounded).", ""]
    for g in s["groups"]:
        L += [f"### {md(g['title'])}", "",
              f"- **Severity:** {md(g['severity'])} · **Recommended action:** {md(g['decision'])}",
              f"- **Evidence:** {md(g['evidence'])} ({md(g['evidence_how'])}) · **Rule:** {md_code(g['code'])}"
              + (f" · **Area:** {md_code(g['capability'])}" if g["capability"] else ""),
              f"- **Fix:** {md(g['fix'])}"]
        if g["locations"]:
            L.append("- **Where:** " + ", ".join(_md_loc(x) for x in g["locations"])
                     + (f" and {g['locations_more']} more" if g["locations_more"] else ""))
        for inst in g["instances"]:
            bits = "; ".join(f"{md(lbl)}: {_md_val(v)}" for lbl, v in inst["items"])
            at = f" at {_md_loc(inst['location'])}" if inst["location"] else ""
            L.append(f"- **Evidence{at}:** {bits}")
        if g["instances_more"]:
            L.append(f"- Evidence for {g['instances_more']} more instances is in the audit record.")
        L.append("")
    return L


def _md_search(b: dict) -> list:
    L = [f"Property {md_code(b['property'])}, {md(b['window_start'])} to {md(b['window_end'])}"
         + (f" ({md(b['days'])} days)" if b["days"] else "") + ".", "",
         "| Measure | Value | Evidence |", "|---|---|---|",
         f"| Clicks | {md(b['clicks'])} | OBSERVED (first-party, as reported by Google Search Console) |",
         f"| Impressions | {md(b['impressions'])} | OBSERVED (first-party, as reported by Google Search Console) |",
         f"| Click-through rate | {md(b['ctr'] or 'n/a')} | OBSERVED (clicks divided by impressions over the window) |",
         f"| Average position | {md(b['position'] or 'n/a')} | ESTIMATED (assumption: the impression-weighted mean of "
         "Google's daily positions approximates the window's position; it is not Google's own figure) |", "",
         "These figures are not paired with revenue (analytics and CRM are not connected), so no decision is drawn "
         "from them.", ""]
    if b["expired"]:
        L += ["The breakdowns by query class and page template are past their retention period; only the window "
              "totals are kept.", ""]
        return L
    if b["classes"]:
        L += ["**By query class** (no query text is kept):", "", "| Class | Clicks | Impressions | CTR |",
              "|---|---|---|---|"]
        L += [f"| {md(c['name'])} | {md(c['clicks'])} | {md(c['impressions'])} | {md(c['ctr'] or 'n/a')} |"
              for c in b["classes"]]
        if b["unattributed"]:
            L.append(f"| unattributed (anonymised by Google, or past the caps) | {md(b['unattributed']['clicks'])} | "
                     f"{md(b['unattributed']['impressions'])} | n/a |")
        L.append("")
    if b["templates"]:
        L += ["**By page template** (aggregated by page; not comparable with the totals above):", "",
              "| Page template | Clicks | Impressions | CTR |", "|---|---|---|---|"]
        L += [f"| {md_code(t['template'])} | {md(t['clicks'])} | {md(t['impressions'])} | {md(t['ctr'] or 'n/a')} |"
              for t in b["templates"]]
        if b["templates_more"]:
            L.append(f"| {b['templates_more']} more templates in the audit record | | | |")
        L.append("")
    return L


def _md_ai(s: dict) -> list:
    L = []
    a = s["ai"]
    if a and a["engines"]:
        L += ["| Engine | Status | Questions asked | Not asked |", "|---|---|---|---|"]
        L += [f"| {md_code(e['engine'])} | {md(e['status'])} | {md(e['asked'] or '—')} | "
              f"{md(e['not_asked'] or '—')} |" for e in a["engines"]]
        L.append("")
        L += [f"- {md_code(e['engine'])}: {md(e['sampling'])}" for e in a["engines"]] + [""]
        L += [("Question set"
               + (f" version {md(a['prompt_set_version'])}, fingerprint {md_code(a['prompt_set_sha'])}"
                  if a["prompt_set_version"] and a["prompt_set_sha"] else "")
               + ". Questions are referred to by number; their text and the answers are not reproduced."), ""]
    v = s.get("volume")
    if v and not v["checked"]:
        L += [f"Prompt volume: **{md(v['label'])}**. How often real people ask these questions is not known.", ""]
    return L


# ------------------------------------------------------------------------------------------------ HTML

def h(text: Any) -> str:
    return html.escape(_clean(text), quote=True)


_CSS = """
:root{--accent:%(accent)s;--ink:#1a1a1a;--muted:#555;--line:#d9d9d9;--soft:#f5f6f8}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--ink);font:15px/1.5 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 20px 48px}
header.cover{border-top:6px solid var(--accent);padding-top:20px;margin-bottom:28px}
h1{font-size:26px;margin:0 0 4px}
h2{font-size:20px;margin:36px 0 8px;padding-bottom:4px;border-bottom:2px solid var(--accent)}
h3{font-size:16px;margin:22px 0 6px}h4{font-size:14px;margin:16px 0 4px}
.brand{color:var(--muted);margin:0 0 16px}
code{font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--soft);padding:1px 4px;
  border-radius:3px;overflow-wrap:anywhere}
dl.facts{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:0}
dl.facts dt{font-weight:600}dl.facts dd{margin:0;overflow-wrap:anywhere}
table{border-collapse:collapse;width:100%%;margin:8px 0 16px;font-size:14px}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
th{background:var(--soft)}
.status{display:inline-block;font-weight:700;font-size:12px;letter-spacing:.03em;padding:2px 8px;border-radius:10px;
  border:1px solid currentColor}
.st-ok{color:#1b6e3a}.st-partial{color:#8a5a00}.st-off{color:#8a1c1c}
.notclean{background:#fff4f4;border-left:4px solid #8a1c1c;padding:8px 12px;margin:8px 0}
.finding{border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:4px;padding:10px 14px;
  margin:12px 0;break-inside:avoid}
.finding h4{margin-top:0}
.sev{font-weight:700;text-transform:uppercase;font-size:12px}
.sev-critical{color:#8a1c1c}.sev-high{color:#b33a00}.sev-medium{color:#8a5a00}.sev-low{color:#35537a}.sev-info{color:#555}
.meta{color:var(--muted);font-size:13px}
ul{padding-left:20px}
.preview{background:#fff4d6;border:1px solid #c9a227;padding:8px 12px;margin-bottom:16px;font-weight:600}
footer{margin-top:40px;border-top:1px solid var(--line);padding-top:12px;color:var(--muted);font-size:13px}
@media print{body{font-size:11pt}main{max-width:none;padding:0}h2{break-after:avoid}section.area{break-before:page}
a{color:inherit;text-decoration:none}}
"""


def _h_val(model: dict) -> str:
    if model["t"] == "text":
        return h(model["v"])
    if model["t"] == "code":
        return f"<code>{h(model['v'])}</code>"
    items = ", ".join(f"<code>{h(x)}</code>" for x in model["v"])
    return items + (", …" if model.get("more") else "")


def _h_loc(loc: Optional[dict], pages: dict) -> str:
    if loc is None:
        return "site-wide"
    if loc["kind"] == "page":
        url = pages.get(loc["path"])
        if url:                                    # the audited page on the registered domain: built from the request
            return f'<a href="{h(url)}" rel="nofollow noopener noreferrer"><code>{h(loc["path"])}</code></a>'
        return f"<code>{h(loc['path'])}</code>"
    return f"<code>{h(loc['text'])}</code>"


def _st_class(st: dict) -> str:
    return "st-ok" if st["outcome"] == "OK" else "st-partial" if st["outcome"] == "PARTIAL" else "st-off"


def render_html(m: dict) -> str:
    pages = {p["path"]: p["url"] for p in m["pages"]}
    css = _CSS % {"accent": m["brand"]["accent"]}
    o = ["<!doctype html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width, initial-scale=1">',
         "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; "
         "base-uri 'none'; form-action 'none'\">",
         '<meta name="referrer" content="no-referrer">', '<meta name="robots" content="noindex, nofollow">',
         f"<title>SEO audit: {h(m['domain'])}</title>", f"<style>{css}</style>", "</head>", "<body>", "<main>"]
    if m["preview"]:
        o.append('<div class="preview">PREVIEW. Rendered offline and not recorded on the ledger. Not for delivery to '
                 "a client.</div>")
    o += ['<header class="cover">', f"<h1>SEO technical audit: <code>{h(m['domain'])}</code></h1>",
          f'<p class="brand">Prepared by {h(m["brand"]["name"])}</p>', '<dl class="facts">',
          f"<dt>Website audited</dt><dd><code>{h(m['scheme'] + '://' + m['domain'])}</code></dd>",
          f"<dt>Audit ID</dt><dd><code>{h(m['audit_id'])}</code></dd>",
          f"<dt>Run</dt><dd>{h(m['started'])} to {h(m['finished'])}</dd>",
          f"<dt>Run outcome</dt><dd>{h(m['run_outcome'])} (whether every check could run; not a score)</dd>",
          f"<dt>Audit record</dt><dd>report SHA-256 <code>{h(m['report_sha256'])}</code>, recorded on a "
          "tamper-evident ledger when the audit completed</dd>",
          f"<dt>Pages audited ({len(m['pages'])})</dt><dd>"
          + ", ".join(_h_loc({"kind": "page", "path": p["path"]}, pages) for p in m["pages"]) + "</dd>",
          "</dl>", "</header>",
          "<section>", "<h2>What was checked</h2>",
          "<table><thead><tr><th>Area</th><th>Status</th><th>Findings</th><th>Data freshness</th></tr></thead><tbody>"]
    for s in m["sections"]:
        st = s["status"]
        o.append(f"<tr><td>{h(s['title'])}</td><td><span class=\"status {_st_class(st)}\">{h(st['label'])}</span></td>"
                 f"<td>{len(s['groups']) if st['checked'] else '—'}</td><td>{h(s['freshness'])}</td></tr>")
    o += ["</tbody></table>",
          "<p>Findings are counted per rule (one rule may affect several pages). An area marked NOT CONNECTED, NOT "
          "COVERED or NOT CHECKED was not observed: it has no findings because it could not be looked at, not because "
          "there is nothing there.</p>"]
    if m["not_connected"] or m["not_connected_other"]:
        o += ["<h3>Not connected for this audit</h3>", "<ul>"]
        o += [f"<li>{h(x)}</li>" for x in m["not_connected"]]
        o += [f"<li><code>{h(x)}</code></li>" for x in m["not_connected_other"]]
        o.append("</ul>")
    o += ["<h3>How to read this report</h3>", "<ul>",
          "<li>Every finding states its <strong>severity</strong> (critical, high, medium, low, info), its "
          "<strong>evidence class</strong> (OBSERVED: measured by this audit's own requests; INFERRED: drawn by a "
          "stated rule; ESTIMATED: an estimate with its assumption), the <strong>rule</strong> that produced it, and a "
          "<strong>recommended fix</strong>.</li>",
          "<li>There is no overall score and no grade. Findings of different kinds are not added together.</li>",
          "<li>This report does not promise rankings, traffic or citations by any search or answer engine, and it "
          "makes no comparison claims about any other provider.</li>",
          "<li>Text from your pages, sitemaps and robots.txt is quoted as data only. Other URLs are shown as templates "
          "(identifiers replaced by placeholders such as {slug} or {number}); the exact URLs are in the audit "
          "record.</li>", "</ul>", "</section>"]
    for s in m["sections"]:
        o += _h_section(s, pages)
    o += ['<section class="area">', "<h2>Methodology and limits</h2>", "<h3>Applies to the whole audit</h3>", "<ul>"]
    o += [f"<li>{h(x)}</li>" for x in m["global_limits"]] + ["</ul>"]
    for meth in m["methods"]:
        o += [f"<h3>{h(meth['title'])}</h3>", f"<p><strong>Method.</strong> {h(meth['method'])}</p>"]
        if meth["limits"]:
            o += ["<p><strong>Limits.</strong></p>", "<ul>"] + [f"<li>{h(x)}</li>" for x in meth["limits"]] + ["</ul>"]
    if m["quarantined"]:
        o += ["<h3>Inputs set aside</h3>", "<p>Malformed or unsafe inputs were set aside unread (never parsed "
              "further):</p>", "<ul>"] + [f"<li><code>{h(r)}</code>: {n}</li>" for r, n in m["quarantined"]]
        o.append("</ul>")
    if m["versions"]:
        o += ["<h3>Versions</h3>", "<ul>"]
        o += [f"<li>{h(k.replace('_', ' '))}: <code>{h(v)}</code></li>" for k, v in m["versions"].items()]
        o += [f"<li>report renderer: <code>{h(m['renderer_version'])}</code></li>", "</ul>"]
    o += ["</section>", f"<footer>Prepared by {h(m['brand']['name'])}. This report recommends changes; it does not "
          "make any change to your site.</footer>", "</main>", "</body>", "</html>", ""]
    return "\n".join(o)


def _h_section(s: dict, pages: dict) -> list:
    st = s["status"]
    o = [f'<section class="area" id="{h(s["key"])}">', f"<h2>{h(s['title'])}</h2>",
         f"<p><span class=\"status {_st_class(st)}\">{h(st['label'])}</span> {h(s['what'])}</p>",
         f'<p class="meta">Data freshness: {h(s["freshness"])}.</p>']
    if not st["checked"]:
        reason = f" (reason <code>{h(st['reason_code'])}</code>)" if st["reason_code"] else ""
        o.append(f'<p class="notclean">{h(st["why"] or "")} {h(NOT_CLEAN)}{reason}</p>')
    elif st["why"]:
        o.append(f"<p>{h(st['why'])}</p>")
    if s["key"] == "search" and s["search"]:
        o += _h_search(s["search"])
    if s["key"] == "ai":
        o += _h_ai(s)
    if not st["checked"] and s["notes"]:
        o.append("<p>Notes from the run are listed under Methodology and limits.</p>")
    if st["checked"] and not s["groups"]:
        o.append(f"<p>{h(_no_findings(st))}</p>")
    if s["dropped"] and st["checked"]:                 # AEGIS N-1
        o.append(f"<p>{h(s['dropped'])} more findings are in the audit record (the list is bounded).</p>")
    for g in s["groups"]:
        o += ['<div class="finding">', f"<h4>{h(g['title'])}</h4>",
              f'<p class="meta"><span class="sev sev-{h(g["severity"])}">{h(g["severity"])}</span> · '
              f"Recommended action: {h(g['decision'])} · Evidence: <strong>{h(g['evidence'])}</strong> "
              f"({h(g['evidence_how'])}) · Rule <code>{h(g['code'])}</code>"
              + (f" · Area <code>{h(g['capability'])}</code>" if g["capability"] else "") + "</p>",
              f"<p><strong>Fix.</strong> {h(g['fix'])}</p>"]
        if g["locations"]:
            o.append("<p><strong>Where.</strong> " + ", ".join(_h_loc(x, pages) for x in g["locations"])
                     + (f" and {g['locations_more']} more" if g["locations_more"] else "") + "</p>")
        if g["instances"]:
            o.append("<ul>")
            for inst in g["instances"]:
                bits = "; ".join(f"{h(lbl)}: {_h_val(v)}" for lbl, v in inst["items"])
                at = f" at {_h_loc(inst['location'], pages)}" if inst["location"] else ""
                o.append(f"<li>Evidence{at}: {bits}</li>")
            if g["instances_more"]:
                o.append(f"<li>Evidence for {g['instances_more']} more instances is in the audit record.</li>")
            o.append("</ul>")
        o.append("</div>")
    o.append("</section>")
    return o


def _h_search(b: dict) -> list:
    obs = "OBSERVED (first-party, as reported by Google Search Console)"
    o = [f"<p>Property <code>{h(b['property'])}</code>, {h(b['window_start'])} to {h(b['window_end'])}"
         + (f" ({h(b['days'])} days)" if b["days"] else "") + ".</p>",
         "<table><thead><tr><th>Measure</th><th>Value</th><th>Evidence</th></tr></thead><tbody>",
         f"<tr><td>Clicks</td><td>{h(b['clicks'])}</td><td>{obs}</td></tr>",
         f"<tr><td>Impressions</td><td>{h(b['impressions'])}</td><td>{obs}</td></tr>",
         f"<tr><td>Click-through rate</td><td>{h(b['ctr'] or 'n/a')}</td><td>OBSERVED (clicks divided by impressions "
         "over the window)</td></tr>",
         f"<tr><td>Average position</td><td>{h(b['position'] or 'n/a')}</td><td>ESTIMATED (assumption: the "
         "impression-weighted mean of Google's daily positions approximates the window's position; it is not "
         "Google's own figure)</td></tr>", "</tbody></table>",
         "<p>These figures are not paired with revenue (analytics and CRM are not connected), so no decision is drawn "
         "from them.</p>"]
    if b["expired"]:
        o.append("<p>The breakdowns by query class and page template are past their retention period; only the "
                 "window totals are kept.</p>")
        return o
    if b["classes"]:
        o += ["<h3>By query class (no query text is kept)</h3>",
              "<table><thead><tr><th>Class</th><th>Clicks</th><th>Impressions</th><th>CTR</th></tr></thead><tbody>"]
        o += [f"<tr><td>{h(c['name'])}</td><td>{h(c['clicks'])}</td><td>{h(c['impressions'])}</td>"
              f"<td>{h(c['ctr'] or 'n/a')}</td></tr>" for c in b["classes"]]
        if b["unattributed"]:
            o.append(f"<tr><td>unattributed (anonymised by Google, or past the caps)</td>"
                     f"<td>{h(b['unattributed']['clicks'])}</td><td>{h(b['unattributed']['impressions'])}</td>"
                     "<td>n/a</td></tr>")
        o.append("</tbody></table>")
    if b["templates"]:
        o += ["<h3>By page template (aggregated by page; not comparable with the totals above)</h3>",
              "<table><thead><tr><th>Page template</th><th>Clicks</th><th>Impressions</th><th>CTR</th></tr></thead>"
              "<tbody>"]
        o += [f"<tr><td><code>{h(t['template'])}</code></td><td>{h(t['clicks'])}</td><td>{h(t['impressions'])}</td>"
              f"<td>{h(t['ctr'] or 'n/a')}</td></tr>" for t in b["templates"]]
        if b["templates_more"]:
            o.append(f"<tr><td colspan=\"4\">{b['templates_more']} more templates in the audit record</td></tr>")
        o.append("</tbody></table>")
    return o


def _h_ai(s: dict) -> list:
    o = []
    a = s["ai"]
    if a and a["engines"]:
        o += ["<table><thead><tr><th>Engine</th><th>Status</th><th>Questions asked</th><th>Not asked</th></tr>"
              "</thead><tbody>"]
        o += [f"<tr><td><code>{h(e['engine'])}</code></td><td>{h(e['status'])}</td><td>{h(e['asked'] or '—')}"
              f"</td><td>{h(e['not_asked'] or '—')}</td></tr>" for e in a["engines"]]
        o.append("</tbody></table>")
        o += ["<ul>"] + [f"<li><code>{h(e['engine'])}</code>: {h(e['sampling'])}</li>" for e in a["engines"]]
        o.append("</ul>")
        o.append("<p>Question set"
                 + (f" version {h(a['prompt_set_version'])}, fingerprint <code>{h(a['prompt_set_sha'])}</code>"
                    if a["prompt_set_version"] and a["prompt_set_sha"] else "")
                 + ". Questions are referred to by number; their text and the answers are not reproduced.</p>")
    v = s.get("volume")
    if v and not v["checked"]:
        o.append(f"<p>Prompt volume: <strong>{h(v['label'])}</strong>. How often real people ask these questions is "
                 "not known.</p>")
    return o


# ------------------------------------------------------------------------------------------------ entry point

def render(audit: dict, tenant_id: str, fmt: str, branding: Branding = DEFAULT_BRANDING,
           preview: bool = False) -> tuple[bytes, dict]:
    """(document bytes, model) for one completed audit. ``fmt`` is ``markdown`` or ``html``."""
    if fmt not in FORMATS:
        raise ReportInputError("unknown format")
    model = build(audit, tenant_id, branding, preview)
    text = render_markdown(model) if fmt == "markdown" else render_html(model)
    return text.encode("utf-8"), model


def document_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
