#!/bin/sh
# uapply-agent installer for macOS and Linux:
#   curl -LsSf https://raw.githubusercontent.com/uapply1/uapply-agent/main/install.sh | sh
#
# Installs uv and the uapply-agent CLI, registers it with Claude Code and Codex, and signs in to
# uApply. Run it again at any time to upgrade.
#
# Environment:
#   UAPPLY_AGENT_SOURCE        install from this pip source instead of the latest commit of main
#   UAPPLY_INSTALL_CLAUDE_CLI  also install the Claude Code CLI (for `uapply-agent run` in a
#                              terminal; Claude Code sessions run tasks without it)
#
# Everything runs inside main(), so a download cut short by the network cannot run half a script.

set -eu

REPO="uapply1/uapply-agent"

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

uv_target() {
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) echo "x86_64-unknown-linux-gnu" ;;
    Linux-aarch64 | Linux-arm64) echo "aarch64-unknown-linux-gnu" ;;
    Darwin-x86_64) echo "x86_64-apple-darwin" ;;
    Darwin-arm64) echo "aarch64-apple-darwin" ;;
    *) echo "" ;;
  esac
}

install_uv() {
  # The official release archive, checksum-verified, into ~/.local/bin.
  target="$(uv_target)"
  if [ -z "$target" ]; then
    say "No prebuilt uv for $(uname -s) $(uname -m); using astral's installer."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    return
  fi
  base="https://github.com/astral-sh/uv/releases/latest/download"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL --retry 3 -o "$tmp/uv.tar.gz" "$base/uv-$target.tar.gz" || fail "could not download uv"
  expected="$(curl -fsSL --retry 3 "$base/uv-$target.tar.gz.sha256" | cut -d' ' -f1)"
  [ "$(sha256_of "$tmp/uv.tar.gz")" = "$expected" ] || fail "the uv download failed its checksum"
  tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
  mkdir -p "$HOME/.local/bin"
  cp "$tmp/uv-$target/uv" "$tmp/uv-$target/uvx" "$HOME/.local/bin/"
}

main() {
  # A source archive of the exact commit of main: Git is not needed, and the agent's self-update
  # knows which version is installed.
  sha="$(curl -fsSL -m 10 -H 'Accept: application/vnd.github.sha' \
    "https://api.github.com/repos/$REPO/commits/main" 2>/dev/null || true)"
  if [ "${#sha}" -eq 40 ]; then archive="$sha.zip"; else sha=""; archive="refs/heads/main.zip"; fi
  if [ -n "${UAPPLY_AGENT_SOURCE:-}" ]; then
    src="$UAPPLY_AGENT_SOURCE"
    UAPPLY_INSTALLED_SHA=""          # not a known commit: the first start installs a versioned copy
  else
    src="uapply-agent @ https://github.com/$REPO/archive/$archive"
    UAPPLY_INSTALLED_SHA="$sha"
  fi
  export UAPPLY_INSTALLED_SHA

  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv (Python tool manager)..."
    install_uv
  fi

  say "Installing uapply-agent from $src ..."
  uv tool install --force --quiet --compile-bytecode "$src"
  uv tool update-shell >/dev/null 2>&1 || say "Note: add $(uv tool dir --bin) to your PATH."
  agent="$(uv tool dir --bin)/uapply-agent"
  [ -x "$agent" ] || fail "uapply-agent was not installed at $agent"

  # In Claude Code the tasks run inside the RCIC's own session. The CLI is only for `uapply-agent run`
  # in a terminal (or Codex users, who have their own CLI), so it is optional.
  if [ -n "${UAPPLY_INSTALL_CLAUDE_CLI:-}" ] && ! command -v claude >/dev/null 2>&1 \
     && [ ! -x "$HOME/.local/bin/claude" ]; then
    say "Installing the Claude Code CLI (for uapply-agent run in a terminal)..."
    # A failure here must not undo the uApply install; setup reports a missing CLI as well.
    curl -fsSL https://claude.ai/install.sh | bash || \
      say "WARNING: the Claude Code CLI is not installed. Run this installer again, or: curl -fsSL https://claude.ai/install.sh | bash"
  fi

  # Sign-in prompts need the keyboard; under `curl | sh` stdin is the script itself.
  if (exec </dev/tty) 2>/dev/null; then
    "$agent" setup </dev/tty
  else
    "$agent" setup
  fi
}

main "$@"
