"""The 8 vertical packs (PROPOSED v0: the agent-spec draft names "8 vertical packs" without listing them; Andre
confirms or swaps this list before the freeze — e.g. wineries/clubs for one of these).

Each pack is the fixture version of what a client would give the front desk: services and fixture prices, hours,
booking rules, hand-off rules, and the safety profile. Prices here are fixture numbers for testing "never invent a
price"; they are not ZBM's or anyone's real prices.
"""

from __future__ import annotations

VERTICALS: dict[str, dict] = {
    "med_spa": {
        "label": "Med spa",
        "health_sensitive": True,
        "emergency_types": ["medical"],
        "services": {"botox_consult": "150", "hydrafacial": "199", "laser_hair_removal_session": "249"},
        "hours": {"mon_fri": ["09:00", "19:00"], "sat": ["10:00", "16:00"], "sun": None},
        "booking": "consult_first",
        "handoff_reasons": ["treatment_question", "complaint", "refund"],
        "reason_categories": ["book_consult", "treatment_question", "pricing", "reschedule"],
    },
    "dental": {
        "label": "Dental clinic",
        "health_sensitive": True,
        "emergency_types": ["medical"],
        "services": {"cleaning": "129", "new_patient_exam": "89", "whitening": "349"},
        "hours": {"mon_fri": ["08:00", "17:00"], "sat": ["09:00", "13:00"], "sun": None},
        "booking": "direct",
        "handoff_reasons": ["treatment_question", "insurance_question", "complaint"],
        "reason_categories": ["book_appointment", "treatment_question", "insurance_question", "reschedule"],
    },
    "personal_injury_law": {
        "label": "Personal-injury law firm",
        "health_sensitive": True,
        "emergency_types": ["medical", "violence"],
        "services": {"free_case_review": "0"},
        "hours": {"mon_fri": ["08:00", "18:00"], "sat": None, "sun": None},
        "booking": "intake_then_attorney_callback",
        "handoff_reasons": ["new_case_intake", "existing_client", "legal_advice_request"],
        "reason_categories": ["new_case", "existing_case_update", "legal_question"],
    },
    "hvac": {
        "label": "HVAC contractor",
        "health_sensitive": False,
        "emergency_types": ["gas_leak", "fire"],
        "services": {"diagnostic_visit": "89", "tune_up": "129", "after_hours_call_fee": "149"},
        "hours": {"mon_fri": ["07:00", "18:00"], "sat": ["08:00", "14:00"], "sun": None},
        "booking": "direct_with_dispatch",
        "handoff_reasons": ["no_heat_no_cool", "quote_request", "complaint"],
        "reason_categories": ["repair", "maintenance", "quote", "reschedule"],
    },
    "plumbing": {
        "label": "Plumbing contractor",
        "health_sensitive": False,
        "emergency_types": ["gas_leak", "fire"],
        "services": {"service_call": "99", "drain_cleaning": "179", "water_heater_flush": "149"},
        "hours": {"mon_fri": ["07:00", "17:00"], "sat": ["08:00", "12:00"], "sun": None},
        "booking": "direct_with_dispatch",
        "handoff_reasons": ["active_leak", "quote_request", "complaint"],
        "reason_categories": ["repair", "drain", "quote", "reschedule"],
    },
    "auto_dealer_service": {
        "label": "Auto dealer service lane",
        "health_sensitive": False,
        "emergency_types": ["fire", "medical"],
        "services": {"oil_change": "79", "tire_rotation": "39", "multipoint_inspection": "0"},
        "hours": {"mon_fri": ["07:00", "18:00"], "sat": ["08:00", "16:00"], "sun": None},
        "booking": "direct",
        "handoff_reasons": ["sales_inquiry", "warranty_question", "complaint"],
        "reason_categories": ["service_appointment", "status_check", "sales_inquiry", "reschedule"],
    },
    "restaurant": {
        "label": "Restaurant",
        "health_sensitive": False,
        "emergency_types": ["fire", "medical", "violence"],
        "services": {"private_event_deposit": "250"},
        "hours": {"mon_fri": ["11:00", "22:00"], "sat": ["10:00", "23:00"], "sun": ["10:00", "21:00"]},
        "booking": "reservation",
        "handoff_reasons": ["large_party", "catering", "complaint"],
        "reason_categories": ["reservation", "hours", "catering", "complaint"],
    },
    "insurance_agency": {
        "label": "Insurance agency",
        "health_sensitive": False,
        "emergency_types": ["medical", "fire"],
        "services": {"quote_review": "0"},
        "hours": {"mon_fri": ["09:00", "17:00"], "sat": None, "sun": None},
        "booking": "agent_callback",
        "handoff_reasons": ["claim_report", "policy_change", "complaint"],
        "reason_categories": ["new_quote", "claim", "policy_change", "billing"],
    },
}

VERTICAL_IDS: tuple[str, ...] = tuple(VERTICALS)

# Business-name parts. Deliberately generic and labelled; combined with a number so they can't be a real business.
NAME_WORDS = ("Summit", "Harbor", "Desert Sky", "Cascade", "Pacific", "Sierra", "Golden Coast", "Juniper",
              "Redwood", "Canyon", "Bayview", "Silverline")
NAME_SUFFIX = {
    "med_spa": "Aesthetics", "dental": "Dental", "personal_injury_law": "Injury Law",
    "hvac": "Heating & Air", "plumbing": "Plumbing", "auto_dealer_service": "Auto Service",
    "restaurant": "Kitchen", "insurance_agency": "Insurance",
}
