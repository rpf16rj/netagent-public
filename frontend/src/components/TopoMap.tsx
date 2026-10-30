import { useEffect, useRef, useState } from 'react'
import type { MouseEvent as ReactMouseEvent } from 'react'
import { api, fmtBps, fmtRtt } from '../api'
import type {
  AdguardResp,
  DevicesStatsResp,
  HealthResp,
  IfaceStats,
  LinkState,
  RouterSnap,
  Snapshot,
  WifiResp,
} from '../types'

// ------------------------------------------------------------------
// Mapa fixo da topologia conhecida (ver SPEC) — sem auto-layout:
//   ISPs em cima (redebr / onda livre), MKs no meio ligados pela
//   fibra 172.20.20.0/30, LANs + APs embaixo, infra no rodape.
// Linhas: ice solida+fluxo = ativo, amber tracejada = standby,
// red fina = down, ghost = sem monitoramento (twibi).
// ------------------------------------------------------------------

const TRANSIT_RE = /transito|lan-rod/i

// geometria fixa (viewBox 1000x560)
const ISP_Y = 55
const MK_TOP = 139 // reta de saida das arestas MK->ISP
const MK = { a: 250, b: 750 } // cx dos nos MK
const LAN_Y = 300 // cy dos nos LAN
const AP_Y = 395 // cy dos nos de AP
const INF_Y = 505 // cy dos nos de infra
const ISP_X = { redebr: 250, onda: 750 }

// helpers de arco p/ o mini-gauge de cpu dentro do no do MK
function polar(cx: number, cy: number, r: number, deg: number) {
  const a = (deg * Math.PI) / 180
  return { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) }
}
function arcPath(cx: number, cy: number, r: number, a0: number, a1: number) {
  const s = polar(cx, cy, r, a0)
  const e = polar(cx, cy, r, a1)
  return `M ${s.x.toFixed(1)} ${s.y.toFixed(1)} A ${r} ${r} 0 ${
    a1 - a0 > 180 ? 1 : 0
  } 1 ${e.x.toFixed(1)} ${e.y.toFixed(1)}`
}
const G0 = 135
const GSW = 270

function cubicAt(p0: number, c1: number, c2: number, p3: number, t: number) {
  const u = 1 - t
  return u * u * u * p0 + 3 * u * u * t * c1 + 3 * u * t * t * c2 + t * t * t * p3
}

// bps compacto p/ chips de aresta (o tooltip usa fmtBps completo)
function fB(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}g`
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}m`
  if (v >= 1e3) return `${(v / 1e3).toFixed(0)}k`
  return `${v.toFixed(0)}`
}

function transitStats(r?: RouterSnap): IfaceStats | null {
  if (!r?.interfaces) return null
  for (const [n, s] of Object.entries(r.interfaces)) {
    if (TRANSIT_RE.test(n)) return s
  }
  return null
}

// qual no ISP o link aponta — pelo comment/isp, com fallback via_transit
function ispOf(link: LinkState, r?: RouterSnap): 'redebr' | 'onda' {
  const s = `${link.isp ?? ''} ${link.route_comment ?? ''}`.toLowerCase()
  if (/onda|livre/.test(s)) return 'onda'
  if (/redebr|rede.?br/.test(s)) return 'redebr'
  const ownOnda = /onda|livre/.test((r?.isp ?? '').toLowerCase())
  return link.via_transit ? (ownOnda ? 'redebr' : 'onda') : ownOnda ? 'onda' : 'redebr'
}

type TipRows = [string, string][]
interface Tip {
  x: number
  y: number
  title: string
  rows: TipRows
}

// chip flutuante sobre uma aresta (▼/▲ bps + rtt/score)
function Chip({
  x,
  y,
  lines,
  w = 92,
}: {
  x: number
  y: number
  lines: { t: string; cls?: string }[]
  w?: number
}) {
  const h = 8 + lines.length * 11
  return (
    <g transform={`translate(${x},${y})`}>
      <rect
        className="topo-chip"
        x={-w / 2}
        y={-h / 2}
        width={w}
        height={h}
        rx={5}
      />
      {lines.map((l, i) => (
        <text
          key={i}
          x={0}
          y={-h / 2 + 11 + i * 11}
          textAnchor="middle"
          className={l.cls ?? 't-rate'}
        >
          {l.t}
        </text>
      ))}
    </g>
  )
}

interface Props {
  snapshot: Snapshot | null
  onNavigate?: (page: string) => void
}

