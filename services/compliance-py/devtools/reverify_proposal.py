"""Build a re-verification proposal for one register row from a page Andre saved (ADR 0024; docs/deploy/railway.md,
"Compliance (38): the hard gate"). Standard library only; nothing is sent anywhere: it prints the JSON body of
``POST /compliance/v1/register/proposals``, which Andre posts with his own token and then approves.

    python3 devtools/reverify_proposal.py --row row.json --page saved-page.html --excerpt "the sentence that still holds"

``row.json`` is the answer of ``GET /compliance/v1/register/<id>`` (its ``row``). ``saved-page.html`` is the row's
``source_url`` page as Andre saved it today, after reading it and confirming the obligation still holds. The proposal
keeps every field of the row except ``verified_at`` (today, the page's date) and ``status`` (verified); the service
recomputes ``expires_at``. Re-verifying never changes what a rule requires: a changed obligation is an ``amend``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone

ROW_FIELDS = ("id", "jurisdiction", "domain", "title", "obligation", "source_url", "additional_sources", "source_kind",
              "source_quality", "effective_date", "effective_note", "verified_at", "expires_at", "status",
              "owner_intelligence", "gates", "applies_when", "check", "penalty_note", "counsel_flag", "report_ref",
              "parameters")


def build(row: dict, page: bytes, excerpt: str, now: datetime) -> dict:
    if row.get("source_kind") in ("house-rule", "counsel-question") or not row.get("source_url"):
        raise SystemExit("this row is not re-verified from a page (house rule, counsel question or no source URL)")
    if not excerpt.strip() or len(excerpt) > 2000:
        raise SystemExit("--excerpt: the quoted sentence, 1..2000 characters")
    text = re.sub(r"\s+", " ", re.sub(rb"<[^>]*>", b" ", page).decode("utf-8", "replace")).strip()
    fetched = now.astimezone(timezone.utc).replace(microsecond=0)
    new = {k: row[k] for k in ROW_FIELDS}
    new.update(verified_at=fetched.date().isoformat(), status="verified", expires_at=None)
    return {"request_id": f"reverify-{row['id'].lower()}-{uuid.uuid4().hex[:12]}", "kind": "reverify",
            "target_id": row["id"], "proposed_row": new,
            "evidence": {"source_url": row["source_url"], "fetched_at": fetched.isoformat().replace("+00:00", "Z"),
                         "snapshot_sha256": hashlib.sha256(page).hexdigest(),
                         "normalized_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                         "quoted_excerpt": excerpt, "doc_number": None}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--row", required=True, help="the JSON answer of GET /compliance/v1/register/<id>")
    ap.add_argument("--page", required=True, help="the row's source page, saved today")
    ap.add_argument("--excerpt", required=True, help="the sentence from the page that shows the obligation holds")
    a = ap.parse_args(argv)
    with open(a.row, encoding="utf-8") as fh:
        doc = json.load(fh)
    with open(a.page, "rb") as fh:
        page = fh.read()
    json.dump(build(doc.get("row", doc), page, a.excerpt, datetime.now(timezone.utc)), sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
