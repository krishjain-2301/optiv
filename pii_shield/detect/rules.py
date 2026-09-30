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

from ..config import CONTEXT_WORDS
from . import validators as v

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

    def __post_init__(self):
        self.regex = re.compile(self.pattern, self.flags)


def ctx(cat: str) -> tuple[str, ...]:
    return tuple(CONTEXT_WORDS.get(cat, ()))


RULES: list[Rule] = [
    Rule("email", "EMAIL_ADDRESS", r"(?<![\w.+-])[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])", 0.92,
         v.check_mailbox),
    # OCR damages e-mails in screenshots ("wilsong@acmeco-p com"): anything around an @ is suspect.
    Rule("email_ocr_damaged", "EMAIL_ADDRESS", r"(?<![\w.+-])[A-Za-z0-9._%+-]{2,} ?@ ?[A-Za-z0-9-]{2,}(?:[., -]{1,2}[A-Za-z0-9-]{2,}){0,3}", 0.55,
         unless="email"),  # fallback for OCR damage only; never crosses a line
    Rule("phone", "PHONE_NUMBER",
         # A leading "+" may follow a letter: OCR glues names to numbers ("Whitfield+1(212)555-0147").
         r"(?:(?<![0-9/+-])\+\d{1,3}[\s.-]?|(?<![\w/+-]))(?:\(\d{1,5}\)[\s.-]?)?\d{1,12}(?:[\s.-]\d{1,8}){0,5}(?![\w/-])",
         0.40, v.check_phone, ctx("PHONE_NUMBER"), min_digits=7),
    # OCR glues neighbours on ("EMP-41877sV", "EMP-41077IAM"), so no trailing \b. O/I/l read for 0/1 are
    # tolerated inside the number, but an ID must end on a real digit unless nothing alphanumeric follows.
    Rule("cadence_person_id", "EMPLOYEE_ID", r"(?<![0-9])(?:EMP|DIR|STF|CON|USR)-(?:[0-9OIl]{2,6}[0-9]|[0-9OIl]{3,7}(?![0-9A-Za-z]))", 0.88),
    Rule("cadence_vendor_id", "VENDOR_ID", r"(?<![0-9])(?:MER|VEN|SUP)-[A-Z]{2}-(?:[0-9OIl]{2,6}[0-9]|[0-9OIl]{3,7}(?![0-9A-Za-z]))", 0.80),
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
    Rule("iban", "IBAN_CODE", r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b", 0.40, v.check_iban, ctx("IBAN_CODE")),
    Rule("dob_numeric", "DATE_OF_BIRTH", r"\b\d{1,2}[/.-]\d{1,2}[/.-](?:19|20)?\d{2}\b", 0.15, v.check_date,
         ctx("DATE_OF_BIRTH"), requires_context=True),
    Rule("dob_iso", "DATE_OF_BIRTH", r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b", 0.15, v.check_date, ctx("DATE_OF_BIRTH"), requires_context=True),
    Rule("dob_text", "DATE_OF_BIRTH", rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTHS}\.?,?\s+(?:19|20)\d{{2}}\b|\b{MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+(?:19|20)\d{{2}}\b",
         0.15, v.check_date, ctx("DATE_OF_BIRTH"), requires_context=True),
    Rule("street_address", "ADDRESS",
         rf"\b\d{{1,5}}[A-Za-z]?\s+(?:[A-Z][A-Za-z'-]+\s+){{1,3}}{STREET}(?![A-Za-z])"
         r"(?:,?\s+(?:Apt|Suite|Unit|Flat)\.?\s*\w+)?"
         r"(?:,\s*[A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+)?)?"
         r"(?:,?\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?)?"
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
    return None


# Screenshot text only: OCR of small UI text often drops the "@" ("emlly.martinezmaomeccrp.com")
# or keeps just "name.surname". Fail closed: such tokens are treated as probable e-mails (review band,
# still redacted). Not used on body text, where "section.3" or "policy.docx" are more likely.
IMAGE_RULES: list[Rule] = [
    # OCR reads 1/0 as I/l/O inside numbers ("+I917555-0164"); at least 7 real digits are still required.
    Rule("phone_ocr_confusable", "PHONE_NUMBER",
         r"(?<![0-9/+-])\+[0-9OIl]{1,3}[\s.-]?(?:\([0-9OIl]{1,5}\)[\s.-]?)?[0-9OIl]{1,12}(?:[\s.-][0-9OIl]{1,8}){0,5}(?![\w/-])",
         0.55, min_digits=7),
    Rule("email_ocr_no_at", "EMAIL_ADDRESS",
         r"(?<![\w@.-])[a-z][a-z0-9_-]*(?:[._-][a-z0-9-]+)*\.(?:com|org|net|io|co|uk|de|in|pl|example|gov|edu|info|biz)(?![\w@-])",
         0.5),
    Rule("name_dot_surname", "EMAIL_ADDRESS",
         r"(?<![\w@.-])[a-z]{2,}[._](?!(?:com|org|net|io|docx?|pdf|xlsx?|pptx?|txt|csv|json|png|jpe?g|exe|html?|py)\b)"
         r"[a-z]{2,}(?![\w@.-])", 0.45),
]


def run_rules(text: str, entities: Optional[list[str]] = None, rules: Optional[list[Rule]] = None) -> list[RecognizerResult]:
    results = []
    spans_by_rule: dict[str, list[tuple[int, int]]] = {}
    for rule in RULES if rules is None else rules:
        if entities and rule.entity not in entities:
            continue
        for m in rule.regex.finditer(text):
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


class RuleRecognizer(EntityRecognizer):
    """Presidio adapter so the rules run inside the same AnalyzerEngine as the NER models."""

    def __init__(self):
        super().__init__(supported_entities=sorted({r.entity for r in RULES}), name="PiiShieldRules",
                         supported_language="en")

    def load(self) -> None:
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        return run_rules(text, entities)
