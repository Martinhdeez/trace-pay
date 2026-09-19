> Configuration publication now follows [process versions](process-versions.md).
> Rule activation and retirement stage edits; manager-approved draft publication makes
> them effective. Compilation no longer auto-activates rules.

# API for the console

The backend is the contract; this page is the map. Live and exact: `make setup`, then
http://localhost:8000/docs (OpenAPI). Every response is JSON in English. Errors come in
two shapes:

- Domain errors (401, 403, 404, 409, the 422s `invalid_document` and `sandbox_error`, 500):
  `{"code", "message"}`.
- Request validation (422 from FastAPI, a body or parameter that does not match the
  schema): `{"detail": [{"loc", "msg", "type"}]}`. Kept as FastAPI sends it; a 422 without
  `code` is always this one.

The committed contract is `frontend/openapi.json` (`make openapi`), with typed
`operationId`s and one name per schema. CORS is open.

**Who may call what.** Identify with `X-User-Id` (the id `POST /login` returns). The console's
user is the manager, who handles only escalations (Q5 in [integration.md](integration.md)).
Reads stay open. These need a manager: run, reprocess, source sync,
`POST /processes/definition`, draft read, edit, validate, publish and discard, resolve, alert ack,
`POST /users`, rule create, compile, activate and retire, norm submit, learning, proposals
(propose, list, accept, reject). A missing header, or an id that does not exist, answers
401 `{"code": "unauthenticated"}`; a user who is not a manager answers 403
`{"code": "permission_denied"}`. Uploads and extraction need any known user.
The first manager comes from the pack (`make setup` loads its `users`).

## One screen, one call

