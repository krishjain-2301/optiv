"""PII Shield demo UI.   streamlit run app.py

Upload artifacts -> extract -> detect -> classify -> redact, with every finding traceable to its
source. Runs entirely on this machine; nothing is sent to any external service.
"""
from __future__ import annotations

import html
import io
import sys
import zipfile
from pathlib import Path

import pymupdf as fitz
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from pii_shield import Settings, run, workspace  # noqa: E402
from pii_shield.detect import ModelMissing  # noqa: E402
from pii_shield.evaluate import evaluate, gold_template, load_gold, structure_retention  # noqa: E402
from pii_shield.report import file_summary, findings_frame  # noqa: E402

st.set_page_config(page_title="PII Shield", layout="wide")

SHAREABLE_REPORTS = ("pii_exposure_register.csv", "pii_exposure_register.xlsx", "summary.json", "audit_log.jsonl")


def shareable_outputs(res, out: Path) -> list[Path]:
    """Explicit allow-list: only outputs that passed the leak gate or never hold raw values.
    Anything else in the folder (extracted text, vault, full register, files added later) stays out."""
    keep = [p for k, p in res.outputs.items() if k.startswith(("masked:", "redacted:"))]
    keep += [out / n for n in SHAREABLE_REPORTS]
    return sorted(p for p in keep if p.exists() and "SENSITIVE" not in p.name)

COLORS = {
    "PERSON": "#f4a261", "EMAIL_ADDRESS": "#2a9d8f", "PHONE_NUMBER": "#e9c46a", "EMPLOYEE_ID": "#8ab17d",
    "VENDOR_ID": "#8ab17d", "US_SSN": "#e76f51", "PASSPORT": "#e76f51", "IN_PAN": "#e76f51", "PL_PESEL": "#e76f51",
    "TAX_ID": "#e76f51", "NATIONAL_ID": "#e76f51", "CREDIT_CARD": "#d62828", "IBAN_CODE": "#d62828",
    "DATE_OF_BIRTH": "#9d4edd", "ADDRESS": "#577590", "LOW_CONFIDENCE_OCR": "#6c757d", "IN_AADHAAR": "#e76f51",
    "IP_ADDRESS": "#457b9d", "CREDENTIAL": "#000000",
}


# ---------------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("PII Shield")
    st.caption("Offline, fail-closed PII detection for PDF, DOCX, PPTX and images. No data leaves this machine.")
    st.subheader("Settings")
    s = Settings()
    s.ocr_engine = st.selectbox("OCR engine", ["auto", "rapidocr", "tesseract"])
    s.ocr_embedded_images = st.checkbox("OCR embedded images / screenshots", True)
    s.use_gliner = st.checkbox("Add GLiNER-PII model (needs `pip install gliner`)", False)
    s.propagate_persons = st.checkbox("Propagate confirmed people across files (L4)", True)
    s.redact_threshold = st.slider("Auto-redact at score ≥", 0.3, 0.95, s.redact_threshold, 0.05)
    s.review_threshold = st.slider("Review band from score ≥", 0.1, s.redact_threshold, s.review_threshold, 0.05)
    s.low_conf_ocr = st.slider("Low OCR confidence (fail closed below)", 0.3, 0.9, s.low_conf_ocr, 0.05)
    extra_allow = st.text_area("Extra allow-list (one per line)", "")
    extra_deny = st.text_area("Always-redact names (one per line)", "")
    s.allow_list = s.allow_list + [x.strip() for x in extra_allow.splitlines() if x.strip()]
    s.extra_deny_list = [x.strip() for x in extra_deny.splitlines() if x.strip()]
    s.vault_passphrase = st.text_input(
        "Vault passphrase (optional)", type="password",
        help="With a passphrase the token vault is saved encrypted (AES-256-GCM) for authorised "
             "re-identification. Without one it is not saved at all.") or None
    st.divider()
    gold_file = st.file_uploader("Gold labels CSV (optional)", type=["csv"])
    if st.session_state.get("work") and st.button("Delete this session's files now",
                                                  help="Uploads, extracted text, reports and masked copies"):
        workspace.empty(st.session_state["work"])
        st.session_state.pop("result", None)
        st.success("Deleted.")


# ------------------------------------------------------------------------------------ input
st.header("1 · Upload artifacts")
c1, c2 = st.columns([3, 1])
uploads = c1.file_uploader("PDF, DOCX, PPTX or images", accept_multiple_files=True,
                           type=["pdf", "docx", "pptx", "png", "jpg", "jpeg", "tif", "tiff", "bmp"])
use_synth = c2.button("Use synthetic samples", help="Generated test artifacts with planted PII and gold labels")
go = c2.button("Run pipeline", type="primary", disabled=not uploads)


