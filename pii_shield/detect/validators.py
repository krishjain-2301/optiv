"""Structure and checksum checks. Each returns (delta, reason): they raise or lower confidence and
never veto on their own. Test-range values (SSN 9xx, 555 phones) that sit next to a label must
still be caught, because in production data they would be real identifiers."""
from __future__ import annotations

import re
from datetime import date

import phonenumbers

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
    region = None if value.strip().startswith("+") else "US"
    try:
        num = phonenumbers.parse(value, region)
        if phonenumbers.is_valid_number(num):
            return 0.25, f"valid {phonenumbers.region_code_for_number(num) or ''} number (libphonenumber)"
        if phonenumbers.is_possible_number(num):
            return 0.1, "possible number (libphonenumber)"
    except phonenumbers.NumberParseException:
        pass
    if "555" in d:
        return 0.0, "fictional 555 exchange (kept when labelled)"
    return -0.1, "not a recognised national format"


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
