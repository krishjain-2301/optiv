// Workspace · Prompt guard: check a prompt -> restore the answer -> conversation -> policy -> protected content -> Samsung replay.
// Text typed or pasted for an LLM goes through the same detection as a file and comes back with
// its values replaced, or is refused by the policy; the answer gets its values back from the
// tokens this conversation issued.
import { Ban, Check, Copy, Fingerprint, KeyRound, Play, RotateCcw, ShieldCheck, Trash2, Upload } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, type GuardAction, type GuardPolicy, type GuardResult, type GuardState, type RegistryState, type Rehydrated, type Replay } from "../api";
import { Meter } from "../components/charts";
import { MultiSelect, Select } from "../components/controls";
import { DataTable } from "../components/table";
import { Card, Chip, Kpis, Muted, Notice, PageHeader, Row, Steps, useStep } from "../components/ui";
import { DECISION_LABEL, pretty } from "../lib/entities";
import { int } from "../lib/format";
import { useStore } from "../store";

const TOKEN = /(\[[A-Z][A-Z_]*(?:_[A-Z0-9*]+)*\])/;
const POLICY_KEY = "pii-shield-guard-policy";
const EXAMPLE = `Summarise this incident for the audit committee in five bullet points.

Priya Raman (EMP-40718) reported that the vendor portal was still open for Rafael Mendoza-Kowalski
three weeks after his contract ended. She can be reached at priya.raman@cadence-demo.example
or on (415) 555-0142. Raman escalated it to the access team the same day.`;
const RULE: Record<string, string> = {
  source_code: "Source code", marking: "Classification marking", category: "Blocked category", size: "Size limit",
  protected: "Protected content",
};
const ACTIONS = [
  { value: "block", label: "Block the prompt" },
  { value: "warn", label: "Warn, and replace values" },
  { value: "allow", label: "Allow, and replace values" },
];
const lines = (text: string) => text.split("\n").map((s) => s.trim()).filter(Boolean);

function savedPolicy(): GuardPolicy | null {
  try {
    return JSON.parse(localStorage.getItem(POLICY_KEY) ?? "null");
  } catch {
    return null;
  }
}

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