@st.cache_resource
def _sweep_once() -> int:
    """Once per server start: remove folders left by sessions that ended."""
    return workspace.sweep_stale()


_sweep_once()
if "work" not in st.session_state:
    st.session_state["work"] = workspace.new_session()
WORK: Path = st.session_state["work"]


def _fresh_run_dirs() -> tuple[Path, Path]:
    """Everything from the previous run in this session (uploads, outputs, extracted text) is
    deleted before a new run starts."""
    workspace.empty(WORK)
    return (WORK / "in").resolve(), (WORK / "out").resolve()


def _run(paths: list[Path], gold_path: Path | None, out: Path):
    bar = st.progress(0.0, "Starting")
    try:
        res = run(paths, s, out, progress=lambda m, f: bar.progress(min(f, 1.0), m))
    except ModelMissing as exc:
        bar.empty()
        st.error(str(exc))
        st.stop()
    bar.empty()
    st.session_state.update(result=res, out_dir=out, gold_path=gold_path)


if use_synth:
    import make_samples

    d, out = _fresh_run_dirs()
    make_samples.main(str(d))
    _run(sorted(p for p in d.iterdir() if p.suffix in (".pdf", ".docx", ".pptx")), d / "gold_labels.csv", out)
elif go and uploads:
    d, out = _fresh_run_dirs()
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for u in uploads:
        p = d / Path(u.name).name
        p.write_bytes(u.getbuffer())
        paths.append(p)
    gp = None
    if gold_file is not None:
        gp = d / "gold.csv"
        gp.write_bytes(gold_file.getbuffer())
    _run(paths, gp, out)

res = st.session_state.get("result")
if res is None:
    st.info("Upload files and press **Run pipeline**, or try the synthetic samples.")
    st.stop()
if not st.session_state["out_dir"].exists():
    st.session_state.pop("result")
    st.info("This run's files were deleted. Upload files to start again.")
    st.stop()
if gold_file is not None and st.session_state.get("gold_path") is None:
    gp = WORK / "gold.csv"
    gp.write_bytes(gold_file.getbuffer())
    st.session_state["gold_path"] = gp

for f, err in res.errors.items():
    st.error(f"{f}: {err} — file withheld (fail closed)")

files = list(res.docs)
df_all = findings_frame(res.findings)
tabs = st.tabs(["Overview", "Extraction", "PII findings", "Redacted for LLM", "Evaluation", "Downloads"])


# --------------------------------------------------------------------------------- overview
with tabs[0]:
    st.subheader("2 · Overview")
    st.caption("Detection components: " + " · ".join(res.components))
    rows = []
    for f in files:
        sm = file_summary(res.docs[f], res.findings[f])
        rows.append({"file": f, "type": sm["type"], "pages/slides": sm["pages"] or "–",
                     "OCR pages": ", ".join(map(str, sm["ocr_pages"])) or "–", "spans": sm["spans"],
                     "images": sm["images"]["total"], "findings": sm["findings"], "auto-redacted": sm["redacted"],
                     "review queue": sm["review_queue"], "extract s": res.timings.get(f"extract:{f}", "")})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    st.markdown("**Exposure** (sensitivity-weighted PII; see README for weights) and **residual risk after redaction**")
    exp = {f: file_summary(res.docs[f], res.findings[f])["exposure"] for f in files}
    st.dataframe(pd.DataFrame([{
        "file": f, "rating": e["rating"], "score": e["score"], "per 1k words": e["per_1k_words"],
        "masked copy": e["residual"]["known"]["masked_copy"],
        "caught by leak gate": e["residual"]["known"]["llm_text_values_caught_by_gate"]
                               + e["residual"]["known"]["masked_values_caught_by_gate"],
        "unreadable images withheld": e["residual"]["unreadable"]["images_withheld"],
        "est. missed instances": e["residual"]["estimated_missed"]["instances"],
        "est. residual per 1k words": e["residual"]["estimated_missed"]["per_1k_words"],
    } for f, e in exp.items()]), hide_index=True, width="stretch")
    st.caption("Estimated missed = found instances × miss rate measured on the held-out set "
               f"({next(iter(exp.values()))['residual']['estimated_missed']['basis'] if exp else ''}).")
    heat = pd.DataFrame({f: e["by_page"] for f, e in exp.items()}).T.fillna(0.0)
    if not heat.empty:
        heat = heat[sorted(heat.columns, key=lambda c: (c == "document", int(c) if c.isdigit() else 0))]
        top = float(heat.values.max()) or 1.0

        def _shade(v: float) -> str:
            a = min(v / top, 1.0)
            return f"background-color: rgba(214, 40, 40, {a:.2f}); color: {'white' if a > 0.55 else 'inherit'}"

        st.markdown("**Exposure by page / slide** (darker = more sensitive PII on that page)")
        st.dataframe(heat.style.map(_shade).format("{:.0f}"), width="stretch")
    if not df_all.empty:
        a, b = st.columns(2)
        a.markdown("**Findings by category and source**")
        a.dataframe(df_all.pivot_table(index="entity_type", columns="context_type", values="text", aggfunc="count",
                                       fill_value=0), width="stretch")
        b.markdown("**Findings by detection layer**")
        b.bar_chart(df_all["layer"].value_counts())
    for f in files:
        for w in res.docs[f].warnings:
            st.warning(f"{f}: {w}")
        imgs = res.docs[f].images
        if imgs:
            with st.expander(f"{f}: {len(imgs)} image(s)"):
                st.dataframe(pd.DataFrame([{"location": i.location, "status": i.ocr_status, "OCR conf": i.ocr_conf,
                                            "size": f"{i.width}×{i.height}"} for i in imgs]), hide_index=True)


