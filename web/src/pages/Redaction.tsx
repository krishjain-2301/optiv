// Documents · Redaction: LLM text -> token map -> leak gate -> rehydrate.
import { KeyRound } from "lucide-react";
import { useState } from "react";
import { api, type Rehydrated, type Run } from "../api";
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
  const linked = tok.filter((t) => t.linked).length;
  const byCat = Object.entries(countBy(tok, (t) => t.entity_type)).sort((a, b) => b[1] - a[1]);
  const groups = new Set(byCat.map(([k]) => groupOf(k)));
  return (
    <>
      <Kpis tiles={[
        { label: "Distinct tokens", value: int(tok.length), sub: `for ${int(run.findings.length)} findings` },
        { label: "People", value: people, sub: "numbered in order of first appearance" },
        { label: "Linked to a person", value: linked, sub: "e-mail, phone or ID sharing the person's number" },
        { label: "Seen in several files", value: tok.filter((t) => t.files > 1).length, sub: "the same token in each" },
        { label: "Token scheme", value: run.keyed_tokens ? "Keyed" : "Numbered", sub: run.keyed_tokens ? "HMAC-SHA256: stable across runs" : "per run; set a token key to keep them" },
        { label: "Profile", value: run.profile.name, sub: run.profile.label },
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
  const known = run.files.map((f) => ({ file: f.file, verification: f.verification, ...f.exposure.residual.known }));
  const blocked = known.filter((k) => k.masked_copy === "withheld" || k.masked_copy === "failed").length;
  return (
    <>
      <Kpis tiles={[
        { label: "Masked copies written", value: known.filter((k) => k.masked_copy === "written").length, status: "good", sub: "passed the leak gate" },
        { label: "Withheld or failed", value: blocked, status: blocked ? "critical" : "good", sub: "not written (fail closed)" },
        { label: "Scrubbed from LLM text", value: sum(known.map((k) => k.llm_text_values_caught_by_gate)), sub: "mentions detection had not located" },
        { label: "Scrubbed from masked files", value: sum(known.map((k) => k.masked_values_caught_by_gate)), sub: "before the masked copy was checked" },
        { label: "Re-read by OCR", value: run.pipeline.verify_on ? int(run.pipeline.verified_pages + run.pipeline.verified_pictures) : "Off",
          status: run.pipeline.verify_on ? "good" : "serious", sub: run.pipeline.verify_on ? `${run.pipeline.verified_pages} pages · ${run.pipeline.verified_pictures} pictures` : "masks on pixels were not checked" },
        { label: "Covered by verification", value: run.pipeline.verify_covered, status: run.pipeline.verify_covered ? "warning" : "good", sub: "values still readable after masking" },
        { label: "Faces / QR codes", value: `${run.pipeline.faces} / ${run.pipeline.qr_codes}`, sub: "blanked in the masked copies" },
      ]} />
      <Card title="Leak gate by file" sub="No vault value may survive in any output, or the file is not written">
        <DataTable rows={known} cols={[
          { key: "file", label: "File", value: (k) => k.file },
          { key: "masked", label: "Masked copy", value: (k) => k.masked_copy },
          { key: "llm", label: "Scrubbed from LLM text", value: (k) => k.llm_text_values_caught_by_gate, align: "right" },
          { key: "maskedc", label: "Scrubbed from masked file", value: (k) => k.masked_values_caught_by_gate, align: "right" },
          { key: "left", label: "Values left in outputs", value: (k) => k.values_left_in_outputs, align: "right" },
          { key: "verified", label: "Verified", value: (k) => verified(k.verification) },
          { key: "covered", label: "Covered on re-read", value: (k) => k.verification.covered ?? 0, align: "right" },
        ]} />
        <Muted>The gate searches text: XML parts, the text layer, metadata. Verification then OCRs every masked page and picture again and searches that, because the gate cannot see pixels. A value still readable is covered; if it cannot be, the file is withheld.</Muted>
      </Card>
      {Object.entries(run.errors).map(([f, e]) => <Notice kind="error" key={f}>{f}: {e}</Notice>)}
    </>
  );
}

function verified(v: Run["files"][number]["verification"]): string {
  if (v.method === "re-OCR") return [v.pages ? `${v.pages} page(s)` : "", v.pictures ? `${v.pictures} picture(s)` : ""].filter(Boolean).join(", ") || "no pictures to read";
  return v.method === "text search" ? "text only (no pictures)" : v.method ?? "not run";
}

function Rehydrate({ run }: { run: Run }) {
  const { settings, meta } = useStore();
  const [text, setText] = useState("");
  const [purpose, setPurpose] = useState("");
  const [out, setOut] = useState<Rehydrated | null>(null);
  const [error, setError] = useState<string | null>(null);
  const go = async () => {
    setError(null);
    try {
      setOut(await api.rehydrate(text, purpose, settings.operator));
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <>
      <Notice kind="warning">Re-identification turns safe text back into personal data. Each use is appended to the audit log with the operator, the tokens restored and the purpose.</Notice>
      <Row>
        <Card title="Text with tokens" sub="For example the answer an LLM gave about the redacted text">
          <textarea className="input" rows={9} value={text} onChange={(e) => setText(e.target.value)}
            placeholder={`[PERSON_001] approved the exception and asked [PERSON_004] to confirm by e-mail.`} />
          <label className="field">
            <span className="field-label">Purpose</span>
            <input className="input" value={purpose} onChange={(e) => setPurpose(e.target.value)} placeholder="why the original values are needed" />
          </label>
          <div className="inline">
            <button className="btn primary" onClick={go} disabled={!text.trim() || !purpose.trim()}><KeyRound size={16} /> Restore original values</button>
            <span className="muted">as {settings.operator || meta.default_operator || "the server's user"} · run {run.run_id}</span>
          </div>
          {error && <Notice kind="error">{error}</Notice>}
        </Card>
        <Card title="With original values" sub="Shown here only; not written to any file">
          <pre className="text-pane">{out ? out.text : "Nothing restored yet."}</pre>
          {out && <Muted>{out.restored.length} token(s) restored.{out.unknown.length ? ` Left as they are: ${out.unknown.join(", ")} (not in this run's vault, or one token stands for several values by design).` : ""}</Muted>}
        </Card>
      </Row>
    </>
  );
}

function Body() {
  const run = useStore().run!;
  const [step, setStep] = useStep(4);
  if (!run.files.length) return <Notice kind="info">No file in this run could be read.</Notice>;
  return (
    <>
      <Steps steps={["LLM text", "Token map", "Leak gate", "Rehydrate"]} active={step} onChange={setStep} />
      {step === 0 && <LlmText run={run} />}
      {step === 1 && <Tokens run={run} />}
      {step === 2 && <LeakGate run={run} />}
      {step === 3 && <Rehydrate run={run} />}
    </>
  );
}

export default function Redaction() {
  return (
    <>
      <PageHeader section="Documents" title="Redaction" subtitle="The text that is safe to hand to an LLM, the tokens behind it, the gate that checks it, and the way back." />
      <NeedRun><Body /></NeedRun>
    </>
  );
}
