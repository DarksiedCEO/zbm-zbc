"""
Server-rendered HTML for the client acceptance page (ADR 0028). No script, no third-party resource, no cookie; one
inline stylesheet allowed by its hash in the Content-Security-Policy. Every value is HTML-escaped.

The words are the founder-approved clickwrap text v1 (scratchpad legal/clickwrap-acceptance-text-v1.md, Parts B, C
and D), character for character, with its ``{{values}}`` filled. Two changes of behaviour, not of words (ADR 0028):
the documents are shown in full inline (no scroll panel, so no script is needed to unlock a box: the client passes the
whole text of each document before reaching its box), and the server, not a disabled button, refuses a submission
with a box unticked or a field empty. The brief adds one field the text does not have: the company the signatory
signs for, which must match the client's legal name on the documents.

Every page builds its HTML and its plain text from the same blocks. The acceptance page's plain text (every value
filled in, each document's exact registered text included) is the "screen evidence" of clickwrap Part E: its
SHA-256 is signed into the form and recorded with the acceptance, and the text itself is stored by Legal.
"""

from __future__ import annotations

import base64
import hashlib
import html
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from legal_client import PortalView

CONTACT = "legalnotice@zbestmedia.com"

CSS = (
    ":root{color-scheme:light dark;--bg:#ffffff;--fg:#1b1b1b;--muted:#4a4a4a;--line:#c8c8c8;--panel:#f6f6f4;"
    "--link:#0b57d0;--err:#b3261e;--focus:#1b1b1b}"
    "@media (prefers-color-scheme:dark){:root{--bg:#121212;--fg:#ececec;--muted:#c4c4c4;--line:#555;"
    "--panel:#1d1d1d;--link:#8ab4f8;--err:#ffb4ab;--focus:#ececec}}"
    "*{box-sizing:border-box}"
    "body{margin:0;background:var(--bg);color:var(--fg);font:1.0625rem/1.6 system-ui,-apple-system,'Segoe UI',"
    "Roboto,sans-serif}"
    "main{max-width:46rem;margin:0 auto;padding:1.5rem 1rem 4rem}"
    "h1{font-size:1.75rem;line-height:1.25;margin:0 0 1rem}h2{font-size:1.25rem;margin:2rem 0 .25rem}"
    "a{color:var(--link)}a:focus,button:focus,input:focus,summary:focus{outline:3px solid var(--focus);"
    "outline-offset:2px}"
    ".skip{position:absolute;left:-999px}.skip:focus{left:1rem;top:1rem;background:var(--bg);padding:.5rem}"
    ".meta{color:var(--muted);margin:0 0 .5rem;overflow-wrap:anywhere}"
    ".doc{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:1rem;margin:.5rem 0 1rem}"
    ".doc pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}"
    ".check{display:flex;gap:.75rem;align-items:flex-start;margin:1rem 0 1.5rem}"
    ".check input{width:1.5rem;height:1.5rem;flex:none;margin-top:.15rem}"
    ".field{margin:1.25rem 0}.field label{display:block;font-weight:600}"
    ".hint{display:block;color:var(--muted)}"
    ".field input{width:100%;max-width:30rem;font:inherit;padding:.5rem;border:2px solid var(--fg);"
    "border-radius:4px;background:var(--bg);color:var(--fg)}"
    ".error{display:block;color:var(--err);font-weight:600}"
    ".errors{border:3px solid var(--err);padding:1rem;margin:0 0 1.5rem}.errors h2{margin-top:0}"
    "button{font:inherit;font-weight:600;padding:.75rem 1.5rem;border:0;border-radius:4px;background:var(--fg);"
    "color:var(--bg);cursor:pointer;margin-right:1rem}"
    "details{margin:1rem 0}summary{cursor:pointer}"
)
CSS_HASH = "sha256-" + base64.b64encode(hashlib.sha256(CSS.encode("utf-8")).digest()).decode("ascii")


def e(text: str) -> str:
    return html.escape(text, quote=True)


