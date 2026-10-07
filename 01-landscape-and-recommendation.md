# Case Study 2: PII Detection in Text Artifacts
## Industry landscape, what the samples show, and a recommended build

*Prepared 30 Sep 2026 for the Optiv VIT case study. Presentation date is 14 Oct 2026.*

> **Status: decision record, not current documentation.** This is the analysis written before the build. Option C
> was built; some choices below changed along the way. The current behaviour is in [`README.md`](README.md).
>
> | Written here | What was built |
> |---|---|
> | Streamlit or a notebook as the demo surface (§5, §6) | A FastAPI server with a React dashboard |
> | Tesseract first, PaddleOCR if needed (§6) | RapidOCR (PP-OCR on ONNX Runtime) with the English model; Tesseract optional |
> | L5: optional local LLM verifier (§5) | Not built. "L5" in the code is the leak gate and the human reviewer |
> | A fail-closed gate on OCR confidence (§5) | That, plus a text leak gate and a re-OCR verification of every masked copy |
> | §6 and §7 (open decisions, timeline) | Historical |

---

## 1. What the brief actually grades

The brief asks for five outcomes, presented as a 10 to 15 minute PPTX walkthrough plus a live demo:

| # | Outcome | What it really means |
|---|---|---|
| 1 | Solution Blueprint | Functional design diagram from file ingestion to PII output |
| 2 | System Architecture | Technical components inside each functional block, and how they interact |
| 3 | Extraction Logic | A per-format extraction approach, plus the detection and classification logic |
| 4 | Reliable Outcomes | **At least 80% of the original structure retained**, with surrounding context preserved. **PII recall approaching 100%.** Minimal false positives |
| 5 | Live Walkthrough | Upload the artifacts, extract, identify and classify PII per file (Colab, Jupyter or a simple UI) |

The problem statement adds three constraints that shape every choice below:

- **Redact before the LLM.** Cadence policy forbids PII from being processed by AI models. That rules out sending raw documents to a cloud AI service to *find* the PII, since that is itself PII processed by an AI model.
- **Transparent tags and source traceability.** Every finding must say what it is, where it came from (file, page or slide, table cell or paragraph, bounding box) and why it was flagged.
- **"No unidentified instances passed downstream."** The system has to fail closed: anything it cannot read confidently must not reach the LLM.

**A hidden rubric.** Appendix E.6 on the last page of the policy PDF lists "exercise prompts for students". It reads like the evaluators' checklist:

1. Extract text from every page and report which pages needed OCR.
2. Enumerate all PII by category, with page and location, split by body text, tables, narrative prose and image-only occurrences.
3. Identify identifiers that appear only inside images, and measure the recall gap against digitally native text.
4. Find identifiers in unlabelled narrative prose (Sections 16.5 and 16.8) and explain why field-based extraction fails there.
5. Produce a masked copy that preserves page count, layout and cross-artifact traceability of individuals through stable tokens.
6. Justify the false-positive controls for PAN, PESEL, TIN and card-number patterns.

The design below is built so each of these six becomes a slide or a demo moment.

---

## 2. File structure analysis (Roles & Responsibilities item 01)

| | Risk Management Policy (PDF) | TPRM Training (DOCX) | Organizational Pack (PPTX) |
|---|---|---|---|
| Size | 35 pages, 14 MB | 93k characters of text, 4.8 MB | 9 slides, 72 KB |
| Text layer | **None. Every page is a single raster image** (Word export flattened to scans). All 35 pages need OCR | Native text in paragraphs, tables, text boxes, content controls, headers and footers | Native text in shapes, grouped shapes and tables |
| Images | Screenshots *inside* the scans: identity record (p9), signed RCSA attestation (p18), user-group admin screen (p19), audit log (p20), staff ID badge (p28), org and escalation charts | 31 PNG screenshots of the TPRM tool with user names and e-mails, plus EMF/SVG diagrams | Logos only |
| Metadata | Author field holds a person's name (likely the real document author) | Creator and last-modified-by hold personal names; also `customXml` parts and a `[trash]` folder | Creator and last-modified-by hold a personal name |
| PII present | Names, employee IDs (`EMP-#####`), director IDs (`DIR-####`), vendor IDs (`MER-IN-####`), internal and external e-mails, phones in 6+ national formats, SSN, passport, Indian PAN, partial PESEL, TIN, dates of birth, home addresses, card last-4 | Names inside tables and inside screenshots | 16 people with name, title, e-mail and mobile; abbreviated names in RACI headers (`M. Vossberg CEO`) |
| Traps | Narrative prose with no field labels (§16.5, §16.8); possessive and surname-only mentions ("Rafael's own record"); tiny text inside screenshots; multi-line table cells that OCR merges across columns | Multi-paragraph table cells glue names together if extracted naively (`Tamika OliverShimane Smith`) | Non-standard phone format `+1 (555) 0114`; e-mails on the `.example` domain |

