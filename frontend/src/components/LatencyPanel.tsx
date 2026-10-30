import { fmtRtt } from '../api'
import type { BaselineMetric, RouterSnap } from '../types'
import type { TimedSnapshot } from '../useLiveHistory'
import { useHistorySeries } from '../useHistorySeries'
import SeriesChart from './SeriesChart'
import Sparkline from './Sparkline'

// Painel de latencia do roteador — uma linha por link (rtt atual +
// sparkline ao vivo ~60s) + grafico da serie rtt_ms_avg do periodo
// selecionado + baseline 7d (mediana em linha tracejada).
export default function LatencyPanel({
  routerName,
  router,
  live,
  baseline,
  hours,
}: {
  routerName: string
  router: RouterSnap
  live: TimedSnapshot[]
  baseline?: BaselineMetric | null
  hours: number
}) {
  const links = router.links ?? []
  // rollups de 1min do periodo — refetch automatico ao trocar `hours`
  const { points } = useHistorySeries(routerName, hours)
  const hist = points.map((p) => p.rtt)
  const period = hours >= 24 ? `${Math.round(hours / 24)}d` : `${hours}h`

  return (
    <div className="card">
      <div className="card-title">
        <span>latência</span>
        <span className="title-sub">rtt · {period}</span>
      </div>
      <div className="card-body link-body">
        {links.map((l, i) => {
          const liveRtt = live
            .slice(-20)
            .map(
              (h) =>
                h.snap.routers[routerName]?.links?.[i]?.rtt_ms ?? null,
            )
          return (
            <div className="lcy-row" key={`${l.gateway}-${i}`}>
              <span className="lcy-name">
                {l.isp ?? 'link'}
                {l.via_transit ? ' ·fibra' : ''}
              </span>
              <span className="lcy-val mono">{fmtRtt(l.rtt_ms)}</span>
              <div className="spark-wrap">
                <Sparkline points={liveRtt} color="var(--warn)" />
              </div>
            </div>
          )
        })}
        {links.length === 0 && (
          <div className="muted small">sem links neste roteador.</div>
        )}
        <div className="chart-fill">
          {hist.some((v) => v != null) ? (
            <SeriesChart
              points={hist}
              unit="ms"
              color="var(--warn)"
              baseline={baseline?.median ?? null}
            />
          ) : (
            <div className="muted small pad-top">
              sem histórico ainda (rollups de 1min acumulando).
            </div>
          )}
        </div>
        <div className="link-foot">
          <span className="hud-label">baseline 7d</span>
          <span className="muted mini">
            {baseline
              ? `median ${fmtRtt(baseline.median)} · p95 ${fmtRtt(
                  baseline.p95,
                )}`
              : '—'}
          </span>
        </div>
      </div>
    </div>
  )
}