@dataclass
class Page:
    """HTML and plain text built from the same blocks."""
    title: str
    html_parts: list[str] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)

    def add(self, html_part: str, text_part: Optional[str]) -> None:
        self.html_parts.append(html_part)
        if text_part is not None:
            self.text_parts.append(text_part)

    def h1(self, text: str) -> None:
        self.add(f"<h1>{e(text)}</h1>", text)

    def p(self, text: str, cls: str = "") -> None:
        self.add(f'<p{f" class={chr(34)}{cls}{chr(34)}" if cls else ""}>{e(text)}</p>', text)

    @property
    def text(self) -> str:
        return "\n\n".join(self.text_parts) + "\n"

    def html(self) -> str:
        return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                "<meta name=\"robots\" content=\"noindex, nofollow\">"
                f"<title>{e(self.title)}</title><style>{CSS}</style></head><body>"
                + "".join(self.html_parts) + "</body></html>\n")


# --- the acceptance page -----------------------------------------------------------------------------------------

@dataclass
class FormState:
    full_name: str = ""
    title: str = ""
    company: str = ""
    errors: dict = field(default_factory=dict)       # field id -> message (ordered as on the page)


def doc_label(view: PortalView, kind: str) -> str:
    if kind == "msa":
        return "Z Best Media Master Services Agreement"
    return f"Statement of Work {view.display.sow_number}: {view.display.sow_title}"


