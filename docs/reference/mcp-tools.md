# MCP Tool Reference

Tools exposed by `uapply-agent mcp` (stdio) to Claude Code, Codex and the
desktop apps. They are defined in `src/uapply_agent/mcp_server.py`. Design
rules:

- **Coarse.** One tool is roughly one thing the RCIC would click in the
  dashboard. There is no raw CRUD and no generic HTTP tool.
- **Small outputs.** Counts, ids, paths and summaries. Task prompts, task
  inputs and chat message bodies never enter the conversation.
- **Safe to repeat.** Uploads skip files already recorded in the manifest or
  already on the case; `create_case` refuses a folder that already has a case.
- **Folder-relative paths.** File paths are relative to the working folder;
  paths that resolve outside it are refused. Only `set_folder` takes an
  absolute path.

## Result shape and errors

Every tool returns either `{ok: true, ...fields}` or
`{ok: false, error: {code, message, hint}}`. `hint` is written for the model
(for example "loop run_tasks / wait_for_stage('analysis') until done, then
confirm_documents"). Codes are UPPER_SNAKE:

| Code | When |
|---|---|
| `NO_CASE` | the tool needs a bound case and the folder has none |
| `AGENT_API_UNAVAILABLE` | the tool needs the backend's local-agent API (`/api/ai-parse/agent/`) and the backend does not serve it |
| `HTTP_<status>` | the backend answered with an error, e.g. `HTTP_401` (hint: `uapply-agent login`) |
| tool-specific | e.g. `CONFIRMATION_REQUIRED`, `CASE_EXISTS`, `BAD_APPLICATION_TYPE`, `UNKNOWN_TYPE`, `BAD_CATEGORY`, `BAD_MODE`, `BAD_PAGES`, `NO_FILE`, `NO_FOLDER`, `UNSUPPORTED`, `PROCESSING_NOT_DONE`, `ANALYSIS_NOT_DONE`, `CHAT_DISABLED` |
| chat source | `NOT_INSTALLED`, `NOT_LOGGED_IN`, `UNSUPPORTED_PLATFORM`, `CLI_ERROR` (from the AnyChat CLI) |
| other | any unexpected exception, reported as its class name in upper case with a short message |

Tools that act on the case need a bound folder (`NO_CASE` otherwise). The
pipeline and finishing tools, except `start_processing`, also need the
local-agent API (`AGENT_API_UNAVAILABLE` otherwise).

## Tools

### Session

| Tool | Input | Output |
|---|---|---|
| `whoami` | none | `backend`, `logged_in`, `agent_api` (whether the backend serves the local-agent API; `null` when not logged in), `runtimes` (`name`, `path`, `logged_in` for Claude Code, `error` when the binary does not start), `acrobat` (`available`, `path`, `reason` for Adobe Acrobat Pro), `folder`, `case`; a `hint` when no runtime is installed, none starts, or the Claude Code CLI is not signed in |
| `set_folder` | `path` (absolute directory) | `folder`, `case`; points the server at another client folder |
| `case_status` | none | server-side status of the folder's case: documents by status, agent tasks, failures. On a backend without the local-agent API, the survey's own document counts. Called first in every session |
| `init_case` | `survey_id`, `llm_mode?` (`local_agent` \| `server`, default `local_agent`) | `case` (the stored `.uapply/case.json`), `chat_uploads` (queued transcripts filed now), `warning` when the backend has no local-agent API and the case was bound in server mode |

### Files and upload

| Tool | Input | Output |
|---|---|---|
| `scan_folder` | `include_manifested?` (default `false`) | `files`: `path`, `size`, `sha256`, `kind` (`pdf` \| `image` \| `office`), `applicant_hint` (first subfolder), `manifested`, `document_id` |
| `preview_document` | `path`, `pages?` (`"1"` or a range such as `"1-3"`, at most 3 pages; default `"1"`) | PDF: `kind`, `page_count`, `pages`, `images` (PNG paths under `.uapply/cache/preview/`). Image: `kind`, `images` (HEIC converted to JPEG). Rendered locally; nothing is uploaded |
| `list_document_types` | `query?` | `document_types` the case accepts: `id`, `name`, `file_name`, `category`, `requirement`, `can_process`; the generic Agent Survey row (`generic: true`, `use_for`) is for filled IMM forms only |
| `sync_documents` | `document_type_id`, `document_category?` (default: the type's category), `paths?` (default: every file not yet uploaded), `applicant?` (default `principal`), `archive_name?` (default: the type's own archive) | `uploaded` (count), `documents` (`path`, `document_id`, `file_name`), `skipped` (already uploaded), `already_on_server` (a case document of the same type, name and size, recorded instead of uploaded again), `failed` (`path`, `reason`) |
| `list_documents` | none | `documents`: `id`, `file_name`, `status`, `document_type_id`, `page_of` (parent document of a page), `error` |

### Pipeline

| Tool | Input | Output |
|---|---|---|
| `start_processing` | `document_ids?` (default: every uploaded, failed or stopped top-level document of a processable type) | `started`: `document_id`, `ok`, `message` per document. Uploads alone never start processing for agent cases; this also retries failed documents |
| `start_analysis` | none | starts the analysis, or runs it again after sections failed. `started`, `message`, `progress`; refused with `PROCESSING_NOT_DONE` while documents are still processing or not started |
| `run_tasks` | `max_tasks?`, `workers?` (default 2, at most 4), `kinds?`, `budget_s?` (default 90, clamped to 20-300) | runs queued agent tasks in fresh headless runtime processes, then returns `runtime`, `accepted`, `rejected`, `released`, `failed`, `plan_limited`, `runtime_error`, `remaining`, `model_calls`, `text_layer_docs`, `failures`, and `progress` (per-document snapshot). Call again while `remaining` > 0 |
| `wait_for_stage` | `stage` (`processing` \| `analysis` \| `filling`, default `processing`), `timeout_s?` (default 45, at most 60) | the backend's stage status (including `done`) plus `progress`; returns early when the stage completes |
| `task_stats` | none | `stats`: agent task counts by status for the case |
| `set_llm_mode` | `llm_mode` (`local_agent` \| `server`) | `llm_mode`; only after the RCIC asked for the switch |

`run_tasks` is the only way tasks are executed. The MCP server pulls each
task, resolves its inputs to local files, spawns the RCIC's runtime headless
with the task's prompt as a real system prompt, validates the JSON locally and
submits it (see [runtime-modes.md](../architecture/runtime-modes.md)). The
chat model never receives a task payload; pull, submit and release are
backend endpoints used by the executor, not MCP tools.

`wait_for_stage` is a server long-poll capped at 60 s because runtimes impose
their own MCP tool-call timeouts. The playbook alternates `run_tasks` and
`wait_for_stage` until `remaining` is 0 and the stage is done.

### Finishing the case

| Tool | Input | Output |
|---|---|---|
| `confirm_documents` | `continue_with_failed_sections?: bool` (default false) | the dashboard's Confirm step: each archive merged into one PDF and compression queued on uApply (no AI). Returns `archives` (count), `failed_documents`, `status`; refused with `ANALYSIS_NOT_DONE` before the analysis is done, and with `ANALYSIS_INCOMPLETE` (naming the failed sections) when analysis sections failed, unless the RCIC chose to continue |
| `autofill_forms` | `budget_s?` (default 90, clamped to 20-300) | fills the case's IMM PDFs. With Adobe Acrobat Pro on this PC (Windows): `mode: local`, `filled`, `failed`, `skipped`, `remaining` (call again while > 0), copies in `.uapply/output/imm_pdfs/`, and `automation_status` / `imm_pdfs` when finished. Otherwise: `mode: platform` with a `reason`; uApply's platform filler fills the forms (no AI) and the caller loops `wait_for_stage("filling")` |
| `final_report` | `download?` (default `true`) | `report_markdown` (shown to the RCIC as is), `report_path` (`uApply output/report.md`), `links` (`case`, `ai_check`, `submit`, `online_portal`), `files` (`package`, `forms`, or an `error`), `status`, `ai_check` (`conflict`, `doubtful`, `missing`), `online_portal`. With `download` the final package zip is saved in `uApply output/` and its forms unpacked into `uApply output/Forms/` |

### Case creation

| Tool | Input | Output |
|---|---|---|
| `list_application_types` | `query?` | `application_types`: `id`, `code`, `name`, `program`, `visa_type`, `visa_location`, `applicant_type`, `default_imm_pdf_types` |
| `create_case` | `name`, `application_type_id`, `confirmation` | creates the survey (charges the RCIC's account) with the type's default IMM forms, sets `llm_mode=local_agent`, binds the folder and files queued chat transcripts. Returns `survey_id`, `team_id`, `case`, `chat_uploads`, and `warning` when the backend has no local-agent API. Refused with `CONFIRMATION_REQUIRED` unless `confirmation` is exactly `create case` or `确认创建`, with `CASE_EXISTS` when the folder already has a case, and with `BAD_APPLICATION_TYPE` for an unknown type |

### Chat history

Optional, through the AnyChat CLI; see
[architecture/chat-sources.md](../architecture/chat-sources.md).

| Tool | Input | Output |
|---|---|---|
| `chat_sources` | none | `sources`: `source`, `ok`, `state` (`ok` \| `not_installed` \| `unsupported_platform` \| `not_logged_in` \| `cli_error` \| `disabled`), `detail`, `hint` |
| `chat_find_contact` | `name` | `candidates`: `display_name`, `kind` (`friend` \| `group`); raw chat ids are not returned |
| `chat_fetch` | `contact`, `days?` (default: setting `chat_default_days`, 180) | saves the transcript under `.uapply/chat/`, derives intake hints with the RCIC's runtime (headless), and files the transcript on the case unless `chat_upload` is off. Returns `transcript` (`source`, `contact`, `from`, `to`, `path`, `messages`, `chars`), `intake` (hints or `null`), `upload` (the filed document, `"queued"` when no case is bound yet, or `"disabled"`), `usage`, and `intake_error` when the hint call failed. Raw WeChat ids in the result are replaced with `[id]`; message bodies are not returned |
| `chat_upload` | `path?` | files one fetched transcript (`document_id`, `pdf`, `already_filed`) or, without `path`, every queued one (`uploads`) on the bound case as an Agent Survey PDF |

## Deliberately absent

- No `delete_*` tools. Deletion stays in the dashboard.
- No `send_email` / `notify_client`. The agent never contacts a client.
- No `submit_to_portal`. The online portal is started by the RCIC from the
  dashboard.
- No raw `http_request`. The typed client in `api.py` is the only path to the
  API.

## Planned (not implemented)

These tools appear in the design documents but do not exist in the server
today:

- `propose_case` / `add_dependent`: case setup and dependents through a dashboard approval page.
- `wait_for_approval` / `list_approvals`: poll and list pending dashboard approvals.
- `get_document_text`: capped, page-marked text excerpt of a processed document.
- `render_pages`: render pages of an uploaded document for the model to look at.
- `stop_processing` / `stop_analysis`: cancel a running stage and its open tasks.
- `reclassify_document`: change a document's type with an audited reason.
- `missing_documents`: required document types with no document, per applicant.
- `get_review_queue`: DOUBTFUL / CONFLICT / MISSING values with evidence.
- `resolve_value` / `propose_resolution` / `withdraw_proposal`: resolve or propose values with evidence.
- `request_approval`: one approval page for a batch of proposals.
- `add_client_question`: record a question for the client, shown in the dashboard.
- `autofill_preflight` / `request_autofill`: auto-fill blockers and an approval page before auto-fill starts.
