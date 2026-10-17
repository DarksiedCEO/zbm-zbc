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
# Locked (founder, Oct 16 round 2): principal vs agent stays UNDETERMINED, and purchases stay blocked, until a CPA or
# counsel confirms. The confirmation is recorded by Andre as a department-wide determination naming who confirmed and
# the reference of their written confirmation; a deal's commercial model may carry PRINCIPAL or AGENT only when it
# equals the current determination.
PRINCIPAL_AGENT_CONFIRMERS = ("CPA", "COUNSEL")

# ---------------------------------------------------------------------------------------------- section 11
FUNDING_POLICY = "CLIENT_PREPAY_REQUIRED"     # the default and, at launch, the only policy: 100% prepaid
FUNDING_STATES = ("NONE", "PARTIAL", "CLEARED_FULL")
# Who may mark client funds verified-cleared. Finance (31) is the intended source; until its client exists here only
# Andre's gate can, with an evidence reference recorded (Oct 16). The Finance source is listed so the rule is visible;
# no code path can produce it in OOH-1.
FUNDS_SOURCES = ("ANDRE_GATE", "FINANCE_31")
FUNDS_SOURCES_CONNECTED = ("ANDRE_GATE",)
# Locked (founder, Oct 16 round 2): clients pay by ACH only. (Vendors are paid by ACH or check, never card: that is a
# vendor-payment rule, VENDOR_PAYMENT_METHODS, not a client-funds one.)
FUNDS_METHODS = ("ach",)
VENDOR_PAYMENT_METHODS = ("ach", "check")

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

# ---------------------------------------------------------------------------------------------- locked limits
# Locked (founder, Oct 16 round 2): the largest single purchase and the longest an authorization lives. The
# environment may LOWER them (OOH_MAX_PURCHASE_AMOUNT, OOH_AUTHORITY_MAX_DAYS), never raise them.
MAX_PURCHASE_CEILING = "10000.00"
AUTHORITY_DAYS_CEILING = 7

# ---------------------------------------------------------------------------------------------- OOH-2: geography
# The Southern California footprint (founder, Oct 16 round 2). Ids, names and the county scope are geography, not
# market data; everything ZBM knows ABOUT a market is a readiness flag set from evidence, and starts NOT_MET.
MARKETS = (
    ("los-angeles", "Los Angeles", "Los Angeles County, CA"),
    ("orange-county", "Orange County", "Orange County, CA"),
    ("inland-empire", "Inland Empire", "Riverside and San Bernardino Counties, CA"),
    ("san-diego", "San Diego", "San Diego County, CA"),
    ("ventura", "Ventura", "Ventura County, CA"),
    ("santa-barbara", "Santa Barbara", "Santa Barbara County, CA"),
    ("imperial-az-border", "Imperial / Arizona border",
     "Imperial County, CA, and the Arizona border area (the Arizona extent is UNKNOWN until researched)"),
)
MARKET_IDS = tuple(m[0] for m in MARKETS)
# Four independent flags per market; there is no single "active" flag and none is derived from the others.
READINESS_FLAGS = ("RESEARCH_COVERAGE", "PLANNING_COVERAGE", "EXECUTION_AUTHORIZED", "OPERATING_MATURITY")
READINESS_VALUES = ("NOT_MET", "MET")
READINESS_DEFAULT = "NOT_MET"
# What each flag gates (beyond the blueprint as restated; ADR 0020 open question 1). RESEARCH_COVERAGE and
# OPERATING_MATURITY gate nothing yet: they are recorded so later waves can read them.
READINESS_GATES = {"PLANNING_COVERAGE": "a campaign reaches PLAN_APPROVED only when every market it names is MET",
                   "EXECUTION_AUTHORIZED": "a purchase is authorized and submitted only when every unit's market is MET",
                   "RESEARCH_COVERAGE": None, "OPERATING_MATURITY": None}

# ---------------------------------------------------------------------------------------------- OOH-2: inventory
# The pilot formats (founder, Oct 16 round 2), each with its own proof of performance.
FORMATS = ("STATIC_BILLBOARD", "DIGITAL_BILLBOARD", "TRANSIT_STREET_FURNITURE", "MOBILE_BILLBOARD")
PROOF_REQUIREMENTS = {"STATIC_BILLBOARD": "POSTING_PHOTO", "DIGITAL_BILLBOARD": "PLAY_LOGS",
                      "TRANSIT_STREET_FURNITURE": "INSTALL_PROOF", "MOBILE_BILLBOARD": "ROUTE_GPS_PROOF"}
