// Building blocks shared by the pages: header, step bar, stat tiles, cards, chips, empty state.
import { ArrowRight } from "lucide-react";
import type { ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { RATING_ICON, RATING_STATUS, type Status } from "../lib/entities";
import { useStore } from "../store";

export function Chip({ label, value, status }: { label: string; value?: ReactNode; status?: Status }) {
  return (
    <span className="chip">
      {status && <span className={`dot ${status}`} />}
      {label}
      {value !== undefined && <b>{value}</b>}
    </span>
  );
}

export function RatingChip({ rating }: { rating: string }) {
  return <Chip label={`${RATING_ICON[rating] ?? ""} ${rating}`} status={RATING_STATUS[rating]} />;
}

export function PageHeader({ section, title, subtitle }: { section: string; title: string; subtitle: string }) {
  const { run } = useStore();
  return (
    <header className="page-head">
      <div>
        <div className="eyebrow">{section}</div>
        <h1>{title}</h1>
        <p>{subtitle}</p>
      </div>
      <div className="chips">
        <Chip label="Offline" status="good" />
        {run && <Chip label="Run" value={run.run_id} />}
        {run && <Chip label="Files" value={run.files.length} />}
      </div>
    </header>
  );
}

/** The step of a mode, kept in the URL (?step=2) so a reload or a shared link lands on it. */
export function useStep(count: number): [number, (i: number) => void] {
  const [params, setParams] = useSearchParams();
  const raw = Number(params.get("step") ?? 1) - 1;
  const active = Number.isInteger(raw) && raw >= 0 && raw < count ? raw : 0;
  const set = (i: number) =>
    setParams((old) => {
      const next = new URLSearchParams(old);
      next.set("step", String(i + 1));
      return next;
    }, { replace: true });
  return [active, set];
}

/** A mode's process, left to right. */
export function Steps({ steps, active, onChange }: { steps: string[]; active: number; onChange: (i: number) => void }) {
  return (
    <div className="steps" role="tablist">
      {steps.map((s, i) => (
        <button key={s} role="tab" aria-selected={i === active} className={i === active ? "step active" : "step"}
          onClick={() => onChange(i)}>
          <span className="step-no">{i + 1}</span>
          {s}
        </button>
      ))}
    </div>
  );
}

export interface Tile {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  status?: Status;
  meter?: number | null;
}

export function Kpis({ tiles }: { tiles: Tile[] }) {
  const cols = tiles.length <= 6 ? tiles.length : Math.ceil(tiles.length / 2);
  return (
    <div className="kpis" style={{ "--cols": cols } as React.CSSProperties}>
      {tiles.map((t) => (
        <div className="kpi" key={t.label}>
          <div className="kpi-label">{t.label}</div>
          <div className="kpi-value">
            {t.status && <span className={`dot ${t.status}`} />}
            {t.value}
          </div>
          {t.sub && <div className="kpi-sub">{t.sub}</div>}
          {t.meter != null && (
            <div className="meter"><i style={{ width: `${Math.max(0, Math.min(t.meter, 1)) * 100}%` }} /></div>
          )}
        </div>
      ))}
    </div>
  );
}

export function Card({ title, sub, children, className = "" }: {
  title?: string; sub?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {title && <h2 className="card-title">{title}</h2>}
      {sub && <p className="card-sub">{sub}</p>}
      {children}
    </section>
  );
}

/** Cards side by side; `cols` is a CSS grid template such as "3fr 2fr". */
export function Row({ cols = "1fr 1fr", children }: { cols?: string; children: ReactNode }) {
  return <div className="row" style={{ gridTemplateColumns: cols }}>{children}</div>;
}

export function Legend({ items }: { items: Record<string, string> }) {
  return (
    <div className="legend">
      {Object.entries(items).map(([name, color]) => (
        <span key={name}><i style={{ background: color }} />{name}</span>
      ))}
    </div>
  );
}

export function Notice({ kind, children }: { kind: "info" | "warning" | "error" | "success"; children: ReactNode }) {
  return <div className={`notice ${kind}`} role={kind === "error" ? "alert" : "status"}>{children}</div>;
}

export function Muted({ children }: { children: ReactNode }) {
  return <p className="muted">{children}</p>;
}

/** Wraps a page that needs a run: shows a way to start one when there is none. */
export function NeedRun({ children }: { children: ReactNode }) {
  const { run, ready, scan } = useStore();
  if (!ready) return <Muted>Loading…</Muted>;
  if (run) return <>{children}</>;
  const busy = scan.state === "running" || scan.state === "paused";
  return (
    <div className="empty">
      <b>{busy ? "A scan is running" : "No scan yet"}</b>
      {busy ? "This view fills in when it finishes." : "Run the pipeline on your files, or on the synthetic samples, to fill this view."}
      <div><Link className="btn" to="/?step=3">{busy ? "See its progress" : "Go to New scan"} <ArrowRight size={16} /></Link></div>
    </div>
  );
}
