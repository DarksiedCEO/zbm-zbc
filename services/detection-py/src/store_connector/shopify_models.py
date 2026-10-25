"""Strict models of exactly what the connector asks Shopify for (ADR 0020 section 4).

The connector selects only the fields below, so an answer with any other field in a node (an email, a name, an
address — protected customer data it never asked for) is not the expected response: every node model forbids
extra fields, and the whole response is refused (STORE_RESPONSE_INVALID). Parsing is JSON-mode strict: a number
where a string belongs, a naive timestamp or an unknown enum value is refused too.

The queries are constants; nothing a client or caller supplies is ever interpolated into them (variables only).
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

API_VERSION = "2026-10"          # ADR 0020 S4: "Latest stable" on Oct 24 2026; supported at least 12 months

PREFLIGHT_QUERY = (
    "query RrPreflight { shop { currencyCode myshopifyDomain } "
    "currentAppInstallation { accessScopes { handle } } }"
)

# Page sizes: ORDERS_PAGE_SIZE is the starting size and is halved (down to 1) when Shopify refuses the requested
# cost (S6: a single query may not exceed 1,000 points). The nested caps are the schema's own per-order limits
# (zbm_schema.limits): an order with more line items or discount applications than these is excluded, never cut.
ORDERS_PAGE_SIZE = 10
LINE_ITEMS_PER_ORDER = 50
DISCOUNT_APPLICATIONS_PER_ORDER = 10

ORDERS_QUERY = (
    "query RrOrders($first: Int!, $after: String, $q: String!) { "
    "orders(first: $first, after: $after, query: $q, sortKey: CREATED_AT, reverse: true) { "
    "pageInfo { hasNextPage endCursor } "
    "nodes { legacyResourceId createdAt cancelledAt test currencyCode displayFinancialStatus "
    "totalRefundedSet { shopMoney { amount currencyCode } } "
    f"discountApplications(first: {DISCOUNT_APPLICATIONS_PER_ORDER}) {{ pageInfo {{ hasNextPage }} "
    "nodes { __typename index allocationMethod targetSelection targetType "
    "value { __typename ... on MoneyV2 { amount currencyCode } ... on PricingPercentageValue { percentage } } "
    "... on DiscountCodeApplication { code } } } "
    f"lineItems(first: {LINE_ITEMS_PER_ORDER}) {{ pageInfo {{ hasNextPage }} "
    "nodes { sku quantity currentQuantity originalUnitPriceSet { shopMoney { amount currencyCode } } "
    "discountAllocations { allocatedAmountSet { shopMoney { amount currencyCode } } "
    "discountApplication { index } } } } } } }"
)

# Fields the connector must never request (protected customer data level 2 and identifying order fields).
FORBIDDEN_FIELDS = ("email", "phone", "customer", "billingAddress", "shippingAddress", "displayAddress", "firstName",
                    "lastName", "note", "clientIp", "customAttributes", "name")

_DECIMAL = r"^-?[0-9]{1,15}(\.[0-9]{1,6})?$"


class _Node(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MoneyV2(_Node):
    amount: str = Field(pattern=_DECIMAL, max_length=24)
    currencyCode: str = Field(pattern=r"^[A-Z]{3}$")


class MoneyBag(_Node):
    shopMoney: MoneyV2


class HasNext(_Node):
    hasNextPage: bool


class AllocationApplicationRef(_Node):
    index: int = Field(ge=0, le=10**6)


class DiscountAllocationNode(_Node):
    """What Shopify recorded as given to this line by one discount application (H-1: the recorded amount is used,
    not a recomputation from the code's nominal value)."""
    allocatedAmountSet: MoneyBag
    discountApplication: AllocationApplicationRef


class LineItemNode(_Node):
    sku: Optional[str] = Field(default=None, max_length=1024)
    quantity: int = Field(ge=0, le=10**9)
    # Units excluding refunded and removed ones; differs from quantity after a refund or an order edit.
    currentQuantity: int = Field(ge=0, le=10**9)
    originalUnitPriceSet: MoneyBag
    # One per discount application that gave this line anything. Includes amounts on refunded and removed units.
    discountAllocations: list[DiscountAllocationNode] = Field(max_length=250)


class LineItems(_Node):
    pageInfo: HasNext
    nodes: list[LineItemNode] = Field(max_length=LINE_ITEMS_PER_ORDER)


class MoneyValue(_Node):
    typename: Literal["MoneyV2"] = Field(alias="__typename")
    amount: str = Field(pattern=_DECIMAL, max_length=24)
    currencyCode: str = Field(pattern=r"^[A-Z]{3}$")


class PercentValue(_Node):
    typename: Literal["PricingPercentageValue"] = Field(alias="__typename")
    percentage: float = Field(ge=0, le=100, allow_inf_nan=False)


class DiscountApplicationNode(_Node):
    typename: Literal["AutomaticDiscountApplication", "DiscountCodeApplication", "ManualDiscountApplication",
                      "ScriptDiscountApplication"] = Field(alias="__typename")
    index: int = Field(ge=0, le=10**6)
    allocationMethod: Literal["ACROSS", "EACH"]
    targetSelection: Literal["ALL", "ENTITLED", "EXPLICIT"]
    targetType: Literal["LINE_ITEM", "SHIPPING_LINE"]
    value: Union[MoneyValue, PercentValue] = Field(discriminator="typename")
    code: Optional[str] = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _code_iff_code_application(self):
        if (self.code is not None) != (self.typename == "DiscountCodeApplication"):
            raise ValueError("code present exactly on DiscountCodeApplication")
        return self


class DiscountApplications(_Node):
    pageInfo: HasNext
    nodes: list[DiscountApplicationNode] = Field(max_length=DISCOUNT_APPLICATIONS_PER_ORDER)


FinancialStatus = Literal["AUTHORIZED", "EXPIRED", "PAID", "PARTIALLY_PAID", "PARTIALLY_REFUNDED", "PENDING",
                          "REFUNDED", "VOIDED"]


class OrderNode(_Node):
    legacyResourceId: str = Field(pattern=r"^[0-9]{1,20}$")
    createdAt: AwareDatetime
    cancelledAt: Optional[AwareDatetime] = None
    test: bool
    currencyCode: str = Field(pattern=r"^[A-Z]{3}$")
    displayFinancialStatus: Optional[FinancialStatus] = None
    totalRefundedSet: MoneyBag
    discountApplications: DiscountApplications
    lineItems: LineItems


class OrdersPageInfo(_Node):
    hasNextPage: bool
    endCursor: Optional[str] = Field(default=None, max_length=1024)


class OrdersConnection(_Node):
    pageInfo: OrdersPageInfo
    nodes: list[OrderNode] = Field(max_length=250)


class OrdersData(_Node):
    orders: OrdersConnection


class AccessScope(_Node):
    handle: str = Field(pattern=r"^[a-z_]{1,64}$")


class AppInstallation(_Node):
    accessScopes: list[AccessScope] = Field(max_length=500)


class ShopInfo(_Node):
    currencyCode: str = Field(pattern=r"^[A-Z]{3}$")
    myshopifyDomain: str = Field(max_length=255)


class PreflightData(_Node):
    shop: ShopInfo
    currentAppInstallation: AppInstallation


class ThrottleStatus(BaseModel):
    model_config = ConfigDict(strict=True)
    maximumAvailable: float = Field(ge=0, le=1e7, allow_inf_nan=False)
    currentlyAvailable: float = Field(ge=0, le=1e7, allow_inf_nan=False)
    restoreRate: float = Field(gt=0, le=1e6, allow_inf_nan=False)


class Cost(BaseModel):
    model_config = ConfigDict(strict=True)
    requestedQueryCost: Optional[float] = Field(default=None, ge=0, le=1e7, allow_inf_nan=False)
    actualQueryCost: Optional[float] = Field(default=None, ge=0, le=1e7, allow_inf_nan=False)
    throttleStatus: ThrottleStatus


class GraphQLErrorExt(BaseModel):
    code: Optional[str] = Field(default=None, max_length=64)


class GraphQLError(BaseModel):
    message: str = Field(default="", max_length=4096)
    extensions: Optional[GraphQLErrorExt] = None


class Extensions(BaseModel):
    cost: Optional[Cost] = None


class Envelope(BaseModel):
    """The top level: ``data`` is parsed separately into the model the query expects."""

    model_config = ConfigDict(strict=True)
    data: Optional[dict[str, Any]] = None
    errors: Optional[list[GraphQLError]] = Field(default=None, max_length=100)
    extensions: Optional[Extensions] = None


class TokenResponse(BaseModel):
    """The client credentials grant's answer (S2)."""

    model_config = ConfigDict(strict=True)
    access_token: str = Field(min_length=8, max_length=1024, pattern=r"^[A-Za-z0-9_.:+/=-]+$")
    scope: str = Field(default="", max_length=4096, pattern=r"^[a-z_, ]*$")
    expires_in: int = Field(ge=60, le=10**7)

