"""
Intelligence 1 — Document Register (Legal spec §B.1, §C.1). Decides versions, the current version and the
approval preconditions; never approves (Andre does) and never answers "current" for an unapproved or expired
version.

- Current version = the highest ``approved`` version with ``effective_at`` <= now and ``review_by`` > now; none
  -> no current version (consumers block).
- Approval (LG-02): Andre's token on the version's content hash AND (``counsel_required`` false OR a counsel
  sign-off whose ``doc_sha256`` equals the version SHA-256). Documents a counsel question blocks (§I: CQ-17 ->
  ic_agreement, CQ-15 -> not_legal_advice_v1) also need that question verified (LG-18); the engagement letter
  needs its AI-use clause ``ENG-AI-01`` (CQ-24, LG-17).
- LG-20 (ADR 0022): a document marked ``founder_approval_allowed`` (client_msa, the SOWs, zbc_order_form) may be
  approved by Andre as founder, with a written rationale; it is recorded with ``approval_basis: founder_approval``.
- Template fill: the template is the document's highest in-force approved version that declares template
  variables and is not itself a fill (a filled instance is never a template); placeholders ``{{name}}`` are
  replaced by typed, bounded variables only. Every string variable passes the advice-text guard (the counsel-
  approved body does not need to).
"""

from __future__ import annotations

import bisect
import html
import re
import unicodedata
from datetime import date, datetime
from typing import Optional

import reasons as R

NUMBER, NAME, ACTOR = 1, "Document Register", "intel_01_documents"
VERSION_RE = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}$")
PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]{0,39})\}\}")
VAR_TYPES = ("string", "date", "int", "enum")
VAR_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_VARS = 50
STRING_MAX = 500
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")


def vkey(v: str) -> tuple[int, int]:
    a, b = v.split(".")
    return int(a), int(b)


def current(versions: list[dict], now: datetime) -> Optional[dict]:
    from clock import parse_iso
    live = [v for v in versions if v["status"] == "approved" and v.get("effective_at") and v.get("review_by")
            and parse_iso(v["effective_at"]) <= now < parse_iso(v["review_by"])]
    return max(live, key=lambda v: vkey(v["version"]), default=None)


def validate_schema(schema) -> dict:
    """Typed template-variable schema: {name: {type, max_length | values | min,max}}. Raises ValueError."""
    if not isinstance(schema, dict) or len(schema) > MAX_VARS:
        raise ValueError(f"template_variables: an object of at most {MAX_VARS} variables")
    out = {}
    for name, spec in schema.items():
        if not isinstance(name, str) or not VAR_NAME.fullmatch(name) or not isinstance(spec, dict):
            raise ValueError("template variable names are [a-z][a-z0-9_]{0,39} with an object spec")
        t = spec.get("type")
        if t not in VAR_TYPES:
            raise ValueError(f"variable {name}: type must be one of {', '.join(VAR_TYPES)}")
        allowed = {"type"} | {"string": {"max_length"}, "enum": {"values"}, "int": {"min", "max"}, "date": set()}[t]
        if set(spec) - allowed:
            raise ValueError(f"variable {name}: unknown keys")
        if t == "string":
            ml = spec.get("max_length", 200)
            if isinstance(ml, bool) or not isinstance(ml, int) or not 1 <= ml <= STRING_MAX:
                raise ValueError(f"variable {name}: max_length 1..{STRING_MAX}")
        if t == "enum":
            vals = spec.get("values")
            if not isinstance(vals, list) or not 1 <= len(vals) <= 50 or not all(
                    isinstance(x, str) and 1 <= len(x) <= 100 and not _CONTROL.search(x) for x in vals):
                raise ValueError(f"variable {name}: values is a list of 1..50 short strings")
        if t == "int":
            for k in ("min", "max"):
                if k in spec and (isinstance(spec[k], bool) or not isinstance(spec[k], int)):
                    raise ValueError(f"variable {name}: {k} must be an integer")
        out[name] = dict(spec)
    return out


def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text))


