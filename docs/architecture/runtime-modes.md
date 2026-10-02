# Runtime Modes

Every model call runs on the RCIC's Claude Code or Codex plan, and never inside
the conversation. Two task runners share one preparation path. In Claude Code
it is always `session`, whatever `task_runner` says: Claude Code never runs a
headless CLI (D16). Other clients use `task_runner` (`auto` | `session` |
`cli`), where `auto` means `cli`.

## Preparation, shared by both runners

For every pulled `AgentTask` the server downloads the input documents into
`.uapply/cache/<task id>/`, extracts PDF text layers with pdfplumber (such a
document is submitted at once, no model), renders the pages a model has to look
at (the first three for classification, all of them for OCR), and writes long
text inputs to files. A result is checked against the task's `output_schema`
locally before it goes to uApply, and uApply's rejection comes back as feedback
for one more attempt.

## Session runner — subagents of the RCIC's own session (Claude Code)

```
run_tasks()  → briefs                       the conversation then, in one message:
  └─ for each pulled task:                    Agent(subagent_type="uapply:task-runner",
       prepare (above); write task.md:              prompt="Task brief: <path>")   × N
       instructions, task, schema, files,   each subagent (fresh context, Read +
       how to submit                        submit_task/release_task only):
                                              reads the brief and its files,
                                              calls submit_task(task_id, result)
```

- The `uapply:task-runner` agent ships with the plugin `setup` writes; it has
  no `model` of its own, so it runs on the session's model, login and plan.
- One Claude sign-in: the desktop app and the CLI do not share logins, and
  Claude Code has no MCP sampling, so this is the only way a task can run on
  the session's credentials.
- The conversation never reads a brief or a document; it sees a one-line
  status per subagent and the `progress` snapshot.

## CLI runner — a fresh headless Codex per task (Codex, batch mode)

```
run_tasks()  /  uapply-agent run
  └─ for each AgentTask (N workers):
       1. prepare (above)
       2. spawn Codex headless (experimental), the system prompt prepended to the prompt:
            codex exec --output-schema …   (the schema in OpenAI's strict dialect)
       3. check the JSON locally; submit; retry once with the rejection reason
  └─ return counts: accepted / rejected / released / failed, remaining, plan_limited, runtime_error
```

Why the work never runs in the conversation itself:

- **The prompt is a system prompt, not data.** Inline execution hands the model
  a `system_prompt` string and asks it to "run" it; that is role-play and
  measurably worse than a real system prompt.
- **Fresh context per task.** Every task sees the full prompt and only its own
  inputs; no drift from earlier documents, no context growth, nothing from the
  case leaks into the RCIC's conversation.
- **Same subscription, same machine.** Subagent or headless process, it is the
  RCIC's own Claude Code / Codex, logged in as them. Nothing is proxied.

`--workers N` (default 2, max 4 headless processes; up to 8 briefs per round)
bounds the parallelism. Plan rate limits, not CPU, are the bottleneck. When a
headless runtime reports a usage limit the executor releases its leases,
reports `plan_limited` and stops; the case resumes on the next `run_tasks` /
`run`. When it cannot work at all (e.g. the Codex CLI is not signed in)
it reports `runtime_error` and stops the same way.

The MCP server finds a runtime's CLI by the absolute path `setup` recorded,
then `PATH`, then the installers' usual locations (desktop apps start the
server with a minimal `PATH`). `whoami` reports the task runner in use, which
CLIs were detected, whether they start and whether Claude Code is signed in.

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

- The CLI runs the headless runner over the bound folder's queued tasks. No
  chat model is involved except inside each spawned task. It needs the Codex
  CLI installed and signed in (`codex login`).
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
| Processing and analysis tasks | Either: same preparation and checks; interactive also starts the stages and shows progress |
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
| MCP config | user-scope server in `~/.claude.json`, written directly | `[mcp_servers.uapply]` in `~/.codex/config.toml`, tools pre-approved (`default_tools_approval_mode = "approve"`) |
| Headless (executor) | never: tasks run as session subagents | `codex exec --output-schema`; the system prompt is prepended to the prompt |
| Vision inputs | page images the subagent reads with its Read tool | image attachments |
| Usage-limit signal | the session's own | parsed by `runners/codex.py` |

Flags change often; the `runners/` module isolates them. Nothing
runtime-specific lives in the playbook.

## Where the forms are filled

`autofill_forms` fills the IMM PDFs with Adobe Acrobat Pro on the RCIC's
Windows PC when it is available (copies in `.uapply/output/imm_pdfs/`), and
otherwise hands them to uApply's platform filler. `final_report` saves the
final package into `uApply output/` in the client folder.
