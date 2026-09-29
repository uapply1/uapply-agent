# Backend API Used by the Agent

Every call the agent makes goes through the typed client in
`src/uapply_agent/api.py`. Requests carry `Authorization: Bearer <token>`: the
RCIC's normal Auth0 JWT, the same one the dashboard uses. The agent refreshes
it with the stored refresh token when the backend answers `401`.

Endpoints under `/api/ai-parse/agent/` form the local-agent API. The agent
probes it with `GET /api/ai-parse/agent/tasks/stats/`; a backend that answers
with an HTML `404` has no local-agent API, and the agent then binds cases in
server mode and refuses `run_tasks` (`AGENT_API_UNAVAILABLE`).

## Auth

| | |
|---|---|
| Auth0 Device Authorization Grant | `uapply-agent login` talks to Auth0 directly (native client, `device_code` flow, `offline_access` scope when the tenant allows it). No backend endpoint is involved |
| Pasted token | `uapply-agent login --token` / `--token-stdin`, or `UAPPLY_TOKEN` |

## Surveys (cases)

| Endpoint | Used by |
|---|---|
| `GET  /api/survey/surveys/{id}/` | case detail: name, documents, the case's document types, dependents (`init_case`, `list_documents`, `start_processing`, progress) |
| `POST /api/survey/surveys/` | `create_case`: `name`, `application_type_id`, `llm_mode`, `imm_pdf_types`, `team_id` |
| `GET  /api/survey/application-types/` | `list_application_types`, `create_case` |
| `GET  /api/survey/document-types/` | the generic Agent Survey type (`file_name="agent_survey"`) |
| `GET  /api/teams/` | `create_case` picks the team when the RCIC belongs to exactly one |

## Documents

| Endpoint | Used by |
|---|---|
| `POST /api/survey/documents/bulk_upload/` | `sync_documents`, chat transcript filing; multipart `files` with `survey_id`, `document_category`, `document_type_id`, `archive_name` |
| `GET  /api/survey/documents/{id}/download/` | executor: a presigned URL for each task input document, downloaded into `.uapply/cache/<task id>/` without the bearer token |

## Pipeline

| Endpoint | Used by |
|---|---|
| `POST /api/ai-parse/surveys/{id}/start_document_processing/` | `start_processing`, one call per document (`document_id`) |
| `POST /api/ai-parse/surveys/{id}/start_analysis/` | `start_analysis` |

## Local-agent API

| Endpoint | Body / params | Used by |
|---|---|---|
| `GET  /api/ai-parse/agent/surveys/{id}/status/` | | `case_status`, `confirm_documents`, `autofill_forms`, `uapply-agent status` |
| `GET  /api/ai-parse/agent/surveys/{id}/wait/` | `stage` (`processing` \| `analysis` \| `filling`), `timeout` (≤ 60 s) | `wait_for_stage`, `uapply-agent run --follow` |
| `POST /api/ai-parse/agent/surveys/{id}/llm_mode/` | `{llm_mode}` | `init_case`, `create_case`, `set_llm_mode` |
| `POST /api/ai-parse/agent/tasks/pull/` | `{survey_ids, n, session_id, runtime, kinds?}` | executor; returns `{tasks: [...]}` and leases them |
| `POST /api/ai-parse/agent/tasks/{id}/result/` | `{result, model, runtime, usage, session_id}` | executor; the server validates the result and, when the last task of a batch is accepted, resumes the pipeline |
| `POST /api/ai-parse/agent/tasks/{id}/release/` | `{reason}` | executor; puts a task back (e.g. plan limit) |
| `GET  /api/ai-parse/agent/tasks/stats/` | `survey` | `task_stats`, the local-agent API probe |
| `POST /api/ai-parse/agent/surveys/{id}/autofill/claim/` | | `autofill_forms` (local mode): forms to fill with their templates and recorded field operations |
| `POST /api/ai-parse/agent/surveys/{id}/autofill/{imm_pdf_id}/result/` | multipart: filled PDF or `error`, `success_rate`, `errors`, `survey_values_updated_at` | `autofill_forms` (local mode), per form |
| `POST /api/ai-parse/agent/surveys/{id}/autofill/finish/` | | `autofill_forms` (local mode); may ask for a refill when values changed during the fill |
| `GET  /api/ai-parse/agent/surveys/{id}/report/` | | `final_report` |

Validation performed on `result/` is described in
[local-llm-task-queue.md § Validation](../architecture/local-llm-task-queue.md#validation-on-submit).

## Finishing

| Endpoint | Used by |
|---|---|
| `POST /api/survey/surveys/{id}/generate_archive_files/` | `confirm_documents`: the dashboard's Confirm (archives merged, compression queued; no AI) |
| `POST /api/survey/surveys/{id}/start_auto_filling/` | `autofill_forms` (platform mode): uApply's own filler, no AI |
| `POST /api/survey/surveys/{id}/download_zip_submit_files/` | `final_report`: the final package zip, streamed into `uApply output/` |

## Errors

Backend errors reach the chat model as `HTTP_<status>` tool errors with the
backend's message; `401` adds the hint to run `uapply-agent login`. A backend
without the local-agent API produces `AGENT_API_UNAVAILABLE`.

## Planned (not implemented)

Endpoints the design documents describe for later stages; the agent does not
call them today.

- Agent-scoped tokens (`scope=agent:cases`) enforced by a DRF permission class; writes tagged `actor=agent`.
- `sha256` stored on `Document` with deduplication in `bulk_upload`, and an `Idempotency-Key` header on mutations.
- `POST /api/survey/documents/{id}/reclassify/` and a missing-documents endpoint.
- `GET /api/ai-parse/agent/documents/{id}/text/`: capped text excerpt of a processed document.
- Review queue: `GET .../surveys/{id}/review-queue/`, `POST .../values/{id}/resolve/` (with `expected_updated_at`), `.../propose/`, `.../proposals/{id}/withdraw/`, with a server-side list of legally significant fields.
- Approvals: `POST .../approvals/`, `GET .../approvals/{id}/`, and a user-session-only `decide/` endpoint behind a dashboard approval page.
- `GET .../surveys/{id}/autofill-preflight/`, client questions, and an append-only audit log per survey.
