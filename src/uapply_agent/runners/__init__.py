"""Per-runtime headless drivers. Flags live here and nowhere else."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .base import PlanLimited, Runner, RunnerError, RunResult, RuntimeUnavailable
from .claude_code import ClaudeCodeRunner
from .codex import CodexRunner

RUNNERS = {"claude-code": ClaudeCodeRunner, "codex": CodexRunner}
_WIN = sys.platform.startswith("win")


def known_locations(binary: str) -> list[Path]:
    """Where the installers put the CLIs, for when they are not on PATH."""
    home = Path.home()
    exts = [".exe", ".cmd", ""] if _WIN else [""]
    cands = [home / ".local" / "bin" / f"{binary}{e}" for e in exts]
    if binary == "claude":
        cands.append(home / ".claude" / "local" / "claude")
    if _WIN and os.environ.get("APPDATA"):
        cands.append(Path(os.environ["APPDATA"]) / "npm" / f"{binary}.cmd")
    return cands


def resolve_binary(binary: str, configured: str = "") -> str | None:
    """Configured path → PATH → known install locations. The MCP server often runs under a
    desktop app whose PATH lacks ~/.local/bin, so PATH alone is not enough."""
    if configured:
        return configured if Path(configured).exists() else None
    found = shutil.which(binary)
    if found:
        return found
    for c in known_locations(binary):
        if c.exists():
            return str(c)
    return None


def configured_paths(settings) -> dict[str, str]:
    """Runtime paths recorded by `setup` (absolute: desktop apps start the server with a minimal PATH)."""
    if settings is None:
        return {}
    return {"claude-code": settings.claude_bin, "codex": settings.codex_bin}


_WORKING: set[str] = set()


def runtime_error(path: str) -> str:
    """'' when `<path> --version` runs; otherwise why not (e.g. a build this Windows cannot start).
    Only successes are cached, so a runtime reinstalled while the server runs is picked up."""
    if path in _WORKING:
        return ""
    try:
        r = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)[:300]
    if r.returncode == 0:
        _WORKING.add(path)
        return ""
    return ((r.stderr or r.stdout or "").strip().splitlines() or [f"exit code {r.returncode}"])[0][:300]


def detect_runtimes(settings=None, check: bool = True) -> list[dict]:
    """[{name, path}] for every runtime that is installed; one that does not start carries `error`
    instead of being silently used."""
    configured = configured_paths(settings)
    out = []
    for name, cls in RUNNERS.items():
        path = resolve_binary(cls.binary, configured.get(name, ""))
        if path:
            row = {"name": name, "path": path}
            if check and (err := runtime_error(path)):
                row["error"] = err
            out.append(row)
    return out


def get_runner(name: str = "auto", model: str = "", settings=None) -> Runner:
    installed = detect_runtimes(settings)
    found = [f for f in installed if not f.get("error")]
    if name in (None, "", "auto"):
        if not found and installed:
            b = installed[0]
            raise RunnerError(f"{b['name']} is installed at {b['path']} but does not start on this machine: "
                              f"{b['error']}. "
                              "Reinstall it with the uApply installer; if Windows reports it is not compatible, this "
                              "Windows version is too old for it.")
        if not found:
            tried = ", ".join(str(c) for cls in RUNNERS.values() for c in known_locations(cls.binary))
            install = ("irm https://claude.ai/install.ps1 | iex" if _WIN
                       else "curl -fsSL https://claude.ai/install.sh | bash")
            raise RunnerError("no runtime found: headless tasks need the Claude Code CLI (or Codex CLI); the desktop "
                              f"app does not provide one. Looked on PATH and at {tried}. Install it with `{install}`, "
                              "sign in with `claude auth login`, then run `uapply-agent setup`. In a Claude Code "
                              "session no CLI is needed: /uapply:run runs the tasks as subagents.")
        name = found[0]["name"]
    if name not in RUNNERS:
        raise RunnerError(f"unknown runtime {name!r}; choose from {list(RUNNERS)}")
    match = next((f for f in found if f["name"] == name), None)
    if not match:
        raise RunnerError(f"{name}: `{RUNNERS[name].binary}` not found; run `uapply-agent setup` from a terminal "
                          "where it works, or set claude_bin / codex_bin in the config")
    runner = RUNNERS[name](model=model)
    runner.binary = match["path"]
    return runner


__all__ = ["RUNNERS", "PlanLimited", "RunResult", "Runner", "RunnerError", "RuntimeUnavailable",
           "configured_paths", "detect_runtimes", "get_runner", "known_locations", "resolve_binary", "runtime_error"]
