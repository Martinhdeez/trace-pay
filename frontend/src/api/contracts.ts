/**
 * The backend contract: aliases of the generated `schema.d.ts`, plus the shapes the API
 * leaves as free JSON. Keys are the API's English; Spanish lives in `i18n/es.ts`.
 */

import type { components } from './schema'

export type User = components['schemas']['UserOut']

export type Role = User['role']

type Schemas = components['schemas']
export type ProcessSummary = Schemas['ProcessSummary']
export type ExecutionMetrics = Schemas['ExecutionMetrics']
export type IngestionMetrics = Schemas['IngestionMetrics']
export type AgentsMetrics = Schemas['AgentsMetrics']
export type Plane = Schemas['Plane']
/** Each plane has its own metrics; they are never added together. */
export type PlaneMetrics = {
  ingestion: IngestionMetrics
  agents: AgentsMetrics
  execution: ExecutionMetrics
}
export type ProcessMetrics = Schemas['ProcessMetrics']
export type PlaneHealth = Schemas['PlaneHealth']
export type VersionDraft = Schemas['VersionDraftOut']
export type VersionOut = Schemas['VersionOut']
export type PublishIn = Schemas['PublishIn']
export type DraftIn = Schemas['DraftIn']
export type ExecutionOut = Schemas['ExecutionOut']
export type ExecutionSettings = Schemas['ExecutionSettings']
export type AgentSettings = Schemas['AgentSettings']
export type ExtractionSettings = Schemas['ExtractionSettings']
export type DecisionReview = Schemas['DecisionReviewConfig']
export type RunSummary = Schemas['RunSummary']
export type ReprocessSummary = Schemas['ReprocessSummary']
export type InstanceOut = Schemas['InstanceOut']
export type InstanceDetail = Schemas['InstanceDetail']
export type DecisionOut = Schemas['DecisionOut']
export type RuleResult = Schemas['RuleResultOut']
export type ReviewOut = Schemas['DecisionReviewOut']
export type InstanceEvent = Schemas['EventOut']
/** What the assistant proposes for an instance waiting on a person. */
export type Suggestion = Schemas['Suggestion']
export type ResolveIn = Schemas['ResolveIn']
export type RuleIn = Schemas['RuleIn']
export type RunOut = Schemas['RunOut']
export type AlertOut = Schemas['AlertOut']
export type Proposal = Schemas['ManagerProposalOut']
export type ProposalStatus = Proposal['status']
/** `payload` of an escalation (`kind: decision`) proposal. */
export type DecisionProposalPayload = {
  proposed: string
  why?: string[]
  options?: { decision: string; consequence: string }[]
  escalation_reason?: string | null
  fired_rules?: number[]
  proposed_rule?: { text: string; type: RuleIn['type'] }
}
export type UseCaseOut = Schemas['UseCaseOut']
export type UseCaseDetail = Schemas['UseCaseDetail']
export type AgentConfigOut = Schemas['AgentConfigOut']
export type AlertStatus = 'open' | 'acknowledged' | 'resolved'
export type RunDetail = Schemas['RunDetail']
export type InstanceTrace = Schemas['InstanceTrace']
export type SpanNode = Schemas['SpanNode']
export type ExtractionResult = Schemas['ExtractionResult']
export type FieldReading = Schemas['FieldReading']

/** One agreed symbol inside `InstanceDetail.symbols`: the value and where it came from. */
export type SymbolReading = { value: string | number | boolean | null; origin?: string }

/** Server-side filters of the instance list. */
export type InstanceFilters = { status?: string; decision?: string; q?: string }
export type DocumentUpload = Schemas['DocumentUpload']
export type WorkbookUpload = Schemas['WorkbookUpload']
export type SourceOut = Schemas['SourceOut']
export type SourceDetail = Schemas['SourceDetail']
export type SyncResult = Schemas['SyncResult']

/** Reported after each file of a batch has been uploaded (and re-extracted when it was stale). */
/** One step of a batch upload: a file starts being read, or its reading comes back. */
export type UploadProgress = {
  index: number
  phase: 'reading' | 'read'
  done: number
  total: number
  name: string
  /** Symbols the reader found, of those the process expects; once read. */
  read?: number
  expected?: number
}

export type Preset = Exclude<ExecutionSettings['preset'], 'custom'>

