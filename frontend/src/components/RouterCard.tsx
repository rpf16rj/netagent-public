import { fmtBytes } from '../api'
import type { RouterSnap } from '../types'
import type { TimedSnapshot } from '../useLiveHistory'
import { useLiveSeries } from '../useLiveHistory'
import { useHistorySeries } from '../useHistorySeries'
import RingGauge from './RingGauge'
import Sparkline from './Sparkline'

function gaugeColor(pct: number | null): string {
  if (pct == null) return 'var(--muted)'
  if (pct > 85) return 'var(--bad)'
  if (pct > 65) return 'var(--warn)'
  return 'var(--ice)'
}

// Card HUD de saude do roteador: aneis JARVIS p/ CPU e memoria com
// sparklines (cpu via rollups de 1min do periodo; mem via buffer ao vivo
// persistido — nao tem rollup), conn_count em destaque com sparkline
// persistido em localStorage + meta (uptime/leases/mem livre).
export default function RouterCard({
  name,
  router,
  history,
  hours,
}: {
  name: string
  router: RouterSnap
  history: TimedSnapshot[]
  hours: number
}) {
  const s = router.system
  const cpu = s?.cpu_load ?? null
  const memPct =
    s?.free_memory != null && s?.total_memory
      ? (1 - s.free_memory / s.total_memory) * 100
      : null
  const conn = s?.conn_count ?? null
  const snapTs = history[history.length - 1]?.t

  // cpu: semeia dos rollups cpu_avg do periodo (fallback p/ janela ao vivo)
  const { points: histPts } = useHistorySeries(name, hours)
  const cpuSpark = histPts.length
    ? histPts.map((p) => p.cpu)
    : history.map((h) => h.snap.routers[name]?.system?.cpu_load ?? null)

  // mem e conn nao tem rollup — buffer ao vivo persistido em localStorage
  // (hidrata no mount p/ reload nao zerar o sparkline)
  const memSpark = useLiveSeries(`mem:${name}`, memPct, snapTs)
  const connSpark = useLiveSeries(`conn:${name}`, conn, snapTs)

  return (
    <div className={`card ${router.reachable ? '' : 'card-down'}`}>
      <div className="card-title">
        <span>{router.label ?? name}</span>
        <span className="title-sub">
          {router.reachable
            ? `${s?.board_name ?? ''} · ros ${s?.version ?? '—'}`
            : 'sem resposta da api'}
        </span>
      </div>
      {!router.reachable ? (
        <div className="card-body router-off">
          <div className="link-state state-down">
            <span className="st-dot" />
            OFFLINE
          </div>
          <div className="muted small">{router.error ?? 'roteador fora do ar'}</div>
        </div>
      ) : (
        <div className="card-body router-body">
          <div className="gauge-col">
            <div className="gauge-box">
              <RingGauge value={cpu} label="cpu" color={gaugeColor(cpu)} />
            </div>
            <div className="gauge-spark">
              <Sparkline points={cpuSpark} color="var(--ice)" />
            </div>
          </div>
          <div className="gauge-col">
            <div className="gauge-box">
              <RingGauge value={memPct} label="memória" color={gaugeColor(memPct)} />
            </div>
            <div className="gauge-spark">
              <Sparkline points={memSpark} color="var(--ice)" />
            </div>
          </div>
          <div className="conn-col">
            <span className="hud-label">conexões</span>
            <span className="big-num conn-num">
              {conn == null ? '—' : conn.toLocaleString('pt-BR')}
            </span>
            <div className="conn-spark">
              <Sparkline points={connSpark} color="var(--blue)" />
            </div>
          </div>
          <div className="meta-col">
            <div className="meta-row">
              <span className="hud-label">uptime</span>
              <span className="mono">{s?.uptime || '—'}</span>
            </div>
            <div className="meta-row">
              <span className="hud-label">leases</span>
              <span className="mono">{router.leases?.length ?? '—'}</span>
            </div>
            <div className="meta-row">
              <span className="hud-label">mem livre</span>
              <span className="mono">{fmtBytes(s?.free_memory)}</span>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
