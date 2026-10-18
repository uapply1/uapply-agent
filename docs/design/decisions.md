# Design Decisions

Each entry: the decision, what else was considered, why. Where the
implementation differs from a decision, a **Status** note says so.

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

**Status.** Superseded by D17: the prepare/continue split became resumable
pipeline steps, local cases no longer resume through Celery tasks, and the
rejected "agent calls next step" alternative is revisited there.

## D3. Coarse MCP tools over a stdio server, one package for both runtimes

**Decision.** `uapply-agent mcp` (stdio) exposes coarse tools (24 today, see
[mcp-tools.md](../reference/mcp-tools.md)); the same binary provides the batch
CLI.

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

**Status.** Result validation and the LLM token log are in place. Token scope,
actor tagging, the audit log, the significant-field refusal and the auto-fill
preflight are planned; case creation uses the confirmation phrase (D13).

## D5. Legally significant fields need RCIC approval; the rest the agent resolves

**Decision.** A server-side allow-list marks fields as significant; the agent
may only *propose* on those. Everything else it resolves with rationale +
evidence, logged.

**Alternatives.** Approve everything (RCIC clicks 80 items — no better than the
dashboard); approve nothing (unacceptable liability for an RCIC).

**Why.** Puts human time where the regulatory risk is. The list is settings,
not code, so it can be tightened per deployment.

**Status.** Not implemented. The agent has no value-resolution tools yet; the
RCIC reviews values in the dashboard's AI Check.

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
desktop-app users; the installer installs it, and `whoami` and `init` report
which runtimes were found.

## D7. The agent doesn't submit to IRCC, doesn't email, doesn't delete

**Decision.** No tools for these exist. The agent reports what is waiting on
the RCIC in the conversation and in the final report; the RCIC contacts the
client and submits.

**Why.** These are the actions with the highest blast radius and lowest need
for automation in v1. Easy to add later behind gates; impossible to undo if
shipped carelessly.

## D8. `sha256` on `Document`, manifest keyed by hash

**Decision.** Upload carries the hash; the server stores it; the manifest is
keyed by it.

**Why.** Makes re-runs, retries after network failure, renamed files and
multi-machine use all idempotent with one mechanism.

**Status.** The agent's manifest is keyed by sha256. The backend does not store
the hash yet; instead `sync_documents` records a case document with the same
type, name and size rather than uploading the file again.

## D9. Per-task-kind `llm_mode` split

**Decision.** `AGENT_LOCAL_TASK_KINDS` chooses which kinds go local; the rest
stay on the server's provider even for `llm_mode=local_agent` cases.

**Why.** Lets uApply keep prompt IP for sensitive analysis steps server-side,
keep web-search-assisted steps working, and roll local mode out one kind at a
time (classification first, see [delivery-plan.md](delivery-plan.md)).

**Status.** Not implemented. `AGENT_LOCAL_TASK_KINDS` is gone (D17): every
model call of a local case runs on the agent, so no kind stays on the server
provider. A case whose prompts must stay on uApply runs in server mode.

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

**Status.** Not implemented; the dashboard has no approval pages. Case creation
uses a confirmation phrase instead (D13), and archives and auto-fill start once
the analysis is done.

## D11. Subscription limits are a first-class design input

**Decision.** Measure model calls and tokens per case per runtime; publish
plan-tier guidance; default to the levers that cut calls (local text
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
call, and by default file the transcript on the case as an `agent_survey` text PDF.
The agent may create the survey after the RCIC confirms in chat: by picking
"Create case" in the runtime's question tool (`AskUserQuestion` in Claude Code;
the RCIC clicks it, the model cannot answer it), or by typing "create case" /
确认创建. The question tool lets the run continue with the answer instead of
stopping to wait for a new message.

**Alternatives.**
- *Reimplement WeChat extraction.* Rejected: AnyChat already does it, and the
  storage format is undocumented and fragile. AnyChat is a separate product
  with its own installation and login; the agent only runs its CLI.
