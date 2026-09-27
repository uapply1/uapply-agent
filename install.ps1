# uapply-agent installer for Windows (PowerShell):
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/uapply1/uapply-agent/main/install.ps1 | iex"
# Installs uv (if missing), the uapply-agent CLI, registers it with Claude Code
# and Codex, and signs in to uApply. Re-run any time to upgrade.
$ErrorActionPreference = "Stop"
$Src = if ($env:UAPPLY_AGENT_SOURCE) { $env:UAPPLY_AGENT_SOURCE } else { "git+https://github.com/uapply1/uapply-agent.git" }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "Installing uv (Python tool manager)..."
  irm https://astral.sh/uv/install.ps1 | iex
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

Write-Host "Installing uapply-agent from $Src ..."
uv tool install --force --quiet $Src
uv tool update-shell | Out-Null

& "$env:USERPROFILE\.local\bin\uapply-agent.exe" setup