/** The backend leaves `validation` as free JSON; these are the fields the console reads. */
export type HistoricalCoverage = {
  evaluated: number
  not_evaluable: number
  partial: number
  none: number
  total?: number
}
export type HistoricalCaseWithoutCoverage = {
  instance_id: number
  name: string
  missing_symbols: string[]
  evaluated_rules: number[]
  unavailable_rules: { rule_id: number; symbol: string }[]
}
export type ValidationReport = {
  valid: boolean
  hash: string
  unchanged?: number
  changes?: ValidationChange[]
  coverage?: HistoricalCoverage
  not_evaluable?: HistoricalCaseWithoutCoverage[]
  conflicts?: ValidationChange[]
  errors?: { instance_id: number; name?: string; reason?: string }[]
  error?: string
  [key: string]: unknown
}

export type ValidationChange = {
  instance_id: number
  decision_id?: number
  name: string
  before: string
  after: string
  reason?: string
}

export type ProcessOut = Schemas['ProcessOut']
/** A process as the API returns it: decision types and symbols in the pack's shape. */
export type ProcessDetail = Schemas['ProcessDetail']
export type DecisionType = Schemas['DecisionTypeIO']
export type SymbolIO = Schemas['SymbolIO']
export type SymbolIn = Schemas['SymbolIn']
/** The whole process as data, the same shape as the files under `processes/`. */
export type Definition = Schemas['Definition']
export type LoadResult = Schemas['LoadResult']

export type RuleStatus = 'compiling' | 'draft' | 'active' | 'blocked' | 'retired'
export type RuleType = RuleIn['type']
export type Rule = Schemas['RuleOut']
export type RuleDetail = Schemas['RuleDetail']
export type NormRule = Schemas['NormRuleOut']
export type NormOut = Schemas['NormOut']
/** A norm as the normalizer read it, before anything is saved: reviewed, then accepted. */
export type Normalization = Schemas['Normalization']
export type NormSentence = Schemas['Sentence']
export type NormPreview = Schemas['NormPreview']
/** `feedback` and `previous` only when revising an earlier preview. */
export type NormPreviewIn = Omit<Schemas['NormPreviewIn'], 'feedback'> & { feedback?: string }
/** A rule a sentence's `covered` names: what the norm asks for that already exists. */
export type ExistingRule = Schemas['ExistingRule']
export type CreatedCheck = Schemas['CreatedCheck']
/** What adding, or removing, a rule would do to the decisions already taken. */
export type Impact = Schemas['ImpactOut']
export type ImpactChange = Schemas['ChangeOut']

/** The compiler leaves `report` as free JSON; these are the fields the console reads. */
export type RuleReport = {
  valid?: boolean
  tests?: { name: string; expected: boolean; got: string; passed: boolean }[]
  discrepancies?: string[]
  attempts?: number
  reviews?: unknown[]
  needs_data?: { by?: string; missing?: string[]; explanation?: string }
}

export type ReadingLocation = {
  candidate: number
  page: number | null
  raw: string
  value: string | null
  method: string
  locator: string
  precision: 'text' | 'ocr' | 'region' | 'page' | 'unavailable'
  boxes: number[][]
}

export type DocumentLocations = {
  extraction_id: string
  sha256: string
  pages: { number: number; width: number; height: number }[]
  fields: Record<string, ReadingLocation[]>
  symbol_fields: Record<string, string>
}

/** The stored reading of a document (`GET /instances/{id}/document`). */
export type DocumentEvidence = ExtractionResult

export type DiscoverySession = Schemas['DiscoveryDraftOut']
export type DiscoverySessionSummary = Schemas['DiscoverySessionSummary']
/** One entry of `DiscoverySession.messages`. */
export type DiscoveryMessage = { role: 'user' | 'assistant'; text: string; author?: string }

/** A past decision a later rule says was wrong. A notice, never a correction. */
export type Finding = Schemas['FindingOut']

export interface ApiClient {
  health(): Promise<boolean>

  login(email: string): Promise<User>
  me(): Promise<User>
  listUsers(): Promise<User[]>

  listProcesses(): Promise<ProcessOut[]>
  getProcess(id: number): Promise<ProcessDetail>
  /** Posts the English pack as it is. 409 when rules carry `code` files or a draft exists. */
  loadDefinition(body: Definition): Promise<LoadResult>