# ------------------------------------------------------------------------------- extraction
def page_preview(doc, page_no: int, findings) -> bytes | None:
    if doc.file_type != "pdf":
        return None
    pdf = fitz.open(doc.path)
    page = pdf[page_no - 1]
    from pii_shield.redact.files import finding_boxes

    for f in findings:
        if f.decision == "drop" or f.page != page_no:
            continue
        span = doc.span(f.span_id)
        for b in finding_boxes(span, f):
            col = COLORS.get(f.entity_type, "#ff0000").lstrip("#")
            rgb = tuple(int(col[i:i + 2], 16) / 255 for i in (0, 2, 4))
            annot = page.add_rect_annot(fitz.Rect(b) + (-1, -1, 1, 1))
            annot.set_colors(stroke=rgb)
            annot.set_border(width=1.2)
            annot.update()
    return page.get_pixmap(dpi=110).tobytes("png")


with tabs[1]:
    st.subheader("3 · Extraction (structure preserved as Markdown)")
    f = st.selectbox("File", files, key="ext_file")
    doc = res.docs[f]
    if doc.file_type == "pdf" and doc.pages:
        pno = st.number_input("Page", 1, doc.pages, 1)
        a, b = st.columns(2)
        img = page_preview(doc, int(pno), res.findings[f])
        a.image(img, caption=f"page {pno} (boxes = detected PII)" + (" · OCR" if pno in doc.ocr_pages else ""))
        page_md = doc.markdown.split(f"<!-- page {pno} -->")
        b.markdown(page_md[1].split("<!-- page")[0] if len(page_md) > 1 else doc.markdown)
    else:
        st.markdown(doc.markdown)
    with st.expander("Structure counts"):
        st.json(doc.structure)
    with st.expander("Span map (provenance of every text element)"):
        st.dataframe(pd.DataFrame([sp.to_dict() for sp in doc.spans]), width="stretch")


# --------------------------------------------------------------------------------- findings
def highlight(text: str, fs) -> str:
    out, pos = [], 0
    for fi in sorted((x for x in fs if x.decision != "drop"), key=lambda x: x.start):
        if fi.start < pos:
            continue
        out.append(html.escape(text[pos:fi.start]))
        c = COLORS.get(fi.entity_type, "#ccc")
        out.append(f'<mark style="background:{c};padding:0 2px;border-radius:3px" title="{fi.entity_type} '
                   f'{fi.score:.2f} {html.escape(fi.layer)}">{html.escape(text[fi.start:fi.end])}'
                   f'<sub style="font-size:0.65em"> {fi.entity_type}</sub></mark>')
        pos = fi.end
    out.append(html.escape(text[pos:]))
    return "".join(out).replace("\n", "<br>")


