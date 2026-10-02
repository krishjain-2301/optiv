export const int = (n: number) => Math.round(n).toLocaleString("en-US");
export const num = (n: number, digits = 1) =>
  n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
export const pct = (part: number, whole: number, digits = 0) =>
  whole ? `${((part / whole) * 100).toFixed(digits)}%` : "–";
export const percent = (fraction: number, digits = 0) => `${(fraction * 100).toFixed(digits)}%`;

export function clock(seconds: number): string {
  const s = Math.floor(seconds);
  return s >= 60 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`;
}

export function fileSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${num(bytes / 1024 / 1024)} MB`;
  return `${num(bytes / 1024)} KB`;
}

export function countBy<T>(rows: T[], key: (row: T) => string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const r of rows) {
    const k = key(r);
    out[k] = (out[k] ?? 0) + 1;
  }
  return out;
}

export const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0);

export function median(xs: number[]): number | null {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

/** Sort a page key: numbers first in order, then "document". */
export const pageOrder = (a: string, b: string) =>
  (a === "document" ? Infinity : Number(a)) - (b === "document" ? Infinity : Number(b));