_ANY_FIELD = re.compile(r"\{\{[^{}]{0,80}\}\}")
# ADR 0022 unfilled-placeholder guard (AEGIS H1/M1, r2 N1-N4/N9, r3 C1/C2/R2/R3). A BACKSTOP to Andre's own reading
# of the text, not a guarantee: it detects exactly the forms below and nothing else (ADR 0022 lists what it misses).
# Preparation: HTML entities decoded and NFKC applied until stable (3 passes at most), format characters (Unicode Cf)
# removed, line endings and curly quotes folded, and every Unicode opening/closing punctuation pair (Ps/Pe, except
# round and curly brackets and the low-9 quotes) read as a square bracket.
# Counted (overlapping detections of one field count once):
#   - ANY ``[...]`` span (empty, any content, any length, wrapped over single line breaks), and any ``[`` that is
#     never closed before a blank line, unless the span is
#       * exactly one boilerplate phrase (case-insensitive, whitespace collapsed, trailing "." dropped),
#       * a numbered footnote ``[^1]``-``[^999]``,
#       * a list marker ``[a]``/``[iv]`` (never ``[x]``) at the start of a line, right after ``(`` or right after a
#         section reference ("Section 4 [a]", "§4[c]"),
#       * a markdown link ``[text](target)`` to an http(s) URL or an existing ``#anchor``, whose text is neither all
#         capitals nor a drafting word (insert, TBD, TBA, TBC, TODO, placeholder);
#   - ``<<..>>``, ``«..»``, ``‹..›``, ``≪..≫``;
#   - a single ``{...}`` that is blank, all capitals, names a field, or is an identifier/words (``{client_name}``);
#     a single ``<...>`` that is blank, all capitals, names a field, or is a snake_case identifier;
#   - a ``{{field}}`` that is not one of the version's declared template variables;
#   - ``___`` (three or more underscores) anywhere except a line that is exactly ``<label>: ____`` with the label one
#     of By, Name, Title, Date, Signature, Its, Signed, Initials; a currency sign before ``___``; ``__/__/____``;
#   - an X-amount (``$X,XXX``) or a placeholder date (``XX/XX/XXXX``, ``00/00/0000``, ``MM/DD/YYYY``);
#   - a free-standing TBD / TBC / TBA / TODO (any case) or ``T.B.D.``.
# Every step is a single regex pass or bounded look-around, so the cost is linear in the text (r3 C1).
_SQUARE = re.compile(r"\[((?:[^\[\]\n]|\n(?![ \t]*\n))*)\]")
_OPEN = re.compile(r"\[")
_FAMILIES = re.compile(r"<<[^<>]*>>|\u00ab[^\u00ab\u00bb]*\u00bb|\u2039[^\u2039\u203a]*\u203a|\u226a[^\u226a\u226b]*\u226b")
_SINGLE = re.compile(r"(?<![{<])\{(?![{])([^{}\n]{0,200})\}(?!\})|(?<!<)<(?!<)([^<>\n]{0,200})>(?!>)")
_CURLY = re.compile(r"\{\{([^{}\n]{0,120})\}\}")
_UNDERSCORES = re.compile(r"_{3,}")
_BLANKS = re.compile(r"[$\u20ac\u00a3][ \t]*_{3,}|(?:_{1,2}/){2}_{2,4}|[$\u20ac\u00a3][ \t]*X[X,.]*(?![A-Za-z])"
                     r"|\b(?:XX|00|MM|DD)/(?:XX|00|MM|DD)/(?:XXXX|0000|YYYY|YY)\b"
                     r"|(?i:\b(?:tbd|tbc|tba|todo)\b)|\bT\.B\.[DCA]\b\.?")
# r4 C4: after the first run, more underscores only after whitespace, so no two parts can both take a "_": a line
# that fails the match fails in linear time ("By: ___ ___" stays exempt)
# Money red team RTM-04: drafting forms outside any bracket. Every repetition is bounded or unambiguous (no two
# adjacent parts can take the same character), so each stays linear.
_DRAFTING = re.compile(
    r"\bX{3,}\b"                                                     # XXX, XXXX, USD XXX
    r"|\bXX/(?:XX|\d{1,2})/(?:XXXX|XX|\d{2,4})\b|\b\d{1,2}/XX/"            # XX/XX/2026, 01/XX/2026
    r"|(?i:\((?:[ \t]*(?:insert|enter|placeholder|tbd|tbc|tba|to be (?:agreed|updated|confirmed|determined"
    r"|completed|provided|inserted))\b)[^()\n]{0,160}\))"           # (insert fee), (to be agreed)
    r"|%%[^%\n]{1,80}%%|%[A-Z][A-Z0-9_]{1,60}%"                       # %%FEE%%, %CLIENT_NAME%
    r"|(?<![\w_])__[A-Z][A-Z0-9_]{0,60}(?<!_)__(?![\w_])"             # __FEE__ (not markdown __bold__)
    r"|(?<![#\w])#[A-Z][A-Z0-9_]{0,40}#(?![#\w])"                     # #FEE#
    r"|(?i:\blorem ipsum\b)|(?i:\bplaceholder\b)|\?{2,}"                # Lorem ipsum, PLACEHOLDER, ??
    r"|[\u2580-\u259f]{2,}"                                           # ████ (block and shade characters)
    r"|\bTK(?:TK)*\b"                                                 # TK, TKTK (journalism "to come")
    r"|(?i:\b(?:name|fee|amount|address|price|email|details|text) here\b)")   # Client name here