def acceptance_page(view: PortalView, token: str, issued: int, form_sig: str, page_sha256: Optional[str],
                    state: Optional[FormState] = None, display_tz: str = "America/Los_Angeles") -> Page:
    """The page for this link. ``page_sha256`` None: compute the page text only (the caller signs its hash)."""
    state = state or FormState()
    d = view.display
    name = d.client_legal_name
    msa, sow = view.doc("msa"), view.doc("sow")
    first = view.page == "first_engagement"
    pg = Page("Review and accept your agreement with Z Best Media" if first
              else "Review and accept your new Statement of Work")
    pg.add('<a class="skip" href="#accept-form">Skip to the acceptance form</a><main>', None)
    pg.h1(pg.title)
    if state.errors:
        items = "".join(f'<li><a href="#{e(k)}">{e(v)}</a></li>' for k, v in state.errors.items())
        pg.add('<div class="errors" role="alert" aria-labelledby="errors-title"><h2 id="errors-title">'
               f'There is a problem</h2><ul>{items}</ul></div>', None)
    if first:
        pg.p(f"Before we start, please read and accept two documents for {name}. The Master Services Agreement sets "
             "the ground rules for working together. The Statement of Work describes this project, its price and its "
             "timeline. Each document is shown in full below, and you can download a copy of each before you accept.")
        pg.p("Accepting on this page is a legal signature. It has the same effect as signing on paper. Only a person "
             f"authorized to sign for {name} should accept.")
    else:
        when = local_date(view.msa_accepted_at or "", display_tz)
        pg.p(f"This Statement of Work is made under the Master Services Agreement, version "
             f"{view.msa_accepted_version}, that {name} accepted on {when}.")
    pg.add(f'<form id="accept-form" method="post" action="/accept/{e(token)}" accept-charset="utf-8">'
           f'<input type="hidden" name="issued" value="{issued}">'
           f'<input type="hidden" name="form_sig" value="{e(form_sig)}">'
           f'<input type="hidden" name="page_sha256" value="{e(page_sha256 or "")}">', None)
    blocks = ([("msa", msa, 1)] if first else []) + [("sow", sow, 2 if first else 1)]
    for kind, doc, n in blocks:
        label = doc_label(view, kind)
        hid = f"doc{n}-title"
        pg.add(f'<section aria-labelledby="{hid}"><h2 id="{hid}">{e(label)}</h2>', label)
        meta = f"Version {doc.version} · SHA-256 fingerprint {doc.doc_sha256}"
        pg.p(meta, "meta")
        open_text = "Open the full agreement" if kind == "msa" else "Open the full Statement of Work"
        pg.add(f'<p><a href="/accept/{e(token)}/document/{n}">{e(open_text)}</a> · '
               f'<a href="/accept/{e(token)}/document/{n}/download">Download a copy</a></p>',
               f"{open_text} · Download a copy")
        pg.add(f'<div class="doc" role="region" aria-label="Full text: {e(label)}" tabindex="0"><pre>'
               f"{e(doc.text)}</pre></div>", doc.text)
        box = f"accept_doc_{n}"
        if kind == "msa":
            check = (f"I have read the Z Best Media Master Services Agreement, version {doc.version}, and I agree to "
                     f"it on behalf of {name}.")
        else:
            check = (f"I have read Statement of Work {d.sow_number}, version {doc.version}, including its fees, and I "
                     f"agree to it on behalf of {name}.")
        err = state.errors.get(box)
        aria = f' aria-invalid="true" aria-describedby="{box}-error"' if err else ""
        pg.add(f'<div class="check"><input type="checkbox" id="{box}" name="{box}" value="yes" required{aria}>'
               f'<label for="{box}">{e(check)}'
               + (f'<span class="error" id="{box}-error">{e(err)}</span>' if err else "")
               + "</label></div></section>", f"☐ {check}")
    for fid, label, hint, auto, maxlen in (
            ("full_name", "Your full name", None, "name", 120),
            ("title", "Your title", "(for example, Owner, CEO, Managing Member)", "organization-title", 120),
            ("company", "Company you are signing for", None, "organization", 200)):
        err = state.errors.get(fid)
        described = " ".join(x for x in ((f"{fid}-hint" if hint else ""), (f"{fid}-error" if err else "")) if x)
        attrs = (f' aria-describedby="{described}"' if described else "") + (' aria-invalid="true"' if err else "")
        value = getattr(state, fid)
        pg.add(f'<div class="field"><label for="{fid}">{e(label)}</label>'
               + (f'<span class="hint" id="{fid}-hint">{e(hint)}</span>' if hint else "")
               + (f'<span class="error" id="{fid}-error">{e(err)}</span>' if err else "")
               + f'<input type="text" id="{fid}" name="{fid}" autocomplete="{auto}" maxlength="{maxlen}" required'
               f' spellcheck="false" value="{e(value)}"{attrs}></div>',
               f"{label}{(' ' + hint) if hint else ''}")
    pg.p(f'By selecting "Accept and sign", I confirm that I am the person named above and that I am authorized to '
         f"bind {name}.")
    pg.p('By selecting "Accept and sign", you agree to do business with Z Best Media electronically, including '
         f"receiving these documents, notices, invoices and reports by email at {d.notice_email}. We will record your "
         "name, title, the time, the documents' fingerprints, and your IP address and browser as evidence of this "
         "acceptance, and we will email you a copy of everything you accept. You can ask us for another copy at any "
         f"time at {CONTACT}.")
    pg.add('<p><button type="submit">Accept and sign</button><a href="/accept/not-now">Not now</a></p></form></main>',
           "Accept and sign · Not now")
    return pg


