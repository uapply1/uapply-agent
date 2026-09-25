# Design Decisions

Each entry: the decision, what else was considered, why. Append status notes;
don't rewrite.

## D1. Local tokens via a server-side task queue, not a local pipeline

**Decision.** The backend keeps orchestrating (`process_document`,
`start_analysis`, section routing, `SurveyValue` writing). A `LocalAgentLLM`
provider turns each LLM call into an `AgentTask` that the RCIC-side agent
executes. See [local-llm-task-queue.md](../architecture/local-llm-task-queue.md).

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

## D2. Temporal async activity completion for waiting

**Decision.** `LocalAgentLLM` raises `complete_async`; `submit_result`
completes the activity by task token.

**Alternatives.** Polling inside the activity (burns worker slots for hours);
splitting every workflow into "before LLM" / "after LLM" halves with a signal
(large refactor of every pipeline step); Celery chords (the Celery path is
being removed).

**Why.** Zero pipeline refactor, durable multi-day waits, cancellation and
timeouts for free. This is the single strongest reason to finish the Temporal
migration before building the agent.

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
`confirmed_by_rcic` flag is a second line only.

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

## D6. Fresh context per task in batch mode

**Decision.** Headless runs spawn the runtime once per `AgentTask`.

**Alternatives.** One long session streaming tasks. Rejected: context growth,
drift between documents, cost of re-reading, and poor recovery on failure.

**Why.** Each task is self-contained by construction (prompt + inputs +
schema). Process spawn overhead is negligible next to model latency.

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
