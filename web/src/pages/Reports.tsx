// Assurance · Reports: shareable -> sensitive -> session.
import { Download, FolderArchive, Trash2 } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type OutputFile } from "../api";
import { DataTable } from "../components/table";
import { Card, Chip, Kpis, Muted, NeedRun, Notice, PageHeader, Steps, useStep } from "../components/ui";
import { fileSize } from "../lib/format";
import { isActive, useStore } from "../store";

function Files({ files }: { files: OutputFile[] }) {
  return (
    <DataTable rows={files} cols={[
      { key: "name", label: "File", value: (f) => f.name },
      { key: "kind", label: "Kind", value: (f) => f.kind },
      { key: "size", label: "Size", value: (f) => f.size, render: (f) => fileSize(f.size), align: "right" },
      { key: "dl", label: "", value: () => null, render: (f) => <a className="btn small" href={api.outputUrl(f.name)} download><Download size={14} /> Download</a>, align: "right" },
    ]} />
  );
}

function Body() {
  const { run, scan, deleteSession } = useStore();
  const [step, setStep] = useStep(3);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();
  const o = run!.outputs;
  const integrity = run!.integrity;
  const remove = async () => {
    setError(null);
    try {
      await deleteSession();
      navigate("/");
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <>
      <Steps steps={["Shareable", "Sensitive", "Session"]} active={step} onChange={setStep} />
      {step === 0 && (
        <>
          <Kpis tiles={[
            { label: "Shareable outputs", value: o.safe.length, status: "good", sub: "passed the leak gate or hold no raw values" },
            { label: "Masked copies", value: o.safe.filter((f) => f.kind === "Masked copy").length, sub: "same layout, metadata cleared" },
            { label: "Sensitive files", value: o.sensitive.length, status: o.sensitive.length ? "serious" : "good", sub: "contain original values" },
            { label: "Token vault", value: o.vault ? "Encrypted" : "Not saved", sub: o.vault ? "AES-256-GCM" : "no passphrase was set" },
            { label: "Outputs match the manifest", value: integrity.ok ? "Yes" : "No", status: integrity.ok ? "good" : "critical", sub: `${integrity.audit_records} audit records, chain ${integrity.ok ? "intact" : "broken"}` },
            { label: "Manifest signature", value: integrity.signed ? "Ed25519" : "Unsigned", status: integrity.signed ? "good" : "warning", sub: integrity.signed ? "valid" : "set PII_SHIELD_SIGNING_KEY to sign" },
          ]} />
          {integrity.problems.map((p) => <Notice kind="error" key={p}>{p}</Notice>)}
          <Card title="Safe to share" sub="Masked files, LLM text, masked register, summary, audit log">
            <Files files={o.safe} />
            <a className="btn primary" href="/api/run/outputs.zip" download><FolderArchive size={16} /> Download all shareable outputs (.zip)</a>
          </Card>
        </>
      )}
      {step === 1 && (
        <>
          <Notice kind="warning">These files contain original values: store securely, never send to an LLM.</Notice>
          <Card title="Contains original values" sub="Extracted text, the full register, the encrypted vault">
            {o.sensitive.length ? <Files files={o.sensitive} /> : <Muted>None.</Muted>}
          </Card>
        </>
      )}
      {step === 2 && (
        <Card title="This session's files" sub="Uploads, extracted text, reports and masked copies">
          <div className="chips"><Chip label="Run" value={run!.run_id} /><Chip label="Operator" value={run!.operator} /><Chip label="Folder" value={o.folder} /></div>
          <Muted>Files are deleted before each new run, on request, and when the server stops. Folders left by a server that was killed are removed the next time it starts.</Muted>
          <button className="btn danger" onClick={remove} disabled={isActive(scan)}><Trash2 size={16} /> Delete this session's files now</button>
          {error && <Notice kind="error">{error}</Notice>}
        </Card>
      )}
    </>
  );
}

export default function Reports() {
  return (
    <>
      <PageHeader section="Assurance" title="Reports" subtitle="Outputs of this run: what can be shared, and what must stay here." />
      <NeedRun><Body /></NeedRun>
    </>
  );
}