function CheckPrompt({ text, setText, result, setResult, policy, refresh }: {
  text: string; setText: (t: string) => void; result: GuardResult | null; setResult: (r: GuardResult | null) => void;
  policy: GuardPolicy; refresh: () => void;
}) {
  const { settings } = useStore();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const check = async () => {
    setError(null);
    setBusy(true);
    try {
      setResult(await api.guardCheck(text, settings, policy));
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
  const blocked = result?.verdict === "blocked";
  const verdict = !result ? null : blocked
    ? { value: "Blocked", status: "critical" as const, sub: "nothing may be sent" }
    : result.verdict === "clean"
      ? { value: "Clean", status: "good" as const, sub: "nothing personal was found" }
      : { value: "Redacted", status: "warning" as const, sub: "send the safe text, not the prompt" };
  return (
    <>
      {result && verdict && (
        <Kpis tiles={[
          { label: "Verdict", ...verdict },
          { label: blocked ? "Values found" : "Values replaced", value: int(result.values), sub: `${int(result.findings.length)} place(s) in the prompt` },
          { label: "Uncertain", value: int(result.review), status: result.review ? "warning" : "good", sub: blocked ? "scored in the review band" : "replaced anyway (fail closed)" },
          { label: "Source code", value: result.code.detected ? `${int(result.code.code_lines)} lines` : "None", status: result.code.detected ? "warning" : "good",
            sub: result.code.detected ? result.code.languages.join(", ") || "language not recognised" : "the prompt reads as prose" },
          { label: "Prompt size", value: `${int(result.bytes)} bytes`, sub: `checked in ${result.elapsed.toFixed(2)} s on this machine` },
        ]} />
      )}
      {blocked && (
        <Notice kind="error">
          <Ban size={18} />
          <span>
            <b>This prompt is blocked by policy and must not be sent.</b>
            {result.blocks.map((b, i) => <span key={i}><br />{RULE[b.rule]}{b.category ? ` (${pretty(b.category)})` : ""}: {b.detail}.</span>)}
          </span>
        </Notice>
      )}
      {result?.warnings.map((w, i) => (
        <Notice kind="warning" key={i}>{RULE[w.rule]}: {w.detail}. Values in it were replaced; whether the rest may leave the company is for you to judge.</Notice>
      ))}
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
        <Card title="Safe to send" sub={blocked ? "Nothing: the prompt was refused" : "Same text, values replaced by tokens"}>
          <pre className="text-pane">{!result ? "Nothing checked yet." : blocked ? "Blocked. There is no safe version of this prompt to send." : <Tokens text={result.safe_text} />}</pre>
          {result && !blocked && <div className="inline"><CopyButton text={result.safe_text} /><span className="muted">Paste the answer under “Restore the answer”.</span></div>}
        </Card>
      </Row>
      {result && result.findings.length > 0 && (
        <Card title={blocked ? "What was found" : "What was replaced"} sub="Every value, where it was, and why it was flagged">
          <DataTable rows={result.findings} pageSize={25} cols={[
            { key: "value", label: "Value", value: (f) => f.text },
            { key: "cat", label: "Category", value: (f) => pretty(f.entity_type) },
            { key: "token", label: "Replaced by", value: (f) => f.token ?? "–" },
            { key: "score", label: "Score", value: (f) => f.score, render: (f) => <Meter value={f.score} label={f.score.toFixed(2)} /> },
            { key: "decision", label: "Decision", value: (f) => DECISION_LABEL[f.decision] ?? f.decision },
            { key: "line", label: "Line", value: (f) => f.line, align: "right" },
            { key: "layer", label: "Layer", value: (f) => f.layer },
            { key: "why", label: "Why it was flagged", value: (f) => f.reasons.join("; "), wrap: true, width: "24rem" },
          ]} />
        </Card>
      )}
      <Muted>Uses the detection policy set under New scan (thresholds, profile, allow and deny lists, token key) and the guard policy in the last step. The prompt is not stored; tokens and their values are held in memory until the server stops or the conversation is forgotten.</Muted>
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
        { label: "Blocked", value: int(state.blocked), status: state.blocked ? "critical" : "good", sub: "refused by policy" },
        { label: "Tokens held", value: int(state.tokens), sub: "with their values, in memory only" },
        { label: "People", value: int(state.people), sub: "looked for again in every later prompt" },
        { label: "Answers restored", value: int(state.restored), sub: "tokens turned back into values" },
      ]} />
      <Card title="Conversation log" sub="Counts only: no prompt, answer or value is kept here">
        <DataTable rows={state.log} pageSize={25} empty="Nothing yet." cols={[
          { key: "when", label: "When (UTC)", value: (e) => e.timestamp.replace("T", " ").slice(0, 19) },
          { key: "what", label: "Event", value: (e) => (e.event === "check" ? `Prompt ${e.id}` : "Answer restored") },
          { key: "verdict", label: "Verdict", value: (e) => e.verdict ?? "" },
          { key: "why", label: "Blocked by", value: (e) => (e.blocked_by ?? []).map((r) => RULE[r] ?? r).join(", ") },
          { key: "values", label: "Values", value: (e) => (e.event === "check" ? e.values ?? 0 : e.count ?? 0), align: "right" },
          { key: "review", label: "Uncertain", value: (e) => e.review ?? null, align: "right" },
          { key: "bytes", label: "Bytes", value: (e) => e.bytes ?? null, align: "right" },
          { key: "code", label: "Code lines", value: (e) => e.code_lines ?? null, align: "right" },
          { key: "cats", label: "Categories", wrap: true, width: "20rem",
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

function Policy({ policy, setPolicy, reset }: { policy: GuardPolicy; setPolicy: (p: Partial<GuardPolicy>) => void; reset: () => void }) {
  const { meta, settings, setSettings } = useStore();
  return (
    <>
      <Row>
        <Card title="When the whole prompt is refused" sub="Personal data and credentials are always replaced; these decide when that is not enough">
          <Select label="A prompt that is source code" value={policy.source_code} options={ACTIONS} onChange={(v) => setPolicy({ source_code: v as GuardAction })} />
          <Select label="A prompt that carries a classification marking" value={policy.markings} options={ACTIONS} onChange={(v) => setPolicy({ markings: v as GuardAction })} />
          <Select label="A prompt that copies from a registered document" value={policy.protected} options={ACTIONS} onChange={(v) => setPolicy({ protected: v as GuardAction })} />
          <MultiSelect label="Categories that refuse the prompt instead of being replaced" all="None: every category is replaced"
            options={meta.entities.map((e) => ({ value: e, label: pretty(e) }))} selected={policy.block_categories} onChange={(v) => setPolicy({ block_categories: v })} />
          <label className="field" style={{ maxWidth: "16rem" }}>
            <span className="field-label">Size limit in bytes (0: none)</span>
            <input className="input" type="number" min={0} step={256} value={policy.max_bytes}
              onChange={(e) => setPolicy({ max_bytes: Math.max(0, Math.round(Number(e.target.value) || 0)) })} />
          </label>
          <div className="inline">
            <button className="btn" onClick={reset}><RotateCcw size={15} /> Back to the organisation's policy</button>
          </div>
        </Card>
        <div className="stack-v">
          <Card title="Confidential terms" sub="Project codenames, unreleased product names, internal hosts: one per line">
            <textarea className="input" rows={6} defaultValue={settings.confidential_terms.join("\n")} placeholder={"Project Falcon\nBluebird"}
              onChange={(e) => setSettings({ confidential_terms: lines(e.target.value) })} />
            <Muted>Replaced by [TERM_…] tokens in prompts and in scanned files, in any letter case.{meta.org_terms ? ` ${meta.org_terms} more come from the organisation's configuration.` : ""}</Muted>
          </Card>
          <Card title="Classification markings" sub="Set in the organisation's configuration (org.yaml)">
            <p className="muted">{meta.markings.join(" · ") || "None configured."}</p>
            <Muted>A phrase of several words counts wherever it is written. A single word counts only as a marking: after “Classification:”, in brackets, or on a short line of its own.</Muted>
          </Card>
        </div>
      </Row>
      <Muted>A size limit alone is a blunt control: it refuses a long harmless prompt and lets a short secret through. It is here for comparison and for organisations that want both.</Muted>
    </>
  );
}

/** Confidential documents registered by fingerprint: a prompt that copies from one is refused. */
function ProtectedContent({ onChange }: { onChange: () => void }) {
  const { settings } = useStore();
  const [reg, setReg] = useState<RegistryState | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { api.registry().then(setReg).catch((e: Error) => setError(e.message)); }, []);
  const act = async (call: () => Promise<RegistryState>, done?: () => void) => {
    setError(null);
    setBusy(true);
    try {
      setReg(await call());
      onChange(); // a verdict belongs to the registry it was made against
      done?.();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const register = () => act(() => api.register(files, name, text, settings.operator), () => { setFiles([]); setName(""); setText(""); });
  const docs = reg?.documents ?? [];
  return (
    <>
      <Kpis tiles={[
        { label: "Registered documents", value: int(docs.length), sub: "a prompt that copies from one is refused" },
        { label: "Fingerprints", value: int(docs.reduce((a, d) => a + d.fingerprints, 0)), sub: "keyed hashes of five-word runs" },
        { label: "Text kept", value: "None", status: "good", sub: "a document is fingerprinted, then deleted" },
      ]} />
      <Row cols="2fr 3fr">
        <Card title="Register a confidential document" sub="It is read on this machine, reduced to fingerprints and deleted">
          <label className="btn">
            <Upload size={16} /> Choose files
            <input type="file" multiple hidden onChange={(e) => { setFiles([...(e.target.files ?? [])]); e.target.value = ""; }} />
          </label>
          {files.length > 0 && <Muted>{files.map((f) => f.name).join(", ")}</Muted>}
          <label className="field">
            <span className="field-label">Or paste text, with a name for it</span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="for example: Q3 yield review notes" />
          </label>
          <textarea className="input" rows={6} value={text} onChange={(e) => setText(e.target.value)} placeholder="Text to protect" />
          <div className="inline">
            <button className="btn primary" onClick={register} disabled={busy || (!files.length && !text.trim())}><Fingerprint size={16} /> {busy ? "Registering…" : "Register"}</button>
          </div>
          {error && <Notice kind="error">{error}</Notice>}
          {Object.entries(reg?.errors ?? {}).map(([f, why]) => <Notice kind="error" key={f}>{f}: {why}</Notice>)}
        </Card>
        <Card title="Registered documents" sub={reg ? `Stored in ${reg.path}` : "Loading…"}>
          <DataTable rows={docs} empty="Nothing is registered yet." cols={[
            { key: "name", label: "Document", value: (d) => d.name, wrap: true, width: "18rem" },
            { key: "words", label: "Words", value: (d) => d.words, align: "right" },
            { key: "prints", label: "Fingerprints", value: (d) => d.fingerprints, align: "right" },
            { key: "when", label: "Registered (UTC)", value: (d) => d.registered.replace("T", " ").slice(0, 16) },
            { key: "who", label: "By", value: (d) => d.operator },
            { key: "rm", label: "", value: () => null, align: "right", render: (d) => (
              <button className="btn icon" aria-label={`Remove ${d.name}`} disabled={busy} onClick={() => act(() => api.unregister(d.id))}><Trash2 size={15} /></button>
            ) },
          ]} />
        </Card>
      </Row>
      <Muted>A prompt overlaps a registered document when it shares ten consecutive words with it, or thirty in all; case, punctuation and line breaks do not matter. This recognises copied wording only: a paraphrase, a translation or a summary is not caught. The registry outlives the server and is not deleted with the session.</Muted>
    </>
  );
}

const CAP_LABEL = { allowed: "Sent as written", blocked: "Refused unread" };
const GUARD_LABEL = { clean: "Sent unchanged", redacted: "Sent with values replaced", blocked: "Refused" };

/** Samsung, March 2023: the three incidents and two contrast cases, under a size cap and under the guard. */
function SamsungReplay({ tryPrompt }: { tryPrompt: (text: string) => void }) {
  const [cap, setCap] = useState(1024);
  const [replay, setReplay] = useState<Replay | null>(null);
  const [open, setOpen] = useState("incident-1");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async () => {
    setError(null);
    setBusy(true);
    try {
      setReplay(await api.guardReplay(cap));
    } catch (e) {
      setReplay(null);
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const s = replay?.summary;
  const shown = replay?.scenarios.find((x) => x.id === open) ?? replay?.scenarios[0];
  return (
    <>
      <Card title="Samsung, March 2023" sub="Three engineers pasted source code and the contents of a meeting into a public chatbot within twenty days">
        <p className="muted">Samsung's first control was a cap of 1,024 bytes per prompt. The same kinds of prompt, all invented here, are put through that cap and through the prompt guard. Two more show that a size cap fails in both directions.</p>
        <div className="inline">
          <label className="field" style={{ width: "11rem" }}>
            <span className="field-label">Size cap in bytes</span>
            <input className="input" type="number" min={1} step={256} value={cap} onChange={(e) => setCap(Math.max(1, Math.round(Number(e.target.value) || 1)))} />
          </label>
          <button className="btn primary" onClick={run} disabled={busy}><Play size={16} /> {busy ? "Running…" : "Run the replay"}</button>
        </div>
        {error && <Notice kind="error">{error}</Notice>}
      </Card>
      {replay && s && (
        <>
          <Kpis tiles={[
            { label: "Size cap: right calls", value: `${s.cap_right} of ${s.scenarios}`, status: s.cap_right === s.scenarios ? "good" : "critical", sub: `at ${int(replay.cap_bytes)} bytes` },
            { label: "Prompt guard: right calls", value: `${s.guard_right} of ${s.scenarios}`, status: s.guard_right === s.scenarios ? "good" : "critical", sub: "the organisation's default policy" },
            { label: "Sensitive prompts sent whole", value: `${s.cap_leaks} vs ${s.guard_leaks}`, status: s.cap_leaks ? "critical" : "good", sub: "under the cap vs under the guard" },
            { label: "Harmless prompts refused", value: `${s.cap_refused_harmless} vs ${s.guard_refused_harmless}`, status: s.cap_refused_harmless ? "warning" : "good", sub: "under the cap vs under the guard" },
          ]} />
          <Card title="Scenario by scenario" sub="A sensitive prompt is handled rightly if it is stopped or stripped; a harmless one, if it goes through whole">
            <DataTable rows={replay.scenarios} cols={[
              { key: "title", label: "Scenario", value: (x) => x.title, wrap: true, width: "17rem" },
              { key: "holds", label: "What the prompt holds", value: (x) => x.holds, wrap: true, width: "17rem" },
              { key: "bytes", label: "Bytes", value: (x) => x.bytes, align: "right" },
              { key: "cap", label: "Size cap", value: (x) => x.cap.verdict, render: (x) => <Chip label={CAP_LABEL[x.cap.verdict]} status={x.cap.right ? "good" : "critical"} /> },
              { key: "guard", label: "Prompt guard", value: (x) => x.guard.verdict, render: (x) => <Chip label={GUARD_LABEL[x.guard.verdict]} status={x.guard.right ? "good" : "critical"} /> },
              { key: "why", label: "What the guard did", value: (x) => x.guard.outcome, wrap: true, width: "20rem" },
              { key: "open", label: "", value: () => null, align: "right", render: (x) => (
                <button className={x.id === shown?.id ? "btn small on" : "btn small"} onClick={() => setOpen(x.id)}>View</button>
              ) },
            ]} />
            <Muted>Green: the control did the right thing. Red: it sent a sensitive prompt whole, or refused a harmless one.</Muted>
          </Card>
          {shown && (
            <>
              <Row>
                <Card title={shown.title} sub={shown.samsung}>
                  <pre className="text-pane">{shown.prompt}</pre>
                  <div className="inline">
                    <button className="btn" onClick={() => tryPrompt(shown.prompt)}><ShieldCheck size={16} /> Try it under “Check a prompt”</button>
                    <span className="muted">{int(shown.bytes)} bytes · invented for this replay</span>
                  </div>
                </Card>
                <Card title="What would have left the company" sub="Under each control">
                  <p className="field-label">Size cap of {int(replay.cap_bytes)} bytes</p>
                  <Notice kind={shown.cap.right ? "success" : "error"}>{shown.cap.outcome}.</Notice>
                  <p className="field-label">Prompt guard</p>
                  <Notice kind={shown.guard.right ? "success" : "error"}>{shown.guard.outcome}.</Notice>
                  {shown.guard.verdict !== "blocked" && <pre className="text-pane"><Tokens text={shown.guard.safe_text} /></pre>}
                </Card>
              </Row>
              {shown.id === "incident-3" && (
                <Notice kind="info">The guard replaces who was in the meeting and the codename ({replay.terms.join(", ")} are listed as confidential terms for this replay). What was discussed, such as the yield figures, still goes out: no rule can tell that a number is a trade secret. A classification marking on the notes, or a stricter policy, would refuse the prompt.</Notice>
              )}
            </>
          )}
        </>
      )}
    </>
  );
}

export default function Guard() {
  const { meta } = useStore();
  const [step, setStep] = useStep(6);
  const [text, setText] = useState("");
  const [result, setResult] = useState<GuardResult | null>(null);
  const [state, setState] = useState<GuardState | null>(null);
  const [own, setOwn] = useState<GuardPolicy | null>(savedPolicy);
  const policy = { ...meta.guard_policy, ...own }; // a policy saved before a rule existed takes that rule's default
  const setPolicy = (patch: Partial<GuardPolicy>) => {
    const next = { ...policy, ...patch };
    localStorage.setItem(POLICY_KEY, JSON.stringify(next));
    setOwn(next);
    setResult(null); // a verdict belongs to the policy it was made under
  };
  const resetPolicy = () => {
    localStorage.removeItem(POLICY_KEY);
    setOwn(null);
    setResult(null);
  };
  const refresh = useCallback(() => { api.guard().then(setState).catch(() => setState(null)); }, []);
  useEffect(refresh, [refresh]);
  const forget = async () => {
    setState(await api.guardForget());
    setResult(null);
  };
  return (
    <>
      <PageHeader section="Workspace" title="Prompt guard" subtitle="Check what you are about to send to an LLM, send the safe text instead, and get the values back in the answer." />
      <Steps steps={["Check a prompt", "Restore the answer", "Conversation", "Policy", "Protected content", "Samsung replay"]} active={step} onChange={setStep} />
      {step === 0 && <CheckPrompt text={text} setText={setText} result={result} setResult={setResult} policy={policy} refresh={refresh} />}
      {step === 1 && <Restore state={state} refresh={refresh} />}
      {step === 2 && <Conversation state={state} forget={forget} />}
      {step === 3 && <Policy policy={policy} setPolicy={setPolicy} reset={resetPolicy} />}
      {step === 4 && <ProtectedContent onChange={() => setResult(null)} />}
      {step === 5 && <SamsungReplay tryPrompt={(t) => { setText(t); setResult(null); setStep(0); }} />}
    </>
  );
}