  listRules(processId: number, status?: RuleStatus): Promise<Rule[]>
  listNormRules(processId: number): Promise<NormRule[]>
  /** Splits a norm into checks, each already created as a draft rule that compiles. */
  normalizeNorm(processId: number, text: string): Promise<NormOut>
  /** What the normalizer makes of a norm, revised with `feedback`. Saves nothing. */
  previewNorm(processId: number, body: NormPreviewIn): Promise<NormPreview>
  /** Saves a reviewed preview: its checks become draft rules that compile. */
  acceptNorm(processId: number, body: Normalization): Promise<NormOut>
  getRule(id: number): Promise<RuleDetail>
  createRule(processId: number, body: RuleIn): Promise<RuleDetail>
  compileRule(id: number): Promise<RuleDetail>
  /** What adding, or removing, this rule would change. Does not change anything. */
  ruleImpact(id: number): Promise<Impact>
  /** Stages the rule into the process draft; publishing makes it effective. */
  activateRule(id: number): Promise<RuleDetail>
  retireRule(id: number): Promise<RuleDetail>

  listDiscoverySessions(): Promise<DiscoverySessionSummary[]>
  startDiscoverySession(processId: number, name: string): Promise<DiscoverySession>
  messageDiscoverySession(id: number, revision: number, message: string): Promise<DiscoverySession>
  uploadDraftWorkbook(id: number, revision: number, file: File): Promise<DiscoverySession>

  summary(processId: number): Promise<ProcessSummary>
  planeMetrics<P extends Plane>(processId: number, plane: P): Promise<PlaneMetrics[P]>
  processMetrics(processId: number): Promise<ProcessMetrics>
  planesHealth(): Promise<PlaneHealth[]>
  /** 404 when the process has no draft. */
  getDraft(processId: number): Promise<VersionDraft>
  validateDraft(processId: number): Promise<VersionDraft>
  publishDraft(processId: number, body: PublishIn): Promise<VersionOut>
  discardDraft(processId: number, revision: number): Promise<void>
  listVersions(processId: number): Promise<VersionOut[]>
  getExecution(processId: number): Promise<ExecutionOut>
  saveDraft(processId: number, body: DraftIn): Promise<VersionDraft>

  run(processId: number): Promise<RunSummary>
  reprocess(processId: number, dryRun: boolean): Promise<ReprocessSummary>
  /** Every run of the process, newest first. */
  listRuns(processId: number): Promise<RunOut[]>
  /** One run and the decisions it appended. */
  getRun(id: number): Promise<RunDetail>
  listInstances(processId: number, filters?: InstanceFilters): Promise<InstanceOut[]>
  getInstance(id: number): Promise<InstanceDetail>
  getDocument(instanceId: number): Promise<ExtractionResult>
  /** The decision, its rule results with their text, the file and the span tree. */
  getTrace(instanceId: number): Promise<InstanceTrace>
  /** Where the stored PDF is served, for an iframe or a download link. */
  fileUrl(instanceId: number): string
  /** Everything waiting for a person, including cases the reviewer disagreed with. */
  queue(processId: number): Promise<InstanceOut[]>
  suggestion(instanceId: number): Promise<Suggestion>
  resolve(instanceId: number, body: ResolveIn): Promise<InstanceDetail>
  /** The assistant proposes a decision for an escalated case; a new one supersedes the open one. */
  proposeDecision(instanceId: number): Promise<Proposal>
  listProposals(processId: number, status?: ProposalStatus): Promise<Proposal[]>
  /** Applies it through its channel: a decision resolves the case; chat and learning stage it. */
  acceptProposal(id: number, reason?: string): Promise<Proposal>
  rejectProposal(id: number, reason: string): Promise<Proposal>

  listFindings(processId: number): Promise<Finding[]>
  /** Past decisions that newer data or rules would decide differently. */
  listAlerts(processId: number, status?: AlertStatus): Promise<AlertOut[]>
  ackAlert(id: number, note?: string): Promise<AlertOut>
  exportOutcomes(processId: number): Promise<string>

  uploadFiles(
    processId: number,
    files: File[],
    onProgress?: (progress: UploadProgress) => void,
  ): Promise<DocumentUpload[]>
  listSources(processId: number): Promise<SourceOut[]>
  /** One source with its rows. `parameters` holds the cut-off date. 404 until loaded. */
  getSource(processId: number, name: string): Promise<SourceDetail>
  /** Excel of suppliers / orders / parameters. The cut-off date is required, there is no default. */
  uploadWorkbook(processId: number, file: File, cutOffDate: string): Promise<WorkbookUpload>
  syncSource(processId: number, name: string): Promise<SyncResult>

  listUseCases(): Promise<UseCaseOut[]>
  /** One use case with its agents and the model each one runs. */
  getUseCase(id: number): Promise<UseCaseDetail>
  /** A new version of that agent's config: the same settings with another model. */
  setAgentModel(useCaseId: number, agent: AgentConfigOut, model: string): Promise<AgentConfigOut>
}
