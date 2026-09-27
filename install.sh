#!/bin/sh
# uapply-agent installer for macOS / Linux:
#   curl -LsSf https://raw.githubusercontent.com/uapply1/uapply-agent/main/install.sh | sh
# Installs uv (if missing), the uapply-agent CLI, registers it with Claude Code
# and Codex, and signs in to uApply. Re-run any time to upgrade.
set -e
SRC="${UAPPLY_AGENT_SOURCE:-git+https://github.com/uapply1/uapply-agent.git}"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (Python tool manager)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

echo "Installing uapply-agent from $SRC ..."
uv tool install --force --quiet "$SRC"
uv tool update-shell >/dev/null 2>&1 || true

"$HOME/.local/bin/uapply-agent" setup
