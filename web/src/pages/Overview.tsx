// Analytics · Overview: summary -> files -> pipeline.
import { useMemo, useState } from "react";
import type { Run } from "../api";
import { Donut, HBar, StackedBar } from "../components/charts";
import { Select } from "../components/controls";
import { DataTable } from "../components/table";
import { Card, Chip, Kpis, Muted, NeedRun, Notice, PageHeader, RatingChip, Row, Steps, useStep } from "../components/ui";
import { DECISION_COLOR, DECISION_LABEL, GROUP_COLOR, GROUP_ORDER, RATING_ICON, RATING_ORDER, RATING_STATUS, groupOf, ratingColor } from "../lib/entities";
import { countBy, int, num, pct, sum } from "../lib/format";
import { useDoc, useStore } from "../store";

function Summary({ run }: { run: Run }) {
  const live = run.findings;
  const auto = live.filter((f) => f.decision === "redact").length;
  const exp = run.files.map((f) => f.exposure);
  const worst = [...exp.map((e) => e.rating)].sort((a, b) => RATING_ORDER.indexOf(a) - RATING_ORDER.indexOf(b))[0] ?? "none";
  const groups = countBy(live, (f) => groupOf(f.entity_type));
  const warnings = run.files.flatMap((f) => f.warnings.map((w) => `${f.file}: ${w}`));
  const ratingLegend = Object.fromEntries(RATING_ORDER.filter((r) => exp.some((e) => e.rating === r)).map((r) => [r, ratingColor(r)]));
  return (
    <>
      {Object.entries(run.errors).map(([f, e]) => <Notice kind="error" key={f}>{f}: {e} (fail closed)</Notice>)}
      <Kpis tiles={[
        { label: "Files scanned", value: run.files.length, sub: Object.keys(run.errors).length ? `${Object.keys(run.errors).length} with errors` : "all processed" },
        { label: "PII findings", value: int(live.length), sub: `${int(run.tokens.length)} distinct values` },
        { label: "Auto-redacted", value: int(auto), sub: `${pct(auto, live.length)} of findings`, meter: live.length ? auto / live.length : 0 },
        { label: "Review queue", value: int(live.length - auto), sub: "redacted, awaiting a human" },
        { label: "Exposure score", value: int(sum(exp.map((e) => e.score))), sub: "sensitivity-weighted" },
        { label: "Highest rating", value: `${RATING_ICON[worst]} ${worst.charAt(0).toUpperCase()}${worst.slice(1)}`, status: RATING_STATUS[worst], sub: `${exp.filter((e) => e.rating === worst).length} of ${exp.length} file(s)` },
        { label: "Caught by leak gate", value: run.pipeline.caught, sub: "values detection had not located" },
        { label: "Estimated missed", value: num(sum(exp.map((e) => e.residual.estimated_missed.instances))), sub: "instances, from held-out miss rates" },
      ]} />
      <Row>
        <Card title="Findings by category group" sub="Share of all redacted findings">
          {live.length ? <Donut unit="findings" rows={GROUP_ORDER.map((g) => ({ label: g, value: groups[g] ?? 0, color: GROUP_COLOR[g] }))} /> : <Muted>No PII found.</Muted>}
        </Card>
        <Card title="Exposure by file" sub="Sensitivity-weighted score per 1,000 words, coloured by rating">
          <HBar unit="Exposure per 1,000 words" format={num} legend={ratingLegend}
            rows={[...run.files].sort((a, b) => b.exposure.per_1k_words - a.exposure.per_1k_words).map((f) => ({
              label: f.file, value: f.exposure.per_1k_words, color: ratingColor(f.exposure.rating),
              tipLabel: `${num(f.exposure.per_1k_words)} · ${f.exposure.rating}`, more: [["Rating", f.exposure.rating]],
            }))} />
        </Card>
      </Row>
      <Row>
        <Card title="Routing by file" sub="Findings redacted automatically vs. redacted and queued for review">
          {live.length ? (
            <StackedBar unit="Findings"
              colors={{ [DECISION_LABEL.redact]: DECISION_COLOR.redact, [DECISION_LABEL.review]: DECISION_COLOR.review }}
              rows={run.files.map((f) => ({ label: f.file, parts: { [DECISION_LABEL.redact]: f.redacted, [DECISION_LABEL.review]: f.review_queue } }))} />
          ) : <Muted>No PII found.</Muted>}
        </Card>
        <Card title="Warnings" sub="Raised during extraction and by the leak gate">
          {warnings.length ? warnings.map((w) => <Notice kind="warning" key={w}>{w}</Notice>) : <Muted>None.</Muted>}
        </Card>
      </Row>
    </>
  );
}

