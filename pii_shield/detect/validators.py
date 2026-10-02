"""Structure and checksum checks. Each returns (delta, reason): they raise or lower confidence and
never veto on their own. Test-range values (SSN 9xx, 555 phones) that sit next to a label must
still be caught, because in production data they would be real identifiers."""
from __future__ import annotations

import re
from datetime import date

import phonenumbers

from ..config import PHONE_REGIONS

Result = tuple[float, str]


def digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def luhn_ok(number: str) -> bool:
    ds = [int(c) for c in digits(number)][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(ds))
    return len(ds) >= 12 and total % 10 == 0


def check_card(value: str) -> Result:
    d = digits(value)
    if not 13 <= len(d) <= 19:
        return -0.4, "wrong length for a card number"
    if len(set(d)) == 1:
        return -0.5, "repeated digit"
    ok = luhn_ok(d)
    iin = d[0] in "3456" or d[:2] in ("22", "27")
    if ok and iin:
        return 0.35, "Luhn checksum valid and known card prefix"
    if ok:
        return 0.2, "Luhn checksum valid"
    if len(d) in (15, 16):
        # A card-length number that fails Luhn is still some account or card identifier (or a
        # mistyped card): kept in the review band, redacted, never silently passed on.
        return 0.05, "Luhn checksum fails, but card-length number (review)"
    return -0.3, "Luhn checksum fails"


def check_ssn(value: str) -> Result:
    d = digits(value)
    if len(d) != 9:
        return -0.5, "not 9 digits"
    area, group, serial = d[:3], d[3:5], d[5:]
    if area in ("000", "666") or group == "00" or serial == "0000":
        return -0.3, "SSN area/group/serial never issued"
    if area.startswith("9"):
        return -0.1, "9xx area is ITIN/test range (kept when labelled)"
    return 0.1, "SSN area/group/serial plausible"


PAN_HOLDER = set("PCHFATBLJG")


def check_pan(value: str) -> Result:
    v = value.upper()
    if not re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", v):
        return -0.5, "not AAAAA9999A shape"
    if v[3] in PAN_HOLDER:
        return 0.2, f"4th letter '{v[3]}' is a valid PAN holder type"
    return -0.25, f"4th letter '{v[3]}' is not a PAN holder type"


PESEL_W = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)


def pesel_birth(d: str) -> date | None:
    yy, mm, dd = int(d[:2]), int(d[2:4]), int(d[4:6])
    century = {0: 1900, 20: 2000, 40: 2100, 60: 2200, 80: 1800}
    base = mm - mm % 20
    if base not in century:
        return None
    try:
        return date(century[base] + yy, mm - base, dd)
    except ValueError:
        return None


def check_pesel(value: str) -> Result:
    v = re.sub(r"\s", "", value)
    head = v[:6]
    if not head.isdigit() or pesel_birth(head) is None:
        return -0.4, "embedded birth date invalid"
    if re.fullmatch(r"\d{11}", v):
        check = (10 - sum(int(a) * b for a, b in zip(v[:10], PESEL_W)) % 10) % 10
        if check == int(v[10]):
            return 0.3, "PESEL checksum valid and birth date valid"
        return -0.1, "birth date valid but checksum fails"
    return 0.1, "partially masked PESEL: visible birth date is still PII"


