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
function Install-ClaudeCli {
  # Same source and checksum check as https://claude.ai/install.ps1, but the ~250 MB binary is
  # fetched with resume + retries: a single Invoke-WebRequest fails on flaky links with
  # "unexpected EOF or 0 bytes from the transport stream".
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
  $base = "https://downloads.claude.ai/claude-code-releases"
  $platform = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "win32-arm64" } else { "win32-x64" }
  $version = "$(Invoke-RestMethod -Uri "$base/latest")".Trim()
  if ($version -notmatch '^\d+\.\d+\.\d+') {
    throw "downloads.claude.ai returned no version (unreachable, or Claude Code is not available in this region)"
  }
  $entry = (Invoke-RestMethod -Uri "$base/$version/manifest.json").platforms.$platform
  if (-not $entry) { throw "platform $platform not in the Claude Code manifest" }
  $dir = "$env:USERPROFILE\.claude\downloads"
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $exe = "$dir\claude-$version-$platform.exe"
  $url = "$base/$version/$platform/claude.exe"
  $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
  $ok = $false
  for ($i = 1; $i -le 20 -and -not $ok; $i++) {
    if ((Test-Path $exe) -and ((Get-Item $exe).Length -ge $entry.size)) {
      $ok = (Get-FileHash -Path $exe -Algorithm SHA256).Hash.ToLower() -eq $entry.checksum
      if (-not $ok) { Remove-Item -Force $exe }   # complete but corrupt: start over
      continue
    }
    if ($i -gt 1) { Write-Host "  download interrupted, resuming (attempt $i of 20)..."; Start-Sleep -Seconds 3 }
    if ($curl) {
      # A stalled link (0 B/s) aborts after 30 s so the loop resumes; no --retry: it restarts from 0.
      & $curl.Source -fL --connect-timeout 30 --speed-limit 1024 --speed-time 30 -C - -o $exe $url
    } else {
      try { Start-BitsTransfer -Source $url -Destination $exe -ErrorAction Stop } catch { Write-Host "  $_" }
    }
  }
  if (-not $ok -and (Test-Path $exe) -and ((Get-Item $exe).Length -ge $entry.size)) {
    $ok = (Get-FileHash -Path $exe -Algorithm SHA256).Hash.ToLower() -eq $entry.checksum
  }
  if (-not $ok) { throw "could not download claude.exe $version completely (network interrupted)" }
  & $exe install
  $code = $LASTEXITCODE
  Start-Sleep -Seconds 1
  Remove-Item -Force $exe -ErrorAction SilentlyContinue
  if ($code -ne 0) { throw "claude install exited with code $code" }
}

$claudeExe = "$env:USERPROFILE\.local\bin\claude.exe"
$haveRuntime = (Get-Command claude -ErrorAction SilentlyContinue) -or (Get-Command codex -ErrorAction SilentlyContinue) -or (Test-Path $claudeExe)
if (-not $haveRuntime -and -not $env:UAPPLY_SKIP_CLAUDE_INSTALL) {
  Write-Host "Installing the Claude Code CLI (runs uApply's AI tasks on your Claude plan)..."
  try {
    Install-ClaudeCli
  } catch {
    # Never lose the uApply install over this; setup below reports the missing CLI too.
    Write-Warning "Claude Code CLI not installed: $_"
    Write-Warning "Run this installer again later (it resumes the download), or install it with: irm https://claude.ai/install.ps1 | iex"
  }
}
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"

& "$env:USERPROFILE\.local\bin\uapply-agent.exe" setup
if ($LASTEXITCODE -ne 0) { throw "uapply-agent setup failed (exit $LASTEXITCODE)" }
