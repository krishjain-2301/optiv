// Analytics · Findings: breakdown -> register -> in context -> dropped candidates.
// One filter row scopes all four steps; filtering happens in the browser.
import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { Finding, Run, SpanFinding } from "../api";
import { Donut, HBar, Histogram, Meter } from "../components/charts";
import { MultiSelect, Select } from "../components/controls";
import { DataTable, type Col } from "../components/table";
import { Card, Kpis, Legend, Muted, NeedRun, Notice, PageHeader, Row, Steps, useStep } from "../components/ui";
import { CONTEXT_ORDER, DECISION_COLOR, DECISION_LABEL, GROUP_COLOR, GROUP_ORDER, NEUTRAL, SERIES, entityColor, groupOf, pretty } from "../lib/entities";
import { countBy, int, median, pct } from "../lib/format";
import { useDoc, useStore } from "../store";

interface Filters {
  files: string[];
  categories: string[];
  decisions: string[];
  sources: string[];
}

const pass = (chosen: string[], value: string) => !chosen.length || chosen.includes(value);

function Breakdown({ view, run }: { view: Finding[]; run: Run }) {
  const auto = view.filter((f) => f.decision === "redact").length;
  const med = median(view.map((f) => f.score));
  if (!view.length) return <Notice kind="info">No findings match the filters.</Notice>;
  const byCat = Object.entries(countBy(view, (f) => f.entity_type)).sort((a, b) => b[1] - a[1]);
  const groups = new Set(byCat.map(([k]) => groupOf(k)));
  const byCtx = countBy(view, (f) => f.context_type);
  const contexts = [...CONTEXT_ORDER.filter((c) => byCtx[c]), ...Object.keys(byCtx).filter((c) => !CONTEXT_ORDER.includes(c))];
  const byLayer = Object.entries(countBy(view, (f) => f.layer)).sort((a, b) => b[1] - a[1]);
  const pivot = byCat.map(([cat]) => ({ cat, counts: countBy(view.filter((f) => f.entity_type === cat), (f) => f.context_type) }));
  return (
    <>
      <Kpis tiles={[
        { label: "Findings", value: int(view.length), sub: "in the current filter" },
        { label: "Distinct values", value: int(new Set(view.map((f) => f.token)).size), sub: "one token each" },
        { label: "Categories", value: byCat.length, sub: "entity types present" },
        { label: "Auto-redacted", value: int(auto), sub: `${pct(auto, view.length)} of findings`, meter: auto / view.length },
        { label: "Review queue", value: int(view.length - auto), sub: "redacted, awaiting a human" },
        { label: "Median score", value: med == null ? "–" : med.toFixed(2), sub: "detector confidence" },
      ]} />
      <Row cols="3fr 2fr">
        <Card title="Findings by category" sub="Coloured by category group">
          <HBar unit="Findings" legend={Object.fromEntries(GROUP_ORDER.filter((g) => groups.has(g)).map((g) => [g, GROUP_COLOR[g]]))}
            rows={byCat.map(([k, v]) => ({ label: pretty(k), value: v, color: entityColor(k), more: [["Group", groupOf(k)]] }))} />
        </Card>
        <div className="stack-v">
          <Card title="Where it was found" sub="Source of the text the finding sits in">
            <Donut unit="findings" rows={contexts.map((c, i) => ({ label: c, value: byCtx[c], color: SERIES[i] ?? NEUTRAL }))} />
          </Card>
          <Card title="Detection layer" sub="Which layer produced the finding">
            <HBar unit="Findings" rows={byLayer.map(([label, value]) => ({ label, value }))} />
          </Card>
        </div>
      </Row>
      <Row cols="3fr 2fr">
        <Card title="Confidence scores" sub="Distribution against the routing thresholds of this run">
          <Histogram xTitle="Confidence score" unit="Findings"
            values={view.map((f) => ({ value: f.score, series: DECISION_LABEL[f.decision] }))}
            colors={{ [DECISION_LABEL.redact]: DECISION_COLOR.redact, [DECISION_LABEL.review]: DECISION_COLOR.review }}
            marks={{ review: run.thresholds.review, "auto-redact": run.thresholds.redact }} />
        </Card>
        <Card title="Category × source" sub="Counts">
          <DataTable rows={pivot} pageSize={25} cols={[
            { key: "cat", label: "Category", value: (r) => pretty(r.cat) },
            ...contexts.map((c): Col<(typeof pivot)[number]> => ({ key: c, label: c, value: (r) => r.counts[c] ?? 0, align: "right" })),
          ]} />
        </Card>
      </Row>
    </>
  );
}

