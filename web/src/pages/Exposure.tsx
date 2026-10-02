// Analytics · Exposure & risk: ranking -> page heatmap -> residual risk.
import type { Run } from "../api";
import { HBar, Heatmap, Meter } from "../components/charts";
import { DataTable } from "../components/table";
import { Card, Kpis, Muted, NeedRun, Notice, PageHeader, RatingChip, Row, Steps, useStep } from "../components/ui";
import { GROUP_COLOR, GROUP_ORDER, RATING_ORDER, groupOf, pretty } from "../lib/entities";
import { int, num, pageOrder, sum } from "../lib/format";
import { useStore } from "../store";

function merge(maps: Record<string, number>[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const m of maps) for (const [k, v] of Object.entries(m)) out[k] = (out[k] ?? 0) + v;
  return out;
}

function Ranking({ run }: { run: Run }) {
  const ranked = [...run.files].sort((a, b) => b.exposure.per_1k_words - a.exposure.per_1k_words);
  const top = ranked[0];
  const critical = run.files.filter((f) => f.exposure.rating === "critical").length;
  const byCat = Object.entries(merge(run.files.map((f) => f.exposure.by_category))).sort((a, b) => b[1] - a[1]);
  const groups = new Set(byCat.map(([k]) => groupOf(k)));
  return (
    <>
      <Kpis tiles={[
        { label: "Total exposure score", value: int(sum(run.files.map((f) => f.exposure.score))), sub: `${int(sum(run.files.map((f) => f.exposure.distinct_values_score)))} counting each value once` },
        { label: "Highest density", value: num(top.exposure.per_1k_words), sub: `per 1,000 words · ${top.file}` },
        { label: "Critical files", value: critical, status: critical ? "critical" : "good", sub: "hold a government / financial ID or a credential" },
        { label: "Words scanned", value: int(sum(run.files.map((f) => f.exposure.words))), sub: `in ${run.files.length} file(s)` },
      ]} />
      <Row cols="3fr 2fr">
        <Card title="Review order" sub="Files by exposure density: the riskiest artifact first">
          <DataTable rows={ranked} cols={[
            { key: "file", label: "File", value: (f) => f.file },
            { key: "rating", label: "Rating", value: (f) => RATING_ORDER.indexOf(f.exposure.rating), render: (f) => <RatingChip rating={f.exposure.rating} /> },
            { key: "density", label: "Per 1k words", value: (f) => f.exposure.per_1k_words, render: (f) => <Meter value={f.exposure.per_1k_words} max={top.exposure.per_1k_words} label={num(f.exposure.per_1k_words)} /> },
            { key: "score", label: "Score", value: (f) => f.exposure.score, align: "right" },
            { key: "distinct", label: "Distinct values", value: (f) => f.exposure.distinct_values_score, align: "right" },
            { key: "words", label: "Words", value: (f) => f.exposure.words, align: "right" },
          ]} />
          <Muted>Rating: critical if any instance weighs 9 or more (government or financial ID, credential); otherwise high / medium / low by density.</Muted>
        </Card>
        <Card title="Exposure by category" sub="Sensitivity weight × instances, all files">
          {byCat.length ? (
            <HBar unit="Exposure score" legend={Object.fromEntries(GROUP_ORDER.filter((g) => groups.has(g)).map((g) => [g, GROUP_COLOR[g]]))}
              rows={byCat.map(([k, v]) => ({ label: pretty(k), value: v, color: GROUP_COLOR[groupOf(k)], more: [["Group", groupOf(k)]] }))} />
          ) : <Muted>No PII found.</Muted>}
        </Card>
      </Row>
    </>
  );
}

