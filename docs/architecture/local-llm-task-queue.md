# Local-LLM Task Queue

The core mechanism that makes "local tokens" work without forking the pipeline.
The backend keeps orchestrating; every LLM call becomes a task that the agent on
the RCIC's machine executes with the model inside Claude Code / Codex.

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

## The change: a `LocalAgentLLM` provider

```
ai_parse/integrations/llm/local_agent_llm.py   (new)

class LocalAgentLLM(BaseLLM):
    async def aask_to_json(self, system_prompt, user_prompt, response_format, images=None, **kw):
        task = await AgentTask.objects.acreate(
            survey=ctx.survey, kind=ctx.kind, document=ctx.document,
            payload={
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "output_schema": response_format.model_json_schema(),
                "inputs": describe_inputs(images, ctx),   # local paths + hashes, page numbers
                "evidence_required": ctx.evidence_required,
            },
            temporal_task_token=activity.info().task_token,
        )
        activity.raise_complete_async()                  # see "Waiting" below
```

- `survey.llm_mode ∈ {server, local_agent}` selects the provider for a case.
  `LLMService` is built per case: `LLMService(LocalAgentLLM())` vs the usual
  `LLMService(GeminiClient(), OpenAIClient())`.
- A per-**kind** override (`AGENT_LOCAL_TASK_KINDS`) lets you keep some steps
  server-side (e.g. analysis prompts you consider IP, or web-search steps).
- The pipeline code is untouched except for passing a small `ctx` (survey,
  document, task kind, evidence flag) — most of which is already in scope where
  the calls are made.

### Inputs: never ship the file, ship a reference

The agent already has the files — they came from the folder it uploaded. The
task carries `{"local_path": "passport.pdf", "sha256": "…", "pages": [1,2]}`.
The agent verifies the hash before use; if the file is gone (RCIC moved it),
the agent downloads it from S3 via the existing `documents/{id}/download/`.

Server-side page rendering / pdfplumber / docx text are **moved to the agent**
(`local_ops/`) in local mode. They need no model, and rendering on the server
just to ship PNGs back down is wasteful. In server mode nothing changes.

## Waiting: Temporal async activity completion

The backend runs pipeline steps as Temporal activities (post-Celery). A
Temporal activity may return `raise_complete_async()`: the worker slot is freed,
the workflow stays open, and anyone holding the **task token** can complete it
later with a result or a failure.

```
workflow: process_document
  ├─ activity extract_content      → LocalAgentLLM → AgentTask(A) → complete_async
  │      ... hours may pass ...
  │   POST /agent/tasks/A/result   → validate → client.complete_activity(token, result)
  ├─ activity classify             → AgentTask(B) …
  └─ activity write_section_memos  (no LLM, runs normally)
```

Properties this buys:

- **Durable pause.** Plan limit hit, laptop closed, RCIC on holiday — the
  workflow just waits. Set a generous activity `schedule_to_close_timeout`
  (e.g. 7 days) and a heartbeat-free wait; on timeout the task is marked
  `expired` and the document `failed` with a clear reason.
- **No polling loops inside the workflow.** No `sleep` / retry to check whether
  the agent has answered.
- **Cancellation** already exists (Redis cancel flag between steps); cancelling
  a document also marks its open tasks `cancelled` so the agent skips them.

> This design depends on the Temporal migration. The Celery-era code in the
> current checkout would block a worker for the entire wait — do not build it
> there.

## The `AgentTask` model

```
ai_parse.models.AgentTask
  id                  UUID
  survey              FK Survey
  document            FK Document, null            (per-document kinds)
  analysis_job        FK AnalysisJob, null         (analysis kinds)
  kind                CharField  (see reference/agent-task-schema.md)
  status              queued | leased | submitted | accepted | rejected | expired | cancelled
  payload             JSON       prompts, schema, input refs, flags
  result              JSON, null validated result
  rejection           JSON, null {code, message, attempt}
  attempts            int
  lease_owner         CharField  agent session id
  lease_expires_at    datetime
  temporal_task_token bytes
  created_at / submitted_at / completed_at
```

Prompt text in `payload` is resolved from `PromptTemplate` at creation time
(same cache and invalidation rules as server mode).

### Lease semantics

- `POST /agent/tasks/pull` returns up to `n` `queued` tasks for the given
  survey(s), sets `leased` with a lease of `ttl` seconds (default 15 min).
- Submitting after lease expiry still succeeds if the task is still `leased`
  by nobody else (last writer wins is fine — results are deterministic enough
  and the server validates).
- `POST /agent/tasks/{id}/release` puts a task back when the agent decides not
  to do it (e.g. plan limit reached).

## Validation on submit

The server never trusts the agent's answer blindly. On `POST .../result`:

1. **Schema.** Result must validate against `payload.output_schema` (the same
   Pydantic model the pipeline would have parsed a Gemini response into). On
   failure → `rejected` with the validation error; task returns to `queued`
   with `attempts+1`; after `max_attempts` (3) → `failed`.
2. **Evidence.** For kinds with `evidence_required`, every quoted span must
   appear verbatim in the document text the server holds (`RawMemo`, or the
   agent-submitted extraction for the OCR kind). Unverifiable quotes are
   dropped, exactly as `financial_proof.agents.extraction_agent` does today;
   a fact with no surviving evidence is downgraded to `DOUBTFUL`.
3. **Consistency.** For classification, the type must be one of the allowed
   `DocumentType`s for the survey's application type.
4. **Provenance.** Result is stamped with `actor=agent`, the agent session id,
   the runtime (`claude-code` / `codex`), and the model name the agent
   reports. An `LLMTokenLog` row is still written (provider `local_agent`,
   token counts as reported by the runtime, or null) so the existing
   monitoring keeps working.

Accepted results are then fed back into the *unchanged* pipeline — `SectionMemo`
writes, `SurveyValue` extraction, conflict detection, formulas — via
`complete_activity`.

## Ordering and parallelism

Tasks are created as the workflow reaches them, so dependencies are implicit:
classification tasks appear only after content extraction has been accepted.
Within a stage (e.g. 12 sections of one document) tasks are independent and can
be leased in parallel by multiple agent workers — see
[runtime-modes.md](runtime-modes.md).

## Failure handling

| Situation | Behaviour |
|---|---|
| Agent submits invalid JSON 3× | task `failed`, activity completed with failure, document `failed`, reason visible in dashboard and in `status` tool |
| Agent offline for 7 days | task `expired`, same as above; `start_document_processing` again re-queues |
| RCIC switches case back to `llm_mode=server` | open tasks `cancelled`; workflow signalled to retry the activity with the server provider |
| Agent reports plan-limit | agent releases leases; nothing changes server-side; `status` tool shows "waiting on local agent" |
| File missing locally and S3 download fails | agent releases task with reason; surfaced to RCIC |

## What does *not* change

- Prompts, section routing (`DocumentTypeSection`), `SurveyValue` semantics,
  conflict detection, formulas, auto-fill data generation, the dashboard.
- Server mode: `llm_mode=server` is the default and behaves exactly as today.
