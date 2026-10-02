"""HTTP API and static hosting for the dashboard.

    /api/scan            GET status · POST start (files or synthetic) · /pause /resume /cancel
    /api/run             GET the whole run (files, findings, tokens, outputs)
    /api/run/doc         GET one document's detail      /api/run/page   GET a PDF page as PNG
    /api/run/evaluation  GET scores vs gold labels      /api/run/gold   POST labels · GET a draft
    /api/run/output      GET one output file            /api/run/outputs.zip  GET the shareable set
    /api/session         DELETE this session's files

Everything else is the built dashboard (web/dist) with index.html as the fallback for its routes.
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from optiv_pii_shield import Settings
from optiv_pii_shield.evaluate import gold_template

from . import payloads
from .session import ACTIVE, Busy, Session

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "web" / "dist"
sys.path.insert(0, str(ROOT / "scripts"))  # make_samples (synthetic fixtures)

UPLOAD_TYPES = {".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
session = Session()


@asynccontextmanager
async def lifespan(_: FastAPI):
    session.open()
    yield
    session.close()  # uploads, extracted text and reports do not outlive the server


app = FastAPI(title="PII Shield", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")


def _json(payload) -> Response:
    def default(o):
        if hasattr(o, "item"):  # numpy scalars (OCR confidences)
            return o.item()
        if isinstance(o, (set, tuple)):
            return list(o)
        return str(o)

    return Response(json.dumps(payload, default=default, ensure_ascii=False), media_type="application/json")


def _result():
    if session.result is None:
        raise HTTPException(404, "no scan yet")
    return session.result


# ------------------------------------------------------------------------------- settings
class ScanSettings(BaseModel):
    """The part of Settings the dashboard exposes (Detection policy step)."""

    ocr_engine: str = Field("auto", pattern="^(auto|rapidocr|tesseract)$")
    ocr_embedded_images: bool = True
    use_gliner: bool = False
    propagate_persons: bool = True
    redact_threshold: float = Field(Settings.redact_threshold, ge=0.3, le=0.95)
    review_threshold: float = Field(Settings.review_threshold, ge=0.1, le=0.95)
    low_conf_ocr: float = Field(Settings.low_conf_ocr, ge=0.3, le=0.9)
    extra_allow_list: list[str] = []
    deny_list: list[str] = []
    vault_passphrase: Optional[str] = None

    def to_settings(self) -> Settings:
        s = Settings()
        s.ocr_engine, s.ocr_embedded_images = self.ocr_engine, self.ocr_embedded_images
        s.use_gliner, s.propagate_persons = self.use_gliner, self.propagate_persons
        s.redact_threshold = self.redact_threshold
        s.review_threshold = min(self.review_threshold, self.redact_threshold)
        s.low_conf_ocr = self.low_conf_ocr
        s.allow_list = s.allow_list + [x.strip() for x in self.extra_allow_list if x.strip()]
        s.extra_deny_list = [x.strip() for x in self.deny_list if x.strip()]
        s.vault_passphrase = self.vault_passphrase or None
        return s


@app.get("/api/settings")
def default_settings():
    return ScanSettings().model_dump()


# ----------------------------------------------------------------------------------- scan
@app.get("/api/scan")
def scan_status():
    return session.job.status() if session.job is not None else {"state": "idle"}


@app.post("/api/scan")
async def start_scan(settings: str = Form("{}"), synthetic: bool = Form(False),
                     files: list[UploadFile] = File(default=[]), gold: Optional[UploadFile] = File(None)):
    try:
        cfg = ScanSettings.model_validate_json(settings)
    except ValueError as exc:
        raise HTTPException(422, f"settings: {exc}") from exc
    uploads = None
    if not synthetic:
        if not files:
            raise HTTPException(422, "no files to scan")
        bad = [f.filename for f in files if Path(f.filename or "").suffix.lower() not in UPLOAD_TYPES]
        if bad:
            raise HTTPException(422, f"unsupported file type: {', '.join(map(str, bad))}")
        uploads = [(f.filename, await f.read()) for f in files]
    try:
        job = session.start(cfg.to_settings(), uploads, await gold.read() if gold is not None else None)
    except Busy as exc:
        raise HTTPException(409, str(exc)) from exc
    return job.status()


def _active_job():
    if session.job is None or session.job.state not in ACTIVE:
        raise HTTPException(409, "no scan is running")
    return session.job


@app.post("/api/scan/pause")
def pause_scan():
    _active_job().pause()
    return session.job.status()


@app.post("/api/scan/resume")
def resume_scan():
    _active_job().resume()
    return session.job.status()


@app.post("/api/scan/cancel")
def cancel_scan():
    _active_job().cancel()
    return session.job.status()


# ------------------------------------------------------------------------------------ run
@app.get("/api/run")
def get_run():
    res = _result()
    if "run" not in session.cache:
        session.cache["run"] = payloads.run_payload(res, session.out, session.settings, session.gold is not None)
    return _json(session.cache["run"])


@app.get("/api/run/doc")
def get_doc(file: str):
    res = _result()
    if file not in res.docs:
        raise HTTPException(404, "no such file in this run")
    return _json(payloads.doc_payload(res, file))


@app.get("/api/run/page")
def get_page(file: str, page: int):
    res = _result()
    doc = res.docs.get(file)
    if doc is None or doc.file_type != "pdf" or not 1 <= page <= doc.pages:
        raise HTTPException(404, "no such page")
    return Response(payloads.page_png(res, file, page), media_type="image/png")


@app.get("/api/run/evaluation")
def get_evaluation():
    res = _result()
    if "evaluation" not in session.cache:
        session.cache["evaluation"] = payloads.evaluation_payload(res, session.gold)
    return _json(session.cache["evaluation"])


@app.post("/api/run/gold")
async def put_gold(gold: UploadFile = File(...)):
    _result()
    session.set_gold(await gold.read())
    session.cache.pop("run", None)  # has_gold changed
    return get_evaluation()


@app.get("/api/run/gold-draft")
def gold_draft():
    res = _result()
    path = gold_template(res.findings, session.work / "gold_draft.SENSITIVE.csv")
    return FileResponse(path, filename="gold_draft.csv", media_type="text/csv")


@app.get("/api/run/output")
def get_output(name: str):
    res = _result()
    known = {p.name: p for p in payloads.shareable_outputs(res, session.out) + payloads.sensitive_outputs(res, session.out)}
    if name not in known:  # only names from the listing: no paths
        raise HTTPException(404, "no such output")
    return FileResponse(known[name], filename=name)


@app.get("/api/run/outputs.zip")
def get_outputs_zip():
    res = _result()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in payloads.shareable_outputs(res, session.out):
            z.write(p, p.name)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="pii_shield_{res.run_id}.zip"'})


@app.delete("/api/session")
def delete_session():
    try:
        session.delete_files()
    except Busy as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"state": "idle"}


# ------------------------------------------------------------------------------ dashboard
NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>PII Shield</title>
<body style="font:16px system-ui;background:#0b0e14;color:#e6eaf2;padding:3rem">
<h1>The dashboard is not built yet</h1><p>Run this once, then reload:</p>
<pre>cd web
npm install
npm run build</pre>"""

if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def dashboard(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    index = DIST / "index.html"
    if not index.exists():
        return HTMLResponse(NOT_BUILT, status_code=503)
    static = (DIST / path).resolve()
    if path and static.is_file() and DIST.resolve() in static.parents:
        return FileResponse(static)
    return FileResponse(index)