A non-PII observation worth one line in the deck: some DOCX screenshots still show a real company's product branding, so the sanitisation of these samples was incomplete. A consultant who notices that is showing the right instincts.

---

## 3. The industry landscape

Every serious PII pipeline has the same two halves: **get faithful text out of the file** and **find the PII in that text**. Vendors differ in how they do each half and where it runs.

### 3a. Extraction

| Approach | Examples | Strengths | Weaknesses |
|---|---|---|---|
| Native parsers | PyMuPDF / pdfplumber (PDF), python-docx, python-pptx, raw OOXML | Exact text, exact positions, free, fast | Useless on scans; you must walk tables, groups, headers, notes and metadata yourself |
| Classic OCR | Tesseract, EasyOCR | Offline, simple, gives word boxes and confidence | Weak on tables and small text; no layout understanding |
| Deep-learning OCR | PaddleOCR (PP-OCR / PP-Structure), docTR | Much better on small or noisy text; PP-Structure recovers tables | Heavier install; slower on CPU |
| Layout-aware document converters | IBM **Docling**, Unstructured, Marker, MinerU | One API for PDF, DOCX, PPTX and scans; outputs Markdown/JSON with headings, tables and reading order; built-in OCR hooks | Younger tools; need tuning for scanned tables |
| Cloud document AI | AWS Textract, Azure Document Intelligence, Google Document AI | Best-in-class tables and forms out of the box | Sends raw PII to a third-party AI service; cost and accounts |

### 3b. Detection

| Approach | Examples | How it works | Where it fails |
|---|---|---|---|
| **Pattern and checksum rules** | Regex with Luhn (cards), PESEL and PAN structure checks, context words | Deterministic, explainable, near-perfect on structured IDs | Blind to names, addresses, and free text; over-fires on look-alike numbers without context |
| **Statistical NER** | spaCy, Stanza, Flair | Classic named-entity models find people, places, organisations | Tuned on news text; misses odd names and breaks on table fragments; many false positives on product and field names |
| **Transformer PII models** | GLiNER-PII (Knowledgator, NVIDIA, Gretel), GLiNER2-PII (Fastino), DeBERTa fine-tunes on ai4privacy, Piiranha | Zero-shot or fine-tuned models with 50+ PII labels | A June 2026 open benchmark found all of them near 0.5 F1 once you leave their training domain. No single model is enough |
| **LLM-based detection** | A local Llama or Qwen prompted to list PII | Understands narrative context ("Rafael's own record") | Slow, non-deterministic, can hallucinate spans. A *cloud* LLM breaks Cadence's policy |
| **Orchestration frameworks** | **Microsoft Presidio** (Analyzer, Anonymizer, Image Redactor) | Runs rules, context boosting, checksums and any NER model side by side, merges results, then masks, replaces or encrypts | Its defaults are US-centric and need custom recognisers |
| **Managed cloud DLP** | Google Sensitive Data Protection, AWS Comprehend PII and Macie, Azure AI Language PII and Purview | 100+ built-in info types, scale, compliance reporting | Raw data leaves the boundary; black-box scores; not demoable offline |
| **Commercial privacy platforms and AI gateways** | Private AI, Nightfall, Tonic Textual, Skyflow, LLM Guard | Pre-LLM redaction proxies with vaulted re-identification | Licensed; same black-box and data-residency questions |

### 3c. How enterprises actually put it together

The mature pattern, whichever vendor is underneath, is a **layered, fail-closed pipeline with a redaction gateway in front of the LLM**:

1. **Ingest and sniff** the true file type (magic bytes, not the extension).
2. **Extract** into one normalised document model that keeps provenance for every text span: file, page or slide, element type (heading, paragraph, table cell, image, metadata) and coordinates.
3. **Detect with an ensemble**: rules and checksums for structured IDs, one or more NER models for names and addresses, and context from table headers.
4. **Resolve**: merge overlapping hits, apply allow-lists (company, product and role names) and propagate every confirmed person across the whole document set.
5. **Route by confidence**: auto-redact high confidence, send the uncertain band to a human reviewer, and never let unreadable content through.
6. **Pseudonymise consistently** (`[PERSON_014]`, `[EMAIL_014]`) with a secured mapping vault, so the LLM can still reason about "who did what" and answers can be re-identified for authorised users.
7. **Log and report**: an exposure register per file and an audit trail of every decision.

---

## 4. What a quick prototype showed on the Cadence files

I ran a throwaway test in this workspace: Tesseract OCR on the 35 PDF pages, native parsing of the DOCX and PPTX, then Microsoft Presidio twice, once with its stock settings and once with a handful of custom recognisers.

