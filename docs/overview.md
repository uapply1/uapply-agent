# Overview

## Problem

Today an RCIC drives a uApply case through the web dashboard: create the
survey, upload each document, wait for processing, click through conflicts and
doubtful values, then start auto-fill. Every LLM call is billed to uApply's
Gemini / OpenAI keys.

Many RCICs already have a Claude Code or Codex subscription and keep each
client's documents in a folder on disk. The agent turns that folder into a
finished case, from the tool they already use, on the plan they already pay
for.

## Goals

1. **Folder in, case out.** `cd ~/Clients/Zhang_Wei && claude` (or `codex`),
   ask for `/uapply:run`, and the agent creates the case, uploads, classifies,
   extracts, reconciles and prepares auto-fill.
2. **Local tokens.** Every LLM call — vision OCR, classification, section
   extraction, analysis, conflict reasoning — runs on the RCIC's Claude Code or
   Codex plan. uApply's servers make no model calls for that case.
3. **Same results as the web app.** One pipeline, one set of prompts, one data
   model. The agent is an alternative *executor*, not an alternative product.
4. **Works in both Claude Code and Codex** from one codebase.
5. **RCIC stays in control.** Legally significant decisions and anything that
   leaves the machine are gated behind explicit approval.

## Non-goals

- Submitting anything to IRCC. Auto-fill produces filled IMM forms / portal
  data; the RCIC submits.
- Replacing the web dashboard. The dashboard remains the review surface; the
  agent writes to the same data.
- Running the backend locally. The agent talks to hosted uApply.
- Supporting arbitrary local models (Ollama etc.). Only the model inside the
  RCIC's Claude Code / Codex session is in scope.

## Architecture in one picture

```
RCIC machine                                                uApply cloud
┌─────────────────────────────────────────────┐            ┌─────────────────────────────────┐
│ Claude Code / Codex  (the LLM)              │            │ Django API + Celery workers      │
│   ├─ uApply playbook (skills / AGENTS.md)   │            │                                  │
│   └─ uapply-agent  (stdio MCP server + CLI) │            │  Survey · Document · SurveyValue │
│        ├─ folder scan / hash / manifest     │── HTTPS ──▶│  PromptTemplate · Section routing│
│        ├─ local ops: render pages, text     │            │  AgentTask queue (new)           │
│        ├─ task loop: pull → run → submit    │◀───────────│  LocalAgentLLM provider (new)    │
│        └─ device-code auth (keychain)       │            │  validation · audit · auto-fill  │
│                                             │            └─────────────────────────────────┘
│ ~/Clients/Zhang_Wei/                        │
│   passport.pdf  bank_2025.pdf  …            │            ┌─────────────────────────────────┐
│   .uapply/case.json  manifest.json          │            │ uapply-desktop (existing)        │
│   .uapply/review.md                         │            │ Selenium IMM-form filler         │
└─────────────────────────────────────────────┘            └─────────────────────────────────┘
```

Three parts:

| Part | Where | Responsibility |
|---|---|---|
| **Backend changes** | `uapply-backend` | `LocalAgentLLM` provider that turns each LLM call into an `AgentTask`; task pull/submit endpoints; result validation; review-queue and resolve endpoints with actor + audit; auto-fill preflight. See [local-llm-task-queue.md](architecture/local-llm-task-queue.md). |
| **`uapply-agent`** | new package, RCIC machine | CLI + stdio MCP server. Owns the working folder, does the non-LLM local work, pulls tasks and submits results, exposes coarse tools to the coding agent. See [mcp-tools.md](reference/mcp-tools.md). |
| **Playbook** | new, ships with the package | The operator instructions: when to call which tool, where the gates are, how to reason about conflicts. Delivered as a Claude Code plugin and a Codex `AGENTS.md`. See [playbook.md](reference/playbook.md). |

## How a case flows

```
RCIC: "/uapply:run"
  1. intake     scan folder → propose application type + family → 🧑 confirm → create Survey (+ dependents)
  2. upload     hash files → bulk_upload new ones → manifest
  3. classify   start_document_processing(llm_mode=local_agent)
                  server creates AgentTasks (content extraction, classification)
                  agent pulls, reads local pages, runs prompt, submits → server validates
                  low-confidence types → agent double-checks against the file → reclassify
  4. extract    start_analysis(force=true) → AgentTasks per section → SurveyValues (CONFIRMED/DOUBTFUL/CONFLICT/MISSING)
  5. resolve    review queue → agent resolves with evidence (tiered) → 🧑 approve legally significant ones
                  MISSING → client questions drafted in .uapply/review.md (never sent)
  6. auto-fill  preflight (no open CONFLICT) → 🧑 approve → start_auto_filling → desktop filler / L3 JSON
```

Detailed stage contracts: [case-workflow.md](architecture/case-workflow.md).

## Key properties

- **The server never waits on a worker slot for the agent.** A Celery stage
  creates the agent's tasks and exits; the API resumes the pipeline when the
  answers arrive. A case can pause for hours (plan limits, RCIC away) and
  resume where it left off.
- **Everything is idempotent.** Re-running `/uapply:run` on the same folder
  uploads nothing twice, re-creates nothing, and returns current state for
  already-resolved values.
- **Guardrails live in the API.** Token scope, actor tagging, audit log,
  auto-fill preflight, evidence verification — enforced server-side regardless
  of which coding agent is driving. See [guardrails.md](design/guardrails.md).
- **Tool results are small.** Evidence snippets and summaries, not raw OCR
  dumps; long jobs are long-polled server-side so the agent's context isn't
  spent on polling.

## Trade-offs accepted

Discussed fully in [decisions.md](design/decisions.md):

- Quality will differ between Gemini (server), Claude and GPT (local); an eval
  set per runtime is mandatory before release.
- Prompt templates become visible on the RCIC's machine; keep the most
  sensitive steps server-side if needed (`llm_mode` is per task kind).
- Local execution is more sequential than the server's parallel sections; batch
  mode with a few headless workers recovers most of it.
- Client documents are processed under the RCIC's own Anthropic / OpenAI
  account and terms — a feature for some firms, a compliance question for
  others. Surface it in onboarding.