| Screen | Call | Notes |
|---|---|---|
| Processes | `GET /processes` | id, name, description |
| Process page | `GET /processes/{id}/summary` | instances, `by_status`, `by_decision`, `queue`, `resolved`, rules with `fires`, current `sources`, `last_run_at` |
| Process setup | `GET /processes/{id}` | decision types (priority, default, requires_human), symbols and optional `decision_review` guidance |
| Extraction plan | `GET /processes/{id}/extraction-plan` | current fields, enforced rule references, warnings and content fingerprint; [configuration and caching](dynamic-extraction.md) |
| Instances | `GET /processes/{id}/instances?status=&decision=&q=` | each row: latest `decision`, `author` (`engine` or a name), `reason`, `decided_at` |
| Queue | `GET /processes/{id}/queue?type=` | human-requiring outcomes and pending reviewer disagreements; `type` filters the current outcome |
| Instance | `GET /instances/{id}` | symbols `{name: {value, origin}}`, decision history, `review_pending`, append-only `reviews` with recommendations and reasoning, events |
| Invoice viewer | `GET /instances/{id}/file` | the PDF bytes, `Content-Disposition: inline` |
| Reading evidence | `GET /instances/{id}/document` | what extraction read, field by field |
| Instance trace | `GET /instances/{id}/trace` | reading/provider spans, stored evidence, decisions and the current export result |
| Provider activity | `GET /traces?process_id={id}&name=provider_call` | model, HTTP status, request fingerprint, replay/network outcome and reported tokens |
| Metrics | `GET /processes/{id}/metrics` | stage timings, agent `llm` usage and separate reader `providers` totals; replay does not count as network usage |
| Assistant | `GET /instances/{id}/suggestion` | decision, reasoning and a proposed rule. 409 if not escalated, 502 if the model failed |
| Resolve | `POST /instances/{id}/resolve` `{decision, reason, proposal_id?}` | manager; adds a decision, the engine's stays. With `proposal_id`, the resolution event records it and settles that proposal |
| Proposals | `GET /processes/{id}/proposals?status=open`, `POST /proposals/{id}/accept`, `POST /proposals/{id}/reject` `{reason?}`, `POST /instances/{id}/proposal`, `POST /instances/{id}/rule-proposal` (the reviewer agent) | everything an agent proposes, in one shape; manager-only. See [Proposals](#proposals) |
| Rules | `GET /processes/{id}/rules?status=` | compiling, draft, active, blocked, retired. Show `summary` (one plain line, may be null) in lists and `text` (what compiles) in the detail |
| Rule | `GET /rules/{id}` | `code`, `tests`, `report` (`valid`, `tests`, `discrepancies`, `attempts`, `reviews`; `needs_data` when blocked) |
| Norm | `POST /processes/{id}/norm/preview`, `POST /processes/{id}/norm/accept`, `POST /processes/{id}/norm`, `GET /processes/{id}/norm-rules` | the client's norm split into norm rules, each with its rules. Definición → Normas: `preview` saves nothing and returns `norm_rules` plus `existing` (the rules its `covered` ids name, any status: "ya existe"); send `previous` + `feedback` to revise it; `accept` saves the reviewed `norm_rules` (409 on a default or unknown decision, or a check an existing rule already says) and compiles them. `POST /norm` does both without review (CLI, evals). All POSTs need a manager |
| Rule lifecycle | `POST /processes/{id}/rules` (compiles in the background), `POST /rules/{id}/compile`, `GET /rules/{id}/impact`, `POST /rules/{id}/activate`, `POST /rules/{id}/retire` | impact = `unchanged`, `changes`, `conflicts`; create, compile, activate and retire need a manager |
| Learning | `POST /processes/{id}/learning`, `GET /processes/{id}/learning` | Manager-only analysis and proposed norms; [full flow](learning.md) |
| Norm proposal | `GET /norm-proposals/{id}`, `POST .../validate`, `POST .../approve`, `POST .../reject` | Isolated previews; explicit manager adoption with a validation ID |
| Sources | `GET /processes/{id}/sources` | current load per source: rows count, origin, `loaded_at`, and from its latest sync `status` (`ok`/`down`, null if never synced), `error`, `checked_at`. A `down` load is not read by runs until a sync succeeds (ADR 0028) |
| Source rows | `GET /processes/{id}/sources/{name}` | same plus `data` |
| Sync the ERP | `POST /processes/{id}/sources/{name}/sync`, `GET .../diff` | the sync needs a manager. 502 when the download fails or the rows miss a canonical field (`processes/<pack>/schema.json`); nothing is written. Runs sync live sources themselves |
| Audit trail | `GET /processes/{id}/events?step=&instance_id=&limit=` | newest first. Steps: `decision`, `resolution`, `ingest_document`, `compile_rule`, `normalize_norm`, `suggest_escalation`, `sync_source`, `sync_source_failed` |
| Findings | `GET /processes/{id}/findings` | past decisions a later rule says were wrong |
| Alerts | `GET /processes/{id}/alerts?status=open\|acknowledged\|resolved` | past decisions that newer data or rules would decide otherwise (ADR 0026): `before`, `after`, `trigger` (`source_sync` with the rows involved, or `rule_change` with rule ids), `evidence` (reason codes before and after), `acknowledged_by`, `resolved_by_decision_id`. Raised after a sync that changes rows and after a version is published |
| Acknowledge | `POST /alerts/{id}/ack` `{note?}` | manager; 409 if already acknowledged. Act with Resolve or Reprocess; the later decision marks the alert `resolved` |
| Run | `POST /processes/{id}/run` | manager. First syncs every live source (`sync_before_run` in the pack's `schema.json`), then decides every PENDING instance with symbols; `down_sources` (`{name: why}`, omitted when none) lists sources whose sync failed, and sources a rule reads that were never loaded in the process (`never loaded`; a loaded empty table is not listed): their rules do not run and those cases escalate `SOURCE_UNAVAILABLE: <source>` unless a rule that ran already rejects (ADR 0028); 409 while a rule is `compiling` or when no rule is `active`/`blocked` |
| Reprocess | `POST /processes/{id}/reprocess?dry_run=` (optional `{"names": [...]}`) | manager. Decides the DECIDED instances again with the current rules and sources; appends a new engine decision only where it changes; an instance a person decided last is never touched and comes back in `conflicts`. Syncs live sources first like Run (`down_sources`), except with `dry_run=true`. Same 409 as Run |
| Run history | `GET /processes/{id}/runs?limit=50` | every run and reprocess, newest first (Q1): `id`, `kind` (`run`/`reprocess`), `started_at`, `finished_at`, `author`, `version_id`, `version_number`, `rules_hash`, `instances` evaluated, `decided` (decisions it appended), `by_decision` and `escalated` over **every** evaluated instance (so a rerun of the same invoices compares with the run before it), `escalation_reasons` (`{reason code: n}`), `down_sources`, `trace_id` |
| Past run | `GET /runs/{id}` | the same plus `decisions[]` (`decision_id`, `instance_id`, `name`, `decision`, `reason`, `created_at`): what that run appended, read-only |
| Export | `GET /processes/{id}/export` | `outcomes.jsonl`; 409 while anything is undecided or awaiting reviewer-requested approval. One batch only: `make export-batch` (`docs/runbook-batch2.md`) |
| Upload | `POST /processes/{id}/files` (multipart `file`) | stores the PDF and fills symbols from the current extraction plan; existing invoice defaults are retained |
| Workbook | `POST /processes/{id}/sources/workbook` (multipart `file`, required `cut_off_date` `YYYY-MM-DD`) | appends supplier/order snapshots and a `parameters` row with the cut-off that R12 reads; never replaces ERP. 422 without a cut-off: there is no default (Q4) |
| Re-extract | `POST /instances/{id}/extract` (JSON `{}` or reader options) | pending documents only; current symbol schema, rules and source snapshots, preserved evidence; 409 if already decided |
| Locate readings | `GET /instances/{id}/document/locations` | needs `X-User-Id`; source quotes and normalized rectangles per candidate, including dynamic fields; see [PDF traceability](ingestion/pdf-traceability.md) |
| Original page | `GET /instances/{id}/document/pages/{page}` | needs `X-User-Id`; PNG of the original PDF page (one-based), including its rotation; 404 for a missing page |
| Users | `GET /users`, `POST /users`, `POST /login`, `GET /me` | roles `manager`, `operator`; `POST /users` needs a manager |
| Metrics | `GET /processes/{id}/metrics?since=` | runs, step durations, LLM tokens by model and role, outcomes (unchanged) |
| Monitoring: ingestion | `GET /processes/{id}/metrics/ingestion?since=`, `GET /metrics/ingestion` (all processes); `IngestionMetrics` | `files`, `files_per_second`, `pages`, `ocr_calls`, `vision_calls`, `judge_calls`, `focused_reads`, `cache_hits`, `abstentions`, `abstentions_by_field`, `steps[]`; `providers[]` per provider, model and operation: `network_requests`, `replays`, tokens, `known_cost_usd`, `unpriced_requests` |
| Monitoring: agents | `GET /processes/{id}/metrics/agents?since=`, `GET /metrics/agents`; `AgentsMetrics` | `total`, and `by_model`, `by_role`, `by_rule`, `by_norm_rule`, `by_use_case`: tokens in/out/cached, requests, retries, fallbacks, truncations, `known_cost_usd` (runs whose model has a price) and `unpriced_requests` (requests with none: show the count, never 0 USD). Helmcode is unpriced until `TRACEPAY_HELMCODE_BILLING_MODE` is `included` or `metered` with its rates, as for ingestion. `per_hour[]`; `compile` (success rate, attempts); `norms[]` (tokens, `seconds_to_active`) |
| Monitoring: execution | `GET /processes/{id}/metrics/execution?since=`, `GET /metrics/execution`; `ExecutionMetrics` | `runs`, `instances_per_second`, `rules[]` (p50/p95 per rule), `decisions_by_outcome`, `failures`, `escalated`, `pending`, `resolutions_by_author`, `resolution_p50_s`/`p95_s`, `open_alerts`. No LLM call: 0 tokens by design |
| Drill-down | `traces` on every row above (`steps[]`, `providers[]`, `llm[]`, `total`, `by_*`, `per_hour[]`, `rules[]`, `norms[]`) | the `GET /traces?...` (or `/traces/{trace_id}` for a norm) that lists the spans behind that row, in the same `process_id` and `since`. `null` when the row's key is null. `GET /traces` filters: `process_id`, `name`, `status`, `plane`, `since`, `until`, `rule_id`, `norm_rule_id`, `use_case_id`, `model`, `role`, `agent` (the `by_role` key), `provider`, `operation`, `limit` (max 1000) |
| Plane health | `GET /health/planes` | per plane `status` `ok`/`degraded`/`down`, `error_rate`, `p95_ms`, `reason` (thresholds `TRACE_HEALTH_*`); ingestion is at least `degraded` (`sources down: <process>:<source>`) while a source's latest sync in the window failed |
| Live feed | `GET /events/stream?plane=&process_id=&after=` | server-sent events: `event` = plane, `id` = span id, `data` = a span as in `GET /traces`; `: ping` every idle second. Use `EventSource` |

## Proposals

Agents propose and the manager decides. Three channels write proposals, and the same
three calls list them and settle them. No proposal changes a decision or the published
process by itself.

| Channel | Created by | `kind` | Accepting it |
|---|---|---|---|
| `escalation` | `POST /instances/{id}/proposal` on an escalated instance (the assistant) | `decision` | resolves the instance with `payload.proposed`, as `POST /instances/{id}/resolve` with `proposal_id` |
| `escalation` | `POST /instances/{id}/rule-proposal` on a resolved instance (the reviewer agent, ADR 0035) | `rule` | creates the amended rule in the process draft, retires `payload.replaces` there, compiles it in the background. Publishing stays `/processes/{id}/draft/validate` then `/publish` |
| `chat` | a `revise` message in a process chat on an existing process (`/process-drafts/{id}/messages`) | `context` (description, decision types, review), `input` (symbols), `rule` (rules, guidance), `source` | accepts that change in the chat draft (`reviews`). `context` and `input` share the draft's `setup` review, which is accepted once all of them are. Publishing stays `/prepare` then `/publish` |
| `learning` | `POST /processes/{id}/learning` (the learner) | `rule` (a norm), `context`, `input`, `source` | `rule`: adopts the norm's latest valid validation (run `/norm-proposals/{id}/validate` first; 409 otherwise). `context` and `input`: staged in the version draft (`/processes/{id}/draft`), then published separately. `source`: recorded only; load it through Sources |

Rejecting a proposal applies nothing. A chat rejection marks the draft review `rejected`, and a learned
norm gets its rejection through `/norm-proposals/{id}/reject`, which also settles the
proposal. Resolving an instance with a `proposal_id` and another decision marks the
proposal `rejected`, with that decision in `outcome`.

**Rejected vs ignored.** `rejected` is the manager's explicit no (`outcome: {reason}`).
A proposal nobody settled ends `superseded` with `outcome: {cause}`, recorded by one
`expire_proposal` event `{proposal_ids, cause}`:

| `outcome.cause` | When |
|---|---|
| `case_changed` | the case got a new decision (a resolution closes every other open proposal on it) |
| `ignored` | the manager resolved another case of the process while a rule suggestion was open |
| `version_published` | a process version was published (open rule suggestions) |
| `superseded` | a newer proposal of the same kind on the same case, or a new chat revision |

If the case gets another decision while the model is answering, nothing is stored and
the call answers 409 (both `/proposal` and `/rule-proposal`).

### The reviewer agent: `POST /instances/{id}/rule-proposal`

No body. Only after a person resolved the case (the latest decision is theirs, and final).
A pure gate runs first; when no amendment can learn the case it answers **409** with the
reason in Spanish in `message`, and no model is called:

| Engine escalation | 409 `message` starts with |
|---|---|
| not resolved yet | `Resuelve el caso antes de pedir una regla.` |
| `MISSING_DATA` | `Faltaba un dato obligatorio (...)` |
| `UNVERIFIED_DATA` | `Es un escaneo y no se pudo confirmar ...` |
| `SOURCE_UNAVAILABLE` | `No se pudo consultar ...` |
| `RULE_CONFLICT` | `Dos reglas con la misma prioridad ...` |
| `SCAN_REVIEW` | `Las reglas lo rechazaban, pero es un escaneo ...` |
| `RULE_ERROR`, `RULE_NEEDS_DATA`, `RULE_COMPILE_FAILED` | `Una regla no pudo evaluarse (...)` |
| no escalation rule, or several | `Ninguna regla de escalado ...` / `Se dispararon varias reglas de escalado ...` |
| without the rule the engine gives another decision | `Sin la regla N el motor decidiría X, no Y ...` |

201 returns the proposal. `summary` and `rationale` are Spanish; `payload.text` is the
English rule that gets compiled:

```json
{
  "id": 31, "process_id": 3, "instance_id": 418, "channel": "escalation", "kind": "rule",
  "summary": "Escala un IVA distinto del 21 % y del 10 %",
  "rationale": "La persona pagó porque la hostelería tributa al 10 %; se escalan los demás.",
  "evidence": ["symbol:vat_rate", "rule:9", "resolution:9120"],
  "payload": {
    "decision_id": 9120, "engine_decision_id": 9004, "replaces": 9,
    "text": "The printed vat_rate is other than 21 and other than 10.",
    "summary": "Escala un IVA distinto del 21 % y del 10 %", "type": "prohibition",
    "decision": "ESCALAR", "resolved_as": "PAGAR", "version_id": 5
  },
  "status": "open", "author": "assistant", "created_at": "2026-09-19T18:10:02Z",
  "resolved_by": null, "resolved_at": null, "outcome": null
}
```

`POST /proposals/{id}/accept` on it returns it `accepted` with
`outcome: {reason, rule_id, retired, draft_revision}`; the new rule is `compiling` until
the background compile ends (`GET /rules/{rule_id}`), and the compile bumps the draft
revision once more. 409 if it is no longer open (for example `superseded` by a publish).
Spans: `suggest_rule` (`learnable`, `replaces`, `why`, `proposal_id`) with its `llm_run`;
on accept, `accept_proposal` → `save_rule`, `retire_rule`, `compile_rules`.

```json
{
  "id": 12, "process_id": 3, "instance_id": 417, "channel": "escalation", "kind": "decision",
  "summary": "NO_PAGAR for factura_1217.pdf",
  "rationale": "The purchase order PO-2026-0813 is already paid in the ERP.",
  "evidence": ["symbol:purchase_order", "rule:41", "escalation"],
  "payload": {
    "proposed": "NO_PAGAR",
    "why": ["Two rules disagree about this invoice, so a person must look at it."],
    "options": [
      {"decision": "PAGAR", "consequence": "The supplier is paid now."},
      {"decision": "NO_PAGAR", "consequence": "The invoice is held and not paid."}
    ],
    "decision_id": 9001, "escalated_as": "ESCALAR", "escalation_reason": "RULE_CONFLICT: ...",
    "fired_rules": [41, 44], "proposed_rule": {"text": "...", "type": "prohibition"}
  },
  "status": "accepted", "author": "assistant", "created_at": "2026-09-19T18:02:11Z",
  "resolved_by": "Ana", "resolved_at": "2026-09-19T18:03:40Z",
  "outcome": {"decision_id": 9002, "decision": "NO_PAGAR"}
}
```

Chat payloads carry `{draft_id, revision, review_key, before, after}`. Learning payloads
carry `{analysis_id, norm_proposal_id, norm_kind, ...}` for a norm, and
`{analysis_id, kind, name, type, text}` for a definition change. Spans:
`propose_decision`, `suggest_rule`, `accept_proposal`, `reject_proposal` (the last two
with `proposal_id`, `channel` and `kind`) and `expire_proposal`.

## Shapes worth knowing

Mail gathering settings and the scoped service API are documented in
[mail-ingestion.md](mail-ingestion.md). Manual `POST /processes/{id}/run` calls retain the
no-body behavior; a body can select nonempty `instance_ids` and an `idempotency_key`.
The mail worker always evaluates only its imported instances.

- **Instance status** is `PENDING` or `DECIDED`. The queue and `summary.queue` include
  decisions whose type has `requires_human` and cases with `review_pending: true`.
  Optional review never changes the engine outcome or instance status. Its configuration,
  approval flow, fallback and export semantics are in [decision-review.md](decision-review.md).
- **A decision row** has `decision`, `author`, `reason`, `results` and `created_at`. The
  engine's `results` list one entry per rule: `rule_id`, `hash`, `fires`, `reason`. Join
  `rule_id` with `GET /processes/{id}/rules` for the text. A person's row has no results.
- **Escalation reasons** from the engine start with `RULE_ERROR <id>:` or `RULE_CONFLICT:`
  when a rule could not run or two outcomes tied, `SOURCE_UNAVAILABLE: <source>` when a
  source the case needed was down or never loaded (ADR 0028); otherwise it is the firing rule's reason
  code (e.g. `IMPOSSIBLE_DATE 2026-02-31`, several joined by ` | `), never its text.
- **Symbols** are stored as `{value, origin}`; `origin` says where extraction read it.
- **Names** (`instance.name`) are the exact file names, accents included; they are the
  `file_id` of the export.