const REGISTER: Col<Finding>[] = [
  { key: "file", label: "File", value: (f) => f.file },
  { key: "page", label: "Page", value: (f) => f.page, align: "right" },
  { key: "location", label: "Location", value: (f) => f.location },
  { key: "source", label: "Source", value: (f) => f.context_type },
  { key: "category", label: "Category", value: (f) => pretty(f.entity_type) },
  { key: "text", label: "Value", value: (f) => f.text },
  { key: "token", label: "Token", value: (f) => f.token },
  { key: "score", label: "Score", value: (f) => f.score, render: (f) => <Meter value={f.score} label={f.score.toFixed(2)} /> },
  { key: "decision", label: "Decision", value: (f) => DECISION_LABEL[f.decision] },
  { key: "layer", label: "Layer", value: (f) => f.layer },
  { key: "reasons", label: "Reasons", value: (f) => f.reasons.join("; "), wrap: true, width: "28rem" },
];

/** A text element with its findings marked. Built from text nodes: document text is never HTML. */
function Highlighted({ text, findings }: { text: string; findings: SpanFinding[] }) {
  const out: ReactNode[] = [];
  let pos = 0;
  findings.forEach((f, i) => {
    if (f.start < pos) return;
    out.push(text.slice(pos, f.start));
    const color = entityColor(f.entity_type);
    out.push(
      <mark key={i} style={{ background: `${color}33`, borderColor: color }} title={`${pretty(f.entity_type)} · score ${f.score.toFixed(2)} · ${f.layer}`}>
        {text.slice(f.start, f.end)}<sub>{pretty(f.entity_type)}</sub>
      </mark>,
    );
    pos = f.end;
  });
  out.push(text.slice(pos));
  return <div className="span-text">{out}</div>;
}

function InContext({ files, flt }: { files: string[]; flt: Filters }) {
  const [file, setFile] = useState(files[0]);
  useEffect(() => {
    if (!files.includes(file)) setFile(files[0]);
  }, [files, file]);
  const { doc, error } = useDoc(file);
  const spans = useMemo(() => (doc?.context ?? [])
    .map((s) => ({ ...s, findings: s.findings.filter((f) => pass(flt.categories, f.entity_type) && pass(flt.decisions, f.decision) && pass(flt.sources, f.context_type)) }))
    .filter((s) => s.findings.length), [doc, flt]);
  const MAX = 150;
  return (
    <>
      <Select label="File" value={file} onChange={setFile} options={files.map((f) => ({ value: f, label: f }))} width="26rem" />
      <Card title="Findings in context" sub="Each text element that holds PII, with its provenance">
        <Legend items={GROUP_COLOR} />
        {error && <Notice kind="error">{error}</Notice>}
        {!doc && !error && <Muted>Loading…</Muted>}
        {doc && !spans.length && <Muted>No findings in this file match the filters.</Muted>}
        {spans.slice(0, MAX).map((s) => (
          <div className="span" key={s.id}>
            <small>{[s.location, s.kind, s.source, s.ocr_conf ? `OCR conf ${s.ocr_conf.toFixed(2)}` : ""].filter(Boolean).join(" · ")}</small>
            <Highlighted text={s.text} findings={s.findings} />
          </div>
        ))}
        {spans.length > MAX && <Muted>First {MAX} of {int(spans.length)} text elements shown.</Muted>}
      </Card>
    </>
  );
}

