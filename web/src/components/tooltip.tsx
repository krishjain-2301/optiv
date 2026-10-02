// One tooltip for every chart mark. Marks spread `tip(title, rows)` onto their element; the
// tooltip follows the pointer, and shows on keyboard focus too. Text only: titles and values come
// from uploaded documents.
import { useEffect, useState } from "react";

interface TipState {
  x: number;
  y: number;
  title: string;
  rows: [string, string][];
}

let publish: (t: TipState | null) => void = () => {};

export function TooltipLayer() {
  const [t, setT] = useState<TipState | null>(null);
  useEffect(() => {
    publish = setT;
    return () => {
      publish = () => {};
    };
  }, []);
  if (!t) return null;
  const left = Math.min(t.x + 14, window.innerWidth - 280);
  const top = Math.min(t.y + 16, window.innerHeight - 40 - t.rows.length * 22);
  return (
    <div className="tooltip" style={{ left, top }} role="tooltip">
      <div className="tooltip-title">{t.title}</div>
      {t.rows.map(([k, v]) => (
        <div className="tooltip-row" key={k}><b>{v}</b><span>{k}</span></div>
      ))}
    </div>
  );
}

export function tip(title: string, rows: [string, string][]) {
  return {
    tabIndex: 0,
    onMouseEnter: (e: React.MouseEvent) => publish({ x: e.clientX, y: e.clientY, title, rows }),
    onMouseMove: (e: React.MouseEvent) => publish({ x: e.clientX, y: e.clientY, title, rows }),
    onMouseLeave: () => publish(null),
    onFocus: (e: React.FocusEvent) => {
      const r = (e.currentTarget as Element).getBoundingClientRect();
      publish({ x: r.left + r.width / 2, y: r.bottom, title, rows });
    },
    onBlur: () => publish(null),
  };
}
