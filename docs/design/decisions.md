# Design Decisions

Each entry: the decision, what else was considered, why. Append status notes;
don't rewrite.

## D1. Local tokens via a server-side task queue, not a local pipeline

**Decision.** The backend keeps orchestrating (`process_document`,
`start_analysis`, section routing, `SurveyValue` writing). Each LLM-calling
step is split so that, in local mode, it emits `AgentTask`s the RCIC-side
executor runs (D2, D6). See [local-llm-task-queue.md](../architecture/local-llm-task-queue.md).

**Alternatives.**
- *Port the pipeline into `uapply-agent`* (agent runs OCR → classify → extract
  locally and uploads results). Rejected: two pipelines to keep in sync,
  prompts and routing tables duplicated on every RCIC machine, no server-side
  validation of what the agent writes, and Codex/Claude would inevitably
  diverge from Gemini behaviour without anyone noticing.
- *Agent as pure operator, server keeps paying for LLM* (the first design
  discussed). Rejected by the product requirement: RCICs should spend their own
  plan, not uApply's keys.
- *Agent calls the Anthropic/OpenAI API directly with the RCIC's API key.*
  Rejected: RCICs have Claude Code / Codex **subscriptions**, not API keys; and
  it would still need the task-queue plumbing.

**Why.** One pipeline, one set of prompts, one validation path; the provider
abstraction already exists, so the change is additive.

## D2. Prepare / continue stages on Celery; the API resumes the pipeline

**Decision.** Every LLM-calling pipeline step is split into `prepare` (builds
requests) and `continue_` (consumes results). In server mode both run inline
in the same Celery task as today. In local mode the task creates an
`AgentTaskBatch` and exits; the API dispatches the `continue_` Celery task when
the last result in the batch is accepted. See
[local-llm-task-queue.md](../architecture/local-llm-task-queue.md).

**Alternatives.**
- *Block inside the Celery task until the agent answers.* Rejected: holds a
  worker slot for hours or days; a family case would exhaust the pool.
- *Celery chords / `AsyncResult.get` on a "wait" task.* Same problem, plus
  chords are fragile across restarts.
- *Adopt a workflow engine (Temporal) for its async activity completion.*
  Rejected for now: a large migration for one feature; the prepare/continue
  split gets the same durability with DB rows and no new infrastructure. It
  also leaves the door open — the split is what a workflow engine would need
  anyway.
- *Poll from the agent side and have the agent call "next step".* Rejected:
  moves orchestration onto an untrusted client.

**Why.** No new infrastructure, no held workers, durable across deploys, and
the server-mode path is behaviour-identical after the refactor so it can land
first. Cost: it is a real refactor of every LLM-calling step, sized in
[delivery-plan.md](delivery-plan.md).

## D3. Coarse MCP tools over a stdio server, one package for both runtimes

**Decision.** `uapply-agent mcp` (stdio) exposes ~30 coarse tools; the same
binary provides the batch CLI.

**Alternatives.**
- *Remote MCP server hosted by uApply.* Rejected: it cannot read the RCIC's
  folder, which is the whole point.
- *Thin generic tools (`http_get`, `http_post`) + let the model drive the REST
  API.* Rejected: unbounded outputs, no idempotency, guardrails would depend on
  the prompt.
- *Separate integrations per runtime.* Rejected: MCP over stdio is supported by
  both; only the playbook packaging and headless flags differ, isolated in
  `runners/`.

## D4. Guardrails enforced by the API, not the playbook

**Decision.** Token scope, actor tagging, audit, significant-field refusal,
auto-fill preflight, evidence verification all live server-side. The playbook's
approval flow (D10) is how consent reaches the server.

