# Runtime Modes

`uapply-agent` has **one task executor** and two ways to drive it. Every model
call runs on the RCIC's Claude Code or Codex plan.

## One executor: a fresh headless runtime per task

Agent tasks are never executed inside the RCIC's chat. The MCP server (or the
CLI) executes them itself:

```
run_tasks()  /  uapply-agent run
  └─ for each AgentTask (N workers):
       1. pull task from the server; download its input documents into .uapply/cache/<task id>/;
          render pages if needed (text-layer PDFs are extracted with pdfplumber, no model)
       2. spawn the runtime headless with the task's prompt as a real system prompt
            Claude Code:  claude -p --system-prompt=… --json-schema … --output-format json
                          (only the Read tool; no MCP servers, plugins or settings)
            Codex:        codex exec --output-schema … (experimental)
          long prompts and document text are written to files the model reads
       3. check the JSON locally against output_schema; submit; retry with the rejection reason
  └─ return counts: accepted / rejected / released / failed, remaining, plan_limited, runtime_error
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
the executor releases its leases, reports `plan_limited` and stops; the case
resumes on the next `run_tasks` / `run`. When the runtime cannot work at all
(e.g. the Claude Code CLI is not signed in) it reports `runtime_error` and
stops the same way.

The MCP server finds the runtime's CLI by the absolute path `setup` recorded,
then `PATH`, then the installers' usual locations (desktop apps start the
server with a minimal `PATH`). `whoami` reports which runtimes were detected,
whether they start and whether Claude Code is signed in.

## Interactive — the RCIC orchestrates from a chat

```
$ cd ~/Clients/Zhang_Wei
$ claude            # or: codex, or the desktop apps
> /uapply:run
```

- `uapply-agent` runs as a **stdio MCP server** launched by the runtime.
- The chat model does the *orchestration*: proposing the case, picking
  document types from page previews, uploading, calling `start_processing`,
  `run_tasks` and `wait_for_stage`, finishing the case and showing the final
  report. It never sees a task payload.
- The RCIC answers questions in the runtime's question tool, sees one
  progress line per call, and can interject.
- Works from Claude Code, Codex CLI, Claude Desktop and the Codex desktop app
  alike, because the MCP server and its prompts are runtime-agnostic (see
  [playbook.md](../reference/playbook.md)). For most RCICs the desktop apps
  are the realistic surface; the CLIs are for power users.

Context stays small by construction: `run_tasks` returns counts and a
progress snapshot, and page previews are rendered locally only when a file's
type is not obvious from its name.

## Batch — the CLI orchestrates, no chat

```
$ cd ~/Clients/Zhang_Wei
$ uapply-agent run --follow --workers 3        # execute tasks until processing is done
$ uapply-agent run --folder ~/Clients/Li_Na    # one pass over the queued tasks of another folder
```

- The CLI runs the same executor over the bound folder's queued tasks. No chat
  model is involved except inside each spawned task.
- It does not upload or start stages: the folder must already be bound
  (`uapply-agent init --survey <id>`) and processing or analysis started, e.g.
  from a chat session or the dashboard. `--follow` keeps going until
  processing is done; `--kinds` and `--max-tasks` narrow a run.
- It exits with code 4 at the plan limit and 5 when the runtime cannot work,
  so a script can retry later.
- Best for: long task queues on large cases and overnight runs.

## Choosing at runtime

| Situation | Mode |
|---|---|
| New case, intake decisions, uploads | Interactive |
| Processing and analysis tasks | Either: same executor; interactive also starts the stages and shows progress |
| Overnight, long task queue | Batch, `uapply-agent run --follow` |
| Finishing (archives, forms, report) | Interactive |
| Plan limit reached mid-run | Either: executor releases leases; running again later resumes |

## Subscription usage limits — the real constraint

Both runtimes are sold on flat plans with rolling usage windows, not on
tokens. That is the whole point of local tokens, and also the main risk: a
case that needs more model calls than the window allows stalls until the
window resets.

Order-of-magnitude call counts for a typical case (estimates, to be replaced
with measured numbers):

| Case | Docs | Pages | OCR calls (scans only) | Classify | Section extraction | Analysis | Total model calls |
|---|---|---|---|---|---|---|---|
| Single applicant, study permit | 12 | ~40 | ~20 | 12 | ~60 | ~10 | ~100 |
| Family, PR | 45 | ~180 | ~90 | 45 | ~250 | ~20 | ~400 |

Many of these are vision calls on page images, which are the most expensive
kind. Levers, in order of impact:

1. **Skip the model where text exists.** Text-layer PDFs (most bank
   statements, letters, transcripts) go through pdfplumber locally — no model
   call. This alone can halve the count.
2. **Batch pages.** One `extract_content` task covers the whole document;
   the executor sends several page images per model call.
3. **Server mode per case.** A case that the plan cannot carry can run in
   server mode instead (`set_llm_mode`, only when the RCIC asks).
4. **Batch mode overnight** spreads a large case across usage windows: the
   executor stops when the runtime reports a limit, and running it again
   after the window resets carries on.

Guidance to write into onboarding once measured: which plan tier handles
which case sizes, and when to use batch or server mode. Until then, assume
entry-level plans are fine for single-applicant cases in batch mode and not
for family cases in one sitting.

Terms of use: the RCIC drives their own Claude Code / Codex, and the executor
spawns that same CLI, logged in as them, on their machine. We never extract or
reuse their subscription credentials in another harness — that is what would
breach provider terms.

## Runtime differences that matter

| | Claude Code | Codex |
|---|---|---|
| Support | supported | experimental |
| Playbook delivery | MCP prompts + server instructions; a Claude Code plugin generated by `setup` adds `/uapply:*` | MCP prompts + server instructions |
| MCP config | `claude mcp add --scope user`, or `~/.claude.json` when only the desktop app is installed | `codex mcp add`, or `[mcp_servers.uapply]` in `~/.codex/config.toml` |
| Headless (executor) | `claude -p` with `--system-prompt` and `--json-schema` | `codex exec --output-schema`; the system prompt is prepended to the prompt |
| Vision inputs (executor) | image file paths the model reads with its Read tool | image attachments |
| Usage-limit signal | parsed by `runners/claude_code.py` | parsed by `runners/codex.py` |

Flags change often; the `runners/` module isolates them. Nothing
runtime-specific lives in the playbook.

## Where the forms are filled

`autofill_forms` fills the IMM PDFs with Adobe Acrobat Pro on the RCIC's
Windows PC when it is available (copies in `.uapply/output/imm_pdfs/`), and
otherwise hands them to uApply's platform filler. `final_report` saves the
final package into `uApply output/` in the client folder.
