import { useState } from 'react'
import { Link } from 'react-router'
import { ChevronDown, CornerDownRight } from 'lucide-react'
import type { ExistingRule, NormPreview, Normalization } from '../../api/contracts'
import { cn } from '../../lib/cn'
import { paths } from '../../lib/paths'
import { ruleLabel } from '../../lib/process'
import { Button } from '../shell/Controls'
import { ErrorNotice } from '../shell/Notice'
import { StatusBadge } from '../shell/StatusBadge'
import { t } from '../../i18n'

type Check = NormPreview['norm_rules'][number]['checks'][number]

export type ProposalState = 'open' | 'accepted' | 'discarded' | 'revised'

/**
 * What the normalizer would add, before anything exists: each check says what it does and
 * when it fires, rules that already cover the sentence are named, and the manager keeps
 * or drops each check. Writing in the chat revises it; only Aceptar creates rules.
 */
export function NormProposal({
  processId,
  preview,
  state,
  accepting,
  error,
  onAccept,
  onDiscard,
}: {
  processId: number
  preview: NormPreview
  state: ProposalState
  accepting: boolean
  error?: unknown
  onAccept: (reviewed: Normalization) => void
  onDiscard: () => void
}) {
  const open = state === 'open'
  // Dropped checks, by sentence index and check index.
  const [dropped, setDropped] = useState<Set<string>>(new Set())
  const existing = new Map(preview.existing.map((rule) => [rule.id, rule]))
  const total = preview.norm_rules.reduce((sum, sentence) => sum + sentence.checks.length, 0)
  const kept = total - dropped.size

  const toggle = (key: string) =>
    setDropped((current) => {
      const next = new Set(current)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  const reviewed = (): Normalization => ({
    norm_rules: preview.norm_rules.map((sentence, s) => ({
      ...sentence,
      checks: sentence.checks.filter((_, c) => !dropped.has(`${s}.${c}`)),
    })),
  })

  return (
    <div
      className={cn(
        'overflow-hidden rounded-[16px] bg-surface ring-1 ring-line',
        !open && 'opacity-70',
      )}
    >
      <div className="flex items-center justify-between gap-3 border-b border-hairline px-4 py-2.5">
        <p className="text-[13px] font-medium text-ink">
          {total === 0
            ? 'Nada nuevo que añadir'
            : `${total} regla${total === 1 ? '' : 's'} nueva${total === 1 ? '' : 's'}`}
        </p>
        <span className="font-mono text-[11px] text-faint">{STATE_LABEL[state]}</span>
      </div>

      <ol className="divide-y divide-hairline">
        {preview.norm_rules.map((sentence, s) => (
          <li key={s} className="space-y-2 px-4 py-3">
            <p className="text-[12px] italic leading-5 text-muted">«{sentence.text}»</p>

            {sentence.checks.map((check, c) => (
              <CheckRow
                key={c}
                check={check}
                kept={!dropped.has(`${s}.${c}`)}
                editable={open}
                onToggle={() => toggle(`${s}.${c}`)}
              />
            ))}

            {sentence.covered.map((id) => (
              <Covered key={id} processId={processId} id={id} rule={existing.get(id)} />
            ))}

            {sentence.policies.map((policy) => (
              <p key={policy} className="text-[11.5px] leading-5 text-faint">
                Criterio: {policy}
              </p>
            ))}
          </li>
        ))}
      </ol>

      {open ? (
        <div className="space-y-2 border-t border-hairline px-4 py-3">
          {error ? <ErrorNotice error={error} /> : null}
          <div className="flex items-center justify-between gap-3">
            <p className="text-[11.5px] text-faint">Escribe abajo para ajustarla.</p>
            <div className="flex items-center gap-1.5">
              <Button tone="ghost" disabled={accepting} onClick={onDiscard}>
                Descartar
              </Button>
              <Button
                tone="primary"
                disabled={accepting || kept === 0}
                onClick={() => onAccept(reviewed())}
              >
                {accepting ? 'Creando…' : kept === 1 ? 'Aceptar 1 regla' : `Aceptar ${kept} reglas`}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}

const STATE_LABEL: Record<ProposalState, string> = {
  open: 'propuesta',
  accepted: 'aceptada',
  discarded: 'descartada',
  revised: 'revisada abajo',
}

/** One check: what it asks for, when it fires and with what, how the norm was read. */
function CheckRow({
  check,
  kept,
  editable,
  onToggle,
}: {
  check: Check
  kept: boolean
  editable: boolean
  onToggle: () => void
}) {
  const [open, setOpen] = useState(false)
  const fires =
    check.type === 'requirement' ? 'Salta cuando no se cumple' : 'Salta cuando ocurre'

  return (
    <div className={cn('rounded-[12px] bg-canvas px-3 py-2.5 ring-1 ring-line', !kept && 'opacity-45')}>
      <div className="flex items-start gap-2.5">
        <input
          type="checkbox"
          checked={kept}
          disabled={!editable}
          onChange={onToggle}
          aria-label={kept ? 'Quitar de la propuesta' : 'Incluir en la propuesta'}
          className="mt-1 h-3.5 w-3.5 shrink-0 accent-ink"
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <p className={cn('text-[13px] font-medium leading-5 text-ink', !kept && 'line-through')}>
              {ruleLabel(check)}
            </p>
            <StatusBadge value={check.decision} className="shrink-0" />
          </div>
          <p className="mt-0.5 text-[12px] leading-5 text-muted">
            {fires} → {check.decision.replaceAll('_', ' ')}
            {check.kind === 'doubt' ? ' · lo decide una persona' : ''}
          </p>
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((next) => !next)}
            className="mt-1 inline-flex items-center gap-1 text-[11.5px] text-faint hover:text-ink"
          >
            Qué comprueba exactamente
            <ChevronDown
              size={11}
              strokeWidth={1.75}
              className={cn('transition-transform', open && 'rotate-180')}
            />
          </button>
          {open ? (
            <div className="mt-1.5 space-y-1.5 text-[12px] leading-5">
              <p className="text-ink">{check.text}</p>
              <p className="text-muted">{check.interpretation}</p>
              {check.quote ? <p className="text-faint">«{check.quote}»</p> : null}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}

function Covered({
  processId,
  id,
  rule,
}: {
  processId: number
  id: number
  rule: ExistingRule | undefined
}) {
  return (
    <Link
      to={paths.rule(processId, id)}
      className="flex items-center gap-2 rounded-[12px] px-3 py-2 text-[12px] ring-1 ring-dashed ring-line hover:bg-canvas"
    >
      <CornerDownRight size={12} strokeWidth={1.6} className="shrink-0 text-faint" />
      <span className="shrink-0 text-muted">Ya existe</span>
      <span className="min-w-0 flex-1 truncate text-ink">
        {rule ? ruleLabel(rule) : `Regla ${id}`}
      </span>
      {rule ? (
        <span className="shrink-0 font-mono text-[10px] text-faint">{t(`ruleStatus.${rule.status}`)}</span>
      ) : null}
    </Link>
  )
}
