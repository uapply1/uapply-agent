# uApply Agent — Design Documentation

`uapply-agent` lets an RCIC run a uApply case from **Claude Code or Codex**,
pointed at a client's working folder on their own machine. The agent creates
the case, uploads and classifies the documents, extracts and reconciles the
data, and prepares auto-fill — while every LLM call runs on the **RCIC's own
Claude Code / Codex plan ("local tokens")**, not on uApply's API keys.

The uApply backend keeps the pipeline logic, prompts, validation and the data
model. It hands each model call to the local agent as a task and validates
what comes back.

Status: **proposed** (2026-09). Nothing in this folder is built yet; endpoint
names refer to the current `uapply-backend` unless marked *new*.

## Start here

| Document | Contents |
|---|---|
| [overview.md](overview.md) | Goals, non-goals, one-page architecture, how a case flows through the system |

## Architecture

| Document | Contents |
|---|---|
| [architecture/local-llm-task-queue.md](architecture/local-llm-task-queue.md) | The core mechanism: `LocalAgentLLM` provider, `AgentTask` queue, Temporal async completion, result validation |
| [architecture/case-workflow.md](architecture/case-workflow.md) | End-to-end stages — intake, upload, classify, extract, resolve, auto-fill — with the RCIC gates |
| [architecture/working-folder.md](architecture/working-folder.md) | Layout of the client folder, `.uapply/` manifest and case state, idempotent sync |
| [architecture/runtime-modes.md](architecture/runtime-modes.md) | Interactive (MCP inside Claude Code / Codex) vs batch (headless `claude -p` / `codex exec`) |

## Reference

| Document | Contents |
|---|---|
| [reference/mcp-tools.md](reference/mcp-tools.md) | MCP tool inventory exposed by `uapply-agent` to the coding agent |
| [reference/backend-api.md](reference/backend-api.md) | Backend endpoints the agent uses — existing ones, and the new ones this project adds |
| [reference/agent-task-schema.md](reference/agent-task-schema.md) | `AgentTask` kinds, payload and result JSON contracts |
| [reference/playbook.md](reference/playbook.md) | Claude Code plugin (skills, `/uapply:*` commands) and Codex `AGENTS.md` — the operator instructions |

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
│   ├── mcp_server.py        # stdio MCP server (tools in reference/mcp-tools.md)
│   ├── api/                 # typed client for the uApply backend
│   ├── folder/              # scan, hash, manifest, case.json
│   ├── local_ops/           # page rendering, pdfplumber/docx text, HEIC → JPEG (no LLM)
│   ├── tasks/               # AgentTask pull / execute / submit loop
│   └── runners/             # headless drivers: claude_code.py, codex.py
├── playbook/
│   ├── claude-code/         # plugin: skills + commands
│   └── codex/               # AGENTS.md + skills
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
