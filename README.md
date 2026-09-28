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
**Claude Code CLI** when neither Claude Code nor Codex is on the machine, the
`uapply-agent` CLI, the MCP registration for Claude Code and Codex, and both
sign-ins. The only prerequisites are a uApply account and a Claude Pro/Max
plan (or a Codex CLI already set up).

The Claude Code CLI is required even when the RCIC works in the Claude desktop
app: every local AI task runs in a separate headless `claude -p` process on the
RCIC's plan, and the desktop app does not provide that command. The CLI has its
own sign-in, separate from the desktop app.

macOS / Linux (Terminal):

```bash
curl -LsSf https://raw.githubusercontent.com/uapply1/uapply-agent/main/install.sh | sh
```

Windows, in a normal (not Administrator) PowerShell window:

```powershell
irm https://raw.githubusercontent.com/uapply1/uapply-agent/main/install.ps1 | iex
```

From the Start menu "Run" box or a Command Prompt instead:
`powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/uapply1/uapply-agent/main/install.ps1 | iex"`.
Everything installs into the user profile, so elevation is never needed; an
elevated window can fail with "Program 'powershell.exe' failed to run: Access
is denied".

The browser opens twice: first to sign in to Claude (the CLI's own login), then
to confirm a code and sign in to uApply. Then open Claude Code (desktop app or
terminal) or Codex in a client folder and type `/uapply:run`. The installer
prints what it did, for example:

```
Installing the Claude Code CLI (runs uApply's AI tasks on your Claude plan)...
uapply-agent: /Users/anna/.local/bin/uapply-agent
Runtimes: claude = /Users/anna/.local/bin/claude
Claude Code CLI: signed in
Claude Code: registered via `claude mcp add` (user scope)
Codex: skipped: Codex not found (no `codex` command, no ~/.codex)
Logged in; token stored in keyring
Done. Open Claude Code or Codex in a client folder and type /uapply:run
```

Re-run the same command to upgrade. `uapply-agent setup` alone re-registers
the MCP server (for example after installing Codex later); `uapply-agent
login` alone renews the sign-in. Optional: install AnyChat (macOS arm64 /
Windows x64) for WeChat intake, see
[docs/architecture/chat-sources.md](docs/architecture/chat-sources.md).

The installer downloads the source archive of `main`, so Git is not needed
on the machine. `UAPPLY_AGENT_SOURCE` overrides the source (a release zip, a
PyPI name once published, or a local checkout).

### What the installer does (manual equivalent)

1. `uv` from https://astral.sh/uv, then `uv tool install --force "uapply-agent @ https://github.com/uapply1/uapply-agent/archive/refs/heads/main.zip"`
   (puts `uapply-agent` in `~/.local/bin`).
2. `uapply-agent setup`: registers the MCP server **by absolute path** with
   `claude mcp add --scope user uapply -- ~/.local/bin/uapply-agent mcp`
   (or writes `~/.claude.json` when only the desktop app is installed) and
   `codex mcp add uapply -- … mcp` (or `[mcp_servers.uapply]` in
   `~/.codex/config.toml`). The absolute path matters: GUI apps start with a
   minimal `PATH`. It then checks `claude mcp list` reports the server
   connected, and writes a small Claude Code plugin to
   `~/.claude/skills/uapply/` (generated from the same playbook prompts) so
   that `/uapply:run`, `/uapply:status` and `/uapply:intake-from-chat` are
   plain slash commands.
3. `uapply-agent login`: Auth0 Device Code flow against production
   (`https://api.uapply.io`); the token goes to the OS keychain, or a `0600`
   file under `~/.config/uapply-agent/` when no keychain exists. Until "Allow
   Offline Access" is enabled on the uApply API in Auth0, no refresh token is
   issued; re-run `uapply-agent login` when a command reports `401`.

The commands need the `uapply` MCP server connected in the session (the
tools they call come from it), and both the server and the plugin load at
session start, so open a new session after installing. Without the plugin,
Claude Code lists the server's prompts as `/uapply:run (MCP)` (also reachable
as `/mcp__uapply__run`); Codex shows them in its prompt picker.

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
uv tool uninstall uapply-agent && claude mcp remove --scope user uapply && rm -r ~/.claude/skills/uapply
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
| `no runtime found` | the Claude Code CLI is missing (the desktop app is not enough): rerun the installer, which installs it; or install it with `curl -fsSL https://claude.ai/install.sh \| bash` / `irm https://claude.ai/install.ps1 \| iex`, then run `uapply-agent setup` |
| `whoami` says the Claude CLI is not signed in | run `claude auth login` in a terminal (Claude subscription), then start a new session |
| `AGENT_API_UNAVAILABLE` / `agent_api: false` | the backend in use does not have the agent branch deployed; the case runs in server mode until it is |
| Windows: `Access is denied` starting the installer | run it in a normal PowerShell window, not "Run as administrator" |
| Windows: `failed to remove directory …\uv\tools\uapply-agent` | the exe is in use or owned by an elevated install: close Claude Code / Codex sessions, delete `%APPDATA%\uv\tools\uapply-agent`, rerun the installer |
| `Unknown command: /uapply:run` | run `uapply-agent setup` (writes the plugin and registers the server), then start a **new** session; `claude plugin list` should show `uapply@skills-dir` and `/mcp` the connected server |
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
