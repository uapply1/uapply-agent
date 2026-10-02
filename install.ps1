# uapply-agent installer for Windows. In a normal (non-administrator) PowerShell window:
#   irm https://raw.githubusercontent.com/uapply1/uapply-agent/main/install.ps1 | iex
#
# Installs uv and the uapply-agent CLI, registers it with Claude Code and Codex, and signs in to
# uApply. Run it again at any time to upgrade.
#
# Environment:
#   UAPPLY_AGENT_SOURCE        install from this pip source instead of the latest commit of main
#   UAPPLY_ALLOW_ADMIN         allow running in an elevated window (not recommended)
$ErrorActionPreference = "Stop"

function Invoke-WithRetry([scriptblock]$Action, [string]$What, [int]$Attempts = 3) {
  for ($i = 1; $i -le $Attempts; $i++) {
    try { return & $Action }
    catch {
      if ($i -eq $Attempts) { throw "could not $What`: $_" }
      Start-Sleep -Seconds 3
    }
  }
}

# Elevated windows break this install: files end up owned by the administrator ("Access is denied" on
# upgrade) and elevated programs often cannot open the browser for sign-in.
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if ($me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -and -not $env:UAPPLY_ALLOW_ADMIN) {
  throw ("This PowerShell window is running as Administrator. Close it, open PowerShell normally " +
         "(not 'Run as administrator'), and run the installer again.")
}
# A source archive of the exact commit of main: Git is not needed, and the agent's self-update knows
# which version is installed.
$Sha = ""
try {
  $Sha = "$(Invoke-RestMethod -Uri https://api.github.com/repos/uapply1/uapply-agent/commits/main -Headers @{ Accept = "application/vnd.github.sha" } -TimeoutSec 10)".Trim()
} catch { $Sha = "" }
if ($Sha.Length -ne 40) { $Sha = "" }
$Archive = if ($Sha) { "$Sha.zip" } else { "refs/heads/main.zip" }
$Src = if ($env:UAPPLY_AGENT_SOURCE) { $env:UAPPLY_AGENT_SOURCE } else { "uapply-agent @ https://github.com/uapply1/uapply-agent/archive/$Archive" }
$env:UAPPLY_INSTALLED_SHA = if ($env:UAPPLY_AGENT_SOURCE) { "" } else { $Sha }

function Install-Uv {
  # The official release zip, checksum-verified, into ~\.local\bin. (astral's install.ps1 ends with
  # `exit 1` on some failures, which closes the RCIC's window under `irm | iex`.)
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
  $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "aarch64" } else { "x86_64" }
  $name = "uv-$arch-pc-windows-msvc.zip"
  $base = "https://github.com/astral-sh/uv/releases/latest/download"
  $tmp = Join-Path ([IO.Path]::GetTempPath()) ("uv-" + [guid]::NewGuid())
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  try {
    $zip = Join-Path $tmp $name
    $prev = $ProgressPreference; $ProgressPreference = "SilentlyContinue"   # PS 5.1 progress bar slows downloads 10x
    try {
      for ($i = 1; $i -le 3; $i++) {
        try { Invoke-WebRequest -Uri "$base/$name" -OutFile $zip -UseBasicParsing; break }
        catch { if ($i -eq 3) { throw "could not download uv: $_" }; Start-Sleep -Seconds 3 }
      }
      $sum = Invoke-WithRetry { Invoke-RestMethod -Uri "$base/$name.sha256" } "download the uv checksum"
      $expected = ("$sum" -split "\s+")[0].ToLower()
    } finally { $ProgressPreference = $prev }
    if ((Get-FileHash -Path $zip -Algorithm SHA256).Hash.ToLower() -ne $expected) { throw "uv download failed its checksum" }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $bin = Join-Path $env:USERPROFILE ".local\bin"
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    Get-ChildItem -Path $tmp -Recurse -Include "uv.exe", "uvx.exe", "uvw.exe" | Copy-Item -Destination $bin -Force
    if (-not (Test-Path (Join-Path $bin "uv.exe"))) { throw "uv.exe not found in $name" }
  } finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
  }
}

$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "Installing uv (Python tool manager)..."
  Install-Uv
}

# An open Claude Code / Codex session runs the uapply-agent MCP server, and Windows locks its exe.
$running = Get-Process uapply-agent -ErrorAction SilentlyContinue
if ($running) {
  Write-Warning ("uapply-agent is running in $(@($running).Count) open Claude Code / Codex session(s). It is stopped " +
                 "for the upgrade: afterwards start a NEW session (or run /mcp and reconnect uapply) to use it again.")
  $running | Stop-Process -Force
  Start-Sleep -Seconds 1
}

Write-Host "Installing uapply-agent from $Src ..."
uv tool install --force --quiet --compile-bytecode $Src
if ($LASTEXITCODE -ne 0) {
  $tools = "$env:APPDATA\uv\tools\uapply-agent"
  throw "uv tool install failed (exit $LASTEXITCODE). If it reported 'Access is denied' on $tools, close Claude Code / Codex, " +
        "delete that folder (it may have been created by an elevated install), then run this installer again in a normal window."
}
uv tool update-shell | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Warning "Could not add uv's tool folder to PATH; new terminals may not find uapply-agent." }
$agentExe = Join-Path "$(uv tool dir --bin)".Trim() "uapply-agent.exe"
if (-not (Test-Path $agentExe)) { throw "uapply-agent was not installed at $agentExe" }

$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"

& $agentExe setup
if ($LASTEXITCODE -ne 0) { throw "uapply-agent setup failed (exit $LASTEXITCODE)" }
