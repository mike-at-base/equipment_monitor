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

export type EMSummary = {
  station: string;
  em_label: string;
  display_name: string;
  confirmed: boolean;
  availability_pct?: number;
  state_min: Record<string, number>;
};

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
  availability_pct?: number;
  state_min: Record<string, number>;
  cycles: CycleStats;
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
  plc_time?: string;
  recv_time: string;
  skew_ms: number;
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
    get<LineSummary>(`/api/v2/lines/${encodeURIComponent(line)}/summary?window=${win}`),
  intervals: (l: string, s: string, e: string, win: string) =>
    get<Interval[]>(`/api/v2/ems/${l}/${s}/${e}/intervals?window=${win}`),
  steps: (l: string, s: string, e: string, win: string, limit = 500) =>
    get<StepRow[]>(`/api/v2/ems/${l}/${s}/${e}/steps?window=${win}&limit=${limit}`),
  stepsRange: (l: string, s: string, e: string, from: string, to: string) =>
    get<StepRow[]>(`/api/v2/ems/${l}/${s}/${e}/steps?from=${encodeURIComponent(from)}` +
      `&to=${encodeURIComponent(to)}&limit=500`),
  cycles: (l: string, s: string, e: string, win: string) =>
    get<{ stats: CycleStats; cycles: CycleRow[] }>(
      `/api/v2/ems/${l}/${s}/${e}/cycles?window=${win}`),
  throughput: (l: string, s: string, e: string, win: string, bucket: string) =>
    get<{ bucket: string; buckets: ThroughputBucket[] }>(
      `/api/v2/ems/${l}/${s}/${e}/throughput?window=${win}&bucket=${bucket}`),
  downs: (l: string, s: string, e: string, win: string) =>
    get<{ from: string; to: string; episodes: EpisodeRow[]; raw_downs: DownRow[];
          top_reasons: ReasonAgg[]; availability_pct?: number;
          state_min: Record<string, number> }>(
      `/api/v2/ems/${l}/${s}/${e}/downs?window=${win}`),
  debug: (l: string, s: string, e: string, win: string) =>
    get<DebugResp>(`/api/v2/ems/${l}/${s}/${e}/debug?window=${win}`),
  unconfirmed: () => get<Unconfirmed[]>("/api/v2/unconfirmed"),
  emConfig: (l: string, s: string, e: string) =>
    get<EMConfig>(`/api/v2/ems/${l}/${s}/${e}/config`),
  saveEMConfig: (l: string, s: string, e: string,
    body: { display_name: string; confirmed: boolean; sequences: SeqConfig[] }) =>
    put<{ ok: boolean }>(`/api/v2/ems/${l}/${s}/${e}/config`, body),
  deleteEM: (l: string, s: string, e: string) => del(`/api/v2/ems/${l}/${s}/${e}`),
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
  "productive", "standby", "starved", "blocked", "process_wait", "wait",
  "paused", "down", "manual", "offline", "no_data",
];

export const STATE_LABEL: Record<string, string> = {
  productive: "Productive", standby: "Standby", starved: "Starved",
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
