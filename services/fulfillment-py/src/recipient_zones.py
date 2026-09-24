"""
Which time zones a phone number can plausibly be in — the half of the
calling-hours rule the caller must not be able to decide (fix wave 1, F3).

Before this module the recipient's zone was whatever `timezone_by_call_id`
the caller sent, never checked against the number: a Los Angeles number at
02:00 local was dialed because the caller said "UTC" (or "Asia/Tokyo").

Policy (no new dependency, no area-code database):

  * The number must be strict E.164. Anything else => no contact.

  * +1 (NANP). The number must be +1 NPA NXX XXXX with NPA and NXX in
    [2-9]XX. The claimed zone must be on NANP_CLAIMABLE_ZONES (US, Canada,
    US territories, and the Caribbean NANP members); "UTC", "Asia/Tokyo",
    "US/Eastern" and every other name are refused. Then the contact time
    must be inside the window in EVERY zone plausible for the number AND in
    the claimed zone:
      - NPAs in _NPA_OUTSIDE_CONTINENT (Hawaii, Alaska, Puerto Rico, USVI,
        Guam, CNMI, American Samoa) are plausible only in their own zones;
      - every other geographic NPA is treated as plausible in ANY
        continental US/Canada zone (CONTINENTAL_US_CA_ZONES), from
        St. John's (UTC-3:30/-2:30) to Los Angeles/Vancouver (UTC-8/-7).
        We deliberately do not ship a full area-code -> zone table: several
        NPAs span two zones, overlays and number portability make a table
        silently wrong in the dangerous direction, and a wrong row is a
        night call. Trade-off, accepted: the daily window for a continental
        +1 number is 08:00-16:30 Pacific (= 12:30-21:00 Newfoundland), not
        08:00-21:00 local. Never a night call anywhere the number could be.
        The Caribbean NANP zones all lie inside that offset span, so the
        continental rule already covers them.
      - non-geographic NPAs (toll-free, premium, personal/PCS, reserved) have
        no location and are refused.
    The claimed zone is still applied on top, so a caller that knows the
    customer lives in Honolulu with a 212 number narrows the window further
    (both New York and Honolulu must be daytime); it can never widen it.

  * Any other country code: refused unless the operator configured a zone
    set for that code (FULFILLMENT_COUNTRY_ZONES, e.g.
    "44=Europe/London;61=Australia/Perth,Australia/Sydney"). Then the claimed
    zone must be one of the configured zones, and the window must hold in
    every configured zone. A malformed value refuses startup.

Sources: zone names are IANA tzdb canonical names (checked to resolve at
import, or the service refuses to start). NPA facts are from the NANPA
area-code assignments: 808 Hawaii; 907 Alaska; 787/939 Puerto Rico; 340 US
Virgin Islands; 671 Guam; 670 Northern Mariana Islands; 684 American
Samoa; N11, N9X, 37X and 96X are not assignable as geographic NPAs; 8YY,
N00, 5XX/6XX easily-recognisable and personal-communications codes, 456,
700 and 710 are non-geographic. An NPA wrongly placed on the
non-geographic list only makes the rule stricter (fail closed); the
dangerous direction — a new Hawaii/Alaska/territory overlay being treated
as continental — requires adding it to _NPA_OUTSIDE_CONTINENT, which is
pinned by test_outbound_gate.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from contact_window import resolve_timezone

# Always .fullmatch (fix wave 4): with .match, "$" also matches before a
# trailing "\n", so "+12125550101\n" passed as E.164 and the gate keyed it as
# a different number from "+12125550101" — a second attempt budget for the
# same phone. (The API's PhoneE164 check, pydantic-core's regex, already
# rejected it; this is the gate's own check.) Both patterns are linear: no
# nested or overlapping quantifiers (tests/test_fix4_limits.py times them).
E164 = re.compile(r"\+[1-9][0-9]{1,14}")
_NANP = re.compile(r"\+1([2-9][0-9]{2})([2-9][0-9]{2})([0-9]{4})")

CONTINENTAL_US_CA_ZONES: tuple[str, ...] = (
    # United States, contiguous 48
    "America/New_York", "America/Detroit",
    "America/Kentucky/Louisville", "America/Kentucky/Monticello",
    "America/Indiana/Indianapolis", "America/Indiana/Vincennes", "America/Indiana/Winamac",
    "America/Indiana/Marengo", "America/Indiana/Petersburg", "America/Indiana/Vevay",
    "America/Chicago", "America/Indiana/Tell_City", "America/Indiana/Knox", "America/Menominee",
    "America/North_Dakota/Center", "America/North_Dakota/New_Salem", "America/North_Dakota/Beulah",
    "America/Denver", "America/Boise", "America/Phoenix", "America/Los_Angeles",
    # Canada
    "America/St_Johns", "America/Halifax", "America/Glace_Bay", "America/Moncton",
    "America/Goose_Bay", "America/Blanc-Sablon", "America/Toronto", "America/Iqaluit",
    "America/Atikokan", "America/Winnipeg", "America/Resolute", "America/Rankin_Inlet",
    "America/Regina", "America/Swift_Current", "America/Edmonton", "America/Cambridge_Bay",
    "America/Inuvik", "America/Creston", "America/Dawson_Creek", "America/Fort_Nelson",
    "America/Whitehorse", "America/Dawson", "America/Vancouver",
)

_ALASKA = (
    "America/Anchorage", "America/Juneau", "America/Sitka", "America/Yakutat",
    "America/Nome", "America/Metlakatla", "America/Adak",
)

# NANP area codes whose subscribers are NOT in the continental span.
_NPA_OUTSIDE_CONTINENT: dict[str, tuple[str, ...]] = {
    "808": ("Pacific/Honolulu",),
    "907": _ALASKA,
    "787": ("America/Puerto_Rico",),
    "939": ("America/Puerto_Rico",),
    "340": ("America/St_Thomas",),
    "671": ("Pacific/Guam",),
    "670": ("Pacific/Saipan",),
    "684": ("Pacific/Pago_Pago",),
}

_CARIBBEAN_NANP = (
    "America/Anguilla", "America/Antigua", "America/Nassau", "America/Barbados",
    "Atlantic/Bermuda", "America/Tortola", "America/Cayman", "America/Dominica",
    "America/Santo_Domingo", "America/Grenada", "America/Jamaica", "America/Montserrat",
    "America/Lower_Princes", "America/St_Kitts", "America/St_Lucia", "America/St_Vincent",
    "America/Port_of_Spain", "America/Grand_Turk",
)

NANP_CLAIMABLE_ZONES: frozenset[str] = frozenset(
    CONTINENTAL_US_CA_ZONES
    + tuple(z for zones in _NPA_OUTSIDE_CONTINENT.values() for z in zones)
    + _CARIBBEAN_NANP
)

_NON_GEOGRAPHIC_NPA: frozenset[str] = frozenset(
    {"456", "700", "710", "900"}
    | {f"8{d}{d}" for d in "0123456789"}             # 800, 822, 833 ... 888 toll-free
    | {f"88{d}" for d in "0123456789"}               # 880-889 reserved toll-free
    | {"500", "521", "522", "523", "524", "525", "526", "527", "528", "529", "532", "533",
       "535", "538", "542", "543", "544", "545", "546", "547", "549", "550", "552", "553",
       "554", "556", "558", "566", "569", "577", "578", "588", "589"}  # personal comms
    | {"600", "622", "633", "644", "655", "677", "688"}                # Canada non-geographic
)


def _npa_not_geographic(npa: str) -> bool:
    return (
        npa in _NON_GEOGRAPHIC_NPA
        or npa[1:] == "11"            # N11 service codes
        or npa[1] == "9"              # N9X reserved for expansion
        or npa[:2] in ("37", "96")    # reserved
        or npa[1:] == "00"            # N00
    )


@dataclass(frozen=True)
class ZoneVerdict:
    """zones: every zone the window must hold in (plausible set + claimed),
    or None with a reason when the number/claim cannot be verified. The
    reason never contains the phone number."""
    zones: tuple[str, ...] | None
    reason: str | None


def _refuse(reason: str) -> ZoneVerdict:
    return ZoneVerdict(None, reason + " — not contacted (fail closed)")


def zones_to_check(phone: str, claimed_tz: str | None, country_zones: dict[str, tuple[str, ...]]) -> ZoneVerdict:
    if not isinstance(phone, str) or not E164.fullmatch(phone):
        return _refuse("phone number is not E.164")
    if resolve_timezone(claimed_tz) is None:
        return _refuse("recipient time zone unknown or invalid")

    if phone.startswith("+1"):
        m = _NANP.fullmatch(phone)
        if not m:
            return _refuse("+1 number is not a valid 10-digit NANP number")
        npa = m.group(1)
        if _npa_not_geographic(npa):
            return _refuse("+1 number has a non-geographic area code, so its time zone cannot be verified")
        if claimed_tz not in NANP_CLAIMABLE_ZONES:
            return _refuse(f"recipient time zone {claimed_tz!r} is not a valid zone for a +1 (NANP) number")
        plausible = _NPA_OUTSIDE_CONTINENT.get(npa, CONTINENTAL_US_CA_ZONES)
        return ZoneVerdict(tuple(dict.fromkeys(plausible + (claimed_tz,))), None)

    for cc, zones in country_zones.items():
        if phone.startswith("+" + cc):
            if claimed_tz not in zones:
                return _refuse(f"recipient time zone {claimed_tz!r} is not configured for country code +{cc}")
            return ZoneVerdict(tuple(zones), None)
    return _refuse("no time-zone rule is configured for this number's country code")


def parse_country_zones(value: str) -> dict[str, tuple[str, ...]]:
    """"44=Europe/London;61=Australia/Perth,Australia/Sydney" -> mapping.
    Raises ValueError on anything it cannot verify."""
    out: dict[str, tuple[str, ...]] = {}
    for part in (p.strip() for p in value.split(";")):
        if not part:
            continue
        cc, sep, zones_raw = part.partition("=")
        cc = cc.strip()
        if not sep or not re.fullmatch(r"[2-9][0-9]{0,2}", cc):
            raise ValueError(f"country code {cc!r} must be 1-3 digits and not +1 (NANP is built in)")
        zones = tuple(z.strip() for z in zones_raw.split(",") if z.strip())
        if not zones:
            raise ValueError(f"country code +{cc} has no zones")
        for z in zones:
            if resolve_timezone(z) is None:
                raise ValueError(f"unknown time zone {z!r} for +{cc}")
        if cc in out:
            raise ValueError(f"country code +{cc} configured twice")
        out[cc] = zones
    codes = list(out)
    for a in codes:
        for b in codes:
            if a != b and b.startswith(a):
                raise ValueError(f"country codes +{a} and +{b} overlap (E.164 codes are prefix-free)")
    return out


def _verify_tables() -> None:
    missing = [z for z in NANP_CLAIMABLE_ZONES if resolve_timezone(z) is None]
    if missing:
        raise RuntimeError(
            f"time zone database is missing {sorted(missing)}; the calling-hours rule cannot be "
            "verified, so this service refuses to start (fail closed)"
        )


_verify_tables()