export default function TopoMap({ snapshot, onNavigate }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const [tip, setTip] = useState<Tip | null>(null)
  const [devStats, setDevStats] = useState<DevicesStatsResp | null>(null)
  const [wifi, setWifi] = useState<WifiResp | null>(null)
  const [adg, setAdg] = useState<AdguardResp | null>(null)
  const [health, setHealth] = useState<HealthResp | null>(null)

  // contagens por LAN (1 request — /api/devices/stats ja agrega by_router)
  useEffect(() => {
    let off = false
    const load = () =>
      api
        .devicesStats()
        .then((r) => !off && setDevStats(r))
        .catch(() => {})
    load()
    const t = setInterval(load, 30000)
    return () => {
      off = true
      clearInterval(t)
    }
  }, [])

  // tenda rx2 (poll interno do backend e ~15s; aqui so consumimos o cache)
  useEffect(() => {
    let off = false
    const load = () =>
      api
        .wifi()
        .then((r) => !off && setWifi(r))
        .catch(() => {})
    load()
    const t = setInterval(load, 15000)
    return () => {
      off = true
      clearInterval(t)
    }
  }, [])

  // adguard + healthscore (score por link alimenta o badge qNN das arestas)
  useEffect(() => {
    let off = false
    const load = () => {
      api
        .adguard()
        .then((r) => !off && setAdg(r))
        .catch(() => {})
      api
        .healthscore()
        .then((r) => !off && setHealth(r))
        .catch(() => {})
    }
    load()
    const t = setInterval(load, 30000)
    return () => {
      off = true
      clearInterval(t)
    }
  }, [])

  const routers = snapshot?.routers ?? {}
  const names = Object.keys(routers)
  const lanPrefix = (n: string) => routers[n]?.lan_subnet?.split('/')[0] ?? ''
  const isMae = (n: string) =>
    /mk-a/i.test(n) || lanPrefix(n).startsWith('10.2.2.')
  const isRod = (n: string) =>
    /mk-b/i.test(n) || lanPrefix(n).startsWith('10.3.3.')
  const aName = names.find(isMae) ?? names.find((n) => !isRod(n))
  const bName = names.find(isRod) ?? names.find((n) => n !== aName)
  const mkA = aName ? routers[aName] : undefined
  const mkB = bName ? routers[bName] : undefined
  const aTr = transitStats(mkA)
  const bTr = transitStats(mkB)

  const hover = (title: string, rows: TipRows) => ({
    onMouseMove: (e: ReactMouseEvent) => {
      const r = wrapRef.current?.getBoundingClientRect()
      if (!r) return
      setTip({
        x: e.clientX - r.left + 14,
        y: e.clientY - r.top + 14,
        title,
        rows,
      })
    },
    onMouseLeave: () => setTip(null),
  })

  // ---------------- nos ----------------

  const renderIsp = (key: 'redebr' | 'onda', label: string) => {
    const cx = ISP_X[key]
    const rows: TipRows = []
    for (const n of names) {
      const r = routers[n]
      ;(r?.links ?? []).forEach((l, i) => {
        if (ispOf(l, r) === key) {
          rows.push([
            `${n} link${i}`,
            `${l.gateway || '—'} · ${
              !r?.reachable || l.disabled ? 'down' : l.active ? 'ativo' : 'standby'
            }`,
          ])
        }
      })
    }
    if (!rows.length) rows.push(['links', '—'])
    return (
      <g className="topo-node" key={key} {...hover(`isp · ${label}`, rows)}>
        <ellipse className="topo-isp" cx={cx} cy={ISP_Y} rx={85} ry={26} />
        <ellipse className="topo-isp-in" cx={cx} cy={ISP_Y} rx={70} ry={19} />
        <text className="t-title" x={cx} y={ISP_Y + 4} textAnchor="middle">
          {label}
        </text>
      </g>
    )
  }

  const renderMk = (
    r: RouterSnap | undefined,
    name: string | undefined,
    cx: number,
    house: string,
  ) => {
    const s = r?.system
    const cpu = s?.cpu_load ?? null
    const pct = cpu == null ? 0 : Math.max(0, Math.min(100, cpu))
    const reach = r?.reachable ?? false
    const x0 = cx - 85
    const gcx = cx - 53
    const rows: TipRows = [
      ['estado', reach ? 'online' : 'offline'],
      ['uptime', s?.uptime || '—'],
      ['cpu', cpu == null ? '—' : `${Math.round(pct)}%`],
      ['conns', s?.conn_count == null ? '—' : String(s.conn_count)],
      ['lan', r?.lan_subnet ?? '—'],
      ['isp local', r?.isp ?? '—'],
      ['', 'clique p/ abrir a aba'],
    ]
    return (
      <g
        className={`topo-node ${name ? 'clickable' : ''}`}
        key={name ?? house}
        onClick={() => name && onNavigate?.(name)}
        {...hover(r?.label ?? name ?? 'mk', rows)}
      >
        <rect
          className={`topo-rect ${reach || !r ? '' : 'topo-rect-down'}`}
          x={x0}
          y={MK_TOP}
          width={170}
          height={92}
          rx={10}
        />
        <circle
          className={`topo-dot ${reach ? 'd-ok topo-pulse' : 'd-bad'}`}
          cx={x0 + 153}
          cy={MK_TOP + 13}
          r={4}
        />
        <path className="topo-g-track" d={arcPath(gcx, 185, 15, G0, G0 + GSW)} />
        {reach && pct > 0 && (
          <path
            className="topo-g-val"
            d={arcPath(gcx, 185, 15, G0, G0 + (GSW * pct) / 100)}
          />
        )}
        <text className="t-num" x={gcx} y={188} textAnchor="middle" fontSize={9}>
          {reach && cpu != null ? Math.round(pct) : '—'}
        </text>
        <text className="t-mini" x={gcx} y={197} textAnchor="middle">
          cpu
        </text>
        <text className="t-title" x={cx - 28} y={MK_TOP + 29}>
          {(name ?? 'mk').toUpperCase()}
        </text>
        {reach ? (
          <>
            <text className="t-sub" x={cx - 28} y={MK_TOP + 42}>
              {house} · {s?.board_name ?? 'routeros'}
            </text>
            <text className="t-sub" x={cx - 28} y={MK_TOP + 55}>
              up {s?.uptime || '—'}
            </text>
            <text className="t-sub" x={cx - 28} y={MK_TOP + 68}>
              {s?.conn_count ?? '—'} conns
            </text>
          </>
        ) : (
          <>
            <text className="t-bad" x={cx - 28} y={MK_TOP + 45}>
              OFFLINE
            </text>
            <text className="t-mini" x={cx - 28} y={MK_TOP + 60}>
              {(r?.error ?? (r ? 'sem resposta' : 'aguardando')).slice(0, 28)}
            </text>
          </>
        )}
      </g>
    )
  }

  const renderLan = (
    r: RouterSnap | undefined,
    name: string | undefined,
    cx: number,
    house: string,
  ) => {
    const count = name ? devStats?.by_router?.[name] : undefined
    const rows: TipRows = [
      ['subnet', r?.lan_subnet ?? '—'],
      ['devices', count == null ? '—' : String(count)],
      ['leases', r?.leases == null ? '—' : String(r.leases.length)],
      ['casa', house],
    ]
    return (
      <g className="topo-node" {...hover(`lan · ${house}`, rows)}>
        <rect
          className="topo-rect"
          x={cx - 75}
          y={LAN_Y - 30}
          width={150}
          height={60}
          rx={10}
        />
        <text className="t-sub" x={cx} y={LAN_Y - 14} textAnchor="middle">
          lan · bridge
        </text>
        <text
          className="t-title"
          x={cx}
          y={LAN_Y + 2}
          textAnchor="middle"
          fontSize={11}
        >
          {r?.lan_subnet ?? '—'}
        </text>
        <text className="t-sub" x={cx} y={LAN_Y + 18} textAnchor="middle">
          {house}
        </text>
        <circle className="topo-bubble" cx={cx + 75} cy={LAN_Y} r={14} />
        <text className="t-num" x={cx + 75} y={LAN_Y + 4} textAnchor="middle">
          {count ?? '—'}
        </text>
        <text className="t-mini" x={cx + 75} y={LAN_Y + 24} textAnchor="middle">
          devices
        </text>
      </g>
    )
  }

  const renderGhost = (ip: string, cx: number) => (
    <g
      className="topo-node"
      key={ip}
      {...hover('twibi ap', [
        ['ip', ip],
        ['estado', 'não monitorado'],
        ['nota', 'fora do alcance do backend'],
      ])}
    >
      <rect
        className="topo-ghost-rect"
        x={cx - 55}
        y={AP_Y - 20}
        width={110}
        height={44}
        rx={9}
      />
      <text className="t-ghost" x={cx} y={AP_Y - 5} textAnchor="middle">
        twibi ap
      </text>
      <text className="t-ghost" x={cx} y={AP_Y + 7} textAnchor="middle">
        {ip}
      </text>
      <text
        className="t-ghost"
        x={cx}
        y={AP_Y + 17}
        textAnchor="middle"
        fontSize={6.5}
      >
        não monitorado
      </text>
    </g>
  )

  const renderTenda = () => {
    const on = wifi?.reachable ?? false
    const n = wifi?.clients?.length
    const rows: TipRows = [
      ['ip', '10.3.3.178'],
      ['estado', wifi == null ? '…' : on ? 'online' : 'offline'],
      ['clientes', n == null ? '—' : String(n)],
      ['ssid', wifi?.meta?.ssid ?? '—'],
    ]
    return (
      <g className="topo-node" {...hover('tenda rx2 pro', rows)}>
        <rect
          className="topo-rect"
          x={685}
          y={AP_Y - 22}
          width={130}
          height={44}
          rx={9}
        />
        <circle
          className={`topo-dot ${
            wifi == null ? 'd-mut' : on ? 'd-ok topo-pulse' : 'd-bad'
          }`}
          cx={802}
          cy={AP_Y - 12}
          r={3.5}
        />
        <text
          className="t-title"
          x={750}
          y={AP_Y - 6}
          textAnchor="middle"
          fontSize={10}
        >
          tenda rx2
        </text>
        <text className="t-sub" x={750} y={AP_Y + 7} textAnchor="middle">
          10.3.3.178 · {wifi == null ? '…' : on ? 'online' : 'offline'}
        </text>
        <text className="t-rate" x={750} y={AP_Y + 18} textAnchor="middle">
          {n == null ? '—' : n} clientes
        </text>
      </g>
    )
  }

  const renderInfra = (
    label: string,
    sub: string,
    cx: number,
    ok: boolean | null,
    rows: TipRows,
  ) => (
    <g className="topo-node" {...hover(label, rows)}>
      <rect
        className="topo-rect"
        x={cx - 65}
        y={INF_Y - 20}
        width={130}
        height={40}
        rx={9}
      />
      <circle
        className={`topo-dot ${
          ok == null ? 'd-mut' : ok ? 'd-ok topo-pulse' : 'd-bad'
        }`}
        cx={cx + 52}
        cy={INF_Y - 10}
        r={3.5}
      />
      <text
        className="t-title"
        x={cx}
        y={INF_Y - 3}
        textAnchor="middle"
        fontSize={10}
      >
        {label}
      </text>
      <text className="t-sub" x={cx} y={INF_Y + 11} textAnchor="middle">
        {sub}
      </text>
    </g>
  )

  // ---------------- arestas ----------------

  // MK -> ISP: uma aresta por link (reta p/ o isp local, curva p/ via transito)
  const renderLinks = (
    r: RouterSnap | undefined,
    name: string | undefined,
    mkx: number,
  ) => {
    const links = r?.links ?? []
    // roteador sem rotas (ou ainda sem snapshot): aresta placeholder '—'
    const eff: (LinkState | null)[] = links.length ? links : [null]
    return eff.map((link, i) => {
      const key = `${name ?? mkx}-${i}`
      const target = link
        ? ispOf(link, r)
        : mkx === MK.b
          ? 'onda'
          : 'redebr'
      const toX = ISP_X[target]
      const stats =
        r?.interfaces?.[link?.out_interface || r?.wan_interface || '']
      const state = !link || !r?.reachable || link.disabled
        ? 'down'
        : link.active
          ? 'ativo'
          : 'standby'
      const cls =
        state === 'ativo' ? 'te-ativo' : state === 'standby' ? 'te-standby' : 'te-down'
      const curved = (link?.via_transit ?? false) || Math.abs(toX - mkx) > 60
      const y1 = 80
      let d: string
      let lx: number
      let ly: number
      if (curved) {
        const dir = Math.sign(toX - mkx) || 1
        const fx = mkx + dir * 35
        const ex = toX - dir * 30
        const dx = ex - fx
        const c1x = fx + dx * 0.35
        const c1y = 126
        const c2x = fx + dx * 0.8
        const c2y = 98
        d = `M ${fx} ${MK_TOP} C ${c1x} ${c1y} ${c2x} ${c2y} ${ex} ${y1}`
        lx = cubicAt(fx, c1x, c2x, ex, 0.3)
        ly = cubicAt(MK_TOP, c1y, c2y, y1, 0.3)
      } else {
        d = `M ${mkx} ${MK_TOP} L ${toX} ${y1}`
        lx = mkx
        ly = 110
      }
      const score = name
        ? health?.routers?.[name]?.links?.find((h) => h.index === i)?.score
        : undefined
      const rows: TipRows = link
        ? [
            ['estado', state],
            ['gw', link.gateway || '—'],
            ['iface', link.out_interface || '—'],
            ['rtt', fmtRtt(link.rtt_ms)],
            ['perda', link.loss_pct == null ? '—' : `${link.loss_pct}%`],
            ['dist', String(link.distance)],
            ['score', score == null ? '—' : String(score)],
            ['▼ rx', fmtBps(stats?.rx_bps)],
            ['▲ tx', fmtBps(stats?.tx_bps)],
          ]
        : [['estado', 'sem dados']]
      if (link?.via_transit) rows.push(['rota', 'via fibra 172.20.20.0/30'])
      return (
        <g
          key={key}
          {...hover(`${link?.isp ?? 'link'} · ${name ?? '?'}`, rows)}
        >
          {state === 'ativo' ? (
            <>
              <path className="topo-edge te-ativo te-base" d={d} />
              <path className="topo-edge te-ativo te-anim" d={d} />
            </>
          ) : (
            <path className={`topo-edge ${cls}`} d={d} />
          )}
          <path className="topo-edge te-hover" d={d} />
          <Chip
            x={lx}
            y={ly}
            w={link ? 96 : 40}
            lines={
              link
                ? [
                    { t: `▼${fB(stats?.rx_bps)} ▲${fB(stats?.tx_bps)}` },
                    {
                      t: `rtt ${fmtRtt(link.rtt_ms)} · q${score ?? '—'}`,
                      cls: 't-warn',
                    },
                  ]
                : [{ t: '—' }]
            }
          />
        </g>
      )
    })
  }

  // ---------------- render ----------------

  const trCls = mkA?.reachable && mkB?.reachable ? 'te-ativo' : 'te-down'

  return (
    <div className="card topo-card">
      <div className="card-title">
        <span>topologia da rede</span>
        <span className="title-sub">
          saúde {health?.global ?? '—'} · {names.length || '—'} roteadores
        </span>
      </div>
      <div className="card-body topo-body">
        <div className="topo-wrap" ref={wrapRef}>
          <svg
            className="topo-svg"
            viewBox="0 0 1000 560"
            preserveAspectRatio="xMidYMid meet"
            role="img"
            aria-label="mapa da topologia da rede"
          >
            <defs>
              <marker
                id="topo-arr"
                viewBox="0 0 8 8"
                refX={7}
                refY={4}
                markerWidth={7}
                markerHeight={7}
                orient="auto-start-reverse"
              >
                <path d="M0,0 L8,4 L0,8 Z" fill="#7fd4ff" opacity={0.85} />
              </marker>
            </defs>

            {/* ---- arestas (desenhadas antes dos nos, ficam por baixo) ---- */}

            {/* MK -> ISP (por link) */}
            {renderLinks(mkA, aName, MK.a)}
            {renderLinks(mkB, bName, MK.b)}

            {/* transito MK-A <-> MK-B: duas linhas, fluxo nos dois sentidos */}
            <g
              {...hover('fibra · trânsito entre casas', [
                ['rede', '172.20.20.0/30'],
                [`${aName ?? 'mk-a'} ▲tx`, fmtBps(aTr?.tx_bps)],
                [`${aName ?? 'mk-a'} ▼rx`, fmtBps(aTr?.rx_bps)],
                [`${bName ?? 'mk-b'} ▲tx`, fmtBps(bTr?.tx_bps)],
                [`${bName ?? 'mk-b'} ▼rx`, fmtBps(bTr?.rx_bps)],
              ])}
            >
              <path
                className={`topo-edge ${trCls} te-anim`}
                d="M 338 180 H 652"
                markerEnd="url(#topo-arr)"
              />
              <path
                className={`topo-edge ${trCls} te-anim`}
                d="M 662 196 H 348"
                markerEnd="url(#topo-arr)"
              />
              <path className="topo-edge te-hover" d="M 335 188 H 665" />
              <text className="t-mini" x={500} y={163} textAnchor="middle">
                fibra 172.20.20.0/30
              </text>
              <text className="t-rate" x={398} y={174} textAnchor="middle">
                ▲{fB(aTr?.tx_bps)}
              </text>
              <text className="t-rate" x={602} y={174} textAnchor="middle">
                ▼{fB(bTr?.rx_bps)}
              </text>
              <text className="t-rate" x={602} y={209} textAnchor="middle">
                ▲{fB(bTr?.tx_bps)}
              </text>
              <text className="t-rate" x={398} y={209} textAnchor="middle">
                ▼{fB(aTr?.rx_bps)}
              </text>
            </g>

            {/* MK -> LAN */}
            <path className="topo-edge te-lan" d={`M ${MK.a} 231 L ${MK.a} 270`} />
            <path className="topo-edge te-lan" d={`M ${MK.b} 231 L ${MK.b} 270`} />

            {/* LAN .2 -> twibi ghosts (casa mãe, nao monitorados) */}
            <path className="topo-edge te-ap" d="M 218 330 L 172 375" />
            <path className="topo-edge te-ap" d="M 282 330 L 328 375" />

            {/* LAN .3 -> tenda + infra */}
            <path className="topo-edge te-lan" d="M 750 330 L 750 373" />
            <path className="topo-edge te-ap" d="M 700 330 L 400 485" />
            <path className="topo-edge te-ap" d="M 720 330 L 560 485" />

            {/* ---- nos ---- */}

            {renderIsp('redebr', 'redebr')}
            {renderIsp('onda', 'onda livre')}

            {renderMk(mkA, aName, MK.a, 'casa mãe')}
            {renderMk(mkB, bName, MK.b, 'casa user')}

            {renderLan(mkA, aName, MK.a, 'casa mãe')}
            {renderLan(mkB, bName, MK.b, 'casa user')}

            {renderGhost('10.2.2.129', 170)}
            {renderGhost('10.2.2.165', 330)}
            {renderTenda()}

            {renderInfra('netagent', 'lxc · backend', 400, true, [
              ['papel', 'poller · api · sse'],
              ['estado', 'online'],
            ])}
            {renderInfra(
              'adguard',
              `dns · ${adg == null ? '…' : adg.reachable ? 'online' : 'offline'}`,
              560,
              adg == null ? null : adg.reachable,
              [
                ['estado', adg == null ? '…' : adg.reachable ? 'online' : 'offline'],
                ['versão', adg?.version ?? '—'],
                [
                  'queries 24h',
                  adg?.queries_24h == null
                    ? '—'
                    : adg.queries_24h.toLocaleString('pt-BR'),
                ],
                [
                  'bloqueadas',
                  adg?.blocked_pct == null ? '—' : `${adg.blocked_pct}%`,
                ],
              ],
            )}

            {/* ---- legenda ---- */}
            <g>
              <text className="t-sub" x={18} y={478}>
                legenda
              </text>
              <line
                className="topo-edge te-ativo"
                x1={18}
                y1={492}
                x2={52}
                y2={492}
              />
              <text className="t-mini" x={60} y={495}>
                ativo
              </text>
              <line
                className="topo-edge te-standby"
                x1={18}
                y1={508}
                x2={52}
                y2={508}
              />
              <text className="t-mini" x={60} y={511}>
                standby
              </text>
              <line
                className="topo-edge te-down"
                x1={18}
                y1={524}
                x2={52}
                y2={524}
              />
              <text className="t-mini" x={60} y={527}>
                down
              </text>
              <rect
                className="topo-ghost-rect"
                x={18}
                y={534}
                width={34}
                height={11}
                rx={3}
              />
              <text className="t-mini" x={60} y={543}>
                sem monitoramento
              </text>
            </g>
          </svg>

          {tip && (() => {
            const w = wrapRef.current?.clientWidth ?? 800
            const h = wrapRef.current?.clientHeight ?? 480
            return (
              <div
                className="topo-tip"
                style={{
                  left: Math.min(tip.x, Math.max(8, w - 252)),
                  top: Math.min(tip.y, Math.max(8, h - 170)),
                }}
              >
                <div className="tt-t">{tip.title}</div>
                {tip.rows.map(([k, v], i) => (
                  <div className="tt-r" key={i}>
                    <span>{k}</span>
                    <b>{v}</b>
                  </div>
                ))}
              </div>
            )
          })()}
        </div>
      </div>
    </div>
  )
}
