# MCP Tool Reference

Tools exposed by `uapply-agent mcp` (stdio) to Claude Code / Codex. Design
rules for every tool:

- **Coarse.** One tool ≈ one thing the RCIC would click. No raw CRUD.
- **Small outputs.** Summaries, ids, evidence snippets. Never raw OCR text
  unless explicitly asked (`get_document_text`, capped).
- **Idempotent.** Calling twice is safe; the second call returns current state.
- **Folder-relative paths.** All paths are relative to the working folder; the
  server refuses anything outside it.
- **No side effects the RCIC hasn't sanctioned.** Tools that create a case,
  approve legally significant values, or start auto-fill do not act directly.
  They create a **pending approval** on the server and return an
  `approval_url`; the RCIC opens it (already logged in to uApply) and clicks
  Approve or Reject; the agent then calls `wait_for_approval`. A model cannot
  fake the click, so nothing the model passes as an argument is treated as
  consent (see [guardrails.md § Approval channel](../design/guardrails.md#approval-channel)).

All tools return `{ok, data?, error?: {code, message, hint}}`. `hint` is
written for the model ("run `wait_for_stage` before calling this again").

## Session

| Tool | Input | Output |
|---|---|---|
| `whoami` | — | backend URL, login state, `agent_api` (does the backend serve the local-agent API), runtimes as `{name, path, logged_in}`, a `hint` when no CLI is installed or signed in, folder, case |
| `case_status` | — | server-truth status of the folder's case: stage, document counts by status, open review items, automation status, open agent tasks, blockers. First call in every session. |

## Intake

| Tool | Input | Output |
|---|---|---|
| `scan_folder` | `include_manifested?: bool` | files: path, size, sha256, kind, applicant hint (from subfolder), manifested (bool) |
| `preview_document` | `path`, `pages?: "1" \| "1-3"` (≤ 3) | PNG paths under `.uapply/cache/preview/` for the chat model to Read before classifying; images returned as-is (HEIC converted). Local only. |
| `list_application_types` | `query?: string` | id, program, visa_type, visa_location, label |
| `propose_case` | `application_type_id`, `principal: {name, …}`, `dependents: [{name, relationship, folder?}]` | `approval_id`, `approval_url` — the page shows the proposed setup with Approve / Edit / Reject |
| `add_dependent` | `name`, `relationship`, `folder?` | same approval flow as `propose_case` |

## Approvals

| Tool | Input | Output |
|---|---|---|
| `wait_for_approval` | `approval_id`, `timeout_s?` (≤ 60) | `status: pending \| approved \| rejected \| edited`, plus the (possibly edited) payload; used for case creation, proposal batches and auto-fill. On an approved `propose_case` the server has already created the case and the tool writes `case.json` |
| `list_approvals` | `status?` | pending approvals for this case with their URLs — so a resumed session can re-surface them |

The MCP server prints the URL in the tool result **and** attempts to open it
in the RCIC's default browser. Approval pages are ordinary dashboard routes
that require the RCIC's normal login.

## Upload

| Tool | Input | Output |
|---|---|---|
| `sync_documents` | `document_type_id` (a case type, or the generic Agent Survey type for filled IMM forms; anything else is refused with `UNKNOWN_TYPE`), `paths?: string[]` (default: all unmanifested), `applicant?`, `document_category?` (default: the type's), `archive_name?` (default: the type's own archive, i.e. the dashboard's default folder) | uploaded: n, skipped (already manifested), already_on_server: [{path, document_id}] (a case document of the same type, name and size — recorded, not re-uploaded), failed: [{path, reason}] |
| `list_documents` | `applicant?`, `status?` | id, path, applicant, document_type, confidence, status, pages |
| `get_document_text` | `document_id`, `pages?: int[]`, `max_chars?` (default 4000) | page-marked text excerpt from `RawMemo` |
| `render_pages` | `document_id` or `path`, `pages: int[]`, `dpi?` | local image paths in `.uapply/cache/` (for the model to look at) |

## Pipeline

| Tool | Input | Output |
|---|---|---|
| `start_processing` | `document_ids?: string[]` | job ids; count of agent tasks expected |
| `run_analysis` | `force?: bool` (default **true**) | analysis job id |
| `wait_for_stage` | `stage: processing \| analysis \| formulas`, `timeout_s?` (default 45, ≤ 60) | done: bool, progress: {completed, failed, pending, open_agent_tasks}, failures: [{document_id, reason}] |
| `stop_processing` / `stop_analysis` | — | ack |

`wait_for_stage` is a server long-poll: it returns early on completion, else at
`timeout_s`. It is capped at 60 s because MCP tool calls have runtime-imposed
timeouts (Claude Code and Codex differ, and the defaults are well under five
minutes). The playbook calls it in a loop with a task-loop in between.

## Task execution (local tokens)

| Tool | Input | Output |
|---|---|---|
| `run_tasks` | `kinds?: string[]`, `max_tasks?` (default: all queued), `workers?` (default 2, ≤ 4) | counts: {accepted, rejected, released, remaining}, `plan_limited: bool`, failures: [{task_id, kind, reason}] |
| `task_stats` | — | queued / leased / submitted / failed counts for the case |

`run_tasks` is the **only** way tasks are executed. The MCP server pulls each
task, resolves inputs to local files, spawns the RCIC's runtime headless with
the task's prompt as a real system prompt, validates the JSON locally, and
submits — see [runtime-modes.md](../architecture/runtime-modes.md). The chat
model never receives a task payload. Pull / submit / release exist as backend
endpoints for the executor, not as MCP tools.

`run_tasks` returns within the MCP timeout by processing tasks in slices; the
playbook calls it in a loop with `wait_for_stage` until `remaining == 0` and
the stage is done.

## Classification review

| Tool | Input | Output |
|---|---|---|
| `reclassify_document` | `document_id`, `document_type_id`, `reason` | ack; audited |
| `missing_documents` | — | required document types with no document, per applicant |

## Review queue

| Tool | Input | Output |
|---|---|---|
| `get_review_queue` | `status?: doubtful \| conflict \| missing`, `applicant?`, `significant_only?` | items: [{value_id, field, description, applicant, status, significant, candidates: [{value, document_id, path, page, quote, status}], proposal?}] |
| `resolve_value` | `value_id`, `value`, `rationale`, `evidence: [{document_id, page, quote}]`, `expected_updated_at` (from the review-queue item) | new status; **rejected with `SIGNIFICANT_FIELD`** if the field is on the allow-list — use `propose_resolution`; **rejected with `STALE_VALUE`** if the RCIC edited it since the queue was read — re-fetch and reconsider |
| `propose_resolution` | same as above | proposal id; status stays CONFLICT/DOUBTFUL |
| `request_approval` | `proposal_ids: string[]` | `approval_id`, `approval_url` — one page listing every proposal with value, rationale, evidence and per-row Approve / Reject; then `wait_for_approval` |
| `withdraw_proposal` | `proposal_id`, `reason` | ack (the agent changed its mind; RCIC rejection happens on the approval page) |
| `add_client_question` | `field`, `question`, `why`, `satisfying_documents?` | appended to `review.md`; also stored server-side *(new)* so the dashboard shows it |

## Auto-fill

| Tool | Input | Output |
|---|---|---|
| `autofill_preflight` | — | ok: bool, blockers: [{code, detail}], summary: {filled, missing, assumed} |
| `request_autofill` | — | `approval_id`, `approval_url` — page shows the preflight summary; on approval the server starts auto-fill itself; fails if preflight blockers exist |
| `autofill_status` | — | automation_status, imm pdf statuses |
| `download_output` | `what: l3 \| imm_pdfs \| all` | paths under `.uapply/output/` |

## Chat history (optional, AnyChat)

| Tool | Input | Output |
|---|---|---|
| `chat_sources` | — | per-source availability: `ok`, `state` (ok \| not_installed \| unsupported_platform \| not_logged_in \| cli_error \| disabled), hint |
| `chat_find_contact` | `name` | candidates as display names only |
| `chat_fetch` | `contact`, `days?` (default 365) | transcript summary (path, messages, range), redacted `intake` hints, `upload` (document id or `"queued"`), usage |
| `chat_upload` | `path?` | uploads one transcript or every queued one as an agent_survey PDF |
| `list_application_types` | `query?` | id, code, name, program, visa_type, visa_location |
| `create_case` | `name`, `application_type_id`, `confirmation` | refused unless `confirmation` is "create case" / "确认创建"; creates the survey (charges the account), binds the folder, flushes queued uploads |

See [architecture/chat-sources.md](../architecture/chat-sources.md).

## Report

| Tool | Input | Output |
|---|---|---|
| `write_review_report` | — | path of `review.md`; the MCP server assembles it from server state + local notes so the model doesn't hand-write it |

## Deliberately absent

- No `delete_*` tools. Deletion stays in the dashboard.
- No `send_email` / `notify_client`. Questions go to `review.md`.
- No `submit_to_portal`.
- No raw `http_request`. The typed client is the only path to the API.
