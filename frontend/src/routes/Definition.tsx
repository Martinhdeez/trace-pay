import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowUp, BookOpenText, Check, ChevronDown, Hammer, Paperclip, Plus, X } from 'lucide-react'
import { api, ApiError } from '../api/client'
import { families, keys } from '../api/queries'
import type {
  CreatedCheck,
  NormPreview,
  Normalization,
  DiscoverySession,
  Finding,
  Proposal,
  ProcessDetail,
  Rule,
  RuleStatus as RuleStatusCode,
  SymbolIn,
  SymbolIO,
  ValidationReport,
  VersionOut,
} from '../api/contracts'
import { DefinitionSwitch } from '../components/process/DefinitionSwitch'
import {
  FileChip,
  revokePreview,
  toPreview,
  type FilePreview,
} from '../components/process/FileChip'
import { ProcessScreen } from '../components/process/ProcessScreen'
import { TruthSources } from '../components/process/TruthSources'
import { ValidationImpact } from '../components/process/ValidationImpact'
import { Button, Input, Select, Textarea } from '../components/shell/Controls'
import { ErrorNotice, Empty, EmptyState, Notice } from '../components/shell/Notice'
import { TerminalLoader } from '../components/shell/TerminalLoader'
import { NormProposal, type ProposalState } from '../components/process/NormProposal'
import { ExpandableText } from '../components/shell/ExpandableText'
import { NestedCard } from '../components/shell/Well'
import { t } from '../i18n'
import { cn } from '../lib/cn'
import { definitionTabFromPath } from '../lib/definitionTabs'
import { formatRunDate } from '../lib/format'
import { paths } from '../lib/paths'
import { ruleLabel } from '../lib/process'
import { useSession } from '../state/session'

type Attachment = FilePreview & { file: File }

/** One exchange: what the manager wrote, and what came back. */
type Turn = {
  id: string
  prompt: string
  files: Attachment[]
  /** The chat's answer (discuss mode). */
  answer?: string
  /** The rules an accepted proposal created; each one compiles in the background. */
  checks?: CreatedCheck[]
  /** Normas: the norm this proposal reads, kept so a later message can revise it. */
  norm?: string
  preview?: NormPreview
  state?: ProposalState
  /** Attachments that were not uploaded, because only Excel teaches a process. */
  skipped?: string[]
  error?: unknown
}

const PANE_CHAT = {
  normas: {
    title: 'Empieza por la norma',
    intro: 'Pega la norma de tu empresa o escribe una regla. La convierto en comprobaciones y tú decides cuáles entran.',
    chips: [
      'El IBAN debe coincidir con el del maestro de proveedores',
      'Si falta el pedido, se escala',
      'El NIF del emisor debe estar dado de alta',
    ],
    placeholder: 'Pega una norma. Te propongo las reglas y tú decides.',
    proposals: 'Propuesta',
  },
  contexto: {
    title: 'Pregunta sobre el proceso',
    intro: 'Qué decide, por qué escala un caso o qué reglas usan un dato.',
    chips: ['¿Por qué se escalan estos casos?', '¿Qué reglas usan el IBAN?', '¿Qué falta para decidir?'],
    placeholder: 'Pregunta al asistente sobre este proceso.',
    proposals: 'Respuesta',
  },
  inputs: {
    title: 'Los datos de cada documento',
    intro: 'Pregunta qué dato falta para una regla o de dónde sale uno.',
    chips: ['issuer_nif', 'iban', 'purchase_order'],
    placeholder: '¿Qué símbolo falta para esta regla? issuer_nif, iban…',
    proposals: 'Respuesta',
  },
  fuentes: {
    title: 'Las tablas de referencia',
    intro: 'Adjunta un Excel o pregunta contra qué se comprueba cada documento.',
    chips: ['Maestro de proveedores', 'Pedidos abiertos', 'Parámetros del ERP'],
    placeholder: 'Adjunta un Excel o pregunta por las fuentes.',
    proposals: 'Respuesta',
  },
} as const

const EXCEL = /\.xlsx$/i

const NORM_VERBS = ['leyendo la norma', 'buscando reglas que ya existen', 'separando comprobaciones', 'redactando la propuesta']
const CHAT_VERBS = ['leyendo el proceso', 'buscando en las reglas', 'redactando la respuesta']

/** The conversation for this process: the one still open, or a new one. */
async function processConversation(processId: number, name: string): Promise<DiscoverySession> {
  const open = (await api.listDiscoverySessions()).find(
    (item) => item.process_id === processId && item.published_process_id == null,
  )
  if (!open) return api.startDiscoverySession(processId, name)
  // Its revision is enough to post; the next answer brings the whole conversation back.
  return { id: open.id, revision: open.revision } as DiscoverySession
}

function lastAnswer(session: DiscoverySession): string {
  const message = [...session.messages].reverse().find((item) => item.role === 'assistant')
  return typeof message?.text === 'string' ? message.text : ''
}

/**
 * The draft is where every edit goes. `GET /execution` says whether one exists
 * (`revision`), so the console never asks for a draft that is not there.
 */
function useDraft(processId: number, enabled = true) {
  const execution = useQuery({
    queryKey: keys.execution(processId),
    queryFn: () => api.getExecution(processId),
    enabled,
  })
  const revision = execution.data?.revision ?? null
  const draft = useQuery({
    queryKey: keys.draft(processId),
    queryFn: () => api.getDraft(processId),
    enabled: enabled && revision != null,
  })
  return {
    revision,
    snapshot: revision != null ? draft.data?.snapshot : undefined,
    pending: execution.isPending,
  }
}

/** A stale revision means someone else edited the draft. */
function draftError(error: unknown): unknown {
  return error instanceof ApiError && error.status === 409
    ? new ApiError(409, error.code, 'Recarga: alguien cambió el borrador')
    : error
}

