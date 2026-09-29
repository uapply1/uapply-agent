# uApply Agent — Design Documentation

`uapply-agent` lets an RCIC run a uApply case from **Claude Code or Codex**,
pointed at a client's working folder on their own machine. The agent binds or
creates the case, uploads the documents, drives the pipeline's processing and
analysis, and finishes the case with archives, IMM form auto-fill and a
report, while every LLM call of the case runs on the **RCIC's own Claude Code /
Codex plan ("local tokens")**, not on uApply's API keys.

The uApply backend keeps the pipeline logic, prompts, validation and the data
model. It hands each model call to the local agent as a task and validates
what comes back.

Status: the agent runs a case end to end. It covers intake (binding a folder
to an existing case, or creating one after the RCIC confirms, optionally from
the client's WeChat history), upload, local processing and analysis (every
model call of a `local_agent` case runs in a headless Claude Code or Codex
process on the RCIC's plan; Codex support is experimental), archives and
compression on uApply, IMM PDF auto-fill (with Adobe Acrobat Pro on the RCIC's
Windows PC, otherwise uApply's platform filler), and an end-of-run report. The
review queue, value resolution by the agent and dashboard approval pages are
planned; documents that describe them say so.

## Start here

| Document | Contents |
|---|---|
| [overview.md](overview.md) | Before/after illustration, goals, non-goals, one-page architecture, who thinks and who works, platform mode vs agent mode (diagrams), how a case flows through the system |

## Architecture

| Document | Contents |
|---|---|
| [architecture/local-llm-task-queue.md](architecture/local-llm-task-queue.md) | The core mechanism: prepare/continue stages on Celery, `AgentTask` queue, API-driven continuation, result validation |
| [architecture/case-workflow.md](architecture/case-workflow.md) | End-to-end stages (intake, upload, processing, analysis, finishing) with the points where the RCIC is asked |
| [architecture/working-folder.md](architecture/working-folder.md) | Layout of the client folder, `.uapply/` manifest and case state, idempotent sync |
| [architecture/chat-sources.md](architecture/chat-sources.md) | Optional WeChat intake through the AnyChat CLI: fetch, local intake hints, agent_survey upload, create_case |
| [architecture/runtime-modes.md](architecture/runtime-modes.md) | Two task runners (session subagents in Claude Code, headless CLI otherwise) over one preparation path; interactive vs batch; subscription usage limits |

## Reference

| Document | Contents |
|---|---|
| [reference/mcp-tools.md](reference/mcp-tools.md) | MCP tool inventory exposed by `uapply-agent` to the coding agent |
| [reference/backend-api.md](reference/backend-api.md) | Backend endpoints the agent uses, and the ones planned for later stages |
| [reference/agent-task-schema.md](reference/agent-task-schema.md) | `AgentTask` kinds, payload and result JSON contracts |
| [reference/playbook.md](reference/playbook.md) | The operator instructions, shipped as MCP prompts + server instructions (`/uapply:*` on every runtime) and a generated Claude Code plugin |

## Design

| Document | Contents |
|---|---|
| [design/decisions.md](design/decisions.md) | Decisions and alternatives considered (why local tokens, why a task queue, why MCP + stdio, why not a fork of the pipeline) |
| [design/guardrails.md](design/guardrails.md) | Security, privacy, prompt-IP, and behavioural guardrails — enforced by the API, not by prompts |
| [design/delivery-plan.md](design/delivery-plan.md) | Build order with status, evaluation plan, rollout |

## Repository layout

```
uapply-agent/
├── README.md                # install, settings, CLI, troubleshooting
├── install.sh / install.ps1 # one-command installers (macOS / Linux, Windows)
├── pyproject.toml
├── docs/                    # this documentation
├── tests/
└── src/uapply_agent/
    ├── cli.py               # `uapply-agent` command: update, setup, login, logout, init, status, run, mcp, chat, clean, acrobat, config
    ├── mcp_server.py        # stdio MCP server: the tools, the /uapply:* prompts, server instructions
    ├── SOURCE.md            # the playbook: server instructions and prompts (single source)
    ├── playbook.py          # reads SOURCE.md; generates the Claude Code plugin commands
    ├── context.py           # ServerContext (settings, folder, API client) and ToolError
    ├── cases.py             # bind a folder to a case, create cases, read progress
    ├── uploads.py           # upload folder files without duplicating case documents
    ├── folder.py            # working folder: scan, sha256, manifest.json, case.json under .uapply/
    ├── briefs.py            # task preparation shared by both runners; briefs for subagents
    ├── session_runner.py    # tasks as subagents of the Claude Code session
    ├── executor.py          # headless runner: pull task, prepare, spawn runtime, validate, submit
    ├── runners/             # headless runtime drivers: claude_code.py (supported), codex.py (experimental)
    ├── local_ops.py         # page rendering, PDF text layer, HEIC to JPEG (no model)
    ├── autofill.py          # IMM PDF auto-fill: local Acrobat Pro, or uApply's platform filler
    ├── acrobat.py           # Adobe Acrobat Pro automation on Windows (IAC through pywin32)
    ├── report.py            # end-of-run report and final package download
    ├── api.py               # typed client for the uApply backend
    ├── auth.py              # Auth0 device login, or a pasted token
    ├── config.py            # settings (config.json) and credential storage
    ├── integrate.py         # MCP registration for Claude Code / Codex, used by `setup`
    ├── updater.py           # self-update at start: versions/<commit>/, hand-over to the newest
    ├── constants.py         # document statuses and supported file types
    ├── util.py              # small file helpers (atomic writes, hashing, JSON)
    └── chat/                # optional chat intake: AnyChat source, transcript store, PDF render, intake call, filing
```

## Related documentation

The backend's own documentation (document pipeline, AI analysis, API
inventory) lives in the uApply backend repository.

## Contributing to these docs

- `architecture/` and `reference/` describe what the code does; mark anything
  not built as planned, and update the documents in the same change as the
  code.
- `design/` records *why*; add status notes rather than rewriting decisions.
