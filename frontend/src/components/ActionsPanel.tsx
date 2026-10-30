import { useState } from 'react'
import { api, fmtTs } from '../api'
import type { ActionItem } from '../types'

interface Props {
  actions: ActionItem[]
  readOnly: boolean
  onChanged: (a: ActionItem) => void
  onKillswitch?: (v: boolean) => void
  onClose?: () => void
}

const STATUS_LABEL: Record<string, string> = {
  proposed: 'proposta',
  approved: 'aprovada',
  executed: 'executada',
  rolledback: 'rollback',
  failed: 'falhou',
  rejected: 'rejeitada',
}

// Painel de acoes — aberto via botao "acoes" da topbar (modal), ja que o
// agente esta desativado e nao merece espaco fixo no dashboard.
export default function ActionsPanel({
  actions,
  readOnly,
  onChanged,
  onKillswitch,
  onClose,
}: Props) {
  const [busy, setBusy] = useState<number | null>(null)
  const [err, setErr] = useState('')

  async function act(id: number, fn: (id: number) => Promise<ActionItem>) {
    setBusy(id)
    setErr('')
    try {
      onChanged(await fn(id))
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="card">
      <div className="card-title">
        <span>ações do agente</span>
        <span className="title-sub">
          {readOnly ? 'read-only' : 'escrita liberada'}
        </span>
      </div>
      <div className="card-body feed">
        {err && <div className="error small pad-top">{err}</div>}
        {actions.length === 0 && (
          <div className="muted small pad-top">
            Nenhuma ação registrada. O agente está desativado — propostas
            manuais ou automáticas apareceriam aqui com o diff para aprovação.
          </div>
        )}
        {actions.map((a) => (
          <div key={a.id} className="action-item">
            <div className="action-head">
              <span className={`badge badge-${a.status}`}>
                {STATUS_LABEL[a.status] ?? a.status}
              </span>
              <b>{a.kind}</b>
              <span className="muted small">
                #{a.id} · {a.router} · {fmtTs(a.ts)}
              </span>
            </div>
            <div className="payload-line">{JSON.stringify(a.payload)}</div>
            {a.diff.length > 0 && (
              <pre className="diff">
                {a.diff.map((s) => s.join(' ')).join('\n')}
              </pre>
            )}
            {a.result && <div className="small">{a.result}</div>}
            {a.status === 'proposed' && (
              <div className="action-btns">
                <button
                  className="btn btn-ok"
                  disabled={busy === a.id || readOnly}
                  onClick={() => act(a.id, api.approve)}
                >
                  Aprovar
                </button>
                <button
                  className="btn"
                  disabled={busy === a.id}
                  onClick={() => act(a.id, api.reject)}
                >
                  Rejeitar
                </button>
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="action-foot">
        {onKillswitch && (
          <button
            className={readOnly ? 'btn btn-danger' : 'btn'}
            onClick={() => onKillswitch(!readOnly)}
            title="Kill-switch: bloqueia qualquer escrita nos roteadores"
          >
            {readOnly ? 'kill-switch: on' : 'kill-switch: off'}
          </button>
        )}
        {onClose && (
          <button className="btn" onClick={onClose}>
            fechar
          </button>
        )}
      </div>
    </div>
  )
}