def check_iban(value: str) -> Result:
    v = re.sub(r"\s", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", v):
        return -0.5, "not IBAN shape"
    n = int("".join(str(int(c, 36)) for c in v[4:] + v[:4]))
    return (0.35, "IBAN mod-97 valid") if n % 97 == 1 else (-0.3, "IBAN mod-97 fails")


def check_phone(value: str) -> Result:
    d = digits(value)
    if len(d) < 7 or len(d) > 15:
        return -0.4, "digit count outside phone range"
    if len(set(d)) <= 2:
        return -0.3, "too few distinct digits"
    v = value.strip()
    if DATE_SHAPE.fullmatch(v) or IP_SHAPE.fullmatch(v):
        return -0.4, "date- or IP-shaped, not a phone number"
    possible = False
    for region, bonus in _phone_regions(v, d):
        try:
            num = phonenumbers.parse(v, region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(num):
            return bonus, f"valid {phonenumbers.region_code_for_number(num) or region} number (libphonenumber)"
        possible = possible or phonenumbers.is_possible_number(num)
    if possible:
        return 0.1, "possible number (libphonenumber)"
    if "555" in d:
        return 0.0, "fictional 555 exchange (kept when labelled)"
    return -0.1, "not a recognised national format"


DATE_SHAPE = re.compile(r"\d{1,4}[./-]\d{1,2}[./-]\d{2,4}")
IP_SHAPE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")


def _phone_regions(value: str, d: str) -> list[tuple[str | None, float]]:
    """Which regions a number written without "+" can belong to, judged by its shape, so a
    landline from London, Warsaw or Mumbai is recognised without assuming the US. Each region
    carries the score a valid parse earns: national formats that are only a digit count (PL, 9
    digits, no trunk prefix) are weaker evidence and land in the review band."""
    if value.startswith("+"):
        return [(None, 0.25)]
    if d.startswith("0"):
        return [(r, 0.25) for r in PHONE_REGIONS["trunk_prefix"]]
    if len(d) == 10:
        return [(r, 0.25) for r in PHONE_REGIONS["ten_digit"]]
    if len(d) == 9:
        return [(r, 0.15) for r in PHONE_REGIONS["nine_digit"]]
    return [(r, 0.25) for r in PHONE_REGIONS["ten_digit"][:1]]


def check_date(value: str) -> Result:
    years = re.findall(r"(?:19|20)\d{2}", value)
    if years:
        y = int(years[-1])
        if y > date.today().year:
            return -0.4, "date in the future"
        if date.today().year - y < 14:
            return -0.15, "recent date, unlikely to be a birth date"
    return 0.0, ""


ROLE_MAILBOX = re.compile(
    r"^(?:info|office|team|support|help|helpdesk|servicedesk|admin|noreply|no-reply|security|privacy|compliance|"
    r"hr|it|legal|finance|procurement|audit|risk|tprm|grc|contact|sales|enquiries|inquiries|mailbox)(?:[._-]|$)"
    r"|[._-](?:office|team|support|desk|mailbox|group|noreply)$"
)


def check_mailbox(value: str) -> Result:
    """Shared/role mailboxes (tprm-office@, support@) identify a function, not a person."""
    local = value.split("@")[0].lower()
    if ROLE_MAILBOX.search(local):
        return -0.45, "role/shared mailbox, not a personal address (review)"
    return 0.0, ""


# Verhoeff tables (Aadhaar check digit)
_VD = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
       [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
       [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
       [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_VP = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
       [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
       [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def verhoeff_ok(number: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits(number))):
        c = _VD[c][_VP[i % 8][int(ch)]]
    return c == 0


def check_aadhaar(value: str) -> Result:
    d = digits(value)
    if len(d) != 12:
        return -0.5, "not 12 digits"
    if d[0] in "01":
        return -0.25, "starts with 0/1, never issued (kept when labelled)"
    if len(set(d)) <= 2:
        return -0.4, "too few distinct digits"
    if verhoeff_ok(d):
        return 0.3, "Aadhaar Verhoeff check digit valid"
    return -0.15, "Verhoeff check digit fails (kept when labelled)"


_GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def check_gstin(value: str) -> Result:
    v = value.upper()
    if not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", v):
        return -0.5, "not GSTIN shape"
    if not 1 <= int(v[:2]) <= 38 and v[:2] not in ("97", "99"):
        return -0.2, f"state code {v[:2]} unknown"
    total = 0
    for i, ch in enumerate(v[:14]):
        x = _GST_CHARS.index(ch) * (2 if i % 2 else 1)
        total += x // 36 + x % 36
    if _GST_CHARS[(36 - total % 36) % 36] == v[14]:
        return 0.3, "GSTIN check character valid (embeds a PAN)"
    return 0.0, "GSTIN shape with embedded PAN; check character fails"


def check_ip(value: str) -> Result:
    import ipaddress

    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return -0.6, "not a valid IP address"
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        return -0.3, "loopback / unspecified / multicast address"
    if ip.version == 4 and value.count(".") == 3 and all(len(p) == 1 for p in value.split(".")):
        return -0.15, "short dotted number (could be a version string)"
    return (0.0, "private-range address") if ip.is_private else (0.1, "public IP address")
