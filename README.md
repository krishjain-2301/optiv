# PII Shield

Offline, fail-closed PII detection and redaction for text artifacts (PDF, including scanned; DOCX; PPTX; images).
Built for the Optiv VIT case study (Case Study 2). The design rationale is in
[`01-landscape-and-recommendation.md`](01-landscape-and-recommendation.md) (Option C).

**Nothing leaves the machine.** No cloud OCR, no cloud PII API, no LLM is used to *find* PII. The output is text
that is safe to hand to an LLM, plus masked copies of the originals and a full audit trail.

```
Upload ─► sniff type (magic bytes)
       ─► EXTRACT  PDF text layer | layout-aware OCR (ruled tables cell-by-cell, screenshots re-read at 2x)
                   DOCX/PPTX OOXML walk: body, tables, text boxes, groups, headers/footers, notes, comments,
                   document properties, customXml, comment/tracked-change authors, embedded images (OCR)
                   → span map: file · page/slide · element · table cell + column header · bbox · OCR confidence
       ─► DETECT   L1 rules + checksums + context words   (inside Presidio)
                   L2 NER: spaCy, optional GLiNER-PII      (inside Presidio)
                   L3 structure: column headers, "Label:" fields, document properties
                   L4 propagation: every confirmed person searched across all files (surname, initial, possessive)
                   L0 fail-closed: identifier-like OCR words below the confidence floor
                   Resolver: trim/allow-list, merge overlaps, agreement bonus, route redact / review / drop
       ─► REDACT   stable tokens [PERSON_007] [EMAIL_007] across files · redacted Markdown for the LLM
                   masked PDF/DOCX/PPTX (layout and page count kept, author metadata cleared)
       ─► REPORT   exposure register (CSV/XLSX) · summary.json · audit_log.jsonl · token vault
       ─► MEASURE  recall / precision / leaks vs gold labels, per category and per source type
```

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m spacy download en_core_web_lg     # or en_core_web_sm (smaller, weaker on names)
python scripts/fetch_models.py              # English OCR model, ~9 MB, one-time (weights only)
```

OCR uses RapidOCR (PP-OCR models on ONNX Runtime), which installs with pip and needs no system binary.
Two settings matter and were chosen from measurements on the scans: the **English** recognition model
(the bundled Chinese model drops the spaces between English words, which breaks name and label detection)
and the angle classifier **off** (it flipped long lines on upright scans, silently losing whole sentences).
Masks use the recogniser's per-character positions and are padded, so they err towards covering a
neighbouring character rather than exposing one.
Tesseract is supported with `--ocr tesseract` if `pytesseract` and the binary are installed.
GLiNER-PII is optional: `pip install gliner`, then `--gliner` (downloads the model once, runs locally).

## Use

**Demo UI**

```powershell
streamlit run app.py
```

Upload files (or press *Use synthetic samples*). The tabs show the overview, extraction side by side with the page
(PII boxed), every finding in context with its reasons, the exact text the LLM would receive, measured metrics
(when gold labels are supplied) and downloads.

**CLI**

```powershell
python -m pii_shield run path\to\*.pdf path\to\*.docx --out out
python -m pii_shield run samples\synthetic\* --out out --gold samples\synthetic\gold_labels.csv
python -m pii_shield gold-template path\to\files\* --out gold_draft.csv   # bootstrap gold labels, then correct by hand
```

**Python**

```python
from pii_shield import run, Settings
res = run(["policy.pdf"], Settings(), out_dir="out")
res.redacted["policy.pdf"]        # LLM-safe Markdown
res.findings["policy.pdf"]        # findings with location, category, token, score, layer, reasons
```

## Outputs (per run)

| File | Contents | Sensitive? |
|---|---|---|
| `<name>.extracted.md` | Faithful Markdown of the source (headings, tables, image text) | yes |
| `<name>.redacted.md` | Same structure with PII replaced by tokens: **what goes to the LLM** | no |
| `<name>.masked.<ext>` | Masked copy of the original, same page count/layout, metadata cleared | no |
| `pii_exposure_register.csv/.xlsx` | Every finding: file, page, location, source type, category, value, token, score, layer, reasons | **yes** |
| `summary.json` | Per-file counts: OCR pages, images and their status, categories, image-only identifiers, warnings | no |
| `audit_log.jsonl` | Every decision including dropped candidates, values partially masked | no |
| `token_vault.SENSITIVE.json` | Token → original value, for authorised re-identification | **yes** |
| `evaluation.json` | With `--gold`: recall, precision, leaks, per-category/source breakdown, structure retention | no |

## Fail-closed behaviour

- A file that cannot be parsed is reported and produces no output (never passed through).
- Review-band findings (`0.35 ≤ score < 0.60` by default) are **redacted** and queued for review.
- OCR words below the confidence floor that contain digits or `@` are masked as `[UNREADABLE_…]`.
- Text from images that were OCR'd with low confidence is withheld from the LLM text entirely; images that
  cannot be read at all (EMF/WMF) are removed from the masked copies. Scanned-page image regions without
  readable text (photos, badges) are blanked in the masked PDF.
- Checksums only raise or lower confidence: test-range values (SSN `9xx`, `555` phones) next to a label are kept.

## Measuring quality

Recall is only reported against gold labels. Format (one row per PII instance):

```csv
file,page,text,category,context_type,note
risk_policy.pdf,3,Priya Raman,PERSON,narrative,
org_pack.pptx,1,+1 (555) 0114,PHONE_NUMBER,table,
```

`context_type` ∈ `native | labelled | table | narrative | image | metadata` drives the per-source breakdown
(Appendix E.6 prompts 2 and 3). Structure retention is computed from the raw OOXML for DOCX/PPTX, and against an
optional hand transcription (`--transcriptions dir/` with `<stem>.txt`) for OCR'd PDFs.

## Synthetic fixtures

`python scripts/make_samples.py samples/synthetic` builds a scanned PDF, a DOCX and a PPTX with invented people
that reproduce the traps found in the real samples (no text layer, ruled tables with multi-line cells,
screenshot text, unlabelled narrative with possessives and surname-only mentions, `.example` e-mails, `+1 (555) 0114`
phones, glued DOCX cells, field names that look like names, author metadata) and writes `gold_labels.csv`.

## Tests

```powershell
pytest -q
```

Unit tests cover validators, rules, name handling and structure; integration tests run the full pipeline on the
synthetic fixtures and check recall/precision floors, leaks in the redacted text and masked files, token stability,
traceability of every finding, structure retention and report outputs.

## Layout

```
pii_shield/
  config.py            thresholds, allow-list, header→category map, context words
  models.py            Span / Word / ImageRef / Document / Finding
  extract/             sniff, pdf, docx, pptx, image, ooxml walkers, layout (tables/regions), ocr backends
  detect/              rules + validators (L1), ner (L2), structure (L3), propagation (L4), resolver
  redact/              tokens (vault), text (LLM output), files (masked PDF/DOCX/PPTX/images)
  render.py            spans → Markdown (shared by extracted and redacted views)
  report.py            exposure register, summary, audit log
  evaluate.py          gold labels, recall/precision/leaks, structure retention
  pipeline.py, cli.py
app.py                 Streamlit demo
scripts/make_samples.py
tests/
```
