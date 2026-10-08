"""HTTP API and static hosting for the dashboard.

    /api/scan            GET status · POST start (files or synthetic) · /pause /resume /cancel
    /api/run             GET the whole run (files, findings, tokens, review queue, outputs)
    /api/run/doc         GET one document's detail      /api/run/page   GET a PDF page as PNG (original or masked)
    /api/run/review      POST a reviewer's decisions: the outputs are written again (a background job)
    /api/run/rehydrate   POST text holding tokens: original values put back, logged
    /api/run/evaluation  GET scores vs gold labels      /api/run/gold   POST labels · GET a draft
    /api/run/transcription  POST a hand transcription of one file, for structure retention
    /api/run/output      GET one output file            /api/run/outputs.zip  GET the shareable set
    /api/guard           GET the prompt guard's conversation · DELETE forget it
    /api/guard/check     POST a prompt: its values replaced by tokens, and what was found
    /api/guard/rehydrate POST an answer holding those tokens: original values put back, the rest inspected
    /api/guard/activity  GET the record of every check and restoration, over all conversations
    /api/guard/replay    GET the Samsung scenarios under a size cap and under the guard
    /api/registry        GET protected documents · POST register files or text · DELETE /{id}
    /api/session         DELETE this session's files

Everything else is the built dashboard (web/dist) with index.html as the fallback for its routes.

The server holds original values, so it answers this machine's browser only (see ``guard``): a
request must name a loopback host, and a request that changes something must come from the
dashboard's own origin. Uploads are capped.
"""
from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from optiv_pii_shield import Settings
from optiv_pii_shield.config import ORG, PROFILES, GuardPolicy
from optiv_pii_shield.detect import OUTPUT_ENTITIES
from optiv_pii_shield.errors import ModelMissing
from optiv_pii_shield.evaluate import gold_template
from optiv_pii_shield.guard import activity
from optiv_pii_shield.replay import SAMSUNG_CAP
from optiv_pii_shield.review import Addition, Decision

from . import payloads
from .session import ACTIVE, Busy, Session

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "web" / "dist"
sys.path.insert(0, str(ROOT / "scripts"))  # make_samples (synthetic fixtures)

UPLOAD_TYPES = {".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
                ".txt", ".csv", ".tsv", ".eml", ".vtt", ".srt"}
MAX_UPLOAD = int(os.environ.get("PII_SHIELD_MAX_UPLOAD_MB", "300")) * 1024 * 1024  # all files of one scan together
MAX_TEXT = 2 * 1024 * 1024  # gold labels, transcriptions, text to rehydrate
MAX_PROMPT = 100_000  # characters of one prompt given to the guard
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
session = Session()


@asynccontextmanager
async def lifespan(_: FastAPI):
    session.open()
    yield
    session.close()  # uploads, extracted text and reports do not outlive the server


