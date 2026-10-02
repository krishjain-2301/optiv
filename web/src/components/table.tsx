// A sortable, paged table. Sorting and paging happen in the browser.
import { ChevronDown, ChevronLeft, ChevronRight, ChevronUp } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { int } from "../lib/format";

export interface Col<T> {
  key: string;
  label: string;
  /** the value sorted on, and shown unless `render` is given */
  value: (row: T) => string | number | null | undefined;
  render?: (row: T) => ReactNode;
  align?: "right";
  /** allow long text to wrap instead of being cut with an ellipsis */
  wrap?: boolean;
  width?: string;
}

export function DataTable<T>({ rows, cols, pageSize = 50, empty = "Nothing to show." }: {
  rows: T[]; cols: Col<T>[]; pageSize?: number; empty?: string;
}) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);
  const [page, setPage] = useState(0);
  useEffect(() => setPage(0), [rows, sort]);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = cols.find((c) => c.key === sort.key);
    if (!col) return rows;
    return [...rows].sort((a, b) => {
      const [x, y] = [col.value(a), col.value(b)];
      if (x == null || y == null) return (x == null ? 1 : 0) - (y == null ? 1 : 0);
      return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * sort.dir;
    });
  }, [rows, cols, sort]);

  if (!rows.length) return <p className="muted">{empty}</p>;
  const pages = Math.ceil(sorted.length / pageSize);
  const shown = sorted.slice(page * pageSize, (page + 1) * pageSize);
  const toggle = (key: string) =>
    setSort((s) => (s?.key !== key ? { key, dir: 1 } : s.dir === 1 ? { key, dir: -1 } : null));

  return (
    <div className="table-wrap">
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {cols.map((c) => (
                <th key={c.key} className={c.align} style={{ width: c.width }}
                  aria-sort={sort?.key === c.key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
                  <button onClick={() => toggle(c.key)}>
                    {c.label}
                    {sort?.key === c.key && (sort.dir === 1 ? <ChevronUp size={13} /> : <ChevronDown size={13} />)}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((r, i) => (
              <tr key={page * pageSize + i}>
                {cols.map((c) => {
                  const v = c.value(r);
                  const text = v == null || v === "" ? "–" : typeof v === "number" ? v.toLocaleString("en-US") : v;
                  return (
                    <td key={c.key} className={`${c.align ?? ""} ${c.wrap ? "wrap" : ""}`} title={c.wrap || c.render ? undefined : String(text)}>
                      {c.render ? c.render(r) : text}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {pages > 1 && (
        <div className="pager">
          <span>{int(page * pageSize + 1)}–{int(Math.min((page + 1) * pageSize, sorted.length))} of {int(sorted.length)}</span>
          <button className="btn icon" disabled={page === 0} onClick={() => setPage(page - 1)} aria-label="Previous page"><ChevronLeft size={16} /></button>
          <button className="btn icon" disabled={page >= pages - 1} onClick={() => setPage(page + 1)} aria-label="Next page"><ChevronRight size={16} /></button>
        </div>
      )}
    </div>
  );
}
