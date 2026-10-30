import { useEffect, useState } from 'react'
import { api, fmtBps } from '../api'
import type { BaselineMetric, RouterSnap } from '../types'
import { useLiveSeries } from '../useLiveHistory'
import { useHistorySeries } from '../useHistorySeries'
import LiveAreaChart from './LiveAreaChart'
import Sparkline from './Sparkline'

// Historico de throughput do WAN do roteador (rollups de 1min) no periodo
// selecionado + sparklines de CPU (cpu_avg) e conexoes (ao vivo, persistido
// em localStorage pois nao tem rollup) + baseline 7d de download.
export default function HistoryPanel({
  routerName,
  router,
  snapTs,
  hours,
  baselineRx,
  baselineCpu,
}: {
  routerName: string
  router: RouterSnap
  snapTs: number | undefined // ts do ultimo snapshot — tick do buffer ao vivo
  hours: number
  baselineRx?: BaselineMetric | null
  baselineCpu?: BaselineMetric | null
}) {
  // rollups de 1min do periodo — semeiam os graficos no mount e refetch
  // automatico quando `hours` muda (cache modulo-level <60s)
  const { points: pts } = useHistorySeries(routerName, hours)
  const rx = pts.map((p) => p.wanRx)
  const tx = pts.map((p) => p.wanTx)
  const cpu = pts.map((p) => p.cpu)
  // conn_count nao tem rollup — buffer ao vivo persistido (nao zera no reload)
  const conn = useLiveSeries(
    `conn:${routerName}`,
    router.system?.conn_count ?? null,
    snapTs,
  )
  const period = hours >= 24 ? `${Math.round(hours / 24)}d` : `${hours}h`
  const hasData = pts.some((p) => p.wanRx != null)

  // interface de transito (mk-b: ether5-TRANSITO-MAE / mk-a: ether4-LAN-ROD)
  // — mostra quanto do trafego da casa corre pelo link do vizinho
  const transitIf = Object.keys(router.interfaces ?? {}).find(
    (i) => /transito|lan-rod/i.test(i),
  )
  const transit = transitIf ? router.interfaces?.[transitIf] : null

  // speedtest — capacidade medida do link ativo (job diario 04-06h)
  const [st, setSt] = useState<Awaited<
    ReturnType<typeof api.speedtest>
  > | null>(null)
  const [stBusy, setStBusy] = useState(false)
  useEffect(() => {
    const load = () => api.speedtest().then(setSt).catch(() => {})
    load()
    const t = setInterval(load, 60000)
    return () => clearInterval(t)
  }, [])
  const last = st?.results?.[0]
  const runTest = () => {
    setStBusy(true)
    api
      .speedtestRun()
      .then(() => api.speedtest().then(setSt))
      .finally(() => setStBusy(false))
  }

  return (
    <div className="card">
      <div className="card-title">
        <span>histórico</span>
        <span className="title-sub">{period} · 1min</span>
      </div>
      <div className="card-body link-body">
        <div className="chart-fill">
          {hasData ? (
            <LiveAreaChart rx={rx} tx={tx} />
          ) : (
            <div className="muted small pad-top">
              sem dados ainda — rollups de 1min acumulando.
            </div>
          )}
        </div>
        <div className="link-foot">
          <span className="hud-label">cpu</span>
          <div className="spark-wrap">
            <Sparkline points={cpu} color="var(--ice)" />
          </div>
          <span className="muted mini">
            {baselineCpu ? `${Math.round(baselineCpu.median ?? 0)}% med` : ''}
          </span>
        </div>
        <div className="link-foot">
          <span className="hud-label">conexões</span>
          <div className="spark-wrap">
            <Sparkline points={conn} color="var(--blue)" />
          </div>
          <span className="muted mini">ao vivo</span>
        </div>
        <div className="link-foot">
          <span className="hud-label">baseline 7d ▼</span>
          <span className="muted mini">
            {baselineRx
              ? `${fmtBps(baselineRx.median)} · p95 ${fmtBps(baselineRx.p95)}`
              : '—'}
          </span>
        </div>
        {transit && (
          <div className="link-foot">
            <span className="hud-label">trânsito</span>
            <span className="muted mini">
              ▼{fmtBps(transit.rx_bps)} ▲{fmtBps(transit.tx_bps)} ·{' '}
              {transitIf}
            </span>
          </div>
        )}
        <div className="link-foot">
          <span className="hud-label">capacidade</span>
          <span className="muted mini">
            {last?.dl_bps
              ? `${fmtBps(last.dl_bps)} ↓ · ${fmtBps(last.ul_bps)} ↑ · ` +
                `${Math.round(last.ping_ms ?? 0)}ms`
              : 'sem medição ainda'}
            {st?.running && ' · medindo…'}
          </span>
          <button
            className="btn-mini"
            disabled={stBusy || !!st?.running}
            onClick={runTest}
            title="medir agora (satura o link ~30s)"
          >
            medir
          </button>
        </div>
      </div>
    </div>
  )
}
