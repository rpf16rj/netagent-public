import { useEffect, useState } from 'react'
import { api } from './api'
import type {
  ActionItem,
  BaselineResp,
  EventItem,
  RouterSnap,
  Snapshot,
} from './types'
import TopBar from './components/TopBar'
import LinkCard from './components/LinkCard'
import type { LinkEntry } from './components/LinkCard'
import RouterCard from './components/RouterCard'
import EventFeed from './components/EventFeed'
import DevicesPanel from './components/DevicesPanel'
import DnsPanel from './components/DnsPanel'
import WifiPanel from './components/WifiPanel'
import LatencyPanel from './components/LatencyPanel'
import HistoryPanel from './components/HistoryPanel'
import ActionsPanel from './components/ActionsPanel'
import TopTalkers from './components/TopTalkers'
import HeatmapPanel from './components/HeatmapPanel'
import TopoMap from './components/TopoMap'
import { useLiveHistory } from './useLiveHistory'

// Dashboard JARVIS: tela unica (100vh, sem scroll) com abas —
//   "rede geral": visao combinada (todos os links + os 2 MKs)
//   casa mae / casa user: mesma estrutura filtrada por roteador
//     (links, gauges, conexoes, latencia, historico, tops, devices, wifi)
// Seletor de periodo (1h/6h/24h/7d) alimenta os graficos de /api/history.
// Acoes do agente ficam num modal (agente desativado).

const PERIODS = [
  { h: 1, label: '1h' },
  { h: 6, label: '6h' },
  { h: 24, label: '24h' },
  { h: 168, label: '7d' },
]

// nome da aba por roteador — casa mae = LAN .2, casa user = LAN .3
function houseLabel(name: string, r?: RouterSnap): string {
  const sub = r?.lan_subnet ?? ''
  if (name.includes('mk-a') || sub.startsWith('10.2.2.')) return 'casa mãe'
  if (name.includes('mk-b') || sub.startsWith('10.3.3.'))
    return 'casa user'
  return (r?.label ?? name).toLowerCase()
}

// "10.3.3.0/24" -> "10.3.3." p/ filtrar top talkers por casa
function subnetPrefix(lan?: string): string | undefined {
  if (!lan) return undefined
  const parts = lan.split('/')[0].split('.')
  return parts.length === 4 ? `${parts[0]}.${parts[1]}.${parts[2]}.` : undefined
}

