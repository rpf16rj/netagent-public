import { useId } from 'react'

// Grafico de serie unica (linha + area) — mesmo visual do LiveAreaChart
// mas generico p/ qualquer metrica (rtt, cpu...). Suporta uma linha
// tracejada de baseline (ex.: mediana de 7d) e rotulo de unidade.

interface Props {
  points: (number | null)[]
  unit?: string // sufixo do tick de topo (ex.: "ms", "%")
  color?: string
  baseline?: number | null // linha horizontal tracejada
}

const W = 200
const H = 72
const PAD = { l: 2, r: 2, t: 8, b: 4 }

function niceCeil(v: number): number {
  if (v <= 0) return 1
  const exp = Math.floor(Math.log10(v))
  const base = Math.pow(10, exp)
  for (const m of [1, 2, 2.5, 5, 10]) {
    if (v <= m * base) return m * base
  }
  return 10 * base
}

// p95 — escala pelo percentil (spike corta no topo, nao achata a linha)
function p95(vals: number[]): number {
  const xs = vals.filter((v) => v > 0).sort((a, b) => a - b)
  return xs.length ? xs[Math.min(xs.length - 1, Math.ceil(0.95 * xs.length) - 1)] : 0
}

export default function SeriesChart({
  points,
  unit = '',
  color = 'var(--warn)',
  baseline = null,
}: Props) {
  const gid = useId().replace(/:/g, '')
  const n = Math.max(points.length, 2)
  const x = (i: number) => PAD.l + (i / (n - 1)) * (W - PAD.l - PAD.r)

  const maxV = niceCeil(
    Math.max(
      baseline ?? 0,
      p95(points.map((v) => v ?? 0)) * 1.2,
      0.001,
    ),
  )
  const y = (v: number) =>
    PAD.t + (1 - Math.min(v, maxV) / maxV) * (H - PAD.t - PAD.b)

  let d = ''
  let open = false
  for (let i = 0; i < points.length; i++) {
    const v = points[i]
    if (v == null) {
      open = false
      continue
    }
    d += `${open ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`
    open = true
  }
  const area = d
    ? `${d} L${x(points.length - 1).toFixed(1)},${(H - PAD.b).toFixed(1)} ` +
      `L${x(0).toFixed(1)},${(H - PAD.b).toFixed(1)} Z`
    : ''

  const yTicks = [0.25, 0.5, 0.75].map((f) => f * maxV)

  return (
    <svg
      className="chart-svg"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      role="img"
    >
      <defs>
        <linearGradient id={`sg${gid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.26" />
          <stop offset="100%" stopColor={color} stopOpacity="0.02" />
        </linearGradient>
      </defs>
      {yTicks.map((v, i) => (
        <line
          key={i}
          className="c-grid"
          x1={PAD.l}
          x2={W - PAD.r}
          y1={y(v)}
          y2={y(v)}
        />
      ))}
      <line
        className="c-grid"
        x1={PAD.l}
        x2={W - PAD.r}
        y1={H - PAD.b}
        y2={H - PAD.b}
      />
      {baseline != null && (
        <line
          x1={PAD.l}
          x2={W - PAD.r}
          y1={y(baseline)}
          y2={y(baseline)}
          stroke="var(--ok)"
          strokeWidth={1}
          strokeDasharray="3 3"
          opacity={0.6}
          vectorEffect="non-scaling-stroke"
        />
      )}
      {area && <path d={area} fill={`url(#sg${gid})`} />}
      {d && (
        <path d={d} fill="none" stroke={color} strokeWidth={1.8} vectorEffect="non-scaling-stroke" />
      )}
      <text className="c-tick" x={W - PAD.r} y={PAD.t + 1} textAnchor="end">
        {`${maxV >= 10 ? maxV.toFixed(0) : maxV.toFixed(1)} ${unit}`.trim()}
      </text>
    </svg>
  )
}
