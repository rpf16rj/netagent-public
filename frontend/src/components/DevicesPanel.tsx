import { useEffect, useState } from 'react'
import { api } from '../api'
import type { DevicesResp } from '../types'

// Dispositivos conhecidos — tracking por MAC a partir dos leases DHCP
// (substitui a tabela de leases crus). Ponto aceso = visto nos ultimos
// 5min; badge NOVO = first_seen dentro da janela `new_days` (default 7d).
export default function DevicesPanel({ router }: { router?: string }) {
  const [data, setData] = useState<DevicesResp | null>(null)
  const [q, setQ] = useState('')

  useEffect(() => {
    const load = () =>
      api
        .devices(router)
        .then(setData)
        .catch(() => {})
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [router])

  const needle = q.trim().toLowerCase()
  const rows = (data?.devices ?? []).filter(
    (d) =>
      !needle ||
      d.ip.includes(needle) ||
      (d.name || '').toLowerCase().includes(needle) ||
      d.mac.toLowerCase().includes(needle),
  )
  const newCount = rows.filter((d) => d.is_new).length
  const now = Date.now() / 1000

  return (
    <div className="card">
      <div className="card-title">
        <span>dispositivos</span>
        <span className="title-sub">
          {data
            ? `${data.count}` + (newCount ? ` · ${newCount} novos` : '')
            : '…'}
        </span>
      </div>
      <input
        className="panel-search"
        placeholder="filtrar ip / nome / mac…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      <div className="card-body scroll-y">
        <div className="lease-grid">
          {rows.map((d) => (
            <div
              className="lease-item"
              key={d.mac}
              title={`${d.mac} · desde ${new Date(
                d.first_seen * 1000,
              ).toLocaleString('pt-BR')}`}
            >
              <span
                className={`l-dot ${now - d.last_seen < 300 ? 'on' : ''}`}
              />
              <span className="ip">{d.ip || '—'}</span>
              <span className="host">{d.name || d.mac}</span>
              {d.is_new && <span className="badge badge-ok">novo</span>}
              <span className="rt">{d.router}</span>
            </div>
          ))}
          {rows.length === 0 && (
            <div className="muted small pad-top">
              {data ? 'nenhum device registrado ainda.' : 'carregando…'}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
