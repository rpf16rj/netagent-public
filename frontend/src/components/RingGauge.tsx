// Gauge circular estilo JARVIS — arco de 270° com anel de ticks e
// dois aneis tracejados decorativos girando devagar (CSS animation).

const C = 60
const START = 135 // graus — gap fica embaixo
const SWEEP = 270

function polar(cx: number, cy: number, r: number, deg: number) {
  const a = (deg * Math.PI) / 180
  return { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) }
}

function arcPath(r: number, a0: number, a1: number) {
  const s = polar(C, C, r, a0)
  const e = polar(C, C, r, a1)
  const large = a1 - a0 > 180 ? 1 : 0
  return `M ${s.x.toFixed(2)} ${s.y.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${e.x.toFixed(2)} ${e.y.toFixed(2)}`
}

interface Props {
  value: number | null // 0..100
  label: string
  color: string
}

export default function RingGauge({ value, label, color }: Props) {
  const pct = value == null ? 0 : Math.max(0, Math.min(100, value))
  const ticks = []
  for (let i = 0; i <= 30; i++) {
    const a = START + (SWEEP * i) / 30
    const major = i % 10 === 0
    const p1 = polar(C, C, major ? 46.5 : 48.5, a)
    const p2 = polar(C, C, 52, a)
    ticks.push(
      <line
        key={i}
        className={major ? 'major' : undefined}
        x1={p1.x}
        y1={p1.y}
        x2={p2.x}
        y2={p2.y}
      />,
    )
  }
  const valEnd = START + (SWEEP * pct) / 100

  return (
    <svg className="gauge-svg" viewBox="0 0 120 120" role="img" aria-label={label}>
      <circle className="g-ring" cx={C} cy={C} r={57} />
      <circle className="g-ring2" cx={C} cy={C} r={55} />
      <g className="g-ticks">{ticks}</g>
      <path className="g-track" d={arcPath(40, START, START + SWEEP)} />
      {value != null && pct > 0 && (
        <path
          className="g-val"
          d={arcPath(40, START, valEnd)}
          stroke={color}
          style={{ color }}
        />
      )}
      <text className="g-num" x={C} y={C - 1} textAnchor="middle">
        {value == null ? '—' : `${Math.round(pct)}%`}
      </text>
      <text className="g-lab" x={C} y={C + 15} textAnchor="middle">
        {label}
      </text>
    </svg>
  )
}
