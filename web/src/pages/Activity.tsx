// Assurance · Guard activity: summary -> people and data -> record.
// What the prompt guard did over all conversations, read from its hash-chained record: what was
// sent, by whom, and what was stopped. Counts only: the record never holds a prompt or a value.
import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, type GuardActivity } from "../api";
import { HBar, StackedBar } from "../components/charts";
import { DataTable } from "../components/table";
import { Card, Kpis, Muted, Notice, PageHeader, Row, Steps, useStep } from "../components/ui";
import { SERIES, STATUS_COLOR, entityColor, pretty } from "../lib/entities";
import { int, percent } from "../lib/format";

const RULE: Record<string, string> = {
  source_code: "Source code", marking: "Classification marking", category: "Blocked category", size: "Size limit",
  protected: "Protected content",
};
const VERDICT = { Blocked: STATUS_COLOR.critical, Redacted: SERIES[0], Clean: STATUS_COLOR.good };
const when = (t: string) => t.replace("T", " ").slice(0, 19);

function Summary({ a }: { a: GuardActivity }) {
  const t = a.totals;
  return (
    <>
      <Kpis tiles={[
        { label: "Prompts checked", value: int(t.checks), sub: a.first ? `since ${a.first.slice(0, 10)}` : "none yet" },
        { label: "Refused", value: int(t.blocked), status: t.blocked ? "critical" : "good", sub: t.checks ? `${percent(t.blocked / t.checks)} of all prompts` : "by policy" },
        { label: "Sent with values replaced", value: int(t.redacted), sub: `${int(t.values)} value(s) kept from the model` },
        { label: "Sent unchanged", value: int(t.clean), status: "good", sub: "nothing sensitive was found" },
        { label: "Answers restored", value: int(t.restored), sub: `${int(t.tokens_restored)} token(s) turned back into values` },
        { label: "People using the guard", value: int(t.operators), sub: "operators in the record" },
      ]} />
      <Row>
        <Card title="Why prompts were refused" sub="A prompt can be refused for more than one reason">
          {Object.keys(a.by_rule).length
            ? <HBar unit="Prompts" rows={Object.entries(a.by_rule).sort((x, y) => y[1] - x[1]).map(([k, v]) => ({ label: RULE[k] ?? k, value: v, color: STATUS_COLOR.critical }))} />
            : <Muted>No prompt has been refused.</Muted>}
          <Muted>{int(t.code_lines_stopped)} line(s) of source code were stopped before they left.</Muted>
        </Card>
        <Card title="Day by day" sub="Prompts checked, by outcome">
          <DataTable rows={[...a.by_day].reverse()} pageSize={10} empty="Nothing checked yet." cols={[
            { key: "day", label: "Day (UTC)", value: (d) => d.day },
            { key: "checks", label: "Checked", value: (d) => d.checks, align: "right" },
            { key: "blocked", label: "Refused", value: (d) => d.blocked, align: "right" },
            { key: "redacted", label: "Values replaced", value: (d) => d.redacted, align: "right" },
            { key: "clean", label: "Unchanged", value: (d) => d.clean, align: "right" },
            { key: "values", label: "Values kept back", value: (d) => d.values, align: "right" },
          ]} />
        </Card>
      </Row>
      {(t.echoed > 0 || t.produced > 0) && (
        <Notice kind="warning">Answers held {int(t.echoed)} value(s) that had only been sent as tokens and {int(t.produced)} value(s) the model produced itself. Both are reported when an answer is restored.</Notice>
      )}
    </>
  );
}

