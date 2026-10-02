// Charts in the dashboard's style: thin marks, the value at every bar tip, a legend whenever
// there is more than one series, a tooltip on every mark. Built from plain HTML/SVG, no chart
// library, so they render offline and take the theme from CSS.
import { ACCENT, RAMP } from "../lib/entities";
import { int, percent } from "../lib/format";
import { tip } from "./tooltip";
import { Legend } from "./ui";

export interface BarRow {
  label: string;
  value: number;
  color?: string;
  /** text at the bar tip; the formatted value when omitted */
  tipLabel?: string;
  /** extra tooltip rows */
  more?: [string, string][];
}

/** Horizontal bars, one per category, value at the tip. */
export function HBar({ rows, max, format = int, unit = "Value", legend }: {
  rows: BarRow[]; max?: number; format?: (n: number) => string; unit?: string; legend?: Record<string, string>;
}) {
  const top = max ?? Math.max(...rows.map((r) => r.value), 0) ?? 1;
  return (
    <>
      <div className="hbar">
        {rows.map((r) => (
          <div className="hbar-row" key={r.label} {...tip(r.label, [[unit, format(r.value)], ...(r.more ?? [])])}>
            <span className="hbar-label" title={r.label}>{r.label}</span>
            <span className="hbar-track">
              <i style={{ width: `calc(${top ? r.value / top : 0} * (100% - 7.5rem))`, background: r.color ?? ACCENT }} />
              <em>{r.tipLabel ?? format(r.value)}</em>
            </span>
          </div>
        ))}
      </div>
      {legend && <Legend items={legend} />}
    </>
  );
}

/** Horizontal stacked bars (part-to-whole per category) with the total at the tip. */
export function StackedBar({ rows, colors, unit = "Value" }: {
  rows: { label: string; parts: Record<string, number> }[]; colors: Record<string, string>; unit?: string;
}) {
  const total = (r: { parts: Record<string, number> }) => Object.values(r.parts).reduce((a, b) => a + b, 0);
  const top = Math.max(...rows.map(total), 1);
  return (
    <>
      <div className="hbar">
        {[...rows].sort((a, b) => total(b) - total(a)).map((r) => (
          <div className="hbar-row" key={r.label}>
            <span className="hbar-label" title={r.label}>{r.label}</span>
            <span className="hbar-track">
              <span className="stack" style={{ width: `calc(${total(r) / top} * (100% - 4rem))` }}>
                {Object.keys(colors).filter((k) => r.parts[k]).map((k) => (
                  <i key={k} style={{ flex: r.parts[k], background: colors[k] }}
                    {...tip(r.label, [[k, int(r.parts[k])], [`Total ${unit.toLowerCase()}`, int(total(r))]])} />
                ))}
              </span>
              <em>{int(total(r))}</em>
            </span>
          </div>
        ))}
      </div>
      <Legend items={colors} />
    </>
  );
}

/** Part-to-whole for a handful of groups: the ring with its total, and beside it the legend as a
 *  list with every value and share, so nothing is readable by colour or hover alone. */
export function Donut({ rows, unit }: { rows: { label: string; value: number; color: string }[]; unit: string }) {
  const data = rows.filter((r) => r.value > 0);
  const total = data.reduce((a, r) => a + r.value, 0);
  const R = 70, C = 2 * Math.PI * R, GAP = data.length > 1 ? 3 : 0;
  let at = 0;
  return (
    <div className="donut">
      <svg viewBox="0 0 180 180" role="img" aria-label={`${int(total)} ${unit} by group`}>
        <g transform="rotate(-90 90 90)">
          {data.map((r) => {
            const len = (r.value / total) * C;
            const el = (
              <circle key={r.label} cx="90" cy="90" r={R} fill="none" stroke={r.color} strokeWidth="26"
                strokeDasharray={`${Math.max(len - GAP, 0.5)} ${C}`} strokeDashoffset={-at}
                {...tip(r.label, [[unit, int(r.value)], ["Share", percent(r.value / total)]])} />
            );
            at += len;
            return el;
          })}
        </g>
        <text x="90" y="88" className="donut-total">{int(total)}</text>
        <text x="90" y="108" className="donut-unit">{unit}</text>
      </svg>
      <div className="shares">
        {data.map((r) => (
          <div className="share" key={r.label}>
            <i style={{ background: r.color }} />
            <span>{r.label}</span>
            <b>{int(r.value)}</b>
            <em>{percent(r.value / total)}</em>
          </div>
        ))}
      </div>
    </div>
  );
}

const hex = (c: string) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));

