// Assurance · Review: review queue -> add missed -> apply.
// A reviewer confirms or rejects values (everywhere they appear) and adds what the detectors
// missed; applying writes every output again on the server.
import { Check, Download, Plus, RotateCcw, ShieldCheck, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { QueueRow, ReviewAddition, ReviewDecision, Run } from "../api";
import { Meter } from "../components/charts";
import { Select, Toggle } from "../components/controls";
import { DataTable } from "../components/table";
import { Card, Kpis, Muted, NeedRun, Notice, PageHeader, Row, Steps, useStep } from "../components/ui";
import { pretty } from "../lib/entities";
import { int } from "../lib/format";
import { isActive, useStore } from "../store";
import { Progress } from "./Scan";

type Action = ReviewDecision["action"];
const keyOf = (r: { entity_type: string; value: string }) => `${r.entity_type}\n${r.value.toLowerCase()}`;

/** Values redacted automatically that nobody reviewed, in the shape of the review queue. */
function autoRedacted(run: Run): QueueRow[] {
  const rows = new Map<string, QueueRow>();
  for (const f of run.findings) {
    if (f.decision !== "redact" || f.review) continue;
    const k = keyOf({ entity_type: f.entity_type, value: f.text });
    const r = rows.get(k) ?? {
      entity_type: f.entity_type, value: f.text, occurrences: 0, files: [], score: f.score, layer: f.layer,
      location: `${f.file}: ${f.location}`, reasons: f.reasons,
    };
    r.occurrences += 1;
    if (!r.files.includes(f.file)) r.files.push(f.file);
    rows.set(k, r);
  }
  return [...rows.values()].sort((a, b) => b.occurrences - a.occurrences);
}

function Queue({ run, picked, pick }: { run: Run; picked: Record<string, Action>; pick: (row: QueueRow, a: Action | null) => void }) {
  const [all, setAll] = useState(false);
  const rows = useMemo(() => (all ? [...run.review_queue, ...autoRedacted(run)] : run.review_queue), [run, all]);
  const decided = Object.keys(picked).length;
  return (
    <>
      <Kpis tiles={[
        { label: "Values awaiting review", value: int(run.review_queue.length), status: run.review_queue.length ? "warning" : "good", sub: "redacted already (fail closed)" },
        { label: "Occurrences", value: int(run.review_queue.reduce((a, r) => a + r.occurrences, 0)), sub: "places those values appear" },
        { label: "Decided, not yet applied", value: decided, sub: "apply them in the last step" },
        { label: "Reviews applied", value: run.reviews.length, sub: run.reviews.length ? `last by ${run.reviews[run.reviews.length - 1].operator}` : "none in this run" },
      ]} />
      <Card title="Review queue" sub="A decision holds for every place the value appears">
        <Toggle label="Also list values that were redacted automatically (to reject false positives)" checked={all} onChange={setAll} />
        <DataTable rows={rows} pageSize={25} empty="Nothing is waiting for review." cols={[
          { key: "value", label: "Value", value: (r) => r.value },
          { key: "cat", label: "Category", value: (r) => pretty(r.entity_type) },
          { key: "occ", label: "Places", value: (r) => r.occurrences, align: "right" },
          { key: "files", label: "Files", value: (r) => r.files.length, align: "right" },
          { key: "score", label: "Score", value: (r) => r.score, render: (r) => <Meter value={r.score} label={r.score.toFixed(2)} /> },
          { key: "where", label: "First seen", value: (r) => r.location },
          { key: "why", label: "Why it was flagged", value: (r) => r.reasons.join("; "), wrap: true, width: "22rem" },
          {
            key: "decision", label: "Decision", value: (r) => picked[keyOf(r)] ?? "", render: (r) => {
              const now = picked[keyOf(r)];
              return (
                <span className="decide">
                  <button className={now === "approve" ? "btn small on good" : "btn small"} aria-pressed={now === "approve"}
                    onClick={() => pick(r, now === "approve" ? null : "approve")}><Check size={14} /> Is PII</button>
                  <button className={now === "reject" ? "btn small on bad" : "btn small"} aria-pressed={now === "reject"}
                    onClick={() => pick(r, now === "reject" ? null : "reject")}><X size={14} /> Not PII</button>
                </span>
              );
            },
          },
        ]} />
        <Muted>“Is PII” keeps the value redacted and takes it off the queue. “Not PII” restores it in every output.</Muted>
      </Card>
    </>
  );
}

function AddMissed({ run, additions, setAdditions }: { run: Run; additions: ReviewAddition[]; setAdditions: (a: ReviewAddition[]) => void }) {
  const { meta } = useStore();
  const [text, setText] = useState("");
  const [entity, setEntity] = useState("PERSON");
  const [file, setFile] = useState("");
  const add = () => {
    const t = text.trim();
    if (t.length < 2) return;
    setAdditions([...additions.filter((a) => !(a.text === t && a.entity_type === entity && a.file === (file || null))),
      { text: t, entity_type: entity, file: file || null }]);
    setText("");
  };
  return (
    <Row cols="2fr 3fr">
      <Card title="Add a value the detectors missed" sub="It is redacted wherever it appears, in any letter case">
        <label className="field">
          <span className="field-label">Value, exactly as written</span>
          <input className="input" value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()}
            placeholder="for example a name read on a page" />
        </label>
        <Select label="Category" value={entity} onChange={setEntity} options={meta.entities.map((e) => ({ value: e, label: pretty(e) }))} />
        <Select label="Where" value={file} onChange={setFile}
          options={[{ value: "", label: "Every file in this run" }, ...run.files.map((f) => ({ value: f.file, label: f.file }))]} />
        <button className="btn" onClick={add} disabled={text.trim().length < 2}><Plus size={16} /> Add to this review</button>
      </Card>
      <Card title="To be added" sub="Applied with the decisions in the next step">
        <DataTable rows={additions} empty="Nothing added yet." cols={[
          { key: "text", label: "Value", value: (a) => a.text },
          { key: "cat", label: "Category", value: (a) => pretty(a.entity_type) },
          { key: "file", label: "Where", value: (a) => a.file ?? "every file" },
          { key: "rm", label: "", value: () => null, align: "right", render: (a) => (
            <button className="btn icon" aria-label={`Remove ${a.text}`} onClick={() => setAdditions(additions.filter((x) => x !== a))}><X size={15} /></button>
          ) },
        ]} />
      </Card>
    </Row>
  );
}

