<div align="center">

<img src="web/public/shield.svg" alt="PII Shield logo" width="72" />

# PII Shield

**Offline, fail-closed PII detection and redaction for PDFs, Office files, images, e-mail and text.**

Hand documents to an LLM without handing over the people inside them.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/dashboard-React%20%2B%20Vite-61dafb?logo=react&logoColor=black)
![Offline](https://img.shields.io/badge/runs-100%25%20offline-2ea44f)
![Fail-closed](https://img.shields.io/badge/design-fail--closed-d03b3b)

[The problem](#the-problem) · [Quick start](#quick-start) · [How it works](#how-it-works) · [Dashboard](#dashboard) ·
[CLI](#command-line) · [Outputs](#outputs-per-run) · [Security](#security-model) · [Quality](#measuring-quality) ·
[Limits](#known-limits) · [History](#how-the-project-got-here)

</div>

---

## The problem

Built for the Optiv VIT case study (Case Study 2: PII detection in text artifacts). The client's policy forbids
personal data from being processed by AI models, yet the documents they want an LLM to work on (a scanned policy
PDF, a training DOCX full of screenshots, an organisation PPTX) are full of names, IDs, e-mails and phone numbers.

The brief asks for three things at once:

- **Redact before the LLM**, with recall approaching 100% and few false positives.
- **Keep the document usable**: at least 80% of its structure, with stable tags so the LLM can still reason
  about who did what.
- **Let nothing unidentified pass downstream**, and show where every finding came from and why.

That rules out the obvious shortcut. Sending the raw files to a cloud OCR, a cloud PII API or a hosted LLM to
*find* the PII is itself PII processed by an AI model. So everything here runs on one machine.

## What PII Shield does

- **Nothing leaves the machine.** No cloud OCR, no cloud PII API, and no LLM is used to find PII. The models are
  small local ones (OCR, a name tagger, a face detector) loaded from pinned files. The test suite runs the
  pipeline with the network blocked and asserts that nothing tried to connect.
- **Fail-closed.** A file that cannot be read produces no output. An uncertain finding is redacted *and* queued
  for a human. A picture nobody could read is blanked. A masked file in which an original value is still
  readable is not released.
- **Many formats.** PDF (text layer and scanned), DOCX, PPTX, XLSX, images, e-mail (`.eml`), CSV and plain text,
  including the places a reader does not see: comments, speaker notes, alt text, document properties, PDF
  bookmarks, hidden sheets, tracked changes, link targets.
- **Consistent tokens.** `Priya Raman` becomes `[PERSON_007]` in every file of the run, and her e-mail becomes
  `[EMAIL_007]`. With a token key the token is the same in every run, and an LLM's answer can be turned back
  into original values from an encrypted vault.
- **A guard for the chat box.** A prompt typed or pasted for an LLM is checked the same way: values become
  tokens before it is sent, and the answer gets them back.
- **A human in the loop.** A reviewer confirms, rejects and adds values in the dashboard; every output is
  written again.
- **Auditable and measurable.** Every decision goes into a hash-chained audit log, each run has a manifest of
  inputs, settings, models and output digests, risk is scored per file, and recall, precision and leaks are
  computed against gold labels.

| | |
|---|---|
| **Extract** | Layout-aware OCR, ruled tables read cell by cell, OOXML walkers for Office files, a span map back to page, element and bounding box |
| **Detect** | Rules and checksums, spaCy NER (optional GLiNER-PII), structural cues, whole-run name propagation, low-confidence OCR guard |
| **Redact** | LLM-ready Markdown, masked copies with layout preserved, metadata cleared, faces / QR codes / unread pictures blanked, redaction profiles |
| **Verify** | A text leak gate, then every masked page and picture is OCR'd again and searched for the original values |
| **Review** | Review queue, add missed values, re-redact; decisions export as gold labels |
| **Report** | Exposure register, exposure score and residual risk, audit chain, run manifest, AES-256-GCM token vault |
| **Measure** | Recall / precision / leaks per category and per source type, structure retention |
| **Explore** | Local React dashboard: scan control, heatmaps, findings in context, original vs masked pages |

## Quick start

Requires Python 3.11+ and, for the dashboard, Node 20+.

```powershell
git clone https://github.com/krishjain-2301/optiv.git
cd optiv

python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt        # exact pins, includes the en_core_web_lg model wheel
python scripts/fetch_models.py         # OCR and face-detection models, ~9 MB, one-time, SHA-256 checked

cd web; npm install; npm run build; cd ..
python app.py                          # http://127.0.0.1:8000, opens the browser
```

On macOS or Linux activate the environment with `source .venv/bin/activate`.

No files to hand? In the dashboard choose **New scan → Run → Run on synthetic samples**: it generates a scanned
PDF, a DOCX and a PPTX with invented people and gold labels, and runs the whole pipeline on them.

Prefer the terminal? Skip the `web/` step and run
`python -m optiv_pii_shield run path/to/*.pdf --out out` ([details](#command-line)).

> **Missing models are errors, not fallbacks.** If the spaCy model, the OCR model, the face model or (when
> requested) GLiNER is missing or does not match its pinned digest, the run stops before any file is read.
> Rules-only runs must be asked for explicitly with `Settings(use_spacy=False)`.

## How it works

```mermaid
flowchart LR
    A[Upload] --> B[Sniff type<br/>magic bytes]
    B --> C[EXTRACT<br/>text, OCR, OOXML]
    C --> D[DETECT<br/>L0 to L4 + resolver]
    D --> E[REDACT<br/>tokens, Markdown, masked files]
    E --> G{Leak gate<br/>text}
    G -- clean --> V{Verify<br/>re-OCR pixels}
    G -- value found --> X[File withheld]
    V -- clean or covered --> F[REPORT<br/>register, score, audit chain, manifest, vault]
    V -- still readable --> X
    F --> R[REVIEW<br/>confirm, reject, add]
    R -- decisions --> E
    F --> H[MEASURE<br/>recall, precision, leaks]
```

### 1. Extract

The true type is read from the file's first bytes, never from its extension.

| Source | What is read |
|---|---|
| PDF | The text layer, or layout-aware OCR when a page has none: ruled tables cell by cell, screenshots inside the page enlarged and read again. Bookmarks and document properties too |
| DOCX / PPTX | Body, tables, text boxes, groups, headers/footers, notes, comments, document properties, customXml, comment and tracked-change authors, embedded images (OCR), alt text, charts, SmartArt, link targets, field codes |
| XLSX | Every sheet (hidden too), cells under their column headers, formulas, comments, properties |
| Images | OCR with per-character positions |
| E-mail (`.eml`) | Address headers (names and addresses apart), subject, body. Attachments are not read and are dropped: scan them as files |
| CSV / TSV, text | Cells under their column headers; paragraphs |

**A file that is only pictures works.** A scanned PDF has no text at all; each page is rendered at 300 dpi and
read by RapidOCR, which returns every word with its position and a confidence. From then on the text is treated
like any other, and the positions are what the masks are drawn from.

Pictures and scanned pages are also searched for **faces** (YuNet) and **QR codes**. Nothing is recognised: a
face is found, never matched to a person.

Refused with a message, never passed on: legacy binary Office and Outlook files (`.doc`, `.xls`, `.ppt`, `.msg`),
password-protected PDFs, PDFs over 2,000 pages, and packages that unpack to more than 2 GB.

Everything lands in a **span map**: file, page or slide, element, table cell and column header, bounding box,
OCR confidence. Every finding points back into it.

### 2. Detect

No LLM decides what is PII. Four layers each look for evidence, and a score decides what happens.

| Layer | Method | Example |
|---|---|---|
| **L1** Rules | Patterns, checksums and context words (inside Presidio) | A 16-digit number that passes the Luhn check near the word "card" |
| **L2** NER | spaCy `en_core_web_lg`, optional GLiNER-PII (inside Presidio) | "Priya Raman noticed that…" |
| **L3** Structure | Column headers, `Label:` fields, document properties | Anything under an "E-mail" column, or after "Full name:" |
| **L4** Propagation | Every confirmed person is searched across all files: surname, initial, possessive, OCR misreadings | "Raman", "Rafael's", "R. Mendoza" |
| **L0** Fail-closed | Identifier-like OCR words below the confidence floor | A blurred string with digits or `@` |
| **L5** Gate and reviewer | A value found in one place is redacted in every other place it appears; a reviewer's additions | |

The **resolver** trims and drops name candidates that are business words, merges overlapping hits, adds a bonus
when layers agree, and routes by score:

| Score | Decision |
|---|---|
| ≥ 0.60 | Redacted automatically |
| 0.35 to 0.60 | Redacted **and** queued for review |
| < 0.35 | Dropped, kept in the audit log with the reason |

Checksums only raise or lower confidence, never veto: a test-range SSN (`9xx`) or a `555` phone next to a label
is still caught, because in production data it would be real.

**Credentials in code and configuration.** Known key formats (AWS, GitHub, GitLab, Slack, Stripe, Google, npm,
PyPI, Hugging Face and others), private key blocks with or without their END line, JWTs, the password inside
`scheme://user:password@host`, `Authorization` and `X-API-Key` headers, and any quoted or `.env` value assigned
to a name that says it is a secret (`db_password`, `API_KEY`, `clientSecret`). The name and the value are both
checked (`detect/secrets.py`): `password_file`, `max_tokens`, `os.environ[...]` and `<your-password>` are not
secrets. A quoted random-looking string with no name to go by lands in the review band.

**Confidential terms.** Project codenames, unreleased product names and internal host names are not personal
data, and are redacted all the same, as `[TERM_...]`, in files and in prompts. They come from `org.yaml`
(`confidential_terms`) and from the prompt guard's Policy step.

**Categories.** Person, e-mail, phone, address (street-suffix and Indian PIN-code forms), date of birth,
employee and vendor IDs, SSN, passport, PAN, Aadhaar, PESEL, UK National Insurance number, voter ID, driving
licence, tax IDs (TIN, EIN, NIP, GSTIN), card, IBAN, bank account / IFSC, UPI ID, IP address, credentials, and
stated health data (a special category), and confidential terms.

### 3. Redact

- **LLM text.** Redacted Markdown with the same headings, tables and page markers as the extracted text.
- **Masked copies.** PDF: true redaction (text and pixels under the box are removed), token printed in the box.
  DOCX / PPTX / XLSX: text nodes rewritten in place, so formatting survives. Text formats are written back in
  their own shape. Page count and layout are kept; author metadata is cleared.
- **What a reader cannot see is removed.** Tracked-change deletions, embedded objects and chart workbooks, the
  thumbnail, image EXIF, PDF annotations, form fields and attachments.
- **Pictures nobody read are blanked.** A picture with no readable text (a photo, a signature, a logo), a
  picture the extractor never visited, every picture when image OCR is off, and every face and QR code.
  Bullets and icons under 32 px are left alone.

**Profiles** set what replaces each category (`--profile`, or the dashboard):

| Profile | Effect |
|---|---|
| `default` | Every category becomes a stable token |
| `gdpr` | Date of birth generalised to its year; health data and credentials masked to the category only |
| `dpdp` | As `gdpr`, and Aadhaar masked to its last four digits |
| `pci` | Card, IBAN and bank account numbers reduced to the last four digits; credentials masked |

Masked and generalised values cannot be rehydrated, by design.

**Keyed tokens.** With `PII_SHIELD_TOKEN_KEY` (or the dashboard field) tokens are an HMAC-SHA256 of the value:
`[PERSON_3FA9C2D1]` is the same person in every run, and cannot be computed from a guessed name without the key.
Without a key, tokens are numbered per run.

### 4. Gate and verify

**Leak gate.** After tokens are assigned, every original value becomes a needle (as written, XML-escaped,
URL-encoded, split across text runs, digits-only for long numbers). Every place a needle appears where no
detector fired becomes a finding of its own. Each masked file is then searched part by part, nested packages
included; if a needle survives anywhere, the file is **not written**.

**Verification.** The gate reads text, and a scanned page is pixels: an unmasked scan passes it. So each masked
PDF page that was OCR'd or holds a picture, each picture left in a masked DOCX/PPTX, and each masked image is
read again by OCR the way extraction read it, and searched for every needle. A value still readable is covered
and the page is read once more; if it cannot be covered, the file is withheld. `--no-verify` turns this off.

### 5. Review, report, rehydrate

- **Review.** A decision is made per value, not per occurrence ("Cloud is not a person" holds everywhere).
  Applying a review writes every output again and appends to the audit log.
- **Report.** Exposure register (CSV/XLSX), a per-file exposure score with a page heatmap and residual risk,
  the audit log, the run manifest and the encrypted vault.
- **Rehydrate.** `rehydrate` (CLI) or **Redaction → Rehydrate** puts original values back into text that holds
  tokens, and records who did it, for which tokens and why.

### 6. Prompt guard

Documents are one way personal data reaches an LLM; the chat box is the other. **Workspace → Prompt guard**
(`optiv_pii_shield/guard.py`) takes text typed or pasted for an LLM and runs it through the same detection
layers, tokens and leak gate. The prompt comes back in its own shape with every value replaced; that text is
what gets sent. The answer is pasted back and its tokens become values again.

- **One conversation, one set of tokens.** A person or value keeps its token from one prompt to the next, and
  a person redacted once is looked for in every later prompt, by surname alone too.
- **Only issued tokens are restored.** A token this conversation did not issue is left as it is.
- **Nothing is stored.** No prompt or answer is kept. Tokens and their values live in memory until the
  server stops or the conversation is forgotten; the log holds counts, never text.
- **Some prompts are refused, not redacted.** Replacing values is not always enough, so a policy decides when
  the whole prompt is blocked. A blocked prompt yields no text to send, issues no tokens and is counted in the
  log. The policy is set in `org.yaml` (`guard_policy`) and can be changed in the page's Policy step.

  | Rule | Default | What it looks at |
  |---|---|---|
  | Source code | block | `detect/code.py` tells that a prompt is code, and roughly which language, from the shape of its lines. It cannot tell whether the code is confidential, so all of it is refused |
  | Classification marking | block | "Confidential", "Internal Use Only", "Do Not Distribute" and the like (`detect/markings.py`). A single word counts only where it is used as a marking |
  | Protected content | block | The prompt shares ten consecutive words, or thirty in all, with a document registered as confidential (below) |
  | Blocked categories | none | Categories whose presence refuses the prompt instead of being replaced, for example cards or confidential terms |
  | Size limit | none | A larger prompt is refused unread. A blunt control, there for comparison |

  Source code, markings and protected content can be set to *warn* (values are replaced, the sender is told) or *allow*.
- **It does not send anything.** The safe text is copied by hand into the LLM. Nothing forces a prompt
  through the guard: that would take a browser extension or a network proxy.

**Protected content.** A trade secret has no shape a rule can match, but text *copied* from a known document
can be recognised. Under the page's Protected content step a confidential document (any format a scan reads,
or pasted text) is read on this machine, reduced to fingerprints and deleted. A fingerprint is a keyed hash of
five consecutive words, so case, punctuation and line breaks do not matter (`optiv_pii_shield/registry.py`).
A prompt that copies from a registered document is refused, with the document's name and the lines that
overlap.

- The registry is a JSON file that outlives the server and is not deleted with the session:
  `$PII_SHIELD_REGISTRY`, else `~/.pii_shield/registry.json`. It holds names and fingerprints, never text.
- It recognises copied wording only. A paraphrase, a translation or a summary is not caught.
- A fingerprint is not the text, but someone holding both the registry file and a candidate text can test
  whether that text was registered. Keep the file where the documents themselves would be kept.

**The Samsung replay.** In March 2023 three Samsung engineers pasted source code and the contents of a meeting
into a public chatbot; the company's first control was a cap of 1,024 bytes per prompt. The page's last step
(`optiv_pii_shield/replay.py`) puts prompts of the same kinds, all invented, through that cap and through the
guard, with two more that show a size cap failing in both directions.

| Scenario | Bytes | 1,024-byte cap | Prompt guard |
|---|---|---|---|
| Incident 1: debug the measurement database loader | 1,524 | refused unread | refused: source code |
| Incident 2: optimise the yield and faulty-equipment code | 459 | **sent as written** | refused: source code |
| Incident 3: turn meeting notes into minutes | 546 | **sent as written** | sent with 10 values replaced |
| A short question with a password in it | 166 | **sent as written** | sent with 2 values replaced |
| A long prompt with nothing sensitive in it | 1,266 | **refused** | sent unchanged |

The cap makes the right call once in five, the guard five times in five. In the third scenario the guard
removes who was in the meeting and the project codename; what was discussed still goes out, because no rule
can tell that a yield figure is a trade secret. The page says so.

## Configuration

**Organisation vocabulary.** Allow-listed product and team names, always-redact names, internal ID formats
(such as `EMP-40718` or `MER-IN-0042`), confidential terms, classification markings and the prompt guard's
policy live in `optiv_pii_shield/data/org.yaml`. Point `PII_SHIELD_ORG_CONFIG`
at another file for another client.

**OCR.** RapidOCR (PP-OCR on ONNX Runtime) installs with pip and needs no system binary. Two settings were
chosen from measurements on real scans:

- the **English** recognition model, because the bundled Chinese model drops spaces between English words and
  breaks name and label detection;
- the angle classifier **off**, because it flipped long lines on upright scans and silently lost sentences.

Masks use the recogniser's per-character positions and are padded, so they err towards covering a neighbouring
character rather than exposing one. Tesseract works with `--ocr tesseract` if `pytesseract` and the binary are
installed.

**GLiNER-PII (optional).** `pip install gliner`, then `python scripts/fetch_models.py --gliner` (caches about
1.8 GB once, at a pinned revision), then pass `--gliner` or use the dashboard toggle. It is off by default:
see [Held-out set](#held-out-set).

**Environment variables**

| Variable | Purpose |
|---|---|
| `PII_SHIELD_VAULT_KEY` | Passphrase for the token vault. Unset: the vault is not saved at all |
| `PII_SHIELD_TOKEN_KEY` | Key for stable tokens across runs |
| `PII_SHIELD_SIGNING_KEY` | Path of an Ed25519 private key (`keygen`); set: run manifests are signed |
| `PII_SHIELD_ORG_CONFIG` | Another organisation vocabulary file |
| `PII_SHIELD_REGISTRY` | Where the registry of protected documents is kept (default `~/.pii_shield/registry.json`) |
| `PII_SHIELD_MAX_UPLOAD_MB` | Dashboard upload limit for one scan (default 300) |
| `PII_SHIELD_REC_MODEL`, `PII_SHIELD_FACE_MODEL` | Another copy of a model file (its digest is then not checked) |

## Dashboard

```powershell
cd web; npm install; npm run build; cd ..   # once, and after changing web/
python app.py                               # http://127.0.0.1:8000, opens the browser
```

A FastAPI server (`server/`) on `127.0.0.1` runs the pipeline and hosts a React dashboard (`web/`). Everything
the page needs is bundled by the build: no CDN, no web fonts, nothing fetched at run time. A scan runs in a
background thread; the page shows its stage, page-by-page progress and elapsed time, and can pause or cancel it
(a cancelled scan's files are deleted). Uploads, extracted text and reports are deleted when the server stops.

| Category | Mode | Steps |
|---|---|---|
| Workspace | New scan | Sources, Detection policy (thresholds, profile, verification, pictures, vault, token key), Run |
| | Prompt guard | Check a prompt, Restore the answer, Conversation, Policy, Protected content, Samsung replay |
| Analytics | Overview | Summary, Files, Pipeline |
| | Exposure & risk | Ranking, Page heatmap, Residual risk |
| | Findings | Breakdown, Register, In context, Dropped candidates |
| Documents | Extraction | Preview (PDF page with PII boxed, beside the masked page), Structure, Span map, Images |
| | Redaction | LLM text, Token map, Leak gate, Rehydrate |
| Assurance | Review | Review queue, Add missed, Apply (outputs are written again) |
| | Evaluation | Gold labels, Scores, Errors, Structure retention (upload a hand transcription for scans) |
| | Reports | Shareable, Sensitive, Session |

**Developing the UI.** Run `python app.py --no-browser` and `npm run dev` in `web/` for hot reload on
<http://localhost:5173>; it forwards `/api` to the Python server. Charts are plain HTML/SVG
(`web/src/components/charts.tsx`).

## Command line

```powershell
python -m optiv_pii_shield run path\to\*.pdf path\to\*.docx --out out
python -m optiv_pii_shield run samples\synthetic\* --out out --gold samples\synthetic\gold_labels.csv
python -m optiv_pii_shield run files\* --out out --profile pci                   # redaction profile
python -m optiv_pii_shield gold-template path\to\files\* --out gold_draft.csv   # bootstrap gold labels, then correct by hand
python -m optiv_pii_shield vault-open out\token_vault.SENSITIVE.enc.json        # decrypt the token vault (logged)
python -m optiv_pii_shield rehydrate out\token_vault.SENSITIVE.enc.json answer.txt --purpose "audit query"
python -m optiv_pii_shield keygen keys                                          # Ed25519 pair for signing manifests
python -m optiv_pii_shield verify-run out                                       # manifest, output digests, audit chain
```

Useful `run` flags: `--gliner`, `--ocr tesseract`, `--no-images` (pictures are blanked instead of read),
`--no-verify`, `--keep-unread-images`, `--no-faces`, `--operator NAME`, `--transcriptions DIR`.

## Python API

```python
from optiv_pii_shield import run, Settings

res = run(["policy.pdf"], Settings(), out_dir="out")
res.redacted["policy.pdf"]   # LLM-safe Markdown
res.findings["policy.pdf"]   # findings with location, category, token, score, layer, reasons

from optiv_pii_shield.pipeline import apply_review
from optiv_pii_shield.review import Addition, Decision
apply_review(res, [Decision("PERSON", "Cloud", "reject")], [Addition("R. Mendoza", "PERSON")], operator="me")
```

## Outputs (per run)

`<name>` keeps the extension (`report.docx.redacted.md`), so files that share a stem never overwrite each other;
two inputs with the same name are both kept (`report.pdf`, `report~2.pdf`). Everything with `SENSITIVE` in its
name holds original values; the dashboard's "download all" zip is built from an explicit allow-list of the
shareable files and never includes them.

| File | Contents | Sensitive? |
|---|---|---|
| `<name>.extracted.SENSITIVE.md` | Faithful Markdown of the source (headings, tables, image text) | **yes** |
| `<name>.redacted.md` | Same structure with PII replaced by tokens: **what goes to the LLM** | no |
| `<name>.masked.<ext>` | Masked copy of the original, same page count and layout, metadata cleared; written only if it passes the leak gate and verification | no |
| `pii_exposure_register.csv/.xlsx` | Every finding: file, page, location, source type, category, value (partially masked), token, score, layer, reasons, review | no |
| `pii_exposure_register.SENSITIVE.csv` | The same with full original values | **yes** |
| `summary.json` | Per-file counts: OCR pages, images and their status, categories, faces and QR codes, verification, exposure, warnings | no |
| `audit_log.jsonl` | Append-only, hash-chained: the run, every decision including dropped candidates (values partially masked), reviews, re-identifications | no |
| `run_manifest.json` | Operator, inputs and their SHA-256, settings, versions, model digests, gate and verification outcome per file, SHA-256 of every output, audit head; Ed25519 signature when a key is set | no |
| `token_vault.SENSITIVE.enc.json` | Token → original value, AES-256-GCM, key from scrypt; written only when a passphrase is set | **yes** |
| `evaluation.json` | With `--gold`: recall, precision, leaks, per-category/source breakdown, structure retention | no |

### Exposure score

Each file gets an exposure profile (`optiv_pii_shield/exposure.py`, weights in `config.py`):

- **score**: the sum of sensitivity weights (1 to 10) over every instance found: credentials and government or
  financial IDs 8 to 10, date of birth 6, address 5, phone and e-mail 4, name 3, vendor ID 1.
- **per_1k_words**, so long and short files compare; **by_page** for the page heatmap.
- **rating**: `critical` if any instance weighs 9 or more, else `high` / `medium` / `low` by density.
- **residual** after redaction, in three parts kept apart because they are known to different degrees:
  `known` (what the gate had to catch, and whether the masked copy was written), `unreadable` (pictures
  withheld, OCR words masked), and `estimated_missed` (found × miss rate from the held-out set; an estimate,
  with its basis recorded).

## Security model

**The server** holds original values, so it answers this machine's browser only:

- A request must name a loopback Host. A page that re-points its own domain at 127.0.0.1 (DNS rebinding) is refused.
- A request that changes anything must come from the dashboard's own origin.
- Responses carry a strict Content-Security-Policy and `no-store`; uploads are capped and read in pieces.
- There is no login: anyone with a session on the machine can open it. It is a single-user local tool.

**The audit trail**

- `audit_log.jsonl` is a hash chain: a changed, removed or reordered line breaks it from that point on.
- `run_manifest.json` pins every input and output by SHA-256 and records the settings and model digests.
- `verify-run` recomputes all of it. The chain shows the log was not edited; only a signature shows who wrote it,
  so without a signing key someone able to rewrite the folder could rewrite the manifest too.

**The supply chain**

- OCR and face models are pinned by SHA-256 (`optiv_pii_shield/modelstore.py`) and checked at download and at
  every load. GLiNER is pinned to a revision.
- Python dependencies are pinned to exact versions and audited in CI (`pip-audit`, `npm audit`). They are not
  yet hash-locked.

**Working files.** Uploads and outputs live in one session folder; it is emptied before each run, on request and
when the server stops, and folders left by a killed server are swept at the next start.

## Measuring quality

Recall is only reported against gold labels. Format (one row per PII instance):

```csv
file,page,text,category,context_type,note
risk_policy.pdf,3,Priya Raman,PERSON,narrative,
org_pack.pptx,1,+1 (555) 0114,PHONE_NUMBER,table,
```

`context_type` ∈ `native | labelled | table | narrative | image | metadata` drives the per-source breakdown.
Structure retention is computed from the raw OOXML for DOCX/PPTX, and against a hand transcription
(`--transcriptions dir/` with `<stem>.txt`, or an upload in the dashboard) for scanned PDFs.

The practical way to build gold labels for real files: run a scan, work through **Assurance → Review**, then
download the result as gold labels. Reviewed rows are marked; the rest still say "auto - verify".

### Synthetic fixtures

`python scripts/make_samples.py samples/synthetic` builds a scanned PDF, a DOCX and a PPTX with invented people
that reproduce the traps found in the case-study samples (no text layer, ruled tables with multi-line cells,
screenshot text, unlabelled narrative with possessives and surname-only mentions, `.example` e-mails,
`+1 (555) 0114` phones, glued DOCX cells, field names that look like names, author metadata) and writes
`gold_labels.csv`. On these: recall 1.000, precision 0.987 on 76 labels, 0 leaks.

### Held-out set

Those fixture scores are optimistic: the fixtures were written alongside the detectors.
`scripts/make_heldout.py` generates a separate set with Faker (en_IN, pl_PL, de_DE, es_ES, en_GB, en_US), with
ALL-CAPS, lowercase and OCR-noise variants, sentences with and without keywords, and decoy paragraphs. Leaks are
counted strictly on the LLM text (a surname left beside a token is a leak). Seed `dev` is used for fixing general
failure classes; seed `test` is reported (`tests/test_heldout.py`).

Test seed, 219 instances (`en_core_web_lg`):

| | caught |
|---|---|
| Phones, e-mails, IPs, IBANs, cards, SSN, Aadhaar | 104/104 |
| Person names, all variants | 90/115 |
| ... Title case | 58/60 |
| ... ALL CAPS | 20/23 |
| ... OCR noise (1–2 digit-for-letter swaps) | 9/15 |
| ... lowercase | 3/17 |
| Decoy paragraphs (false positives) | 0 tokens in 20 |

The test seed was inspected once, before one fix: a labelled Aadhaar starting with `1` leaked and the Aadhaar
rule was widened (labelled → review band). Treat the Aadhaar line as no longer held out.

GLiNER (`knowledgator/gliner-pii-base-v1.0`) was measured once on the test seed on 2026-10-03: person names
93/115 instead of 90/115 (lowercase unchanged), structured identifiers still 104/104, but 8 tokens in the 20
decoy paragraphs instead of 0, and detection about 10x slower on CPU. A small recall gain bought with false
positives, so it stays off by default.

## Known limits

- **Recall on the real case-study files is not yet measured.** Their gold labels are a machine-made draft
  awaiting a hand-check; only the fixture and held-out numbers above are measured.
- **Names with no evidence leak.** Lowercase names whose given name is not in
  `optiv_pii_shield/data/given_names.txt`, non-English names that the English model does not tag and that no
  keyword, header or confirmed mention supports, and names garbled by OCR beyond one or two digit confusions.
  The Review page exists to close this gap by hand.
- **NER is English.** Names in other scripts are found only by structure, the gazetteer or a reviewer.
- **OCR reads print, not handwriting.** Handwritten names and signatures drawn on a scanned page are mostly
  missed; a signature stored as its own picture is blanked with the other unread pictures.
- **Verification is a second OCR reading, not a proof.** It catches a mask that missed its target, not text the
  engine cannot read either way.
- **Not read:** e-mail attachments, legacy binary Office and Outlook files, password-protected PDFs.
- **One user, one session.** There is no login, queue or multi-user separation.

## Tests

```powershell
pytest -q            # 231 tests, a few minutes on CPU
ruff check .
cd web; npm run typecheck
```

- **Units**: validators, rules, name handling, structure, layout detection.
- **Pipeline**: the synthetic fixtures end to end, with recall and precision floors, leaks in the LLM text and
  masked files, token stability, traceability of every finding, structure retention, reports. This run happens
  with sockets and name lookups blocked, and a test asserts that nothing tried to connect.
- **Planted leaks** (`tests/test_leaks.py`): values hidden in mailto targets, field codes, tracked deletions,
  descriptions, alt text, PNG metadata, chart caches, embedded workbooks and thumbnails; every part of the masked
  files is searched for them.
- **Hardening** (`tests/test_hardening.py`): an unmasked scan passes the text gate and is caught by re-OCR;
  blanked pictures; bookmarks; text, CSV and e-mail inputs; review; the audit chain and manifest (a removed
  line, an edited line and an edited output are each detected); signing; keyed tokens, profiles, rehydration.
- **Prompt guard** (`tests/test_guard.py`): a prompt keeps its shape, tokens hold across prompts, known people
  are found again, only issued tokens are restored, the log holds no text.
- **Code and secrets** (`tests/test_code_and_secrets.py`): credentials in code, config and URLs; names and
  placeholders that are not secrets; six kinds of code recognised and five kinds of prose left alone.
- **Guard policy** (`tests/test_guard_policy.py`): code, markings, blocked categories and the size limit each
  refuse a prompt; a refused prompt issues no tokens; confidential terms; the policy over HTTP.
- **Protected content** (`tests/test_registry.py`): no text in the registry file; copied wording recognised
  whatever its case and layout; a paraphrase is not; the guard refuses, warns or allows; the registry over HTTP.
- **Replay** (`tests/test_replay.py`): the outcome of each Samsung scenario under the cap and under the guard.
- **Server** (`tests/test_server.py`): progress, pause and cancel, the Host and origin guard, the upload cap,
  review and rehydrate over HTTP, shareable downloads free of original values.

CI (`.github/workflows/ci.yml`) installs the pinned dependencies, lints, audits the pins, fetches the models
with their digest check, runs the suite on Windows, and builds and type-checks the dashboard on Linux.

## Project layout

```
optiv_pii_shield/
  config.py            thresholds, header→category map, context words, sensitivity weights, redaction profiles,
                       the prompt guard's policy
  data/                org.yaml (allow-list, deny-list, ID formats, confidential terms, markings, guard policy),
                       given_names.txt (gazetteer)
  models.py            Span / Word / ImageRef / Visual / Document / Finding
  modelstore.py        model files pinned by SHA-256
  extract/             sniff, pdf, docx, pptx, xlsx, image, plain (text/CSV/e-mail), ooxml walkers, layout
                       (tables/regions), ocr backends, visual (faces, QR codes)
  detect/              rules + validators (L1), ner (L2), structure (L3), propagation (L4), resolver, secrets
                       (names and values of credentials), code (is this text source code?), markings
                       (classification markings)
  redact/              tokens (vault, keyed tokens, profiles, rehydrate), text (LLM output), files (masked
                       copies), leakcheck (text gate), verify (re-OCR of masked copies)
  review.py            review queue, reviewer decisions and additions
  guard.py             prompt guard: a prompt's values replaced by tokens, and the answer's put back
  registry.py          protected content: documents registered by fingerprint, and what a text overlaps
  replay.py            the Samsung incidents of 2023 under a size cap and under the guard
  audit.py             hash-chained audit log, run manifest, signing, verify-run
  exposure.py          exposure score and residual risk
  workspace.py         per-session working folders and their removal
  render.py            spans → Markdown (shared by extracted and redacted views)
  report.py            exposure register, summary, audit records
  evaluate.py          gold labels, recall/precision/leaks, structure retention
  pipeline.py, cli.py
app.py                 starts the dashboard server and opens the browser
server/
  api.py               FastAPI routes, the request guard, hosting of web/dist
  session.py           the one local session: working folder, background scan and review jobs, result
  payloads.py          the JSON the dashboard is sent: the run, one document's detail, the evaluation
web/                   React + TypeScript dashboard (Vite)
  src/api.ts           types of the server's JSON and the calls that fetch it
  src/store.tsx        app state: current run, job in progress, scan settings, queued files
  src/components/      header, step bar, stat tiles, cards, charts, table, form controls
  src/pages/           one file per mode: Scan, Guard, Overview, Exposure, Findings, Extraction, Redaction,
                       Review, Evaluation, Reports
  src/lib/entities.ts  entity labels, category groups and chart colours
scripts/               make_samples (fixtures), make_heldout (held-out set), fetch_models, run_extract
tests/
01-landscape-and-recommendation.md   the analysis written before the build (a decision record)
```

## How the project got here

| Date | What changed |
|---|---|
| 30 Sep 2026 | Landscape study of extraction and detection approaches; Option C chosen (a layered, offline pipeline orchestrated by Presidio). First pipeline: extraction for PDF, DOCX and PPTX, the four detection layers, stable tokens, masked files, reports, gold-label evaluation |
| 2 Oct 2026 | An external review found leaks. Fixed: names in any letter case and with accents, hidden OOXML parts, an unsafe download zip, coverage gaps. Added the **leak gate**, the Faker held-out set, hard failure on missing models, the encrypted vault, session clean-up, the exposure score with page heatmap and residual risk, XLSX, organisation vocabulary in YAML, pinned dependencies and CI. Package renamed to `optiv_pii_shield` |
| 3 Oct 2026 | GLiNER measured and constrained (hits must look like a value of their category). The Streamlit demo replaced by a FastAPI server and a React dashboard. README and UI polish |
| 7 Oct 2026 | A second review showed the leak gate could not see pixels. Added the **re-OCR verification** of masked copies, blanking of faces, QR codes and unread pictures, the **review** workflow with re-redaction, **rehydration**, keyed tokens, redaction profiles, the **hash-chained audit log and run manifest** with signing, model pinning by SHA-256, the server's request guard and upload cap, text / CSV / e-mail inputs, PDF bookmarks, new identifier categories, and an offline test |

## Contributing

1. Branch from `main`, keep commits small and focused.
2. Run `pytest -q`, `ruff check .` and `cd web && npm run typecheck` before pushing.
3. Open a pull request describing what changed and why.

Never commit real documents or real PII; use the synthetic fixtures.

No licence has been chosen yet, so the code is not licensed for reuse.
