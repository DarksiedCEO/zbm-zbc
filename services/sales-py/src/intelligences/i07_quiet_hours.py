"""Quiet hours: 8 am to 9 pm in the RECIPIENT's local time (TCPA 47 CFR 64.1200(c)(1); ADR 0013 decision 12).

Decides: whether a text or call may be placed now. The recipient's IANA time zone is required: unknown or invalid ->
refused (never the sender's zone, never a guess from the area code). Never: allows a call outside the window."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NUMBER = 7
NAME = "quiet_hours"
DECIDES = "whether now is inside 08:00-21:00 recipient-local"

START_HOUR = 8
END_HOUR = 21


def zone(name: Optional[str]) -> Optional[ZoneInfo]:
    if not name or not isinstance(name, str) or len(name) > 64 or name.startswith("/") or ".." in name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


# AEGIS S1-M1 / S2-L1: a North American (+1) number can only be in a NANP country's zone (US and territories, Canada,
# the NANP Caribbean). South America, Greenland and Mexico are not NANP. A recorded zone that contradicts the number
# is refused, so nobody can move a recipient to a daytime zone to text them at night.
US_ZONES = frozenset({
    "America/New_York", "America/Detroit", "America/Kentucky/Louisville", "America/Kentucky/Monticello",
    "America/Indiana/Indianapolis", "America/Indiana/Vincennes", "America/Indiana/Winamac",
    "America/Indiana/Marengo", "America/Indiana/Petersburg", "America/Indiana/Vevay", "America/Indiana/Tell_City",
    "America/Indiana/Knox", "America/Chicago", "America/Menominee", "America/North_Dakota/Center",
    "America/North_Dakota/New_Salem", "America/North_Dakota/Beulah", "America/Denver", "America/Boise",
    "America/Phoenix", "America/Los_Angeles", "America/Anchorage", "America/Juneau", "America/Sitka",
    "America/Metlakatla", "America/Yakutat", "America/Nome", "America/Adak", "Pacific/Honolulu",
    "America/Puerto_Rico", "America/St_Thomas", "Pacific/Guam", "Pacific/Saipan", "Pacific/Pago_Pago"})
CANADA_ZONES = frozenset({
    "America/St_Johns", "America/Halifax", "America/Glace_Bay", "America/Moncton", "America/Goose_Bay",
    "America/Toronto", "America/Iqaluit", "America/Atikokan", "America/Winnipeg", "America/Rankin_Inlet",
    "America/Resolute", "America/Regina", "America/Swift_Current", "America/Edmonton", "America/Cambridge_Bay",
    "America/Inuvik", "America/Creston", "America/Dawson_Creek", "America/Fort_Nelson", "America/Whitehorse",
    "America/Dawson", "America/Vancouver", "America/Blanc-Sablon"})
CARIBBEAN_ZONES = frozenset({
    "America/Anguilla", "America/Antigua", "America/Barbados", "America/Cayman", "America/Dominica",
    "America/Grenada", "America/Jamaica", "America/Montserrat", "America/Nassau", "America/Santo_Domingo",
    "America/St_Kitts", "America/St_Lucia", "America/St_Vincent", "America/Tortola", "America/Port_of_Spain",
    "America/Grand_Turk", "America/Lower_Princes", "Atlantic/Bermuda"})
NANP_ZONES = US_ZONES | CANADA_ZONES | CARIBBEAN_ZONES
# Where a US area code's numbers are (S2-L1). The recorded zone AND the area code's zone must both be inside the
# window (the stricter wins); an area code not listed must be inside the window in BOTH Eastern and Pacific time
# (11:00-21:00 Eastern).
_AREA = {
    "America/New_York": "201 202 203 207 212 215 216 220 223 234 239 240 248 267 272 276 301 302 304 305 313 315 "
                        "321 324 330 332 336 339 347 351 352 380 386 401 404 407 410 412 413 419 434 440 443 445 470 "
                        "475 478 484 508 513 516 517 518 540 551 561 567 570 571 585 586 603 607 609 610 614 616 617 "
                        "631 646 656 678 680 689 703 704 706 716 717 718 724 727 732 734 740 754 757 762 770 772 774 "
                        "781 786 802 803 804 810 813 814 828 835 838 843 845 848 854 856 857 860 862 863 864 878 904 "
                        "908 910 912 914 917 919 929 934 937 941 947 954 959 973 978 980 984 989",
    "America/Chicago": "205 210 214 217 218 219 224 225 228 251 254 256 262 269 281 309 312 314 316 318 319 320 "
                       "325 331 334 337 346 361 402 405 409 414 417 430 432 456 469 479 501 504 507 512 515 563 573 "
                       "580 601 605 608 612 615 618 620 630 636 641 651 660 662 682 701 708 712 713 715 726 731 737 "
                       "763 769 773 779 785 806 815 816 817 830 832 847 850 870 872 901 903 913 918 920 931 936 940 "
                       "952 956 972 979",
    "America/Denver": "303 307 385 406 435 505 575 719 720 801 970 983",
    "America/Phoenix": "480 520 602 623 928",
    "America/Los_Angeles": "206 209 213 253 279 310 323 341 360 408 415 424 425 442 503 509 510 530 541 559 562 564 "
                           "619 626 628 650 657 661 669 702 707 714 725 747 760 775 805 818 820 831 840 858 909 916 "
                           "925 949 951 971",
    "America/Anchorage": "907",
    "Pacific/Honolulu": "808",
}
AREA_ZONE = {code: z for z, codes in _AREA.items() for code in codes.split() if code.isdigit()}


def zone_fits_phone(tz_name: Optional[str], e164: Optional[str]) -> bool:
    if not tz_name or not e164 or not e164.startswith("+1"):
        return True
    return tz_name in NANP_ZONES


def _inside(now_utc: datetime, z: ZoneInfo) -> bool:
    return START_HOUR <= now_utc.astimezone(z).hour < END_HOUR


def allowed(now_utc: datetime, tz_name: Optional[str], e164: Optional[str] = None) -> Optional[bool]:
    """True inside the window, False outside, None when the time zone is unknown or contradicts the number (the
    caller refuses). For a +1 number the area code's zone (or, unknown, both Eastern and Pacific) must be inside too."""
    z = zone(tz_name)
    if z is None or not zone_fits_phone(tz_name, e164):
        return None
    zones = [z]
    if e164 and e164.startswith("+1") and len(e164) == 12:
        area = AREA_ZONE.get(e164[2:5])
        zones += [ZoneInfo(area)] if area else [ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles")]
    return all(_inside(now_utc, x) for x in zones)
