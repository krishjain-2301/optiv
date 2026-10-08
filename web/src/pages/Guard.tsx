// Workspace · Prompt guard: check a prompt -> restore the answer -> conversation.
// Text typed or pasted for an LLM goes through the same detection as a file and comes back with
// its values replaced; the answer gets them back from the tokens this conversation issued.
import { Check, Copy, KeyRound, ShieldCheck, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, type GuardResult, type GuardState, type Rehydrated } from "../api";
import { Meter } from "../components/charts";
import { DataTable } from "../components/table";
import { Card, Kpis, Muted, Notice, PageHeader, Row, Steps, useStep } from "../components/ui";
import { DECISION_LABEL, pretty } from "../lib/entities";
import { int } from "../lib/format";
import { useStore } from "../store";

const TOKEN = /(\[[A-Z][A-Z_]*(?:_[A-Z0-9*]+)*\])/;
const EXAMPLE = `Summarise this incident for the audit committee in five bullet points.

Priya Raman (EMP-40718) reported that the vendor portal was still open for Rafael Mendoza-Kowalski
three weeks after his contract ended. She can be reached at priya.raman@cadence-demo.example
or on (415) 555-0142. Raman escalated it to the access team the same day.`;

/** Text with its tokens set apart, so what was replaced is visible at a glance. */
function Tokens({ text }: { text: string }) {
  return <>{text.split(TOKEN).map((part, i) => (i % 2 ? <span className="tok" key={i}>{part}</span> : part))}</>;
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return <button className="btn" onClick={copy}>{copied ? <Check size={16} /> : <Copy size={16} />} {copied ? "Copied" : "Copy"}</button>;
}

