# Local-LLM Task Queue

The core mechanism that makes "local tokens" work without forking the pipeline.
The backend keeps orchestrating; every model call of a `local_agent` case
becomes a task that the agent on the RCIC's machine executes with the model
inside Claude Code / Codex (D17).

Server-mode cases run on **Celery** (`ai_parse/tasks.py`, `document_queue` and
`analysis_queue`, Redis broker). Local-agent cases use no Celery at all, and no
server thread ever waits on the agent: the pipeline is a chain of resumable
steps; a step that needs a model stores its requests as agent tasks and stops,
and the agent API applies the answers when the agent submits them.

## Resumable steps, shared by both modes

The pipeline (`ai_parse/service/pipeline/`) is a set of named steps. A step
has two halves, neither of which calls a model:

```
requests(state) -> list[LLMRequestSpec]     # prompts, inputs, output schema / text mode
apply(state, answers) -> list[(step, state)] # parse, write memos / values, name the next steps
```

`state` is JSON (a local case stores it with the batch). Two executors run
the same steps:

- **`InlineExecutor`, server mode.** Inside the existing Celery tasks:
  `process_document_task` runs `doc.extract` → … → `doc.finalize` for one
  document; in `process_analysis_task`, `AnalysisRunner` still runs sections
  in tiers on threads and `SectionAnalyzer.analyze` runs one section's
  analysis steps. Each request is answered in-process (`server_fn`: Gemini,
  then OpenAI, with the usual concurrency and timeouts) and the next step
  starts at once.
- **`AgentExecutor`, local-agent cases.** A step with requests becomes one
  `AgentTaskBatch` (`continuation` = step name, `continuation_kwargs` = step
  state) with one `AgentTask` per request (`seq` = its position), and the
  chain stops there. A step without requests (e.g. an XFA form read on the
  server) applies immediately and the chain carries on.

```python
# sketch of AgentExecutor (ai_parse/service/pipeline/executor.py)
def advance(pending):
    with local_step():                       # provider guard, see "Fail closed"
        while pending:
            name, state = pending.pop(0)
            step = get_step(name)
            if not step.is_live(state):      # document / job stopped or settled
                continue
            specs = step.requests(state)
            if specs:
                park(step, state, specs)     # AgentTaskBatch + AgentTasks; stop this branch
            else:
                pending += step.apply(state, [])
```

Chained calls (extract → translate) are two rounds of a step; fan-out (one
call per section) is one batch with N tasks. Generic model calls all use the
kind `llm_call`; only OCR (`extract_content`) and the passport sub-type
classifier (`classify_document`) have kinds of their own. See
[agent-task-schema.md](../reference/agent-task-schema.md).

## The steps

**Documents** (`ai_parse/service/documents/steps.py`):

| Step | Requests (local) | Applies |
|---|---|---|
| `doc.extract` | one `extract_content` for a PDF, image, Word or Excel file; none for an XFA form (read on the server) | the `RawMemo` (and page memos for a split PDF), then routes |
| routing | — | multi-page merge, archive group, multi-applicant Excel/Word, Agent Survey (every section, serially), composite types classified first, else the plain section filter |
| `doc.classify` | one `classify_document` | the sub-types pick the sections; none → kept as a supporting document |
| `doc.filter_sections` | one `llm_call` per section, purpose `section_filter`, text mode | `SectionMemo`s; failed sections stored with defaults |
| `doc.multi_applicant` | one `llm_call` per applicant × section, purpose `applicant_section`, text mode | per-applicant `SectionMemo`s; nothing found falls back to the plain filter |
| `doc.finalize` | — | the document's final status, its job and (local) page documents |

A single-applicant Word or Excel file is extracted once (the agent's text is
the `RawMemo`), and a multi-applicant Word file reads that `RawMemo` too.

**Analysis** (`ai_parse/service/analysis/steps.py`), per section:

| Step | Requests | Applies |
|---|---|---|
| `analysis.section` | one `llm_call`, purpose `analyze_section`, JSON (`AnalysisResult` schema); its `validator` runs `SectionAnalyzer.check_agent_result`, so an answer that does not survive the pipeline's normalisation is rejected and asked again | `AnalysisMemo`; `AnalysisResultStore.prepare` plans the value chunks (`AnalysisSurveyValueSync.plan_simple` / `plan_complex`) |
| `analysis.reformat` | per chunk: extract, then translate (`reformat_extract`, `reformat_translate`; JSON schema from the `PromptTemplate`) | after the last chunk, `write_simple` / `write_complex` write the `SurveyValue`s |

For a local case `LocalAnalysis` replaces the runner's tiers: it starts every
section at once, except those that read another section's values
(`personal_timeline` and `address_history` wait for
`personal_basic_information`), and when the last section settles it finishes
the job like the runner does (manual-value overlay, job and survey status,
`AllSectionsFailedError`, post-analysis steps).

## Request → batch → apply on submit

