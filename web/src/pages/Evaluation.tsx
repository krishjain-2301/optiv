// Assurance · Evaluation: gold labels -> scores -> errors -> structure retention.
import { CheckCircle2, Download, Upload } from "lucide-react";
import { useState } from "react";
import { api, type Bucket, type Evaluation as Ev } from "../api";
import { HBar, Meter } from "../components/charts";
import { DataTable } from "../components/table";
import { Card, Kpis, Muted, NeedRun, Notice, PageHeader, Row, Steps, useStep } from "../components/ui";
import { pretty } from "../lib/entities";
import { percent } from "../lib/format";
import { useEvaluation, useStore } from "../store";

const NEED_GOLD = "Recall and precision are only reported against gold labels. Add them in the Gold labels step.";

function Gold({ ev, setEv }: { ev: Ev | null; setEv: (e: Ev) => void }) {
  const { refreshRun } = useStore();
  const [error, setError] = useState<string | null>(null);
  const upload = async (f: File | undefined) => {
    if (!f) return;
    setError(null);
    try {
      setEv(await api.uploadGold(f));
      await refreshRun();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <Row>
      <Card title="Gold labels" sub="One row per PII instance: file, page, text, category, context_type, note">
        <label className="btn">
          <Upload size={16} /> Upload gold CSV
          <input type="file" accept=".csv" hidden onChange={(e) => { upload(e.target.files?.[0]); e.target.value = ""; }} />
        </label>
        {error && <Notice kind="error">{error}</Notice>}
        {ev?.has_gold
          ? <Notice kind="success"><CheckCircle2 size={16} /> Gold labels loaded: {ev.gold_rows} instance(s).</Notice>
          : <Muted>No gold labels yet. No recall figure is shown without them.</Muted>}
      </Card>
      <Card title="Start from a draft" sub="This run's findings as a gold file, to correct by hand">
        <a className="btn" href="/api/run/gold-draft" download><Download size={16} /> Download draft gold file</a>
        <Muted>Contains original values. Add what was missed, delete what is not PII, then upload it.</Muted>
      </Card>
    </Row>
  );
}

function recallRows(buckets: Record<string, Bucket>, label: (k: string) => string = (k) => k) {
  return Object.entries(buckets).map(([k, b]) => ({ label: label(k), value: b.recall, tipLabel: `${percent(b.recall)} · ${b.recalled}/${b.gold}` }));
}

function Scores({ ev }: { ev: NonNullable<Ev["scores"]> }) {
  return (
    <>
      <Kpis tiles={[
        { label: "Recall", value: percent(ev.recall, 1), sub: `${ev.recalled}/${ev.gold_instances} gold instances`, meter: ev.recall },
        { label: "Category recall", value: percent(ev.category_recall, 1), sub: "found with the right category", meter: ev.category_recall },
        { label: "Precision", value: percent(ev.precision, 1), sub: `${ev.true_positive_findings}/${ev.live_findings} findings`, meter: ev.precision },
        { label: "Precision, auto-redact", value: percent(ev.precision_auto_redact, 1), sub: "excludes the review band", meter: ev.precision_auto_redact },
        { label: "F1", value: ev.f1.toFixed(3), sub: "of recall and precision" },
        { label: "Leaks in LLM text", value: ev.leaks.length, status: ev.leaks.length ? "critical" : "good", sub: "gold values present verbatim" },
      ]} />
      <Row>
        <Card title="Recall by category" sub="Gold instances found, per category">
          <HBar unit="Recall" max={1} format={(n) => percent(n)} rows={recallRows(ev.by_category, pretty)} />
        </Card>
        <div className="stack-v">
          <Card title="Recall by source type" sub="Table, labelled field, narrative, image…">
            <HBar unit="Recall" max={1} format={(n) => percent(n)} rows={recallRows(ev.by_context)} />
          </Card>
          <Card title="Recall by file" sub="Gold instances found, per file">
            <HBar unit="Recall" max={1} format={(n) => percent(n)} rows={recallRows(ev.by_file)} />
          </Card>
        </div>
      </Row>
    </>
  );
}

function Rows({ rows }: { rows: Record<string, unknown>[] }) {
  if (!rows.length) return <Muted>None.</Muted>;
  const keys = Object.keys(rows[0]);
  return <DataTable rows={rows} cols={keys.map((k) => ({ key: k, label: k, value: (r: Record<string, unknown>) => (r[k] == null ? null : String(r[k])) }))} />;
}

function Errors({ ev }: { ev: NonNullable<Ev["scores"]> }) {
  return (
    <>
      <Kpis tiles={[
        { label: "Missed", value: ev.missed.length, status: ev.missed.length ? "serious" : "good", sub: "gold instances not found" },
        { label: "False positives", value: ev.false_positives.length, sub: "findings matching no gold value" },
        { label: "Leaks", value: ev.leaks.length, status: ev.leaks.length ? "critical" : "good", sub: "verbatim in the LLM text" },
      ]} />
      <Card title="Leaks" sub="Gold values present verbatim in the redacted text"><Rows rows={ev.leaks} /></Card>
      <Card title="Missed" sub="Gold instances no finding matched"><Rows rows={ev.missed} /></Card>
      <Card title="False positives" sub="Findings that correspond to no gold value in their file"><Rows rows={ev.false_positives} /></Card>
    </>
  );
}

function Retention({ ev, setEv }: { ev: Ev; setEv: (e: Ev) => void }) {
  const [error, setError] = useState<string | null>(null);
  const upload = async (file: string, f: File | undefined) => {
    if (!f) return;
    setError(null);
    try {
      setEv(await api.uploadTranscription(file, f));
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const files = Object.entries(ev.retention);
  const scored = files.map(([, r]) => r.score).filter((s): s is number => s != null);
  const mean = scored.length ? scored.reduce((a, b) => a + b, 0) / scored.length : null;
  const detail = files.flatMap(([file, r]) => (r.score == null ? [] : Object.entries(r.detail ?? {}).flatMap(([el, d]) =>
    typeof d === "number" ? [] : [{ file, el, ...d }])));
  return (
    <>
      <Kpis tiles={[{ label: "Mean retention", value: mean == null ? "–" : percent(mean, 1), sub: `${scored.length} of ${files.length} file(s) have a reference`, meter: mean }]} />
      <Row cols="2fr 3fr">
        <Card title="Retention by file" sub="Share of structural elements kept by extraction">
          <DataTable rows={files} cols={[
            { key: "file", label: "File", value: ([f]) => f },
            { key: "score", label: "Retained", value: ([, r]) => r.score, render: ([, r]) => (r.score == null ? "–" : <Meter value={r.score} label={percent(r.score, 1)} />) },
            { key: "method", label: "Method", value: ([, r]) => r.method, wrap: true },
            { key: "tr", label: "", value: () => null, align: "right", render: ([f, r]) => (r.method.startsWith("element counts") ? null : (
              <label className="btn small">
                <Upload size={14} /> Transcription
                <input type="file" accept=".txt" hidden onChange={(e) => { upload(f, e.target.files?.[0]); e.target.value = ""; }} />
              </label>
            )) },
          ]} />
          <Muted>A scanned PDF or a picture has no structure of its own to count. Type out a few pages by hand, upload the text, and retention is the token similarity between it and what was extracted.</Muted>
          {error && <Notice kind="error">{error}</Notice>}
        </Card>
        <Card title="Element counts" sub="Extracted vs. the raw document XML">
          {detail.length ? (
            <DataTable rows={detail} cols={[
              { key: "file", label: "File", value: (d) => d.file },
              { key: "el", label: "Element", value: (d) => d.el },
              { key: "ref", label: "Reference", value: (d) => d.reference, align: "right" },
              { key: "ext", label: "Extracted", value: (d) => d.extracted, align: "right" },
              { key: "ret", label: "Retained", value: (d) => d.retained, render: (d) => <Meter value={d.retained} label={percent(d.retained)} /> },
            ]} />
          ) : <Muted>No DOCX or PPTX in this run. Scanned PDFs need a hand transcription (CLI: --transcriptions).</Muted>}
        </Card>
      </Row>
    </>
  );
}

function Body() {
  const [step, setStep] = useStep(4);
  const { ev, setEv } = useEvaluation();
  return (
    <>
      <Steps steps={["Gold labels", "Scores", "Errors", "Structure retention"]} active={step} onChange={setStep} />
      {step === 0 && <Gold ev={ev} setEv={setEv} />}
      {step > 0 && !ev && <Muted>Loading…</Muted>}
      {step === 1 && ev && (ev.scores ? <Scores ev={ev.scores} /> : <Notice kind="info">{NEED_GOLD}</Notice>)}
      {step === 2 && ev && (ev.scores ? <Errors ev={ev.scores} /> : <Notice kind="info">{NEED_GOLD}</Notice>)}
      {step === 3 && ev && <Retention ev={ev} setEv={setEv} />}
    </>
  );
}

export default function Evaluation() {
  return (
    <>
      <PageHeader section="Assurance" title="Evaluation" subtitle="Measured, not claimed: recall, precision and leaks against gold labels." />
      <NeedRun><Body /></NeedRun>
    </>
  );
}
