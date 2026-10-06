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
# Where each NANP geographic area code's numbers are (S2-L1, S3-L1, S4-M1, S4-L1), built from the NANPA geographic
# NPA list by state / province / territory. An area code that spans time zones lists EVERY zone it covers, so the
# window must hold in all of them AND in the recorded zone: the stricter always wins. Overlays carry the zones of
# the area they overlay. Non-geographic codes (N11, 456, 5XX, 600, 700, 710, 8XX toll-free, 900) are never listed.
# An area code not listed is refused (phone_problem).
ET, CT, MT, PT = "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"
AZ, AK, ADAK, HI = "America/Phoenix", "America/Anchorage", "America/Adak", "Pacific/Honolulu"
_GROUPS = [
    # ---- United States, single zone
    ((ET,), "201 202 203 207 212 215 216 220 223 227 229 234 239 240 248 252 260 267 272 276 283 301 302 304 305 "
            "313 315 317 321 324 326 329 330 332 336 339 347 351 352 363 380 386 401 404 407 410 412 413 419 434 436 "
            "440 443 445 463 470 472 475 478 484 502 508 513 516 517 518 540 551 561 567 570 571 582 585 586 603 "
            "607 609 610 614 616 617 624 631 640 645 646 656 667 678 679 680 681 686 689 703 704 706 716 717 718 "
            "724 727 728 732 734 740 743 754 757 762 765 770 771 772 774 781 786 802 803 804 810 813 814 821 826 "
            "828 835 838 839 843 845 848 854 856 857 859 860 862 863 864 865 878 904 908 910 912 914 917 919 929 "
            "934 937 941 943 947 948 954 959 973 978 980 984 989"),
    ((CT,), "205 210 214 217 218 224 225 228 235 251 254 256 262 274 281 309 312 314 316 318 319 320 325 327 331 "
            "337 346 353 361 402 405 409 414 417 430 447 464 469 479 501 504 507 512 515 531 534 539 557 563 572 "
            "573 601 608 612 615 618 629 630 636 641 651 659 660 662 682 708 712 713 715 726 730 731 737 763 769 "
            "773 779 806 815 816 817 830 832 847 861 870 872 901 903 913 918 920 924 936 938 940 945 952 956 972 "
            "975 979 985"),
    ((MT,), "303 307 385 406 435 505 575 719 720 801 970 983"),
    ((AZ,), "480 520 602 623"),
    ((PT,), "206 209 213 253 279 310 323 341 350 357 360 369 408 415 424 425 442 509 510 530 559 562 564 619 626 "
            "503 628 650 657 661 669 702 707 714 725 738 747 760 805 818 820 831 837 840 858 909 916 925 949 951 971"),
    ((HI,), "808"),
    # ---- United States, area codes that span two or more zones (S4-M1): every zone they cover
    ((ET, CT), "219 270 364 423 574 606 812 930 850 448 906 931 334 483"),   # FL panhandle, IN, KY, TN, MI UP, AL (Phenix City)
    ((AZ, MT), "928"),                           # Navajo Nation keeps daylight time
    ((PT, MT), "541 458 775"),                   # Oregon (Malheur), Nevada (West Wendover)
    ((MT, PT), "208 986"),                       # Idaho (north is Pacific)
    ((CT, MT), "308 605 701 785 620 915 432 580"),   # NE, SD, ND, KS, TX (El Paso, Culberson), OK (Kenton)
    ((AK, ADAK), "907"),
    # ---- Canada
    ((PT,), "604"),
    (("America/Vancouver", "America/Edmonton", "America/Dawson_Creek"), "236 250 257 672 778"),
    (("America/Edmonton",), "368 403 587 780 825"),
    (("America/Regina", "America/Edmonton"), "306 474 639"),
    (("America/Winnipeg",), "204 431 584"),
    (("America/Toronto",), "226 249 263 289 343 354 365 382 387 416 437 438 450 468 514 519 548 579 613 647 683 "
                           "705 742 753 819 873 905 942"),
    (("America/Toronto", "America/Halifax"), "367 418 581"),           # Quebec: Magdalen Islands, Blanc-Sablon
    (("America/Toronto", "America/Winnipeg"), "807"),
    (("America/Halifax",), "428 506 782 902"),
    (("America/St_Johns", "America/Goose_Bay"), "709 879"),
    (("America/Whitehorse", "America/Edmonton", "America/Winnipeg", "America/Toronto"), "867"),
    # ---- US territories and the NANP Caribbean
    (("America/Puerto_Rico",), "787 939"), (("America/St_Thomas",), "340"), (("Pacific/Guam",), "671"),
    (("Pacific/Saipan",), "670"), (("Pacific/Pago_Pago",), "684"),
    (("America/Nassau",), "242"), (("America/Barbados",), "246"), (("America/Anguilla",), "264"),
    (("America/Antigua",), "268"), (("America/Tortola",), "284"), (("America/Cayman",), "345"),
    (("Atlantic/Bermuda",), "441"), (("America/Grenada",), "473"), (("America/Grand_Turk",), "649"),
    (("America/Jamaica",), "658 876"), (("America/Montserrat",), "664"), (("America/Lower_Princes",), "721"),
    (("America/St_Lucia",), "758"), (("America/Dominica",), "767"), (("America/St_Vincent",), "784"),
    (("America/Santo_Domingo",), "809 829 849"), (("America/Port_of_Spain",), "868"), (("America/St_Kitts",), "869"),
]
AREA_ZONES: dict[str, tuple] = {}
for _zones, _codes in _GROUPS:
    for _code in _codes.split():
        if _code.isdigit():
            AREA_ZONES[_code] = tuple(dict.fromkeys(AREA_ZONES.get(_code, ()) + _zones))   # a code listed twice: all
AREA_ZONE = {k: v[0] for k, v in AREA_ZONES.items()}


def phone_problem(e164: Optional[str]) -> Optional[str]:
    """For SMS and voice (AEGIS S3-M1, S3-L1): only +1 numbers of exactly 12 characters whose area code is in the
    table. An unknown area code is REFUSED: the fallback would be the intersection of 08:00-21:00 across every NANP
    zone, and from Guam (UTC+10) to Newfoundland (UTC-2:30) that leaves at most about 90 minutes a day (22:00-23:30
    UTC in summer) — too small to be a usable window, so it is not offered."""
    if not e164 or not e164.startswith("+1"):
        return "NON_NANP_NOT_SUPPORTED"
    if len(e164) != 12 or not e164[1:].isdigit():
        return "PHONE_INVALID"
    if e164[2:5] not in AREA_ZONES:
        return "AREA_CODE_UNKNOWN"
    return None


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
    if e164 and e164.startswith("+1"):
        if phone_problem(e164):
            return None                       # S3-L1: malformed or unknown area code -> refused, never a guess
        zones += [ZoneInfo(x) for x in AREA_ZONES[e164[2:5]]]
    return all(_inside(now_utc, x) for x in zones)
