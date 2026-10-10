"""Layer L1: pattern + checksum + context rules, packaged as a Presidio ``EntityRecognizer``.

Scoring is explicit and explainable: base score for the pattern, plus or minus the validator
result, plus a context boost when a keyword for that category is nearby. Every result carries
the list of reasons that produced its score.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from presidio_analyzer import AnalysisExplanation, EntityRecognizer, RecognizerResult

from ..config import CONTEXT_WORDS, ORG
from . import secrets as sec
from . import validators as v
from .names import gazetteer_names

MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
STREET = (r"(?:Street|St\.?|Road|Rd\.?|Avenue|Ave\.?|Lane|Ln\.?|Drive|Dr\.?|Boulevard|Blvd\.?|Way|Court|Ct\.?|"
          r"Place|Pl\.?|Terrace|Close|Crescent|Square|Sq\.?|Parkway|Pkwy\.?|Highway|Hwy\.?|Row|Mews|Gardens|Ulica|ul\.)")


@dataclass
class Rule:
    name: str
    entity: str
    pattern: str
    score: float
    validator: Optional[Callable[[str], tuple[float, str]]] = None
    context: tuple[str, ...] = ()
    requires_context: bool = False  # without a context word the score is capped below the review band
    group: int | str = 0
    flags: int = 0
    min_digits: int = 0
    unless: str = ""  # skip matches overlapping a match of this (stricter) rule
    accept: Optional[Callable[[re.Match], bool]] = None  # a check on the whole match, e.g. the name beside the value

    def __post_init__(self):
        self.regex = re.compile(self.pattern, self.flags)


def ctx(cat: str) -> tuple[str, ...]:
    return tuple(CONTEXT_WORDS.get(cat, ()))


RULES: list[Rule] = [
    # "scheme://user:password@host": first, so that "password@host" is not read as an e-mail address.
    Rule("secret_url_password", "CREDENTIAL",
         r"\b[A-Za-z][A-Za-z0-9+.-]{1,20}://[^\s:/@\"'<>]{1,64}:([^\s@/\"'<>]{3,128})@(?=[A-Za-z0-9\[])", 0.85,
         sec.check_secret_value, group=1),
    Rule("email", "EMAIL_ADDRESS", r"(?<![\w.+-])[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])", 0.92,
         v.check_mailbox, unless="secret_url_password"),
    # OCR damages e-mails in screenshots ("wilsong@acmeco-p com"): anything around an @ is suspect.
    Rule("email_ocr_damaged", "EMAIL_ADDRESS", r"(?<![\w.+-])[A-Za-z0-9._%+-]{2,} ?@ ?[A-Za-z0-9-]{2,}(?:[., -]{1,2}[A-Za-z0-9-]{2,}){0,3}", 0.55,
         unless="email"),  # fallback for OCR damage only; never crosses a line
    Rule("phone", "PHONE_NUMBER",
         # A leading "+" may follow a letter: OCR glues names to numbers ("Whitfield+1(212)555-0147").
         r"(?:(?<![0-9/+-])\+\d{1,3}[\s.-]?|(?<![\w/+-]))(?:\(\d{1,5}\)[\s.-]?)?\d{1,12}(?:[\s.-]\d{1,8}){0,5}(?![\w/-])",
         0.40, v.check_phone, ctx("PHONE_NUMBER"), min_digits=7),
    # Organisation-specific identifier formats come from optiv_pii_shield/data/org.yaml (id_patterns).
    *[Rule(r["name"], r["entity"], r["pattern"], float(r["score"]), None, ctx(r["entity"])) for r in ORG["id_patterns"]],
    Rule("us_ssn", "US_SSN", r"(?<![\d-])\d{3}[- ]\d{2}[- ]\d{4}(?![\d-])", 0.45, v.check_ssn, ctx("US_SSN")),
    Rule("passport", "PASSPORT", r"\b[A-Z]{1,2}\d{6,8}\b", 0.20, None, ctx("PASSPORT"), requires_context=True),
    Rule("in_pan", "IN_PAN", r"\b[A-Z]{5}\d{4}[A-Z]\b", 0.45, v.check_pan, ctx("IN_PAN")),
    Rule("pl_pesel", "PL_PESEL", r"(?<!\d)\d{6}(?:\d{5}|[xX*•]{5})(?![\dxX*])", 0.35, v.check_pesel, ctx("PL_PESEL")),
    Rule("us_ein", "TAX_ID", r"(?<![\d-])\d{2}-\d{7}(?![\d-])", 0.25, None, ctx("TAX_ID"), requires_context=True),
    Rule("pl_nip", "TAX_ID", r"(?<![\d-])\d{3}-\d{3}-\d{2}-\d{2}(?![\d-])", 0.25, None, ctx("TAX_ID"), requires_context=True),
    Rule("credit_card", "CREDIT_CARD", r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])", 0.35, v.check_card, ctx("CREDIT_CARD")),
    Rule("card_last4", "CREDIT_CARD",
         r"(?i:\bending(?:\s+in)?|\bends\s+in|\blast\s+(?:4|four)(?:\s+digits)?|x{4}|\*{4}|•{4})[\s:#-]{0,4}(\d{4})\b", 0.62,
         group=1),
    # "the last four digits of your social security number (4417)": the number is named, then quoted.
    Rule("ssn_last4", "US_SSN",
         r"(?i:\blast\s+(?:4|four)(?:\s+digits)?\s+of\s+(?:[a-z']+\s+){0,3}(?:social\s+security(?:\s+number)?|ssn|ss#))"
         r"[\s:#(\"'-]{0,5}(?:is\s+|are\s+)?[(\"']?(\d{4})\b", 0.7, group=1),
    # A title in front of a capitalised word is a person, with no first name to go by ("Dear Ms Varga-Lindt").
    Rule("honorific_name", "PERSON",
         r"\b(?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof|Shri|Smt)\.?[ \u00a0]([A-Z][^\W\d_]+(?:[-'’][A-Z][^\W\d_]+)*"
         r"(?:[ \u00a0][A-Z][^\W\d_]+(?:[-'’][A-Z][^\W\d_]+)*){0,2})", 0.7, group=1),
    Rule("in_aadhaar", "IN_AADHAAR", r"(?<![\d-])\d{4}([ -]?)\d{4}\1\d{4}(?![\d-])", 0.40, v.check_aadhaar,
         ctx("IN_AADHAAR")),
    Rule("in_gstin", "TAX_ID", r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b", 0.55, v.check_gstin, ctx("TAX_ID")),
    Rule("ipv4", "IP_ADDRESS", r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d|\.\d)",
         0.55, v.check_ip, ctx("IP_ADDRESS")),
    Rule("ipv6", "IP_ADDRESS", r"(?<![\w:])(?=[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:)(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])",
         0.55, v.check_ip, ctx("IP_ADDRESS")),
    # Credentials are not personal data, but they must never reach a model either.
    Rule("secret_known", "CREDENTIAL",
         r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"
         r"|\bxox[abposr]-[A-Za-z0-9-]{10,}\b|\b[sr]k[_-](?:live|test)[_-][A-Za-z0-9]{16,}\b|\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}"
         r"|\bAIza[0-9A-Za-z_-]{35}\b|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
         r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----", 0.9),
    Rule("secret_assigned", "CREDENTIAL",
         r"(?i:\b(?:api[_ -]?key|secret(?:[_ -]?key)?|access[_ -]?token|auth[_ -]?token|bearer|password|passwd|pwd|client[_ -]?secret)"
         # the value stops before a full stop that ends the sentence ("the password is Hunter2!Hunter2.")
         r"[\"']?\s*(?:[:=]|is)\s*[\"']?)([^\s\"',;]{8,}?)(?=\.?(?:[\s\"',;]|$))", 0.75, sec.check_secret_value, group=1),
    Rule("secret_known_more", "CREDENTIAL",
         r"\bglpat-[A-Za-z0-9_-]{20,}|\bnpm_[A-Za-z0-9]{36}\b|\bpypi-[A-Za-z0-9_-]{50,}|\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}"
         r"|\bhf_[A-Za-z0-9]{30,}\b|\bshp(?:at|ca|pa|ss)_[A-Fa-f0-9]{32}\b|\bdop_v1_[a-f0-9]{64}\b|\bya29\.[A-Za-z0-9_-]{20,}"
         r"|https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+|(?<!\d)\d{8,10}:AA[A-Za-z0-9_-]{33}(?![\w-])"
         r"|(?<=AccountKey=)[A-Za-z0-9+/]{40,}={0,2}"
         r"|-----BEGIN PGP PRIVATE KEY BLOCK-----[\s\S]+?(?:-----END PGP PRIVATE KEY BLOCK-----|\Z)"
         r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=\s]+\Z", 0.9),
    # Source code and configuration: the value is a secret because of the name it is assigned to
    # (secrets.py), or because of where it sits (a URL's password, an Authorization header).
    Rule("secret_in_code", "CREDENTIAL",
         r"(?<![A-Za-z0-9_.-])(?P<k>[A-Za-z_][A-Za-z0-9_.-]{1,60})[\"']?\]?\s*(?:=>|:=|[:=])\s*"
         r"(?P<q>[\"'`])(?P<v>(?:(?!(?P=q)).){4,200})(?P=q)", 0.6, sec.check_secret_value, group="v", accept=sec.named_secret),
    Rule("secret_env", "CREDENTIAL",
         r"(?m)^[ \t]*(?:export[ \t]+|set[ \t]+)?(?P<k>[A-Za-z_][A-Za-z0-9_]{2,60})[ \t]*=[ \t]*(?P<v>[^\s\"'`#;]{6,200})[ \t]*$",
         0.55, sec.check_secret_value, group="v", accept=sec.named_secret),
    Rule("secret_auth_header", "CREDENTIAL",
         r"(?i:\b(?:authorization|x-api-key|x-auth-token|api-key)\b[\"']?\s*[:=]\s*[\"']?(?:(?:bearer|basic|token)\s+)?)"
         r"([A-Za-z0-9._~+/=-]{12,})", 0.8, sec.check_secret_value, group=1),
    Rule("secret_bearer", "CREDENTIAL", r"\b[Bb]earer\s+([A-Za-z0-9._~+/=-]{20,})", 0.7, sec.check_secret_value, group=1),
    # A quoted random-looking string with no name to go by: review band, unless a context word confirms it.
    Rule("secret_high_entropy", "CREDENTIAL", r"(?P<q>[\"'`])(?P<v>[A-Za-z0-9+/_=-]{24,200})(?P=q)", 0.3, sec.check_entropy,
         ctx("CREDENTIAL"), group="v", accept=sec.mixed),
    # The body of a key or certificate pasted without its BEGIN line.
    Rule("pem_body", "CREDENTIAL", r"(?m)(?:^[A-Za-z0-9+/]{40,76}={0,2}[ \t]*(?:\n|\Z)){3,}", 0.5),
    # Bank details outside IBAN countries: an IFSC code names a branch and sits next to the account
    # number; a bare run of digits is an account number only when a label says so.
    Rule("in_ifsc", "BANK_ACCOUNT", r"\b[A-Z]{4}0[A-Z0-9]{6}\b", 0.45, None, ctx("BANK_ACCOUNT")),
    Rule("bank_account", "BANK_ACCOUNT", r"(?<![\d-])\d{9,18}(?![\d-])", 0.25, None, ctx("BANK_ACCOUNT"), requires_context=True),
    # UPI addresses have a bank handle where an e-mail has a domain ("priya.raman@okhdfcbank").
    Rule("in_upi", "UPI_ID",
         r"(?<![\w.+-])[A-Za-z0-9._-]{2,}@(?:ok(?:axis|hdfcbank|icici|sbi)|ybl|ibl|axl|apl|upi|paytm|ptyes|ptsbi|pthdfc|ptaxis"
         r"|sbi|hdfcbank|icici|axisbank|kotak|yesbank|idfcbank|airtel|jio|freecharge|fbl|okbizaxis)(?![\w.-])", 0.85),
    Rule("uk_nino", "UK_NINO", r"\b(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z] ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b",
         0.45, None, ctx("UK_NINO")),
    Rule("in_voter_id", "IN_VOTER_ID", r"\b[A-Z]{3}\d{7}\b", 0.2, None, ctx("IN_VOTER_ID"), requires_context=True),
    Rule("in_driving_licence", "DRIVING_LICENCE", r"\b[A-Z]{2}[- ]?\d{2}[- ]?(?:19|20)\d{2}[- ]?\d{7}\b", 0.5, None,
         ctx("DRIVING_LICENCE")),
    Rule("driving_licence_labelled", "DRIVING_LICENCE",
         r"(?i:\b(?:driving licen[cs]e|driver'?s? licen[cs]e|dl)\s*(?:no\.?|number|#)?\s*[:#-]?\s*)([A-Z0-9][A-Z0-9 -]{5,18}[A-Z0-9])\b",
         0.65, group=1, min_digits=4),
    # Health data is a special category. Only stated facts are flagged ("diagnosed with ...", a
    # blood group next to the word); both land in the review band unless a header confirms them.
    Rule("blood_group", "HEALTH_DATA", r"(?<![A-Za-z0-9])(?:AB|A|B|O)\s?(?:[+-]|\bpositive\b|\bnegative\b)(?:ve\b)?(?![A-Za-z0-9])",
         0.2, None, ctx("HEALTH_DATA"), requires_context=True),
    Rule("health_statement", "HEALTH_DATA",
         r"(?i:\b(?:diagnosed with|suffers from|undergoing treatment for|medical condition\s*[:-])\s+)"
         r"([A-Za-z][A-Za-z0-9' -]{2,40}?)(?=[.,;:\n)]|\s+(?:and|since|in|on|at|by|for)\b|$)", 0.5, group=1),
    # Indian postal addresses have no street-suffix word; they start with a unit and end in a PIN code.
    Rule("in_address", "ADDRESS",
         r"(?i:\b(?:flat|plot|house|h\.? ?no|door no|d\.? ?no|block|sector|shop)\b\.?\s*(?:no\.?\s*)?#?\s*)[0-9A-Z][^\n]{4,90}?(?<!\d)[1-9]\d{2} ?\d{3}(?!\d)",
         0.6, None, ctx("ADDRESS")),
    Rule("iban", "IBAN_CODE", r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b", 0.40, v.check_iban, ctx("IBAN_CODE")),
    Rule("dob_numeric", "DATE_OF_BIRTH", r"\b\d{1,2}[/.-]\d{1,2}[/.-](?:19|20)?\d{2}\b", 0.15, v.check_date,
         ctx("DATE_OF_BIRTH"), requires_context=True),
    Rule("dob_iso", "DATE_OF_BIRTH", r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b", 0.15, v.check_date, ctx("DATE_OF_BIRTH"), requires_context=True),
    Rule("dob_text", "DATE_OF_BIRTH", rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTHS}\.?,?\s+(?:19|20)\d{{2}}\b|\b{MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+(?:19|20)\d{{2}}\b",
         0.15, v.check_date, ctx("DATE_OF_BIRTH"), requires_context=True),
    Rule("street_address", "ADDRESS",
         rf"\b\d{{1,5}}[A-Za-z]?\s+(?:[A-Z][A-Za-z'-]+\s+){{1,3}}{STREET}(?![A-Za-z])"
         r"(?:,?\s+(?:Apartment|Apt|Suite|Ste|Unit|Flat|Floor|Fl)\.?\s*#?\w+)?"
         r"(?:,\s*[A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+)?)?"
         r"(?:,?\s*[A-Z]{2}\s*\d{5}(?:-\d{4})?)?"  # "TX78664": OCR drops the space
         r"(?:,?\s*[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})?",
         0.65, None, ctx("ADDRESS")),
]

CONTEXT_BOOST = 0.35
NO_CONTEXT_CAP = 0.2
BEFORE, AFTER = 45, 20


def _find_context(text: str, start: int, end: int, words: tuple[str, ...]) -> Optional[str]:
    window = text[max(0, start - BEFORE) : start].lower() + " " + text[end : end + AFTER].lower()
    for w in words:
        if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", window):
            return w
    # OCR of form labels drops the spaces ("DATEOFBIRTH"): phrases are also looked for written as one word.
    for w in words:
        if " " in w and re.search(rf"(?<![a-z]){re.escape(w.replace(' ', ''))}(?![a-z])", window):
            return w
    return None


# Screenshot text only: OCR of small UI text often drops the "@" ("emlly.martinezmaomeccrp.com")
# or keeps just "name.surname". Fail closed: such tokens are treated as probable e-mails (review band,
# still redacted). Not used on body text, where "section.3" or "policy.docx" are more likely.
IMAGE_RULES: list[Rule] = [
    # OCR reads 1/0 as I/l/O inside numbers ("+I917555-0164"); at least 7 real digits are still required.
    Rule("phone_ocr_confusable", "PHONE_NUMBER",
         r"(?<![0-9/+-])\+[0-9OIl]{1,3}[\s.-]?(?:\([0-9OIl]{1,5}\)[\s.-]?)?[0-9OIl]{1,12}(?:[\s.-][0-9OIl]{1,8}){0,5}(?![\w/-])",
         0.55, min_digits=7),
    # Log lines run words together and misread more digits ("phone=+1-212-555-e147resultapproved").
    Rule("phone_ocr_glued", "PHONE_NUMBER",
         r"(?<![0-9/+-])\+[0-9OIl]{1,3}[\s.-]?(?:\([0-9OIl]{1,5}\)[\s.-]?)?[0-9OIleoSB]{2,5}(?:[\s.-][0-9OIleoSB]{2,5}){1,4}(?![0-9/-])",
         0.5, min_digits=7, unless="phone_ocr_confusable"),
    # ".com" is often read as ".con", ".ccm", ".corn".
    Rule("email_ocr_no_at", "EMAIL_ADDRESS",
         r"(?<![\w@.-])[a-z][a-z0-9_-]*(?:[._-][a-z0-9-]+)*\.(?:com|con|ccn|ccm|cam|corn|c0m|org|net|io|co|uk|de|in|pl|example"
         r"|gov|edu|info|biz)(?![\w@-])",
         0.5),
    # "name.unreadabledomain.xyz": a long run of letters after a dot where the "@" and the domain were.
    Rule("email_ocr_garbled", "EMAIL_ADDRESS",
         r"(?<![\w@.-])[a-z][a-z0-9_-]+\.[a-z][a-z0-9]{11,}(?:\.[a-z0-9]{2,})+", 0.45),
    # An address after its label, with digits misread as letters ("src_ip-s2.i60.14.8").
    Rule("ip_ocr_labelled", "IP_ADDRESS",
         r"(?i:(?<![a-z])(?:src|dst|client|remote)?_?[i1l]p[-=: ]{1,2})([0-9A-Za-z]{1,3}(?:\.[0-9A-Za-z]{1,3}){2}\.[0-9OIlSB]{1,3})(?![0-9.])",
         0.5, group=1, min_digits=3),
    # Initials and a surname written as one ("T.R.Moreau", "D.R.Kumar"): how names sit in small table text.
    Rule("initials_surname", "PERSON",
         r"(?<![\w.])(?:[A-Z]\.){1,3}[A-Z][^\W\d_]{2,}(?:-[A-Z][^\W\d_]+)?(?![\w@])", 0.5),
    # The organisation's ID formats (org.yaml, image_id_patterns), as OCR writes them.
    *[Rule(r["name"], r["entity"], r["pattern"], float(r["score"]), min_digits=3) for r in ORG["image_id_patterns"]],
    Rule("name_dot_surname", "EMAIL_ADDRESS",
         r"(?<![\w@.-])[a-z]{2,}[._](?!(?:com|org|net|io|docx?|pdf|xlsx?|pptx?|txt|csv|json|png|jpe?g|exe|html?|py)\b)"
         r"[a-z]{2,}(?![\w@.-])", 0.45),
]


def run_rules(text: str, entities: Optional[list[str]] = None, rules: Optional[list[Rule]] = None) -> list[RecognizerResult]:
    results = []
    if rules is None and (not entities or "PERSON" in entities):
        results.extend(_gazetteer(text))
    spans_by_rule: dict[str, list[tuple[int, int]]] = {}
    for rule in RULES if rules is None else rules:
        if entities and rule.entity not in entities:
            continue
        for m in rule.regex.finditer(text):
            if rule.accept is not None and not rule.accept(m):
                continue
            start, end = m.span(rule.group)
            spans_by_rule.setdefault(rule.name, []).append((start, end))
            if rule.unless and any(a < end and b > start for a, b in spans_by_rule.get(rule.unless, [])):
                continue
            value = m.group(rule.group)
            if not value.strip():
                continue
            if rule.min_digits and len(v.digits(value)) < rule.min_digits:
                continue
            score = rule.score
            reasons = [f"pattern '{rule.name}' (base {rule.score:.2f})"]
            if rule.validator:
                delta, why = rule.validator(value)
                if why:
                    score += delta
                    reasons.append(f"{why} ({delta:+.2f})")
            hit = _find_context(text, start, end, rule.context) if rule.context else None
            if hit:
                score += CONTEXT_BOOST
                reasons.append(f"context word '{hit}' nearby (+{CONTEXT_BOOST:.2f})")
            elif rule.requires_context:
                score = min(score, NO_CONTEXT_CAP)
                reasons.append("no supporting context word: kept below review band")
            score = round(max(0.0, min(score, 1.0)), 3)
            expl = AnalysisExplanation(recognizer="PiiShieldRules", original_score=rule.score, pattern_name=rule.name,
                                       pattern=rule.pattern, textual_explanation="; ".join(reasons))
            results.append(RecognizerResult(
                entity_type=rule.entity, start=start, end=end, score=score, analysis_explanation=expl,
                recognition_metadata={
                    RecognizerResult.RECOGNIZER_NAME_KEY: f"rule:{rule.name}",
                    RecognizerResult.IS_SCORE_ENHANCED_BY_CONTEXT_KEY: True,  # context handled here, not by Presidio
                    "reasons": reasons,
                    "layer": "L1 rules",
                },
            ))
    return results


def _gazetteer(text: str) -> list[RecognizerResult]:
    """Given-name gazetteer (names.py): catches names in capitals or lowercase that NER misses."""
    out = []
    for start, end, score, why in gazetteer_names(text):
        reasons = [why]
        expl = AnalysisExplanation(recognizer="PiiShieldRules", original_score=score, pattern_name="given_name",
                                   textual_explanation=why)
        out.append(RecognizerResult(
            entity_type="PERSON", start=start, end=end, score=score, analysis_explanation=expl,
            recognition_metadata={RecognizerResult.RECOGNIZER_NAME_KEY: "rule:given_name",
                                  RecognizerResult.IS_SCORE_ENHANCED_BY_CONTEXT_KEY: True,
                                  "reasons": reasons, "layer": "L1 rules"},
        ))
    return out


class RuleRecognizer(EntityRecognizer):
    """Presidio adapter so the rules run inside the same AnalyzerEngine as the NER models."""

    def __init__(self):
        super().__init__(supported_entities=sorted({r.entity for r in RULES} | {"PERSON"}), name="PiiShieldRules",
                         supported_language="en")

    def load(self) -> None:
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        return run_rules(text, entities)
