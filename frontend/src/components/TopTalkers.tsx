import { useEffect, useState } from 'react'
import { api, fmtBps, fmtBytes } from '../api'
import type { TalkerSparkPoint } from '../types'
import Sparkline from './Sparkline'

interface TopRow {
  ip: string
  name: string
  rx_bps: number
  tx_bps: number
  total_bps: number
}

interface TotalRow {
  ip: string
  router: string
  rx_bytes: number
  tx_bytes: number
  mins: number
}

interface TopResp {
  ts: number
  window_s: number
  data_records: number
  top: TopRow[]
}

// Top consumidores — modo "taxa" (IPFIX 60s, barras por bps) ou "total"
// (ip_1m acumulado, bytes no periodo). Busca por IP/nome, lista com scroll,
// sparkline de download 1h por linha no modo taxa.
export default function TopTalkers({ subnet }: { subnet?: string }) {
  const [mode, setMode] = useState<'taxa' | 'total'>('taxa')
  const [q, setQ] = useState('')
  const [data, setData] = useState<TopResp | null>(null)
  const [totals, setTotals] = useState<TotalRow[]>([])
  const [sparks, setSparks] = useState<Record<string, TalkerSparkPoint[]>>({})

  useEffect(() => {
    const load = () =>
      fetch('/api/hosts/top')
        .then((r) => r.json())
        .then(setData)
        .catch(() => {})
    load()
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  }, [])

  // modo total: bytes acumulados por IP nas ultimas 24h (ip_1m)
  useEffect(() => {
    if (mode !== 'total') return
    let off = false
    const load = () =>
      api
        .talkersTotal(24)
        .then((r) => {
          if (!off) setTotals(r.talkers ?? [])
        })
        .catch(() => {})
    load()
    const t = setInterval(load, 60000)
    return () => {
      off = true
      clearInterval(t)
    }
  }, [mode])

  const needle = q.trim().toLowerCase()
  const match = (ip: string, name: string) =>
    !needle ||
    ip.includes(needle) ||
    (name || '').toLowerCase().includes(needle)

  const rows = (data?.top ?? []).filter(
    (t) =>
      (!subnet || t.ip.startsWith(subnet)) && match(t.ip, t.name),
  )
  const max = Math.max(1, ...rows.map((r) => r.total_bps))
  const trows = totals.filter(
    (t) => (!subnet || t.ip.startsWith(subnet)) && match(t.ip, ''),
  )
  const tmax = Math.max(
    1,
    ...trows.map((r) => r.rx_bytes + r.tx_bytes),
  )

  const ipsKey = rows.map((r) => r.ip).join(',')
  useEffect(() => {
    if (!ipsKey || mode !== 'taxa') return
    let off = false
    const load = () =>
      api
        .talkersSpark(ipsKey.split(','), 1)
        .then((r) => {
          if (!off) setSparks(r.series ?? {})
        })
        .catch(() => {})
    load()
    const t = setInterval(load, 60000)
    return () => {
      off = true
      clearInterval(t)
    }
  }, [ipsKey, mode])

  return (
    <div className="card">
      <div className="card-title">
        <span>top consumidores</span>
        <div className="period" style={{ marginLeft: 'auto' }}>
          {(['taxa', 'total'] as const).map((m) => (
            <button
              key={m}
              className={`tab-pill ${mode === m ? 'on' : ''}`}
              onClick={() => setMode(m)}
            >
              {m === 'taxa' ? 'taxa' : 'total 24h'}
            </button>
          ))}
        </div>
      </div>
      <input
        className="panel-search"
        placeholder="filtrar ip / nome…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      <div className="card-body clip-fade scroll-y">
        {mode === 'taxa' ? (
          <>
            {rows.length === 0 && (
              <div className="muted small pad-top">
                {data
                  ? `coletor ipfix ativo — ${data.data_records} records.`
                  : 'carregando…'}
              </div>
            )}
            {rows.map((t, i) => (
              <div className="talker-row" key={t.ip}>
                <span className="rank">{String(i + 1).padStart(2, '0')}</span>
                <div className="talker-id">
                  <span className="mono">{t.ip}</span>
                  <span className="name">{t.name || 'desconhecido'}</span>
                </div>
                <div className="talker-bar">
                  <div
                    className="talker-fill"
                    style={{ width: `${(t.total_bps / max) * 100}%` }}
                  />
                </div>
                <div className="talker-spark" title="download 1h">
                  <Sparkline
                    points={(sparks[t.ip] ?? []).map((p) => p.rx)}
                    color="var(--ice)"
                  />
                </div>
                <span className="talker-val">
                  ▼{fmtBps(t.rx_bps)} ▲{fmtBps(t.tx_bps)}
                </span>
              </div>
            ))}
          </>
        ) : (
          <>
            {trows.length === 0 && (
              <div className="muted small pad-top">
                acumulando dados por ip (ip_1m)…
              </div>
            )}
            {trows.map((t, i) => (
              <div className="talker-row" key={t.ip}>
                <span className="rank">{String(i + 1).padStart(2, '0')}</span>
                <div className="talker-id">
                  <span className="mono">{t.ip}</span>
                  <span className="name">{t.mins}min ativo</span>
                </div>
                <div className="talker-bar">
                  <div
                    className="talker-fill"
                    style={{
                      width: `${((t.rx_bytes + t.tx_bytes) / tmax) * 100}%`,
                    }}
                  />
                </div>
                <span className="talker-val">
                  ▼{fmtBytes(t.rx_bytes)} ▲{fmtBytes(t.tx_bytes)}
                </span>
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  )
}
