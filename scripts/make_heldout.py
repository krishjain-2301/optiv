"""Held-out evaluation set, generated with Faker and never used to write rules.

The fixture set (make_samples.py) was written together with the detectors, so scores on it are
optimistic. This set is generated from other sources: Faker names, phones and identifiers for
Indian, Polish, German, Spanish, British and US locales, with accents, ALL-CAPS and lowercase
variants, OCR-style character noise, and sentences with and without a keyword nearby. Decoy
paragraphs (dates, versions, amounts, business words) measure false positives.

Two seeds: ``dev`` may be inspected while fixing general failure classes; ``test`` is only ever
reported. Usage:

    python scripts/make_heldout.py OUT_DIR [--seed test] [--n 120]
"""
from __future__ import annotations

import argparse
import csv
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

from faker import Faker

SEEDS = {"dev": 1101, "test": 2207}
LOCALES = ["en_IN", "pl_PL", "de_DE", "es_ES", "en_GB", "en_US"]


@dataclass
class Para:
    text: str
    gold: list[tuple[str, str]] = field(default_factory=list)  # (value as it appears, category)
    variant: str = "plain"  # plain | upper | lower | ocr | decoy
    keyword: bool = True


# --------------------------------------------------------------------------------- values
def person(f: Faker) -> str:
    for _ in range(20):
        n = f"{f.first_name()} {f.last_name()}"
        if 2 <= len(n.split()) <= 4 and not re.search(r"[\d.]", n):
            return n
    return n


def phone(f: Faker, loc: str) -> str:
    for _ in range(20):
        p = f.phone_number()
        if "x" not in p.lower() and len(re.sub(r"\D", "", p)) >= 9:
            return p
    return p


def identifier(f: Faker, loc: str, rng: random.Random) -> tuple[str, str, str]:
    """(value, category, keyword phrase) for a locale-appropriate identifier."""
    choices = [("iban", "IBAN_CODE", "IBAN"), ("card", "CREDIT_CARD", "card"), ("ip", "IP_ADDRESS", "from IP")]
    if loc == "en_IN":
        choices += [("aadhaar", "IN_AADHAAR", "Aadhaar")]
    if loc == "pl_PL":
        choices += [("pesel", "PL_PESEL", "PESEL")]
    if loc == "en_US":
        choices += [("ssn", "US_SSN", "SSN")]
    kind, cat, kw = rng.choice(choices)
    value = {
        "iban": lambda: f.iban(),
        "card": lambda: f.credit_card_number(card_type=rng.choice(["visa16", "mastercard", "amex"])),
        "ip": lambda: f.ipv4_public() if rng.random() < 0.7 else f.ipv4_private(),
        "aadhaar": lambda: " ".join(re.findall(r"\d{4}", f.aadhaar_id())) if hasattr(f, "aadhaar_id") else "",
        "pesel": lambda: f.pesel(),
        "ssn": lambda: f.ssn(),
    }[kind]()
    return value, cat, kw


# ------------------------------------------------------------------------------ variants
OCR_SWAPS = [("rn", "m"), ("l", "1"), ("O", "0"), ("o", "0"), ("e", "c"), ("i", "l"), ("S", "5"), ("B", "8")]


def ocr_noise(text: str, rng: random.Random) -> str:
    """One or two OCR-style confusions inside the value."""
    out = text
    for _ in range(rng.choice([1, 2])):
        cands = [(a, b) for a, b in OCR_SWAPS if a in out]
        if not cands:
            break
        a, b = rng.choice(cands)
        idx = [m.start() for m in re.finditer(re.escape(a), out)]
        i = rng.choice(idx)
        out = out[:i] + b + out[i + len(a):]
    return out


# ----------------------------------------------------------------------------- templates
WITH_KEYWORD = [
    "Please contact {name} on {phone} about the vendor questionnaire.",
    "The exception was raised by {name}; mobile {phone}, e-mail {email}.",
    "{name} ({email}) owns the remediation plan and can be reached at tel. {phone}.",
    "Payment details for {name}: {kw} {ident}.",
    "Escalate to {name} if the assessment is not completed by Friday.",
]
WITHOUT_KEYWORD = [
    "{name} confirmed the figures with {name2} before the steering meeting.",
    "Notes from the call: {name} will follow up, {phone} after 5pm.",
    "Reimbursement goes to {ident} once {name} signs off.",
    "Last quarter {name2} reviewed the supplier file and passed it to {name}.",
    "Thanks to {name} — reach me at {email} or {phone}.",
]
DECOYS = [
    "The policy was approved on 14.03.2024 and takes effect from version 6.0.2 onwards.",
    "Revenue grew 12.5% to 4,250,000 EUR across 1,204 suppliers in 2024 1234 batches.",
    "Section 16.5 of the Risk Management Policy applies to all Tier 2 vendors.",
    "Build 10.0.19045 was deployed to 312 endpoints on 2025-11-03 at 12:30:45.",
    "The Risk Committee meets on the first Monday in April and May to review controls.",
    "Order PO-2024-118734 for 2,500 units is due in Q3; invoice INV 4471 0091 2210 remains open.",
    "West region sales lead the Grace period review; Page 4 of the North report lists Low risks.",
    "Contact the Service Desk via the Self-Service Portal for Access Management requests.",
]


