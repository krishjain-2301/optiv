// App state: the current run, the scan in progress, the scan settings and queued files.
// The run is fetched once and every page reads it from here, so moving between pages and steps
// never waits on the server.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  api, IDLE, type DocDetail, type Evaluation, type Meta, type ReviewAddition, type ReviewDecision, type Run, type ScanSettings,
  type ScanStatus,
} from "./api";

export const DEFAULT_SETTINGS: ScanSettings = {
  ocr_engine: "auto", ocr_embedded_images: true, use_gliner: false, propagate_persons: true,
  redact_threshold: 0.6, review_threshold: 0.35, low_conf_ocr: 0.6,
  extra_allow_list: [], deny_list: [], confidential_terms: [], vault_passphrase: null,
  profile: "default", verify_outputs: true, blank_textless_images: true, detect_faces: true, token_key: null, operator: null,
};
const NO_META: Meta = {
  default_operator: "", profiles: [], entities: [], max_upload_mb: 300, markings: [], org_terms: 0,
  guard_policy: { source_code: "block", markings: "block", block_categories: [], max_bytes: 0, protected: "block" },
};
/** Secrets are typed in for one session and never written to the browser's storage. */
const SECRETS = { vault_passphrase: null, token_key: null };
const STORAGE_KEY = "pii-shield-settings";

function loadSettings(): ScanSettings {
  try {
    return { ...DEFAULT_SETTINGS, ...JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}"), ...SECRETS };
  } catch {
    return DEFAULT_SETTINGS;
  }
}

export const isActive = (s: ScanStatus) => s.state === "running" || s.state === "paused";

interface Store {
  ready: boolean;
  run: Run | null;
  scan: ScanStatus;
  meta: Meta;
  settings: ScanSettings;
  setSettings: (patch: Partial<ScanSettings>) => void;
  files: File[];
  setFiles: (files: File[]) => void;
  gold: File | null;
  setGold: (gold: File | null) => void;
  startError: string | null;
  start: (synthetic: boolean) => Promise<void>;
  pause: () => Promise<void>;
  resume: () => Promise<void>;
  cancel: () => Promise<void>;
  /** Apply a reviewer's decisions; the server writes every output again as a background job. */
  submitReview: (decisions: ReviewDecision[], additions: ReviewAddition[]) => Promise<void>;
  refreshRun: () => Promise<void>;
  deleteSession: () => Promise<void>;
  loadDoc: (file: string) => Promise<DocDetail>;
}

const Ctx = createContext<Store | null>(null);

export function StoreProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const [run, setRun] = useState<Run | null>(null);
  const [scan, setScan] = useState<ScanStatus>(IDLE);
  const [meta, setMeta] = useState<Meta>(NO_META);
  const [settings, setAll] = useState<ScanSettings>(loadSettings);
  const [files, setFiles] = useState<File[]>([]);
  const [gold, setGold] = useState<File | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const docs = useRef(new Map<string, Promise<DocDetail>>());

  const refreshRun = useCallback(async () => {
    docs.current.clear();
    setRun(await api.run());
  }, []);

  useEffect(() => {
    Promise.all([api.scan(), api.run(), api.meta()])
      .then(([s, r, m]) => {
        setScan(s);
        setRun(r);
        setMeta(m);
      })
      .finally(() => setReady(true));
  }, []);

  // While a scan is running, ask the server where it is. The scan itself runs server-side.
  const active = isActive(scan);
  useEffect(() => {
    if (!active) return;
    const id = setInterval(async () => {
      try {
        const s = await api.scan();
        if (s.state === "done") await refreshRun();
        else if (s.state === "cancelled" || s.state === "failed") setRun(await api.run()); // a stopped review deletes the run
        setScan(s);
      } catch {
        /* server restarting: the next tick tries again */
      }
    }, 500);
    return () => clearInterval(id);
  }, [active, refreshRun]);

  const setSettings = useCallback((patch: Partial<ScanSettings>) => {
    setAll((old) => {
      const next = { ...old, ...patch };
      next.review_threshold = Math.min(next.review_threshold, next.redact_threshold);
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...next, ...SECRETS }));
      return next;
    });
  }, []);

  const start = useCallback(async (synthetic: boolean) => {
    setStartError(null);
    try {
      const s = await api.start(settings, synthetic ? null : files, synthetic ? null : gold);
      docs.current.clear();
      setRun(null); // a new scan deletes the previous run's files
      setScan(s);
    } catch (e) {
      setStartError((e as Error).message);
    }
  }, [settings, files, gold]);

  const submitReview = useCallback(async (decisions: ReviewDecision[], additions: ReviewAddition[]) => {
    const s = await api.review(decisions, additions, settings.operator);
    docs.current.clear();
    setScan({ ...IDLE, ...s }); // the run stays on screen while its outputs are rewritten
  }, [settings.operator]);

  const control = (call: () => Promise<ScanStatus>) => async () => {
    try {
      setScan(await call());
    } catch {
      setScan(await api.scan()); // it finished between the click and the request
    }
  };

  const deleteSession = useCallback(async () => {
    await api.deleteSession();
    docs.current.clear();
    setRun(null);
    setScan(IDLE);
  }, []);

  const loadDoc = useCallback((file: string) => {
    let p = docs.current.get(file);
    if (!p) {
      p = api.doc(file);
      docs.current.set(file, p);
      p.catch(() => docs.current.delete(file));
    }
    return p;
  }, []);

  const value = useMemo<Store>(() => ({
    ready, run, scan, meta, settings, setSettings, files, setFiles, gold, setGold, startError, start,
    pause: control(api.pause), resume: control(api.resume), cancel: control(api.cancel),
    submitReview, refreshRun, deleteSession, loadDoc,
  }), [ready, run, scan, meta, settings, setSettings, files, gold, startError, start, submitReview, refreshRun, deleteSession, loadDoc]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useStore(): Store {
  const s = useContext(Ctx);
  if (!s) throw new Error("useStore outside StoreProvider");
  return s;
}

/** One document's detail (text, spans, boxes), fetched on first use and kept for the run. */
export function useDoc(file: string | undefined) {
  const { loadDoc, run } = useStore();
  const [state, setState] = useState<{ doc: DocDetail | null; error: string | null }>({ doc: null, error: null });
  useEffect(() => {
    if (!file) return;
    let live = true;
    setState({ doc: null, error: null });
    loadDoc(file)
      .then((doc) => live && setState({ doc, error: null }))
      .catch((e: Error) => live && setState({ doc: null, error: e.message }));
    return () => {
      live = false;
    };
  }, [file, loadDoc, run?.run_id, run?.reviews.length]);
  return state;
}

export function useEvaluation() {
  const { run } = useStore();
  const [ev, setEv] = useState<Evaluation | null>(null);
  const reload = useCallback(() => api.evaluation().then(setEv).catch(() => setEv(null)), []);
  useEffect(() => {
    if (run) reload();
  }, [run?.run_id, run?.reviews.length, reload]); // eslint-disable-line react-hooks/exhaustive-deps
  return { ev, setEv };
}
