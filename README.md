# uapply-agent

Run a uApply case from Claude Code, Codex or the desktop apps, on the RCIC's own
subscription. Design docs are in [docs/](docs/README.md).

Phase 1 scope (this code): bind a client folder to a survey, upload documents,
and execute the pipeline's local-agent tasks — currently the passport/visa/permit
sub-type classification — in fresh headless `claude -p` / `codex exec` processes.

## Install (dev)

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest
```

## Use

```bash
uapply-agent config --set backend_url=https://api.uapply.io
uapply-agent login --token <jwt>          # or plain `login` once an Auth0 device client exists
cd ~/Clients/Zhang_Wei
uapply-agent init --survey <survey-id>    # sets the case to llm_mode=local_agent
uapply-agent run --follow                 # batch: execute tasks until processing is done
```

Interactive, from Claude Code:

```bash
claude mcp add uapply -- uapply-agent mcp      # once
cd ~/Clients/Zhang_Wei && claude
> /uapply:run
```

The MCP server exposes `case_status`, `scan_folder`, `sync_documents`,
`run_tasks`, `wait_for_stage` and friends, plus the `run` / `status` prompts.
`run_tasks` never executes work in the chat: it spawns the runtime headless
per task with the task's prompt as a real system prompt.

## Layout

```
src/uapply_agent/
  cli.py          login | init | status | run | mcp | clean | config
  mcp_server.py   stdio MCP server (tools, prompts, instructions)
  executor.py     pull → download inputs → spawn runtime → validate → submit
  runners/        claude_code.py, codex.py (all runtime flags live here)
  api.py          typed backend client
  folder.py       .uapply/ manifest + case.json
  local_ops.py    page rendering, pdf text, HEIC → JPEG
playbook/SOURCE.md   instructions + prompts (copied into the package)
```
