# uapply-agent

Run a uApply case from Claude Code, Codex or the desktop apps, on the RCIC's own
subscription. Design docs are in [docs/](docs/README.md).

What it does today: bind a client folder to an existing uApply case, or
create one after the RCIC confirms (optionally drafting the intake from the
client's WeChat history through the AnyChat CLI); upload the folder's
documents; run every model call of the case's processing and analysis on the
RCIC's plan, as subagents of the Claude Code session (or headless `codex exec`
processes in Codex, experimental); then finish the case with archives and compression on uApply,
IMM PDF auto-fill (Adobe Acrobat Pro on the RCIC's Windows PC, otherwise
uApply's platform filler) and an end-of-run report.

## Install (production)

One command installs everything: `uv` (Python tool manager), the
`uapply-agent` CLI, the MCP registration for Claude Code and Codex, and the
uApply sign-in. The only prerequisites are a uApply account and Claude Code
(the desktop app's Code tab, or the terminal) on a Claude Pro/Max plan, or Codex.

In Claude Code the AI tasks always run as subagents of the RCIC's own session,
on the login and plan that session already has. uapply-agent never installs,
signs in to or runs the Claude Code CLI (`claude`) on its own: no second
Claude sign-in and no headless `claude -p`. Codex users need the `codex` CLI (setup also finds the one bundled with
the Codex desktop app); the app and CLI share one login. Setup writes the
commands as Codex skills in `~/.agents/skills`: type `$uapply-run`,
`$uapply-status` or `$uapply-intake-from-chat`.

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

The browser opens once, to confirm a code and sign in to uApply. Then open
a client folder in Claude Code (the desktop app's **Code** tab, not Chat, or
the terminal) or Codex and type `/uapply:run`. The installer prints what it did, for example:

```
uapply-agent: /Users/anna/.local/bin/uapply-agent
Runtimes: no `codex` CLI found. Claude Code needs none (tasks run as subagents of your session); Codex runs tasks through the `codex` CLI.
Claude Code: written to /Users/anna/.claude.json
Claude Code: commands written to /Users/anna/.claude/skills/uapply
Codex: skipped: Codex not found (no `codex` command, no ~/.codex)
Logged in; token stored in keyring
Done. Next: open the client folder in Claude Code (the desktop app's Code tab, not Chat, or the terminal) and type /uapply:run
```

The agent updates itself at each start (see Updates); re-running the installer also upgrades. `uapply-agent setup` alone re-registers
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
2. `uapply-agent setup`: registers the MCP server **by absolute path** as a
   user-scope server in `~/.claude.json` (read by Claude Code in the terminal
   and in the desktop app; written directly, never via `claude mcp add`) and
   `[mcp_servers.uapply]` in `~/.codex/config.toml` (with
   `default_tools_approval_mode = "approve"`, or Codex refuses the tools under
   approval policy "never"). The absolute path matters: GUI apps start with a
   minimal `PATH`. It then writes a small Claude Code plugin to
   `~/.claude/skills/uapply/` (generated from the same playbook prompts) so
   that `/uapply:run`, `/uapply:status` and `/uapply:intake-from-chat` are
   plain slash commands.
3. `uapply-agent login`: Auth0 Device Code flow against production
   (`https://api.uapply.io`); the token goes to the OS keychain, or a `0600`
   file under `~/.config/uapply-agent/` when no keychain exists. A refresh
   token renews the sign-in when the Auth0 tenant issues one; otherwise re-run
   `uapply-agent login` when a command reports `401`.

The commands need the `uapply` MCP server connected in the session (the
tools they call come from it), and both the server and the plugin load at
session start, so open a new session after installing. Without the plugin,
Claude Code lists the server's prompts as `/uapply:run (MCP)` (also reachable
as `/mcp__uapply__run`); Codex uses the `$uapply-*` skills setup writes.

### Updates

The agent updates itself. Each time a Claude / Codex session starts it (`uapply-agent mcp`),
and each `uapply-agent run`, it asks GitHub for the latest commit on `main`. When there is a
newer one it installs it next to the current install and hands the session to it, so every
run uses the latest release. The installed command never overwrites itself (Windows locks a
running program's files); versions live in `~/.local/share/uapply-agent/versions/<commit>/`
(`%LOCALAPPDATA%\uapply-agent\versions\` on Windows) and the three newest are kept.

- The check takes about a second; a normal update a few seconds. If an update takes longer than
  25 s at a session start (3 min for `uapply-agent run`), for example a first-time dependency
  download on a slow link, the installed version starts and the update finishes in the background
  for the next one.
- Offline, the installed version starts as usual.
- `uapply-agent update` updates immediately and prints the result; `uapply-agent --version` shows
  the commit. `uapply-agent config --set auto_update=false` (or `UAPPLY_NO_UPDATE=1`) turns it off.
- New `/uapply:*` commands from an update are written at that start and load in the next session.

### Settings

`~/.config/uapply-agent/config.json`, edited with `uapply-agent config --set KEY=VALUE`:

| Key | Default | Meaning |
|---|---|---|
| `backend_url` | `https://api.uapply.io` | uApply API; change only to use another uApply environment |
| `app_url` | derived | dashboard used in report links; empty means `backend_url` with `api.` replaced by `app.` |
| `auto_update` | `true` | install and switch to the latest `main` at session / run start |
| `auth0_domain` / `auth0_client_id` / `auth0_audience` | production tenant | change only together with `backend_url` |
| `runtime` | `auto` | headless runtime for `uapply-agent run` and Codex sessions: `codex` (Claude Code never runs headless) |
| `model` | runtime default | model passed to the headless runtime |
| `codex_bin` | recorded by `setup` | absolute path of the Codex CLI |
| `workers` | `2` | parallel headless processes (at most 4) |
| `task_timeout_s` | `300` | time limit for one headless model call |
| `force_ocr` | `false` | ignore PDF text layers and always OCR with the model |
| `team_id` | auto | team for created cases (business accounts with several teams) |
| `chat_source` | `anychat` | `none` disables chat intake |
| `anychat_bin` | auto | AnyChat CLI location |
| `chat_default_days` | `180` | how far back `chat_fetch` reads |
| `chat_upload` | `true` | file fetched transcripts on the case; `false` keeps them in the client folder (hints only) |
| `chat_max_chars` | `200000` | transcript length sent to the intake call (the most recent part is kept) |

Environment overrides: `UAPPLY_BACKEND_URL`, `UAPPLY_APP_URL`, `UAPPLY_RUNTIME`,
`UAPPLY_MODEL`, `UAPPLY_TEAM_ID`, `UAPPLY_FORCE_OCR`, `UAPPLY_CHAT_SOURCE`,
`UAPPLY_CODEX_BIN`, `ANYCHAT_BIN`; `UAPPLY_TOKEN` overrides
the stored access token and `UAPPLY_FOLDER` the client folder.

### Uninstall

```bash
uv tool uninstall uapply-agent && rm -r ~/.claude/skills/uapply ~/.agents/skills/uapply-*
```

Then delete the `uapply` entry under `mcpServers` in `~/.claude.json` and the
`[mcp_servers.uapply]` table in `~/.codex/config.toml`.

### Backend requirements

Local processing needs a uApply backend that serves the local-agent API under
`/api/ai-parse/agent/` (see [docs/reference/backend-api.md](docs/reference/backend-api.md)).
For a `local_agent` case the backend starts nothing on upload and runs no AI
itself: every model call is answered by the RCIC's agent (decision D14 in
[docs/design/decisions.md](docs/design/decisions.md)). Against a backend without
that API, `whoami` and `case_status` report `agent_api: false`, cases are bound
in server mode, and uApply's own models process the documents.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `no runtime found` (`uapply-agent run`, or a Codex session) | headless tasks need the Codex CLI: install it, sign in with `codex login`, then run `uapply-agent setup`. Claude Code sessions need no CLI |
| `run_tasks` returns `mode: session` with tasks but nothing runs | the conversation must spawn the `uapply:task-runner` subagents; the agent comes with the plugin, so after `uapply-agent setup` start a **new** session (`/agents` lists `uapply:task-runner`) |
| `does NOT start on this machine` | the installed `codex` cannot run here; reinstall the Codex CLI, then run `uapply-agent setup` (it prefers whichever build starts) |
| `codex exited 1: …` in `run_tasks` failures | the text after it is Codex's own error. `Not logged in`: run `codex login`. Schema errors (`additionalProperties`, `required`) should not occur since the runner converts schemas to OpenAI's strict dialect; report them |
| `AGENT_API_UNAVAILABLE` / `agent_api: false` | the backend in use does not serve the local-agent API; the case runs in server mode (uApply's own models process the documents) |
| Windows: `Access is denied` starting the installer, or "running as Administrator" | run it in a normal PowerShell window, not "Run as administrator"; the installer refuses elevated windows |
| Windows: `failed to remove directory …\uv\tools\uapply-agent` | the exe is in use or owned by an elevated install: close Claude Code / Codex sessions, delete `%APPDATA%\uv\tools\uapply-agent`, rerun the installer |
| Claude desktop app: "I don't recognize `/uapply:run`" | you are in the **Chat** tab; switch to the **Code** tab (`</>`, needs a paid Claude plan) and open the client folder |
| `Unknown command: /uapply:run` | run `uapply-agent setup` (writes the plugin and registers the server), then start a **new** session; `/plugin` should list `uapply@skills-dir` and `/mcp` the connected server |
| `401` from the API | `uapply-agent login` again (no refresh token yet) |
| `chat sources` → `not_installed` / `not_logged_in` | install the AnyChat plugin and run its own login; the agent never handles that token |
| `whoami` → `acrobat.available: false` although Acrobat is installed | Reader, or Acrobat without a Pro licence, has no automation; forms are filled on uApply instead. `uapply-agent acrobat` shows the reason |
| headless run hits the plan's usage limit | `run_tasks` reports `plan_limited` (`uapply-agent run` exits with code 4); run again after the limit resets, e.g. `uapply-agent run --follow` |
| `run_tasks` reports `runtime_error` (`uapply-agent run` exits with code 5) | the runtime cannot work, e.g. the Codex CLI is not signed in (`codex login`): fix the reported cause, then run again |

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
uapply-agent chat sources                 # AnyChat installed and signed in?
uapply-agent chat find "张伟"              # AnyChat: candidates
uapply-agent chat fetch "张伟" --days 365  # saves the transcript under .uapply/chat/ (no hints, no upload)
uapply-agent acrobat                      # can this PC fill IMM forms with Acrobat Pro?
uapply-agent clean                        # delete .uapply/cache/ and .uapply/output/ files
uapply-agent config --set chat_upload=false
```

`chat fetch` on the command line only saves the transcript; the `chat_fetch`
MCP tool also derives intake hints and files it on the case, and `chat_upload`
files a transcript saved earlier.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | the command ran and reports a negative result (e.g. `acrobat` finds no usable Acrobat Pro) |
| 2 | usage error (bad arguments, folder has no case) |
| 3 | backend, login, runtime, setup or chat-source error |
| 4 | `run` stopped at the runtime's plan limit; run again later |
| 5 | `run` stopped because the runtime cannot work (e.g. not signed in) |
| 130 | interrupted (Ctrl+C) |

Interactive, from Claude Code or Codex: `cd` into the client folder, start the
runtime, and use `/uapply:run`, `/uapply:status` or `/uapply:intake-from-chat`
(in Codex: `$uapply-run`, `$uapply-status`, `$uapply-intake-from-chat`).
The MCP server exposes 24 tools, among them `case_status`, `scan_folder`,
`sync_documents`, `start_processing`, `run_tasks`, `wait_for_stage`,
`confirm_documents`, `autofill_forms`, `final_report`, `chat_fetch` and
`create_case` ([docs/reference/mcp-tools.md](docs/reference/mcp-tools.md)). `run_tasks`
never executes work in the conversation. In Claude Code (`task_runner`
`session`) it prepares one brief per task and the conversation spawns a
`uapply:task-runner` subagent per brief: an isolated context that reads the
brief and its files and calls `submit_task`, on the session's own login and
plan. Otherwise (`cli`: Codex, batch mode) it spawns the runtime headless per
task with the task's prompt as a real system prompt.

After the analysis `/uapply:run` finishes the case: `confirm_documents` (the
dashboard's Confirm: archives + compression on uApply) and `autofill_forms`.
With **Adobe Acrobat Pro on Windows** the IMM PDFs are filled on the RCIC's PC
(uApply sends the recorded field operations; the agent replays them in Acrobat
as JavaScript batches through Acrobat's own automation, as uApply's platform
filler does, and only ever in the form it opened; copies land in
`.uapply/output/imm_pdfs/`). Without it
— Reader only, macOS, automation failing, or a form where no field could be
written — uApply's own filler does it (no AI). `uapply-agent acrobat` checks the
PC; `uapply-agent acrobat --pdf IMM5709.pdf` test-fills one field.

The run ends with `final_report`: the dashboard's Submit step in the chat — status,
AI Check conflicts / doubtful / missing, documents, IMM forms with fill rate,
archives — saved as `uApply output/report.md` next to the final package zip
(forms unpacked in `uApply output/Forms/`). Its links open the dashboard on the
right step (`?step=ai_check`, `?step=submit`) and, for PR cases, on a one-click
"Start online portal" (`?step=submit&portal=1`): the IRCC portal automation runs
through the uApply Chrome extension, which only the dashboard page can talk to.
The dashboard host comes from `app_url` (default: `api.` → `app.` of the backend).

## Layout

```
src/uapply_agent/
  cli.py          update | setup | login | logout | init | status | run | mcp | chat | clean | acrobat | config
  mcp_server.py   stdio MCP server (tools, prompts, instructions)
  SOURCE.md       the playbook: server instructions + /uapply:* prompts (single source)
  playbook.py     reads SOURCE.md; generates the Claude Code plugin commands
  context.py      ServerContext and ToolError (the {ok: false, error} result)
  cases.py        bind / create cases, progress
  uploads.py      upload without duplicating case documents
  folder.py       .uapply/ manifest + case.json, folder scan
  briefs.py       task preparation shared by both runners: inputs, page images, briefs, result checks
  session_runner.py  tasks as subagents of the Claude Code session: briefs per round, local finishes
  executor.py     headless runner: pull → prepare → spawn runtime → validate → submit
  runners/        codex.py (experimental; Claude Code never runs headless); all runtime flags live here
  local_ops.py    page rendering, pdf text, HEIC → JPEG
  autofill.py     IMM PDF auto-fill: local Acrobat Pro or uApply's platform filler
  acrobat.py      Adobe Acrobat Pro automation (Windows): JavaScript batches via AFormAut, as pdf_auto
  report.py       end-of-run report + final package download
  api.py          typed backend client
  auth.py         Auth0 device login / pasted token
  config.py       settings + credential storage
  integrate.py    MCP registration for Claude Code / Codex (used by `setup`)
  updater.py      self-update at start: versions/<commit>/, hand-over to the newest
  constants.py    document statuses, supported file types
  util.py         small file helpers
  chat/           AnyChat source, transcript store, PDF render, intake call, filing
```
