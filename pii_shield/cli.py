"""Command line.

    python -m pii_shield run samples/*.pdf samples/*.docx --out out/
    python -m pii_shield run samples/synthetic/* --out out/ --gold samples/synthetic/gold_labels.csv
    python -m pii_shield gold-template samples/*.pptx --out gold_draft.csv
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import sys
from pathlib import Path

from .config import Settings
from .evaluate import evaluate, gold_template, load_gold, structure_retention
from .pipeline import run


def _expand(patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for p in patterns:
        matches = glob.glob(p) or [p]
        files.extend(Path(m) for m in matches if Path(m).is_file() and not Path(m).name.endswith(".csv"))
    return files


def _settings(a) -> Settings:
    s = Settings()
    s.ocr_engine = a.ocr
    s.use_gliner = a.gliner
    s.spacy_model = a.spacy_model
    if a.no_images:
        s.ocr_embedded_images = False
    return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pii_shield", description="Offline PII detection and redaction")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "gold-template"):
        p = sub.add_parser(name)
        p.add_argument("files", nargs="+")
        p.add_argument("--out", default="out" if name == "run" else "gold_draft.csv")
        p.add_argument("--ocr", default="auto", choices=["auto", "rapidocr", "tesseract"])
        p.add_argument("--gliner", action="store_true", help="add the GLiNER-PII model (needs `pip install gliner`)")
        p.add_argument("--spacy-model", default="en_core_web_lg")
        p.add_argument("--no-images", action="store_true", help="skip OCR of embedded images")
        p.add_argument("-v", "--verbose", action="store_true")
        if name == "run":
            p.add_argument("--gold", help="gold-label CSV: prints recall / precision / leaks")
            p.add_argument("--transcriptions", help="folder of <stem>.txt hand transcriptions for OCR'd PDFs")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    files = _expand(a.files)
    if not files:
        print("no input files", file=sys.stderr)
        return 2
    progress = lambda msg, frac: print(f"[{frac:4.0%}] {msg}", file=sys.stderr)  # noqa: E731

    if a.cmd == "gold-template":
        res = run(files, _settings(a), None, progress)
        print(f"wrote {gold_template(res.findings, a.out)}")
        return 0

    res = run(files, _settings(a), a.out, progress)
    print(f"\nrun {res.run_id}  components: {', '.join(res.components)}")
    for f, doc in res.docs.items():
        live = [x for x in res.findings[f] if x.decision != "drop"]
        review = sum(x.decision == "review" for x in live)
        ocr = f", OCR pages {doc.ocr_pages}" if doc.ocr_pages else ""
        print(f"  {f}: {len(doc.spans)} spans, {len(live)} findings ({review} for review){ocr}")
        for w in doc.warnings:
            print(f"    ! {w}")
    for f, err in res.errors.items():
        print(f"  ERROR {f}: {err}")

    if a.gold:
        ev = evaluate(load_gold(a.gold), res.findings, res.redacted)
        print(f"\nrecall {ev.recall:.1%}  category-recall {ev.category_recall:.1%}  precision {ev.precision:.1%} (auto-redact {ev.precision_auto:.1%})  "
              f"F1 {ev.f1:.3f}  leaks {len(ev.leaks)}  ({ev.recalled}/{ev.total_gold} gold instances)")
        for ctx, m in ev.by_context.items():
            print(f"  {ctx:<12} recall {m['recall']:.1%} ({m['recalled']}/{m['gold']})")
        for m in ev.missed:
            print(f"  MISSED {m['file']} p{m['page']} [{m['category']}] {m['text']!r}")
        structure = {}
        for f, doc in res.docs.items():
            tr = None
            if a.transcriptions:
                tp = Path(a.transcriptions) / f"{Path(f).stem}.txt"
                tr = tp.read_text(encoding="utf-8") if tp.exists() else None
            structure[f] = structure_retention(doc, tr)
        out = Path(a.out) / "evaluation.json"
        out.write_text(json.dumps({"metrics": ev.as_dict(), "structure_retention": structure}, indent=2, default=str),
                       encoding="utf-8")
        print(f"  evaluation written to {out}")
    print(f"\noutputs in {Path(a.out).resolve()}")
    return 0 if not res.errors else 1


if __name__ == "__main__":
    sys.exit(main())
