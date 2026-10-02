"""Tunable settings. Everything a reviewer might question lives here, in one place."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Settings:
    # --- extraction ---------------------------------------------------------------
    ocr_engine: str = "auto"  # auto | rapidocr | tesseract
    ocr_dpi: int = 300  # page render resolution for scanned PDFs
    region_upscale: float = 2.0  # screenshots / tables inside scans are re-read at this scale
    detect_regions: bool = True  # find tables and screenshots inside scanned pages
    ocr_embedded_images: bool = True  # OCR images embedded in DOCX / PPTX / PDF
    min_image_px: int = 120  # skip icons and logos smaller than this on both sides

    # --- detection ----------------------------------------------------------------
    use_spacy: bool = True
    spacy_model: str = "en_core_web_lg"
    use_gliner: bool = False  # needs `pip install gliner` (pulls torch)
    gliner_model: str = "knowledgator/gliner-pii-base-v1.0"
    gliner_threshold: float = 0.45
    propagate_persons: bool = True
    context_window: int = 60  # characters of neighbouring text given to recognisers

    # --- routing ------------------------------------------------------------------
    redact_threshold: float = 0.60  # >= this: redact automatically
    review_threshold: float = 0.35  # between review and redact: redact AND queue for review (fail closed)
    low_conf_ocr: float = 0.60  # OCR words below this that look like identifiers are masked
    low_conf_image_ocr: float = 0.80  # stricter floor for text read from screenshots / embedded images
    withhold_low_conf_images: bool = True  # text from images OCR'd below low_conf_ocr never reaches the LLM

    # --- outputs ------------------------------------------------------------------
    # The token vault is saved only when this is set, and only encrypted. Kept out of repr so it
    # never lands in a log or traceback.
    vault_passphrase: str | None = field(default=None, repr=False)

    # --- vocabularies -------------------------------------------------------------
    allow_list: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOW_LIST))
    extra_deny_list: list[str] = field(default_factory=list)  # names to always redact


# Organisation, product, system and role names that must never be treated as people.
DEFAULT_ALLOW_LIST = [
    "Cadence", "Cadence Design Systems", "Optiv", "OneTrust", "ServiceNow", "Archer", "Microsoft",
    "Microsoft Teams", "SharePoint", "Outlook", "Excel", "Word", "PowerPoint", "Azure", "AWS", "Google",
    "Okta", "Workday", "Salesforce", "SAP", "Oracle", "Jira", "Confluence", "Slack", "Zoom", "Acme",
    "AcmeCo", "Vendor Tier", "Risk Owner", "Control Owner", "Process Owner", "Business Owner",
    "Data Owner", "Risk Manager", "Risk Committee", "Audit Committee", "Board", "Board of Directors",
    "Internal Audit", "Compliance", "Legal", "Procurement", "Finance", "Human Resources", "HR",
    "Information Security", "InfoSec", "IT", "CISO", "CIO", "CEO", "CFO", "COO", "CRO", "CTO", "DPO",
    "GRC", "TPRM", "RCSA", "KRI", "KPI", "BCP", "DR", "SOC", "ISO", "NIST", "GDPR", "SOX", "PCI DSS",
    "Appendix", "Section", "Policy", "Procedure", "Standard", "Guideline", "Framework", "Register",
    "Questionnaire", "Assessment", "Inherent Risk", "Residual Risk", "Risk Appetite", "Risk Register",
    "Third Party", "Third-Party", "Vendor", "Supplier", "Engagement", "Dashboard", "Admin", "Administrator",
]

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
    onboarding offboarding member members administration approver reviewers
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
}
