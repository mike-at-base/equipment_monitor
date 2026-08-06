// Typed client for the emhub query API. One rule: no math here — every
// number displayed comes from the API so UI, agents, and CLI agree.

export type LiveEM = {
  line: string;
  station: string;
  em_label: string;
  state: string;
  reason_type?: string;
  reason?: string;
  step?: string;
  since: string;
  last_seen: string;
};

export type LineRollup = {
  name: string;
  em_count: number;
  live_states: Record<string, number> | null;
};

export type CycleStats = {
  count: number;
  interrupted: number;
  avg_ms?: number;
  p10_ms?: number;
  p50_ms?: number;
  p90_ms?: number;
  work_avg_ms?: number;
  exchange_avg_ms?: number;
  per_hour?: number;
};

export type ReasonAgg = {
  reason: string;
  reason_type: string;
  count: number;
  minutes: number;
};

export type FlowAgg = {
  station: string;
  em_label: string;
  state: string;
  minutes: number;
  count: number;
  top_reason: string;
};

export type FlowReasonAgg = {
  reason: string;
  state: string; // starved | blocked
  minutes: number;
  count: number;
};

export type FlowReasonTimeline = {
  bucket: string; // 15m | 30m | 1h | 4h | 1d
  buckets: string[]; // RFC3339 bucket starts
  series: { reason: string; state: string; minutes: number[] }[];
};

export type EMSummary = {
  station: string;
  em_label: string;
  display_name: string;
  confirmed: boolean;
  availability_pct?: number;
  state_min: Record<string, number>;
};

export type Shift = { dow: number; start_min: number; end_min: number };

export type Unconfirmed = {
  line: string;
  station: string;
  em_label: string;
  display_name: string;
  wire_version: number;
};

export type SeqConfig = {
  index: number;
  name: string;
  is_production: boolean;
  cycle_start_step: string;
  cycle_complete_step: string;
  starved_steps: string[];
  blocked_steps: string[];
  nva_steps: string[]; // non-value-added (purge/prime/clean)
};

export type EMConfig = {
  station: string;
  em_label: string;
  line: string;
  display_name: string;
  confirmed: boolean;
  wire_version: number;
  sequences: SeqConfig[];
  observed: { seq_index: number; steps: string[] }[];
};

export type LineSummary = {
  line: string;
  from: string;
  to: string;
  /** Composed (k-of-n) availability over production time. */
  availability_pct?: number;
  /** Simple average of per-EM episode availability (not line reality). */
  em_avg_availability_pct?: number;
  state_min: Record<string, number>;
  /** minutes = composed-down wall clock; count = EM episode count. */
  episodes: { count: number; minutes: number; retries: number; ongoing: number };
  cycles: CycleStats;
  /** Wall-clock composed-down minutes by reason (not EM-summed). */
  top_down_reasons: ReasonAgg[];
  flow_losses: FlowAgg[];
  mode_min: Record<string, number>;
  mttr: {
    downs: number;
    avg_min?: number;
    acked: number;
    response_avg_min?: number;
    repair_avg_min?: number;
  };
  ems: EMSummary[];
};

export type Interval = {
  start_ts: string;
  end_ts: string;
  state: string;
  reason_type?: string;
  reason?: string;
  step_name?: string;
  ack_ts?: string;
};

export type StepRow = {
  start_ts: string;
  end_ts: string;
  seq_index: number;
  step: string;
  description: string;
  duration_ms: number;
  was_faulted: boolean;
  branch_taken: string; // v5: nextStep of the branch the sequencer took
};

export type StepsPage = {
  steps: StepRow[];
  total: number;
  limit: number;
  offset: number;
  next_offset?: number;
};

// Per-step duration distribution over the whole window (server-side).
export type StepStat = {
  seq_index: number;
  step: string;
  description: string;
  count: number;
  faulted: number;
  min_ms: number; p05_ms: number; p25_ms: number; p50_ms: number;
  p75_ms: number; p95_ms: number; max_ms: number; avg_ms: number;
};

