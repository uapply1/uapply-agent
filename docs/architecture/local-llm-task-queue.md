# Local-LLM Task Queue

The core mechanism that makes "local tokens" work without forking the pipeline.
The backend keeps orchestrating; every LLM call becomes a task that the agent on
the RCIC's machine executes with the model inside Claude Code / Codex.

The backend runs background work on **Celery** (`ai_parse/tasks.py`,
`document_queue` and `analysis_queue`, Redis broker). Celery has no way to
park a running task for hours without holding a worker, so the design below
never waits inside a worker: a stage task *creates* agent tasks and exits, and
the API *resumes* the pipeline when the agent's answers arrive.

## Where LLM calls happen today

All providers implement one interface in `ai_parse/integrations/llm/`
(`base_llm.py`, `gemini_llm.py`, `openai_llm.py`), used through `LLMService`:

| Method | Used for |
|---|---|
| `aask_to_text(system_prompt, user_prompt, images?, model?)` | OCR (Gemini vision on page images), free-text steps |
| `aask_to_json(system_prompt, user_prompt, response_format: BaseModel, images?, model?)` | classification, section extraction, analysis, extract-step, conflict reasoning |
| `aask_reasoning(...)`, `asearch_web_*` | a few analysis steps; web search is out of scope for local mode |

Every call is logged to `LLMTokenLog`. The pipeline (`DocumentProcessor`,
analysis orchestrators, `confirmation_orchestrator`) does not know which
provider it is talking to.

Important for the design: LLM calls are **not** one-per-Celery-task. Single
functions chain several calls — e.g. `analysis_survey_sync.py` runs
extract → retry → translate in sequence, and section splitting `gather`s N
calls in one step — with parsing and `SectionMemo` writes right after the
call in the same function.

## The change: prepare / continue stages

Every pipeline step that calls the LLM is split into two plain functions:

```
prepare(ctx) -> list[LLMRequest]           # no LLM, builds prompts/inputs/schemas
continue_(ctx, results: list[LLMResult])   # no LLM, parses, writes memos, dispatches next step
```

and run through one helper:

```python
# ai_parse/agent/stage_runner.py  (new)
def run_llm_stage(stage: str, ctx, prepare, continue_):
    requests = prepare(ctx)
    if ctx.survey.llm_mode == "server":
        results = run_inline(requests)          # Gemini/OpenAI, exactly as today
        return continue_(ctx, results)          # same Celery task, no behaviour change
    batch = AgentTaskBatch.objects.create(
        survey=ctx.survey, document=ctx.document, stage=stage,
        continuation=f"{continue_.__module__}.{continue_.__name__}",
        continuation_kwargs=ctx.to_kwargs(), total=len(requests))
    AgentTask.objects.bulk_create(AgentTask.from_request(batch, r) for r in requests)
    mark_waiting_on_agent(ctx)                  # see "Status while waiting"
    return None                                 # Celery task ends here; worker slot freed
```

- In **server mode** the helper calls the provider inline and continues in the
  same task — identical to today's behaviour, so the refactor is safe to land
  before any agent exists.
- In **local mode** the Celery task ends after creating the batch. Nothing is
  held.
- Chained calls (`extract → translate`) become two stages, each with its own
  batch and continuation. Fan-out calls (`gather` over 12 sections) become one
  batch of 12 tasks with one continuation.
- A per-**kind** override (`AGENT_LOCAL_TASK_KINDS`) lets some stages keep
  running on the server provider for `local_agent` cases (e.g. analysis
  prompts you consider IP, or web-search steps).

This is a real refactor of every LLM-calling step, not a provider swap.
[delivery-plan.md](../design/delivery-plan.md) sizes it per stage; the
classification stage goes first because it is a single call with a single
continuation.

### Inputs: never ship the file, ship a reference

The agent already has the files — they came from the folder it uploaded. The
task carries `{"local_path": "passport.pdf", "sha256": "…", "pages": [1,2]}`.
The agent verifies the hash before use; if the file is gone (RCIC moved it),
the agent downloads it from S3 via the existing `documents/{id}/download/`.

Server-side page rendering / pdfplumber / docx text are **moved to the agent**
(`local_ops/`) in local mode. They need no model, and rendering on the server
just to ship PNGs back down is wasteful. In server mode nothing changes.

## Resuming: the API dispatches the continuation

```
Celery document_queue                          API (Django)                       RCIC machine
──────────────────────                         ────────────                       ────────────
process_document(doc)
  ├─ run_llm_stage("extract_content")
  │     └─ AgentTaskBatch(A) + 3 tasks ──────▶ (rows in DB)          ◀── pull ─── executor
  └─ returns                                                          ── result ──▶ executor
                                               POST tasks/1/result ✓
                                               POST tasks/2/result ✓
                                               POST tasks/3/result ✓  (last)
                                                 └─ batch.accepted == total
                                                    → continue_extract_content.delay(batch_id)
continue_extract_content(batch)
  ├─ writes RawMemo
  └─ run_llm_stage("classify_document")
        └─ AgentTaskBatch(B) + 1 task …
```

`POST /agent/tasks/{id}/result/` validates the result (below), then inside one
transaction does `select_for_update()` on the batch, increments `accepted`, and
if `accepted == total` dispatches the continuation Celery task **once**. A
second submission for an already-accepted task returns the stored verdict and
does not re-dispatch (idempotent).

Two obligations on every `continue_` task:

- **Idempotent.** The beat task may re-dispatch a continuation that the
  original dispatch also delivered (Celery at-least-once, or a
  `completed_pending_dispatch` retry). `continue_` must upsert `SectionMemo`
  / `SurveyValue` rows keyed by (document, section) / (survey, field, row)
  and check `batch.status` before doing work.