function Files({ run }: { run: Run }) {
  const withImages = run.files.filter((f) => f.images.total > 0).map((f) => f.file);
  const [file, setFile] = useState(withImages[0]);
  const { doc } = useDoc(file);
  return (
    <>
      <Card title="Files in this run" sub="What was read from each file and what was found in it">
        <DataTable rows={run.files} cols={[
          { key: "file", label: "File", value: (f) => f.file },
          { key: "type", label: "Type", value: (f) => f.type },
          { key: "pages", label: "Pages / slides", value: (f) => f.pages || null, align: "right" },
          { key: "ocr", label: "OCR pages", value: (f) => f.ocr_pages.length, align: "right" },
          { key: "spans", label: "Text elements", value: (f) => f.spans, align: "right" },
          { key: "images", label: "Images", value: (f) => f.images.total, align: "right" },
          { key: "findings", label: "Findings", value: (f) => f.findings, align: "right" },
          { key: "auto", label: "Auto-redacted", value: (f) => f.redacted, align: "right" },
          { key: "review", label: "Review queue", value: (f) => f.review_queue, align: "right" },
          { key: "dropped", label: "Dropped", value: (f) => f.dropped_candidates, align: "right" },
          { key: "rating", label: "Rating", value: (f) => RATING_ORDER.indexOf(f.exposure.rating), render: (f) => <RatingChip rating={f.exposure.rating} /> },
          { key: "secs", label: "Extract (s)", value: (f) => f.extract_seconds, align: "right" },
        ]} />
      </Card>
      {withImages.length > 0 && (
        <Card title="Embedded images" sub="OCR outcome for every image; unreadable ones are withheld">
          <Select label="File" value={file} onChange={setFile} options={withImages.map((f) => ({ value: f, label: f }))} width="26rem" />
          {doc ? (
            <DataTable rows={doc.images} cols={[
              { key: "location", label: "Location", value: (i) => i.location },
              { key: "status", label: "Status", value: (i) => i.status },
              { key: "conf", label: "OCR confidence", value: (i) => i.ocr_conf, render: (i) => (i.ocr_conf == null ? "–" : i.ocr_conf.toFixed(2)), align: "right" },
              { key: "size", label: "Size", value: (i) => i.width * i.height, render: (i) => `${i.width}×${i.height}`, align: "right" },
            ]} />
          ) : <Muted>Loading…</Muted>}
        </Card>
      )}
    </>
  );
}

function Pipeline({ run }: { run: Run }) {
  const p = run.pipeline;
  const live = run.findings;
  const auto = live.filter((f) => f.decision === "redact").length;
  const timings = useMemo(() => {
    const rows = Object.entries(run.timings).filter(([k]) => k.startsWith("extract:"))
      .map(([k, v]) => ({ label: `Extract · ${k.slice(8)}`, value: v }));
    rows.push({ label: "Detect (all files)", value: run.timings.detect ?? 0 });
    rows.push({ label: "Tokenise, redact, report", value: Math.max((run.timings.total ?? 0) - sum(rows.map((r) => r.value)), 0) });
    return rows;
  }, [run]);
  const layers = Object.entries(countBy(live, (f) => f.layer)).sort((a, b) => b[1] - a[1]);
  const stages: [string, string, string][] = [
    ["Extract", `${run.files.length} files`, `${int(p.spans)} text elements · ${p.ocr_pages} OCR pages · ${p.images} images`],
    ["Detect", `${int(live.length + run.dropped.length)} candidates`, `${int(run.dropped.length)} dropped as false positives`],
    ["Route", `${int(live.length)} findings`, `${int(auto)} auto · ${int(live.length - auto)} review`],
    ["Tokenise", `${int(p.tokens)} tokens`, `${int(p.people)} people, stable across files`],
    ["Leak gate", `${p.caught} caught`, `${p.masked_written} masked written · ${p.masked_withheld} withheld`],
    ["Report", `${p.outputs} outputs`, "register · summary · audit log"],
  ];
  return (
    <>
      <div className="flow">
        {stages.map(([name, value, sub]) => (
          <div className="stage" key={name}>
            <div className="stage-name">{name}</div>
            <div className="stage-value">{value}</div>
            <div className="stage-sub">{sub}</div>
          </div>
        ))}
      </div>
      <Row cols="3fr 2fr">
        <Card title="Where the time went" sub={`Seconds per stage · ${num(run.timings.total ?? 0)} s in total`}>
          <HBar rows={timings} unit="Seconds" format={num} />
        </Card>
        <Card title="Detection components" sub="Loaded for this run">
          <div className="chips">{run.components.map((c) => <Chip key={c} label={c} />)}</div>
          {layers.length > 0 && <HBar unit="Findings" rows={layers.map(([label, value]) => ({ label, value }))} />}
        </Card>
      </Row>
    </>
  );
}

function Body() {
  const run = useStore().run!;
  const [step, setStep] = useStep(3);
  if (!run.files.length) {
    return (
      <>
        {Object.entries(run.errors).map(([f, e]) => <Notice kind="error" key={f}>{f}: {e} (fail closed)</Notice>)}
        <Notice kind="info">No file in this run could be read.</Notice>
      </>
    );
  }
  return (
    <>
      <Steps steps={["Summary", "Files", "Pipeline"]} active={step} onChange={setStep} />
      {step === 0 && <Summary run={run} />}
      {step === 1 && <Files run={run} />}
      {step === 2 && <Pipeline run={run} />}
    </>
  );
}

export default function Overview() {
  return (
    <>
      <PageHeader section="Analytics" title="Overview" subtitle="What the scan found, how it was routed, and how the pipeline ran." />
      <NeedRun><Body /></NeedRun>
    </>
  );
}