# a label with nothing but a dash after it, or a sentence that ENDS on "the date of" / "as of" / "between"
_HANGING = re.compile(r":[ \t]*[-\u2013\u2014]+[ \t]*(?:\.[ \t]*)?$"
                      r"|(?i:\b(?:date of|as of|between))[ \t]*[.;]", re.MULTILINE)   # never a hard-wrapped line end
_SIGNATURE_LINE = re.compile(r"[ \t]*(?i:by|name|title|date|signature|its|signed|initials)[ \t]*:[ \t]*_{3,}"
                             r"(?:[ \t]+_+)*[ \t]*")
_FIELD_WORD = re.compile(r"\b(?:insert|tbd|tbc|tba|todo|placeholder|name|fee|fees|price|amount|date|address|client"
                         r"|party|company|entity|email|phone|title|rate|term)\b", re.IGNORECASE)
_DRAFTING_WORD = re.compile(r"\b(?:insert|tbd|tbc|tba|todo|placeholder)\b", re.IGNORECASE)
_LINK_TAIL = re.compile(r"\(([^()\s]{0,2000})\)")
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
_EXPLICIT_ANCHOR = re.compile(r"\{#([\w-]+)\}|\b(?:id|name)=[\"']([\w-]+)[\"']")
_MARKER = re.compile(r"(?:[a-wyzA-WYZ]|i|ii|iii|iv|v|vi|vii|viii|ix)")
_MARKER_AT_LINE_START = re.compile(r"[ \t>*+-]*")
_MARKER_WINDOW = 60
_BOILERPLATE = frozenset(
    {"signature page follows", "signature pages follow", "remainder of page intentionally left blank",
     "remainder of this page intentionally left blank", "this page intentionally left blank",
     "intentionally left blank", "intentionally omitted", "reserved", "n/a", "not applicable"}
    | {f"{k} {x}{suffix}" for k in ("exhibit", "schedule", "annex", "appendix", "attachment")
       for x in [chr(c) for c in range(ord("a"), ord("z") + 1)] + [str(n) for n in range(1, 100)]
       for suffix in [""] + ([f"-{n}" for n in range(1, 100)] if x.isalpha() else [])})
_KEEP_PAIRS = {ord("("), ord(")"), ord("{"), ord("}"), ord("["), ord("]"), 0x201A, 0x201E}
_TO_SQUARE = {c: ("[" if unicodedata.category(chr(c)) == "Ps" else "]") for c in range(0x30000)
              if unicodedata.category(chr(c)) in ("Ps", "Pe") and c not in _KEEP_PAIRS}
_TO_SQUARE.update({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x00A0: " "})
_TO_SQUARE.update({c: None for c in range(0x110000) if unicodedata.category(chr(c)) == "Cf"})   # format characters
_SECTION_WORDS = ("\u00a7", "section", "sec.", "clause", "article", "paragraph")


def _normalise(text: str) -> str:
    t = text
    for _ in range(3):                                   # &amp;#91; and full-width entities settle in a few passes
        u = unicodedata.normalize("NFKC", html.unescape(t))
        if u == t:
            break
        t = u
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    return t.translate(_TO_SQUARE)                       # Cf removed, Ps/Pe read as square, quotes folded


def _slug(heading: str) -> str:
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", heading.strip().lower()))


class _Lines:
    """Line boundaries computed once (r3 C1: no per-match scan back to the line start)."""

    def __init__(self, t: str):
        self.t = t
        self.starts = [0] + [m.end() for m in re.finditer("\n", t)]
        self._signature: dict[int, bool] = {}

    def index(self, pos: int) -> int:
        return bisect.bisect_right(self.starts, pos) - 1

    def start(self, pos: int) -> int:
        return self.starts[self.index(pos)]

    def is_signature_line(self, pos: int) -> bool:
        i = self.index(pos)
        if i not in self._signature:
            end = self.starts[i + 1] - 1 if i + 1 < len(self.starts) else len(self.t)
            self._signature[i] = _SIGNATURE_LINE.fullmatch(self.t, self.starts[i], end) is not None
        return self._signature[i]


