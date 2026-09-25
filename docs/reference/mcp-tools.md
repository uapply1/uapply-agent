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
  resolve legally significant values, or start auto-fill require the caller to
  pass `confirmed_by_rcic: true`; the playbook only sets it after asking. The
  server-side guardrails do not rely on this flag (see
  [guardrails.md](../design/guardrails.md)) — it is a second line.

All tools return `{ok, data?, error?: {code, message, hint}}`. `hint` is
written for the model ("run `wait_for_stage` before calling this again").

## Session

| Tool | Input | Output |
|---|---|---|
| `whoami` | — | RCIC name, team, backend URL, token scope, runtime detected |
| `case_status` | — | server-truth status of the folder's case: stage, document counts by status, open review items, automation status, open agent tasks, blockers. First call in every session. |

## Intake

| Tool | Input | Output |
|---|---|---|
| `scan_folder` | `include_manifested?: bool` | files: path, size, sha256, kind, applicant hint (from subfolder), manifested (bool) |
| `list_application_types` | `query?: string` | id, program, visa_type, visa_location, label |
| `create_case` | `application_type_id`, `principal: {name, …}`, `dependents: [{name, relationship, folder?}]`, `confirmed_by_rcic: true` | survey ids; writes `case.json` |
| `add_dependent` | `name`, `relationship`, `folder?`, `confirmed_by_rcic: true` | survey id |

## Upload

| Tool | Input | Output |
|---|---|---|
| `sync_documents` | `paths?: string[]` (default: all unmanifested), `applicant?`, `document_category?`, `document_type_id?` | uploaded: n, skipped (already manifested): n, failed: [{path, reason}] |
| `list_documents` | `applicant?`, `status?` | id, path, applicant, document_type, confidence, status, pages |
| `get_document_text` | `document_id`, `pages?: int[]`, `max_chars?` (default 4000) | page-marked text excerpt from `RawMemo` |
| `render_pages` | `document_id` or `path`, `pages: int[]`, `dpi?` | local image paths in `.uapply/cache/` (for the model to look at) |

## Pipeline

| Tool | Input | Output |
|---|---|---|
| `start_processing` | `document_ids?: string[]` | job ids; count of agent tasks expected |
| `run_analysis` | `force?: bool` (default **true**) | analysis job id |
| `wait_for_stage` | `stage: processing \| analysis \| formulas`, `timeout_s?` (≤ 300) | done: bool, progress: {completed, failed, pending, open_agent_tasks}, failures: [{document_id, reason}] |
| `stop_processing` / `stop_analysis` | — | ack |

`wait_for_stage` is a server long-poll: it returns early on completion, else at
`timeout_s`. The playbook calls it in a loop with a task-loop in between.

## Task loop (local tokens)

| Tool | Input | Output |
|---|---|---|
| `pull_tasks` | `n?` (default 5), `kinds?: string[]`, `lease_s?` | tasks: [{id, kind, payload}] — payload shape in [agent-task-schema.md](agent-task-schema.md); input file refs are resolved to local paths (downloaded from S3 to `cache/` if missing) |
| `submit_result` | `task_id`, `result: object`, `model?: string`, `usage?: {input_tokens, output_tokens}` | accepted: bool, rejection?: {code, message} |
| `release_task` | `task_id`, `reason` | ack |
| `task_stats` | — | queued / leased / submitted / failed counts for the case |

The MCP server performs local schema validation before submitting and returns
the validation error to the model without a network round-trip.

## Classification review

| Tool | Input | Output |
|---|---|---|
| `reclassify_document` | `document_id`, `document_type_id`, `reason` | ack; audited |
| `missing_documents` | — | required document types with no document, per applicant |

## Review queue

| Tool | Input | Output |
|---|---|---|
| `get_review_queue` | `status?: doubtful \| conflict \| missing`, `applicant?`, `significant_only?` | items: [{value_id, field, description, applicant, status, significant, candidates: [{value, document_id, path, page, quote, status}], proposal?}] |
| `resolve_value` | `value_id`, `value`, `rationale`, `evidence: [{document_id, page, quote}]` | new status; **rejected with `SIGNIFICANT_FIELD`** if the field is on the allow-list — use `propose_resolution` |
| `propose_resolution` | same as above | proposal id; status stays CONFLICT/DOUBTFUL |
| `approve_proposals` | `proposal_ids: string[]`, `confirmed_by_rcic: true` | resolved count |
| `reject_proposal` | `proposal_id`, `reason` | ack |
| `add_client_question` | `field`, `question`, `why`, `satisfying_documents?` | appended to `review.md`; also stored server-side *(new)* so the dashboard shows it |

## Auto-fill

| Tool | Input | Output |
|---|---|---|
| `autofill_preflight` | — | ok: bool, blockers: [{code, detail}], summary: {filled, missing, assumed} |
| `start_autofill` | `confirmed_by_rcic: true` | ack; fails if preflight blockers exist |
| `autofill_status` | — | automation_status, imm pdf statuses |
| `download_output` | `what: l3 \| imm_pdfs \| all` | paths under `.uapply/output/` |

## Report

| Tool | Input | Output |
|---|---|---|
| `write_review_report` | — | path of `review.md`; the MCP server assembles it from server state + local notes so the model doesn't hand-write it |

## Deliberately absent

- No `delete_*` tools. Deletion stays in the dashboard.
- No `send_email` / `notify_client`. Questions go to `review.md`.
- No `submit_to_portal`.
- No raw `http_request`. The typed client is the only path to the API.
