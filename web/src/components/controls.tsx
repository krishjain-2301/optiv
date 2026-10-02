// Form controls: multi-select filter, single select, toggle, slider, Markdown view.
import { Check, ChevronDown } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export interface Option {
  value: string;
  label: string;
}

/** Filter by any of several values. Nothing selected means "all". */
export function MultiSelect({ label, options, selected, onChange, all }: {
  label: string; options: Option[]; selected: string[]; onChange: (values: string[]) => void; all: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  const names = options.filter((o) => selected.includes(o.value)).map((o) => o.label);
  const summary = !names.length ? all : names.length <= 2 ? names.join(", ") : `${names.length} selected`;
  const flip = (v: string) => onChange(selected.includes(v) ? selected.filter((x) => x !== v) : [...selected, v]);
  return (
    <div className="field" ref={ref}>
      <span className="field-label">{label}</span>
      <button className="select" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className={names.length ? "" : "placeholder"}>{summary}</span>
        <ChevronDown size={16} />
      </button>
      {open && (
        <div className="menu" role="listbox" aria-multiselectable>
          {names.length > 0 && <button className="menu-clear" onClick={() => onChange([])}>Clear ({all.toLowerCase()})</button>}
          {options.map((o) => {
            const on = selected.includes(o.value);
            return (
              <button key={o.value} role="option" aria-selected={on} className="menu-item" onClick={() => flip(o.value)}>
                <span className={on ? "box on" : "box"}>{on && <Check size={12} />}</span>
                {o.label}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function Select({ label, value, options, onChange, width }: {
  label: string; value: string; options: Option[]; onChange: (v: string) => void; width?: string;
}) {
  return (
    <label className="field" style={{ maxWidth: width }}>
      <span className="field-label">{label}</span>
      <select className="select" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  );
}

export function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="toggle">
      <input type="checkbox" role="switch" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="switch" />
      {label}
    </label>
  );
}

export function Slider({ label, value, min, max, step = 0.05, onChange }: {
  label: string; value: number; min: number; max: number; step?: number; onChange: (v: number) => void;
}) {
  return (
    <label className="field">
      <span className="field-label">{label}<b>{value.toFixed(2)}</b></span>
      <input type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} />
    </label>
  );
}

/** Extracted Markdown. Page / slide markers are HTML comments in the source; shown as small labels.
 *  Raw HTML in the document is not rendered. */
export function Markdown({ text }: { text: string }) {
  const readable = text.replace(/<!--\s*(.+?)\s*-->/g, "\n\n`$1`\n\n");
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{readable}</ReactMarkdown></div>;
}