def _after_section_ref(window: str) -> bool:
    """True when ``window`` ends with ``(``, or with a section reference: one of _SECTION_WORDS, optional spaces, up
    to 20 characters of ``[\\w.()]`` (the number) and optional spaces ("Section 4 ", "§4", "clause 3.1(a)"). At most
    21 suffix tests on a window of at most 60 characters: constant time per marker."""
    if window.endswith("("):
        return True
    w = window.rstrip(" \t").lower()
    run = 0
    while run < 20 and run < len(w) and (w[-1 - run].isalnum() or w[-1 - run] in "_.()"):
        run += 1
    return any(w[:len(w) - k].rstrip(" \t").endswith(_SECTION_WORDS) for k in range(run + 1))


def _square_exempt(t: str, m: re.Match, anchors: set, lines: _Lines) -> bool:
    inner = " ".join(m.group(1).split())
    if inner.rstrip(". ").lower() in _BOILERPLATE:
        return True
    if re.fullmatch(r"\^[1-9]\d{0,2}", inner):
        # a numbered footnote, but never right after a currency sign: "$[^500]" is an open amount (r4)
        return t[m.start() - 1:m.start()] not in ("$", "\u20ac", "\u00a3")
    if _MARKER.fullmatch(inner):
        ls = lines.start(m.start())
        lo = max(ls, m.start() - _MARKER_WINDOW)
        if lo == ls and _MARKER_AT_LINE_START.fullmatch(t, lo, m.start()):
            return True
        return _after_section_ref(t[lo:m.start()])
    tail = _LINK_TAIL.match(t, m.end())
    if tail:
        target = tail.group(1)
        url = re.fullmatch(r"https?://[^\s/]+\.[^\s]+", target) is not None
        anchor = target.startswith("#") and target[1:].lower() in anchors
        caps_field = re.search(r"[A-Z]{2}", inner) is not None and not re.search(r"[a-z]", inner)
        return (url or anchor) and not caps_field and not _DRAFTING_WORD.search(inner)
    return False


def _single_counts(inner: str, brace: bool) -> bool:
    inner = inner.strip()
    if not re.search(r"[^\W_]", inner):                                       # blank or punctuation only
        return True
    if re.search(r"[A-Z]{2}", inner) and not re.search(r"[a-z]", inner):      # all capitals
        return True
    if _FIELD_WORD.search(inner):                                              # names a field
        return True
    if brace:
        return re.fullmatch(r"[A-Za-z_][\w ]{0,80}", inner) is not None       # {client_name}, {client name}
    return re.fullmatch(r"[A-Za-z]\w*_\w+", inner) is not None                # <client_name>


def bracket_placeholders(text: str, declared: Optional[set] = None) -> int:
    """How many unfilled placeholders a text holds (the tokens themselves never leave Legal). ``declared``: the
    version's template variable names, whose ``{{name}}`` fields are legitimate. Linear in the text."""
    t = _normalise(text)
    lines = _Lines(t)
    anchors = {_slug(h) for h in _HEADING.findall(t)}
    anchors |= {(a or b).lower() for a, b in _EXPLICIT_ANCHOR.findall(t)}
    spans: list[tuple[int, int]] = []
    closed: list[tuple[int, int]] = []
    for m in _SQUARE.finditer(t):
        closed.append(m.span())
        if not _square_exempt(t, m, anchors, lines):
            spans.append(m.span())
    k = 0                                                # an opening bracket inside no closed span was never closed
    for m in _OPEN.finditer(t):
        while k < len(closed) and closed[k][1] <= m.start():
            k += 1
        if not (k < len(closed) and closed[k][0] <= m.start() < closed[k][1]):
            spans.append((m.start(), m.start() + 1))
    spans += [m.span() for m in _FAMILIES.finditer(t)]
    spans += [m.span() for m in _SINGLE.finditer(t)
              if _single_counts(m.group(1) if m.group(1) is not None else m.group(2), m.group(1) is not None)]
    names = declared or set()
    spans += [m.span() for m in _CURLY.finditer(t) if m.group(1) not in names]
    spans += [m.span() for m in _UNDERSCORES.finditer(t) if not lines.is_signature_line(m.start())]
    spans += [m.span() for m in _BLANKS.finditer(t)]
    spans += [m.span() for m in _DRAFTING.finditer(t)]
    spans += [m.span() for m in _HANGING.finditer(t)]
    count, reach = 0, -1                                # overlapping detections of one field count once
    for start, end in sorted(spans):
        if start >= reach:
            count += 1
        reach = max(reach, end)
    return count


def has_unresolved(text: str) -> bool:
    """Any ``{{...}}`` left in a text (well-formed placeholder or not): a template or a broken fill, never an
    instance a party can accept (AEGIS N17-4)."""
    return bool(_ANY_FIELD.search(text))


