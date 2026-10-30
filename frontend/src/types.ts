// Tipos espelhando os payloads do backend (ver app/poller.py e app/main.py)

export interface LinkState {
  isp: string
  route_comment: string
  gateway: string
  out_interface?: string
  via_transit?: boolean
  disabled: boolean
  active: boolean
  distance: number
  rtt_ms: number | null
  loss_pct: number | null
}

export interface IfaceStats {
  rx_bytes: number
  tx_bytes: number
  rx_bps: number | null
  tx_bps: number | null
  running: boolean
  type: string
}

export interface SystemInfo {
  cpu_load: number | null
  free_memory: number | null
  total_memory: number | null
  uptime: string
  version: string
  board_name: string
  architecture: string
  conn_count?: number | null
}

export interface Lease {
  address?: string
  'mac-address'?: string
  'host-name'?: string
  comment?: string
  status?: string
  'expires-after'?: string
}

export interface RouterSnap {
  reachable: boolean
  error?: string
  label?: string
  isp?: string
  lan_subnet?: string
  wan_interface?: string
  system?: SystemInfo
  interfaces?: Record<string, IfaceStats>
  links?: LinkState[]
  leases?: Lease[]
}

export interface Snapshot {
  ts: number
  routers: Record<string, RouterSnap>
}

export interface EventItem {
  id: number
  ts: number
  level: 'info' | 'warning' | 'critical' | string
  source: string
  message: string
}

export interface RollupPoint {
  ts_min: number
  cpu_avg: number | null
  wan_rx_bps_avg: number | null
  wan_tx_bps_avg: number | null
  rtt_ms_avg: number | null
  // link_rx_<i>/link_tx_<i>/link_rtt_<i>/link_up_<i> dinamicos por link
  [k: string]: number | null | undefined
}

export interface HistoryResp {
  ts: number
  hours: number
  series: Record<string, RollupPoint[]>
}

export interface ActionItem {
  id: number
  ts: number
  status: 'proposed' | 'approved' | 'executed' | 'rolledback' | 'failed' | 'rejected'
  kind: string
  router: string
  payload: Record<string, unknown>
  diff: string[][]
  approved_by: string | null
  result: string | null
  rollback: string[][]
}

// ---------- devices (tracking via DHCP leases) ----------

export interface DeviceItem {
  mac: string
  ip: string
  name: string
  router: string
  first_seen: number
  last_seen: number
  is_new: boolean
}

export interface DevicesResp {
  ts: number
  new_days: number
  count: number
  devices: DeviceItem[]
}

export interface DevicesStatsResp {
  ts: number
  total: number
  new: number
  seen_24h: number
  // contagens agregadas por roteador (devices.router)
  by_router: Record<string, number>
}

// ---------- baselines 7d ----------

export interface BaselineMetric {
  computed_ts: number
  median: number | null
  p95: number | null
  avg: number | null
  n: number
}

export interface BaselineResp {
  ts: number
  days: number
  // router -> metric (wan_rx_bps_avg|wan_tx_bps_avg|rtt_ms_avg|cpu_avg)
  routers: Record<string, Record<string, BaselineMetric>>
}

// ---------- monitoring v2 ----------

export interface UptimeResp {
  ts: number
  days: number
  // router -> linkIndex -> {uptime 0..1, samples}
  routers: Record<string, Record<string, { uptime: number; samples: number }>>
}

export interface HealthLink {
  index: number
  isp: string | null
  score: number
  penalties: string[]
}

export interface HealthResp {
  ts: number
  global: number
  routers: Record<string, { score: number; links: HealthLink[] }>
}

export interface HeatmapCell {
  day: string // 'YYYY-MM-DD'
  hour: number // 0-23
  v: number // wan_rx_bps_avg medio do bucket
}

export interface HeatmapResp {
  ts: number
  days: number
  routers: Record<string, HeatmapCell[]>
}

export interface TalkerSparkPoint {
  t: number // epoch s (ts_min)
  rx: number // bps
  tx: number // bps
}

export interface TalkersSparkResp {
  ts: number
  hours: number
  series: Record<string, TalkerSparkPoint[]>
}

// ---------- wifi (Tenda RX2 Pro) ----------

export interface WifiClient {
  mac: string
  ip: string
  hostname: string
  band: string
  signal: string | number | null
  rssi: string | number | null
  uptime?: string
}

export interface WifiMeta {
  ssid?: string
  ssid_5?: string
  channel_24?: string | number
  channel_5?: string | number
  [k: string]: unknown
}

export interface WifiResp {
  ts: number
  reachable: boolean
  error?: string
  note?: string
  clients: WifiClient[]
  meta: WifiMeta
}

export interface AdguardResp {
  ts: number
  reachable: boolean
  error?: string
  note?: string
  version?: string
  protection_enabled?: boolean
  queries_24h?: number
  blocked_24h?: number
  blocked_pct?: number
  avg_ms?: number
  top_domains?: Record<string, number>[]
  top_blocked?: Record<string, number>[]
  top_clients?: Record<string, number>[]
}

export interface TalkerTotalRow {
  ip: string
  router: string
  rx_bytes: number
  tx_bytes: number
  mins: number
}