export type StepDetail = {
  step: string;
  seq_index: number;
  bucket: string;
  histogram: { lo_ms: number; hi_ms: number; bin_ms: number; bins: number[]; overflow: number };
  drift: { bucket_ts: string; count: number; p25_ms: number; p50_ms: number; p75_ms: number; p95_ms: number }[];
};

export type ThroughputBucket = {
  bucket_ts: string;
  count: number;
};

export type CycleRow = {
  start_ts: string;
  end_ts: string;
  seq_index: number;
  work_ms?: number;
  exchange_ms?: number;
  total_ms: number;
  interrupted: boolean;
};

export type EpisodeRow = {
  station: string;
  em_label: string;
  start_ts: string;
  end_ts: string;
  ongoing?: boolean;
  minutes: number;
  reason_type: string;
  reason: string;
  step_name?: string;
  retries: number;
  raw_down_min: number;
  ack_ts?: string;
  response_min?: number;
  repair_min?: number;
};

export type DownRow = {
  station: string;
  em_label: string;
  start_ts: string;
  end_ts: string;
  minutes: number;
  reason_type: string;
  reason: string;
  step_name?: string;
  ack_ts?: string;
  response_min?: number;
  repair_min?: number;
};

export type BitFlag = { name: string; on: boolean };

export type RawEM = {
  line: string;
  station: string;
  em_label: string;
  msg_type: number;
  seq: number;
  active_sequence: number;
  step: string;
  step_desc: string;
  step_active_ms: number;
  status_bits: number;
  mode_bits: number;
  status: BitFlag[];
  modes: BitFlag[];
  alarm_msg: string;
  interlock_fails: string;
  fault_conds: string;
  waiting_on: string;
  wire_version: number;
  branch_taken: string;
  dwell_reason: string;
  plc_time?: string;
  recv_time: string;
  skew_ms: number;
};

// ── composed (k-of-n) availability ────────────────────────────────────────
// A node is either a leaf ({em} at station scope, {station} at line scope)
// or a group: k = "all" (series) | number (at least k of children).
export type AvailNode = {
  em?: string;
  station?: string;
  k?: "all" | number;
  children?: AvailNode[];
};

export type ComposedResult = {
  pct: number | null;
  up_spans: { start: number; end: number }[]; // epoch ms
  down: { start_ts: string; end_ts: string; causes: string[] }[];
  causes: { name: string; minutes: number }[];
  default_model: boolean;
  model: AvailNode;
  production_min: number;
};

export type AvailModelResp = {
  model: AvailNode | null;
  default_model: AvailNode;
  members: string[];
};

export type ResetEvent = { ts: string; event: string };
export type ModeWindow = { flag: string; start_ts: string; end_ts: string; minutes: number };
export type RawState = {
  start_ts: string; end_ts: string; state: string;
  reason_type: string; reason: string; step_name: string; seconds: number;
};
export type DebugResp = {
  live: RawEM | null;
  resets: ResetEvent[];
  modes: ModeWindow[];
  states: RawState[];
};

// A window is either a named preset ("today", "8h", "prod") or a custom
// absolute range encoded as "custom:<fromISO>|<toISO>". winQuery turns
// either into the query string the API expects — the server has always
// accepted ?from=&to=, so a custom range needs no backend change.
export const CUSTOM_PREFIX = "custom:";

export function isCustomWin(win: string): boolean {
  return win.startsWith(CUSTOM_PREFIX);
}

export function customWin(fromISO: string, toISO: string): string {
  return `${CUSTOM_PREFIX}${fromISO}|${toISO}`;
}

/** Parse a custom window back into its two ISO strings. */
export function parseCustomWin(win: string): { from: string; to: string } | null {
  if (!isCustomWin(win)) return null;
  const [from, to] = win.slice(CUSTOM_PREFIX.length).split("|");
  return from && to ? { from, to } : null;
}