function People({ a }: { a: GuardActivity }) {
  const replaced = Object.entries(a.replaced_by_category).sort((x, y) => y[1] - x[1]);
  const stopped = Object.entries(a.stopped_by_category).sort((x, y) => y[1] - x[1]);
  return (
    <>
      <Row cols="3fr 2fr">
        <Card title="By operator" sub="Who used the guard, and what happened to their prompts">
          <StackedBar unit="Prompts" colors={VERDICT}
            rows={a.by_operator.filter((o) => o.checks).map((o) => ({ label: o.operator, parts: { Blocked: o.blocked, Redacted: o.redacted, Clean: o.clean } }))} />
          <DataTable rows={a.by_operator} pageSize={10} empty="Nobody yet." cols={[
            { key: "who", label: "Operator", value: (o) => o.operator },
            { key: "checks", label: "Checked", value: (o) => o.checks, align: "right" },
            { key: "blocked", label: "Refused", value: (o) => o.blocked, align: "right" },
            { key: "redacted", label: "Values replaced", value: (o) => o.redacted, align: "right" },
            { key: "clean", label: "Unchanged", value: (o) => o.clean, align: "right" },
            { key: "restored", label: "Answers restored", value: (o) => o.restored, align: "right" },
            { key: "last", label: "Last seen (UTC)", value: (o) => when(o.last) },
          ]} />
        </Card>
        <div className="stack-v">
          <Card title="What was kept from the model" sub="Occurrences replaced by tokens in prompts that were sent">
            {replaced.length ? <HBar unit="Occurrences" rows={replaced.map(([k, v]) => ({ label: pretty(k), value: v, color: entityColor(k) }))} /> : <Muted>Nothing yet.</Muted>}
          </Card>
          <Card title="What refused prompts held" sub="Found in prompts that never left">
            {stopped.length ? <HBar unit="Occurrences" rows={stopped.map(([k, v]) => ({ label: pretty(k), value: v, color: entityColor(k) }))} /> : <Muted>Nothing yet.</Muted>}
          </Card>
        </div>
      </Row>
      <Muted>The operator is the name set under New scan, or the user the server runs as. There is no login: the name is recorded as given.</Muted>
    </>
  );
}

function Record({ a }: { a: GuardActivity }) {
  return (
    <>
      <Kpis tiles={[
        { label: "Record", value: a.integrity.ok ? "Intact" : "Broken", status: a.integrity.ok ? "good" : "critical", sub: a.integrity.ok ? "every line follows the one before" : a.integrity.detail },
        { label: "Entries", value: int(a.integrity.records), sub: a.last ? `last at ${when(a.last)}` : "none yet" },
        { label: "Text kept", value: "None", status: "good", sub: "counts and categories only" },
      ]} />
      <Card title="Latest entries" sub={`Stored in ${a.path}`}>
        <DataTable rows={a.recent} pageSize={25} empty="Nothing recorded yet." cols={[
          { key: "when", label: "When (UTC)", value: (e) => when(e.timestamp) },
          { key: "who", label: "Operator", value: (e) => e.operator },
          { key: "what", label: "Event", value: (e) => (e.event === "check" ? "Prompt checked" : "Answer restored") },
          { key: "verdict", label: "Outcome", value: (e) => (e.event === "check" ? e.verdict ?? "" : `${e.count ?? 0} token(s) restored`) },
          { key: "why", label: "Refused by", value: (e) => (e.blocked_by ?? []).map((r) => RULE[r] ?? r).join(", ") },
          { key: "values", label: "Values", value: (e) => e.values ?? null, align: "right" },
          { key: "bytes", label: "Bytes", value: (e) => e.bytes ?? null, align: "right" },
          { key: "code", label: "Code lines", value: (e) => e.code_lines ?? null, align: "right" },
          { key: "cats", label: "Categories", wrap: true, width: "18rem",
            value: (e) => Object.entries(e.by_category ?? {}).map(([k, n]) => `${pretty(k)} ${n}`).join(", ") },
        ]} />
        <Muted>The record is append-only and hash-chained: a changed, removed or reordered line breaks it from that point on. It outlives the server, the session and “Forget this conversation”.</Muted>
      </Card>
    </>
  );
}

export default function Activity() {
  const [step, setStep] = useStep(3);
  const [a, setA] = useState<GuardActivity | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => { api.guardActivity().then((x) => { setA(x); setError(null); }).catch((e: Error) => setError(e.message)); }, []);
  useEffect(load, [load]);
  return (
    <>
      <PageHeader section="Assurance" title="Guard activity" subtitle="What was sent to an LLM through the prompt guard, by whom, and what was stopped." />
      <Steps steps={["Summary", "People and data", "Record"]} active={step} onChange={setStep} />
      {error && <Notice kind="error">{error}</Notice>}
      {!a && !error && <Muted>Loading…</Muted>}
      {a && !a.integrity.ok && <Notice kind="error">The record has been altered: {a.integrity.detail}. Figures below are read from what is left of it.</Notice>}
      {a && step === 0 && <Summary a={a} />}
      {a && step === 1 && <People a={a} />}
      {a && step === 2 && <Record a={a} />}
      <div className="inline"><button className="btn" onClick={load}><RefreshCw size={15} /> Refresh</button></div>
    </>
  );
}
