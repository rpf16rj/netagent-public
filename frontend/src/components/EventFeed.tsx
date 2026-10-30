import { useState } from 'react'
import { fmtTs } from '../api'
import type { EventItem } from '../types'

const LEVELS = ['info', 'warning', 'critical'] as const
type Level = (typeof LEVELS)[number]
const LS_KEY = 'netagent:events:hide_before'

// Agrupa repeticoes da MESMA mensagem (niveis iguais) — ex.: "Latencia
// alta no link X" x5 vira uma linha so c/ badge ×N.
function group(events: EventItem[]) {
  const map = new Map<string, { e: EventItem; n: number }>()
  for (const e of events) {
    const k = `${e.level}|${e.message}`
    const g = map.get(k)
    if (g) g.n += 1
    else map.set(k, { e, n: 1 })
  }
  return [...map.values()].sort((a, b) => b.e.ts - a.e.ts)
}

export default function EventFeed({ events }: { events: EventItem[] }) {
  const [level, setLevel] = useState<Level | 'all'>('all')
  // cutoff "ocultar lidos" — esconde eventos <= ts (persiste em localStorage)
  const [hideBefore, setHideBefore] = useState<number>(() =>
    Number(localStorage.getItem(LS_KEY) || 0),
  )
  const clear = () => {
    const ts = Date.now() / 1000
    localStorage.setItem(LS_KEY, String(ts))
    setHideBefore(ts)
  }
  const showHidden = () => {
    localStorage.removeItem(LS_KEY)
    setHideBefore(0)
  }

  const visible = events.filter(
    (e) => e.ts > hideBefore && (level === 'all' || e.level === level),
  )
  const hidden = events.length - events.filter((e) => e.ts > hideBefore).length
  const rows = group(visible)

  return (
    <div className="card">
      <div className="card-title">
        <span>eventos</span>
        <div className="period" style={{ marginLeft: 'auto' }}>
          {(['all', ...LEVELS] as const).map((l) => (
            <button
              key={l}
              className={`tab-pill ${level === l ? 'on' : ''}`}
              onClick={() => setLevel(l)}
            >
              {l === 'all' ? 'todos' : l}
            </button>
          ))}
          <button className="tab-pill" onClick={clear} title="ocultar lidos">
            limpar
          </button>
        </div>
      </div>
      <div className="card-body feed clip-fade scroll-y">
        {rows.length === 0 && (
          <div className="muted small pad-top">
            nenhum evento {level !== 'all' ? `(${level}) ` : ''}visivel.
            {hidden > 0 && (
              <>
                {' '}
                <button className="btn-mini" onClick={showHidden}>
                  mostrar {hidden} ocultos
                </button>
              </>
            )}
          </div>
        )}
        {rows.map(({ e, n }) => (
          <div key={e.id} className={`feed-item feed-${e.level}`}>
            <span className="feed-ts">{fmtTs(e.ts)}</span>
            <span className={`badge badge-${e.level}`}>{e.level}</span>
            <span className="feed-msg">
              {e.message}
              {n > 1 && <span className="feed-count">×{n}</span>}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
