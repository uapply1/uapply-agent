# Backend API Used by the Agent

Existing endpoints are listed as they are in `uapply-backend` today; items
marked **(new)** or **(change)** are the backend work this project adds. The
full existing inventory is in
[`uapply-backend/docs/reference/api.md`](../../../uapply-backend/docs/reference/api.md).

All requests carry `Authorization: Bearer <agent token>` (see
[Auth](#auth)) and, for mutations, `Idempotency-Key: <uuid>`.

## Auth

| | |
|---|---|
| **(new)** `POST /api/auth/device/start/` | Begin Auth0 device-code flow; returns `user_code`, `verification_uri`, `device_code`, interval |
| **(new)** `POST /api/auth/device/poll/` | Exchange `device_code` for an **agent token** once the RCIC approves in the browser |
| **(new)** `POST /api/auth/agent-token/refresh/` | Refresh; refresh token lives in the OS keychain on the RCIC machine |

Agent tokens carry scope `agent:cases` limited to the RCIC's team and cannot
call team-admin, billing or delete endpoints. Every write made with an agent
token is tagged `actor=agent`.

## Surveys (cases)

| Endpoint | Use | Notes |
|---|---|---|
| `POST /api/survey/surveys/` | create case | **(change)** accept `llm_mode: server \| local_agent` |
| `POST /api/survey/surveys/{id}/add_dependent/` | family | |
| `GET  /api/survey/surveys/{id}/survey_status/?type=detail` | status | already auto-fails stale jobs after 30 min — see [known-issues](../design/known-issues.md) |
| `GET  /api/survey/application-types/` | intake | |
| `GET  /api/survey/document-types/` | classification, missing-docs | |
| **(new)** `GET /api/agent/surveys/{id}/status/` | one-call status for `case_status` tool | aggregates documents, review counts, open tasks, blockers |
| **(new)** `GET /api/agent/surveys/{id}/wait/?stage=&timeout=` | long-poll | returns on stage completion or timeout ≤ 300 s |

## Documents

| Endpoint | Use | Notes |
|---|---|---|
| `POST /api/survey/documents/bulk_upload/` | upload | **(change)** accept per-file `sha256[]`; store on `Document.sha256` **(new field)**; skip duplicates within the survey |
| `GET  /api/survey/documents/{id}/download/` | fetch file when missing locally | |
| `GET  /api/survey/documents/?survey=` | list | **(change)** include `classification_confidence` |
| **(new)** `POST /api/survey/documents/{id}/reclassify/` | `{document_type_id, reason}` | audited; re-queues processing for that document |
| **(new)** `GET /api/agent/surveys/{id}/missing-documents/` | required types with no document | derived from `ApplicationType` requirements (`DocumentRequirement.REQUIRED`) |
| **(new)** `GET /api/agent/documents/{id}/text/?pages=&max_chars=` | `RawMemo` excerpt | |

## Pipeline

| Endpoint | Use | Notes |
|---|---|---|
| `POST /api/ai-parse/surveys/{id}/start_document_processing/` | classify stage | in `local_agent` mode creates `AgentTask`s instead of calling Gemini |
| `POST /api/ai-parse/surveys/{id}/stop_document_processing/` | | also cancels open tasks |
| `POST /api/ai-parse/surveys/{id}/start_analysis/` | extract stage | **(change)** honour `force=true` (see known-issues) |
| `POST /api/ai-parse/surveys/{id}/stop_analysis/` | | |

## Agent tasks **(new)**

| Endpoint | Body / params | Returns |
|---|---|---|
| `POST /api/agent/tasks/pull/` | `{survey_ids, n, kinds?, lease_s?, session_id, runtime}` | `[{id, kind, payload}]`; sets `leased` |
| `POST /api/agent/tasks/{id}/result/` | `{result, model?, usage?}` | `{accepted, rejection?}`; on accept completes the Temporal activity |
| `POST /api/agent/tasks/{id}/release/` | `{reason}` | ack |
| `GET  /api/agent/tasks/stats/?survey=` | | counts by status |

Validation performed on `result/` is specified in
[local-llm-task-queue.md § Validation](../architecture/local-llm-task-queue.md#validation-on-submit).

## Review queue **(new)**

| Endpoint | Body / params | Notes |
|---|---|---|
| `GET  /api/agent/surveys/{id}/review-queue/?status=&applicant=&significant_only=` | | each item includes candidates with `document_id, page, quote` (from `SurveyValue.source_documents` + `SectionMemo` evidence) and `significant: bool` |
| `POST /api/agent/values/{id}/resolve/` | `{value, rationale, evidence[]}` | 403 `SIGNIFICANT_FIELD` for allow-listed fields when actor=agent; sets `CONFIRMED`; writes audit; invalidates formula cache for the survey |
| `POST /api/agent/values/{id}/propose/` | same | creates `ValueProposal` **(new model)**; dashboard shows it |
| `POST /api/agent/proposals/approve/` | `{proposal_ids}` | requires a **user** token or an agent token with `rcic_confirmed` header set by the MCP server only after the gate; writes audit as actor=rcic-via-agent |
| `POST /api/agent/proposals/{id}/reject/` | `{reason}` | |
| `POST /api/agent/surveys/{id}/client-questions/` | `{field, question, why, satisfying_documents?}` | stored; shown in dashboard; never emailed |

Significance allow-list: `settings.AGENT_SIGNIFICANT_FIELDS` (field names /
regexes) — DOB, given/family names, passport number & expiry, marital status,
relationship, refusal history, criminal/medical declarations, and any
date field feeding a travel/employment/education/residence history table.

## Auto-fill

| Endpoint | Use | Notes |
|---|---|---|
| **(new)** `GET /api/agent/surveys/{id}/autofill-preflight/` | blockers + summary | blocks on open CONFLICT, pending proposals, `automation_status` stuck |
| `POST /api/survey/surveys/{id}/start_auto_filling/` | start | **(fix)** wrap in try/except; on failure set `automation_status=FAILED`, not leave `STARTED` |
| `POST /api/survey/surveys/{id}/l3_data/` | fetch L3 JSON | |
| `GET  /api/survey/imm-pdfs/{id}/download/` | filled IMM PDFs | |

## Audit **(new)**

| Endpoint | |
|---|---|
| `GET /api/agent/surveys/{id}/audit/` | append-only log of every agent write: actor, session, runtime, model, tool, target, before/after, rationale, evidence |

Modelled on `financial_proof.ReviewAction`; shown in the dashboard as an
"Agent activity" tab.

## Error contract

Existing convention: `{"success": false, "message": ..., "error": ...}`. Agent
endpoints add a machine `code` and a `hint` string written for the model:

```json
{"success": false, "code": "STAGE_NOT_READY",
 "message": "Analysis cannot start: 3 documents still processing",
 "hint": "Call wait_for_stage('processing') then retry."}
```