function CheckPrompt({ text, setText, result, setResult, refresh }: {
  text: string; setText: (t: string) => void; result: GuardResult | null; setResult: (r: GuardResult | null) => void; refresh: () => void;
}) {
  const { settings } = useStore();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const check = async () => {
    setError(null);
    setBusy(true);
    try {
      setResult(await api.guardCheck(text, settings));
      refresh();
    } catch (e) {
      setResult(null); // fail closed: an earlier safe text must not stand in for this prompt
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const edit = (t: string) => {
    setText(t);
    setResult(null); // the safe text belongs to the prompt it was made from
  };
  return (
    <>
      {result && (
        <Kpis tiles={[
          { label: "Verdict", value: result.verdict === "clean" ? "Clean" : "Redacted", status: result.verdict === "clean" ? "good" : "warning",
            sub: result.verdict === "clean" ? "nothing personal was found" : "send the safe text, not the prompt" },
          { label: "Values replaced", value: int(result.values), sub: `${int(result.findings.length)} place(s) in the prompt` },
          { label: "Uncertain", value: int(result.review), status: result.review ? "warning" : "good", sub: "replaced anyway (fail closed)" },
          { label: "Source code", value: result.code.detected ? `${int(result.code.code_lines)} lines` : "None", status: result.code.detected ? "warning" : "good",
            sub: result.code.detected ? result.code.languages.join(", ") || "language not recognised" : "the prompt reads as prose" },
          { label: "Prompt size", value: `${int(result.chars)} chars`, sub: `${int(result.bytes)} bytes` },
          { label: "Checked in", value: `${result.elapsed.toFixed(2)} s`, sub: "on this machine, no network" },
        ]} />
      )}
      {result?.code.detected && (
        <Notice kind="warning">
          This prompt contains source code ({result.code.blocks.map(([a, b]) => (a === b ? `line ${a}` : `lines ${a} to ${b}`)).join(", ")}).
          Credentials and personal data in it were replaced; the code itself was not, and whether it may leave the company is for you to judge.
        </Notice>
      )}
      {result?.restarted && <Notice kind="warning">The profile or token key changed, so a new conversation was started: tokens issued before this prompt can no longer be restored.</Notice>}
      <Row>
        <Card title="Your prompt" sub="Checked on this machine before anything is sent">
          <textarea className="input" rows={14} value={text} onChange={(e) => edit(e.target.value)} placeholder="Type or paste what you were about to send to an LLM" />
          <div className="inline">
            <button className="btn primary" onClick={check} disabled={busy || !text.trim()}><ShieldCheck size={16} /> {busy ? "Checking…" : "Check before sending"}</button>
            <button className="btn" onClick={() => edit(EXAMPLE)} disabled={busy}>Load an example</button>
            <span className="muted">{int(text.length)} chars · profile {settings.profile}</span>
          </div>
          {error && <Notice kind="error">{error}</Notice>}
        </Card>
        <Card title="Safe to send" sub="Same text, values replaced by tokens">
          <pre className="text-pane">{result ? <Tokens text={result.safe_text} /> : "Nothing checked yet."}</pre>
          {result && <div className="inline"><CopyButton text={result.safe_text} /><span className="muted">Paste the answer under “Restore the answer”.</span></div>}
        </Card>
      </Row>
      {result && (
        <Card title="What was replaced" sub="Every value, where it was, and why it was flagged">
          <DataTable rows={result.findings} pageSize={25} empty="Nothing personal was found in this prompt." cols={[
            { key: "value", label: "Value", value: (f) => f.text },
            { key: "cat", label: "Category", value: (f) => pretty(f.entity_type) },
            { key: "token", label: "Replaced by", value: (f) => f.token },
            { key: "score", label: "Score", value: (f) => f.score, render: (f) => <Meter value={f.score} label={f.score.toFixed(2)} /> },
            { key: "decision", label: "Decision", value: (f) => DECISION_LABEL[f.decision] ?? f.decision },
            { key: "line", label: "Line", value: (f) => f.line, align: "right" },
            { key: "layer", label: "Layer", value: (f) => f.layer },
            { key: "why", label: "Why it was flagged", value: (f) => f.reasons.join("; "), wrap: true, width: "24rem" },
          ]} />
        </Card>
      )}
      <Muted>Uses the detection policy set under New scan (thresholds, profile, allow and deny lists, token key). The prompt is not stored; tokens and their values are held in memory until the server stops or the conversation is forgotten.</Muted>
    </>
  );
}

function Restore({ state, refresh }: { state: GuardState | null; refresh: () => void }) {
  const { settings } = useStore();
  const [text, setText] = useState("");
  const [out, setOut] = useState<Rehydrated | null>(null);
  const [error, setError] = useState<string | null>(null);
  const go = async () => {
    setError(null);
    try {
      setOut(await api.guardRehydrate(text, settings.operator));
      refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <>
      {state && !state.tokens && <Notice kind="info">No tokens have been issued in this conversation yet. Check a prompt first.</Notice>}
      <Row>
        <Card title="The LLM's answer" sub="As it came back, tokens included">
          <textarea className="input" rows={12} value={text} onChange={(e) => { setText(e.target.value); setOut(null); }}
            placeholder="[PERSON_001] reported the open portal; [PERSON_002]'s access should have ended with the contract." />
          <div className="inline">
            <button className="btn primary" onClick={go} disabled={!text.trim()}><KeyRound size={16} /> Restore original values</button>
          </div>
          {error && <Notice kind="error">{error}</Notice>}
        </Card>
        <Card title="With original values" sub="Shown here only; not written to any file">
          <pre className="text-pane">{out ? out.text : "Nothing restored yet."}</pre>
          {out && <div className="inline"><CopyButton text={out.text} /></div>}
          {out && <Muted>{out.restored.length} token(s) restored.{out.unknown.length ? ` Left as they are: ${out.unknown.join(", ")} (not issued in this conversation, or one token stands for several values by design).` : ""}</Muted>}
        </Card>
      </Row>
      <Muted>Only tokens issued by this conversation can be restored. Each use is added to the conversation log.</Muted>
    </>
  );
}

function Conversation({ state, forget }: { state: GuardState | null; forget: () => void }) {
  if (!state) return <Muted>Loading…</Muted>;
  return (
    <>
      <Kpis tiles={[
        { label: "Prompts checked", value: int(state.checks), sub: "in this conversation" },
        { label: "Tokens held", value: int(state.tokens), sub: "with their values, in memory only" },
        { label: "People", value: int(state.people), sub: "looked for again in every later prompt" },
        { label: "Answers restored", value: int(state.restored), sub: "tokens turned back into values" },
      ]} />
      <Card title="Conversation log" sub="Counts only: no prompt, answer or value is kept here">
        <DataTable rows={state.log} pageSize={25} empty="Nothing yet." cols={[
          { key: "when", label: "When (UTC)", value: (e) => e.timestamp.replace("T", " ").slice(0, 19) },
          { key: "what", label: "Event", value: (e) => (e.event === "check" ? `Prompt ${e.id}` : "Answer restored") },
          { key: "verdict", label: "Verdict", value: (e) => e.verdict ?? "" },
          { key: "values", label: "Values", value: (e) => (e.event === "check" ? e.values ?? 0 : e.count ?? 0), align: "right" },
          { key: "review", label: "Uncertain", value: (e) => e.review ?? null, align: "right" },
          { key: "bytes", label: "Bytes", value: (e) => e.bytes ?? null, align: "right" },
          { key: "code", label: "Code lines", value: (e) => e.code_lines ?? null, align: "right" },
          { key: "cats", label: "Categories", wrap: true, width: "22rem",
            value: (e) => Object.entries(e.by_category ?? {}).map(([k, n]) => `${pretty(k)} ${n}`).join(", ") },
          { key: "who", label: "Operator", value: (e) => e.operator },
        ]} />
        <div className="inline">
          <button className="btn danger" onClick={forget} disabled={!state.checks && !state.log.length}><Trash2 size={16} /> Forget this conversation</button>
          <span className="muted">Deletes the tokens, their values and this log. Earlier answers can then no longer be restored.</span>
        </div>
      </Card>
    </>
  );
}

export default function Guard() {
  const [step, setStep] = useStep(3);
  const [text, setText] = useState("");
  const [result, setResult] = useState<GuardResult | null>(null);
  const [state, setState] = useState<GuardState | null>(null);
  const refresh = useCallback(() => { api.guard().then(setState).catch(() => setState(null)); }, []);
  useEffect(refresh, [refresh]);
  const forget = async () => {
    setState(await api.guardForget());
    setResult(null);
  };
  return (
    <>
      <PageHeader section="Workspace" title="Prompt guard" subtitle="Check what you are about to send to an LLM, send the safe text instead, and get the values back in the answer." />
      <Steps steps={["Check a prompt", "Restore the answer", "Conversation"]} active={step} onChange={setStep} />
      {step === 0 && <CheckPrompt text={text} setText={setText} result={result} setResult={setResult} refresh={refresh} />}
      {step === 1 && <Restore state={state} refresh={refresh} />}
      {step === 2 && <Conversation state={state} forget={forget} />}
    </>
  );
}
