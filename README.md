# uapply-agent

Run a uApply case from Claude Code, Codex or the desktop apps, on the RCIC's own
subscription. Design docs are in [docs/](docs/README.md).

Phase 1 scope (this code): bind a client folder to a survey, upload documents,
execute the pipeline's local-agent tasks (OCR / content extraction and the
passport/visa/permit sub-type classification) in fresh headless `claude -p` /
`codex exec` processes, and — optionally — pull a client's WeChat history
through the AnyChat CLI to draft the intake and create the case.

## Install (production)

One command installs everything: `uv` (Python tool manager), the
`uapply-agent` CLI, the MCP registration for Claude Code and Codex, and the
uApply login. It only needs Claude Code or Codex to be installed and logged in
first, plus a uApply account.

macOS / Linux (Terminal):

```bash
curl -LsSf https://raw.githubusercontent.com/uapply1/uapply-agent/main/install.sh | sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/uapply1/uapply-agent/main/install.ps1 | iex"
```

When the browser opens, confirm the code and sign in with the uApply account.
Then open Claude Code (desktop app or terminal) or Codex in a client folder
and type `/uapply:run`. The installer prints what it did, for example:

```
uapply-agent: /Users/anna/.local/bin/uapply-agent
Claude Code: registered via `claude mcp add` (user scope)
Codex: written to /Users/anna/.codex/config.toml
Logged in; token stored in keyring
Done. Open Claude Code or Codex in a client folder and type /uapply:run
```

Re-run the same command to upgrade. `uapply-agent setup` alone re-registers
the MCP server (for example after installing Codex later); `uapply-agent
login` alone renews the sign-in. Optional: install AnyChat (macOS arm64 /
Windows x64) for WeChat intake, see
[docs/architecture/chat-sources.md](docs/architecture/chat-sources.md).

The repository is private today, so the installer needs GitHub access on the
machine (`gh auth login` or an SSH key with
`UAPPLY_AGENT_SOURCE=git+ssh://git@github.com/uapply1/uapply-agent.git`).
Publishing the package to PyPI or making the repository public removes that
step.

### What the installer does (manual equivalent)

1. `uv` from https://astral.sh/uv, then `uv tool install --force git+https://github.com/uapply1/uapply-agent.git`
   (puts `uapply-agent` in `~/.local/bin`).
2. `uapply-agent setup`: registers the MCP server **by absolute path** with
   `claude mcp add --scope user uapply -- ~/.local/bin/uapply-agent mcp`
   (or writes `~/.claude.json` when only the desktop app is installed) and
   `codex mcp add uapply -- … mcp` (or `[mcp_servers.uapply]` in
   `~/.codex/config.toml`). The absolute path matters: GUI apps start with a
   minimal `PATH`. It then checks `claude mcp list` reports the server connected.
3. `uapply-agent login`: Auth0 Device Code flow against production
   (`https://api.uapply.io`); the token goes to the OS keychain, or a `0600`
   file under `~/.config/uapply-agent/` when no keychain exists. Until "Allow
   Offline Access" is enabled on the uApply API in Auth0, no refresh token is
   issued; re-run `uapply-agent login` when a command reports `401`.

The `/uapply:run`, `/uapply:status` and `/uapply:intake-from-chat` commands
are MCP prompts served by `uapply-agent mcp`; they exist only in a session
where the `uapply` server is connected, and MCP servers load at session start,
so open a new session after installing.

### Settings

`~/.config/uapply-agent/config.json`, edited with `uapply-agent config --set KEY=VALUE`:

| Key | Default | Meaning |
|---|---|---|
| `backend_url` | `https://api.uapply.io` | uApply API; change only for staging |
| `auth0_domain` / `auth0_client_id` / `auth0_audience` | production tenant | change only for staging |
| `runtime` | `auto` | `claude-code` or `codex` when both are installed |
| `model` | runtime default | model passed to the headless runtime |
| `workers` | `2` | parallel headless processes |
| `force_ocr` | `false` | ignore PDF text layers and always OCR with the model |
| `team_id` | auto | team for created cases (business accounts with several teams) |
| `chat_source` | `anychat` | `none` disables chat intake |
| `chat_default_days` | `180` | how far back `chat_fetch` reads |
| `chat_upload` | `true` | `false` keeps transcripts local (hints only) |

