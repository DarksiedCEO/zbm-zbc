"""
The version-1 fix scope (founder decision 3, ADR 0017) as a closed catalogue: four lanes, the check codes each lane
fixes, and which connectors may carry a fix for each check. A finding names one check; a change set for it may only
use a connector listed for that check, and only that connector's allowlisted operations (connectors/). Anything not
listed here is not fixable by this service.

Fire teams (founder decision 2): two teams of lane specialists running on Claude (Anthropic API) inside delivery-py's
runtime. ``alpha`` has three specialists, ``bravo`` four. A job goes to the smallest team that covers every lane it
needs (ADR 0017 decision 9; defaulted).
"""

from __future__ import annotations

LANES = ("store_settings", "tracking_analytics", "followup_automations", "listings_reviews")

# check code -> (lane, connectors that may fix it). A connector that is NOT_BUILT is listed so the gap is visible: a
# plan naming it is refused CONNECTOR_NOT_BUILT, never silently dropped.
CHECKS: dict[str, tuple[str, tuple[str, ...]]] = {
    # store and website settings (Shopify / Woo product pages, checkout, broken links, speed)
    "product_seo_missing": ("store_settings", ("shopify",)),
    "product_content_error": ("store_settings", ("shopify",)),
    "page_content_error": ("store_settings", ("shopify",)),
    "broken_link": ("store_settings", ("shopify",)),
    "product_metafield_wrong": ("store_settings", ("shopify",)),
    "woo_product_page_error": ("store_settings", ("woocommerce",)),
    "checkout_setting_wrong": ("store_settings", ("shopify_checkout",)),
    "site_speed": ("store_settings", ("shopify_theme",)),
    # tracking and analytics (pixels, GA4, conversion tracking)
    "ga4_key_event_missing": ("tracking_analytics", ("ga4",)),
    "ga4_key_event_wrong": ("tracking_analytics", ("ga4",)),
    "gtm_tag_paused": ("tracking_analytics", ("gtm",)),
    "gtm_tag_trigger_wrong": ("tracking_analytics", ("gtm",)),
    "ad_pixel_missing": ("tracking_analytics", ("ad_pixels",)),
    # follow-up automations (missed calls, abandoned carts, unanswered leads, booking flows)
    "missed_call_followup_off": ("followup_automations", ("service_automations",)),
    "abandoned_cart_flow_off": ("followup_automations", ("service_automations", "sales_automations")),
    "unanswered_lead_followup_off": ("followup_automations", ("sales_automations", "crm")),
    "booking_flow_broken": ("followup_automations", ("service_automations", "crm")),
    # listings and reviews (Google / Yelp profile errors, wrong address or phone)
    "listing_phone_wrong": ("listings_reviews", ("gbp", "yelp")),
    "listing_address_wrong": ("listings_reviews", ("gbp", "yelp")),
    "listing_website_wrong": ("listings_reviews", ("gbp",)),
}

# detection-py's LeakCategory values (services/detection-py/src/zbm_schema/__init__.py), recorded when a finding came
# from Revenue Recovery; never re-derived here
DETECTION_CATEGORIES = frozenset({
    "affiliate_coupon_extension", "discount_misuse", "abandoned_cart_coverage", "renewal_never_triggered",
    "server_side_attribution_gap", "cross_channel_misattribution_risk", "platform_integration_gap",
    "contract_pricing_term_drift"})

FIRE_TEAMS: dict[str, tuple[str, ...]] = {
    "alpha": ("store_settings", "tracking_analytics", "listings_reviews"),
    "bravo": ("store_settings", "tracking_analytics", "followup_automations", "listings_reviews"),
}


def lane_of(check: str) -> str:
    return CHECKS[check][0]


def team_for(lanes: set) -> str:
    """The smallest team covering every lane (alpha before bravo)."""
    for name in sorted(FIRE_TEAMS, key=lambda n: (len(FIRE_TEAMS[n]), n)):
        if set(lanes) <= set(FIRE_TEAMS[name]):
            return name
    raise AssertionError("bravo covers every lane")
