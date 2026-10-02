// Documents · Extraction: preview -> structure -> span map -> images.
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useState } from "react";
import { api, type DocDetail } from "../api";
import { Donut, HBar } from "../components/charts";
import { Markdown, Select } from "../components/controls";
import { DataTable } from "../components/table";
import { tip } from "../components/tooltip";
import { Card, Kpis, Legend, Muted, NeedRun, Notice, PageHeader, Row, Steps, useStep } from "../components/ui";
import { DECISION_LABEL, GROUP_COLOR, NEUTRAL, SERIES, entityColor, pretty } from "../lib/entities";
import { int } from "../lib/format";
import { useDoc, useStore } from "../store";

const SOURCE_COLOR: Record<string, string> = { native: SERIES[0], ocr: SERIES[1], image_ocr: SERIES[2] };
const label = (key: string) => key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

function Preview({ doc, runId }: { doc: DocDetail; runId: string }) {
  const [page, setPage] = useState(1);
  useEffect(() => setPage(1), [doc.file]);
  if (!doc.previewable) {
    return <Card title="Extracted text" sub="Structure preserved as Markdown"><Markdown text={doc.markdown} /></Card>;
  }
  const parts = doc.markdown.split(`<!-- page ${page} -->`);
  const pageText = parts.length > 1 ? parts[1].split("<!-- page")[0] : doc.markdown;
  const boxes = doc.boxes[String(page)] ?? [];
  const go = (p: number) => setPage(Math.max(1, Math.min(doc.pages, p)));
  return (
    <>
      <div className="inline">
        <button className="btn icon" disabled={page <= 1} onClick={() => go(page - 1)} aria-label="Previous page"><ChevronLeft size={16} /></button>
        <label className="pageno">Page <input className="input" type="number" min={1} max={doc.pages} value={page} onChange={(e) => go(Number(e.target.value))} /> of {doc.pages}</label>
        <button className="btn icon" disabled={page >= doc.pages} onClick={() => go(page + 1)} aria-label="Next page"><ChevronRight size={16} /></button>
        <span className="muted">{boxes.length} finding box(es) on this page{doc.ocr_pages.includes(page) ? " · read by OCR" : ""}</span>
      </div>
      <Row>
        <Card title={`Page ${page}`} sub="Boxes mark detected PII; hover one for its category and score">
          <Legend items={GROUP_COLOR} />
          <div className="page-view">
            <img src={api.pageUrl(doc.file, page, runId)} alt={`${doc.file}, page ${page}`} />
            {boxes.map((b, i) => (
              <span key={i} className="page-box" style={{ left: `${b.x * 100}%`, top: `${b.y * 100}%`, width: `${b.w * 100}%`, height: `${b.h * 100}%`, outlineColor: entityColor(b.entity_type) }}
                {...tip(pretty(b.entity_type), [["Score", b.score.toFixed(2)], ["Decision", DECISION_LABEL[b.decision]]])} />
            ))}
          </div>
        </Card>
        <Card title="Extracted text" sub="Structure preserved as Markdown"><Markdown text={pageText} /></Card>
      </Row>
    </>
  );
}

function Structure({ doc }: { doc: DocDetail }) {
  const counts = Object.entries(doc.structure).filter(([, v]) => typeof v === "number") as [string, number][];
  return (
    <>
      <Kpis tiles={[
        { label: "Text elements", value: int(doc.spans.length), sub: "with provenance" },
        { label: "Pages / slides", value: doc.pages || "–", sub: `${doc.ocr_pages.length} read by OCR` },
        ...counts.filter(([k]) => k !== "pages").map(([k, v]) => ({ label: label(k), value: int(v) })),
      ]} />
      {!doc.spans.length ? <Notice kind="info">No text was extracted from this file.</Notice> : (
        <Row cols="3fr 2fr">
          <Card title="Text elements by kind" sub="What the document is made of">
            <HBar unit="Elements" rows={Object.entries(doc.kinds).map(([k, v]) => ({ label: k, value: v }))} />
          </Card>
          <Card title="How the text was read" sub="Native text layer vs. OCR">
            <Donut unit="elements" rows={Object.entries(doc.sources).map(([k, v]) => ({ label: k, value: v, color: SOURCE_COLOR[k] ?? NEUTRAL }))} />
          </Card>
        </Row>
      )}
    </>
  );
}

