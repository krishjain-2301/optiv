// Workspace · New scan: sources -> detection policy -> run.
import { ArrowRight, FlaskConical, Pause, Play, Square, Upload, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Select, Slider, Toggle } from "../components/controls";
import { Card, Kpis, Notice, PageHeader, Row, Steps, useStep } from "../components/ui";
import { DECISION_COLOR, NEUTRAL } from "../lib/entities";
import { clock, fileSize, percent } from "../lib/format";
import { isActive, useStore } from "../store";

const ACCEPT = ".pdf,.docx,.pptx,.xlsx,.png,.jpg,.jpeg,.tif,.tiff,.bmp";
const lines = (text: string) => text.split("\n").map((x) => x.trim()).filter(Boolean);

function Sources() {
  const { files, setFiles, gold, setGold } = useStore();
  const [over, setOver] = useState(false);
  const add = (list: FileList | null) => {
    if (!list) return;
    const names = new Set(files.map((f) => f.name));
    setFiles([...files, ...Array.from(list).filter((f) => !names.has(f.name))]);
  };
  return (
    <Row cols="3fr 2fr">
      <Card title="Artifacts" sub="PDF (text or scanned), DOCX, PPTX, XLSX and images">
        <label className={over ? "drop over" : "drop"}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); add(e.dataTransfer.files); }}>
          <Upload size={22} />
          <b>Drop files here, or click to choose</b>
          <span>They stay on this machine.</span>
          <input type="file" multiple accept={ACCEPT} onChange={(e) => { add(e.target.files); e.target.value = ""; }} />
        </label>
        {files.length > 0 && (
          <ul className="file-list">
            {files.map((f) => (
              <li key={f.name}>
                <span title={f.name}>{f.name}</span>
                <em>{fileSize(f.size)}</em>
                <button className="btn icon" aria-label={`Remove ${f.name}`} onClick={() => setFiles(files.filter((x) => x !== f))}><X size={15} /></button>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <div className="stack-v">
        <Card title="Gold labels (optional)" sub="A CSV of known PII, to measure recall and precision">
          <div className="inline">
            <label className="btn">
              <Upload size={16} /> {gold ? "Replace" : "Choose CSV"}
              <input type="file" accept=".csv" hidden onChange={(e) => { setGold(e.target.files?.[0] ?? null); e.target.value = ""; }} />
            </label>
            {gold && <span className="muted">{gold.name}</span>}
            {gold && <button className="btn icon" aria-label="Remove gold labels" onClick={() => setGold(null)}><X size={15} /></button>}
          </div>
        </Card>
        <Card title="No files to hand?" sub="The Run step can generate synthetic artifacts with planted PII and gold labels.">
          <p className="muted">A scanned PDF, a DOCX and a PPTX with invented people.</p>
        </Card>
      </div>
    </Row>
  );
}

function Policy() {
  const { settings: s, setSettings } = useStore();
  return (
    <Row>
      <div className="stack-v">
        <Card title="Extraction" sub="How text is read out of scans and embedded images">
          <Select label="OCR engine" value={s.ocr_engine} onChange={(v) => setSettings({ ocr_engine: v as typeof s.ocr_engine })}
            options={["auto", "rapidocr", "tesseract"].map((v) => ({ value: v, label: v }))} />
          <Toggle label="OCR embedded images and screenshots" checked={s.ocr_embedded_images} onChange={(v) => setSettings({ ocr_embedded_images: v })} />
          <Slider label="Low OCR confidence (fail closed below)" min={0.3} max={0.9} value={s.low_conf_ocr} onChange={(v) => setSettings({ low_conf_ocr: v })} />
        </Card>
        <Card title="Detection layers" sub="Rules, NER and structure always run; these are optional">
          <Toggle label="Propagate confirmed people across files (L4)" checked={s.propagate_persons} onChange={(v) => setSettings({ propagate_persons: v })} />
          <Toggle label="Add the GLiNER-PII model (slower; more names, more false positives)" checked={s.use_gliner} onChange={(v) => setSettings({ use_gliner: v })} />
        </Card>
        <Card title="Token vault" sub="Token → original value, for authorised re-identification">
          <label className="field">
            <span className="field-label">Vault passphrase (optional)</span>
            <input className="input" type="password" autoComplete="new-password" value={s.vault_passphrase ?? ""}
              onChange={(e) => setSettings({ vault_passphrase: e.target.value || null })} />
          </label>
          <p className="muted">With a passphrase the vault is saved encrypted (AES-256-GCM). Without one it is not saved at all.</p>
        </Card>
      </div>
      <div className="stack-v">
        <Card title="Routing thresholds" sub="What happens to a finding, by confidence score">
          <Slider label="Auto-redact at score ≥" min={0.3} max={0.95} value={s.redact_threshold} onChange={(v) => setSettings({ redact_threshold: v })} />
          <Slider label="Review band from score ≥" min={0.1} max={s.redact_threshold} value={s.review_threshold} onChange={(v) => setSettings({ review_threshold: v })} />
          <div className="band">
            <div style={{ flex: s.review_threshold, background: NEUTRAL }}>Dropped</div>
            <div style={{ flex: Math.max(s.redact_threshold - s.review_threshold, 0.001), background: DECISION_COLOR.review }}>Redact + review</div>
            <div style={{ flex: 1 - s.redact_threshold, background: DECISION_COLOR.redact }}>Auto-redact</div>
          </div>
          <div className="band-scale"><span>score 0</span><span>{s.review_threshold.toFixed(2)}</span><span>{s.redact_threshold.toFixed(2)}</span><span>1</span></div>
          <p className="muted">Review-band findings are redacted too, and queued for a human (fail closed).</p>
        </Card>
        <Card title="Vocabulary" sub="One entry per line">
          <label className="field">
            <span className="field-label">Extra allow-list (never a person)</span>
            <textarea className="input" rows={4} defaultValue={s.extra_allow_list.join("\n")} onChange={(e) => setSettings({ extra_allow_list: lines(e.target.value) })} />
          </label>
          <label className="field">
            <span className="field-label">Always-redact names</span>
            <textarea className="input" rows={4} defaultValue={s.deny_list.join("\n")} onChange={(e) => setSettings({ deny_list: lines(e.target.value) })} />
          </label>
        </Card>
      </div>
    </Row>
  );
}

function Progress() {
  const { scan, pause, resume, cancel } = useStore();
  const paused = scan.state === "paused";
  const state = scan.cancel_requested ? "Cancelling…" : paused ? "Paused" : scan.pause_requested ? "Pausing after the current step…" : "Running";
  return (
    <Card title={paused ? "Scan paused" : "Scan in progress"}
      sub="The scan runs on the server: you can open other pages meanwhile. Pause and Cancel take effect at the next page or file.">
      <div className="inline">
        {paused || scan.pause_requested
          ? <button className="btn" onClick={resume} disabled={scan.cancel_requested}><Play size={16} /> Resume</button>
          : <button className="btn" onClick={pause} disabled={scan.cancel_requested}><Pause size={16} /> Pause</button>}
        <button className="btn danger" onClick={cancel} disabled={scan.cancel_requested}><Square size={15} /> Cancel scan</button>
        <span className="muted">{scan.files.join(", ")}</span>
      </div>
      <div className="flow">
        {scan.stages.map((name, i) => {
          const status = i < scan.stage ? "done" : i === scan.stage ? (paused ? "active paused" : "active") : "pending";
          return (
            <div className={`stage ${status}`} key={name}>
              <div className="stage-name">{i + 1} · {name}</div>
              <div className="stage-value"><span className="pulse" />{i < scan.stage ? "Done" : i === scan.stage ? state : "Waiting"}</div>
              <div className="stage-sub">{i === scan.stage ? scan.message : ""}</div>
            </div>
          );
        })}
      </div>
      <div className="progress" role="progressbar" aria-valuenow={Math.round(scan.fraction * 100)} aria-valuemin={0} aria-valuemax={100}>
        <i style={{ width: percent(scan.fraction, 1) }} className={paused ? "paused" : ""} />
      </div>
      <div className="progress-text"><b>{percent(scan.fraction)} complete</b><span>{clock(scan.elapsed)} elapsed</span></div>
    </Card>
  );
}

function RunStep() {
  const { settings: s, files, scan, start, startError, run } = useStore();
  const active = isActive(scan);
  const layers = [s.propagate_persons && "propagation", s.use_gliner && "GLiNER"].filter(Boolean) as string[];
  return (
    <>
      <Kpis tiles={[
        { label: "Files queued", value: files.length, sub: "from the Sources step" },
        { label: "OCR engine", value: s.ocr_engine, sub: `embedded images ${s.ocr_embedded_images ? "on" : "off"}` },
        { label: "Auto-redact at", value: `≥ ${s.redact_threshold.toFixed(2)}`, sub: `review band from ${s.review_threshold.toFixed(2)}` },
        { label: "Optional layers", value: layers.length, sub: layers.join(" · ") || "none" },
        { label: "Token vault", value: s.vault_passphrase ? "Encrypted" : "Not saved", sub: s.vault_passphrase ? "AES-256-GCM" : "no passphrase set" },
      ]} />
      {active ? <Progress /> : (
        <Card title="Run the pipeline" sub="Extract → detect → tokenise → redact → leak gate → report. A new run deletes the previous run's files.">
          <div className="inline">
            <button className="btn primary" disabled={!files.length} onClick={() => start(false)}><Play size={16} /> Run on uploaded files</button>
            <button className="btn" onClick={() => start(true)}><FlaskConical size={16} /> Run on synthetic samples</button>
          </div>
          {startError && <Notice kind="error">{startError}</Notice>}
          {scan.state === "failed" && <Notice kind="error">The scan stopped: {scan.error}</Notice>}
          {scan.state === "cancelled" && <Notice kind="info">Scan cancelled after {clock(scan.elapsed)}. Its files were deleted.</Notice>}
          {run && <p><Link className="link" to="/overview">Open the current run's overview <ArrowRight size={15} /></Link></p>}
        </Card>
      )}
    </>
  );
}

export default function Scan() {
  const [step, setStep] = useStep(3);
  const { scan } = useStore();
  const navigate = useNavigate();
  const active = isActive(scan);
  const wasActive = useRef(active);
  useEffect(() => {
    if (wasActive.current && scan.state === "done") navigate("/overview"); // the scan this page was showing finished
    wasActive.current = active;
  }, [active, scan.state, navigate]);

  return (
    <>
      <PageHeader section="Workspace" title="New scan" subtitle="Choose the artifacts, set the detection policy, run the pipeline." />
      <Steps steps={["Sources", "Detection policy", "Run"]} active={step} onChange={setStep} />
      {step === 0 && <Sources />}
      {step === 1 && <Policy />}
      {step === 2 && <RunStep />}
    </>
  );
}
