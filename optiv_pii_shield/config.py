"""Tunable settings. Everything a reviewer might question lives here, in one place."""
from __future__ import annotations

import getpass
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .modelstore import GLINER_REVISION


@dataclass
class Settings:
    # --- extraction ---------------------------------------------------------------
    ocr_engine: str = "auto"  # auto | rapidocr | tesseract
    ocr_dpi: int = 300  # page render resolution for scanned PDFs
    region_upscale: float = 2.0  # screenshots / tables inside scans are re-read at this scale
    detect_regions: bool = True  # find tables and screenshots inside scanned pages
    ocr_embedded_images: bool = True  # OCR images embedded in DOCX / PPTX / PDF
    min_image_px: int = 120  # skip icons and logos smaller than this on both sides
    detect_faces: bool = True  # faces in pictures and scans are blanked in the masked copy
    detect_qr: bool = True  # QR codes encode text nobody reads by eye: blanked too
    face_threshold: float = 0.8
    max_pages: int = 2000  # a PDF with more pages is refused (resource limit)

    # --- detection ----------------------------------------------------------------
    use_spacy: bool = True
    spacy_model: str = "en_core_web_lg"
    use_gliner: bool = False  # needs `pip install gliner` (pulls torch)
    gliner_model: str = "knowledgator/gliner-pii-base-v1.0"
    gliner_revision: str = GLINER_REVISION  # pinned snapshot of the weights
    gliner_threshold: float = 0.45
    propagate_persons: bool = True
    context_window: int = 60  # characters of neighbouring text given to recognisers

    # --- routing ------------------------------------------------------------------
    redact_threshold: float = 0.60  # >= this: redact automatically
    review_threshold: float = 0.35  # between review and redact: redact AND queue for review (fail closed)
    low_conf_ocr: float = 0.60  # OCR words below this that look like identifiers are masked
    low_conf_image_ocr: float = 0.80  # stricter floor for text read from screenshots / embedded images
    withhold_low_conf_images: bool = True  # text from images OCR'd below low_conf_ocr never reaches the LLM

    # --- redaction ----------------------------------------------------------------
    profile: str = "default"  # what replaces each category: see PROFILES
    # Pictures nobody could read (photos, signatures, logos without text, images left un-OCR'd)
    # are blanked in the masked copy: an image that was not read is not trusted.
    blank_textless_images: bool = True
    min_blank_px: int = 32  # bullets and icons smaller than this on a side are left alone
    # After masking, every masked page and picture is OCR'd again and searched for the vault's
    # values; a value still readable is covered, and the file is withheld if it cannot be.
    verify_outputs: bool = True
    # With a key, tokens are derived from the value (HMAC-SHA256) and so are the same in every
    # run; without one they are numbered per run. Kept out of repr like the passphrase.
    token_key: str | None = field(default=None, repr=False)

    # --- outputs ------------------------------------------------------------------
    operator: str = field(default_factory=lambda: _os_user())  # recorded in the manifest and audit log
    # The token vault is saved only when this is set, and only encrypted. Kept out of repr so it
    # never lands in a log or traceback.
    vault_passphrase: str | None = field(default=None, repr=False)

    # --- vocabularies -------------------------------------------------------------
    allow_list: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOW_LIST))
    extra_deny_list: list[str] = field(default_factory=lambda: list(ORG["deny_list"]))  # names to always redact
    # Project codenames, product names, internal hosts: not personal data, redacted all the same.
    confidential_terms: list[str] = field(default_factory=lambda: list(ORG["confidential_terms"]))


ACTIONS = ("block", "warn", "allow")


