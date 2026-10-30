import { useEffect, useState } from 'react'
import { api } from '../api'
import type { WifiResp } from '../types'

// Painel Wi-Fi — AP Tenda RX2 Pro (LAN da Casa User). Badge de
// alcancabilidade + clientes conectados (host/ip/banda/sinal) + canais.
// Sem TENDA_PASS ou auth falha -> nota muted em vez de erro feio.
export default function WifiPanel() {
  const [data, setData] = useState<WifiResp | null>(null)
  const [q, setQ] = useState('')

  useEffect(() => {
    const load = () =>
      api
        .wifi()
        .then(setData)
        .catch(() => {})
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [])

  const needle = q.trim().toLowerCase()
  const clients = (data?.clients ?? []).filter(
    (c) =>
      !needle ||
      (c.hostname || '').toLowerCase().includes(needle) ||
      (c.ip || '').includes(needle) ||
      (c.mac || '').toLowerCase().includes(needle),
  )
  const meta = data?.meta ?? {}
  const badge = !data
    ? { cls: 'proposed', txt: '…' }
    : !data.reachable
      ? { cls: 'bad', txt: 'offline' }
      : data.error === 'auth'
        ? { cls: 'warn', txt: 'auth' }
        : { cls: 'ok', txt: 'online' }

  const hasMeta =
    meta.ssid != null ||
    meta.ssid_5 != null ||
    meta.channel_24 != null ||
    meta.channel_5 != null

  return (
    <div className="card">
      <div className="card-title">
        <span>wi-fi · tenda rx2</span>
        <span className={`badge badge-${badge.cls}`}>{badge.txt}</span>
      </div>
      <input
        className="panel-search"
        placeholder="filtrar cliente…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      <div className="card-body scroll-y">
        {hasMeta && (
          <div className="link-meta pad-top">
            {[meta.ssid, meta.ssid_5].filter(Boolean).join(' · ')}
            {meta.channel_24 != null || meta.channel_5 != null
              ? ` · ch ${meta.channel_24 ?? '—'}/${meta.channel_5 ?? '—'}`
              : ''}
          </div>
        )}
        {clients.map((c, i) => (
          <div className="wifi-row" key={c.mac || c.ip || i}>
            <span className="wifi-host mono">{c.hostname || c.mac || '—'}</span>
            <span className="wifi-ip">{c.ip || ''}</span>
            <span className="wifi-band">{c.band || ''}</span>
            <span className="talker-val">
              {c.rssi != null ? `${c.rssi} dBm` : (c.signal ?? '')}
            </span>
          </div>
        ))}
        {clients.length === 0 && (
          <div className="muted small pad-top">
            {data
              ? data.error === 'auth'
                ? 'login falhou no webui do AP.'
                : 'sem clientes listados.'
              : 'carregando…'}
          </div>
        )}
        {data?.note && (
          <div className="muted mini pad-top">
            {data.note === 'senha nao configurada'
              ? 'configure TENDA_PASS no backend'
              : data.note}
          </div>
        )}
      </div>
    </div>
  )
}
