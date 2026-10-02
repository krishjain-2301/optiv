// Documents · Redaction: LLM text -> token map -> leak gate.
import { useState } from "react";
import type { Run } from "../api";
import { HBar } from "../components/charts";
import { Select } from "../components/controls";
import { DataTable } from "../components/table";
import { Card, Kpis, Muted, NeedRun, Notice, PageHeader, Row, Steps, useStep } from "../components/ui";
import { GROUP_COLOR, GROUP_ORDER, entityColor, groupOf, pretty } from "../lib/entities";
import { countBy, int, sum } from "../lib/format";
import { useDoc, useStore } from "../store";

function LlmText({ run }: { run: Run }) {
  const files = run.files.map((f) => f.file);
  const [file, setFile] = useState(files[0]);
  const { doc, error } = useDoc(file);
  return (
    <>
      <Select label="File" value={file} onChange={setFile} options={files.map((f) => ({ value: f, label: f }))} width="26rem" />
      {error && <Notice kind="error">{error}</Notice>}
      <Row>
        <Card title="Extracted" sub="Contains original values"><pre className="text-pane">{doc?.markdown ?? "Loading…"}</pre></Card>
        <Card title="Redacted" sub="What the LLM receives"><pre className="text-pane">{doc?.redacted ?? "Loading…"}</pre></Card>
      </Row>
      <Muted>Review-band findings are redacted too (fail closed). Tokens are stable across files.</Muted>
    </>
  );
}

function Tokens({ run }: { run: Run }) {
  const tok = run.tokens;
  if (!tok.length) return <Notice kind="info">No PII found, so no tokens were assigned.</Notice>;
  const people = tok.filter((t) => t.entity_type === "PERSON").length;
  const linked = tok.filter((t) => t.entity_type !== "PERSON" && !/_U\d+\]$/.test(t.token)).length;
  const byCat = Object.entries(countBy(tok, (t) => t.entity_type)).sort((a, b) => b[1] - a[1]);
  const groups = new Set(byCat.map(([k]) => groupOf(k)));
  return (
    <>
      <Kpis tiles={[
        { label: "Distinct tokens", value: int(tok.length), sub: `for ${int(run.findings.length)} findings` },
        { label: "People", value: people, sub: "numbered in order of first appearance" },
        { label: "Linked to a person", value: linked, sub: "e-mail, phone or ID sharing the person's number" },
        { label: "Seen in several files", value: tok.filter((t) => t.files > 1).length, sub: "the same token in each" },
      ]} />
      <Row cols="2fr 3fr">
        <Card title="Distinct values by category" sub="One token per value">
          <HBar unit="Tokens" legend={Object.fromEntries(GROUP_ORDER.filter((g) => groups.has(g)).map((g) => [g, GROUP_COLOR[g]]))}
            rows={byCat.map(([k, v]) => ({ label: pretty(k), value: v, color: entityColor(k) }))} />
        </Card>
        <Card title="Token map" sub="Tokens only: original values stay in the encrypted vault">
          <DataTable rows={[...tok].sort((a, b) => b.occurrences - a.occurrences)} cols={[
            { key: "token", label: "Token", value: (t) => t.token },
            { key: "cat", label: "Category", value: (t) => pretty(t.entity_type) },
            { key: "occ", label: "Occurrences", value: (t) => t.occurrences, align: "right" },
            { key: "files", label: "Files", value: (t) => t.files, align: "right" },
          ]} />
        </Card>
      </Row>
    </>
  );
}

function LeakGate({ run }: { run: Run }) {
  const known = run.files.map((f) => ({ file: f.file, ...f.exposure.residual.known }));
  const blocked = known.filter((k) => k.masked_copy === "withheld" || k.masked_copy === "failed").length;
  return (
    <>
      <Kpis tiles={[
        { label: "Masked copies written", value: known.filter((k) => k.masked_copy === "written").length, status: "good", sub: "passed the leak gate" },
        { label: "Withheld or failed", value: blocked, status: blocked ? "critical" : "good", sub: "not written (fail closed)" },
        { label: "Scrubbed from LLM text", value: sum(known.map((k) => k.llm_text_values_caught_by_gate)), sub: "mentions detection had not located" },
        { label: "Scrubbed from masked files", value: sum(known.map((k) => k.masked_values_caught_by_gate)), sub: "before the masked copy was checked" },
      ]} />
      <Card title="Leak gate by file" sub="No vault value may survive in any output, or the file is not written">
        <DataTable rows={known} cols={[
          { key: "file", label: "File", value: (k) => k.file },
          { key: "masked", label: "Masked copy", value: (k) => k.masked_copy },
          { key: "llm", label: "Scrubbed from LLM text", value: (k) => k.llm_text_values_caught_by_gate, align: "right" },
          { key: "maskedc", label: "Scrubbed from masked file", value: (k) => k.masked_values_caught_by_gate, align: "right" },
          { key: "left", label: "Values left in outputs", value: (k) => k.values_left_in_outputs, align: "right" },
        ]} />
      </Card>
      {Object.entries(run.errors).map(([f, e]) => <Notice kind="error" key={f}>{f}: {e}</Notice>)}
    </>
  );
}

function Body() {
  const run = useStore().run!;
  const [step, setStep] = useStep(3);
  if (!run.files.length) return <Notice kind="info">No file in this run could be read.</Notice>;
  return (
    <>
      <Steps steps={["LLM text", "Token map", "Leak gate"]} active={step} onChange={setStep} />
      {step === 0 && <LlmText run={run} />}
      {step === 1 && <Tokens run={run} />}
      {step === 2 && <LeakGate run={run} />}
    </>
  );
}

export default function Redaction() {
  return (
    <>
      <PageHeader section="Documents" title="Redaction" subtitle="The text that is safe to hand to an LLM, the tokens behind it, and the gate that checks it." />
      <NeedRun><Body /></NeedRun>
    </>
  );
}
