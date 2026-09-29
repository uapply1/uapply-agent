"""Per-runtime headless drivers. Flags live here and nowhere else."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .base import PlanLimited, Runner, RunnerError, RunResult, RuntimeUnavailable
from .claude_code import ClaudeCodeRunner
from .codex import CodexRunner

RUNNERS = {"claude-code": ClaudeCodeRunner, "codex": CodexRunner}
_WIN = sys.platform.startswith("win")


def _known_locations(binary: str) -> list[Path]:
    home = Path.home()
    exts = [".exe", ".cmd", ""] if _WIN else [""]
    cands = [home / ".local" / "bin" / f"{binary}{e}" for e in exts]
    if binary == "claude":
        cands.append(home / ".claude" / "local" / "claude")
    if _WIN and os.environ.get("APPDATA"):
        cands.append(Path(os.environ["APPDATA"]) / "npm" / f"{binary}.cmd")
    return cands


def resolve_binary(binary: str, configured: str = "") -> Optional[str]:
    """Configured path → PATH → known install locations. The MCP server often runs under a
    desktop app whose PATH lacks ~/.local/bin, so PATH alone is not enough."""
    if configured:
        return configured if Path(configured).exists() else None
    found = shutil.which(binary)
    if found:
        return found
    for c in _known_locations(binary):
        if c.exists():
            return str(c)
    return None


def _configured(name: str) -> str:
    try:
        from ..config import Settings
        s = Settings.load()
    except Exception:
        return ""
    return {"claude-code": s.claude_bin, "codex": s.codex_bin}.get(name, "")


@lru_cache(maxsize=16)
def runtime_error(path: str) -> str:
    """'' when `<path> --version` runs; otherwise why not (e.g. a build this Windows cannot start)."""
    try:
        r = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)[:300]
    if r.returncode == 0:
        return ""
    return ((r.stderr or r.stdout or "").strip().splitlines() or [f"exit code {r.returncode}"])[0][:300]


def detect_runtimes(check: bool = True) -> list[dict]:
    """[{name, path}] for every runtime that is installed and actually starts; a broken install
    carries `error` instead of being silently used."""
    out = []
    for name, cls in RUNNERS.items():
        path = resolve_binary(cls.binary, _configured(name))
        if path:
            row = {"name": name, "path": path}
            if check and (err := runtime_error(path)):
                row["error"] = err
            out.append(row)
    return out


def get_runner(name: str = "auto", model: str = "") -> Runner:
    installed = detect_runtimes()
    found = [f for f in installed if not f.get("error")]
    if name in (None, "", "auto"):
        if not found and installed:
            b = installed[0]
            raise RunnerError(f"{b['name']} is installed at {b['path']} but does not start on this machine: {b['error']}. "
                              "Reinstall it with the uApply installer; if Windows reports it is not compatible, this "
                              "Windows version is too old for it.")
        if not found:
            tried = ", ".join(str(c) for cls in RUNNERS.values() for c in _known_locations(cls.binary))
            install = ("irm https://claude.ai/install.ps1 | iex" if _WIN
                       else "curl -fsSL https://claude.ai/install.sh | bash")
            raise RunnerError("no runtime found: local tasks need the Claude Code CLI (or Codex CLI); the desktop app "
                              f"does not provide one. Looked on PATH and at {tried}. Rerun the uApply installer (it "
                              f"installs the CLI), or install it with `{install}`, sign in with `claude auth login`, "
                              "then run `uapply-agent setup`.")
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


__all__ = ["Runner", "RunResult", "RunnerError", "PlanLimited", "RuntimeUnavailable", "get_runner", "detect_runtimes", "resolve_binary", "runtime_error", "RUNNERS"]