function Dropped({ view }: { view: Finding[] }) {
  const byCat = Object.entries(countBy(view, (f) => f.entity_type)).sort((a, b) => b[1] - a[1]);
  return (
    <>
      <Kpis tiles={[
        { label: "Candidates dropped", value: int(view.length), sub: "not redacted, kept in the audit log" },
        { label: "Categories affected", value: byCat.length, sub: "entity types" },
      ]} />
      {!view.length ? <Notice kind="info">No dropped candidates match the filters.</Notice> : (
        <Row cols="2fr 3fr">
          <Card title="Dropped by category" sub="False-positive controls at work">
            <HBar unit="Candidates dropped" rows={byCat.map(([k, v]) => ({ label: pretty(k), value: v }))} />
          </Card>
          <Card title="Dropped candidates" sub="Each with the reason it was dropped">
            <DataTable rows={view} pageSize={25} cols={REGISTER.filter((c) => ["file", "location", "category", "text", "score", "layer", "reasons"].includes(c.key))} />
          </Card>
        </Row>
      )}
    </>
  );
}

function Body() {
  const run = useStore().run!;
  const [step, setStep] = useStep(4);
  const [flt, setFlt] = useState<Filters>({ files: [], categories: [], decisions: [], sources: [] });
  const all = run.files.map((f) => f.file);
  const scope = (rows: Finding[], decisions: boolean) => rows.filter((f) =>
    pass(flt.files, f.file) && pass(flt.categories, f.entity_type) && pass(flt.sources, f.context_type) && (!decisions || pass(flt.decisions, f.decision)));
  const view = useMemo(() => scope(run.findings, true), [run, flt]); // eslint-disable-line react-hooks/exhaustive-deps
  const dropped = useMemo(() => scope(run.dropped, false), [run, flt]); // eslint-disable-line react-hooks/exhaustive-deps
  const options = (values: string[], label: (v: string) => string = (v) => v) => [...new Set(values)].sort().map((v) => ({ value: v, label: label(v) }));
  return (
    <>
      <div className="filters">
        <MultiSelect label="File" all="All files" selected={flt.files} onChange={(files) => setFlt({ ...flt, files })} options={options(all)} />
        <MultiSelect label="Category" all="All categories" selected={flt.categories} onChange={(categories) => setFlt({ ...flt, categories })}
          options={options([...run.findings, ...run.dropped].map((f) => f.entity_type), pretty)} />
        <MultiSelect label="Decision" all="All decisions" selected={flt.decisions} onChange={(decisions) => setFlt({ ...flt, decisions })}
          options={["redact", "review"].map((v) => ({ value: v, label: DECISION_LABEL[v] }))} />
        <MultiSelect label="Source" all="All sources" selected={flt.sources} onChange={(sources) => setFlt({ ...flt, sources })}
          options={options(run.findings.map((f) => f.context_type))} />
      </div>
      <Steps steps={["Breakdown", "Register", "In context", "Dropped candidates"]} active={step} onChange={setStep} />
      {step === 0 && <Breakdown view={view} run={run} />}
      {step === 1 && (
        <Card title="PII exposure register" sub={`${int(view.length)} finding(s) with their source, token, score and reasons`}>
          <DataTable rows={view} cols={REGISTER} empty="No findings match the filters." />
          <Muted>This table shows original values. The shareable register on the Reports page masks them.</Muted>
        </Card>
      )}
      {step === 2 && (all.length ? <InContext files={flt.files.length ? flt.files : all} flt={flt} /> : <Muted>No files.</Muted>)}
      {step === 3 && <Dropped view={dropped} />}
    </>
  );
}

export default function Findings() {
  return (
    <>
      <PageHeader section="Analytics" title="Findings" subtitle="Every identified and classified value, traceable to its source." />
      <NeedRun><Body /></NeedRun>
    </>
  );
}
