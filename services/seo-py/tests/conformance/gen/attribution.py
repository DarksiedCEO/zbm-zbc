"""Seeded generators for Farrah's conformance suite (farrah-spec/1, CLARIFICATIONS v1.1 GF-2).

* ``OseiStore``: the FIXTURE of Osei's dataset store (Osei full is not on the base): ``osei.dataset/1`` documents
  shaped by tests/conformance/data/osei_dataset_v1.json, fake ``source_read_recorded`` / ``crm_import_recorded``
  records whose keyed HMAC the service's own vault computes (Osei's W-OS-2 seal field ``dataset``), and optional
  ``osei.reconciliation/1`` / ``osei.panel_freshness/1`` documents. It counts every read.
* builders of analytics (GA4-shaped, GO-2) and CRM (B-OS-19) rows.
* ``truth_case``: a tenant-month whose TRUE channel revenue is known (the attribution oracle, SUITE_PLAN 2.2): the
  generator decides each transaction's channel, then hides some of it the ways first-party data does (unknown
  channel fields, the documented "(other)" row, thresholded rows with a documented share, rows dropped at a cap) and
  keeps the truth. Every value is synthetic; a failure prints the case id and seed, never a value.
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

DATA = Path(__file__).resolve().parents[1] / "data" / "osei_dataset_v1.json"
FROZEN = json.loads(DATA.read_text())
TZ = "America/Los_Angeles"
MONTH = "2026-09"
PRODUCED = "2026-10-05T12:00:00Z"
CENTS = Decimal("0.01")


class OseiStore:
    def __init__(self):
        self.t: dict = {}
        self.reads = 0

    def tenant(self, tid: str) -> dict:
        return self.t.setdefault(tid, {"datasets": [], "records": [], "reconciliation": None, "freshness": None})

    def view(self, tid: str):
        self.reads += 1
        x = self.t.get(tid) or {"datasets": [], "records": [], "reconciliation": None, "freshness": None}
        return SimpleNamespace(datasets=list(x["datasets"]), records=list(x["records"]),
                               reconciliation=x["reconciliation"], freshness=x["freshness"])

    def add(self, svc, doc: dict, tid: str | None = None, recorded: bool = True, **kw) -> dict:
        """``doc`` into tenant ``tid``'s store (default: the tenant the document names), with its record."""
        t = self.tenant(tid or doc["tenant_id"])
        t["datasets"].append(doc)
        if recorded:
            t["records"].append(record(svc, doc, tid=tid, **kw))
        return doc


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def record(svc, doc: dict, kind=None, state: str = "VALID", hmac=None, tid=None) -> dict:
    src = doc["source"]
    tid = tid or doc["tenant_id"]
    mac = hmac if hmac is not None else svc.vault.field_hash(doc["tenant_id"], "dataset", canonical(doc))[1]
    return {"kind": kind or FROZEN["contracts"]["records"]["kinds"][src], "tenant_id": tid,
            "dataset_id": doc["dataset_id"], "read_id": doc["read_id"], "validation_state": state,
            "dataset_hmac": mac, "at": PRODUCED}


def ds_id(rng: random.Random) -> str:
    return "ds-" + "".join(rng.choice("0123456789abcdef") for _ in range(16))


def month_days(month: str = MONTH) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    return first, date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)


def dataset(tid: str, source: str, rows: list, month: str = MONTH, tz: str = TZ, start=None, end=None,
            sampled=False, thresholded=False, other_row_share=None, rows_dropped_cap=0, totals=None, labels=None,
            rng=None, read_id=None, produced_at=PRODUCED, property_tz=None) -> dict:
    rng = rng or random.Random(0)
    first, last = month_days(month)
    parts = {"adapter_version": "fake/1", "allowlist_version": "osei-allowlist/2026-11-13.1",
             "property_settings": {"timezone": property_tz or tz, "currency": "USD"}}
    flags = [f for f, on in (("sampled", sampled), ("thresholded", thresholded),
                             ("other_row_share", other_row_share is not None),
                             ("rows_dropped_cap", bool(rows_dropped_cap))) if on]
    did = ds_id(rng)
    return {"contract": "osei.dataset/1", "tenant_id": tid, "dataset_id": did, "source": source,
            "read_id": read_id or "read-" + did[3:], "window": {"start": (start or first).isoformat(),
                                                                "end": (end or last).isoformat(), "timezone": tz},
            "grain": "day", "surface": {"fingerprint": did[3:], "parts": parts},
            "validation": {"state": "VALID", "reason": None, "counts": {"rows": len(rows)}},
            "completeness": {"sampled": sampled, "thresholded": thresholded, "other_row_share": other_row_share,
                             "rows_dropped_cap": rows_dropped_cap},
            "totals": totals if totals is not None else {}, "rows": rows, "produced_at": produced_at,
            "refresh_tier": "first_party_daily", "evidence_ref": {"run_id": "read-" + did[3:],
                                                                 "contract": "osei.dataset/1", "item_key": "rows"},
            "data_class": "measured", "labels": labels if labels is not None else [f"incomplete:{f}" for f in flags]}


def arow(day, revenue, source="google", medium="organic", group="Organic Search", landing="/") -> dict:
    d = day.strftime("%Y%m%d") if isinstance(day, date) else day
    return {"date": d, "sessionSource": source, "sessionMedium": medium, "sessionDefaultChannelGroup": group,
            "landingPage": landing, "sessions": 1, "purchaseRevenue": revenue}


def crow(day, amount, stage="won", utm_medium=None, utm_source=None, lead_source=None, ref="d") -> dict:
    d = day.isoformat() if isinstance(day, date) else day
    return {"deal_ref": ref, "created_date": None, "closed_date": d if stage == "won" else None, "stage": stage,
            "amount": amount, "currency": "USD", "lead_source": lead_source, "utm_source": utm_source,
            "utm_medium": utm_medium, "utm_campaign": None, "landing_path": None}


def freshness(tid: str, states: dict | None = None, panel: str = "revenue") -> dict:
    states = states or {"analytics": "FRESH", "crm": "FRESH"}
    return {"contract": "osei.panel_freshness/1", "tenant_id": tid, "produced_at": PRODUCED,
            "panels": [{"panel": panel, "state": "FRESH", "freshness_timestamp": PRODUCED,
                        "inputs": [{"producer": p, "contract": "osei.dataset/1", "run_id": "read-1",
                                    "produced_at": PRODUCED, "tier": "first_party", "state": s}
                                   for p, s in states.items()]}]}


def reconciliation(tid: str, items: list) -> dict:
    return {"contract": "osei.reconciliation/1", "tenant_id": tid, "produced_at": PRODUCED, "items": items}


def rc4(basis_doc: dict, other_doc: dict, difference: str, month: str = MONTH, result: str = "COMPARED") -> dict:
    first, last = month_days(month)
    return {"rule": "RC-4", "datasets": [basis_doc["dataset_id"], other_doc["dataset_id"]],
            "window": {"start": first.isoformat(), "end": last.isoformat(), "timezone": basis_doc["window"]["timezone"]},
            "values": {"crm_won_revenue": "0", "analytics_revenue": "0", "difference": difference}
            if not result.startswith("NOT_COMPARABLE") else {},
            "result": result, "label": "measured",
            "evidence_ref": {"run_id": "rec-1", "contract": "osei.reconciliation/1", "item_key": "RC-4"}}


def money(rng: random.Random, lo: int = 5, hi: int = 900) -> str:
    return f"{rng.randint(lo, hi)}.{rng.randint(0, 99):02d}"
