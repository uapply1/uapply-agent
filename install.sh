#!/bin/sh
# uapply-agent installer for macOS / Linux:
#   curl -LsSf https://raw.githubusercontent.com/uapply1/uapply-agent/main/install.sh | sh
# Installs uv and the Claude Code CLI (if missing), the uapply-agent CLI, registers
# it with Claude Code and Codex, and signs in to Claude and uApply. Re-run any time
# to upgrade.
set -e
# A source archive, so Git is not required on the machine.
SRC="${UAPPLY_AGENT_SOURCE:-uapply-agent @ https://github.com/uapply1/uapply-agent/archive/refs/heads/main.zip}"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (Python tool manager)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

echo "Installing uapply-agent from $SRC ..."
uv tool install --force --quiet "$SRC"
uv tool update-shell >/dev/null 2>&1 || true

# Local tasks run in a headless Claude Code (or Codex) CLI process on the RCIC's plan;
# the desktop apps do not provide one.
if ! command -v claude >/dev/null 2>&1 && ! command -v codex >/dev/null 2>&1 \
   && [ ! -x "$HOME/.local/bin/claude" ] && [ -z "$UAPPLY_SKIP_CLAUDE_INSTALL" ]; then
  echo "Installing the Claude Code CLI (runs uApply's AI tasks on your Claude plan)..."
  curl -fsSL https://claude.ai/install.sh | bash
fi

# Sign-in prompts need the keyboard; under `curl | sh` stdin is the script itself.
if (exec </dev/tty) 2>/dev/null; then
  "$HOME/.local/bin/uapply-agent" setup </dev/tty
else
  "$HOME/.local/bin/uapply-agent" setup
fi