```
agent (RCIC machine)                API request (Django)
────────────────────                ─────────────────────────────────────────────
start_processing ─────────────────▶ start_document_processing
                                      └─ on commit: doc.extract → batch A (1 task)
pull ◀───────────────────────────── advance(); lease task A1
submit A1 ────────────────────────▶ validate → accepted → batch A completed
                                      └─ on commit: apply_batch(A)
                                           ├─ doc.extract.apply → RawMemo, routing
                                           └─ doc.filter_sections → batch B (12 tasks)
pull ◀───────────────────────────── lease B1…B12
submit B1…B12 ────────────────────▶ … last one settles B → apply → doc.finalize
```

- **Start.** `start_document_processing` from the agent runs the first step
  of each document in that request (`LocalDocumentRun`, after commit);
  `start_analysis` runs `LocalAnalysis.start`. Nothing is queued on Celery.
- **Submit.** `POST /agent/tasks/{id}/result/` validates the result (below)
  and records it under a row lock on the batch. When every task of the batch
  is accepted or failed, the batch becomes `completed` or `failed` and,
  once the transaction commits, `AgentExecutor.apply_batch` runs in the same
  request.
- **Apply once.** `apply_batch` claims the batch (`applied_at`, under
  `select_for_update`), passes the answers to the step in `seq` order (a
  failed task becomes an answer with its error), records `apply_done_at` /
  `apply_error`, purges `llm_call` payloads and results, and advances the
  steps that follow, which park their own batches. A repeated submit returns
  the stored verdict and applies nothing.
- **Stopped work.** `is_live` drops the answers when the document or analysis
  job was stopped or settled meanwhile. `stop_document_processing` and
  `stop_analysis` (and switching the case to server mode) cancel open tasks
  and batches; cancelled batches are never applied.
- **Errors.** A step that raises settles what it was working on: the document
  ends `FAILED` (or `STOPPED` when cancelled), the analysis section `failed`
  (`no_data`, with defaults written, when it has no memos).

Properties this buys:

- **No worker or thread waits.** Plan limit hit, laptop closed, RCIC on
  holiday: the batch sits in the DB and nothing on the server is held.
- **Durable pause.** Progress is rows, not in-memory state; a deploy loses
  nothing.
- **No background infrastructure.** No `agent_queue` worker, no beat task, no
  continuation Celery tasks. Everything happens in agent API requests.

## Advance on every pull

Each `pull` (and each `wait` round) first runs `AgentTaskService.advance` for
the case and its dependants, doing what a periodic sweep would:

- leased tasks whose lease ran out go back to `queued`;
- open batches older than `AGENT_BATCH_TTL_DAYS` (default 7) expire, their
  open tasks with them;
- settled batches that no request applied (the applying request died after the
  answer was committed, or a claim older than 10 minutes never finished) are
  applied now. An expired batch is applied with every open task as a failed
  answer, so the step settles with that reason instead of hanging.

So a case only moves while its agent talks to uApply, and whatever was left
half-done is picked up by the next pull.

### Status while waiting