| Finding | Evidence |
|---|---|
| OCR is mandatory and cheap | All 35 PDF pages had no text layer. Tesseract read them in about 50 s on 4 CPU cores and recovered 123k characters |
| Stock Presidio misses the PPTX contact data entirely | 0 of 34 e-mails (it rejects the `.example` domain) and 0 of 34 mobiles (it does not know the `+1 (555) 0114` format). With two custom patterns: 34 of 34 each |
| Stock Presidio misses every Cadence identifier | 0 of 189 `EMP-`/`DIR-` ID occurrences, no PAN, no passport. A custom recogniser caught them all |
| Plain OCR damages tables | Multi-line cells merge across columns (`= +1. (212) 555-0193` leaking into a neighbouring cell), which breaks the 80% structure target. Needs table-aware OCR |
| Screenshots inside scans are the weakest point | The e-mails in the p19 user list came out garbled (`wilsong@acmeco-p com`). Page-level 300 dpi OCR is not enough; image regions need their own higher-resolution pass or must be masked outright |
| Naive DOCX extraction creates false negatives and false positives together | Glued cell text (`Rob TilneyJyoti KarwalJonathan`) confuses the NER, while field names such as `Vendor Tier` and `QRC_BYOD` are tagged as people |
| Test-format identifiers defeat strict validation | The SSNs start with `900` and phones use `555`. A strict validity check would *reject* them and report a miss. Checksums should raise or lower confidence, never veto an ID that has a context word such as "SSN" or "passport" next to it |
| Naive DOB rules over-fire | A generic date pattern flagged policy approval dates as dates of birth. DOB needs context gating ("DOB", "born", "date of birth") |

The lesson matches the June 2026 benchmark: **no single engine gets near 100%**. Recall comes from layering components that fail in different places, and precision comes from context and allow-lists.

---

## 5. Three options

### Option A: Cloud document AI plus cloud PII API
*Azure Document Intelligence and Azure AI Language PII, or the AWS equivalents.*

Fastest to high-quality tables and OCR. **It contradicts the client's own policy**, because raw PII is sent to an external AI model in order to find it. It is also hard to explain score by score and needs paid accounts for the demo. Good to mention as the "enterprise at scale" alternative, not to build.

### Option B: Presidio with its defaults
Quick to stand up, fully offline. The prototype shows it misses most of what these files contain (Cadence IDs, `.example` e-mails, `555` phones, image-only text, narrative context). It would fail the ~100% recall requirement.

### Option C (recommended): A layered, offline hybrid pipeline, orchestrated by Presidio

```
             ┌─────────────────────────── EXTRACTION ───────────────────────────┐
 Upload ──►  │ Type sniff ─► PDF: text layer? yes → PyMuPDF │ no → OCR          │
             │               DOCX: OOXML walk (body, tables, text boxes,        │
             │                     headers, footers, comments, metadata, images)│
             │               PPTX: shapes, groups, tables, notes, metadata,     │
             │                     images                                       │
             │   OCR: Tesseract / PaddleOCR with word boxes and confidence;     │
             │        embedded images cropped and re-OCR'd at higher dpi        │
             │   Output: Markdown + JSON "span map" (file, page, element,      │
             │           cell, bbox, OCR confidence)                            │
             └──────────────────────────────┬───────────────────────────────────┘
                                            ▼
             ┌──────────────────────────── DETECTION ───────────────────────────┐
             │ L1 Rules + checksums: e-mail, phones (libphonenumber), EMP/DIR/   │
             │    MER IDs, SSN, PAN, PESEL, passport, card (Luhn), IBAN, DOB     │
             │ L2 NER: GLiNER-PII (zero-shot labels) + spaCy                     │
             │ L3 Structure: table header → column type ("EMAIL", "NATIONAL ID")│
             │ L4 Propagation: every confirmed person searched across all files │
             │    (full name, surname, initial + surname, possessive)           │
             │ L5 Optional local LLM verifier on narrative paragraphs only      │
             │ Resolver: merge overlaps, allow-list (Cadence, Optiv, OneTrust,  │
             │           job titles), confidence score + reason per hit         │
             └──────────────────────────────┬───────────────────────────────────┘
                                            ▼
             ┌────────────────────── REDACTION & OUTPUT ────────────────────────┐
             │ Fail-closed gate: low-confidence OCR regions and unread images   │
             │   are masked, never passed through                               │
             │ Stable tokens: [PERSON_007], [EMAIL_007] consistent across files │
             │ Outputs: redacted text for the LLM · masked PDF/DOCX/PPTX        │
             │          · PII exposure report (per file, category, location)    │
             │          · audit log · recall/precision vs gold labels           │
             └──────────────────────────────────────────────────────────────────┘
```