def next_version(existing: list[str], bump: str = "minor") -> Optional[str]:
    """The next version number of a document, assigned by Legal (AEGIS N17-5): 1.0 first; then the next minor
    (``a.b`` -> ``a.(b+1)``, rolling to ``(a+1).0`` after .9999) or, for Andre's ``bump: major``, ``(a+1).0``.
    None when the number space is exhausted."""
    if not existing:
        return "1.0"
    a, b = max(vkey(v) for v in existing)
    if bump == "major" or b >= 9999:
        a, b = a + 1, 0
    else:
        b += 1
    return None if a > 9999 else f"{a}.{b}"


def check_variables(schema: dict, variables) -> dict:
    """Type/bounds check of supplied variables (all declared variables required, no others). Returns the values
    as strings for rendering. Raises ValueError."""
    if not isinstance(variables, dict):
        raise ValueError("variables must be an object")
    extra, missing = set(variables) - set(schema), set(schema) - set(variables)
    if extra or missing:
        raise ValueError(f"variables: unknown {sorted(extra)[:5]}, missing {sorted(missing)[:5]}")
    out = {}
    for name, spec in schema.items():
        v = variables[name]
        t = spec["type"]
        if t == "string":
            if not isinstance(v, str) or not v.strip() or len(v) > spec.get("max_length", 200) or _CONTROL.search(v):
                raise ValueError(f"variable {name}: a printable single-line string of at most "
                                 f"{spec.get('max_length', 200)} characters")
            out[name] = v
        elif t == "date":
            if not isinstance(v, str) or len(v) != 10:
                raise ValueError(f"variable {name}: a YYYY-MM-DD date")
            date.fromisoformat(v)
            out[name] = v
        elif t == "int":
            if isinstance(v, bool) or not isinstance(v, int) or not spec.get("min", -10**9) <= v <= spec.get("max", 10**9):
                raise ValueError(f"variable {name}: an integer within bounds")
            out[name] = str(v)
        else:
            if v not in spec["values"]:
                raise ValueError(f"variable {name}: one of the declared values")
            out[name] = v
    return out


def render(template_text: str, values: dict) -> str:
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], template_text)


def approval_reasons(doc: dict, version: dict, cq_verified, now: datetime, founder: bool = False,
                     text: Optional[str] = None, placeholders: Optional[int] = None) -> list[dict]:
    """Every precondition that is unmet (complete, no short-circuit). ``cq_verified(cq_id) -> bool``.
    ``founder``: Andre's founder approval (LG-20, ADR 0022), allowed only on a document marked
    ``founder_approval_allowed``; it takes the place of the LG-02 sign-off record, every other precondition stays."""
    out = []
    if version["status"] not in ("draft", "counsel_review"):
        out.append(R.item("VERSION_NOT_APPROVABLE", f"version {version['version']} is {version['status']}"))
    if placeholders is not None:                 # r3 C1: counted by the caller outside the service lock
        count = placeholders
    else:
        count = bracket_placeholders(text, set(version.get("template_variables") or {})) if text is not None else 0
    if count:
        out.append(R.item("UNFILLED_PLACEHOLDER", f"{doc['doc_id']} {version['version']} still holds {count} "
                          "unfilled placeholder(s): fill them and upload again"))
    if founder and not doc.get("founder_approval_allowed"):
        out.append(R.item("FOUNDER_PATH_NOT_ALLOWED", f"{doc['doc_id']} is not a client commercial contract: LG-02 "
                          "applies to it"))
    if doc["counsel_required"] and not founder:
        so = version.get("counsel_signoff")
        if so is None:
            out.append(R.item("COUNSEL_RECORD_MISSING", f"{doc['doc_id']} {version['version']} has no counsel sign-off"
                              " record"))
        elif so.get("doc_sha256") != version["sha256"]:
            out.append(R.item("COUNSEL_RECORD_MISSING", "the counsel sign-off names a different document hash"))
    for cq in doc.get("approval_blocked_by") or []:
        if not cq_verified(cq):
            out.append(R.item("CQ_UNVERIFIED", f"approval of {doc['doc_id']} waits on counsel question {cq}", cq_id=cq))
    have = {c["clause_id"] for c in version.get("clause_ids") or []}
    for c in doc.get("required_clause_ids") or []:
        if c not in have:
            out.append(R.item("ENGAGEMENT_AI_CLAUSE_MISSING", f"{doc['doc_id']} {version['version']} lacks clause {c}",
                              cq_id="CQ-24"))
    return out