- *Keep the transcript local, hints only.* Not the default: the pipeline
  should extract the self-reported facts, and analysis already ranks
  `agent_survey` lowest in conflicts. It remains available as a setting
  (`chat_upload=false`).
- *Approval-page consent for case creation (D10).* Deferred: the dashboard has
  no approval pages yet; the confirmation phrase is an explicit exception
  that the approval page replaces later.

**Why.** Intake starts from what the client actually said; the cost is one
local model call per transcript plus the pipeline's normal section pass.
Details: [architecture/chat-sources.md](../architecture/chat-sources.md).

## D14. No server-side AI for local-agent cases; every model call is intercepted at the client

**Decision.** For a `local_agent` case nothing starts automatically on uApply
(not on upload, not after processing) and no AI runs on uApply's servers. OCR,
classification, section extraction, Financial Proof extraction, DOCX/XLSX
transcription, analysis, reformat/translate, drafts and dashboard field saves
all run on the RCIC's plan.

**How.** Instead of turning each of the ~34 call sites into a prepare/continue
stage (D2), the public methods of the two LLM clients (`GeminiLLM`,
`OpenaiLLM`) are wrapped (`ai_parse/agent/llm_proxy.py`). Inside an agent
context, entered by every Celery task of a local case and carried into thread
pools and the shared async runner, a call writes an `AgentTask` (`llm_call`:
system prompt, user prompt, JSON schema or text mode, image/HTML inputs) and
waits for the agent's answer, which it returns in the shape the caller already
reads. LlamaParse is refused.

**Where it waits.** Local-case jobs run on a dedicated `agent_queue` worker: a
thread pool, so a waiting job costs a thread rather than a prefork slot other
customers need. The worker runs with server-side model calls switched off, so
any model call without an agent context raises instead of reaching a provider
(fail closed). This relaxes D2's "no blocking waits" for that worker only; OCR
and classification keep their prepare/continue path.

**Alternatives.** Per-call-site hand-offs: no new worker, but analysis
(threads, tiers, nested reformat calls) would need restructuring, and any
missed path would silently use a server-side provider.

**Details.** A dependant counts as local when its principal is
(dashboard-created dependants default to server mode), and a pull also serves
dependants added after the folder was bound. Stored images are deleted and
prompts/answers cleared as soon as a call ends, with a periodic sweep as a
safety net. Once no agent picks up a call, the rest of the job fails at once
instead of waiting again through each provider fallback; a repeated identical
request is sent as a retry with the reason. Files of never-processed types
(photos) do not block progress or analysis.

**Consequences.** A case only progresses while the agent runs (`run_tasks`); a
call nobody picks up fails after 10 min (`AGENT_IDLE_TIMEOUT_S`). Dashboard
saves on a local case need an active agent (409 `AGENT_REQUIRED` otherwise).
The backend needs the `agent_queue` worker for local cases to progress.

**Status.** Superseded by D17 for the mechanism: the blocking proxy, the
`agent_queue` worker and 409 `AGENT_REQUIRED` are gone. The goal (no
server-side AI for the work the agent runs) stands; dashboard actions on a
local case now run on the server.

## D15. In Claude Code, tasks run as subagents of the RCIC's own session

**Decision.** When the MCP client is Claude Code (`task_runner` `auto` →
`session`), `run_tasks` does not spawn a headless `claude -p`. It prepares one
brief per task (`.uapply/cache/<task id>/task.md`: instructions, task, schema,
files, how to submit) and the conversation spawns a `uapply:task-runner`
subagent per brief. The agent ships with the plugin `setup` writes; its tools
are Read, `submit_task` and `release_task`, it inherits the session's model
and it runs in the background with a fresh context. Codex and `uapply-agent
run` keep the headless runner.

