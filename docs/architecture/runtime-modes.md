# Runtime Modes

The same `uapply-agent` package serves two ways of running a case. Both use the
RCIC's Claude Code or Codex plan for every model call.

## Interactive — the RCIC works inside the coding agent

```
$ cd ~/Clients/Zhang_Wei
$ claude            # or: codex
> /uapply:run
```

- `uapply-agent` runs as a **stdio MCP server** launched by Claude Code / Codex
  (configured once: `claude mcp add uapply -- uapply-agent mcp`, or the
  equivalent entry in Codex's `config.toml`).
- The coding agent's model is the LLM. It calls the tools in
  [mcp-tools.md](../reference/mcp-tools.md), including `pull_tasks` /
  `submit_result` for the task queue: it reads the task's prompt and inputs,
  reasons, and submits structured output.
- The RCIC sees everything, answers gates in the chat, and can interject.
- Best for: intake, review of conflicts, the auto-fill gate — anything where
  judgement and conversation matter.

Context management: one long chat with 60 documents × 10 sections would blow
the context window. The playbook therefore tells the agent to run the task
loop through **subagents / fresh contexts** when the runtime supports it
(Claude Code `Agent` tool; Codex sub-tasks), one small batch of tasks per
subagent, returning only counts. For large cases, the playbook recommends
switching to batch mode for stages 3–4 and coming back to interactive for
stage 5.

## Batch — headless, one fresh context per task

```
$ uapply-agent run ~/Clients/Zhang_Wei --runtime claude-code --workers 3
$ uapply-agent run ~/Clients/Zhang_Wei --runtime codex --stages classify,extract
```

- The CLI drives the case itself. For each `AgentTask` it spawns the runtime
  headless with a prompt built from the task payload and a JSON output schema:
  - Claude Code: `claude -p "<prompt>" --output-format json` (plus the
    structured-output flag current at build time), model set from the RCIC's
    defaults.
  - Codex: `codex exec "<prompt>" --output-schema schema.json`.
  - Images (rendered pages) are passed as file attachments the runtime
    supports; text-layer PDFs are pre-extracted and inlined.
- The CLI validates the output shape locally before `submit_result` to save a
  server round-trip; the server validates again regardless.
- `--workers N` runs N runtimes concurrently. Keep N small (2–4): plan rate
  limits, not CPU, are the bottleneck.
- Gates in batch mode: the CLI **stops** at each 🧑 gate and prints what it
  needs (`uapply-agent status` shows it); the RCIC answers in interactive mode
  or via the dashboard, then re-runs `run`, which resumes.
- Best for: stages 3–4 on large cases, overnight runs, running several clients
  back-to-back.

Fresh context per task is the main quality lever: every task gets the full
prompt and only its own inputs, with no drift from earlier documents.

## Choosing at runtime

| Situation | Mode |
|---|---|
| New case, intake decisions | Interactive |
| ≤ ~15 documents | Interactive with subagents is fine |
| > ~15 documents or family case | Batch for classify + extract, interactive for resolve |
| Overnight, many clients | Batch, `--stages classify,extract`, review next morning |
| Plan limit reached mid-run | Either mode: agent releases leases; `run` again later resumes |

## Runtime differences that matter

| | Claude Code | Codex |
|---|---|---|
| Playbook delivery | plugin: `skills/` + `commands/` (`/uapply:run`) | `AGENTS.md` at folder or home level + skills dir |
| MCP config | `claude mcp add` / `.mcp.json` | `~/.codex/config.toml` `[mcp_servers.uapply]` |
| Headless | `claude -p` | `codex exec` |
| Structured output | via output-format JSON + schema flag / tool | `--output-schema` |
| Subagents | `Agent` tool | sub-tasks (check current docs) |
| Vision inputs | image files readable by `Read` | image attachments |

Flags change often; the `runners/` module isolates them and the eval suite
runs against both runtimes on every release. Do not hard-code flags in the
playbook.

## What the desktop app does in each mode

Nothing changes for `uapply-desktop`: it still consumes the L3 JSON / IMM PDFs
for the survey. The agent's job ends at producing approved data and, in v1,
downloading those artefacts into `.uapply/output/`.
