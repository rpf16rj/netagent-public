// Cliente REST minimo + helpers de formatacao

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path)
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`)
  return r.json() as Promise<T>
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status} ${await r.text()}`)
  return r.json() as Promise<T>
}

export const api = {
  snapshot: () => get<import('./types').Snapshot>('/api/snapshot'),
  events: (limit = 100) =>
    get<{ events: import('./types').EventItem[] }>(`/api/events?limit=${limit}`),
  history: (hours = 1) =>
    get<import('./types').HistoryResp>(`/api/history?hours=${hours}`),
  devices: (router?: string, newDays = 7) =>
    get<import('./types').DevicesResp>(
      `/api/devices?new_days=${newDays}` +
        (router ? `&router=${encodeURIComponent(router)}` : ''),
    ),
  devicesStats: (newDays = 7) =>
    get<import('./types').DevicesStatsResp>(
      `/api/devices/stats?new_days=${newDays}`,
    ),
  baseline: () => get<import('./types').BaselineResp>('/api/baseline'),
  uptime: (days = 7) =>
    get<import('./types').UptimeResp>(`/api/uptime?days=${days}`),
  healthscore: () => get<import('./types').HealthResp>('/api/healthscore'),
  heatmap: (days = 7) =>
    get<import('./types').HeatmapResp>(`/api/heatmap?days=${days}`),
  talkersSpark: (ips: string[], hours = 1) =>
    get<import('./types').TalkersSparkResp>(
      `/api/talkers/spark?ips=${ips.map(encodeURIComponent).join(',')}` +
        `&hours=${hours}`,
    ),
  wifi: () => get<import('./types').WifiResp>('/api/wifi'),
  speedtest: () =>
    get<{
      running: boolean
      last_error: string | null
      results: {
        id: number
        ts: number
        dl_bps: number | null
        ul_bps: number | null
        ping_ms: number | null
        server: string
        trigger: string
      }[]
    }>('/api/speedtest'),
  speedtestRun: () =>
    post<{ ok: boolean; note?: string }>('/api/speedtest/run'),
  adguard: () => get<import('./types').AdguardResp>('/api/adguard'),
  talkersTotal: (hours = 24, router?: string) =>
    get<{ talkers: import('./types').TalkerTotalRow[] }>(
      `/api/talkers/total?hours=${hours}` +
        (router ? `&router=${encodeURIComponent(router)}` : ''),
    ),
  actions: () =>
    get<{ actions: import('./types').ActionItem[]; read_only: boolean }>(
      '/api/actions',
    ),
  agentStatus: () =>
    get<{
      read_only: boolean
      ntfy_enabled: boolean
      agent_enabled: boolean
      interval_s: number
      model: string
      has_api_key: boolean
      last_run_ts: number | null
      last_error: string | null
    }>('/api/agent/status'),
  agentAsk: (question: string) =>
    post<{ answer: string; ts: number }>('/api/agent/ask', { question }),
  approve: (id: number) =>
    post<import('./types').ActionItem>(`/api/actions/${id}/approve`),
  reject: (id: number) =>
    post<import('./types').ActionItem>(`/api/actions/${id}/reject`),
  killswitch: (read_only: boolean) =>
    post<{ read_only: boolean }>('/api/agent/killswitch', { read_only }),
}

// ---------- formatacao ----------

export function fmtBps(bps: number | null | undefined): string {
  if (bps === null || bps === undefined) return '—'
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(2)} Gbps`
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} Mbps`
  if (bps >= 1e3) return `${(bps / 1e3).toFixed(0)} kbps`
  return `${bps.toFixed(0)} bps`
}

export function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`
  return `${(n / 1e3).toFixed(0)} kB`
}

export function fmtRtt(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  return `${ms.toFixed(1)} ms`
}

export function fmtTs(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString('pt-BR')
}