- **Same job bookkeeping as today.** `DocumentProcessor` currently checks the
  Redis cancel flag and updates `DocumentProcessingJob.progress` between
  stages inside one task. After the split each `continue_` does the same at
  its start, so cancellation and progress reporting behave identically in
  both modes.

Properties this buys:

- **No worker is held while waiting.** Plan limit hit, laptop closed, RCIC on
  holiday — the batch just sits in the DB. Worker capacity is untouched.
- **Durable pause.** Progress is rows, not in-memory state. A worker restart
  or deploy loses nothing.
- **No polling.** The continuation is event-driven from the API.
- **Expiry is explicit.** A Celery beat task (`expire_agent_tasks`, every 15
  min) returns expired leases to `queued` and fails batches older than
  `AGENT_BATCH_TTL` (default 7 days), dispatching the continuation with
  `failed=True` so the document / analysis job ends in `FAILED` with a clear
  reason.
- **Cancellation** reuses the existing Redis cancel flag: `stop_document_processing`
  / `stop_analysis` also mark open tasks and batches `cancelled`; the executor's
  next pull no longer sees them.

### Status while waiting

Today `survey_status` auto-fails any document `STARTED` for 30 minutes and any
analysis job untouched for 30 minutes (see
[known-issues.md #3](../design/known-issues.md)). In local mode a document
legitimately waits hours. `mark_waiting_on_agent` sets
`DocumentProcessingJob.progress["waiting_on_agent"] = true` (and the analogue
on `AnalysisJob`); the stale sweep skips jobs with that flag and an open
batch, and the dashboard shows "waiting on RCIC's agent" with the last agent
heartbeat instead of a spinner.

## The `AgentTask` model

```
ai_parse.models.AgentTaskBatch
  id, survey, document?, analysis_job?, stage
  continuation         dotted path of the continue_ Celery task
  continuation_kwargs  JSON
  total, accepted, failed  ints
  status               open | completed | failed | cancelled | expired
  created_at, completed_at

ai_parse.models.AgentTask
  id                  UUID
  batch               FK AgentTaskBatch
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
(same cache and invalidation rules as server mode).

### Lease semantics

- `POST /agent/tasks/pull` returns up to `n` `queued` tasks for the given
  survey(s), sets `leased` with a lease of `ttl` seconds (default 15 min).
- Submitting after lease expiry still succeeds if nobody else has since
  accepted the task; the server validates regardless of who submits.
- `POST /agent/tasks/{id}/release` puts a task back when the agent decides not
  to do it (e.g. plan limit reached).

## Validation on submit

The server never trusts the agent's answer blindly. On `POST .../result`:

1. **Schema.** Result must validate against `payload.output_schema` (the same
   Pydantic model the pipeline would have parsed a Gemini response into). On
   failure → `rejected` with the validation error; task returns to `queued`
   with `attempts+1`; after `max_attempts` (3) → `failed`, which counts toward
   the batch's `failed` and ends the batch with `failed=True`.
2. **Evidence.** For kinds with `evidence_required`, every quoted span must
   appear verbatim in the document text the server holds (`RawMemo`).
   Unverifiable quotes are dropped, exactly as
   `financial_proof.agents.extraction_agent` does today; a fact with no
   surviving evidence is downgraded to `DOUBTFUL`. Caveat: for the
   `extract_content` kind the agent *produces* `RawMemo`, so there is no
   ground truth — text-layer PDFs bypass the model (pdfplumber, deterministic),
   and scanned pages get a per-page character-count sanity threshold plus an
   optional server-side spot-check of one sampled page.
3. **Consistency.** For classification, the type must be one of the allowed
   `DocumentType`s for the survey's application type.
4. **Provenance.** Result is stamped with `actor=agent`, the agent session id,
   the runtime (`claude-code` / `codex`), and the model name the agent
   reports. An `LLMTokenLog` row is still written (provider `local_agent`,
   token counts as reported by the runtime, or null) so the existing
   monitoring keeps working.

Accepted results are then fed to the continuation — `SectionMemo` writes,
`SurveyValue` extraction, conflict detection, formulas — which is the same
code the server-mode path runs inline.

## Ordering and parallelism

Batches are created as the pipeline reaches them, so dependencies are
implicit: classification tasks appear only after the content-extraction batch
has completed. Within a batch tasks are independent and can be leased in
parallel by multiple agent workers — see [runtime-modes.md](runtime-modes.md).

## Failure handling

| Situation | Behaviour |
|---|---|
| Agent submits invalid JSON 3× | task `failed` → batch `failed` → continuation runs with `failed=True` → document `failed`, reason visible in dashboard and in `case_status` |
| Agent offline for 7 days | beat task expires the batch; same as above; `start_document_processing` again re-creates it |
| RCIC switches case back to `llm_mode=server` | open batches `cancelled`; the stage task is re-dispatched and now runs inline on the server provider |
| Agent reports plan-limit | agent releases leases; nothing changes server-side; `case_status` shows "waiting on local agent" |
| File missing locally and S3 download fails | agent releases task with reason; surfaced to RCIC |
| Two agents submit the same task | first accepted result wins; second gets the stored verdict; continuation dispatched once (row lock) |
| API dispatches continuation but Celery is down | Celery `apply_async` failure is caught; batch stays `completed_pending_dispatch`; beat task re-dispatches |

## What does *not* change

- Prompts, section routing (`DocumentTypeSection`), `SurveyValue` semantics,
  conflict detection, formulas, auto-fill data generation, the dashboard.
- Server mode: `llm_mode=server` is the default; after the prepare/continue
  refactor it runs the same calls in the same Celery task as today.
- Celery itself: two queues, Redis broker, existing entrypoints. One new beat
  task.