@dataclass
class GuardPolicy:
    """What the prompt guard does with a prompt beyond replacing values (guard.py). Personal data
    and credentials are always replaced; this decides when the whole prompt is refused instead."""

    source_code: str = "block"  # block | warn | allow: a prompt that is source code
    markings: str = "block"  # block | warn | allow: a prompt carrying a classification marking
    block_categories: list[str] = field(default_factory=list)  # a value of these categories refuses the prompt
    max_bytes: int = 0  # a larger prompt is refused unread; 0: no limit

    def __post_init__(self):
        for name in ("source_code", "markings"):
            if getattr(self, name) not in ACTIONS:
                raise ValueError(f"guard policy: {name} must be one of {', '.join(ACTIONS)}")
        if self.max_bytes < 0:
            raise ValueError("guard policy: max_bytes cannot be negative")

    @classmethod
    def default(cls) -> "GuardPolicy":
        """The organisation's policy (org.yaml: guard_policy), over the defaults above."""
        return cls(**ORG["guard_policy"])


def _os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def load_org_config(path: str | Path | None = None) -> dict:
    """Organisation-specific vocabulary (allow-list, deny-list, internal ID formats, confidential
    terms, classification markings) and its prompt-guard policy, from YAML:
    ``path``, else $PII_SHIELD_ORG_CONFIG, else the bundled optiv_pii_shield/data/org.yaml."""
    p = Path(path or os.environ.get("PII_SHIELD_ORG_CONFIG") or Path(__file__).parent / "data" / "org.yaml")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    for i, rule in enumerate(data.get("id_patterns") or []):
        missing = {"name", "entity", "pattern", "score"} - set(rule)
        if missing:
            raise ValueError(f"{p}: id_patterns[{i}] is missing {sorted(missing)}")
    data.setdefault("allow_list", [])
    data.setdefault("deny_list", [])
    data.setdefault("id_patterns", [])
    for key in ("confidential_terms", "markings"):
        data[key] = [str(x) for x in data.get(key) or []]
    data["guard_policy"] = dict(data.get("guard_policy") or {})
    unknown = set(data["guard_policy"]) - {"source_code", "markings", "block_categories", "max_bytes"}
    if unknown:
        raise ValueError(f"{p}: guard_policy has unknown keys {sorted(unknown)}")
    data["path"] = str(p)
    return data


ORG = load_org_config()
# Organisation, product, system and role names that must never be treated as people.
DEFAULT_ALLOW_LIST: list[str] = list(ORG["allow_list"])

# Words that look like names to a statistical model but are business vocabulary.
NOT_A_NAME_WORDS = {
    w.lower()
    for w in """
    risk risks tier vendor vendors owner owners control controls policy policies procedure process
    assessment assessments register review reviewer approval approver approved status level high medium
    low critical inherent residual appetite committee board audit compliance security privacy data
    information management manager director officer lead head team group department function unit
    engagement questionnaire dashboard report reports reporting template form field fields admin
    administrator user users role roles access account accounts system systems application platform
    tool module workflow task tasks issue issues finding findings exception exceptions action actions
    plan plans remediation mitigation monitoring metric metrics indicator indicators key performance
    third party parties supplier suppliers contract contracts service services business operations
    operational strategic financial legal regulatory reputational technology cyber incident incidents
    escalation matrix chart org organisation organization structure appendix section page table figure
    version date owner's due total score rating category categories type types name names id ids
    number email phone mobile address title department location country region global local
    annual quarterly monthly weekly daily new open closed pending draft final active inactive yes no
    n/a tbd na none other misc general overview summary introduction purpose scope objective objectives
    responsibilities responsible accountable consulted informed raci signature signed attestation
    training personnel office theme record records identity contacts narrative case module appendix
    onboarding offboarding member members administration approver reviewers internal external
    """.split()
}

