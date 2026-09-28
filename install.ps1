# uapply-agent installer for Windows. In a normal (non-administrator) PowerShell window:
#   irm https://raw.githubusercontent.com/uapply1/uapply-agent/main/install.ps1 | iex
# Installs uv and the Claude Code CLI (if missing), the uapply-agent CLI, registers
# it with Claude Code and Codex, and signs in to Claude and uApply. Re-run any time
# to upgrade.
$ErrorActionPreference = "Stop"
# A source archive, so Git is not required on the machine.
$Src = if ($env:UAPPLY_AGENT_SOURCE) { $env:UAPPLY_AGENT_SOURCE } else { "uapply-agent @ https://github.com/uapply1/uapply-agent/archive/refs/heads/main.zip" }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "Installing uv (Python tool manager)..."
  irm https://astral.sh/uv/install.ps1 | iex
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

# A running MCP server (an open Claude Code / Codex session) locks the exe on Windows.
$running = Get-Process uapply-agent -ErrorAction SilentlyContinue
if ($running) {
  Write-Host "Stopping the running uapply-agent MCP server for the upgrade (open sessions reconnect on restart)..."
  $running | Stop-Process -Force
  Start-Sleep -Seconds 1
}

Write-Host "Installing uapply-agent from $Src ..."
uv tool install --force --quiet $Src
if ($LASTEXITCODE -ne 0) {
  $tools = "$env:APPDATA\uv\tools\uapply-agent"
  throw "uv tool install failed (exit $LASTEXITCODE). If it reported 'Access is denied' on $tools, close Claude Code / Codex, " +
        "delete that folder (it may have been created by an elevated install), then run this installer again in a normal window."
}
uv tool update-shell | Out-Null

# Local tasks run in a headless Claude Code (or Codex) CLI process on the RCIC's plan;
# the desktop apps do not provide one. Git for Windows is not required.
$claudeExe = "$env:USERPROFILE\.local\bin\claude.exe"
$haveRuntime = (Get-Command claude -ErrorAction SilentlyContinue) -or (Get-Command codex -ErrorAction SilentlyContinue) -or (Test-Path $claudeExe)
if (-not $haveRuntime -and -not $env:UAPPLY_SKIP_CLAUDE_INSTALL) {
  Write-Host "Installing the Claude Code CLI (runs uApply's AI tasks on your Claude plan)..."
  irm https://claude.ai/install.ps1 | iex
}
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"

& "$env:USERPROFILE\.local\bin\uapply-agent.exe" setup
if ($LASTEXITCODE -ne 0) { throw "uapply-agent setup failed (exit $LASTEXITCODE)" }