/** Human label for a window — never the raw "custom:<iso>|<iso>" string. */
export function winLabel(win: string): string {
  const c = parseCustomWin(win);
  if (!c) return win === "prod" ? "prod today" : win;
  const f = new Date(c.from), t = new Date(c.to);
  const hm = (d: Date) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const md = (d: Date) => d.toLocaleDateString([], { month: "numeric", day: "numeric" });
  return f.toDateString() === t.toDateString()
    ? `${md(f)} ${hm(f)}–${hm(t)}`
    : `${md(f)} ${hm(f)} – ${md(t)} ${hm(t)}`;
}

export function winQuery(win: string): string {
  const c = parseCustomWin(win);
  if (c) {
    return `from=${encodeURIComponent(c.from)}&to=${encodeURIComponent(c.to)}`;
  }
  return `window=${encodeURIComponent(win)}`;
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function del(path: string): Promise<void> {
  const r = await fetch(path, { method: "DELETE" });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
}

export const api = {
  lines: () => get<LineRollup[]>("/api/v2/lines"),
  live: () => get<LiveEM[]>("/api/v2/live"),
  summary: (line: string, win: string) =>
    get<LineSummary>(`/api/v2/lines/${encodeURIComponent(line)}/summary?${winQuery(win)}`),
  intervals: (l: string, s: string, e: string, win: string) =>
    get<Interval[]>(`/api/v2/ems/${l}/${s}/${e}/intervals?${winQuery(win)}`),
  steps: (l: string, s: string, e: string, win: string, limit = 800, offset = 0) =>
    get<StepsPage>(`/api/v2/ems/${l}/${s}/${e}/steps?${winQuery(win)}&limit=${limit}&offset=${offset}`),
  stepsRange: (l: string, s: string, e: string, from: string, to: string) =>
    get<StepsPage>(`/api/v2/ems/${l}/${s}/${e}/steps?from=${encodeURIComponent(from)}` +
      `&to=${encodeURIComponent(to)}&limit=500&offset=0`),
  stepStats: (l: string, s: string, e: string, win: string) =>
    get<{ from: string; to: string; steps: StepStat[] }>(
      `/api/v2/ems/${l}/${s}/${e}/stepstats?${winQuery(win)}`),
  stepDetail: (l: string, s: string, e: string, win: string, step: string, seq: number) =>
    get<StepDetail>(`/api/v2/ems/${l}/${s}/${e}/stepdetail?${winQuery(win)}` +
      `&step=${encodeURIComponent(step)}&seq=${seq}`),
  cycles: (l: string, s: string, e: string, win: string) =>
    get<{ stats: CycleStats; cycles: CycleRow[] }>(
      `/api/v2/ems/${l}/${s}/${e}/cycles?${winQuery(win)}`),
  throughput: (l: string, s: string, e: string, win: string, bucket: string) =>
    get<{ bucket: string; buckets: ThroughputBucket[] }>(
      `/api/v2/ems/${l}/${s}/${e}/throughput?${winQuery(win)}&bucket=${bucket}`),
  downs: (l: string, s: string, e: string, win: string) =>
    get<{ from: string; to: string; episodes: EpisodeRow[]; raw_downs: DownRow[];
          top_reasons: ReasonAgg[]; flow_reasons: FlowReasonAgg[];
          flow_reasons_timeline: FlowReasonTimeline;
          availability_pct?: number; state_min: Record<string, number> }>(
      `/api/v2/ems/${l}/${s}/${e}/downs?${winQuery(win)}`),
  debug: (l: string, s: string, e: string, win: string) =>
    get<DebugResp>(`/api/v2/ems/${l}/${s}/${e}/debug?${winQuery(win)}`),
  unconfirmed: () => get<Unconfirmed[]>("/api/v2/unconfirmed"),
  emConfig: (l: string, s: string, e: string) =>
    get<EMConfig>(`/api/v2/ems/${l}/${s}/${e}/config`),
  saveEMConfig: (l: string, s: string, e: string,
    body: { display_name: string; confirmed: boolean; sequences: SeqConfig[] }) =>
    put<{ ok: boolean }>(`/api/v2/ems/${l}/${s}/${e}/config`, body),
  deleteEM: (l: string, s: string, e: string) => del(`/api/v2/ems/${l}/${s}/${e}`),
  getSchedule: (line: string) =>
    get<{ line: string; shifts: Shift[] }>(`/api/v2/lines/${encodeURIComponent(line)}/schedule`),
  saveSchedule: (line: string, shifts: Shift[]) =>
    put<{ ok: boolean }>(`/api/v2/lines/${encodeURIComponent(line)}/schedule`, { shifts }),
  lineComposed: (line: string, win: string) =>
    get<{ from: string; to: string; line: string; composed: ComposedResult;
          stations: Record<string, number | null> }>(
      `/api/v2/lines/${encodeURIComponent(line)}/composed?${winQuery(win)}`),
  stationComposed: (line: string, station: string, win: string) =>
    get<{ from: string; to: string; station: string; composed: ComposedResult }>(
      `/api/v2/lines/${encodeURIComponent(line)}/stations/${encodeURIComponent(station)}/composed?${winQuery(win)}`),
  getLineModel: (line: string) =>
    get<AvailModelResp>(`/api/v2/lines/${encodeURIComponent(line)}/availmodel`),
  saveLineModel: (line: string, model: AvailNode | null) =>
    put<{ ok: boolean }>(`/api/v2/lines/${encodeURIComponent(line)}/availmodel`, { model }),
  getStationModel: (line: string, station: string) =>
    get<AvailModelResp>(
      `/api/v2/lines/${encodeURIComponent(line)}/stations/${encodeURIComponent(station)}/availmodel`),
  saveStationModel: (line: string, station: string, model: AvailNode | null) =>
    put<{ ok: boolean }>(
      `/api/v2/lines/${encodeURIComponent(line)}/stations/${encodeURIComponent(station)}/availmodel`,
      { model }),
};

// SSE live stream with polling fallback
export function streamLive(onData: (ems: LiveEM[]) => void, onStatus: (ok: boolean) => void) {
  let es: EventSource | null = null;
  let poll: number | null = null;
  const startPoll = () => {
    if (poll) return;
    poll = window.setInterval(() => {
      api.live().then((d) => { onData(d); onStatus(true); }).catch(() => onStatus(false));
    }, 2000);
  };
  try {
    es = new EventSource("/api/v2/stream");
    es.onmessage = (ev) => { onData(JSON.parse(ev.data)); onStatus(true); };
    es.onerror = () => { onStatus(false); es?.close(); es = null; startPoll(); };
  } catch {
    startPoll();
  }
  api.live().then((d) => { onData(d); onStatus(true); }).catch(() => onStatus(false));
  return () => { es?.close(); if (poll) clearInterval(poll); };
}

export const STATE_ORDER = [
  "productive", "nva", "standby", "starved", "blocked", "process_wait", "wait",
  "paused", "down", "manual", "offline", "no_data",
];

export const STATE_LABEL: Record<string, string> = {
  productive: "Productive", nva: "Non-value-added", standby: "Standby", starved: "Starved",
  blocked: "Blocked", process_wait: "Process wait", wait: "Waiting",
  paused: "Paused", down: "Down", manual: "Manual", offline: "Offline",
  no_data: "No data",
};

export function stateColor(state: string): string {
  return `var(--st-${state || "no_data"})`;
}

export function fmtMs(ms?: number): string {
  if (ms == null) return "–";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

export function fmtSince(iso: string, now: number): string {
  const s = Math.max(0, Math.floor((now - Date.parse(iso)) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${(s % 60).toString().padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export function fmtClock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}