# No interactive API docs: Swagger UI loads its scripts from a CDN, and nothing here is fetched at run time.
app = FastAPI(title="PII Shield", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")


def _hostname(value: str) -> str:
    """"127.0.0.1:8000" -> "127.0.0.1"; "[::1]:8000" -> "[::1]"."""
    value = value.strip().lower()
    return value[:value.index("]") + 1] if value.startswith("[") else value.rsplit(":", 1)[0] if ":" in value else value


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


@app.middleware("http")
async def guard(request: Request, call_next):
    """Only this machine's browser, only the dashboard's own pages.

    * Host must be a loopback name. A page on another site that re-points its own domain at
      127.0.0.1 (DNS rebinding) still sends its own domain as Host, and is refused.
    * A request that changes state must not come from another origin: a form on any web page can
      POST to 127.0.0.1, and would otherwise start, cancel or delete a scan.
    """
    if _hostname(request.headers.get("host", "")) not in LOOPBACK:
        return JSONResponse({"detail": "this server answers on 127.0.0.1 / localhost only"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        cross = request.headers.get("sec-fetch-site") in ("cross-site", "same-site")
        if cross or (origin is not None and (urlsplit(origin).hostname or "") not in {h.strip("[]") for h in LOOPBACK}):
            return JSONResponse({"detail": "requests from other sites are refused"}, status_code=403)
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"  # responses hold original values
    return response


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


async def _read(upload: UploadFile, limit: int, what: str = "upload") -> bytes:
    """Read an upload in pieces and stop at ``limit``: a request cannot fill the memory."""
    chunks, size = [], 0
    while chunk := await upload.read(1 << 20):
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"{what} is larger than {limit // (1024 * 1024)} MB")
        chunks.append(chunk)
    return b"".join(chunks)


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
    extra_allow_list: list[str] = Field(default_factory=list, max_length=500)
    deny_list: list[str] = Field(default_factory=list, max_length=500)
    confidential_terms: list[str] = Field(default_factory=list, max_length=500)  # added to the organisation's own
    vault_passphrase: Optional[str] = Field(None, max_length=256)
    profile: str = "default"
    verify_outputs: bool = True
    blank_textless_images: bool = True
    detect_faces: bool = True
    token_key: Optional[str] = Field(None, max_length=256)
    operator: Optional[str] = Field(None, max_length=80)

    def to_settings(self) -> Settings:
        if self.profile not in PROFILES:
            raise ValueError(f"unknown profile '{self.profile}'")
        s = Settings()
        s.ocr_engine, s.ocr_embedded_images = self.ocr_engine, self.ocr_embedded_images
        s.use_gliner, s.propagate_persons = self.use_gliner, self.propagate_persons
        s.redact_threshold = self.redact_threshold
        s.review_threshold = min(self.review_threshold, self.redact_threshold)
        s.low_conf_ocr = self.low_conf_ocr
        s.allow_list = s.allow_list + [x.strip() for x in self.extra_allow_list if x.strip()]
        s.extra_deny_list = [x.strip() for x in self.deny_list if x.strip()]
        s.confidential_terms = s.confidential_terms + [x.strip() for x in self.confidential_terms if x.strip()]
        s.vault_passphrase = self.vault_passphrase or None
        s.profile, s.verify_outputs = self.profile, self.verify_outputs
        s.blank_textless_images = self.blank_textless_images
        s.detect_faces = s.detect_qr = self.detect_faces
        s.token_key = self.token_key or None
        if self.operator and self.operator.strip():
            s.operator = self.operator.strip()
        return s


@app.get("/api/settings")
def default_settings():
    return {**ScanSettings().model_dump(), "default_operator": Settings().operator,
            "profiles": [{"name": k, "label": v["label"], "actions": v["actions"]} for k, v in PROFILES.items()],
            "entities": OUTPUT_ENTITIES, "max_upload_mb": MAX_UPLOAD // (1024 * 1024),
            "guard_policy": asdict(GuardPolicy.default()), "markings": ORG["markings"],
            "org_terms": len(ORG["confidential_terms"])}


# ----------------------------------------------------------------------------------- scan
@app.get("/api/scan")
def scan_status():
    return session.job.status() if session.job is not None else {"state": "idle"}


@app.post("/api/scan")
async def start_scan(request: Request, settings: str = Form("{}"), synthetic: bool = Form(False),
                     files: list[UploadFile] = File(default=[]), gold: Optional[UploadFile] = File(None)):
    try:
        cfg = ScanSettings.model_validate_json(settings).to_settings()
    except ValueError as exc:
        raise HTTPException(422, f"settings: {exc}") from exc
    uploads = None
    if not synthetic:
        if not files:
            raise HTTPException(422, "no files to scan")
        bad = [f.filename for f in files if Path(f.filename or "").suffix.lower() not in UPLOAD_TYPES]
        if bad:
            raise HTTPException(422, f"unsupported file type: {', '.join(map(str, bad))}")
        if int(request.headers.get("content-length") or 0) > MAX_UPLOAD + MAX_TEXT:
            raise HTTPException(413, f"the files are larger than {MAX_UPLOAD // (1024 * 1024)} MB together")
        uploads, left = [], MAX_UPLOAD
        for f in files:
            content = await _read(f, left, "the files together")
            left -= len(content)
            uploads.append((f.filename, content))
    try:
        job = session.start(cfg, uploads, await _read(gold, MAX_TEXT, "the gold file") if gold is not None else None)
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
def get_page(file: str, page: int, masked: bool = False):
    res = _result()
    doc = res.docs.get(file)
    if doc is None or doc.file_type != "pdf" or not 1 <= page <= doc.pages:
        raise HTTPException(404, "no such page")
    if masked and f"masked:{file}" not in res.outputs:
        raise HTTPException(404, "this file has no masked copy (withheld)")
    return Response(payloads.page_png(res, file, page, masked), media_type="image/png")


# --------------------------------------------------------------------------------- review
class ReviewDecision(BaseModel):
    entity_type: str = Field(max_length=40)
    value: str = Field(max_length=500)
    action: str = Field(pattern="^(approve|reject)$")


class ReviewAddition(BaseModel):
    text: str = Field(min_length=2, max_length=500)
    entity_type: str = Field(max_length=40)
    file: Optional[str] = None


class ReviewRequest(BaseModel):
    decisions: list[ReviewDecision] = Field(default_factory=list, max_length=5000)
    additions: list[ReviewAddition] = Field(default_factory=list, max_length=1000)
    operator: Optional[str] = Field(None, max_length=80)


@app.post("/api/run/review")
def post_review(body: ReviewRequest):
    res = _result()
    if not body.decisions and not body.additions:
        raise HTTPException(422, "nothing to apply")
    known = set(OUTPUT_ENTITIES)
    for a in body.additions:
        if a.entity_type not in known:
            raise HTTPException(422, f"unknown category '{a.entity_type}'")
        if a.file is not None and a.file not in res.docs:
            raise HTTPException(422, f"no such file in this run: {a.file}")
    try:
        job = session.review([Decision(d.entity_type, d.value, d.action) for d in body.decisions],
                             [Addition(a.text, a.entity_type, a.file) for a in body.additions],
                             (body.operator or "").strip() or None)
    except Busy as exc:
        raise HTTPException(409, str(exc)) from exc
    return job.status()


class RehydrateRequest(BaseModel):
    text: str = Field(max_length=MAX_TEXT)
    purpose: str = Field("", max_length=300)
    operator: Optional[str] = Field(None, max_length=80)


@app.post("/api/run/rehydrate")
def post_rehydrate(body: RehydrateRequest):
    _result()
    if session.job is not None and session.job.state in ACTIVE:
        raise HTTPException(409, "the outputs are being rewritten; try again when that has finished")
    return _json(session.rehydrate(body.text, (body.operator or "").strip() or None, body.purpose.strip()))


# --------------------------------------------------------------------------- prompt guard
class GuardPolicyBody(BaseModel):
    """When the guard refuses a prompt instead of replacing its values (config.GuardPolicy)."""

    source_code: str = Field(GuardPolicy.default().source_code, pattern="^(block|warn|allow)$")
    markings: str = Field(GuardPolicy.default().markings, pattern="^(block|warn|allow)$")
    block_categories: list[str] = Field(default_factory=lambda: list(GuardPolicy.default().block_categories), max_length=60)
    max_bytes: int = Field(GuardPolicy.default().max_bytes, ge=0, le=MAX_PROMPT * 4)
    protected: str = Field(GuardPolicy.default().protected, pattern="^(block|warn|allow)$")

    def to_policy(self) -> GuardPolicy:
        unknown = set(self.block_categories) - set(OUTPUT_ENTITIES)
        if unknown:
            raise ValueError(f"unknown category '{sorted(unknown)[0]}'")
        return GuardPolicy(self.source_code, self.markings, list(self.block_categories), self.max_bytes, self.protected)


class GuardCheck(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_PROMPT)
    settings: ScanSettings = Field(default_factory=ScanSettings)
    policy: GuardPolicyBody = Field(default_factory=GuardPolicyBody)


@app.get("/api/guard")
def guard_state():
    return _json(session.guard.state())


@app.post("/api/guard/check")
def guard_check(body: GuardCheck):
    if not body.text.strip():
        raise HTTPException(422, "nothing to check")
    try:
        cfg, policy = body.settings.to_settings(), body.policy.to_policy()
    except ValueError as exc:
        raise HTTPException(422, f"settings: {exc}") from exc
    try:
        return _json(session.check_prompt(body.text, cfg, policy))
    except Busy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ModelMissing as exc:
        raise HTTPException(503, str(exc)) from exc


class GuardAnswer(RehydrateRequest):
    settings: ScanSettings = Field(default_factory=ScanSettings)  # for the look at what else the answer holds


@app.post("/api/guard/rehydrate")
def guard_rehydrate(body: GuardAnswer):
    try:
        cfg = body.settings.to_settings()
    except ValueError as exc:
        raise HTTPException(422, f"settings: {exc}") from exc
    try:
        return _json(session.restore_answer(body.text, (body.operator or "").strip() or None, body.purpose.strip(), cfg))
    except ModelMissing as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/guard/activity")
def guard_activity():
    return _json(activity(session.guard.record_path))


@app.get("/api/guard/replay")
def guard_replay(cap: int = Query(SAMSUNG_CAP, ge=1, le=MAX_PROMPT * 4)):
    try:
        return _json(session.replay(cap))
    except Busy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ModelMissing as exc:
        raise HTTPException(503, str(exc)) from exc


@app.delete("/api/guard")
def guard_forget():
    session.guard.reset()
    return _json(session.guard.state())


# ---------------------------------------------------------------------- protected content
def _registry() -> dict:
    return {"documents": session.registry.list(), "path": str(session.registry.path)}


@app.get("/api/registry")
def registry_list():
    return _json(_registry())


@app.post("/api/registry")
async def registry_add(request: Request, files: list[UploadFile] = File(default=[]), name: str = Form("", max_length=200),
                       text: str = Form("", max_length=MAX_TEXT), operator: str = Form("", max_length=80)):
    if not files and not text.strip():
        raise HTTPException(422, "nothing to register: add a file or paste text")
    bad = [f.filename for f in files if Path(f.filename or "").suffix.lower() not in UPLOAD_TYPES]
    if bad:
        raise HTTPException(422, f"unsupported file type: {', '.join(map(str, bad))}")
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD + 2 * MAX_TEXT:
        raise HTTPException(413, f"the files are larger than {MAX_UPLOAD // (1024 * 1024)} MB together")
    uploads, left = [], MAX_UPLOAD
    for f in files:
        content = await _read(f, left, "the files together")
        left -= len(content)
        uploads.append((f.filename, content))
    try:
        errors = await run_in_threadpool(session.register, uploads, name.strip(), text, operator.strip() or None)
    except Busy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ModelMissing as exc:
        raise HTTPException(503, str(exc)) from exc
    return _json({**_registry(), "errors": errors})


@app.delete("/api/registry/{doc_id}")
def registry_remove(doc_id: str):
    if not session.registry.remove(doc_id):
        raise HTTPException(404, "no such registered document")
    return _json(_registry())


# ----------------------------------------------------------------------------- evaluation
@app.get("/api/run/evaluation")
def get_evaluation():
    res = _result()
    if "evaluation" not in session.cache:
        session.cache["evaluation"] = payloads.evaluation_payload(res, session.gold, session.transcriptions)
    return _json(session.cache["evaluation"])


@app.post("/api/run/gold")
async def put_gold(gold: UploadFile = File(...)):
    _result()
    session.set_gold(await _read(gold, MAX_TEXT, "the gold file"))
    session.cache.pop("run", None)  # has_gold changed
    return get_evaluation()


@app.post("/api/run/transcription")
async def put_transcription(file: str = Form(...), transcription: UploadFile = File(...)):
    res = _result()
    if file not in res.docs:
        raise HTTPException(404, "no such file in this run")
    text = (await _read(transcription, MAX_TEXT, "the transcription")).decode("utf-8-sig", errors="replace")
    session.set_transcription(file, text)
    return get_evaluation()


@app.get("/api/run/gold-draft")
def gold_draft():
    res = _result()
    path = gold_template(res.findings, session.work / "gold_draft.SENSITIVE.csv")
    return FileResponse(path, filename="gold_draft.csv", media_type="text/csv")


# -------------------------------------------------------------------------------- outputs
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