def generate(seed: str | int = "test", n: int = 120) -> list[Para]:
    s = SEEDS.get(seed, seed) if isinstance(seed, str) else seed
    rng = random.Random(s)
    fakers = {loc: Faker(loc) for loc in LOCALES}
    for i, f in enumerate(fakers.values()):
        f.seed_instance(s * 31 + i)
    paras: list[Para] = []
    for i in range(n):
        if i % 6 == 5:
            paras.append(Para(rng.choice(DECOYS), [], "decoy", False))
            continue
        loc = rng.choice(LOCALES)
        f = fakers[loc]
        keyword = rng.random() < 0.5
        tpl = rng.choice(WITH_KEYWORD if keyword else WITHOUT_KEYWORD)
        variant = rng.choices(["plain", "upper", "lower", "ocr"], weights=[5, 2, 2, 2])[0]
        name, name2 = person(f), person(fakers[rng.choice(LOCALES)])
        ph = phone(f, loc)
        email = f.email()
        ident, icat, kw = identifier(f, loc, rng)
        if variant == "upper":
            name, name2 = name.upper(), name2.upper()
        elif variant == "lower":
            name, name2 = name.lower(), name2.lower()
        elif variant == "ocr":
            name, ph = ocr_noise(name, rng), ocr_noise(ph, rng)
        values = {"name": (name, "PERSON"), "name2": (name2, "PERSON"), "phone": (ph, "PHONE_NUMBER"),
                  "email": (email, "EMAIL_ADDRESS"), "ident": (ident, icat)}
        text = tpl.format(name=name, name2=name2, phone=ph, email=email, ident=ident, kw=kw)
        gold = [values[k] for k in re.findall(r"\{(\w+)\}", tpl) if k in values]
        paras.append(Para(text, list(dict.fromkeys(gold)), variant, keyword))
    return paras


def write(paras: list[Para], out: Path, stem: str = "heldout") -> dict[str, Path]:
    """The same paragraphs as a text file and as a DOCX (narrative + a contact table)."""
    import docx

    out.mkdir(parents=True, exist_ok=True)
    txt = out / f"{stem}.txt"
    txt.write_text("\n\n".join(p.text for p in paras) + "\n", encoding="utf-8")
    d = docx.Document()
    d.add_heading("Supplier contact notes", 1)
    for p in paras:
        d.add_paragraph(p.text)
    d.save(out / f"{stem}.docx")
    with open(out / f"{stem}_gold.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["paragraph", "variant", "keyword", "category", "value"])
        for i, p in enumerate(paras):
            for value, cat in p.gold:
                w.writerow([i, p.variant, p.keyword, cat, value])
    return {"txt": txt, "docx": out / f"{stem}.docx", "gold": out / f"{stem}_gold.csv"}


# ------------------------------------------------------------------------------ scoring
TOKEN = re.compile(r"\[[A-Z_]+_U?\d{3}(?:_\d+)?\]")


def residue(value: str, cat: str, redacted: str) -> list[str]:
    """Pieces of ``value`` still readable in ``redacted``. Strict on purpose: a surname left next to
    a token, or the last digit groups of a phone number, count as leaks."""
    low = redacted.lower()
    if value.lower() in low:
        return [value]
    if cat == "PERSON":
        return [t for t in re.findall(r"[^\W\d_]{3,}", value)
                if re.search(rf"(?<![^\W\d_]){re.escape(t.lower())}(?![^\W\d_])", low)]
    if cat == "EMAIL_ADDRESS":
        local = value.split("@")[0]
        return [local] if re.search(rf"(?<![\w.]){re.escape(local.lower())}(?![\w])", low) else []
    groups = [g for g in re.findall(r"[0-9A-Za-z]+", value) if sum(c.isdigit() for c in g) >= 3]
    return [g for g in groups if re.search(rf"(?<![0-9A-Za-z]){re.escape(g.lower())}(?![0-9A-Za-z])", low)]


def score(paras: list[Para], redacted: list[str]) -> dict:
    """Leak rate per category and variant; false positives = tokens placed in decoy paragraphs."""
    rows, by_cat, by_variant = [], {}, {}
    fp = 0
    for p, r in zip(paras, redacted):
        if p.variant == "decoy":
            fp += len(TOKEN.findall(r))
            if TOKEN.search(r):
                rows.append({"variant": "decoy", "category": "-", "value": p.text, "left": TOKEN.findall(r)})
            continue
        for value, cat in p.gold:
            left = residue(value, cat, r)
            for key, bucket in ((cat, by_cat), (p.variant, by_variant)):
                b = bucket.setdefault(key, {"gold": 0, "leaked": 0})
                b["gold"] += 1
                b["leaked"] += bool(left)
            if left:
                rows.append({"variant": p.variant, "keyword": p.keyword, "category": cat, "value": value,
                             "left": left, "redacted": r})
    total = sum(b["gold"] for b in by_cat.values())
    leaked = sum(b["leaked"] for b in by_cat.values())
    decoys = sum(p.variant == "decoy" for p in paras)
    return {"gold": total, "leaked": leaked, "recall": 1 - leaked / max(total, 1), "by_category": by_cat,
            "by_variant": by_variant, "decoy_paragraphs": decoys, "decoy_tokens": fp, "failures": rows}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--seed", default="test")
    ap.add_argument("--n", type=int, default=120)
    a = ap.parse_args(argv)
    paths = write(generate(a.seed, a.n), a.out, f"heldout_{a.seed}")
    print("\n".join(f"{k}: {v}" for k, v in paths.items()))


if __name__ == "__main__":
    main()
