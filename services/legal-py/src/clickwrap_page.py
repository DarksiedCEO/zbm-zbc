"""
The client acceptance page's words (ADR 0028, AEGIS M-5): the founder-approved clickwrap text v1, Parts B and C,
character for character, with its values filled from the documents a link binds and the link's display block.

Legal renders the page as a list of blocks; the client-portal service turns the blocks into HTML and shows nothing
else, and Legal checks that the page text the portal submits is exactly ``page_text(blocks)`` for the link's current
state. So the screen evidence recorded with an acceptance is text Legal rendered, not text the portal says it showed.

Standard library only (the portal's tests load this file by path to build realistic blocks).

A block: ``{"k": kind, "text": <its line in the page text>, ...}``. Kinds: ``title``, ``p``, ``doc_heading`` (``n``),
``meta`` (``n``), ``links`` (``n``, ``open_label``), ``doc_text`` (``n``), ``check`` (``n``, ``label``), ``field``
(``id``, ``label``, ``hint``), ``actions``. The page text is every block's ``text`` joined by a blank line, plus a
final newline.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

CONTACT = "legalnotice@zbestmedia.com"
FIELDS = (("full_name", "Your full name", None),
          ("title", "Your title", "(for example, Owner, CEO, Managing Member)"),
          ("company", "Company you are signing for", None))


def msa_label() -> str:
    return "Z Best Media Master Services Agreement"


def sow_label(display: dict) -> str:
    return f"Statement of Work {display['sow_number']}: {display['sow_title']}"


def doc_label(doc_id: str, display: dict) -> str:
    return msa_label() if doc_id == "client_msa" else sow_label(display)


def long_date(ts: str, tz: str) -> str:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
    return f"{dt:%B} {dt.day}, {dt.year}"


def blocks(page: str, display: dict, documents: list[dict], msa_acceptance: Optional[dict],
           tz: str = "America/Los_Angeles") -> list[dict]:
    """``documents``: [{doc_id, version, doc_sha256, text}] (the MSA first). ``page``: first_engagement or new_sow."""
    name = display["client_legal_name"]
    first = page == "first_engagement"
    msa = next((d for d in documents if d["doc_id"] == "client_msa"), None)
    sow = next((d for d in documents if d["doc_id"] != "client_msa"), None)
    out = [{"k": "title", "text": "Review and accept your agreement with Z Best Media" if first
            else "Review and accept your new Statement of Work"}]
    if first:
        out.append({"k": "p", "text": f"Before we start, please read and accept two documents for {name}. The Master "
                    "Services Agreement sets the ground rules for working together. The Statement of Work describes "
                    "this project, its price and its timeline. Each document is shown in full below, and you can "
                    "download a copy of each before you accept."})
        out.append({"k": "p", "text": "Accepting on this page is a legal signature. It has the same effect as signing "
                    f"on paper. Only a person authorized to sign for {name} should accept."})
    else:
        out.append({"k": "p", "text": "This Statement of Work is made under the Master Services Agreement, version "
                    f"{msa_acceptance['version']}, that {name} accepted on "
                    f"{long_date(msa_acceptance['accepted_at'], tz)}."})
    shown = ([("msa", msa)] if first else []) + [("sow", sow)]
    for n, (kind, doc) in enumerate(shown, start=1):
        label = msa_label() if kind == "msa" else sow_label(display)
        open_label = "Open the full agreement" if kind == "msa" else "Open the full Statement of Work"
        if kind == "msa":
            check = (f"I have read the Z Best Media Master Services Agreement, version {doc['version']}, and I agree "
                     f"to it on behalf of {name}.")
        else:
            check = (f"I have read Statement of Work {display['sow_number']}, version {doc['version']}, including its "
                     f"fees, and I agree to it on behalf of {name}.")
        out += [{"k": "doc_heading", "n": n, "text": label},
                {"k": "meta", "n": n, "text": f"Version {doc['version']} · SHA-256 fingerprint {doc['doc_sha256']}"},
                {"k": "links", "n": n, "open_label": open_label, "text": f"{open_label} · Download a copy"},
                {"k": "doc_text", "n": n, "text": doc["text"]},
                {"k": "check", "n": n, "label": check, "text": f"☐ {check}"}]
    for fid, label, hint in FIELDS:
        out.append({"k": "field", "id": fid, "label": label, "hint": hint,
                    "text": f"{label}{(' ' + hint) if hint else ''}"})
    out.append({"k": "p", "text": 'By selecting "Accept and sign", I confirm that I am the person named above and '
                f"that I am authorized to bind {name}."})
    out.append({"k": "p", "text": 'By selecting "Accept and sign", you agree to do business with Z Best Media '
                "electronically, including receiving these documents, notices, invoices and reports by email at "
                f"{display['notice_email']}. We will record your name, title, the time, the documents' fingerprints, "
                "and your IP address and browser as evidence of this acceptance, and we will email you a copy of "
                f"everything you accept. You can ask us for another copy at any time at {CONTACT}."})
    out.append({"k": "actions", "text": "Accept and sign · Not now"})
    return out


def page_text(bl: list[dict]) -> str:
    return "\n\n".join(b["text"] for b in bl) + "\n"


def norm_company(s: str) -> str:
    """Case, spaces and punctuation ignored (``Acme, L.L.C.`` == ``ACME LLC``), after NFKC."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", s).casefold() if ch.isalnum())


INVISIBLE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp", "Co", "Cn"})


def has_invisible(s: str) -> bool:
    """AEGIS L-1: bidi controls, zero-width and other invisible or control characters (after NFKC)."""
    return any(unicodedata.category(ch) in INVISIBLE for ch in unicodedata.normalize("NFKC", s))