/** Position `t` (0-1) on the one-hue ramp. */
function ramp(t: number): string {
  const x = Math.max(0, Math.min(t, 1)) * (RAMP.length - 1);
  const i = Math.min(Math.floor(x), RAMP.length - 2);
  const [a, b] = [hex(RAMP[i]), hex(RAMP[i + 1])];
  return `rgb(${a.map((v, k) => Math.round(v + (b[k] - v) * (x - i))).join(",")})`;
}

/** Magnitude on a grid: one hue, brighter = more. Values are printed while the grid is small. */
export function Heatmap({ rows, cols, value, colTitle, unit }: {
  rows: string[]; cols: string[]; value: (row: string, col: string) => number | undefined; colTitle: string; unit: string;
}) {
  const all = rows.flatMap((r) => cols.map((c) => value(r, c) ?? 0));
  const top = Math.max(...all, 1);
  const labelled = rows.length * cols.length <= 150;
  return (
    <div className="heat-wrap">
      <div className="heat" style={{ gridTemplateColumns: `minmax(8rem, max-content) repeat(${cols.length}, minmax(1.6rem, 1fr))` }}>
        {rows.map((r) => (
          <div className="heat-line" key={r}>
            <span className="heat-label" title={r}>{r}</span>
            {cols.map((c) => {
              const v = value(r, c);
              if (v === undefined) return <span key={c} className="heat-cell blank" />;
              const t = v / top;
              return (
                <span key={c} className="heat-cell" style={{ background: ramp(t), color: t > 0.72 ? "#0b0e14" : "#e6eaf2" }}
                  {...tip(r, [[unit, int(v)], [colTitle, c]])}>
                  {labelled ? int(v) : ""}
                </span>
              );
            })}
          </div>
        ))}
        <div className="heat-line">
          <span />
          {cols.map((c) => <span key={c} className="heat-col">{c}</span>)}
        </div>
      </div>
      <div className="heat-scale">
        <span>0</span>
        <i style={{ background: `linear-gradient(90deg, ${RAMP.join(",")})` }} />
        <span>{int(top)} {unit.toLowerCase()}</span>
        <span className="heat-axis">{colTitle}</span>
      </div>
    </div>
  );
}

/** The next round number at or above n (1, 1.5, 2, 3 … × a power of ten), for an axis top. */
function niceCeil(n: number): number {
  const p = 10 ** Math.floor(Math.log10(n));
  return [1, 1.5, 2, 3, 4, 5, 6, 8, 10].map((m) => m * p).find((v) => v >= n)!;
}

/** Stacked distribution of scores in 0.05 bins, with labelled vertical rules at `marks`. */
export function Histogram({ values, colors, marks, xTitle, unit }: {
  values: { value: number; series: string }[]; colors: Record<string, string>; marks: Record<string, number>;
  xTitle: string; unit: string;
}) {
  const BINS = 20;
  const bins: Record<string, number>[] = Array.from({ length: BINS }, () => ({}));
  for (const v of values) {
    const b = bins[Math.min(Math.floor(v.value * BINS + 1e-9), BINS - 1)];
    b[v.series] = (b[v.series] ?? 0) + 1;
  }
  const total = (b: Record<string, number>) => Object.values(b).reduce((a, x) => a + x, 0);
  const top = niceCeil(Math.max(...bins.map(total), 1));
  return (
    <>
      <div className="histo">
        <div className="histo-y"><span>{int(top)}</span><span>{int(top / 2)}</span><span>0</span></div>
        <div className="histo-plot">
          {bins.map((b, i) => (
            <div className="histo-bin" key={i}
              {...tip(`${(i / BINS).toFixed(2)} – ${((i + 1) / BINS).toFixed(2)}`,
                [...Object.keys(colors).filter((k) => b[k]).map((k) => [k, int(b[k])] as [string, string]), [`Total ${unit.toLowerCase()}`, int(total(b))]])}>
              {Object.keys(colors).filter((k) => b[k]).map((k) => (
                <i key={k} style={{ height: `${(b[k] / top) * 100}%`, background: colors[k] }} />
              ))}
            </div>
          ))}
          {Object.entries(marks).map(([name, x]) => (
            <div className="histo-mark" key={name} style={{ left: `${x * 100}%` }}><span>{name} ≥ {x.toFixed(2)}</span></div>
          ))}
        </div>
        <div className="histo-x">
          {[0, 0.2, 0.4, 0.6, 0.8, 1].map((x) => <span key={x} style={{ left: `${x * 100}%` }}>{x.toFixed(1)}</span>)}
        </div>
        <div className="histo-title">{xTitle}</div>
      </div>
      <Legend items={colors} />
    </>
  );
}

export function Meter({ value, max = 1, label }: { value: number; max?: number; label: string }) {
  return (
    <span className="cell-meter">
      <span className="meter"><i style={{ width: `${max ? Math.min(value / max, 1) * 100 : 0}%` }} /></span>
      <span>{label}</span>
    </span>
  );
}