While a document waits, its `DocumentProcessingJob.metadata` carries
`waiting_on_agent` and `agent_batch_id`; `doc.finalize` (or a failure) clears
it. The agent status lists `documents_waiting_on_agent`, task counts and
`stages` (`processing`, `analysis`). The dashboard's 30-minute stale check
spares documents with an open batch (their own or their parent PDF's).

## Fail closed

While a local step runs (`local_mode.local_step()`, a contextvar carried into
thread pools and the shared async runner), the public model methods of
`GeminiClient` / `OpenAIClient` (`agent_routable`) and LlamaParse raise
`ServerLLMDisabled`. A step only builds requests, so a provider call there is
a bug, and it fails instead of spending uApply's keys.

Where a local case's work runs is decided per action (`RunOrigin`): requests
from uapply-agent run on the agent; dashboard actions on a local case
(processing, analysis, AI Check saves, the shorten button) run on the server
as for any case, and every Celery task runs with origin `SERVER`.

## Inputs: never ship the file, ship a reference

The agent already has the files — they came from the folder it uploaded. The
task carries `{"local_path": "passport.pdf", "sha256": "…", "pages": [1,2]}`.
The agent verifies the hash before use; if the file is gone (RCIC moved it),
the agent downloads it from S3 via the existing `documents/{id}/download/`.

Status: the executor currently downloads every input document from uApply
(presigned URL from `documents/{id}/download/`) into
`.uapply/cache/<task id>/`, rather than reusing the local copy. `llm_call`
tasks carry their text in the payload (`user_prompt`, `text_files`).

Work that needs no model runs on the agent (`local_ops.py`): page rendering,
PDF text layers (pdfplumber), Word (python-docx) and Excel (openpyxl, one
`# 表格: <name>` markdown table per sheet). In server mode nothing changes.

## The `AgentTask` model

```
ai_parse.models.AgentTaskBatch           one parked step
  id, survey, document?, analysis_job?, stage
  continuation         step name (e.g. doc.filter_sections)
  continuation_kwargs  step state (JSON)
  total, accepted, failed  ints
  status               open | completed | failed | cancelled | expired
  applied_at           a request claimed the answers
  apply_done_at        the step finished applying them
  apply_error          what the step raised, if anything
  error, created_at, completed_at

ai_parse.models.AgentTask
  id                  UUID
  batch               FK AgentTaskBatch
  seq                 position of the request in its step
  kind                CharField  (see reference/agent-task-schema.md)
  status              queued | leased | submitted | accepted | rejected | failed | expired | cancelled
  payload             JSON       prompts, schema, input refs, flags
  result              JSON, null validated result
  rejection           JSON, null {code, message, attempt}
  attempts            int
  lease_owner         CharField  agent session id
  lease_expires_at    datetime
  created_at / submitted_at / completed_at
```

Prompt text in `payload` is resolved from `PromptTemplate` at creation time
(same cache and invalidation rules as server mode). `llm_call` payloads and
results are reduced to `{output_mode, purpose, purged}` once their batch is
applied.

### Lease semantics

- `POST /agent/tasks/pull` returns up to `n` `queued` tasks of open batches for
  the given survey(s) and their dependants, sets `leased` with a lease of
  `AGENT_TASK_LEASE_SECONDS` (default 15 min).
- Submitting after lease expiry still succeeds if nobody else has since
  accepted the task; the server validates regardless of who submits.
- `POST /agent/tasks/{id}/release` puts a task back when the agent decides not
  to do it (e.g. plan limit reached). A counted release spends an attempt, so a
  task that always fails on the agent fails after 3.

## Validation on submit

The server never trusts the agent's answer blindly. On `POST .../result`:

1. **Schema.** Kinds with a Pydantic model are parsed with the very model the
   pipeline uses; `llm_call` results must carry the schema's required fields
   (`text` as a string in text mode). On failure → `rejected` with the error;
   the task returns to `queued` with `attempts+1`; after `max_attempts` (3) →
   `failed`, which counts toward the batch's `failed`.
2. **Step check.** A payload `validator` (an import path) runs the step's own
   check: for `analyze_section`, the JSON must survive the analysis
   normalisation, so a malformed answer is re-asked rather than lost.
3. **Consistency.** Classification values must be in `allowed_values`; OCR must
   cover every page the server knows (`page_count`).
4. **Evidence.** For tasks with `evidence_required`, at least one quoted span
   must appear verbatim in the text the task was given. For `extract_content`
   the agent *produces* the `RawMemo`, so there is no ground truth: text-layer
   PDFs, Word and Excel bypass the model (deterministic readers), and scans
   rely on the page-coverage check.
5. **Provenance.** An `LLMTokenLog` row is written per accepted task
   (`local_agent:<runtime>:<model>`, token counts as reported, the session id)
   so the existing monitoring keeps working.

Accepted results are then applied by the step — `RawMemo` / `SectionMemo`
writes, `AnalysisMemo`, `SurveyValue` extraction, conflict detection — the same
code the server-mode path runs inline.

## Ordering and parallelism

Batches are created as the pipeline reaches them, so dependencies are
implicit: section filters appear only after the extraction batch has been
applied. Within a batch tasks are independent and can be leased in parallel by
multiple agent workers — see [runtime-modes.md](runtime-modes.md). Documents
advance independently; analysis sections run in parallel except the dependent
ones above.

## Failure handling

| Situation | Behaviour |
|---|---|
| Agent submits an invalid result 3× | task `failed` → batch `failed` → the step applies it as an error: a failed extraction or classification fails the document, a failed section filter is stored with defaults and noted on the job, a failed analysis call fails the section; reasons visible in the dashboard and in `case_status` |
| Agent offline for 7 days | the next pull expires the batch and applies it as failed; same as above; `start_processing` again re-creates it |
| Request dies after the last answer is committed | the batch is settled but not applied; the next pull or wait applies it |
| Process dies while applying | the claim (`applied_at`) goes stale after 10 minutes and the next pull applies it again |
| Step raises while applying | `apply_error` recorded; the document or section is settled as failed |
| RCIC switches case back to `llm_mode=server` | open tasks and batches `cancelled`; processing or analysis started again runs on the server provider |
| Agent reports plan-limit | agent releases leases; nothing changes server-side; the case resumes on the next `run_tasks` |
| File missing locally and S3 download fails | agent releases task with reason; surfaced to RCIC |
| Two agents submit the same task | first accepted result wins; second gets the stored verdict; the batch is applied once (row lock + claim) |

## What does *not* change

- Prompts, section routing (`DocumentTypeSection`), `SurveyValue` semantics,
  conflict detection, formulas, auto-fill data generation, the dashboard.
- Server mode: `llm_mode=server` is the default and runs the same steps in the
  same Celery tasks, answered by Gemini / OpenAI.
- Celery itself: two queues and a Redis broker, for server-mode work only.
