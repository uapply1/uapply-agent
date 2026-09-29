# uApply Agent — Design Documentation

`uapply-agent` lets an RCIC run a uApply case from **Claude Code or Codex**,
pointed at a client's working folder on their own machine. The agent creates
the case, uploads and classifies the documents, extracts and reconciles the
data, and prepares auto-fill — while every LLM call runs on the **RCIC's own
Claude Code / Codex plan ("local tokens")**, not on uApply's API keys.

The uApply backend keeps the pipeline logic, prompts, validation and the data
model. It hands each model call to the local agent as a task and validates
what comes back.

Status (2026-09-25): **Phase 1 vertical slice implemented** — backend task
queue (`ai_parse/agent/`, migrations `survey.0058` / `ai_parse.0008`) and the
`uapply-agent` package (`src/uapply_agent/`). The stage wired to local tokens
is the passport/visa/permit **sub-type classifier**
(`DocumentProcessor._classify_document_subtype`), which is the only
classification LLM call the pipeline has today — documents are uploaded with
a document type already chosen. Agent endpoints live under
`/api/ai-parse/agent/` (the docs' `/api/agent/` prefix reads as that).
Everything from Phase 2 on is still a plan.

## Start here

| Document | Contents |
|---|---|
| [overview.md](overview.md) | Before/after poster, goals, non-goals, one-page architecture, who thinks and who works, platform mode vs agent mode (diagrams), how a case flows through the system |

## Architecture

| Document | Contents |
|---|---|
| [architecture/local-llm-task-queue.md](architecture/local-llm-task-queue.md) | The core mechanism: prepare/continue stages on Celery, `AgentTask` queue, API-driven continuation, result validation |
| [architecture/case-workflow.md](architecture/case-workflow.md) | End-to-end stages — intake, upload, classify, extract, resolve, auto-fill — with the RCIC gates |
| [architecture/working-folder.md](architecture/working-folder.md) | Layout of the client folder, `.uapply/` manifest and case state, idempotent sync |
| [architecture/chat-sources.md](architecture/chat-sources.md) | Optional WeChat intake through the AnyChat CLI: fetch, local intake hints, agent_survey upload, create_case |
| [architecture/runtime-modes.md](architecture/runtime-modes.md) | One headless executor per task; interactive (chat orchestrates) vs batch (CLI orchestrates); subscription usage limits |

## Reference

| Document | Contents |
|---|---|
| [reference/mcp-tools.md](reference/mcp-tools.md) | MCP tool inventory exposed by `uapply-agent` to the coding agent |
| [reference/backend-api.md](reference/backend-api.md) | Backend endpoints the agent uses — existing ones, and the new ones this project adds |
| [reference/agent-task-schema.md](reference/agent-task-schema.md) | `AgentTask` kinds, payload and result JSON contracts |
| [reference/playbook.md](reference/playbook.md) | The operator instructions, shipped as MCP prompts + server instructions (`/uapply:*` on every runtime); optional plugin / `AGENTS.md` wrappers |

## Design

| Document | Contents |
|---|---|
| [design/decisions.md](design/decisions.md) | Decisions and alternatives considered (why local tokens, why a task queue, why MCP + stdio, why not a fork of the pipeline) |
| [design/guardrails.md](design/guardrails.md) | Security, privacy, prompt-IP, and behavioural guardrails — enforced by the API, not by prompts |
| [design/delivery-plan.md](design/delivery-plan.md) | Phased build order, first vertical slice (classification), evaluation plan |
| [design/known-issues.md](design/known-issues.md) | Backend behaviours an agent will trip over that must be fixed first |

## Repository orientation (planned)

```
uapply-agent/
├── docs/                    # this documentation
├── src/uapply_agent/
│   ├── cli.py               # `uapply-agent login | init | run | status`
│   ├── mcp_server.py        # stdio MCP server: tools, prompts, instructions
│   ├── api/                 # typed client for the uApply backend
│   ├── folder/              # scan, hash, manifest, case.json
│   ├── local_ops/           # page rendering, pdfplumber/docx text, HEIC → JPEG (no LLM)
│   ├── executor/            # pull task → spawn headless runtime → validate → submit
│   └── runners/             # per-runtime spawn/flags/limit detection: claude_code.py, codex.py
├── playbook/
│   ├── SOURCE.md            # instructions, prompts, resolution policy (single source)
│   └── wrappers/            # optional plugin / AGENTS.md, generated
└── evals/                   # golden client folders + scoring
```

## Related documentation

- Backend: `uapply-backend/docs/` — [architecture overview](../../uapply-backend/docs/architecture/overview.md),
  [document pipeline](../../uapply-backend/docs/architecture/document-pipeline.md),
  [AI analysis](../../uapply-backend/docs/architecture/ai-analysis.md).
- Desktop auto-fill app: `uapply-desktop/` (Selenium IMM-form filler driven by L3 JSON).

## Contributing to these docs

- `architecture/` and `reference/` describe the intended build; once code
  lands, update them in the same change and drop the *(new)* markers.
- `design/` records *why*; append status notes rather than rewriting decisions.