**Why.** The Claude desktop app and the `claude` CLI keep separate logins
(anthropics/claude-code#62206), so the headless runner made an RCIC sign in to
Claude twice and the installer download the 250 MB CLI. Claude Code has no
MCP sampling, so the server cannot ask the host to run a model call; a
subagent is the one thing that runs on the session's login and plan with an
isolated context. Codex's app and CLI already share one login.

**Alternatives.** *Run the task in the conversation itself*: rejected, the
prompt would be data instead of a system prompt and the conversation would
fill with document text. *MCP sampling*: not supported by Claude Code.

**Consequences.** The conversation orchestrates one more step (spawn, wait,
call again) and must never read a brief. A subagent's requests count toward the
same usage limits as the conversation. `submit_task` repeats the local schema
check and relays uApply's rejection as feedback for one more attempt. The
Claude Code CLI is optional (`UAPPLY_INSTALL_CLAUDE_CLI=1`).

## D16. The Claude Code CLI is never used

**Decision.** In Claude Code, tasks always run as `uapply:task-runner`
subagents, even with `task_runner=cli`. uapply-agent never installs, signs in
to or runs `claude`: no headless `claude -p` runner, no `claude mcp add` (setup
writes `~/.claude.json` itself), no `claude auth login`. The headless runner,
for `uapply-agent run` and Codex sessions, is Codex only. Supersedes the
optional CLI of D15.

**Why.** The CLI needed a second Claude sign-in and a ~250 MB download, and
RCICs were asked to install it although their Claude Code session already runs
the tasks.

## D17. Agent-driven resumable steps; no agent_queue

**Decision.** Every model-calling stage of document processing and analysis is
a resumable pipeline step (`ai_parse/service/pipeline/`): `requests(state)`
builds the model requests, `apply(state, answers)` consumes the answers and
names the next steps. Server mode answers the requests in-process inside the
existing Celery tasks (`InlineExecutor`). For a local-agent case a step with
requests is stored as an `AgentTaskBatch` and stops (`AgentExecutor`); when the
agent's submit settles the batch, the agent API applies it in that same request
and starts the next steps. Each `pull` first reclaims dead leases, expires
batches past their TTL and applies settled batches nobody applied. See
[local-llm-task-queue.md](../architecture/local-llm-task-queue.md).
Supersedes D14's blocking proxy and `agent_queue` worker, and D2's Celery
continuations for local cases.

**What went.** The `llm_proxy` (calls polling for the agent's answer, the agent
context, `web_context` and 409 `AGENT_REQUIRED`, the idle and call timeouts),
the `agent_queue` worker with `UAPPLY_SERVER_LLM=off`, the continuation Celery
tasks and the `expire_agent_tasks` beat task, and `AGENT_LOCAL_TASK_KINDS`.
Local cases use no Celery at all.

**Why.** The proxy kept a thread per waiting call on a dedicated worker for as
long as the RCIC took to answer, needed a second worker type in every
deployment, and failed calls after 10 idle minutes, so a closed laptop or a
plan limit turned into failed documents. Steps hold nothing while waiting,
survive deploys as rows, and let a case resume whenever the agent comes back.
One step definition serves both modes, so the server path cannot drift from
the local one.

**Revisits D2.** D2 rejected "the agent calls the next step" because it moves
orchestration onto an untrusted client. Here it does not: the server still
decides every step from its own stored state and routing, builds every prompt,
and validates every answer before a step sees it (schema, allowed values, page
coverage, and a step's own check such as the analysis normalisation). The
agent's requests only *trigger* the server's next move; the agent cannot pick
a step, skip one or change its inputs.

**Fail closed.** While a local step runs (`local_mode.local_step()`, a
contextvar), the provider clients' public methods and LlamaParse raise
`ServerLLMDisabled`. Dashboard actions on a local case run on the server as for
any case (`RunOrigin.SERVER`), and Celery tasks always run with that origin.

**Consequences.** A case only moves while its agent pulls and submits; nothing
fails for being idle except a batch older than `AGENT_BATCH_TTL_DAYS` (7).
Local analysis starts every section at once, except those reading another
section's values, instead of the runner's tiers. Generic calls keep the kind
`llm_call`, so the agent needed no change for them.

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