export default function App() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [events, setEvents] = useState<EventItem[]>([])
  const [actions, setActions] = useState<ActionItem[]>([])
  const [readOnly, setReadOnly] = useState(false)
  const [connected, setConnected] = useState(false)
  const [actionsOpen, setActionsOpen] = useState(false)
  const [page, setPage] = useState<string>(() =>
    window.location.hash.replace('#/', '') || 'geral')
  const [hours, setHours] = useState(24)
  const [baselines, setBaselines] = useState<BaselineResp | null>(null)

  // carga inicial
  useEffect(() => {
    api.snapshot().then(setSnapshot).catch(() => {})
    api
      .events()
      .then((r) => setEvents(r.events))
      .catch(() => {})
    api
      .actions()
      .then((r) => {
        setActions(r.actions)
        setReadOnly(r.read_only)
      })
      .catch(() => {})
    api
      .agentStatus()
      .then((s) => setReadOnly(s.read_only))
      .catch(() => {})
  }, [])

  // stream SSE — snapshot a cada ciclo de poll + alertas/acoes em tempo real
  useEffect(() => {
    const es = new EventSource('/api/stream')
    es.onopen = () => setConnected(true)
    es.onerror = () => setConnected(false)
    es.addEventListener('snapshot', (e) =>
      setSnapshot(JSON.parse((e as MessageEvent).data)),
    )
    es.addEventListener('alert', (e) => {
      const a = JSON.parse((e as MessageEvent).data)
      setEvents((prev) =>
        [
          { id: a.event_id ?? Date.now(), ts: Date.now() / 1000, ...a },
          ...prev,
        ].slice(0, 200),
      )
    })
    es.addEventListener('action', (e) => {
      const wrap = JSON.parse((e as MessageEvent).data)
      const act: ActionItem = wrap.data ?? wrap
      setActions((prev) => {
        const i = prev.findIndex((x) => x.id === act.id)
        if (i === -1) return [act, ...prev].slice(0, 100)
        const next = [...prev]
        next[i] = act
        return next
      })
    })
    es.addEventListener('agent', (e) => {
      const a = JSON.parse((e as MessageEvent).data)
      if (typeof a.read_only === 'boolean') setReadOnly(a.read_only)
    })
    return () => es.close()
  }, [])

  // historico (rollups 1min) e buscado por useHistorySeries dentro de cada
  // card — cache modulo-level (<60s) deduplica e reutiliza entre abas.

  // baselines 7d por roteador+metrica — refresh a cada 60s
  useEffect(() => {
    const load = () =>
      api
        .baseline()
        .then(setBaselines)
        .catch(() => {})
    load()
    const t = setInterval(load, 60000)
    return () => clearInterval(t)
  }, [])

  const history = useLiveHistory(snapshot)
  const routers = snapshot?.routers ?? {}
  const routerNames = Object.keys(routers)

  // abas: uma por roteador + topologia + rede geral (deep-link #/topo)
  const pages = [
    ...routerNames.map((n) => ({ key: n, label: houseLabel(n, routers[n]) })),
    { key: 'topo', label: 'topologia' },
    { key: 'geral', label: 'rede geral' },
  ]
  const activePage = pages.some((p) => p.key === page) ? page : 'geral'
  // deep-link: #/mk-a etc — troca de aba atualiza o hash (e vice-versa)
  const gotoPage = (k: string) => {
    setPage(k)
    window.location.hash = '/' + k
  }
  useEffect(() => {
    const onHash = () => setPage(window.location.hash.replace('#/', '') || 'geral')
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  // um card por link; roteador sem rotas (ou offline) vira um card "OFFLINE"
  const entriesFor = (names: string[]): LinkEntry[] =>
    names.flatMap((name): LinkEntry[] => {
      const r = routers[name]
      if (!r) return []
      const ls = r.links ?? []
      if (ls.length === 0) {
        return [{ routerName: name, router: r, link: null, linkIndex: -1 }]
      }
      return ls.map((link, linkIndex) => ({
        routerName: name,
        router: r,
        link,
        linkIndex,
      }))
    })

  const pending = actions.filter((a) => a.status === 'proposed').length
  const killswitch = async (v: boolean) =>
    setReadOnly((await api.killswitch(v)).read_only)

  const blOf = (routerName: string, metric: string) =>
    baselines?.routers?.[routerName]?.[metric] ?? null

  const renderLinkCards = (entries: LinkEntry[]) => (
    <section className="hud-links">
      {entries.map((e) => (
        <LinkCard
          key={`${e.routerName}:${e.linkIndex}`}
          entry={e}
          history={history}
          baseline={blOf(e.routerName, 'wan_rx_bps_avg')}
          baselineRtt={blOf(e.routerName, 'rtt_ms_avg')}
          hours={hours}
        />
      ))}
      {entries.length === 0 && (
        <div className="card">
          <div className="card-body">
            <div className="muted small">aguardando snapshot…</div>
          </div>
        </div>
      )}
    </section>
  )

  const housePage = (name: string) => {
    const r = routers[name]
    if (!r) return null
    const isRod = subnetPrefix(r.lan_subnet) === '10.3.3.'
    return (
      <>
        {renderLinkCards(entriesFor([name]))}
        <section className="hud-routers">
          <RouterCard
            name={name}
            router={r}
            history={history}
            hours={hours}
          />
          <LatencyPanel
            routerName={name}
            router={r}
            live={history}
            baseline={blOf(name, 'rtt_ms_avg')}
            hours={hours}
          />
          <HistoryPanel
            routerName={name}
            router={r}
            snapTs={snapshot?.ts}
            hours={hours}
            baselineRx={blOf(name, 'wan_rx_bps_avg')}
            baselineCpu={blOf(name, 'cpu_avg')}
          />
        </section>
        <section
          className={`hud-panels ${isRod ? 'hud-panels-5' : 'hud-panels-4'}`}
        >
          <TopTalkers subnet={subnetPrefix(r.lan_subnet)} />
          <DevicesPanel router={name} />
          {isRod && <WifiPanel />}
          <HeatmapPanel routerName={name} />
          <EventFeed events={events} />
        </section>
      </>
    )
  }

  return (
    <div className="app">
      <TopBar
        snapshot={snapshot}
        connected={connected}
        readOnly={readOnly}
        actionCount={pending}
        onOpenActions={() => setActionsOpen(true)}
      />

      <nav className="tabbar">
        <div className="tabs">
          {pages.map((p) => (
            <button
              key={p.key}
              className={`tab-pill ${activePage === p.key ? 'on' : ''}`}
              onClick={() => gotoPage(p.key)}
            >
              {p.label}
            </button>
          ))}
        </div>
        <div className="period">
          {PERIODS.map((p) => (
            <button
              key={p.h}
              className={`tab-pill ${hours === p.h ? 'on' : ''}`}
              onClick={() => setHours(p.h)}
            >
              {p.label}
            </button>
          ))}
        </div>
      </nav>

      <main className="hud">
        {activePage === 'topo' ? (
          <section className="hud-topo">
            <TopoMap snapshot={snapshot} onNavigate={gotoPage} />
          </section>
        ) : activePage === 'geral' ? (
          <>
            {renderLinkCards(entriesFor(routerNames))}
            <section className="hud-routers">
              {routerNames.map((name) => (
                <RouterCard
                  key={name}
                  name={name}
                  router={routers[name]}
                  history={history}
                  hours={hours}
                />
              ))}
            </section>
            <section className="hud-panels hud-panels-4">
              <TopTalkers />
              <DevicesPanel />
              <DnsPanel />
              <EventFeed events={events} />
            </section>
          </>
        ) : (
          housePage(activePage)
        )}
      </main>

      {actionsOpen && (
        <div className="modal-overlay" onClick={() => setActionsOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <ActionsPanel
              actions={actions}
              readOnly={readOnly}
              onKillswitch={killswitch}
              onClose={() => setActionsOpen(false)}
              onChanged={(a) =>
                setActions((prev) =>
                  prev.map((x) => (x.id === a.id ? a : x)),
                )
              }
            />
          </div>
        </div>
      )}
    </div>
  )
}
