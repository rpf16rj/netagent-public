import { useEffect, useState } from 'react'
import { api } from './api'
import type { HistoryResp } from './types'

// Ponto normalizado p/ os graficos (backend emite 1 rollup/min; aqui a
// serie e decimada p/ <= MAX_PTS mantendo os paths SVG leves em 7d).
export interface HistPoint {
  t: number // epoch s do bucket
  cpu: number | null
  wanRx: number | null // bps
  wanTx: number | null // bps
  rtt: number | null // ms
  // series por link (idx) — link_rx_<i>/link_tx_<i>/link_rtt_<i> rollups
  linkRx?: Record<string, number | null>
  linkTx?: Record<string, number | null>
  linkRtt?: Record<string, number | null>
}

const TTL = 60_000 // ms — cache <60s e reutilizado p/ todos os consumidores
const MAX_PTS = 360

// /api/history devolve TODOS os roteadores numa so chamada — o cache
// modulo-level e chaveado por `hours` e evita refetch em remounts (troca
// de aba casa mae/casa user/rede geral); `inflight` deduplica fetches
// simultaneos dos varios cards montados juntos.
const cache = new Map<number, { resp: HistoryResp; at: number }>()
let inflight: { hours: number; p: Promise<HistoryResp> } | null = null

function fetchHistory(hours: number): Promise<HistoryResp> {
  const hit = cache.get(hours)
  if (hit && Date.now() - hit.at < TTL) return Promise.resolve(hit.resp)
  if (inflight && inflight.hours === hours) return inflight.p
  const p = api
    .history(hours)
    .then((resp) => {
      cache.set(hours, { resp, at: Date.now() })
      return resp
    })
    .finally(() => {
      if (inflight?.p === p) inflight = null
    })
  inflight = { hours, p }
  return p
}

const avgOf = (vals: (number | null)[]): number | null => {
  const xs = vals.filter((v): v is number => v != null)
  return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null
}

function normalize(resp: HistoryResp | null, router: string): HistPoint[] {
  const rows = resp?.series?.[router] ?? []
  const pts: HistPoint[] = rows.map((r) => {
    const linkRx: Record<string, number | null> = {}
    const linkTx: Record<string, number | null> = {}
    const linkRtt: Record<string, number | null> = {}
    for (const k of Object.keys(r)) {
      if (k.startsWith('link_rx_')) linkRx[k.slice(8)] = (r as never)[k]
      else if (k.startsWith('link_tx_'))
        linkTx[k.slice(8)] = (r as never)[k]
      else if (k.startsWith('link_rtt_'))
        linkRtt[k.slice(9)] = (r as never)[k]
    }
    return {
      t: r.ts_min,
      cpu: r.cpu_avg,
      wanRx: r.wan_rx_bps_avg,
      wanTx: r.wan_tx_bps_avg,
      rtt: r.rtt_ms_avg,
      linkRx, linkTx, linkRtt,
    }
  })
  if (pts.length <= MAX_PTS) return pts
  const out: HistPoint[] = []
  const bucket = pts.length / MAX_PTS
  for (let i = 0; i < MAX_PTS; i++) {
    const slice = pts.slice(Math.floor(i * bucket), Math.floor((i + 1) * bucket))
    if (!slice.length) continue
    const last = slice[slice.length - 1]
    out.push({
      t: last.t,
      cpu: avgOf(slice.map((p) => p.cpu)),
      wanRx: avgOf(slice.map((p) => p.wanRx)),
      wanTx: avgOf(slice.map((p) => p.wanTx)),
      rtt: avgOf(slice.map((p) => p.rtt)),
      linkRx: last.linkRx, linkTx: last.linkTx, linkRtt: last.linkRtt,
    })
  }
  return out
}

// Serie de rollups (1min) de um roteador p/ o periodo `hours`.
// - hidrata de forma sincrona a partir do cache (remount nao zera o grafico)
// - busca no mount e a cada mudanca de `hours`
// - refresh a cada 60s (uma so request compartilhada entre consumidores)
export function useHistorySeries(router: string, hours: number) {
  const [st, setSt] = useState<{
    hours: number
    points: HistPoint[]
    loading: boolean
  }>(() => {
    const resp = cache.get(hours)?.resp ?? null
    const points = normalize(resp, router)
    return { hours, points, loading: points.length === 0 }
  })
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    let off = false
    const apply = (resp: HistoryResp | null) => {
      if (off) return
      setSt({
        hours,
        points: normalize(resp, router),
        loading: false,
      })
      setError(null)
    }
    const load = () => {
      fetchHistory(hours)
        .then(apply)
        .catch((e) => {
          if (off) return
          setError(e)
          setSt((s) => ({ ...s, loading: false }))
        })
    }
    // re-semear sincrono p/ o periodo pedido (cache ou vazio) antes do fetch —
    // evita exibir serie do periodo anterior ate a resposta chegar
    apply(cache.get(hours)?.resp ?? null)
    load()
    const t = setInterval(() => {
      cache.delete(hours) // invalida p/ forcar refresh do periodo ativo
      load()
    }, TTL)
    return () => {
      off = true
      clearInterval(t)
    }
  }, [router, hours])

  // entre a mudanca de `hours` e o effect rodar, ja devolve o cache do
  // periodo pedido (nunca serie stale do periodo anterior)
  const points =
    st.hours === hours
      ? st.points
      : normalize(cache.get(hours)?.resp ?? null, router)
  return { points, loading: st.loading && points.length === 0, error }
}