**Why this one**

- **It is compliant by design.** Everything runs locally, so no PII reaches any AI model before redaction, which is the literal policy requirement.
- **It is the only realistic route to "approaching 100%" recall.** Each layer covers another's blind spot: rules catch structured IDs that NER ignores, NER catches names that rules cannot express, table headers catch values in labelled columns, and propagation catches the "Rafael's own record" mentions that everything else misses.
- **It is explainable.** Every hit carries the recogniser that fired, its score, the context words that boosted it, and its exact location. That is the "transparent tags and source traceability" requirement.
- **It is what practitioners actually ship.** Presidio is the de facto open-source orchestrator, and the model underneath can be swapped as better PII models appear.
- **It demos well.** A Streamlit app (or a Colab notebook) can show the upload, the extracted Markdown side by side with the original, highlighted PII, the exposure table and the redacted output the LLM would receive.

### How we prove the numbers
The brief sets targets, so the deck should show measured results, not claims:

- **Gold labels.** Hand-annotate every PII instance in the three files once (a spreadsheet of file, location, text, category). This is a few hours of work and it is what makes the recall figure credible.
- **Recall and precision** per category and per source type (native text, table, narrative, image-only). This directly answers E.6 prompts 2 and 3.
- **Structure retention.** For DOCX and PPTX, compare headings, tables, rows and cells extracted against the file's own counts. For the scanned PDF, compare against a hand transcription of a sample of pages and report a character or word-level similarity score.

### False-positive controls (E.6 prompt 6)
- **PAN**: `AAAAA9999A` shape, 4th letter must be a valid holder type (P, C, H, F, …), plus a context word nearby.
- **PESEL**: 11 digits, embedded date must be valid, weighted checksum, and context. Partially masked values such as `890412xxxxx` still count as PII because the date of birth is visible.
- **TIN / SSN**: area and group rules raise or lower confidence, but test-range values (`9xx`) are kept when a label is present.
- **Card numbers**: Luhn check, IIN prefix, and a "card", "ending" or "PAN" context word. Last-four references ("card ending 2218") are flagged as partial identifiers.
- **Allow-list** of organisation, product, system and role names; **deny-list** built from confirmed people so that surname-only mentions are caught.

---

## 6. Decisions to make together before building

1. **Demo surface**: Streamlit web app (recommended; looks like a product and runs from a laptop) or a Jupyter/Colab notebook (simpler to share).
2. **OCR engine**: Tesseract (light, installed in seconds) or PaddleOCR (better on the small screenshot text, heavier). Recommended: Tesseract first, with PaddleOCR or Docling swapped in if the screenshot recall is too low.
3. **Local LLM verifier**: include it for the narrative sections, or leave it as a "future enhancement" slide. Recommended: leave it as optional, since the other layers should already cover §16.5 and §16.8.
4. **Team split**: your group can divide naturally into extraction, detection, evaluation and deck.

## 7. Suggested timeline to 14 Oct

| Dates | Work |
|---|---|
| 1 to 3 Oct | Extraction layer for all three formats with the span map; gold-label the three files |
| 4 to 7 Oct | Detection layers, resolver, stable tokens, exposure report; first recall and precision numbers |
| 8 to 10 Oct | Close recall gaps (screenshots, narrative), redacted file output, Streamlit UI |
| 11 to 13 Oct | Diagrams, deck, rehearsal of the 10 to 15 minute walkthrough |

---

### Sources
- [Benchmarking Open-Source PII Detection Across Domains (Albert Sikkema, June 2026)](https://albertsikkema.com/python/security/privacy/2026/06/01/benchmarking-open-source-pii-detection.html)
- [Best Open Source Models for PII Redaction (Grepture)](https://grepture.com/blog/best-open-source-models-pii-redaction)
- [GLiNER2-PII (Fastino)](https://fastino.ai/blog/gliner2-pii-open-source-privacy-filtering-with-pii-detection)
- [knowledgator/gliner-pii-base-v1.0 (Hugging Face)](https://huggingface.co/knowledgator/gliner-pii-base-v1.0)
- [Using GLiNER within Presidio](https://presidio.dataprivacystack.org/samples/python/gliner/)
- [Presidio Image Redactor](https://microsoft.github.io/presidio/image-redactor/)
- [Docling vs Unstructured PDF accuracy benchmark (Ertas AI)](https://www.ertas.ai/blog/pdf-parsing-accuracy-benchmark-docling-unstructured)
- [PDF Data Extraction Benchmark: Docling, Unstructured, LlamaParse (Procycons)](https://procycons.com/en/blogs/pdf-data-extraction-benchmark/)
- [Best Open Source OCR Tools 2026 (Unstract)](https://unstract.com/blog/best-opensource-ocr-tools/)
