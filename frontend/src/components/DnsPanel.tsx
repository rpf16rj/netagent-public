import { useEffect, useState } from 'react'
import { api } from '../api'
import type { AdguardResp } from '../types'

// Painel DNS/AdGuard Home (CT109) — queries 24h, bloqueio, latencia media
// e top dominios consultados/bloqueados.
export default function DnsPanel() {
  const [st, setSt] = useState<AdguardResp | null>(null)
  useEffect(() => {
    const load = () => api.adguard().then(setSt).catch(() => {})
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [])

  const ok = !!st?.reachable
  const rows = (lst?: Record<string, number>[], max = 6) =>
    (lst ?? []).slice(0, max).map((d, i) => {
      const [name, n] = Object.entries(d)[0] ?? ['', 0]
      return { name, n, i }
    })

  return (
    <div className="card">
      <div className="card-title">
        <span>dns · adguard</span>
        <span className={ok ? 'badge badge-ok' : 'badge badge-bad'}>
          {ok ? 'ONLINE' : 'OFFLINE'}
        </span>
      </div>
      <div className="card-body link-body">
        {ok ? (
          <>
            <div className="link-foot">
              <span className="hud-label">queries 24h</span>
              <span className="mono">
                {(st?.queries_24h ?? 0).toLocaleString('pt-BR')}
              </span>
              <span className="hud-label">bloqueadas</span>
              <span className="mono" style={{ color: 'var(--warn)' }}>
                {(st?.blocked_24h ?? 0).toLocaleString('pt-BR')}
              </span>
            </div>
            <div className="link-foot">
              <span className="hud-label">% filtrado</span>
              <span className="mono">{st?.blocked_pct ?? 0}%</span>
              <span className="hud-label">média</span>
              <span className="mono">{st?.avg_ms ?? 0} ms</span>
            </div>
            <div className="hud-label" style={{ marginTop: 4 }}>
              top domínios
            </div>
            <div className="scroll-y" style={{ maxHeight: 110 }}>
              {rows(st?.top_domains).map((r) => (
                <div className="lcy-row" key={'q' + r.i}>
                  <span className="lcy-name" title={r.name}>{r.name}</span>
                  <span className="lcy-val mono">{r.n}</span>
                </div>
              ))}
            </div>
            <div className="hud-label">bloqueados</div>
            <div className="scroll-y" style={{ maxHeight: 80 }}>
              {rows(st?.top_blocked).map((r) => (
                <div className="lcy-row" key={'b' + r.i}>
                  <span className="lcy-name" title={r.name}
                    style={{ color: 'var(--warn)' }}>{r.name}</span>
                  <span className="lcy-val mono">{r.n}</span>
                </div>
              ))}
            </div>
          </>
        ) : (
          <div className="muted small pad-top">
            {st?.note || st?.error || 'sem resposta do AdGuard — confira credenciais no .env'}
          </div>
        )}
      </div>
    </div>
  )
}