export function Definition() {
  const processId = Number(useParams().processId)
  const location = useLocation()
  const pane = definitionTabFromPath(location.pathname)
  const chat = PANE_CHAT[pane]
  const queryClient = useQueryClient()
  const [turns, setTurns] = useState<Turn[]>([])
  const [draft, setDraft] = useState('')
  const [focusTick, setFocusTick] = useState(0)
  const session = useRef<{ id: number; revision: number } | null>(null)

  useEffect(() => {
    setDraft('')
  }, [pane])

  const process = useQuery({
    queryKey: keys.process(processId),
    queryFn: () => api.getProcess(processId),
  })
  const findings = useQuery({
    queryKey: keys.findings(processId),
    queryFn: () => api.listFindings(processId),
  })
  const versions = useQuery({
    queryKey: keys.versions(processId),
    queryFn: () => api.listVersions(processId),
  })
  const rules = useQuery({
    queryKey: keys.rules(processId),
    queryFn: () => api.listRules(processId),
    refetchInterval: (query) =>
      query.state.data?.some((rule) => rule.status === 'compiling') ? 2_000 : false,
  })

  // Chat and learning proposals wait here; escalation ones live on their case in Revisión.
  const proposals = useQuery({
    queryKey: keys.proposals(processId, 'open'),
    queryFn: () => api.listProposals(processId, 'open'),
    select: (items) => items.filter((item) => item.channel !== 'escalation'),
  })
  const inbox = proposals.data ?? []

  const all = rules.data ?? []
  const outcomes = process.data?.decision_types.map((outcome) => outcome.name) ?? []
  const history = [...(versions.data ?? [])].sort((a, b) => b.number - a.number)
  const latestVersion = history[0]
  // A selected version is read only; null shows the live definition panes.
  const [viewing, setViewing] = useState<number | null>(null)
  const viewed = history.find((version) => version.id === viewing)

  // The proposal the next message revises, if one waits for the manager.
  const openProposal =
    pane === 'normas' ? turns.findLast((turn) => turn.preview && turn.state === 'open') : undefined

  /**
   * Normas: the normalizer proposes, nothing is created; a message while a proposal is
   * open revises it. Anywhere else: the process chat, in discuss mode.
   */
  const send = useMutation({
    mutationFn: async ({
      prompt,
      files,
      revising,
    }: {
      prompt: string
      files: Attachment[]
      revising?: Turn
    }): Promise<Partial<Turn>> => {
      if (pane === 'normas') {
        const norm = revising?.norm ?? prompt
        const preview = await api.previewNorm(
          processId,
          revising?.preview
            ? { text: norm, feedback: prompt, previous: { norm_rules: revising.preview.norm_rules } }
            : { text: prompt },
        )
        return { preview, norm, state: 'open' }
      }
      if (!session.current) {
        session.current = await processConversation(processId, process.data?.name ?? 'Definición')
      }
      const excel = files.filter((item) => EXCEL.test(item.name))
      for (const file of excel) {
        const next = await api.uploadDraftWorkbook(session.current.id, session.current.revision, file.file)
        session.current = { id: next.id, revision: next.revision }
      }
      const next = await api.messageDiscoverySession(
        session.current.id,
        session.current.revision,
        prompt,
      )
      session.current = { id: next.id, revision: next.revision }
      return {
        answer: lastAnswer(next),
        skipped: files.filter((item) => !EXCEL.test(item.name)).map((item) => item.name),
      }
    },
    onMutate: ({ prompt, files }) => {
      const id = crypto.randomUUID()
      setTurns((current) => [...current, { id, prompt, files }])
      return { id }
    },
    onSuccess: (result, { revising }, context) => {
      setTurns((current) =>
        current.map((turn) =>
          turn.id === context?.id
            ? { ...turn, ...result }
            : turn.id === revising?.id
              ? { ...turn, state: 'revised' }
              : turn,
        ),
      )
    },
    onError: (error, _vars, context) => {
      // A stale revision: start from the conversation's current one next time.
      session.current = null
      setTurns((current) =>
        current.map((turn) => (turn.id === context?.id ? { ...turn, error } : turn)),
      )
    },
  })

  const settleTurn = (id: string, change: Partial<Turn>) =>
    setTurns((current) => current.map((turn) => (turn.id === id ? { ...turn, ...change } : turn)))

  // Only now do rules exist: the reviewed proposal is created and compiles.
  const accept = useMutation({
    mutationFn: ({ reviewed }: { turnId: string; reviewed: Normalization }) =>
      api.acceptNorm(processId, reviewed),
    onSuccess: (out, { turnId }) => {
      settleTurn(turnId, {
        state: 'accepted',
        checks: out.norm_rules.flatMap((item) => item.checks),
      })
      void queryClient.invalidateQueries({ queryKey: ['rules'] })
      void queryClient.invalidateQueries({ queryKey: keys.norm(processId) })
    },
  })

  return (
    <ProcessScreen
      processId={processId}
      crumbs={[
        { label: 'Procesos', to: paths.processes },
        { label: process.data?.name ?? '…', to: paths.process(processId) },
        { label: 'Definición' },
      ]}
    >
      <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(0,1fr)_minmax(22rem,1fr)]">
        <section className="flex min-h-0 flex-col border-b border-hairline lg:border-b-0 lg:border-r">
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
            {proposals.isError ? <ErrorNotice error={proposals.error} /> : null}
            {turns.length > 0 || inbox.length > 0 ? (
              <ol className="space-y-6">
                {inbox.length > 0 ? (
                  <li className="space-y-3">
                    <p className="text-[12px] text-muted">
                      {inbox.length} propuesta{inbox.length === 1 ? '' : 's'} esperan tu decisión ·
                      aceptar entra al borrador
                    </p>
                    <ul className="space-y-2">
                      {inbox.map((proposal) => (
                        <ProposalCard key={proposal.id} processId={processId} proposal={proposal} />
                      ))}
                    </ul>
                  </li>
                ) : null}
                {turns.map((turn) => (
                  <li key={turn.id} className="space-y-3">
                    <div className="rounded-[16px] bg-surface px-4 py-3 ring-1 ring-line">
                      <p className="text-[13px] leading-6 text-ink">{turn.prompt}</p>
                      {turn.files.length ? (
                        <ul className="mt-2 flex flex-wrap gap-2">
                          {turn.files.map((file) => (
                            <li key={file.id}>
                              <FileChip file={file} />
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </div>
                    {turn.answer === undefined && !turn.preview && !turn.checks && !turn.error ? (
                      <TerminalLoader verbs={pane === 'normas' ? NORM_VERBS : CHAT_VERBS} />
                    ) : (
                      <p className="font-mono text-[11px] tracking-[0.08em] text-faint">
                        {chat.proposals.toUpperCase()}
                      </p>
                    )}
                    {turn.skipped?.length ? (
                      <Notice tone="warning" title="Solo Excel">
                        No se han subido: {turn.skipped.join(', ')}
                      </Notice>
                    ) : null}
                    {turn.error ? <ErrorNotice error={turn.error} /> : null}
                    {turn.answer ? (
                      <div className="rounded-[16px] bg-surface px-4 py-3 ring-1 ring-line">
                        <p className="whitespace-pre-line text-[13px] leading-6 text-ink">
                          {turn.answer}
                        </p>
                      </div>
                    ) : null}
                    {turn.preview && turn.state ? (
                      <NormProposal
                        processId={processId}
                        preview={turn.preview}
                        state={turn.state}
                        accepting={accept.isPending && accept.variables?.turnId === turn.id}
                        error={accept.variables?.turnId === turn.id ? accept.error : undefined}
                        onAccept={(reviewed) => accept.mutate({ turnId: turn.id, reviewed })}
                        onDiscard={() => settleTurn(turn.id, { state: 'discarded' })}
                      />
                    ) : null}
                    {turn.checks ? (
                      <ul className="space-y-2">
                        {turn.checks.map((check) => (
                          <ProposalCard key={check.rule_id} processId={processId} check={check} />
                        ))}
                      </ul>
                    ) : null}
                  </li>
                ))}
              </ol>
            ) : (
              <EmptyState title={chat.title} className="h-full justify-center">
                {chat.intro}
              </EmptyState>
            )}
          </div>

          {turns.length === 0 ? (
            <div className="flex flex-wrap gap-2 px-5 pb-1">
              {chat.chips.map((chip) => (
                <button
                  key={chip}
                  type="button"
                  onClick={() => {
                    setDraft(chip)
                    setFocusTick((tick) => tick + 1)
                  }}
                  className={cn(
                    'rounded-full px-3 py-1.5 text-left text-[12px] ring-1',
                    draft === chip
                      ? 'bg-surface text-ink shadow-lift ring-line'
                      : 'bg-canvas text-ink/75 ring-line hover:bg-surface hover:text-ink',
                  )}
                >
                  {chip}
                </button>
              ))}
            </div>
          ) : null}

          <Composer
            placeholder={
              openProposal ? 'Pide cambios a la propuesta: «quita la del IBAN», «que escale»…' : chat.placeholder
            }
            draft={draft}
            onDraft={setDraft}
            focusTick={focusTick}
            busy={send.isPending}
            onSend={(text, files) => {
              send.mutate({ prompt: text, files, revising: openProposal })
              setDraft('')
            }}
          />
        </section>

        <aside className="flex min-h-0 flex-col">
          <header className="flex shrink-0 items-start justify-between gap-3 border-b border-hairline px-5 py-3">
            <DefinitionSwitch processId={processId} />
            <VersionChip
              versions={history}
              selected={viewed ?? latestVersion}
              onSelect={(version) => setViewing(version.id)}
              findings={findings.data ?? []}
            />
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
            {viewed && latestVersion ? (
              <VersionView
                processId={processId}
                version={viewed}
                current={latestVersion}
                rules={all}
                onBack={() => setViewing(null)}
              />
            ) : null}
            {viewed ? null : pane === 'normas' ? (
              <RulesPane processId={processId} rules={all} outcomes={outcomes} />
            ) : null}
            {!viewed && pane === 'contexto' ? (
              <ContextPane processId={processId} process={process.data} />
            ) : null}
            {!viewed && pane === 'inputs' ? (
              <InputsPane processId={processId} process={process.data} />
            ) : null}
            {!viewed && pane === 'fuentes' ? <TruthSources processId={processId} /> : null}
          </div>
        </aside>
      </div>
    </ProcessScreen>
  )
}

const RULE_STATUS: Record<
  RuleStatusCode,
  { dot: string; ink: string; soft: string; pill: string; ring: string }
> = {
  active: {
    dot: 'bg-pagar',
    ink: 'text-pagar',
    soft: 'bg-pagar-soft',
    pill: 'group-hover:bg-pagar-soft [@media(hover:none)]:bg-pagar-soft',
    ring: 'border-pagar/20 border-t-pagar',
  },
  compiling: {
    dot: 'bg-ocr',
    ink: 'text-ocr',
    soft: 'bg-ocr-soft',
    pill: 'group-hover:bg-ocr-soft [@media(hover:none)]:bg-ocr-soft',
    ring: 'border-ocr/20 border-t-ocr',
  },
  draft: {
    dot: 'bg-faint',
    ink: 'text-muted',
    soft: 'bg-well',
    pill: 'group-hover:bg-well [@media(hover:none)]:bg-well',
    ring: 'border-faint/25 border-t-faint',
  },
  blocked: {
    dot: 'bg-escalar',
    ink: 'text-escalar',
    soft: 'bg-escalar-soft',
    pill: 'group-hover:bg-escalar-soft [@media(hover:none)]:bg-escalar-soft',
    ring: 'border-escalar/20 border-t-escalar',
  },
  retired: {
    dot: 'bg-faint/50',
    ink: 'text-faint',
    soft: 'bg-well',
    pill: 'group-hover:bg-well [@media(hover:none)]:bg-well',
    ring: 'border-faint/20 border-t-faint',
  },
}

function RulesPane({
  processId,
  rules,
  outcomes,
}: {
  processId: number
  rules: Rule[]
  outcomes: string[]
}) {
  const queryClient = useQueryClient()
  const input = useRef<HTMLInputElement>(null)
  const [adding, setAdding] = useState(false)
  const [text, setText] = useState('')
  const [pending, setPending] = useState<Set<number>>(new Set())

  useEffect(() => {
    if (adding) input.current?.focus()
  }, [adding])

  const create = useMutation({
    mutationFn: (text: string) =>
      api.createRule(processId, { text, type: 'requirement', decision: outcomes[0] ?? 'ESCALAR' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['rules'] })
      setText('')
      requestAnimationFrame(() => input.current?.focus())
    },
  })

  const compile = useMutation({
    mutationFn: (id: number) => api.compileRule(id),
    onMutate: (id) => {
      setPending((current) => new Set(current).add(id))
    },
    onSettled: (_data, _error, id) => {
      setPending((current) => {
        const next = new Set(current)
        next.delete(id)
        return next
      })
      void queryClient.invalidateQueries({ queryKey: ['rules'] })
    },
  })

  const submit = () => {
    const value = text.trim()
    if (!value || create.isPending) return
    create.mutate(value)
  }

  return (
    <>
      <p className="mb-2 font-mono text-[11px] tracking-[0.12em] text-faint">
        NORMA · {rules.length}
      </p>
      {rules.length === 0 ? (
        <EmptyState icon={BookOpenText} title="Aún no hay normas" className="py-8">
          Escríbelas en el chat de la izquierda, o añade una a mano aquí abajo.
        </EmptyState>
      ) : null}
      <ul className="divide-y divide-hairline">
        {rules.map((rule) => {
          const status = rule.status as RuleStatusCode
          const working = pending.has(rule.id) || status === 'compiling'
          const canCompile = !working && (status === 'draft' || status === 'blocked')
          return (
            <li key={rule.id} className="group flex h-9 items-center gap-2">
              <Link
                to={paths.rule(processId, rule.id)}
                title={rule.text}
                className="min-w-0 flex-1 truncate text-[13px] leading-5 text-ink hover:text-ink"
              >
                {ruleLabel(rule)}
              </Link>
              <RuleStatus
                status={status}
                working={working}
                action={canCompile ? 'Compilar' : undefined}
                onAction={canCompile ? () => compile.mutate(rule.id) : undefined}
              />
            </li>
          )
        })}
      </ul>

      {adding ? (
        <div className="flex items-center gap-2 py-2">
          <Plus size={12} strokeWidth={2} className="shrink-0 text-faint" />
          <input
            ref={input}
            value={text}
            disabled={create.isPending}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                submit()
              }
              if (event.key === 'Escape') {
                setAdding(false)
                setText('')
              }
            }}
            onBlur={() => {
              if (!text.trim() && !create.isPending) setAdding(false)
            }}
            placeholder="Una frase. Enter guarda, Esc cancela."
            className="min-w-0 flex-1 bg-transparent text-[13px] text-ink outline-none placeholder:text-faint disabled:opacity-60"
          />
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setAdding(true)}
          className="mt-1 flex w-full items-center gap-2 py-2 text-left text-[13px] text-muted hover:text-ink"
        >
          <Plus size={12} strokeWidth={2} />
          Añadir regla
        </button>
      )}

      {create.isError ? (
        <div className="mt-3">
          <ErrorNotice error={create.error} />
        </div>
      ) : null}
      {compile.isError ? (
        <div className="mt-3">
          <ErrorNotice error={compile.error} />
        </div>
      ) : null}
    </>
  )
}

