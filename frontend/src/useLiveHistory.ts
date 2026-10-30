import { useEffect, useMemo, useRef, useState } from 'react'
import type { Snapshot } from './types'

export interface TimedSnapshot {
  t: number
  snap: Snapshot
}

// Buffer circular dos ultimos snapshots (~3s cada, via SSE) p/ os graficos
// "ao vivo" — janela de ~2min sem polling extra.
export function useLiveHistory(
  snapshot: Snapshot | null,
  max = 45,
): TimedSnapshot[] {
  const [history, setHistory] = useState<TimedSnapshot[]>([])
  const lastTs = useRef(0)

  useEffect(() => {
    if (!snapshot?.ts || snapshot.ts === lastTs.current) return
    lastTs.current = snapshot.ts
    setHistory((h) => [...h, { t: snapshot.ts, snap: snapshot }].slice(-max))
  }, [snapshot, max])

  return history
}

// ---------------------------------------------------------------------------
// useLiveSeries — buffer ao vivo persistido em localStorage p/ metricas que
// NAO tem rollup no backend (conn_count, memoria). Guarda os ultimos `max`
// pontos {t,v} por chave e hidrata no mount, entao um reload nao zera o
// sparkline. A escrita acontece a cada tick do SSE (ts novo).

interface LSPoint {
  t: number
  v: number | null
}

// espelho em memoria do localStorage (uma leitura por chave por sessao)
const lsBuf = new Map<string, LSPoint[]>()

function loadSeries(key: string): LSPoint[] {
  const hit = lsBuf.get(key)
  if (hit) return hit
  let pts: LSPoint[] = []
  try {
    const raw = localStorage.getItem(`netagent:${key}`)
    const arr = raw ? JSON.parse(raw) : []
    if (Array.isArray(arr))
      pts = arr.filter((p) => p && typeof p.t === 'number')
  } catch {
    // storage indisponivel/corrompido — segue so com buffer em memoria
  }
  lsBuf.set(key, pts)
  return pts
}

export function useLiveSeries(
  key: string,
  value: number | null,
  ts: number | undefined,
  max = 120,
): (number | null)[] {
  const [pts, setPts] = useState<LSPoint[]>(() => loadSeries(key))
  const lastTs = useRef(pts[pts.length - 1]?.t ?? 0)

  // chave mudou (ex.: outro roteador) — reidrata do storage/memoria
  useEffect(() => {
    const loaded = loadSeries(key)
    lastTs.current = loaded[loaded.length - 1]?.t ?? 0
    setPts(loaded)
  }, [key])

  useEffect(() => {
    if (!ts || ts <= lastTs.current) return
    const buf = loadSeries(key)
    // outro consumidor ja gravou esse tick (mesma chave, ex.: conn do router
    // no RouterCard e no HistoryPanel) — so sincroniza, nao duplica
    if (buf[buf.length - 1]?.t === ts) {
      lastTs.current = ts
      setPts(buf)
      return
    }
    if (ts <= (buf[buf.length - 1]?.t ?? 0)) return
    lastTs.current = ts
    const next = [...buf, { t: ts, v: value }].slice(-max)
    lsBuf.set(key, next)
    try {
      localStorage.setItem(`netagent:${key}`, JSON.stringify(next))
    } catch {
      // quota/storage cheio — perde a persistencia, nao o grafico
    }
    setPts(next)
  }, [key, value, ts, max])

  return useMemo(() => pts.map((p) => p.v), [pts])
}