function PageHeat({ run }: { run: Run }) {
  const cells = run.files.flatMap((f) => Object.entries(f.exposure.by_page).map(([page, exposure]) => ({ file: f.file, page, exposure })));
  if (!cells.length) return <Muted>No PII found.</Muted>;
  const pages = [...new Set(cells.map((c) => c.page))].sort(pageOrder);
  const lookup = new Map(cells.map((c) => [`${c.file}\n${c.page}`, c.exposure]));
  const hottest = [...cells].sort((a, b) => b.exposure - a.exposure);
  return (
    <>
      <Card title="Exposure by page / slide" sub="Brighter = more sensitive PII on that page. “document” holds formats without pages, and properties.">
        <Heatmap rows={run.files.map((f) => f.file)} cols={pages} colTitle="Page / slide" unit="Exposure"
          value={(file, page) => lookup.get(`${file}\n${page}`)} />
      </Card>
      <Card title="Hottest pages" sub="Where a reviewer should look first">
        <DataTable rows={hottest} pageSize={10} cols={[
          { key: "file", label: "File", value: (c) => c.file },
          { key: "page", label: "Page / slide", value: (c) => c.page },
          { key: "exposure", label: "Exposure", value: (c) => c.exposure, render: (c) => <Meter value={c.exposure} max={hottest[0].exposure} label={int(c.exposure)} /> },
        ]} />
      </Card>
    </>
  );
}

function Residual({ run }: { run: Run }) {
  const res = run.files.map((f) => ({ file: f.file, ...f.exposure.residual }));
  const withheld = res.filter((r) => r.known.masked_copy === "withheld").length;
  const caught = (r: (typeof res)[number]) => r.known.llm_text_values_caught_by_gate + r.known.masked_values_caught_by_gate;
  const missed = Object.entries(merge(res.map((r) => r.estimated_missed.by_category))).sort((a, b) => b[1] - a[1]);
  return (
    <>
      <Kpis tiles={[
        { label: "Known residual", value: withheld ? `${withheld} withheld` : "0", status: withheld ? "critical" : "good", sub: "original values left in outputs (the leak gate enforces it)" },
        { label: "Caught by leak gate", value: sum(res.map(caught)), sub: "values scrubbed after detection" },
        { label: "Unreadable images", value: sum(res.map((r) => r.unreadable.images_withheld)), sub: "withheld; content unknown" },
        { label: "OCR words masked", value: sum(res.map((r) => r.unreadable.ocr_words_masked)), sub: "below the confidence floor" },
        { label: "Estimated missed", value: num(sum(res.map((r) => r.estimated_missed.instances))), sub: "instances; an estimate" },
      ]} />
      <Row cols="3fr 2fr">
        <Card title="Residual risk by file" sub="Kept in three parts because they are known to different degrees">
          <DataTable rows={res} cols={[
            { key: "file", label: "File", value: (r) => r.file },
            { key: "masked", label: "Masked copy", value: (r) => r.known.masked_copy },
            { key: "gate", label: "Gate catches", value: caught, align: "right" },
            { key: "images", label: "Images withheld", value: (r) => r.unreadable.images_withheld, align: "right" },
            { key: "ocr", label: "OCR masked", value: (r) => r.unreadable.ocr_words_masked, align: "right" },
            { key: "missed", label: "Est. missed", value: (r) => r.estimated_missed.instances, align: "right" },
            { key: "per1k", label: "Est. / 1k words", value: (r) => r.estimated_missed.per_1k_words, align: "right" },
          ]} />
          <Muted>Estimated missed = found instances × miss rate measured on the held-out set ({res[0].estimated_missed.basis}).</Muted>
        </Card>
        <Card title="Estimated missed, by category" sub="Instances the detectors probably did not find">
          {missed.length ? <HBar unit="Estimated instances" format={(n) => num(n, 2)} rows={missed.map(([k, v]) => ({ label: pretty(k), value: v }))} /> : <Muted>Nothing estimated.</Muted>}
        </Card>
      </Row>
    </>
  );
}

function Body() {
  const run = useStore().run!;
  const [step, setStep] = useStep(3);
  if (!run.files.length) return <Notice kind="info">No file in this run could be read.</Notice>;
  return (
    <>
      <Steps steps={["Ranking", "Page heatmap", "Residual risk"]} active={step} onChange={setStep} />
      {step === 0 && <Ranking run={run} />}
      {step === 1 && <PageHeat run={run} />}
      {step === 2 && <Residual run={run} />}
    </>
  );
}

export default function Exposure() {
  return (
    <>
      <PageHeader section="Analytics" title="Exposure & risk" subtitle="How much personal data each file carries, where it sits, and what risk is left after redaction." />
      <NeedRun><Body /></NeedRun>
    </>
  );
}