Environment overrides: `UAPPLY_BACKEND_URL`, `UAPPLY_RUNTIME`, `UAPPLY_MODEL`,
`UAPPLY_TOKEN`, `UAPPLY_TEAM_ID`, `UAPPLY_FORCE_OCR`, `UAPPLY_CHAT_SOURCE`,
`ANYCHAT_BIN`.

### Uninstall

```bash
uv tool uninstall uapply-agent && claude mcp remove --scope user uapply
```

### Server side (uApply operations)

The agent needs the backend branch that adds the local-agent task queue:
`ai_parse/agent/` with migrations `survey.0058` and `ai_parse.0008`, the
`AGENT_*` settings, the `expire_agent_tasks` Celery beat entry, and Celery
workers on `document_queue` / `analysis_queue`. Endpoints live under
`/api/ai-parse/agent/` (see [docs/reference/backend-api.md](docs/reference/backend-api.md)).
In Auth0, the native client must have the Device Code grant and the API should
have "Allow Offline Access" enabled so refresh tokens are issued.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `no runtime found` | install Claude Code or Codex and log in to it; on Windows make sure `claude`/`codex` is on `PATH` |
| `Unknown command: /uapply:run` | the `uapply` MCP server is not connected: run `uapply-agent setup`, then start a new session; `/mcp` in a session shows server status |
| `401` from the API | `uapply-agent login` again (no refresh token yet) |
| `404` on `/api/ai-parse/agent/...` | the backend in use does not have the agent branch deployed |
| `chat sources` → `not_installed` / `not_logged_in` | install the AnyChat plugin and run its own login; the agent never handles that token |
| headless run hits the plan's usage limit | the executor stops with `PlanLimited`; resume with `uapply-agent run --follow` later |

## Install (dev)

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest
```

Point the dev install at a local stack with
`UAPPLY_BACKEND_URL=http://localhost:8000` and `uapply-agent login --token <jwt>`.

## Use (CLI, batch mode)

```bash
cd ~/Clients/Zhang_Wei
uapply-agent init --survey <survey-id>    # sets the case to llm_mode=local_agent
uapply-agent status
uapply-agent run --follow                 # execute tasks until processing is done
uapply-agent chat find "张伟"              # AnyChat: candidates
uapply-agent chat fetch "张伟" --days 365  # transcript → intake hints → agent_survey upload
```

Interactive, from Claude Code or Codex: `cd` into the client folder, start the
runtime, and use `/uapply:run`, `/uapply:status` or `/uapply:intake-from-chat`.
The MCP server exposes `case_status`, `scan_folder`, `sync_documents`,
`run_tasks`, `wait_for_stage`, `chat_fetch`, `create_case` and friends
([docs/reference/mcp-tools.md](docs/reference/mcp-tools.md)). `run_tasks`
never executes work in the chat: it spawns the runtime headless per task with
the task's prompt as a real system prompt.

## Layout

```
src/uapply_agent/
  cli.py          setup | login | logout | init | status | run | mcp | chat | clean | config
  integrate.py    MCP registration for Claude Code / Codex (used by `setup`)
  mcp_server.py   stdio MCP server (tools, prompts, instructions)
  executor.py     pull → download inputs → spawn runtime → validate → submit
  runners/        claude_code.py, codex.py (all runtime flags live here)
  api.py          typed backend client
  auth.py         Auth0 device login / pasted token
  folder.py       .uapply/ manifest + case.json
  local_ops.py    page rendering, pdf text, HEIC → JPEG
  chat/           AnyChat source, transcript store, PDF render, intake call
playbook/SOURCE.md   instructions + prompts (copied into the package)
```