with tabs[2]:
    st.subheader("4 · Identified and classified PII")
    if df_all.empty:
        st.success("No PII found.")
    else:
        c = st.columns(4)
        ff = c[0].multiselect("File", files, default=files)
        fc = c[1].multiselect("Category", sorted(df_all.entity_type.unique()))
        fd = c[2].multiselect("Decision", ["redact", "review"], default=["redact", "review"])
        fx = c[3].multiselect("Source", sorted(df_all.context_type.unique()))
        view = df_all[df_all.file.isin(ff) & df_all.decision.isin(fd)]
        if fc:
            view = view[view.entity_type.isin(fc)]
        if fx:
            view = view[view.context_type.isin(fx)]
        st.dataframe(view.drop(columns=[c for c in ("span_id", "start", "end") if c in view]), hide_index=True,
                     width="stretch", height=380)
        st.markdown("**In context**")
        f = st.selectbox("File", files, key="hl_file")
        doc = res.docs[f]
        by_span = {}
        for x in res.findings[f]:
            by_span.setdefault(x.span_id, []).append(x)
        shown = 0
        for sp in doc.spans:
            if sp.id in by_span and any(x.decision != "drop" for x in by_span[sp.id]):
                st.markdown(f"<div style='border-left:3px solid #ddd;padding-left:8px;margin:6px 0'>"
                            f"<small style='color:#888'>{html.escape(sp.location)} · {sp.kind} · {sp.source}"
                            f"{' · OCR conf %.2f' % sp.ocr_conf if sp.ocr_conf else ''}</small><br>"
                            f"{highlight(sp.text, by_span[sp.id])}</div>", unsafe_allow_html=True)
                shown += 1
                if shown >= 150:
                    st.caption("… first 150 spans shown")
                    break
    dropped = findings_frame(res.findings, include_dropped=True)
    dropped = dropped[dropped.decision == "drop"]
    with st.expander(f"False-positive controls: {len(dropped)} candidate(s) dropped, with reasons"):
        st.dataframe(dropped[["file", "location", "entity_type", "text", "score", "layer", "reasons"]],
                     hide_index=True, width="stretch")


# --------------------------------------------------------------------------------- redacted
with tabs[3]:
    st.subheader("5 · What the LLM receives")
    st.caption("Review-band findings are redacted too (fail closed). Tokens are stable across files.")
    f = st.selectbox("File", files, key="red_file")
    a, b = st.columns(2)
    a.markdown("**Extracted (contains PII)**")
    a.text_area("orig", res.docs[f].markdown, height=520, label_visibility="collapsed")
    b.markdown("**Redacted (safe for the LLM)**")
    b.text_area("red", res.redacted[f], height=520, label_visibility="collapsed")


# ------------------------------------------------------------------------------- evaluation
with tabs[4]:
    st.subheader("6 · Measured results")
    gp = st.session_state.get("gold_path")
    if not gp:
        st.info("Upload a gold-label CSV in the sidebar to measure recall and precision. "
                "No recall figure is shown without one.")
        tmp = WORK / "gold_draft.SENSITIVE.csv"
        gold_template(res.findings, tmp)
        st.download_button("Download a draft gold file to correct by hand", tmp.read_bytes(), "gold_draft.csv")
    else:
        ev = evaluate(load_gold(gp), res.findings, res.redacted)
        m = st.columns(5)
        m[0].metric("Recall", f"{ev.recall:.1%}", help=f"{ev.recalled}/{ev.total_gold} gold instances")
        m[1].metric("Category recall", f"{ev.category_recall:.1%}")
        m[2].metric("Precision", f"{ev.precision:.1%}")
        m[3].metric("F1", f"{ev.f1:.3f}")
        m[4].metric("Leaks in LLM text", len(ev.leaks))
        a, b = st.columns(2)
        a.markdown("**Recall by source type**")
        a.dataframe(pd.DataFrame(ev.by_context).T, width="stretch")
        b.markdown("**Recall by category**")
        b.dataframe(pd.DataFrame(ev.by_category).T, width="stretch")
        if ev.missed:
            st.markdown("**Missed**")
            st.dataframe(pd.DataFrame(ev.missed), hide_index=True, width="stretch")
        if ev.false_positives:
            st.markdown("**False positives**")
            st.dataframe(pd.DataFrame(ev.false_positives), hide_index=True, width="stretch")
        if ev.leaks:
            st.error("Values present verbatim in the redacted text:")
            st.dataframe(pd.DataFrame(ev.leaks), hide_index=True)
    st.markdown("**Structure retention**")
    st.json({f: structure_retention(res.docs[f]) for f in files})


# -------------------------------------------------------------------------------- downloads
with tabs[5]:
    st.subheader("7 · Outputs")
    out: Path = st.session_state["out_dir"]
    st.caption(f"Run {res.run_id} · written to {out}")
    safe = shareable_outputs(res, out)
    sensitive = [p for p in sorted(out.iterdir()) if p.is_file() and p not in safe]
    st.markdown("**Safe to share** (masked files, LLM text, masked register, summary, audit log)")
    for p in safe:
        st.download_button(p.name, p.read_bytes(), p.name, key=f"dl_{p.name}")
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in safe:
            z.write(p, p.name)
    st.download_button("Download all shareable outputs (.zip)", zbuf.getvalue(), f"pii_shield_{res.run_id}.zip",
                       type="primary")
    with st.expander(f"⚠ Contains original values ({len(sensitive)} file(s)): store securely, never send to an LLM"):
        for p in sensitive:
            st.download_button(p.name, p.read_bytes(), p.name, key=f"dl_{p.name}")