function Apply({ run, decisions, additions, clear }: { run: Run; decisions: ReviewDecision[]; additions: ReviewAddition[]; clear: () => void }) {
  const { scan, submitReview, settings, setSettings, meta } = useStore();
  const [error, setError] = useState<string | null>(null);
  const active = isActive(scan);
  const nothing = !decisions.length && !additions.length;
  const apply = async () => {
    setError(null);
    try {
      await submitReview(decisions, additions);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <>
      <Kpis tiles={[
        { label: "Confirmed as PII", value: decisions.filter((d) => d.action === "approve").length, sub: "stay redacted" },
        { label: "Rejected", value: decisions.filter((d) => d.action === "reject").length, sub: "restored in every output" },
        { label: "Added", value: additions.length, sub: "values the detectors missed" },
        { label: "Reviewer", value: settings.operator || meta.default_operator || "–", sub: "recorded in the audit log" },
      ]} />
      {active ? <Progress /> : (
        <Card title="Apply and write the outputs again"
          sub="The LLM text, masked copies, register and manifest are rewritten; the audit log is appended to, never rewritten.">
          <label className="field" style={{ maxWidth: "22rem" }}>
            <span className="field-label">Reviewer</span>
            <input className="input" value={settings.operator ?? ""} placeholder={meta.default_operator}
              onChange={(e) => setSettings({ operator: e.target.value || null })} />
          </label>
          <div className="inline">
            <button className="btn primary" disabled={nothing} onClick={apply}><ShieldCheck size={16} /> Apply {decisions.length + additions.length} change(s)</button>
            <button className="btn" disabled={nothing} onClick={clear}><RotateCcw size={15} /> Discard</button>
            <a className="btn" href="/api/run/gold-draft" download><Download size={16} /> Download as gold labels</a>
          </div>
          <Muted>Masked copies are verified again after rewriting, so this takes about as long as the redaction stage of a scan. The gold-label file marks reviewed rows and contains original values.</Muted>
          {error && <Notice kind="error">{error}</Notice>}
          {scan.state === "failed" && scan.kind === "review" && <Notice kind="error">The review stopped: {scan.error}</Notice>}
        </Card>
      )}
      <Card title="Reviews applied to this run" sub="Each is one entry in the audit chain">
        <DataTable rows={run.reviews} empty="None yet." cols={[
          { key: "when", label: "When (UTC)", value: (r) => r.timestamp.replace("T", " ").slice(0, 19) },
          { key: "who", label: "Reviewer", value: (r) => r.operator },
          { key: "ok", label: "Confirmed", value: (r) => r.approved, align: "right" },
          { key: "no", label: "Rejected", value: (r) => r.rejected, align: "right" },
          { key: "add", label: "Added", value: (r) => r.added, align: "right" },
        ]} />
      </Card>
    </>
  );
}

function Body() {
  const run = useStore().run!;
  const [step, setStep] = useStep(3);
  const [picked, setPicked] = useState<Record<string, Action>>({});
  const [values, setValues] = useState<Record<string, QueueRow>>({});
  const [additions, setAdditions] = useState<ReviewAddition[]>([]);
  const clear = () => { setPicked({}); setValues({}); setAdditions([]); };
  useEffect(clear, [run.run_id, run.reviews.length]); // applied, or a new run: start clean
  const pick = (row: QueueRow, a: Action | null) => {
    const k = keyOf(row);
    setPicked((old) => {
      const next = { ...old };
      if (a) next[k] = a; else delete next[k];
      return next;
    });
    setValues((old) => ({ ...old, [k]: row }));
  };
  const decisions: ReviewDecision[] = Object.entries(picked).map(([k, action]) => ({ entity_type: values[k].entity_type, value: values[k].value, action }));
  return (
    <>
      <Steps steps={["Review queue", "Add missed", "Apply"]} active={step} onChange={setStep} />
      {step === 0 && <Queue run={run} picked={picked} pick={pick} />}
      {step === 1 && <AddMissed run={run} additions={additions} setAdditions={setAdditions} />}
      {step === 2 && <Apply run={run} decisions={decisions} additions={additions} clear={clear} />}
    </>
  );
}

export default function Review() {
  return (
    <>
      <PageHeader section="Assurance" title="Review" subtitle="A human confirms, rejects and adds; every output is then written again." />
      <NeedRun><Body /></NeedRun>
    </>
  );
}
