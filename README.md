<div align="center">

<img src="web/public/shield.svg" alt="PII Shield logo" width="72" />

# PII Shield

**Offline, fail-closed PII detection and redaction for PDFs, Office files and images.**

Hand documents to an LLM without handing over the people inside them.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/dashboard-React%20%2B%20Vite-61dafb?logo=react&logoColor=black)
![Offline](https://img.shields.io/badge/runs-100%25%20offline-2ea44f)
![Fail-closed](https://img.shields.io/badge/design-fail--closed-d03b3b)

[Quick start](#quick-start) · [How it works](#how-it-works) · [Dashboard](#dashboard) · [CLI](#command-line) · [Python API](#python-api) · [Quality](#measuring-quality) · [Layout](#project-layout)

</div>

---

## Why PII Shield

Sending contracts, scans and spreadsheets to a cloud LLM leaks names, IDs, e-mails and credentials. PII Shield
sits in front of the model: it finds personal data locally, replaces it with stable tokens, and proves nothing
slipped through before anything is written to disk.

- **Nothing leaves the machine.** No cloud OCR, no cloud PII API, and no LLM is used to *find* PII.
- **Fail-closed.** Unparseable files produce no output. Uncertain findings are redacted and queued for review.
  A leak gate refuses to write any masked file that still contains an original value.
- **Many formats.** PDF (text layer and scanned), DOCX, PPTX, XLSX and images, including hidden content such as
  comments, speaker notes, alt text, document properties, hidden sheets and tracked changes.
- **Consistent tokens.** `Priya Raman` becomes `[PERSON_007]` in every file of the run, so the LLM can still
  reason about who is who.
- **Auditable and measurable.** Every decision is logged, risk is scored per file, and recall, precision and
  leaks are computed against gold labels.

## Features at a glance

| | |
|---|---|
| **Extract** | Layout-aware OCR, ruled tables read cell by cell, OOXML walkers for Office files, span map back to page, element and bounding box |
| **Detect** | Rules and checksums, spaCy NER (optional GLiNER-PII), structural cues, whole-run name propagation, low-confidence OCR guard |
| **Redact** | LLM-ready Markdown, masked PDF/DOCX/PPTX/XLSX with layout preserved, metadata cleared, leak gate |
| **Report** | Exposure register (CSV/XLSX), exposure score and residual risk, audit log, AES-256-GCM token vault |
| **Measure** | Recall / precision / leaks per category and per source type, structure retention |
| **Explore** | Local React dashboard with scan control, heatmaps, findings in context, and evaluation views |

Built for the Optiv VIT case study (Case Study 2). The design rationale is in
[`01-landscape-and-recommendation.md`](01-landscape-and-recommendation.md) (Option C).
