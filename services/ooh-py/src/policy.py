"""
The OOH / DOOH department constitution as data (ADR 0019, wave OOH-0). Everything here is a closed vocabulary or a
fixed rule the service enforces; nothing here is configurable at runtime.

Sources: the founder decisions of Oct 16 2026 and the OOH/DOOH master blueprint v1 (Oct 7 2026) sections named
below. The blueprint was not available to this build as a file; the section contents used are the ones restated
in the build brief and in the AEGIS round 1 direction (the section 4 state names, the section 40 capability names
and the section 42 initial cell). What goes beyond the blueprint is marked here and in ADR 0019.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# ---------------------------------------------------------------------------------------------- authority (Oct 16)
# Andre alone holds these roles at launch. Agents prepare work and never commit.
ANDRE_ROLES = ("DIRECTOR_PNL", "BUYER", "CONTRACT_SIGNER")
SIGNER = "andre"
AUTHORITY_SCOPES = ("OOH_PURCHASE",)

# ---------------------------------------------------------------------------------------------- section 10
PRINCIPAL_AGENT_STATES = ("PRINCIPAL", "AGENT", "UNDETERMINED")
PRINCIPAL_AGENT_DEFAULT = "UNDETERMINED"

# ---------------------------------------------------------------------------------------------- section 11
FUNDING_POLICY = "CLIENT_PREPAY_REQUIRED"     # the default and, at launch, the only policy: 100% prepaid
FUNDING_STATES = ("NONE", "PARTIAL", "CLEARED_FULL")
# Who may mark client funds verified-cleared. Finance (31) is the intended source; until its client exists here only
# Andre's gate can, with an evidence reference recorded (Oct 16). The Finance source is listed so the rule is visible;
# no code path can produce it in OOH-1.
FUNDS_SOURCES = ("ANDRE_GATE", "FINANCE_31")
FUNDS_SOURCES_CONNECTED = ("ANDRE_GATE",)
# Vendor-related client money is ACH only, never card (founder, Oct 5); checks are recorded by hand.
FUNDS_METHODS = ("ach", "check")

# ---------------------------------------------------------------------------------------------- section 19
PRICE_TYPES = ("VENDOR_COST_ESTIMATE", "VENDOR_QUOTE", "NEGOTIATION_TARGET", "APPROVED_BUY_LIMIT", "CLIENT_SELL_PRICE",
               "ACTUAL_RECONCILED_COST")
PRICE_TYPES_ANDRE = ("APPROVED_BUY_LIMIT", "CLIENT_SELL_PRICE")      # P&L decisions
PRICE_TYPES_NOT_IN_WAVE = ("ACTUAL_RECONCILED_COST",)                 # reconciliation is a later wave

# ---------------------------------------------------------------------------------------------- section 54
EVIDENCE_CLASSES = ("OBSERVED", "VENDOR_ASSERTED", "ESTIMATED", "MODELED", "ATTRIBUTED", "INCREMENTAL",
                    "CAUSALLY_SUPPORTED", "FINANCIALLY_VERIFIED", "UNKNOWN")
# The class a record may be CREATED with, per kind. A record's class is fixed when it is recorded and no route can
# change it ("never silently upgraded"); the first entry is the default.
EVIDENCE_ALLOWED = {
    "VENDOR_COST_ESTIMATE": ("ESTIMATED", "MODELED", "UNKNOWN"),
    "VENDOR_QUOTE": ("VENDOR_ASSERTED",),
    "NEGOTIATION_TARGET": ("MODELED", "ESTIMATED"),
    "APPROVED_BUY_LIMIT": (None,),          # a decision, not a measurement
    "CLIENT_SELL_PRICE": (None,),           # a decision, not a measurement
    "hold_confirmation": ("VENDOR_ASSERTED",),   # Andre confirms the vendor's assertion; he did not observe the hold
    "hold_evidence": ("VENDOR_ASSERTED",),
    "funds_andre_gate": ("OBSERVED",),      # Andre saw cleared funds; never FINANCIALLY_VERIFIED (that is Finance's)
    "order_outcome": ("VENDOR_ASSERTED",),
}

# ---------------------------------------------------------------------------------------------- section 9
# The longest flight a unit may carry (AEGIS R3: a flight of 0001-01-01..9999-12-31 must not hold a face forever).
FLIGHT_MAX_DAYS = 366
# Why Andre may register a vendor whose normalised name collides with another's (AEGIS R3): closed set.
VENDOR_DISTINCT_REASONS = ("SEPARATE_LEGAL_ENTITY", "SEPARATE_OPERATOR_VERIFIED", "NAME_COINCIDENCE_VERIFIED")
SUGGESTED_MARKUP_PCT = "15.00"      # a suggestion shown to Andre, never applied by the service (Oct 16)
PRICING_METHODS = ("DISCLOSED_MARKUP",)
PRESENTATIONS = ("COST_AND_FEE_BROKEN_OUT", "BLENDED_WITH_MARKUP_DISCLOSED")
CURRENCIES = ("USD",)

# ---------------------------------------------------------------------------------------------- section 48
# Decision outputs an agent may PROPOSE (OOH-1 builds no agent). None of them is an authorization: the only path to a
# purchase is Andre's authorization gate. No model self-authorizes BUY.
DECISIONS = ("RECOMMEND_BUY", "NEGOTIATE", "REQUEST_HOLD", "WAIT", "DO_NOT_BUY", "INSUFFICIENT_EVIDENCE")

# ---------------------------------------------------------------------------------------------- section 15
AVAILABILITY_STATES = ("IDENTIFIED", "VERIFIED", "AVAILABILITY_REQUESTED", "QUOTED", "HOLD_REQUESTED", "HOLD_CONFIRMED",
                       "BOOKING_PENDING", "BOOKING_CONFIRMED", "UNAVAILABLE", "EXPIRED", "WITHDRAWN", "DISPUTED")
_AV_EXIT = ("UNAVAILABLE", "EXPIRED", "WITHDRAWN", "DISPUTED")
AVAILABILITY_ALLOWED = {
    "IDENTIFIED": ("VERIFIED", "WITHDRAWN", "UNAVAILABLE"),
    "VERIFIED": ("AVAILABILITY_REQUESTED", "WITHDRAWN", "UNAVAILABLE"),
    "AVAILABILITY_REQUESTED": ("QUOTED",) + _AV_EXIT,
    "QUOTED": ("HOLD_REQUESTED",) + _AV_EXIT,
    "HOLD_REQUESTED": ("HOLD_CONFIRMED", "QUOTED") + _AV_EXIT,      # QUOTED: the vendor declined the hold
    "HOLD_CONFIRMED": ("HOLD_REQUESTED",) + _AV_EXIT,                # HOLD_REQUESTED: an extension asked for
    "BOOKING_PENDING": (),                                           # set and cleared by the order flow only
    "BOOKING_CONFIRMED": ("DISPUTED",),
    "UNAVAILABLE": ("VERIFIED",),
    "EXPIRED": ("VERIFIED",),
    "WITHDRAWN": (),
    "DISPUTED": (),
}
# Reached only through the order flow (never through the availability route): booking is a purchase consequence.
AVAILABILITY_GATED = ("BOOKING_PENDING", "BOOKING_CONFIRMED")
# Reached only through Andre's hold decision (founder decision, Oct 16 round 2; replaces AEGIS L4): an agent attaches
# the vendor's evidence and submits it; only Andre sets HOLD_CONFIRMED, from one named evidence record. The generic
# availability route refuses it for everyone, Andre included.
AVAILABILITY_ANDRE_ONLY = ("HOLD_CONFIRMED",)
# A hold-evidence record's life. PENDING_ANDRE is the only state Andre may decide; anything recorded on the unit after
# the evidence (a move, a new flight, newer evidence) leaves it STALE or SUPERSEDED.
HOLD_EVIDENCE_STATES = ("PENDING_ANDRE", "CONFIRMED_BY_ANDRE", "REJECTED_BY_ANDRE", "SUPERSEDED", "STALE")
HOLD_DECISIONS = ("CONFIRM", "REJECT")
HOLD_REJECT_REASONS = ("EVIDENCE_INSUFFICIENT", "REFERENCE_NOT_VERIFIED", "EXPIRY_WRONG", "WRONG_FACE_OR_FLIGHT",
                       "VENDOR_WITHDREW")

# ---------------------------------------------------------------------------------------------- sections 3 and 4
# The blueprint's section 4 vocabulary, complete (main and exception states), and how this service stores it.
SECTION_4_MAIN = ("INTAKE", "BRIEF_VALIDATED", "PLAN_DRAFTED", "PLAN_APPROVED", "AVAILS_REQUESTED", "QUOTES_VALIDATED",
                  "CLIENT_APPROVED", "FUNDING_REQUIRED", "FUNDS_VERIFIED", "HOLD_REQUESTED", "HOLD_CONFIRMED",
                  "PURCHASE_AUTHORIZED", "BOOKED", "CREATIVE_PENDING", "CREATIVE_ACCEPTED", "PRODUCTION_PENDING", "LIVE",
                  "DELIVERY_REVIEWED", "RECONCILIATION_PENDING", "RECONCILED", "CLOSED")
SECTION_4_EXCEPTION = ("EXPIRED", "WITHDRAWN", "UNAVAILABLE", "CLIENT_HOLD", "VENDOR_HOLD", "CANCELLED",
                       "SUBSTITUTION_PROPOSED", "SUBSTITUTION_REQUIRES_APPROVAL", "PARTIAL_DELIVERY", "UNDERDELIVERY",
                       "OUTAGE", "MAKEGOOD_PENDING", "CREDIT_PENDING", "DISPUTED", "REFUND_PENDING", "FAILED",
                       "BLOCKED")
# HOLD_REQUESTED and HOLD_CONFIRMED are section 4 campaign-level states that this service DERIVES from its units
# (holds are per face, section 15, and may be requested before any money): the campaign's ``hold_state`` is
# HOLD_CONFIRMED when every live unit is HOLD_CONFIRMED with an unexpired hold, HOLD_REQUESTED when at least one live
# unit is HOLD_REQUESTED or HOLD_CONFIRMED but not all are confirmed, and null otherwise. They are never stored as the
# campaign's ``state``; the purchase gate reads the units themselves.
DERIVED_STATES = ("HOLD_REQUESTED", "HOLD_CONFIRMED")
# Added beyond section 4, each for a reason (ADR 0019 decision 9).
BEYOND_SECTION_4 = {
    "PROPOSAL_SENT": "the moment the price leaves ZBM: the client's approval binds to that exact sell price",
    "ORDER_SUBMITTED": "the order is with the vendor and its answer is unknown (section 51): nothing may be re-sent",
    "CHANGE_REQUESTED": "the client changes the brief after a proposal: approvals and authorizations are voided",
}
CAMPAIGN_STATES = ("INTAKE", "BRIEF_VALIDATED", "PLAN_DRAFTED", "PLAN_APPROVED", "AVAILS_REQUESTED", "QUOTES_VALIDATED",
                   "PROPOSAL_SENT", "CLIENT_APPROVED", "FUNDING_REQUIRED", "FUNDS_VERIFIED", "PURCHASE_AUTHORIZED",
                   "ORDER_SUBMITTED", "BOOKED", "CREATIVE_PENDING", "CREATIVE_ACCEPTED", "PRODUCTION_PENDING", "LIVE",
                   "DELIVERY_REVIEWED", "RECONCILIATION_PENDING", "RECONCILED", "CLOSED")
EXCEPTION_STATES = SECTION_4_EXCEPTION + ("CHANGE_REQUESTED",)
ALL_STATES = CAMPAIGN_STATES + EXCEPTION_STATES
# Defined, unreachable, refused STATE_NOT_BUILT on any route: production, delivery, reconciliation and every exception
# state OOH-1 does not handle (OOH-2 and later).
CAMPAIGN_NOT_BUILT = ("CREATIVE_PENDING", "CREATIVE_ACCEPTED", "PRODUCTION_PENDING", "LIVE", "DELIVERY_REVIEWED",
                      "RECONCILIATION_PENDING", "RECONCILED", "CLOSED",
                      "EXPIRED", "WITHDRAWN", "UNAVAILABLE", "SUBSTITUTION_PROPOSED", "SUBSTITUTION_REQUIRES_APPROVAL",
                      "PARTIAL_DELIVERY", "UNDERDELIVERY", "OUTAGE", "MAKEGOOD_PENDING", "CREDIT_PENDING",
                      "REFUND_PENDING", "FAILED", "BLOCKED")
EXCEPTIONS_BUILT = ("CLIENT_HOLD", "VENDOR_HOLD", "DISPUTED", "CANCELLED", "CHANGE_REQUESTED")
TERMINAL = ("CANCELLED", "CLOSED")
HOLD_STATES = ("CLIENT_HOLD", "VENDOR_HOLD")
RESUMABLE = HOLD_STATES + ("DISPUTED",)
_EXITS = HOLD_STATES + ("CHANGE_REQUESTED", "CANCELLED")
CAMPAIGN_ALLOWED = {
    "INTAKE": ("BRIEF_VALIDATED",) + HOLD_STATES + ("CANCELLED",),
    "BRIEF_VALIDATED": ("PLAN_DRAFTED",) + _EXITS,
    "PLAN_DRAFTED": ("PLAN_APPROVED",) + _EXITS,
    "PLAN_APPROVED": ("AVAILS_REQUESTED",) + _EXITS,
    "AVAILS_REQUESTED": ("QUOTES_VALIDATED",) + _EXITS,
    "QUOTES_VALIDATED": ("PROPOSAL_SENT",) + _EXITS,
    "PROPOSAL_SENT": ("CLIENT_APPROVED",) + _EXITS,
    "CLIENT_APPROVED": ("FUNDING_REQUIRED", "FUNDS_VERIFIED") + _EXITS,
    "FUNDING_REQUIRED": ("FUNDS_VERIFIED",) + _EXITS,
    "FUNDS_VERIFIED": ("PURCHASE_AUTHORIZED",) + _EXITS,
    "PURCHASE_AUTHORIZED": ("PURCHASE_AUTHORIZED", "ORDER_SUBMITTED") + _EXITS,
    # an order in flight cannot be held, changed or cancelled here: its outcome is unknown until recorded (section 51)
    "ORDER_SUBMITTED": ("BOOKED", "FUNDS_VERIFIED", "DISPUTED"),
    "BOOKED": ("CREATIVE_PENDING", "DISPUTED"),
    # NOT_BUILT from here on (the table records the intended order; no transition into them is ever allowed)
    "CREATIVE_PENDING": ("CREATIVE_ACCEPTED", "DISPUTED"),
    "CREATIVE_ACCEPTED": ("PRODUCTION_PENDING", "DISPUTED"),
    "PRODUCTION_PENDING": ("LIVE", "DISPUTED"),
    "LIVE": ("DELIVERY_REVIEWED", "DISPUTED"),
    "DELIVERY_REVIEWED": ("RECONCILIATION_PENDING", "DISPUTED"),
    "RECONCILIATION_PENDING": ("RECONCILED", "DISPUTED"),
    "RECONCILED": ("CLOSED",),
    "CLOSED": (),
    "CHANGE_REQUESTED": ("PLAN_DRAFTED", "CANCELLED"),
    "CLIENT_HOLD": (),                      # only back to the state it was entered from (``resume_to``)
    "VENDOR_HOLD": (),
    "DISPUTED": (),                         # likewise; Andre only
    "CANCELLED": (),
    **{x: () for x in SECTION_4_EXCEPTION if x not in EXCEPTIONS_BUILT},
}
# Reached only through their own gate (client approval, funds, authorization, submission, outcome), never through
# the generic transition route. "No state may be silently promoted."
CAMPAIGN_GATED = ("CLIENT_APPROVED", "FUNDING_REQUIRED", "FUNDS_VERIFIED", "PURCHASE_AUTHORIZED", "ORDER_SUBMITTED",
                  "BOOKED")
_COMMITTED = ("CLIENT_APPROVED", "FUNDING_REQUIRED", "FUNDS_VERIFIED", "PURCHASE_AUTHORIZED")
# Transitions the generic route allows only with Andre's approval.
CAMPAIGN_ANDRE = {("PLAN_DRAFTED", "PLAN_APPROVED"), ("QUOTES_VALIDATED", "PROPOSAL_SENT")} \
    | {(s, "CHANGE_REQUESTED") for s in _COMMITTED} | {(s, "CANCELLED") for s in _COMMITTED}
# Leaving a hold or a dispute never restores an authorization: resuming to PURCHASE_AUTHORIZED resumes to
# FUNDS_VERIFIED (the authorization was voided on the way in).
RESUME_MAP = {"PURCHASE_AUTHORIZED": "FUNDS_VERIFIED"}

# ---------------------------------------------------------------------------------------------- section 51
SUBMISSION_STATES = ("NOT_SUBMITTED", "SUBMISSION_UNKNOWN", "ACCEPTED", "REJECTED", "DUPLICATE_BLOCKED")
SUBMISSION_OPEN = ("SUBMISSION_UNKNOWN", "ACCEPTED")    # a vendor commitment exists or may exist
OUTCOMES = ("ACCEPTED", "REJECTED")                     # a timeout is not an outcome: it stays SUBMISSION_UNKNOWN
SUBMISSION_CHANNELS = ("MANUAL_BY_ANDRE",)              # the vendor port is NOT_CONNECTED in OOH-1

# ---------------------------------------------------------------------------------------------- sections 40, 42
ROSTER_CELL_STATUS = "INITIAL_CELL"
ROSTER_INACTIVE_STATUS = "REGISTERED_INACTIVE_READY"
# The 23-capability map (section 40), names and order exactly as the blueprint. ``cell``: the section 42 initial
# operating cell. No agent is built in waves 0-1, so every cell member is also ``NOT_BUILT_YET``.
CAPABILITIES = (
    ("C01", "Department Coordination", False),
    ("C02", "Client Fit", False),
    ("C03", "Market/Geography Intelligence", False),
    ("C04", "Inventory/Operator Intelligence", True),
    ("C05", "Avails/Availability", True),
    ("C06", "Audience/Mobility Intelligence", False),
    ("C07", "Pricing/Transaction Evidence", False),
    ("C08", "Planning", False),
    ("C09", "Proposal/Estimation", False),
    ("C10", "Procurement Liaison", False),
    ("C11", "Contract/Terms Control", False),
    ("C12", "Purchase Authorization Control", False),
    ("C13", "Creative Suitability/Specs", True),
    ("C14", "Production/Trafficking", False),
    ("C15", "Regulatory/Permit Intelligence", False),
    ("C16", "Programmatic DOOH", False),
    ("C17", "Proof-of-Posting/Delivery Verification", True),
    ("C18", "Outage/Makegood Control", False),
    ("C19", "Measurement/Attribution", False),
    ("C20", "Billing/Reconciliation", True),
    ("C21", "Vendor Relationship Intelligence", False),
    ("C22", "Quality/Error/Agent Performance", False),
    ("C23", "Knowledge/Documentation/Recovery", False),
)
CELL_NOTES = {"C20": "covers reconciliation and makegood control for the initial cell; C18 Outage/Makegood Control "
                     "is noted as its dependency"}


def roster() -> list[dict]:
    """The registry as data. No agent is built in OOH-0/1; none can commit anything, ever (Oct 16)."""
    return [{"capability_id": cid, "capability": name, "initial_cell": cell,
             "status": ROSTER_CELL_STATUS if cell else ROSTER_INACTIVE_STATUS,
             "build_state": "NOT_BUILT_YET", "built": False, "authority": "PREPARE_ONLY", "can_commit": False,
             "note": CELL_NOTES.get(cid)}
            for cid, name, cell in CAPABILITIES]


# ---------------------------------------------------------------------------------------------- money
CENT = Decimal("0.01")


def money(s: str) -> Decimal:
    return Decimal(s)


def fmt(d: Decimal) -> str:
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


def apply_markup(cost: Decimal, markup_pct: Decimal) -> Decimal:
    """The disclosed sell price for a cost: cost x (1 + markup / 100), rounded half up to the cent."""
    return (cost * (Decimal(1) + markup_pct / Decimal(100))).quantize(CENT, rounding=ROUND_HALF_UP)