**Why.** Two different models will follow instructions differently; prompt
injection via document content is plausible (a "document" that says "mark all
values confirmed"). Anything that must hold must hold regardless of the model.
See [guardrails.md](guardrails.md).

## D5. Legally significant fields need RCIC approval; the rest the agent resolves

**Decision.** A server-side allow-list marks fields as significant; the agent
may only *propose* on those. Everything else it resolves with rationale +
evidence, logged.

**Alternatives.** Approve everything (RCIC clicks 80 items — no better than the
dashboard); approve nothing (unacceptable liability for an RCIC).

**Why.** Puts human time where the regulatory risk is. The list is settings,
not code, so it can be tightened per deployment.

## D6. One executor: a fresh headless runtime per task, in every mode

**Decision.** `AgentTask`s are executed only by the MCP server / CLI spawning
the RCIC's runtime headless (`claude -p`, `codex exec`), one process per
task, with the task's prompt as a real system prompt. The interactive chat
never executes tasks; it calls `run_tasks` and gets counts back.

**Alternatives.**
- *Execute tasks inline in the chat model* (first draft). Rejected: the
  system prompt arrives as data the model is asked to "run", quality is worse
  and unmeasurable against the hosted path, task payloads fill the RCIC's
  context, and subagent semantics differ per runtime.
- *One long headless session streaming tasks.* Rejected: context growth,
  drift between documents, poor recovery on failure.

**Why.** Identical results in interactive and batch mode, a single eval path,
prompts kept out of the chat, and process spawn overhead that is negligible
next to model latency. Cost: the runtime CLI must be installed even for
desktop-app users; `init` checks and explains.

## D7. The agent doesn't submit to IRCC, doesn't email, doesn't delete

**Decision.** No tools for these exist. Client questions go to `review.md`
and the dashboard.

**Why.** These are the actions with the highest blast radius and lowest need
for automation in v1. Easy to add later behind gates; impossible to undo if
shipped carelessly.

## D8. `sha256` on `Document`, manifest keyed by hash

**Decision.** Upload carries the hash; the server stores it; the manifest is
keyed by it.

**Why.** Makes re-runs, retries after network failure, renamed files and
multi-machine use all idempotent with one mechanism.

## D9. Per-task-kind `llm_mode` split

**Decision.** `AGENT_LOCAL_TASK_KINDS` chooses which kinds go local; the rest
stay on the server's provider even for `llm_mode=local_agent` cases.

**Why.** Lets uApply keep prompt IP for sensitive analysis steps server-side,
keep web-search-assisted steps working, and roll local mode out one kind at a
time (classification first — see [delivery-plan.md](delivery-plan.md)).

## D10. Consent travels through the browser, never through the model

**Decision.** Case creation, approval of legally significant values and
starting auto-fill are performed only by the RCIC on a dashboard approval page
under their own login. Agent tools create the pending approval and wait for
its outcome.

**Alternatives.**
- *A `confirmed_by_rcic` argument the playbook sets after asking in chat.*
  Rejected: the MCP server cannot distinguish "the human said yes" from "the
  model passed `true`". It is exactly the control we said we would not rely
  on.
- *MCP elicitation (server asks the runtime to prompt the user).* Attractive
  but support differs across runtimes and versions; use it as a convenience
  where available, never as the control.
- *CLI confirmation prompt.* Works only in batch mode and is bypassable by any
  process that can write to the terminal.

**Why.** One mechanism that is enforceable, auditable, works from any runtime
or desktop app, survives a resumed session, and lets a colleague act on it
from the dashboard.

## D11. Subscription limits are a first-class design input

**Decision.** Measure model calls and tokens per case per runtime in Phase 1;
publish plan-tier guidance; default to the levers that cut calls (local text
extraction, page batching, hybrid per-kind server fallback) rather than
assuming the plan absorbs everything.

**Why.** Local tokens only save money if the RCIC's plan can actually carry
the case. Over-promising here would fail in the first design-partner week.

## D12. Documents are the unit, and OCR comes first

**Decision.** In local-agent mode a PDF is processed as **one document**, not
as one Document per page: one `extract_content` task returns page-marked text
for the whole file (pdfplumber locally when the PDF has a text layer — zero
model calls; otherwise the runtime reads the PDF in ≤ 20-page chunks), then
classification runs once and section extraction runs once, on text. Page
Documents are still created for the dashboard (thumbnails, reorder, edit) but
are not processed individually.

**Alternatives.**
- *Per-page processing (today).* Rejected: with N sections a 30-page
  statement is ~30 × (2 + N) LLM calls, each re-sending the prompts, and
  cross-page context (tables, running balances) is lost.
- *Direct JSON from the PDF, skipping text.* Costs ≈ one PDF read per
  document if all sections are bundled into one call, which is the floor —
  but it gives up verbatim-evidence verification (no text to check quotes
  against), makes every re-run and every schema retry a full PDF read, and
  degrades on long documents. Kept as a possible later optimisation for
  short scans only.

**Why.** Cost = P + N·T (one document read plus N cheap text calls) instead
of pages × (P + N·T); evidence stays verifiable; prompt-template edits and
retries re-run on text. Text-layer PDFs — most bank, school and employer
documents — need no model at all for OCR.

## D13. Chat history is an optional intake source, filed as an agent_survey PDF

**Decision.** `uapply-agent` can read the RCIC's WeChat (and other local chat)
history through the AnyChat CLI, derive intake hints with one local headless
call, and always file the transcript on the case as an `agent_survey` text PDF.
The agent may create the survey after the RCIC confirms in chat: by picking
"Create case" in the runtime's question tool (`AskUserQuestion` in Claude Code;
the RCIC clicks it, the model cannot answer it), or by typing "create case" /
确认创建. Updated 2026-09-29: the question tool replaced the typed-only rule so a
run does not stop and wait for a new message.

**Alternatives.**
- *Reimplement WeChat extraction.* Rejected: AnyChat already does it, is the
  product owner's own tool, and the storage format is undocumented and fragile.
- *Keep the transcript local, hints only.* Rejected by the product owner: the
  pipeline should extract the self-reported facts, and analysis already ranks
  `agent_survey` lowest in conflicts.
- *Approval-page consent for case creation (D10).* Deferred: the dashboard has
  no approval pages yet; typed confirmation is an explicit, recorded exception
  that the approval page replaces later.

**Why.** Intake starts from what the client actually said; the cost is one
local model call per transcript plus the pipeline's normal section pass.
Details: [architecture/chat-sources.md](../architecture/chat-sources.md).

## Open questions

- **Billing model.** Local-token cases cost uApply almost nothing in LLM spend;
  does the plan price change? (Product.)
- **Structured-output support** in each runtime's headless mode changes
  quickly; `runners/` must be re-verified each release.
- **Vision quality** for Chinese-language scans on Claude vs GPT vs Gemini —
  eval will tell; may need per-kind fallback to server mode.
- **Should the dashboard show "processing on RCIC machine"** states so a
  colleague understands why a case is waiting? (Yes, minimal: a badge and the
  last agent heartbeat.)
