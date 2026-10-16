"""
The OOH / DOOH department constitution as data (ADR 0019, wave OOH-0). Everything here is a closed vocabulary or a
fixed rule the service enforces; nothing here is configurable at runtime.

Sources: the founder decisions of Oct 16 2026 and the OOH/DOOH master blueprint v1 (Oct 7 2026) sections named
below. The blueprint was not available to this build as a file; the section contents used are the ones restated
in the build brief. Where the blueprint's exact names were not restated (the campaign state names of section 4, the
23-capability map and the initial operating cell of section 42) the names here are RECONSTRUCTED and marked
``provisional``: reconciling them with the blueprint is an open item of ADR 0019, not a silent choice.
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
    "hold_confirmation": ("VENDOR_ASSERTED",),
    "funds_andre_gate": ("OBSERVED",),      # Andre saw cleared funds; never FINANCIALLY_VERIFIED (that is Finance's)
    "order_outcome": ("VENDOR_ASSERTED",),
}

# ---------------------------------------------------------------------------------------------- section 9
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

# ---------------------------------------------------------------------------------------------- sections 3 and 4
# The lifecycle (section 3) as campaign states. Names reconstructed (provisional), order and gates as briefed.
CAMPAIGN_STATES = ("DRAFT", "BRIEF_CONFIRMED", "PLANNING", "QUOTING", "PROPOSAL_READY", "PROPOSAL_SENT",
                   "CLIENT_APPROVED", "FUNDS_VERIFIED", "PURCHASE_AUTHORIZED", "ORDER_SUBMITTED", "BOOKED",
                   "IN_PRODUCTION", "LIVE", "COMPLETED", "RECONCILED", "CLOSED")
EXCEPTION_STATES = ("ON_HOLD", "CHANGE_REQUESTED", "DISPUTED", "CANCELLED")
ALL_STATES = CAMPAIGN_STATES + EXCEPTION_STATES
TERMINAL = ("CANCELLED", "CLOSED")
_PRE_ORDER = ("DRAFT", "BRIEF_CONFIRMED", "PLANNING", "QUOTING", "PROPOSAL_READY", "PROPOSAL_SENT", "CLIENT_APPROVED",
              "FUNDS_VERIFIED", "PURCHASE_AUTHORIZED")
CAMPAIGN_ALLOWED = {
    "DRAFT": ("BRIEF_CONFIRMED", "ON_HOLD", "CANCELLED"),
    "BRIEF_CONFIRMED": ("PLANNING", "ON_HOLD", "CHANGE_REQUESTED", "CANCELLED"),
    "PLANNING": ("QUOTING", "ON_HOLD", "CHANGE_REQUESTED", "CANCELLED"),
    "QUOTING": ("PROPOSAL_READY", "ON_HOLD", "CHANGE_REQUESTED", "CANCELLED"),
    "PROPOSAL_READY": ("PROPOSAL_SENT", "ON_HOLD", "CHANGE_REQUESTED", "CANCELLED"),
    "PROPOSAL_SENT": ("CLIENT_APPROVED", "ON_HOLD", "CHANGE_REQUESTED", "CANCELLED"),
    "CLIENT_APPROVED": ("FUNDS_VERIFIED", "ON_HOLD", "CHANGE_REQUESTED", "CANCELLED"),
    "FUNDS_VERIFIED": ("PURCHASE_AUTHORIZED", "ON_HOLD", "CHANGE_REQUESTED", "CANCELLED"),
    "PURCHASE_AUTHORIZED": ("PURCHASE_AUTHORIZED", "ORDER_SUBMITTED", "ON_HOLD", "CHANGE_REQUESTED", "CANCELLED"),
    # an order in flight cannot be held, changed or cancelled here: its outcome is unknown until recorded (section 51)
    "ORDER_SUBMITTED": ("BOOKED", "FUNDS_VERIFIED", "DISPUTED"),
    "BOOKED": ("IN_PRODUCTION", "DISPUTED"),
    "IN_PRODUCTION": ("LIVE", "DISPUTED"),
    "LIVE": ("COMPLETED", "DISPUTED"),
    "COMPLETED": ("RECONCILED", "DISPUTED"),
    "RECONCILED": ("CLOSED",),
    "CLOSED": (),
    "CHANGE_REQUESTED": ("PLANNING", "CANCELLED"),
    "ON_HOLD": (),                          # only back to the state it was entered from (``resume_to``)
    "DISPUTED": (),                         # likewise; Andre only
    "CANCELLED": (),
}
# Reached only through their own gate (client approval, funds verification, authorization, submission, outcome),
# never through the generic transition route. "No state may be silently promoted."
CAMPAIGN_GATED = ("CLIENT_APPROVED", "FUNDS_VERIFIED", "PURCHASE_AUTHORIZED", "ORDER_SUBMITTED", "BOOKED")
# Transitions the generic route allows only with Andre's approval.
CAMPAIGN_ANDRE = {("PROPOSAL_READY", "PROPOSAL_SENT")} | {(s, "CHANGE_REQUESTED") for s in
                                                          ("CLIENT_APPROVED", "FUNDS_VERIFIED", "PURCHASE_AUTHORIZED")} \
    | {(s, "CANCELLED") for s in ("CLIENT_APPROVED", "FUNDS_VERIFIED", "PURCHASE_AUTHORIZED")}
# Production, go-live, proof of performance and reconciliation are later waves (OOH-2+): refused, never pretended.
CAMPAIGN_NOT_IN_WAVE = ("IN_PRODUCTION", "LIVE", "COMPLETED", "RECONCILED", "CLOSED")
HOLDABLE = tuple(s for s in _PRE_ORDER)
# Leaving ON_HOLD / DISPUTED / CHANGE_REQUESTED never restores an authorization: resuming to PURCHASE_AUTHORIZED
# resumes to FUNDS_VERIFIED (the authorization was voided on the way in).
RESUME_MAP = {"PURCHASE_AUTHORIZED": "FUNDS_VERIFIED"}

# ---------------------------------------------------------------------------------------------- section 51
SUBMISSION_STATES = ("NOT_SUBMITTED", "SUBMISSION_UNKNOWN", "ACCEPTED", "REJECTED", "DUPLICATE_BLOCKED")
SUBMISSION_OPEN = ("SUBMISSION_UNKNOWN", "ACCEPTED")    # a vendor commitment exists or may exist
OUTCOMES = ("ACCEPTED", "REJECTED")                     # a timeout is not an outcome: it stays SUBMISSION_UNKNOWN
SUBMISSION_CHANNELS = ("MANUAL_BY_ANDRE",)              # the vendor port is NOT_CONNECTED in OOH-1

# ---------------------------------------------------------------------------------------------- section 42 (roster)
ROSTER_ACTIVE_STATUS = "INITIAL_CELL_NOT_BUILT"
ROSTER_INACTIVE_STATUS = "REGISTERED_INACTIVE_READY"
# The 23-capability map (provisional names: see the module docstring). ``cell``: the initial operating cell.
CAPABILITIES = (
    ("C01", "campaign_intake_and_brief", True),
    ("C02", "audience_and_market_research", False),
    ("C03", "geo_market_planning", False),
    ("C04", "inventory_discovery", True),
    ("C05", "inventory_verification", False),
    ("C06", "availability_and_holds", True),
    ("C07", "vendor_quotes_and_rate_cards", True),
    ("C08", "negotiation_preparation", False),
    ("C09", "media_plan_and_proposal_assembly", False),
    ("C10", "disclosed_markup_preparation", False),
    ("C11", "client_approval_packaging", False),
    ("C12", "cash_exposure_and_funds_readiness", True),
    ("C13", "purchase_readiness_check", False),
    ("C14", "contract_and_terms_preparation", False),
    ("C15", "order_submission_tracking", False),
    ("C16", "creative_specs_and_production_coordination", False),
    ("C17", "creative_compliance_and_vendor_approval", False),
    ("C18", "installation_and_go_live_verification", False),
    ("C19", "dooh_play_log_intake", False),
    ("C20", "proof_of_performance", False),
    ("C21", "measurement_and_attribution", False),
    ("C22", "reconciliation_and_invoice_matching", False),
    ("C23", "vendor_relationship_and_performance", False),
)


def roster() -> list[dict]:
    """The registry as data. No agent is built in OOH-0/1; none can commit anything, ever (Oct 16)."""
    return [{"capability_id": cid, "capability": name, "initial_cell": cell,
             "status": ROSTER_ACTIVE_STATUS if cell else ROSTER_INACTIVE_STATUS, "built": False,
             "authority": "PREPARE_ONLY", "can_commit": False, "provisional_name": True}
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
