# uapply-agent installer for Windows. In a normal (non-administrator) PowerShell window:
#   irm https://raw.githubusercontent.com/uapply1/uapply-agent/main/install.ps1 | iex
#
# Installs uv and the uapply-agent CLI, registers it with Claude Code and Codex, and signs in to
# uApply. Run it again at any time to upgrade.
#
# Environment:
#   UAPPLY_AGENT_SOURCE        install from this pip source instead of the latest commit of main
#   UAPPLY_INSTALL_CLAUDE_CLI  also install the Claude Code CLI (for `uapply-agent run` in a
#                              terminal; Claude Code sessions run tasks without it)
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

# Local tasks run in a headless Claude Code (or Codex) CLI process on the RCIC's plan;
# the desktop apps do not provide one. Git for Windows is not required.
function Install-ClaudeCli {
  # Same source and checksum as https://claude.ai/install.ps1, but the ~250 MB binary is downloaded
  # with resume and retries, which slow or unstable connections need.
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
  $base = "https://downloads.claude.ai/claude-code-releases"
  $platform = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "win32-arm64" } else { "win32-x64" }
  $version = "$(Invoke-WithRetry { Invoke-RestMethod -Uri "$base/latest" } "look up the Claude Code version")".Trim()
  if ($version -notmatch '^\d+\.\d+\.\d+') {
    throw "downloads.claude.ai returned no version (unreachable, or Claude Code is not available in this region)"
  }
  $manifest = Invoke-WithRetry { Invoke-RestMethod -Uri "$base/$version/manifest.json" } "download the Claude Code manifest"
  $entry = $manifest.platforms.$platform
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
function Test-Runs($cmd) {
  # Installed is not enough: an incompatible build fails to start ("not compatible with the version of Windows").
  if (-not $cmd) { return $false }
  try { & $cmd --version *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}
$claudeCmd = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $claudeCmd -and (Test-Path $claudeExe)) { $claudeCmd = $claudeExe }
$codexCmd = (Get-Command codex -ErrorAction SilentlyContinue).Source
$haveRuntime = (Test-Runs $claudeCmd) -or (Test-Runs $codexCmd)
if ($claudeCmd -and -not (Test-Runs $claudeCmd)) {
  Write-Warning "Claude Code at $claudeCmd is installed but does not start on this machine; installing the native build."
}
# Claude Code needs Windows 10 1809 (build 17763) / Windows Server 2019 or later, x64 or ARM64.
$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
$build = [int]([Environment]::OSVersion.Version.Build)
$tooOld = $build -lt 17763 -or -not [Environment]::Is64BitOperatingSystem
$wantCli = [bool]$env:UAPPLY_INSTALL_CLAUDE_CLI
if ($wantCli -and -not $haveRuntime -and $tooOld) {
  Write-Warning ("This Windows ($($os.Caption), build $build) is too old for the Claude Code CLI, which needs " +
                 "Windows 10 version 1809 / Windows Server 2019 or later (64-bit). uApply installs without it.")
}
if ($wantCli -and -not $haveRuntime -and -not $tooOld) {
  Write-Host "Installing the Claude Code CLI (for uapply-agent run in a terminal)..."
  try {
    Install-ClaudeCli
  } catch {
    # Never lose the uApply install over this; setup below reports the missing CLI too.
    Write-Warning "Claude Code CLI not installed: $_"
    Write-Warning "Run this installer again later (it resumes the download), or install it with: irm https://claude.ai/install.ps1 | iex"
  }
}
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"

& $agentExe setup
if ($LASTEXITCODE -ne 0) { throw "uapply-agent setup failed (exit $LASTEXITCODE)" }