# Column headers / field labels -> the PII category their values carry (layer L3).
HEADER_CATEGORIES: list[tuple[str, str]] = [
    (r"e-?mail|email address|mail id", "EMAIL_ADDRESS"),
    (r"phone|mobile|tel\b|telephone|cell|contact no|contact number|ext\.?\b", "PHONE_NUMBER"),
    (r"date of birth|\bdob\b|birth ?date|born", "DATE_OF_BIRTH"),
    (r"\bssn\b|social security", "US_SSN"),
    (r"passport", "PASSPORT"),
    (r"\bpan\b(?! ?card)|permanent account", "IN_PAN"),
    (r"pesel", "PL_PESEL"),
    (r"aadhaa?r|\buid\b", "IN_AADHAAR"),
    (r"\bip\b|ip address", "IP_ADDRESS"),
    (r"api ?key|secret|password|passwd|\btoken\b|credential", "CREDENTIAL"),
    (r"\btin\b|tax ?id|taxpayer|\bnip\b|\bein\b|gstin|\bgst\b|\bvat\b", "TAX_ID"),
    (r"bank account|account (?:no|number|#)|a/c ?(?:no|number)?\b|\bifsc\b|sort code|routing", "BANK_ACCOUNT"),
    (r"\bupi\b|\bvpa\b", "UPI_ID"),
    (r"driving licen[cs]e|driver'?s? licen[cs]e|\bdl (?:no|number)", "DRIVING_LICENCE"),
    (r"voter|\bepic\b|election card", "IN_VOTER_ID"),
    (r"national insurance|\bnino\b|\bni (?:no|number)", "UK_NINO"),
    (r"blood (?:group|type)|diagnosis|medical condition|disabilit|allerg", "HEALTH_DATA"),
    (r"national id|nat\.? id|national identifier|id number|identity (?:no|number)", "NATIONAL_ID"),
    (r"employee id|emp(?:loyee)? ?(?:no|#|number)|staff id|badge", "EMPLOYEE_ID"),
    (r"(?:home |postal |residential |mailing )?address", "ADDRESS"),
    (
        r"\bname\b|full name|employee\b|owner|approver|approved by|prepared by|reviewed by|author|"
        r"signed by|signatory|contact person|\bcontact\b|assignee|assigned to|requester|requestor|"
        r"reviewer|attendee|member|manager name|person|staff|user ?name|display name|"
        r"last.?modified.?by|lastmodifiedby|creator|^manager$|\blead\b|champion|delegate|escalat",
        "PERSON",
    ),
]

# Exposure score (exposure.py). Sensitivity of one instance, 1-10: harm if it reached the wrong
# party. Government and financial identifiers and credentials are at the top; a work e-mail or a
# name alone is low but not zero.
SENSITIVITY: dict[str, float] = {
    "CREDENTIAL": 10, "US_SSN": 10, "PASSPORT": 10, "NATIONAL_ID": 10, "IN_AADHAAR": 10, "PL_PESEL": 10,
    "CREDIT_CARD": 9, "IBAN_CODE": 8, "IN_PAN": 8, "TAX_ID": 7, "DATE_OF_BIRTH": 6, "ADDRESS": 5,
    "PHONE_NUMBER": 4, "EMAIL_ADDRESS": 4, "PERSON": 3, "IP_ADDRESS": 3, "EMPLOYEE_ID": 3, "VENDOR_ID": 1,
    "BANK_ACCOUNT": 8, "UPI_ID": 5, "DRIVING_LICENCE": 9, "IN_VOTER_ID": 9, "UK_NINO": 10, "HEALTH_DATA": 9,
    "CONFIDENTIAL_TERM": 7, "LOW_CONFIDENCE_OCR": 2, "_default": 3,
}
# Special categories (GDPR Art. 9, "sensitive personal data"): flagged in reports.
SPECIAL_CATEGORIES = {"HEALTH_DATA"}

