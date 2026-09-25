# Known Backend Issues an Agent Will Trip Over

A human using the dashboard works around these; an automated agent will loop,
stall, or misreport. Fix in Phase 0. References are to the current
`uapply-backend` checkout.

| # | Issue | Where | Effect on the agent | Fix |
|---|---|---|---|---|
| 1 | `start_auto_filling` sets `automation_status=STARTED` **before** generating extra files; an exception leaves it `STARTED` forever, and the UI/preflight then treats auto-fill as running | `survey/mixins/survey_handler.py` `handle_start_auto_filling` | the approved auto-fill "starts" then `autofill_status` never changes; agent waits until timeout | wrap in `try/except`, set `FAILED` with reason on error; preflight also treats a `STARTED` older than N minutes with no `ImmPdfFile` progress as stale |
| 2 | Re-analysis without `force=true` takes the incremental path and can strand the survey at `CONFIRMED_DOCUMENTS`; the UI spins forever | `ai_parse` confirmation orchestrator / `start_analysis` | `wait_for_stage("analysis")` never completes | agent always sends `force=true`; server: make the incremental path either finish or fail loudly |
| 3 | `survey_status` auto-fails any document `STARTED`/`ANALYZING` and any analysis job untouched for **30 minutes** | `handle_survey_status` | in local mode a document legitimately waits hours for the agent → gets marked `FAILED` by the next status call | exclude documents/jobs with an open `AgentTask` from the stale sweep (or key staleness on the task lease instead) |
| 4 | `SurveyFieldFormula` results cached 24 h in Redis; editing a value does not invalidate | formula service | agent resolves a value, downstream formula fields stay stale, preflight summary is wrong | invalidate formula cache for the survey on every resolve/approve (part of the new endpoints) |
| 5 | Jinja errors in formulas are swallowed → blank fields with no signal | formula service | agent cannot tell "missing" from "broken" | surface formula errors as `MISSING` with an error note in the review queue |
| 6 | `PromptTemplate` cache is keyed by survey visa scope; edits to a template need invalidation by scope, and a missing section `filter_prompt` fails the whole document | prompt cache | task payloads carry stale prompts; documents `FAILED` with an opaque reason | expose the failure reason in `AgentTask.rejection` / document error; keep existing invalidation rule documented for ops |
| 7 | Vision APIs reject raw DOCX bytes | content loader | n/a once DOCX text is extracted locally, but the server path must not be hit for local-mode tasks | route DOCX `extract_content` to local text extraction only |
| 8 | `bulk_upload` requires `document_type_id`; the generic type is found by `file_name='agent_survey'` | `survey/views.py` `bulk_upload` | agent must know the generic type id up front | return it from `list_application_types` / expose `GET document-types/?generic=true`; accept omitted type = generic |
| 9 | No `sha256` on `Document`; a retried upload creates duplicates | `Document` model | manifest and server disagree after a network failure | add field + dedup (Phase 0) |
| 10 | If the Celery workers are down or a queue is backed up, tasks look queued but nothing progresses; today the UI just spins | ops | agent waits on `wait_for_stage` with no way to tell "backend stuck" from "still working" | `case_status` includes a worker heartbeat (Celery inspect ping or the metrics sidecar) so the agent can report "backend not processing" instead of waiting |

Items 1, 2, 4 and 5 also affect today's dashboard users and are worth fixing
independently of this project.
