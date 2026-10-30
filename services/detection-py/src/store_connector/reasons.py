"""Closed reason codes of the store connector (ADR 0020 section 5).

Every refusal or failure of a store status check, sync or snapshot read carries exactly one of these codes. A code
never carries upstream text, a token, a header or a customer field: the message that goes with it is chosen here.
"""

from __future__ import annotations

# code -> (HTTP status detection-py answers with, the fixed message shown to callers)
REASONS: dict[str, tuple[int, str]] = {
    # no outbound call was made for any of these
    "NOT_CONNECTED": (409, "no store connection is registered for this client"),
    "STORE_READS_DISABLED": (503, "store reads are switched off (kill switch)"),
    "STORE_CONFIG_INVALID": (503, "the store connections file is missing, unreadable or invalid"),
    "STORE_CREDENTIAL_UNREADABLE": (503, "the registered credential file cannot be read safely"),
    "STORE_BUDGET_EXHAUSTED": (429, "this client's store-read budget for the day is used up"),
    "STORE_SYNC_IN_PROGRESS": (409, "a store sync is already running for this client"),
    "STORE_SYNC_CAPACITY": (429, "too many store syncs are running at once; try again shortly"),
    "LEDGER_UNAVAILABLE": (503, "the evidence ledger did not record the read request; nothing was read"),
    "NOT_SYNCED": (409, "the store is connected but has not been synced yet"),
    "SNAPSHOT_STALE": (409, "the last store snapshot is older than this client's allowed age; sync again"),
    # ADR 0024: Compliance (38) is the hard gate before a client is served
    "COMPLIANCE_NOT_ACTIVATED": (409, "Compliance has no current activation for this client; nothing was read"),
    "COMPLIANCE_UNAVAILABLE": (503, "Compliance could not confirm this client's activation; nothing was read"),
    # an outbound read was attempted and stopped
    "STORE_UNAVAILABLE": (502, "the store could not be reached"),
    "STORE_DEADLINE_EXCEEDED": (502, "the store read did not finish within its deadline"),
    "STORE_AUTH_REFUSED": (502, "the store refused the credential"),
    "STORE_SHOP_UNAVAILABLE": (502, "the shop is frozen, inactive or unavailable"),
    "STORE_THROTTLED": (502, "the store kept throttling reads past the retry limit"),
    "STORE_QUERY_TOO_COSTLY": (502, "the store refused the query as too costly at the smallest page size"),
    "STORE_RESPONSE_INVALID": (502, "the store answered with something that is not the expected response"),
    "STORE_API_VERSION_MISMATCH": (502, "the store answered with a different API version than the pinned one"),
    "STORE_SHOP_MISMATCH": (502, "the store answered for a different shop than the one registered"),
    "STORE_TOKEN_HAS_WRITE_SCOPE": (422, "the credential grants write access; only read scopes are accepted"),
    "STORE_TOKEN_EXCESS_SCOPE": (422, "the credential grants scopes beyond the minimum read scopes"),
    "STORE_SCOPE_MISSING": (422, "the credential lacks a required read scope"),
    "STORE_CURRENCY_UNSUPPORTED": (422, "the shop's currency is not USD"),
    "STORE_COST_BUDGET_EXHAUSTED": (502, "the read used this client's query-cost budget before finishing"),
    "STORE_SNAPSHOT_TOO_LARGE": (502, "the minimised snapshot exceeds the size the scan path accepts"),
    "STORE_REGISTRATION_CHANGED": (409, ("the client's store registration changed during the sync; the read was "
                                         "discarded, sync again")),
    "LEDGER_COMPLETION_UNRECORDED": (503, ("the store was read but the evidence ledger did not record the completion; "
                                           "the read was discarded, sync again")),
}

NO_OUTBOUND = frozenset({"NOT_CONNECTED", "STORE_READS_DISABLED", "STORE_CONFIG_INVALID",
                         "STORE_CREDENTIAL_UNREADABLE", "STORE_BUDGET_EXHAUSTED", "STORE_SYNC_IN_PROGRESS",
                         "STORE_SYNC_CAPACITY", "LEDGER_UNAVAILABLE", "NOT_SYNCED", "SNAPSHOT_STALE",
                         "COMPLIANCE_NOT_ACTIVATED", "COMPLIANCE_UNAVAILABLE"})


class StoreError(Exception):
    """A store operation stopped. ``code`` is one of REASONS; ``str()`` is the fixed message, never upstream text."""

    def __init__(self, code: str):
        if code not in REASONS:
            raise AssertionError(f"not a store reason code: {code}")
        super().__init__(REASONS[code][1])
        self.code = code

    @property
    def http_status(self) -> int:
        return REASONS[self.code][0]

    def detail(self) -> dict:
        return {"reason": self.code, "message": REASONS[self.code][1]}

    def __repr__(self) -> str:
        return f"StoreError({self.code})"
