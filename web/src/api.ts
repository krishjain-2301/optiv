// Types of the server's JSON (server/payloads.py) and the calls that fetch it.

export type Decision = "redact" | "review" | "drop";

export interface Finding {
  file: string;
  page: number | null;
  location: string;
  kind: string;
  source: string;
  context_type: string;
  entity_type: string;
  text: string;
  token: string | null;
  score: number;
  decision: Decision;
  layer: string;
  recognizer: string;
  reasons: string[];
}

export interface Exposure {
  score: number;
  distinct_values_score: number;
  per_1k_words: number;
  words: number;
  rating: string;
  by_category: Record<string, number>;
  by_page: Record<string, number>;
  residual: {
    known: {
      values_left_in_outputs: number | null;
      masked_copy: string;
      llm_text_values_caught_by_gate: number;
      masked_values_caught_by_gate: number;
    };
    unreadable: { images_withheld: number; ocr_words_masked: number };
    estimated_missed: {
      instances: number;
      by_category: Record<string, number>;
      score: number;
      per_1k_words: number;
      basis: string;
    };
  };
}

export interface FileSummary {
  file: string;
  type: string;
  pages: number;
  ocr_pages: number[];
  spans: number;
  structure: Record<string, unknown>;
  images: { total: number; by_status: Record<string, number> };
  findings: number;
  redacted: number;
  review_queue: number;
  dropped_candidates: number;
  warnings: string[];
  exposure: Exposure;
  extract_seconds: number | null;
}

export interface OutputFile {
  name: string;
  kind: string;
  size: number;
}

export interface TokenRow {
  token: string;
  entity_type: string;
  occurrences: number;
  files: number;
}

export interface Run {
  run_id: string;
  components: string[];
  timings: Record<string, number>;
  errors: Record<string, string>;
  thresholds: { redact: number; review: number };
  has_gold: boolean;
  files: FileSummary[];
  findings: Finding[];
  dropped: Finding[];
  tokens: TokenRow[];
  pipeline: {
    spans: number;
    ocr_pages: number;
    images: number;
    tokens: number;
    people: number;
    caught: number;
    masked_written: number;
    masked_withheld: number;
    outputs: number;
  };
  outputs: { folder: string; vault: boolean; safe: OutputFile[]; sensitive: OutputFile[] };
}

export interface SpanFinding {
  start: number;
  end: number;
  entity_type: string;
  score: number;
  layer: string;
  decision: Decision;
  context_type: string;
}

export interface SpanRow {
  id: string;
  page: number | null;
  location: string;
  kind: string;
  source: string;
  ocr_conf: number | null;
  header: string | null;
  text: string;
}

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
  entity_type: string;
  score: number;
  decision: Decision;
}

export interface DocDetail {
  file: string;
  file_type: string;
  pages: number;
  ocr_pages: number[];
  previewable: boolean;
  markdown: string;
  redacted: string;
  structure: Record<string, unknown>;
  kinds: Record<string, number>;
  sources: Record<string, number>;
  spans: SpanRow[];
  context: (SpanRow & { findings: SpanFinding[] })[];
  boxes: Record<string, Box[]>;
  images: { location: string; page: number | null; status: string; ocr_conf: number | null; width: number; height: number }[];
  warnings: string[];
}

export interface Bucket {
  gold: number;
  recalled: number;
  category_correct: number;
  recall: number;
}

export interface Retention {
  method: string;
  score: number | null;
  detail?: Record<string, { reference: number; extracted: number; retained: number } | number>;
}

export interface Evaluation {
  has_gold: boolean;
  gold_rows?: number;
  retention: Record<string, Retention>;
  scores?: {
    gold_instances: number;
    recall: number;
    category_recall: number;
    precision: number;
    precision_auto_redact: number;
    f1: number;
    live_findings: number;
    recalled: number;
    true_positive_findings: number;
    by_category: Record<string, Bucket>;
    by_context: Record<string, Bucket>;
    by_file: Record<string, Bucket>;
    missed: Record<string, unknown>[];
    false_positives: Record<string, unknown>[];
    leaks: Record<string, unknown>[];
  };
}

export type ScanState = "idle" | "running" | "paused" | "done" | "failed" | "cancelled";

export interface ScanStatus {
  state: ScanState;
  fraction: number;
  message: string;
  error: string | null;
  pause_requested: boolean;
  cancel_requested: boolean;
  stage: number;
  stages: string[];
  elapsed: number;
  files: string[];
}

export interface ScanSettings {
  ocr_engine: "auto" | "rapidocr" | "tesseract";
  ocr_embedded_images: boolean;
  use_gliner: boolean;
  propagate_persons: boolean;
  redact_threshold: number;
  review_threshold: number;
  low_conf_ocr: number;
  extra_allow_list: string[];
  deny_list: string[];
  vault_passphrase: string | null;
}

export const IDLE: ScanStatus = {
  state: "idle", fraction: 0, message: "", error: null, pause_requested: false, cancel_requested: false,
  stage: 0, stages: [], elapsed: 0, files: [],
};

async function problem(r: Response): Promise<Error> {
  let detail = r.statusText;
  try {
    const body = await r.json();
    if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    /* not JSON */
  }
  return new Error(detail);
}

async function get<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw await problem(r);
  return r.json();
}

async function post<T>(url: string, body?: FormData, method = "POST"): Promise<T> {
  const r = await fetch(url, { method, body });
  if (!r.ok) throw await problem(r);
  return r.json();
}

export const api = {
  defaults: () => get<ScanSettings>("/api/settings"),
  scan: async (): Promise<ScanStatus> => ({ ...IDLE, ...(await get<Partial<ScanStatus>>("/api/scan")) }),
  run: async (): Promise<Run | null> => {
    const r = await fetch("/api/run");
    if (r.status === 404) return null;
    if (!r.ok) throw await problem(r);
    return r.json();
  },
  doc: (file: string) => get<DocDetail>(`/api/run/doc?file=${encodeURIComponent(file)}`),
  pageUrl: (file: string, page: number, runId: string) =>
    `/api/run/page?file=${encodeURIComponent(file)}&page=${page}&run=${encodeURIComponent(runId)}`,
  evaluation: () => get<Evaluation>("/api/run/evaluation"),
  outputUrl: (name: string) => `/api/run/output?name=${encodeURIComponent(name)}`,
  start: (settings: ScanSettings, files: File[] | null, gold: File | null) => {
    const form = new FormData();
    form.append("settings", JSON.stringify(settings));
    form.append("synthetic", files === null ? "true" : "false");
    for (const f of files ?? []) form.append("files", f);
    if (gold) form.append("gold", gold);
    return post<ScanStatus>("/api/scan", form);
  },
  pause: () => post<ScanStatus>("/api/scan/pause"),
  resume: () => post<ScanStatus>("/api/scan/resume"),
  cancel: () => post<ScanStatus>("/api/scan/cancel"),
  uploadGold: (gold: File) => {
    const form = new FormData();
    form.append("gold", gold);
    return post<Evaluation>("/api/run/gold", form);
  },
  deleteSession: () => post<{ state: string }>("/api/session", undefined, "DELETE"),
};