def local_date(ts: str, tz: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
    except ValueError:
        return ts
    return f"{dt:%B} {dt.day}, {dt.year}"


def local_time(ts: str, tz: str) -> str:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
    hour = dt.hour % 12 or 12
    return f"{dt:%B} {dt.day}, {dt.year} at {hour}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


# --- other pages -------------------------------------------------------------------------------------------------

def message_page(title: str, *paragraphs: str, link: Optional[tuple[str, str]] = None) -> Page:
    pg = Page(title)
    pg.add("<main>", None)
    pg.h1(title)
    for p in paragraphs:
        pg.p(p)
    if link:
        pg.add(f'<p><a href="{e(link[0])}">{e(link[1])}</a></p>', link[1])
    pg.add("</main>", None)
    return pg


NOT_FOUND = message_page(
    "This link cannot be used",
    "It may have expired, been used already, or been replaced by a newer link. If you need a new link, write to "
    f"{CONTACT}.")
RATE_LIMITED = message_page("Too many requests", "Please wait a few minutes, then open your link again.")
UNAVAILABLE = message_page("We cannot show your documents right now",
                           "Nothing was recorded. Please try again in a few minutes.")
NOT_NOW = message_page("Nothing was recorded",
                       "Your link stays valid until it expires. You can close this page.")
FORM_REFUSED = message_page("This form could not be accepted",
                            "Nothing was recorded. Open your acceptance link again and submit the form from that page.")
TOO_LARGE = message_page("This form could not be accepted", "Nothing was recorded. The form was too large.")


def changed_page(token: str) -> Page:
    return message_page("The documents changed after you opened this page",
                        "Nothing was recorded. Open the page again to review the current documents.",
                        link=(f"/accept/{token}", "Open the page again"))


def confirmation_page(view: PortalView, accepted: tuple, full_name: str, title: str, display_tz: str,
                      emailed: bool) -> Page:
    pg = Page("Thank you. Your acceptance is recorded.")
    pg.add("<main>", None)
    pg.h1(pg.title)
    pg.p(f"This page confirms what you accepted for {view.display.client_legal_name}.")
    by_id = {d.doc_id: d for d in view.documents}
    for a in accepted:
        kind = "msa" if a.doc_id == "client_msa" else "sow"
        label = doc_label(view, kind)
        pg.add(f"<h2>{e(label)}, version {e(a.version)}</h2>", f"{label}, version {a.version}")
        pg.p(f"SHA-256 fingerprint: {a.doc_sha256}", "meta")
        pg.p(f"Accepted: {local_time(a.accepted_at, display_tz)} (Pacific Time) by {full_name}, {title}")
        pg.p(f"Acceptance reference: {a.acceptance_id}")
        doc = by_id[a.doc_id]
        pg.add(f"<details><summary>Full text of {e(label)}</summary><div class=\"doc\"><pre>{e(doc.text)}</pre></div>"
               "</details>", None)
    if emailed:
        pg.p(f"We have emailed a copy of each document to {view.display.notice_email}.")
    else:
        pg.p(f"Z Best Media will email a copy of each document to {view.display.notice_email}. You can ask for "
             f"another copy at any time at {CONTACT}.")
    pg.add("</main>", None)
    return pg


def confirmation_email(view: PortalView, accepted: tuple, full_name: str, title: str, display_tz: str,
                       postal_line: str, next_step: str) -> tuple[str, str]:
    """Clickwrap text v1, Part D: (subject, body). ``postal_line`` is ZBM's entity name and address (founder facts
    from configuration, never hard-coded); ``next_step`` the sentence for this engagement."""
    first = full_name.split()[0] if full_name.split() else full_name
    lines = [f"Hi {first},", "",
             f"Thank you. This email confirms what you accepted for {view.display.client_legal_name}.", ""]
    for a in accepted:
        label = doc_label(view, "msa" if a.doc_id == "client_msa" else "sow")
        lines += [f"- {label}, version {a.version}", f"  SHA-256 fingerprint: {a.doc_sha256}",
                  f"  Accepted: {local_time(a.accepted_at, display_tz)} (Pacific Time) by {full_name}, {title}",
                  f"  Acceptance reference: {a.acceptance_id}"]
    lines += ["", "A copy of each document is attached as a text file. The fingerprint lets anyone check that the "
              "attached copy is exactly the version you accepted: the SHA-256 of the attached file will match the "
              "fingerprint above.", "", f"Next step: {next_step}", "",
              f"If anything here looks wrong, reply to this email or write to {CONTACT}.", "", "Z Best Media",
              postal_line]
    return "Your agreement with Z Best Media: copy for your records", "\n".join(lines) + "\n"


def document_text_headers(n: int, doc, download: bool) -> list[tuple[bytes, bytes]]:
    name = f"{doc.doc_id}-v{doc.version}.md"
    disp = f'{"attachment" if download else "inline"}; filename="{name}"'
    return [(b"content-type", b"text/plain; charset=utf-8"), (b"content-disposition", disp.encode("ascii"))]
