"""Run extraction only (no detection) on each file and print what came out.

    python scripts/run_extract.py FILE [FILE ...] [--out DIR]

For each file: page/slide count, pages that needed OCR, span counts by kind and by source,
the structure summary, embedded-image OCR status, warnings and timing. With --out, the
extracted Markdown is written to DIR/<stem>.extracted.md. Input files are only read.
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from optiv_pii_shield.config import Settings  # noqa: E402
from optiv_pii_shield.extract import extract, sniff  # noqa: E402


def report(path: Path, settings: Settings, out: Path | None) -> bool:
    print(f"\n=== {path.name}  ({path.stat().st_size / 1e6:.1f} MB, sniffed as {sniff(path)})")
    t0 = time.perf_counter()
    try:
        doc = extract(path, settings)
    except Exception:
        print(f"  FAILED after {time.perf_counter() - t0:.1f}s")
        traceback.print_exc()
        return False
    secs = time.perf_counter() - t0

    unit = "slides" if doc.file_type == "pptx" else "pages"
    print(f"  {unit}: {doc.pages or '(not paginated)'}")
    if doc.file_type in ("pdf", "image"):
        ocr = doc.ocr_pages
        print(f"  OCR needed on {len(ocr)} of {doc.pages} pages: {_ranges(ocr) or 'none'}")
    print(f"  spans: {len(doc.spans)}  ({sum(len(s.text) for s in doc.spans):,} characters)")
    for kind, n in Counter(s.kind for s in doc.spans).most_common():
        print(f"    {kind:<12} {n}")
    print(f"  by source: {dict(Counter(s.source for s in doc.spans))}")
    confs = [s.ocr_conf for s in doc.spans if s.ocr_conf is not None]
    if confs:
        low = sum(c < settings.low_conf_ocr for c in confs)
        print(f"  OCR confidence: mean {sum(confs) / len(confs):.3f}, {low} span(s) below {settings.low_conf_ocr}")
    print(f"  structure: {doc.structure}")
    if doc.images:
        print(f"  images: {len(doc.images)}  status {dict(Counter(i.ocr_status for i in doc.images))}")
    for w in doc.warnings:
        print(f"  ! {w}")
    print(f"  time: {secs:.1f}s" + (f"  ({secs / max(len(doc.ocr_pages), 1):.1f}s per OCR page)" if doc.ocr_pages else ""))
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        md = out / f"{path.stem}.extracted.md"
        md.write_text(doc.markdown, encoding="utf-8")
        print(f"  markdown: {md}")
    return True


def _ranges(nums: list[int]) -> str:
    out, start, prev = [], None, None
    for n in sorted(nums):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, help="write <stem>.extracted.md here")
    ap.add_argument("--ocr", default="auto", choices=["auto", "rapidocr", "tesseract"])
    a = ap.parse_args()
    settings = Settings(ocr_engine=a.ocr)
    ok = [report(f, settings, a.out) for f in a.files]
    print(f"\n{sum(ok)}/{len(ok)} file(s) extracted")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