function RuleStatus({
  status,
  working,
  action,
  onAction,
}: {
  status: RuleStatusCode
  working: boolean
  action?: string
  onAction?: () => void
}) {
  const [seconds, setSeconds] = useState(0)
  const tone = RULE_STATUS[status] ?? RULE_STATUS.draft
  const label = working ? `${seconds}s` : t(`ruleStatus.${status}`)

  useEffect(() => {
    if (!working) {
      setSeconds(0)
      return
    }
    const id = window.setInterval(() => setSeconds((value) => value + 1), 1000)
    return () => window.clearInterval(id)
  }, [working])

  return (
    <div
      className={cn(
        'inline-flex h-5 max-w-2 shrink-0 items-center overflow-hidden rounded-full',
        'transition-[max-width,padding,gap,background-color] duration-150 ease-out',
        'group-hover:max-w-[12rem] group-hover:gap-1 group-hover:px-1',
        '[@media(hover:none)]:max-w-[12rem] [@media(hover:none)]:gap-1 [@media(hover:none)]:px-1',
        working && 'max-w-[12rem] gap-1 px-1',
        tone.pill,
        working && tone.soft,
      )}
    >
      <span className="relative h-2 w-2 shrink-0">
        {working ? (
          <>
            <span className={cn('absolute inset-[2px] rounded-full', tone.dot)} />
            <span
              aria-hidden
              className={cn(
                'absolute inset-0 rounded-full border-[1.5px] animate-spin motion-reduce:animate-none',
                tone.ring,
              )}
            />
          </>
        ) : (
          <span className={cn('absolute inset-0 rounded-full', tone.dot)} />
        )}
      </span>
      <span
        className={cn(
          'whitespace-nowrap text-[11px] leading-none tabular-nums',
          'opacity-0 transition-opacity duration-150 ease-out',
          'group-hover:opacity-100 [@media(hover:none)]:opacity-100',
          working && 'opacity-100',
          tone.ink,
        )}
      >
        {label}
      </span>
      {action && onAction && !working ? (
        <button
          type="button"
          title={action}
          onClick={onAction}
          className={cn(
            'grid h-4 w-4 shrink-0 place-items-center text-faint',
            'opacity-0 transition-opacity duration-150 ease-out',
            'group-hover:opacity-100 hover:text-ink',
            '[@media(hover:none)]:opacity-100',
          )}
        >
          <Hammer size={11} strokeWidth={1.7} />
        </button>
      ) : null}
    </div>
  )
}

