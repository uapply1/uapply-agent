# uapply-agent

Run a uApply case from Claude Code, Codex or the desktop apps, on the RCIC's own
subscription. Design docs are in [docs/](docs/README.md).

Phase 1 scope (this code): bind a client folder to a survey, upload documents,
execute the pipeline's local-agent tasks (OCR / content extraction and the
passport/visa/permit sub-type classification) in fresh headless `claude -p` /
`codex exec` processes, and — optionally — pull a client's WeChat history
through the AnyChat CLI to draft the intake and create the case.

## Install (production)

The agent is a small Python CLI installed on the RCIC's machine. It is not on
PyPI; install it straight from the GitHub repository.

### 1. Prerequisites

| Need | Why | Check |
|---|---|---|
| Python 3.11+ | runtime for the CLI | `python3 --version` |
| [`uv`](https://docs.astral.sh/uv/) (or `pipx`) | installs the CLI in its own isolated environment and puts `uapply-agent` on `PATH` | `uv --version` |
| Claude Code **or** Codex CLI, logged in | every model call runs through it on the RCIC's plan | `claude --version` / `codex --version` |
| a uApply account | the case, documents and results live on api.uapply.io | log in to the dashboard once |
| AnyChat (optional; macOS arm64 / Windows x64) | chat-history intake, see [docs/architecture/chat-sources.md](docs/architecture/chat-sources.md) | `anychat whoami --json` |

Install `uv` if missing:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh        # macOS / Linux
```

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows
```

### 2. Install the CLI

```bash
uv tool install git+https://github.com/uapply1/uapply-agent.git
```

Pin a release instead of `main` with `@v0.1.0` (or a commit) at the end of the
URL. With `pipx`: `pipx install git+https://github.com/uapply1/uapply-agent.git`.
The repository is private, so the machine needs GitHub access (SSH key or
`gh auth login`); use `git+ssh://git@github.com/uapply1/uapply-agent.git` for SSH.

Verify:

```bash
uapply-agent --version
uapply-agent chat sources          # AnyChat availability; "unsupported_platform" on Linux is expected
```

### 3. Sign in to uApply

The defaults already point at production (`https://api.uapply.io`, the uApply
Auth0 tenant, Device Code flow). No config file is needed.

```bash
uapply-agent login
```

Open the printed URL, confirm the code, and log in with your uApply account.
The token is stored in the OS keychain (macOS Keychain, Windows Credential
Manager, Secret Service on Linux) or, failing that, in a `0600` file under
`~/.config/uapply-agent/`. `uapply-agent logout` removes it.

Until "Allow Offline Access" is enabled on the uApply API in Auth0, no refresh
token is issued and the login lasts as long as the access token; re-run
`uapply-agent login` when a command reports `401`.

Alternative for scripted use: `uapply-agent login --token <jwt>` or
`--token-stdin`, or set `UAPPLY_TOKEN` in the environment.

### 4. Register the MCP server with your coding agent

Claude Code (user scope, so it works from any client folder):

```bash
claude mcp add --scope user uapply -- uapply-agent mcp
```

Codex:

```bash
codex mcp add uapply -- uapply-agent mcp
```

or in `~/.codex/config.toml`:

```toml
[mcp_servers.uapply]
command = "uapply-agent"
args = ["mcp"]
```

Claude Desktop / other MCP clients: point a stdio server at the command
`uapply-agent` with the argument `mcp`.

### 5. First case

```bash
mkdir -p ~/Clients/Zhang_Wei && cp <client documents> ~/Clients/Zhang_Wei/
cd ~/Clients/Zhang_Wei && claude          # or: codex
> /uapply:run
```

The agent binds the folder to a survey (or, with WeChat history, drafts the
intake and creates the case after you type "create case"), uploads the
documents, runs the local tasks on your plan and waits for the pipeline.
Everything the agent keeps locally lives in `.uapply/` inside the folder.

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

### Upgrade and uninstall

```bash
uv tool upgrade uapply-agent          # or: uv tool install --force git+https://github.com/uapply1/uapply-agent.git@v0.2.0
uv tool uninstall uapply-agent
```

The MCP registration keeps working across upgrades because it refers to the
`uapply-agent` command, not to a path.

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
| `401` from the API | `uapply-agent login` again (no refresh token yet, see step 3) |
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
  cli.py          login | logout | init | status | run | mcp | chat | clean | config
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
