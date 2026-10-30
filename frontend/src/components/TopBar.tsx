import { useEffect, useState } from 'react'
import { api, fmtBps, fmtRtt } from '../api'
import type { Snapshot } from '../types'

interface Props {
  snapshot: Snapshot | null
  connected: boolean
  readOnly: boolean
  actionCount: number
  onOpenActions: () => void
}

interface Pill {
  key: string
  label: string
  cls: 'ativo' | 'standby' | 'down'
  title: string
}

export default function TopBar({
  snapshot,
  connected,
  readOnly,
  actionCount,
  onOpenActions,
}: Props) {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])

  // saude global = min dos scores de links/roteadores (/api/healthscore)
  const [health, setHealth] = useState<number | null>(null)
  useEffect(() => {
    const load = () =>
      api
        .healthscore()
        .then((r) => setHealth(r.global))
        .catch(() => {})
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [])
  const healthCls =
    health == null
      ? 'proposed'
      : health >= 90
        ? 'ok'
        : health >= 70
          ? 'warn'
          : 'bad'

  const pills: Pill[] = Object.entries(snapshot?.routers ?? {}).flatMap(
    ([name, r]) => {
      const links = r.links ?? []
      if (!r.reachable) {
        return [
          {
            key: name,
            label: r.label ?? name,
            cls: 'down' as const,
            title: r.error ?? 'roteador offline',
          },
        ]
      }
      if (links.length === 0) {
        return [
          {
            key: name,
            label: r.label ?? name,
            cls: 'standby' as const,
            title: 'sem rota comentada',
          },
        ]
      }
      return links.map((l, i) => {
        const stats =
          r.interfaces?.[l.out_interface || r.wan_interface || '']
        return {
          key: `${name}-${i}`,
          label: l.isp ?? name,
          cls: l.disabled
            ? ('down' as const)
            : l.active
              ? ('ativo' as const)
              : ('standby' as const),
          title:
            `${r.label ?? name} · gw ${l.gateway || '—'} · ` +
            `${fmtRtt(l.rtt_ms)} · ${fmtBps(stats?.rx_bps)}↓`,
        }
      })
    },
  )

  return (
    <header className="topbar">
      <div className="brand">
        <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden>
          <polygon
            points="12,2 21,7 21,17 12,22 3,17 3,7"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.4"
          />
          <circle
            cx="12"
            cy="12"
            r="3.4"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.4"
          />
          <circle cx="12" cy="12" r="1.2" fill="currentColor" />
        </svg>
        netagent
      </div>
      <div className="pills">
        {pills.map((p) => (
          <span key={p.key} className={`pill pill-${p.cls}`} title={p.title}>
            <span className="p-dot" />
            {p.label}
          </span>
        ))}
      </div>
      <div className="topbar-right">
        <span className={`badge badge-${healthCls}`}>
          saúde {health == null ? '—' : health}
        </span>
        {readOnly && <span className="badge badge-warn">read-only</span>}
        <span className="clock">
          {now.toLocaleTimeString('pt-BR', { hour12: false })}
        </span>
        <span className={`dot ${connected ? 'dot-ok' : 'dot-bad'}`} />
        <span className="live-label">
          {connected ? 'online' : 'reconectando'}
        </span>
        <button className="hud-btn" onClick={onOpenActions}>
          ações
          {actionCount > 0 && (
            <span className="badge-count">{actionCount}</span>
          )}
        </button>
      </div>
    </header>
  )
}
