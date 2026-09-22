"""
Loads the Fulfillment department's fixture pool (fixtures/fulfillment_*.json
at repo root) into fulfillment_schema domain models. Explicitly non-live —
same discipline as detection-py/src/fixtures_loader.py: never wire this
into a code path that could present its output as real client data.
"""

from __future__ import annotations

import json
from pathlib import Path

from fulfillment_schema import Appointment, CallEvent, CustomerDossier

# repo root is four levels up: src/ -> fulfillment-py/ -> services/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES_DIR = _REPO_ROOT / "fixtures"


def _strip_notes(records: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k != "_fixture_note"} for r in records]


def load_call_events() -> list[CallEvent]:
    raw = json.loads((_FIXTURES_DIR / "fulfillment_call_events.json").read_text())
    return [CallEvent.model_validate(r) for r in _strip_notes(raw)]


def load_appointments() -> list[Appointment]:
    raw = json.loads((_FIXTURES_DIR / "fulfillment_appointments.json").read_text())
    return [Appointment.model_validate(r) for r in _strip_notes(raw)]


def load_dossiers() -> dict[str, CustomerDossier]:
    raw = json.loads((_FIXTURES_DIR / "fulfillment_customers.json").read_text())
    dossiers = [CustomerDossier.model_validate(r) for r in _strip_notes(raw)]
    return {d.customer_id: d for d in dossiers}