function ContextPane({
  processId,
  process,
}: {
  processId: number
  process: ProcessDetail | undefined
}) {
  const queryClient = useQueryClient()
  const { revision, snapshot } = useDraft(processId)
  // The draft's description when one exists, else the published one.
  const current =
    typeof snapshot?.description === 'string' ? snapshot.description : (process?.description ?? '')
  const [text, setText] = useState<string | null>(null)
  const value = text ?? current

  // Only the description: every other field of the draft keeps its value.
  const save = useMutation({
    mutationFn: () =>
      api.saveDraft(processId, {
        ...(revision != null ? { expected_revision: revision } : {}),
        description: value.trim(),
        refresh_agents: false,
      }),
    onSuccess: () => {
      setText(null)
      void queryClient.invalidateQueries({ queryKey: keys.execution(processId) })
      void queryClient.invalidateQueries({ queryKey: keys.draft(processId) })
    },
  })

  const dirty = value.trim() !== current.trim()
  // Read first: the lead sentence, the conventions one click away. Editing is asked for.
  const editing = text != null || !current.trim()

  return (
    <NestedCard
      label="convenciones del agente"
      action={
        editing && current.trim() ? (
          <Button tone="ghost" disabled={save.isPending} onClick={() => setText(null)}>
            Cancelar
          </Button>
        ) : !editing ? (
          <Button tone="ghost" onClick={() => setText(current)}>
            Editar
          </Button>
        ) : null
      }
    >
      <div className="space-y-3 px-3.5 py-3">
        {editing ? (
          <>
            <Textarea
              rows={8}
              value={value}
              onChange={(event) => setText(event.target.value)}
              placeholder="NIF en mayúsculas, sin espacios. Importes en euros. Si falta el pedido, ESCALAR."
            />
            <div className="flex items-center justify-between gap-3">
              <p className="text-[11px] text-faint">
                Entra en cada compilación. No es una regla: es el marco. Queda en el borrador.
              </p>
              <Button
                tone="primary"
                disabled={!dirty || !process || save.isPending}
                onClick={() => save.mutate()}
              >
                {save.isPending ? 'Guardando…' : 'Guardar'}
              </Button>
            </div>
          </>
        ) : (
          <ExpandableText text={current} className="text-[13px] leading-6 text-ink" />
        )}
        {save.isError ? <ErrorNotice error={draftError(save.error)} /> : null}
        {save.isSuccess ? <DraftSaved processId={processId} /> : null}
      </div>
    </NestedCard>
  )
}

