import { useEffect, useState } from 'react'
import { api, fmtBps, fmtRtt } from '../api'
import type { BaselineMetric, LinkState, RouterSnap } from '../types'
import type { TimedSnapshot } from '../useLiveHistory'
import { useHistorySeries } from '../useHistorySeries'
import LiveAreaChart from './LiveAreaChart'
import Sparkline from './Sparkline'

export interface LinkEntry {
  routerName: string
  router: RouterSnap
  link: LinkState | null // null = roteador sem rota comentada / offline
  linkIndex: number // -1 quando link === null
}

// Card HUD de UM link WAN: estado grande (ATIVO/STANDBY/DOWN), throughput
// atual, grafico de area e sparkline de RTT semeados pelos rollups de 1min
// do periodo selecionado (fallback p/ janela ao vivo ~60s quando o backend
// ainda nao tem rollups — fresh boot).
export default function LinkCard({
  entry,
  history,
  baseline,
  baselineRtt,
  hours,
}: {
  entry: LinkEntry
  history: TimedSnapshot[]
  baseline?: BaselineMetric | null // baseline 7d do wan_rx do roteador
  baselineRtt?: BaselineMetric | null // baseline 7d do rtt do roteador
  hours: number
}) {
  const { routerName, router, link, linkIndex } = entry
  const iface = link?.out_interface || router.wan_interface || ''
  const stats = router.interfaces?.[iface]

  // uptime por link (rollups link_up_<i>) — 24h e 7d, refresh de 60s
  const [uptime, setUptime] = useState<{
    d1: number | null
    d7: number | null
  }>({ d1: null, d7: null })
  useEffect(() => {
    if (linkIndex < 0) return
    let off = false
    const load = () =>
      Promise.all([api.uptime(1), api.uptime(7)])
        .then(([u1, u7]) => {
          if (off) return
          setUptime({
            d1: u1.routers?.[routerName]?.[String(linkIndex)]?.uptime ?? null,
            d7: u7.routers?.[routerName]?.[String(linkIndex)]?.uptime ?? null,
          })
        })
        .catch(() => {})
    load()
    const t = setInterval(load, 60000)
    return () => {
      off = true
      clearInterval(t)
    }
  }, [routerName, linkIndex])

  // rollups de 1min do periodo — semeiam os graficos no mount/remount
  const { points: hist } = useHistorySeries(routerName, hours)

  // ~60s de janela ao vivo (20 amostras x ~3s de poll) — so fallback
  const win = history.slice(-20)
  const liveRx = win.map(
    (h) => h.snap.routers[routerName]?.interfaces?.[iface]?.rx_bps ?? null,
  )
  const liveTx = win.map(
    (h) => h.snap.routers[routerName]?.interfaces?.[iface]?.tx_bps ?? null,
  )
  const liveRtt = win.map((h) =>
    linkIndex >= 0
      ? h.snap.routers[routerName]?.links?.[linkIndex]?.rtt_ms ?? null
      : null,
  )

  // serie do link especifico (links_rates do rollup); sem ela cai na
  // WAN total (comportamento antigo — links replicavam o mesmo dado)
  const li = linkIndex >= 0 ? String(linkIndex) : null
  const hasLink = !!li && hist.some((p) => p.linkRx?.[li] != null)
  const rx = hasLink
    ? hist.map((p) => p.linkRx?.[li] ?? null)
    : hist.length ? hist.map((p) => p.wanRx) : liveRx
  const tx = hasLink
    ? hist.map((p) => p.linkTx?.[li] ?? null)
    : hist.length ? hist.map((p) => p.wanTx) : liveTx
  const rtt = hasLink
    ? hist.map((p) => p.linkRtt?.[li] ?? null)
    : hist.length ? hist.map((p) => p.rtt) : liveRtt
  const period = hours >= 24 ? `${Math.round(hours / 24)}d` : `${hours}h`

  const state = !router.reachable
    ? 'down'
    : !link || link.disabled
      ? 'down'
      : link.active
        ? 'ativo'
        : 'standby'
  const stateLabel =
    state === 'ativo'
      ? 'ATIVO'
      : state === 'standby'
        ? 'STANDBY'
        : router.reachable && link
          ? 'DOWN'
          : 'OFFLINE'

  return (
    <div className={`card link-card ${router.reachable ? '' : 'card-down'}`}>
      <div className="card-title">
        <span>{link?.isp ?? router.isp ?? router.label ?? routerName}</span>
        <span className="title-sub">{router.label ?? routerName}</span>
      </div>
      <div className="card-body link-body">
        <div className={`link-state state-${state}`}>
          <span className="st-dot" />
          {stateLabel}
        </div>
        <div className="link-meta">
          {link
            ? `gw ${link.gateway || '—'} · dist ${link.distance}` +
              (link.via_transit ? ' · via fibra' : '') +
              ` · ${iface || 'wan'}`
            : (router.error ?? 'sem rota comentada (LINK …)')}
        </div>
        <div className="tp-row">
          <div className="tp-item rx">
            <span className="hud-label">▼ download</span>
            <span className="big-num">{fmtBps(stats?.rx_bps)}</span>
          </div>
          <div className="tp-item tx">
            <span className="hud-label">▲ upload</span>
            <span className="big-num">{fmtBps(stats?.tx_bps)}</span>
          </div>
          <div className="tp-item rtt">
            <span className="hud-label">rtt</span>
            <span className="big-num">{fmtRtt(link?.rtt_ms)}</span>
            <span className="muted mini">
              perda {link?.loss_pct == null ? '—' : `${link.loss_pct}%`}
            </span>
          </div>
        </div>
        <div className="chart-fill">
          <LiveAreaChart rx={rx} tx={tx} />
        </div>
        <div className="link-foot">
          <span className="hud-label">rtt · {period}</span>
          <div className="spark-wrap">
            <Sparkline
              points={rtt}
              color="var(--warn)"
              baseline={baselineRtt?.median ?? null}
            />
          </div>
          <span className="muted mini baseline-note">
            {baseline?.median != null
              ? `bl7d ${fmtBps(baseline.median)} · p95 ${fmtBps(
                  baseline.p95,
                )}`
              : ''}
          </span>
        </div>
        {(uptime.d1 != null || uptime.d7 != null) && (
          <div className="link-foot">
            <span className="hud-label">uptime</span>
            <span className="muted mini">
              24h {uptime.d1 != null ? `${(uptime.d1 * 100).toFixed(1)}%` : '—'}
              {' · '}7d {uptime.d7 != null ? `${(uptime.d7 * 100).toFixed(1)}%` : '—'}
            </span>
          </div>
        )}
      </div>
    </div>
  )
}
