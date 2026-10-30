// Mini sparkline SVG — linha fina com ponto no valor mais recente.
// O wrapper controla o tamanho (svg estica com preserveAspectRatio=none).

interface Props {
  points: (number | null)[]
  color?: string
  max?: number
  baseline?: number | null // linha horizontal tracejada (ex.: mediana 7d)
}

const W = 120
const H = 24

export default function Sparkline({
  points,
  color = 'var(--ice)',
  max,
  baseline = null,
}: Props) {
  const vals = points.filter((v): v is number => v != null)
  const yMax = Math.max(1e-9, max ?? 0, baseline ?? 0, ...vals)
  const n = Math.max(points.length, 2)
  const x = (i: number) => 2 + (i / (n - 1)) * (W - 4)
  const y = (v: number) => 3 + (1 - Math.min(v, yMax) / yMax) * (H - 6)

  let d = ''
  let open = false
  let last: { x: number; y: number } | null = null
  for (let i = 0; i < points.length; i++) {
    const v = points[i]
    if (v == null) {
      open = false
      continue
    }
    d += `${open ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`
    open = true
    last = { x: x(i), y: y(v) }
  }

  return (
    <svg
      className="spark-svg"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      role="img"
    >
      {baseline != null && (
        <line
          x1={2}
          x2={W - 2}
          y1={y(baseline)}
          y2={y(baseline)}
          stroke={color}
          strokeWidth={0.8}
          strokeDasharray="3 3"
          opacity={0.55}
          vectorEffect="non-scaling-stroke"
        />
      )}
      {d && (
        <path
          className="spark-line"
          d={d}
          stroke={color}
          vectorEffect="non-scaling-stroke"
        />
      )}
      {last && (
        <circle
          className="spark-dot"
          cx={last.x}
          cy={last.y}
          r={2.2}
          fill={color}
          style={{ color }}
        />
      )}
    </svg>
  )
}
