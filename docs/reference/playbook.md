# Playbook: Operator Instructions for the Coding Agent

The playbook is the natural-language half of the product: it tells Claude Code
or Codex *how* to run a uApply case with the tools in
[mcp-tools.md](mcp-tools.md). It is written once, in
`src/uapply_agent/SOURCE.md`, and shipped inside the package.

## Packaging

`playbook.py` splits `SOURCE.md` by its `##` headings:

- **`## instructions`** becomes the MCP server `instructions`, sent to the
  runtime on connect. Claude Code, the Claude desktop app and Codex surface
  them to the model.
- **`## prompt: <name>`** sections become MCP prompts. Runtimes expose them as
  commands (`/uapply:run (MCP)` in Claude Code, Codex's prompt picker).
- **Claude Code plugin.** `uapply-agent setup` also writes a small plugin to
  `~/.claude/skills/uapply/` from the same prompts, so `/uapply:run`,
  `/uapply:status` and `/uapply:intake-from-chat` are plain slash commands. A
  self-update rewrites it at the next start.

One definition serves every surface, with no build step.

## Commands

| Command | Does |
|---|---|
| `/uapply:run` | The whole case in one turn. Starts with `case_status`; with no bound case it previews the identity documents and asks the RCIC to create a new case (confirmation) or use an existing survey id. Then it picks document types, uploads, runs processing and analysis, and finishes with `confirm_documents`, `autofill_forms` and `final_report` |
| `/uapply:intake-from-chat` | Set up a case from the RCIC's chat history with a client: `chat_sources` → `chat_find_contact` → `chat_fetch`, present the intake hints and the suggested application type, ask the create-case question, then run processing |
| `/uapply:status` | Summarise `case_status` in five lines or fewer; once the analysis is done, show `final_report(download=false)` instead |

## Behavioural rules

Abbreviated here; `SOURCE.md` has the full text.

**Asking the RCIC**

- Never end the turn to ask a question: use the runtime's question tool
  (`AskUserQuestion` in Claude Code and the Claude desktop app) with 2-4 short
  options, the recommendation first, and continue with the answer. Batch up
  to four questions in one call; put the facts needed to decide in the
  question.

**Always**

- Call `case_status` first and trust it over `.uapply/case.json` and over
  memory of earlier turns.
- If `agent_api` is false, say so, upload what is classified, and stop: the
  case runs in server mode.
- When a file's type is not obvious from its name, `preview_document` it
  before asking; never guess a type from a filename. Filled IMM forms go under
  the generic Agent Survey type; any other file with no matching type is asked
  about.
- Upload only files inside the working folder, through `sync_documents`.
- Loop `run_tasks` / `wait_for_stage` for processing and analysis, with one
  progress line after every call; name failed documents as they appear.
- Stop and report on `plan_limited` (never switch the case to server mode on
  its own) and on `runtime_error`.
- Never ask for, print or reason about a task's prompt or inputs.

**Messy client folders**

- If the folder holds several child folders or unrelated files, copy only the
  files the application type needs into a new subfolder of the folder root
  named after the type, `set_folder` to it and bind the case there. Files are
  copied, never moved or deleted; unrelated files are skipped without asking.

**Case creation**

- `create_case` charges the RCIC's account. Propose the name and type in a
  question whose first option is exactly "Create case", and pass
  `confirmation="create case"` only when the RCIC picked it or typed
  "create case" / 确认创建.

**Chat history**

- Fetch only for a contact the RCIC named; confirm the candidate from
  `chat_find_contact` first.
- Show the intake hints and the suggested type with its rationale; never print
  message bodies, quotes or chat ids.

**Never**

- Delete anything, contact a client, submit to a government portal, or retry a
  failing tool more than twice.

## Context hygiene

- `run_tasks` returns counts and a progress snapshot; the chat never receives
  task payloads.
- For long task queues, batch mode (`uapply-agent run --follow`) runs the same
  executor without a chat.
- The playbook contains no runtime-specific flags and no backend URLs; those
  live in the MCP server and `runners/`.

## Planned (not implemented)

- A resolution policy for DOUBTFUL / CONFLICT / MISSING values (prefer primary
  documents, verbatim quotes, propose rather than resolve legally significant
  fields) and `/uapply:review` / `/uapply:autofill` commands, once the review
  queue and approval tools exist.