function Images({ doc }: { doc: DocDetail }) {
  if (!doc.images.length) return <Notice kind="info">This file has no embedded images.</Notice>;
  const status: Record<string, number> = {};
  for (const i of doc.images) status[i.status] = (status[i.status] ?? 0) + 1;
  return (
    <>
      <Kpis tiles={[
        { label: "Images", value: doc.images.length, sub: "embedded or scanned regions" },
        ...Object.entries(status).map(([k, v]) => ({
          label: label(k), value: v, status: k === "read" ? "good" as const : k === "skipped" ? "warning" as const : "serious" as const,
          sub: k === "read" ? "text used" : k === "skipped" ? "too small to hold text" : "withheld from the LLM text",
        })),
      ]} />
      <Card title="Images" sub="OCR outcome per image">
        <DataTable rows={doc.images} cols={[
          { key: "location", label: "Location", value: (i) => i.location },
          { key: "page", label: "Page", value: (i) => i.page, align: "right" },
          { key: "status", label: "Status", value: (i) => i.status },
          { key: "conf", label: "OCR confidence", value: (i) => i.ocr_conf, render: (i) => (i.ocr_conf == null ? "–" : i.ocr_conf.toFixed(2)), align: "right" },
          { key: "size", label: "Size", value: (i) => i.width * i.height, render: (i) => `${i.width}×${i.height}`, align: "right" },
        ]} />
      </Card>
    </>
  );
}

function Body() {
  const run = useStore().run!;
  const files = run.files.map((f) => f.file);
  const [file, setFile] = useState(files[0]);
  const [step, setStep] = useStep(4);
  const { doc, error } = useDoc(file);
  if (!files.length) return <Notice kind="info">No file in this run could be read.</Notice>;
  return (
    <>
      <Select label="File" value={file} onChange={setFile} options={files.map((f) => ({ value: f, label: f }))} width="26rem" />
      <Steps steps={["Preview", "Structure", "Span map", "Images"]} active={step} onChange={setStep} />
      {error && <Notice kind="error">{error}</Notice>}
      {!doc ? !error && <Muted>Loading…</Muted> : (
        <>
          {step === 0 && <Preview doc={doc} runId={run.run_id} />}
          {step === 1 && <Structure doc={doc} />}
          {step === 2 && (
            <Card title="Span map" sub="Provenance of every text element: page, location, kind, source">
              <DataTable rows={doc.spans} cols={[
                { key: "id", label: "ID", value: (s) => s.id },
                { key: "page", label: "Page", value: (s) => s.page, align: "right" },
                { key: "location", label: "Location", value: (s) => s.location },
                { key: "kind", label: "Kind", value: (s) => s.kind },
                { key: "source", label: "Source", value: (s) => s.source },
                { key: "conf", label: "OCR conf.", value: (s) => s.ocr_conf, render: (s) => (s.ocr_conf == null ? "–" : s.ocr_conf.toFixed(2)), align: "right" },
                { key: "header", label: "Header / label", value: (s) => s.header },
                { key: "text", label: "Text", value: (s) => s.text, wrap: true, width: "32rem" },
              ]} />
            </Card>
          )}
          {step === 3 && <Images doc={doc} />}
        </>
      )}
    </>
  );
}

export default function Extraction() {
  return (
    <>
      <PageHeader section="Documents" title="Extraction" subtitle="What was read out of each file, and where every piece of text came from." />
      <NeedRun><Body /></NeedRun>
    </>
  );
}
