# Runtime Modes

`uapply-agent` has **one task executor** and two ways to drive it. Every model
call runs on the RCIC's Claude Code or Codex plan.

## One executor: a fresh headless runtime per task

Agent tasks are never executed inside the RCIC's chat. The MCP server (or the
CLI) executes them itself:

```
run_tasks()  /  uapply-agent run
  └─ for each AgentTask (N workers):
       1. pull task from the server; resolve inputs to local files; render pages if needed
       2. spawn the runtime headless with the task's prompt as a real system prompt
            Claude Code:  claude -p --system-prompt <file> --output-format json …
            Codex:        codex exec --output-schema schema.json …
          images attached; text-layer PDFs pre-extracted and inlined
       3. validate the JSON locally against output_schema; submit; on rejection retry once
  └─ return counts: accepted / rejected / released, plus any plan-limit signal
```

Why this is the only executor:

- **The prompt is a system prompt, not data.** Inline execution in a chat
  hands the model a `system_prompt` string and asks it to "run" it; that is
  role-play and measurably worse than what the hosted model gets. Headless
  runs pass it as the actual system prompt.
- **Fresh context per task.** Every task sees the full prompt and only its
  own inputs; no drift from earlier documents, no context growth, nothing
  from the case leaks into the RCIC's chat.
- **One code path to evaluate.** Interactive and batch produce identical
  results because they run the same executor; the eval matrix collapses to
  runtime × case, not runtime × mode × case.
- **Same subscription, same machine.** The spawned runtime is the RCIC's own
  Claude Code / Codex CLI, logged in as them. Nothing is proxied.

`--workers N` (default 2, max 4) runs N runtimes concurrently. Plan rate
limits, not CPU, are the bottleneck. When the runtime reports a usage limit
the executor releases its leases, records `plan_limited_until` if the runtime
exposes it, and stops; the case resumes on the next `run_tasks` / `run`.

The MCP server needs the runtime's CLI on `PATH`. `whoami` reports which
runtimes were detected; `run_tasks` fails with a clear hint if none is.

## Interactive — the RCIC orchestrates from a chat

```
$ cd ~/Clients/Zhang_Wei
$ claude            # or: codex, or the desktop apps
> /uapply:run
```

- `uapply-agent` runs as a **stdio MCP server** launched by the runtime.
- The chat model does the *orchestration*: intake proposal, calling
  `run_tasks`, reviewing classifications, working the review queue, drafting
  client questions, presenting approval links. It never sees a task payload.
- The RCIC sees the summaries, opens approval pages, and can interject.
- Works from Claude Code, Codex CLI, Claude Desktop and the Codex desktop app
  alike, because the MCP server and its prompts are runtime-agnostic (see
  [playbook.md](../reference/playbook.md)). For most RCICs the desktop apps
  are the realistic surface; the CLIs are for power users.

Context stays small by construction: `run_tasks` returns counts, the review
queue returns snippets, and `get_document_text` is capped and used only when a
decision needs it.

## Batch — the CLI orchestrates, no chat

```
$ uapply-agent run ~/Clients/Zhang_Wei --stages classify,extract --workers 3
$ uapply-agent run ~/Clients/*/ --stages classify,extract        # several clients
```

- The CLI runs the stage sequence itself using the same executor. No chat
  model is involved except inside each spawned task.
- It stops at every 🧑 gate and prints the pending approval URLs
  (`uapply-agent status` shows them). The RCIC decides on the page, then
  re-runs `run`, which resumes.
- Best for: stages 3–4 on large cases, overnight runs, several clients
  back-to-back.

## Choosing at runtime

| Situation | Mode |
|---|---|
| New case, intake decisions | Interactive |
| Any case size, stages 3–4 | Either — same executor; interactive just shows progress |
| Overnight, many clients | Batch, `--stages classify,extract`, review next morning |
| Review of conflicts and doubts | Interactive |
| Plan limit reached mid-run | Either: executor releases leases; run again later resumes |

## Subscription usage limits — the real constraint

Both runtimes are sold on flat plans with rolling usage windows, not on
tokens. That is the whole point of local tokens, and also the main risk: a
case that needs more model calls than the window allows stalls until the
window resets.

Order-of-magnitude call counts for a typical case (to be **measured** in
Phase 1 and replaced with real numbers):

| Case | Docs | Pages | OCR calls (scans only) | Classify | Section extraction | Analysis | Total model calls |
|---|---|---|---|---|---|---|---|
| Single applicant, study permit | 12 | ~40 | ~20 | 12 | ~60 | ~10 | ~100 |
| Family, PR | 45 | ~180 | ~90 | 45 | ~250 | ~20 | ~400 |

Many of these are vision calls on page images, which are the most expensive
kind. Levers, in order of impact:

1. **Skip the model where text exists.** Text-layer PDFs (most bank
   statements, letters, transcripts) go through pdfplumber locally — no model
   call. This alone can halve the count.
2. **Batch pages.** 3–4 page images per `extract_content` task instead of one.
3. **Keep expensive steps server-side per case** (`AGENT_LOCAL_TASK_KINDS`),
   e.g. OCR on uApply's provider, everything else local — a hybrid that still
   removes most of the cost.
4. **Batch mode overnight** spreads a large case across windows
   automatically: the executor backs off when the runtime reports a limit and
   resumes when it clears.

Guidance to write into onboarding once measured: which plan tier handles
which case sizes, and when to use batch or hybrid mode. Until then, assume
entry-level plans are fine for single-applicant cases in batch mode and not
for family cases in one sitting.

Terms of use: the RCIC drives their own Claude Code / Codex, and the executor
spawns that same CLI, logged in as them, on their machine. We never extract or
reuse their subscription credentials in another harness — that is what would
breach provider terms.

## Runtime differences that matter

| | Claude Code | Codex |
|---|---|---|
| Playbook delivery | MCP prompts + server instructions (primary); optional plugin wrapper | MCP prompts + server instructions (primary); optional `AGENTS.md` wrapper |
| MCP config | `claude mcp add` / `.mcp.json` / Claude Desktop config | `~/.codex/config.toml` `[mcp_servers.uapply]` |
| Headless (executor) | `claude -p` with system-prompt and JSON output flags | `codex exec --output-schema` |
| Vision inputs (executor) | image file paths in the prompt | image attachments |
| Usage-limit signal | exit code / message parsed by `runners/claude_code.py` | parsed by `runners/codex.py` |

Flags change often; the `runners/` module isolates them and the eval suite
runs against both runtimes on every release. Nothing runtime-specific lives in
the playbook.

## What the desktop app does in each mode

Nothing changes for `uapply-desktop`: it still consumes the L3 JSON / IMM PDFs
for the survey. The agent's job ends at producing approved data and, in v1,
downloading those artefacts into `.uapply/output/`.