function DraftSaved({ processId }: { processId: number }) {
  return (
    <Notice
      title="Queda en el borrador. Publica para que se aplique"
      action={
        <Link
          to={`${paths.process(processId)}?publicar=1`}
          className="text-[12px] text-muted hover:text-ink"
        >
          Publicar
        </Link>
      }
    />
  )
}

const SYMBOL_TYPES = ['text', 'number', 'date'] as const

function InputsPane({
  processId,
  process,
}: {
  processId: number
  process: ProcessDetail | undefined
}) {
  const queryClient = useQueryClient()
  const { revision, snapshot } = useDraft(processId)
  // The draft's symbols when one exists, with `required` and `extraction` as they are.
  const symbols: SymbolIO[] = Array.isArray(snapshot?.symbols)
    ? (snapshot.symbols as SymbolIO[])
    : (process?.symbols ?? [])
  const outcomes = process?.decision_types ?? []
  const [draft, setDraft] = useState({ name: '', type: 'text', description: '' })

  // Always the full list, so no symbol loses what the form does not show.
  const replace = useMutation({
    mutationFn: (next: SymbolIO[]) =>
      api.saveDraft(processId, {
        ...(revision != null ? { expected_revision: revision } : {}),
        // The backend accepts only its enum; what it sent back is already in it.
        symbols: next as SymbolIn[],
        refresh_agents: false,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.execution(processId) })
      void queryClient.invalidateQueries({ queryKey: keys.draft(processId) })
    },
  })

  const add = () => {
    const name = draft.name.trim()
    if (!name) return
    if (symbols.some((item) => item.name === name)) return
    replace.mutate([
      ...symbols,
      { name, type: draft.type, description: draft.description.trim(), required: false },
    ])
    setDraft({ name: '', type: 'text', description: '' })
  }

  return (
    <div className="space-y-8">
      <section>
        <p className="mb-3 font-mono text-[11px] tracking-[0.12em] text-faint">
          SÍMBOLOS · {symbols.length}
        </p>
        <NestedCard
          label="campos del documento"
          action={
            replace.isPending ? (
              <span className="font-mono text-[11px] text-faint">guardando…</span>
            ) : null
          }
        >
          {symbols.length ? (
            <ul className="divide-y divide-hairline">
              {symbols.map((symbol) => (
                <li key={symbol.name} className="flex items-start gap-3 px-3.5 py-2.5">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline gap-2">
                      <span className="font-mono text-[12px]">{symbol.name}</span>
                      <span className="text-[11px] text-faint">
                        {t(`symbolType.${symbol.type}`)}
                      </span>
                    </div>
                    <ExpandableText
                      text={symbol.description ?? ''}
                      className="mt-0.5 text-[12px] leading-5 text-muted"
                    />
                  </div>
                  <button
                    type="button"
                    className="grid h-6 w-6 shrink-0 place-items-center rounded-full text-faint hover:bg-canvas hover:text-ink"
                    title="Quitar"
                    onClick={() =>
                      replace.mutate(symbols.filter((item) => item.name !== symbol.name))
                    }
                  >
                    <X size={12} strokeWidth={1.8} />
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>Ningún símbolo. Las reglas no tienen de qué leer.</Empty>
          )}
          <div className="grid gap-2 border-t border-hairline px-3.5 py-3">
            <Input
              value={draft.name}
              onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))}
              placeholder="issuer_nif"
            />
            <div className="grid grid-cols-[7rem_minmax(0,1fr)] gap-2">
              <Select
                value={draft.type}
                onChange={(event) => setDraft((current) => ({ ...current, type: event.target.value }))}
              >
                {SYMBOL_TYPES.map((type) => (
                  <option key={type} value={type}>
                    {t(`symbolType.${type}`)}
                  </option>
                ))}
              </Select>
              <Input
                value={draft.description}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, description: event.target.value }))
                }
                placeholder="NIF del emisor, sin espacios."
              />
            </div>
            <Button tone="soft" disabled={!draft.name.trim() || replace.isPending} onClick={add}>
              <Plus size={12} strokeWidth={2} />
              Añadir
            </Button>
          </div>
          {replace.isError ? (
            <div className="px-3.5 pb-3">
              <ErrorNotice error={draftError(replace.error)} />
            </div>
          ) : null}
          {replace.isSuccess ? (
            <div className="px-3.5 pb-3">
              <DraftSaved processId={processId} />
            </div>
          ) : null}
        </NestedCard>
      </section>

      <section>
        <p className="mb-3 font-mono text-[11px] tracking-[0.12em] text-faint">
          SALIDAS · {outcomes.length}
        </p>
        <NestedCard label="tipos de decisión">
          {outcomes.length ? (
            <ul className="divide-y divide-hairline">
              {outcomes.map((outcome) => (
                <li key={outcome.name} className="flex items-center gap-3 px-3.5 py-2.5">
                  <span className="min-w-0 flex-1 font-mono text-[12px]">{outcome.name}</span>
                  <span className="font-mono text-[11px] text-faint">p{outcome.priority}</span>
                  {outcome.is_default ? (
                    <span className="text-[11px] text-muted">por defecto</span>
                  ) : null}
                  {outcome.requires_human ? (
                    <span className="text-[11px] text-escalar">persona</span>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <Empty>Sin tipos de decisión.</Empty>
          )}
        </NestedCard>
      </section>
    </div>
  )
}

function VersionChip({
  versions,
  selected,
  onSelect,
  findings,
}: {
  /** Newest first. */
  versions: VersionOut[]
  selected: VersionOut | undefined
  onSelect: (version: VersionOut) => void
  findings: Finding[]
}) {
  const label = selected ? `v${selected.number}` : 'borrador'
  const current = versions[0]
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onPointer = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div ref={root} className="relative shrink-0">
      <button
        type="button"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((next) => !next)}
        className="inline-flex items-center gap-1 rounded-full bg-canvas px-2.5 py-1 font-mono text-[11px] text-ink ring-1 ring-line hover:bg-surface"
      >
        {label}
        <ChevronDown
          size={11}
          strokeWidth={1.75}
          className={cn('text-faint transition-transform', open && 'rotate-180')}
        />
      </button>
      {open ? (
        <div
          role="menu"
          className="absolute right-0 z-20 mt-1 w-72 origin-top-right rounded-[12px] bg-surface p-1 shadow-float ring-1 ring-line"
        >
          <p className="px-2.5 pb-1 pt-2 font-mono text-[10px] tracking-[0.12em] text-faint">
            VERSIONES · {versions.length}
          </p>
          {versions.length ? (
            <ul className="max-h-64 overflow-y-auto">
              {versions.map((version) => (
                <li key={version.id}>
                  <button
                    type="button"
                    role="menuitemradio"
                    aria-checked={version === selected}
                    onClick={() => {
                      onSelect(version)
                      setOpen(false)
                    }}
                    className={cn(
                      'flex w-full items-start gap-2 rounded-[8px] px-2.5 py-1.5 text-left hover:bg-canvas',
                      version === selected && 'bg-canvas',
                    )}
                  >
                    <span className="w-3 shrink-0 pt-0.5">
                      {version === selected ? <Check size={11} strokeWidth={2} /> : null}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-baseline justify-between gap-2">
                        <span className="font-mono text-[12px] text-ink">
                          v{version.number}
                          {version === current ? (
                            <span className="ml-1.5 text-pagar">en vigor</span>
                          ) : null}
                        </span>
                        <span className="shrink-0 font-mono text-[10px] text-faint">
                          {formatRunDate(version.created_at)}
                        </span>
                      </span>
                      <span className="block truncate text-[11px] text-muted" title={version.reason}>
                        {version.reason || version.author}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="px-2.5 py-1.5 text-[12px] text-muted">Aún no hay versiones publicadas.</p>
          )}
          <div className="my-1 border-t border-hairline" />
          <p className="px-2.5 py-1.5 text-[12px] text-muted">
            {findings.length
              ? `Impacto histórico · ${findings.length} aviso${findings.length === 1 ? '' : 's'}`
              : 'Sin avisos sobre decisiones anteriores'}
          </p>
          {findings.length ? (
            <ul className="max-h-40 overflow-y-auto border-t border-hairline py-1">
              {findings.slice(0, 6).map((finding) => (
                <li key={finding.id} className="px-2.5 py-1 text-[11px] leading-4 text-muted">
                  {finding.type.replaceAll('_', ' ')}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

type SnapshotRule = { id?: number; text: string; decision: string; type: string }
type SnapshotSymbol = { name: string; type: string; description?: string }

/** A published version as it was: what it decided with, never editable from here. */
function VersionView({
  processId,
  version,
  current,
  rules,
  onBack,
}: {
  processId: number
  version: VersionOut
  current: VersionOut
  rules: Rule[]
  onBack: () => void
}) {
  const queryClient = useQueryClient()
  const { isManager } = useSession()
  const { revision, pending } = useDraft(processId, isManager)
  const [restored, setRestored] = useState(false)
  const restore = useMutation({
    mutationFn: () => api.saveDraft(processId, {
      expected_revision: null,
      restore_version_id: version.id,
      refresh_agents: false,
    }),
    onSuccess: async () => {
      setRestored(true)
      await queryClient.invalidateQueries({ queryKey: keys.execution(processId) })
      await queryClient.invalidateQueries({ queryKey: keys.draft(processId) })
    },
  })
  const process = (version.snapshot.process ?? {}) as {
    description?: string
    symbols?: SnapshotSymbol[]
  }
  const snapshotRules = (version.snapshot.rules ?? []) as SnapshotRule[]
  // Summaries live on the rules; a version keeps each rule's id.
  const live = new Map(rules.map((rule) => [rule.id, rule]))

  return (
    <div className="space-y-6">
      <Notice
        title={version === current ? `v${version.number} en vigor` : `Estás viendo v${version.number}, solo lectura`}
        action={
          <button type="button" onClick={onBack} className="text-[12px] text-muted hover:text-ink">
            Volver a la definición
          </button>
        }
      >
        {formatRunDate(version.created_at)} · {version.author}
        {version.reason ? ` · ${version.reason}` : ''}
      </Notice>

      {isManager && version !== current ? <section className="space-y-2">
        {restored ? <Notice title={`v${version.number} restaurada como borrador`}>
          Valida y publica el borrador para ponerla en vigor como una versión nueva.
        </Notice> : pending ? <p className="text-[12px] text-muted">Comprobando el borrador…</p> : revision != null ? <p className="text-[12px] text-muted">
          Ya existe un borrador. Descártalo o publícalo antes de restaurar esta versión.
        </p> : <Button disabled={restore.isPending} onClick={() => restore.mutate()}>
          Restaurar como borrador
        </Button>}
        {restore.isError ? <ErrorNotice error={restore.error} /> : null}
      </section> : null}

      <section>
        <p className="mb-2 font-mono text-[11px] tracking-[0.12em] text-faint">IMPACTO HISTÓRICO AL PUBLICAR</p>
        <ValidationImpact processId={processId} validation={version.validation as ValidationReport} />
      </section>

      {process.description ? (
        <section>
          <p className="mb-2 font-mono text-[11px] tracking-[0.12em] text-faint">CONTEXTO</p>
          <ExpandableText text={process.description} className="text-[13px] leading-6 text-ink" />
        </section>
      ) : null}

      <section>
        <p className="mb-2 font-mono text-[11px] tracking-[0.12em] text-faint">
          NORMA · {snapshotRules.length}
        </p>
        <ul className="divide-y divide-hairline">
          {snapshotRules.map((rule, index) => {
            const known = rule.id != null ? live.get(rule.id) : undefined
            return (
              <li key={rule.id ?? index} className="flex h-9 items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-[13px] text-ink" title={rule.text}>
                  {ruleLabel({ text: rule.text, summary: known?.summary })}
                </span>
                <span className="shrink-0 font-mono text-[10px] text-faint">{rule.decision}</span>
              </li>
            )
          })}
        </ul>
      </section>

      {process.symbols?.length ? (
        <section>
          <p className="mb-2 font-mono text-[11px] tracking-[0.12em] text-faint">
            ENTRADAS · {process.symbols.length}
          </p>
          <div className="flex flex-wrap gap-1.5">
            {process.symbols.map((symbol) => (
              <span
                key={symbol.name}
                title={symbol.description}
                className="rounded-full bg-canvas px-2 py-0.5 font-mono text-[11px] text-muted ring-1 ring-line"
              >
                {symbol.name}
              </span>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  )
}

/**
 * One card per proposal: a check the normalizer already created as a draft rule, or a
 * chat or learning proposal that waits for the manager. Accepting one stages it into the
 * draft; nothing here publishes.
 */
function ProposalCard(
  props:
    | { processId: number; check: CreatedCheck; proposal?: undefined }
    | { processId: number; proposal: Proposal; check?: undefined },
) {
  if (props.proposal) return <InboxCard proposal={props.proposal} />
  const { processId, check } = props
  return (
    <li className="rounded-[16px] bg-surface px-4 py-3 ring-1 ring-line">
      <div className="flex items-start justify-between gap-3">
        <p className="font-mono text-[11px] tracking-[0.12em] text-faint">
          {t(`ruleType.${check.type}`)} · {check.decision}
        </p>
        <Link
          to={paths.rule(processId, check.rule_id)}
          className="text-[12px] text-muted hover:text-ink"
        >
          Regla {check.rule_id} →
        </Link>
      </div>
      <p className="mt-2 text-[13px] leading-6 text-ink">{ruleLabel(check)}</p>
      {check.summary ? (
        <ExpandableText text={check.text} className="mt-1 text-[12px] leading-5 text-muted" />
      ) : null}
      {check.quote ? <p className="mt-1 text-[12px] text-muted">«{check.quote}»</p> : null}
    </li>
  )
}

function InboxCard({ proposal }: { proposal: Proposal }) {
  const queryClient = useQueryClient()
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')

  const settle = useMutation({
    mutationFn: (accept: boolean) =>
      accept ? api.acceptProposal(proposal.id) : api.rejectProposal(proposal.id, reason.trim()),
    // Keep the card with its outcome until the list comes back without it.
    onSuccess: () => {
      for (const name of families.proposals) {
        void queryClient.invalidateQueries({ queryKey: [name] })
      }
    },
  })
  const settled = settle.data?.status

  return (
    <li className="rounded-[16px] bg-surface px-4 py-3 ring-1 ring-line">
      <div className="flex items-start justify-between gap-3">
        <p className="font-mono text-[11px] tracking-[0.12em] text-faint">
          {t(`proposalKind.${proposal.kind}`)} · {t(`proposalChannel.${proposal.channel}`)}
        </p>
        {settled ? (
          <span className={cn('text-[12px]', settled === 'accepted' ? 'text-pagar' : 'text-muted')}>
            {settled === 'accepted' ? 'Aceptada' : 'Rechazada'}
          </span>
        ) : (
          <div className="flex gap-2">
            <Button
              tone="soft"
              disabled={settle.isPending || (rejecting && !reason.trim())}
              onClick={() => (rejecting ? settle.mutate(false) : setRejecting(true))}
            >
              Rechazar
            </Button>
            <Button tone="primary" disabled={settle.isPending} onClick={() => settle.mutate(true)}>
              Aceptar
            </Button>
          </div>
        )}
      </div>

      <p className="mt-2 text-[13px] leading-6 text-ink">{proposal.summary}</p>
      {proposal.rationale ? (
        <p className="mt-1 text-[12px] text-muted">{proposal.rationale}</p>
      ) : null}
      {proposal.evidence.length ? (
        <p className="mt-1 text-[12px] text-muted">
          <span className="font-mono">{proposal.evidence.join(' · ')}</span>
        </p>
      ) : null}

      {rejecting && !settled ? (
        <div className="mt-3 space-y-2">
          <Textarea
            rows={2}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Por qué no"
          />
        </div>
      ) : null}
      {settle.isError ? (
        <div className="mt-3 space-y-2">
          <ErrorNotice error={settle.error} />
        </div>
      ) : null}
    </li>
  )
}

function Composer({
  placeholder,
  draft,
  onDraft,
  focusTick,
  busy,
  onSend,
}: {
  placeholder: string
  draft: string
  onDraft: (text: string) => void
  focusTick: number
  busy: boolean
  onSend: (text: string, files: Attachment[]) => void
}) {
  const input = useRef<HTMLInputElement>(null)
  const box = useRef<HTMLDivElement>(null)
  const [files, setFiles] = useState<Attachment[]>([])
  const [over, setOver] = useState(false)

  useEffect(() => {
    if (!focusTick) return
    const field = box.current?.querySelector('textarea')
    if (!field) return
    field.focus()
    const end = field.value.length
    field.setSelectionRange(end, end)
  }, [focusTick])

  const send = () => {
    const prompt = draft.trim()
    if (busy || (!prompt && files.length === 0)) return
    onSend(prompt || 'Revisa los adjuntos y propone cambios.', files)
    onDraft('')
    setFiles([])
  }

  const addFiles = (incoming: File[]) => {
    setFiles((current) => [...current, ...incoming.map(toPreview)])
  }

  const remove = (id: string) => {
    setFiles((current) => {
      const gone = current.find((item) => item.id === id)
      if (gone) revokePreview(gone)
      return current.filter((item) => item.id !== id)
    })
  }

  return (
    <div className="shrink-0 border-t border-hairline px-5 py-4">
      <div
        ref={box}
        className={cn(
          'rounded-[16px] bg-surface p-2 ring-1 transition-colors',
          over ? 'ring-focus' : 'ring-line',
        )}
        onDragOver={(event) => {
          event.preventDefault()
          setOver(true)
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(event) => {
          event.preventDefault()
          setOver(false)
          addFiles([...event.dataTransfer.files])
        }}
      >
        {files.length ? (
          <ul className="mb-1.5 flex flex-wrap gap-2 px-1.5 pt-1">
            {files.map((file) => (
              <li key={file.id}>
                <FileChip file={file} onRemove={() => remove(file.id)} />
              </li>
            ))}
          </ul>
        ) : null}
        <Textarea
          rows={3}
          value={draft}
          onChange={(event) => onDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
              event.preventDefault()
              send()
            }
          }}
          placeholder={placeholder}
          className="border-0 bg-transparent ring-0"
        />
        <div className="flex items-center justify-between px-1 pb-0.5">
          <button
            type="button"
            onClick={() => input.current?.click()}
            className="grid h-8 w-8 place-items-center rounded-full text-muted hover:bg-canvas hover:text-ink"
            title="Adjuntar"
          >
            <Paperclip size={14} strokeWidth={1.6} />
          </button>
          <Button
            tone="primary"
            disabled={busy || (!draft.trim() && files.length === 0)}
            onClick={send}
            className="h-8 px-3"
          >
            <ArrowUp size={13} strokeWidth={2} />
            Enviar
          </Button>
        </div>
      </div>
      <input
        ref={input}
        type="file"
        multiple
        hidden
        accept=".xlsx"
        onChange={(event) => {
          addFiles([...(event.target.files ?? [])])
          event.target.value = ''
        }}
      />
      <p className="mt-2 px-1 text-[11px] text-faint">
        ⌘⏎ para enviar. Solo Excel: enseña al proceso, no entra al lote.
      </p>
    </div>
  )
}