# Redaction profiles: what replaces a value of each category in every output.
#   token  a stable pseudonym, reversible through the vault            [PERSON_007]
#   mask   the category only: nothing of the value, not reversible      [CARD]
#   last4  the category and the last four digits                        [CARD_****2218]
#   year   the category and the year (a date of birth generalised)      [DOB_1985]
# Categories a profile does not name are tokenised.
PROFILES: dict[str, dict] = {
    "default": {"label": "Tokenise everything", "actions": {}},
    "gdpr": {"label": "GDPR: data minimisation",
             "actions": {"DATE_OF_BIRTH": "year", "HEALTH_DATA": "mask", "CREDENTIAL": "mask"}},
    "dpdp": {"label": "India DPDP Act: masked Aadhaar",
             "actions": {"IN_AADHAAR": "last4", "DATE_OF_BIRTH": "year", "HEALTH_DATA": "mask", "CREDENTIAL": "mask"}},
    "pci": {"label": "PCI DSS: card numbers to last four",
            "actions": {"CREDIT_CARD": "last4", "BANK_ACCOUNT": "last4", "IBAN_CODE": "last4", "CREDENTIAL": "mask"}},
}
EXPOSURE_RATING = {"critical_weight": 9, "high_density": 25.0, "medium_density": 5.0}  # density = score per 1k words
# Share of instances the detectors miss, by (category, source), measured on the held-out test seed
# (2026-10-02, en_core_web_lg). Categories with no observed miss use the rule-of-three upper bound
# 3/n over all structured instances (104). Re-measure after detection changes.
MISS_RATES: dict = {
    ("PERSON", "native"): 25 / 115,
    ("PERSON", "ocr"): 6 / 15,
    "_default": 3 / 104,
    "_basis": "held-out test seed 2026-10-02: PERSON 25/115 missed (OCR-noise names 6/15); "
              "structured identifiers 0/104 missed, rule-of-three bound 3/104 used",
}

# Regions tried for phone numbers written without "+", by the shape of the number (validators.py).
PHONE_REGIONS: dict[str, list[str]] = {
    "trunk_prefix": ["GB", "IN", "DE", "FR", "IT", "NL", "IE", "AU", "ES"],  # national format starts with 0
    "ten_digit": ["US", "IN"],  # 10 digits, no 0: NANP or Indian mobile
    "nine_digit": ["PL"],  # 9 digits, no trunk prefix
}

# Words that, when near a candidate, raise confidence for that category (layer L1 context boost).
CONTEXT_WORDS: dict[str, list[str]] = {
    "US_SSN": ["ssn", "social security", "social sec", "ss#", "ss no"],
    "PASSPORT": ["passport", "passport no", "passport number", "travel document"],
    "IN_PAN": ["pan", "permanent account", "income tax", "pan no", "pan card"],
    "PL_PESEL": ["pesel", "national id", "polish id", "nat. id"],
    "TAX_ID": ["tin", "tax id", "taxpayer", "nip", "ein", "tax identification", "vat"],
    "CREDIT_CARD": ["card", "credit", "debit", "visa", "mastercard", "amex", "pan", "ending", "cardholder"],
    "PHONE_NUMBER": ["phone", "mobile", "tel", "telephone", "cell", "call", "contact", "fax", "ext", "whatsapp"],
    "DATE_OF_BIRTH": ["dob", "d.o.b", "date of birth", "born", "birth date", "birthdate", "birthday", "age"],
    "EMPLOYEE_ID": ["employee", "emp id", "staff", "badge", "personnel", "id"],
    "IBAN_CODE": ["iban", "account", "bank"],
    "ADDRESS": ["address", "resides", "lives at", "home", "residence", "street", "postal"],
    "NATIONAL_ID": ["national id", "id number", "identity", "citizen"],
    "IN_AADHAAR": ["aadhaar", "aadhar", "uid", "uidai", "unique id"],
    "IP_ADDRESS": ["ip", "ip address", "host", "server", "client", "source", "login from"],
    "CREDENTIAL": ["key", "api key", "apikey", "secret", "token", "password", "passwd", "pwd", "credential"],
    "BANK_ACCOUNT": ["account", "a/c", "acct", "bank", "ifsc", "sort code", "routing", "beneficiary", "savings", "current account"],
    "UPI_ID": ["upi", "vpa", "gpay", "phonepe", "paytm", "bhim"],
    "DRIVING_LICENCE": ["driving licence", "driving license", "driver's license", "drivers license", "licence no", "license no", "dl no", "dl"],
    "IN_VOTER_ID": ["voter", "epic", "election"],
    "UK_NINO": ["national insurance", "nino", "ni number", "ni no"],
    "HEALTH_DATA": ["blood", "blood group", "blood type"],
}
