import { useId } from 'react'

// Grafico de area ao vivo (rx/tx em Mbps) alimentado pelo buffer de
// snapshots do SSE (~60s). Estica p/ preencher o card via
// preserveAspectRatio="none" — strokes usam non-scaling-stroke.

interface Props {
  rx: (number | null)[] // bps
  tx: (number | null)[] // bps
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

// p95 dos valores — escala pelo percentil (spike corta no topo em vez de
// achatar a linha inteira no rodape); fallback p/ max se serie vazia
function p95(vals: number[]): number {
  const xs = vals.filter((v) => v > 0).sort((a, b) => a - b)
  return xs.length ? xs[Math.min(xs.length - 1, Math.ceil(0.95 * xs.length) - 1)] : 0
}

export default function LiveAreaChart({ rx, tx }: Props) {
  const gid = useId().replace(/:/g, '')
  const n = Math.max(rx.length, tx.length, 2)
  const x = (i: number) => PAD.l + (i / (n - 1)) * (W - PAD.l - PAD.r)

  const all = [...rx, ...tx].map((v) => (v ?? 0) / 1e6)
  const maxV = niceCeil(Math.max(p95(all) * 1.2, 0.001))
  const y = (mbps: number) =>
    PAD.t + (1 - Math.min(mbps, maxV) / maxV) * (H - PAD.t - PAD.b)

  const line = (vals: (number | null)[]) =>
    vals
      .map(
        (v, i) =>
          `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y((v ?? 0) / 1e6).toFixed(1)}`,
      )
      .join(' ')
  const area = (vals: (number | null)[]) =>
    `${line(vals)} L${x(n - 1).toFixed(1)},${(H - PAD.b).toFixed(1)} ` +
    `L${x(0).toFixed(1)},${(H - PAD.b).toFixed(1)} Z`

  const yTicks = [0.25, 0.5, 0.75].map((f) => f * maxV)

  return (
    <svg
      className="chart-svg"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      role="img"
    >
      <defs>
        <linearGradient id={`rxg${gid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#7fd4ff" stopOpacity="0.32" />
          <stop offset="100%" stopColor="#7fd4ff" stopOpacity="0.02" />
        </linearGradient>
        <linearGradient id={`txg${gid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#3aa0ff" stopOpacity="0.18" />
          <stop offset="100%" stopColor="#3aa0ff" stopOpacity="0.01" />
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
      <path d={area(rx)} fill={`url(#rxg${gid})`} />
      <path d={area(tx)} fill={`url(#txg${gid})`} />
      <path className="c-rx" d={line(rx)} vectorEffect="non-scaling-stroke" />
      <path className="c-tx" d={line(tx)} vectorEffect="non-scaling-stroke" />
      <text className="c-tick" x={W - PAD.r} y={PAD.t + 1} textAnchor="end">
        {maxV >= 10 ? maxV.toFixed(0) : maxV.toFixed(1)} mbps
      </text>
    </svg>
  )
}
