import { useEffect, useMemo, useState } from 'react'
import { api, fmtBps } from '../api'
import type { HeatmapResp } from '../types'

const DAYS = 7
const HOURS = 24

// Heatmap dia x hora do download WAN (wan_rx_bps_avg dos rollups 1min) —
// 7 linhas (hoje no topo) x 24 celulas por hora, intensidade = v/max do
// periodo. /api/heatmap devolve todos os roteadores; aqui filtramos o nosso.
export default function HeatmapPanel({ routerName }: { routerName: string }) {
  const [data, setData] = useState<HeatmapResp | null>(null)

  useEffect(() => {
    const load = () =>
      api
        .heatmap(DAYS)
        .then(setData)
        .catch(() => {})
    load()
    const t = setInterval(load, 60000)
    return () => clearInterval(t)
  }, [])

  // "YYYY-MM-DD|h" -> v
  const cells = useMemo(() => {
    const m = new Map<string, number>()
    for (const c of data?.routers?.[routerName] ?? [])
      m.set(`${c.day}|${c.hour}`, c.v)
    return m
  }, [data, routerName])
  const max = Math.max(1e-9, ...cells.values())

  // ultimos 7 dias em horario local — mais novo primeiro (linha de cima)
  const days = useMemo(() => {
    const out: { key: string; label: string }[] = []
    const d = new Date()
    d.setHours(0, 0, 0, 0)
    for (let i = 0; i < DAYS; i++) {
      const key =
        `${d.getFullYear()}-` +
        `${String(d.getMonth() + 1).padStart(2, '0')}-` +
        String(d.getDate()).padStart(2, '0')
      out.push({
        key,
        label:
          `${String(d.getDate()).padStart(2, '0')}/` +
          String(d.getMonth() + 1).padStart(2, '0'),
      })
      d.setDate(d.getDate() - 1)
    }
    return out
  }, [])

  return (
    <div className="card">
      <div className="card-title">
        <span>mapa de calor · download</span>
        <span className="title-sub">7d · máx {fmtBps(max)}</span>
      </div>
      <div className="card-body">
        <div className="hm-grid">
          {days.map((d) => (
            <div className="hm-row" key={d.key}>
              <span className="hm-day">{d.label}</span>
              {Array.from({ length: HOURS }, (_, h) => {
                const v = cells.get(`${d.key}|${h}`)
                const pct = v == null ? 0 : Math.min(1, v / max)
                return (
                  <span
                    key={h}
                    className="hm-cell"
                    title={
                      `${d.key} ${String(h).padStart(2, '0')}h · ` +
                      (v == null ? 'sem dados' : fmtBps(v))
                    }
                    style={
                      v == null
                        ? undefined
                        : {
                            background: `rgba(127, 212, 255, ${(
                              0.08 +
                              0.85 * pct
                            ).toFixed(3)})`,
                          }
                    }
                  />
                )
              })}
            </div>
          ))}
        </div>
        <div className="hm-hours">
          <span />
          {Array.from({ length: HOURS }, (_, h) => (
            <span key={h} className="hm-h">
              {h % 6 === 0 || h === 23 ? String(h).padStart(2, '0') : ''}
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}