# Where inventory facts come from. Now: vendor quotes and rate cards, recorded by hand. A vendor API or a platform
# feed is a port that is NOT_CONNECTED: a source of that kind is refused.
SOURCE_KINDS = ("VENDOR_QUOTE", "RATE_CARD")
SOURCE_KINDS_NOT_CONNECTED = ("VENDOR_API", "PLATFORM_FEED")
_S, _D, _T, _M = FORMATS
_FIXED = (_S, _D, _T)
# The canonical face: one object for every format. Each attribute is (value type, the formats it applies to). An
# attribute nobody has sourced is UNKNOWN; there are no defaults. Impressions and audience are NOT here: measurement
# providers are not chosen (a later port).
FACE_ATTRIBUTES = {
    "latitude": ("lat", _FIXED), "longitude": ("lon", _FIXED),
    "location_description": ("text", FORMATS), "facing": ("facing", _FIXED),
    "width_ft": ("feet", FORMATS), "height_ft": ("feet", FORMATS),
    "illuminated": ("bool", (_S, _T)),
    "screen_width_px": ("int", (_D,)), "screen_height_px": ("int", (_D,)),
    "slot_length_s": ("int", (_D,)), "loop_length_s": ("int", (_D,)),
    "route_description": ("text", (_M,)),
    "rate_card_amount": ("money", FORMATS), "rate_card_period_days": ("int", FORMATS),
}
FACINGS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
# The supplier (operator) profile, sourced the same way. No contacts: no personal data.
OPERATOR_TYPES = ("OPERATOR", "REP_FIRM", "NETWORK")
SUPPLIER_ATTRIBUTES = {"operator_type": "operator_type", "formats_offered": "formats", "markets_served": "markets",
                       "payment_methods": "payment_methods", "payment_terms_days": "int",
                       "cancellation_policy_ref": "ref"}
# AEGIS wave 2 F-2: no contact attribute (contacts are personal data; this repo and its ledger evidence are not the
# place for them). Free-text values are screened for e-mail addresses and phone numbers as well.
AVAILS_ANSWERS = ("AVAILABLE", "UNAVAILABLE")

# ---------------------------------------------------------------------------------------------- OOH-3: planning
# What a deal's markup may be set from (founder: "disclosed and set per deal from evidence"; 15% is a suggestion).
MARKUP_EVIDENCE_KINDS = ("COMPARABLE_DEALS", "MARKET_RATE_SURVEY", "COST_TO_SERVE", "CLIENT_NEGOTIATION")
# How a rate card becomes a cost estimate: the card's amount for its period, prorated by calendar day to the flight,
# rounded half up to the cent. An estimate, never a quote (class ESTIMATED).
ESTIMATE_METHODS = ("RATE_CARD_PRORATED_DAILY",)
# Where a plan line's cost comes from, best first. UNKNOWN when nothing is sourced: never filled in.
COST_BASES = ("VENDOR_QUOTE", "VENDOR_COST_ESTIMATE", "UNKNOWN")
BRIEF_STATES = ("INTAKE", "BRIEF_VALIDATED", "PLAN_DRAFTED", "CHANGE_REQUESTED")
BRIEF_WINDOW_MAX_DAYS = 731     # a brief's flight window spans at most two years (each flight in it: FLIGHT_MAX_DAYS)

# ---------------------------------------------------------------------------------------------- OOH-4: contracts
# The vendor contract (insertion order) a purchase is made under. Agents draft it from the vendor's document; only
# Andre approves its terms; the authorization names it and the order flow moves it.
CONTRACT_STATES = ("DRAFT", "TERMS_APPROVED_BY_ANDRE", "SUBMITTED", "EXECUTED", "VOID")
CONTRACT_OPEN = ("DRAFT", "TERMS_APPROVED_BY_ANDRE")
CONTRACT_WRITABLE = ("QUOTES_VALIDATED", "PROPOSAL_SENT", "CLIENT_APPROVED", "FUNDING_REQUIRED", "FUNDS_VERIFIED",
                     "PURCHASE_AUTHORIZED")
CONTRACT_PAYMENT_TERMS_MAX_DAYS = 120

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
    # HOLD_CONFIRMED is a legal move but only through Andre's hold decision (AVAILABILITY_ANDRE_ONLY, below)
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
