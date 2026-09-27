"""Per-runtime headless drivers. Flags live here and nowhere else."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from .base import PlanLimited, Runner, RunnerError, RunResult
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


def detect_runtimes() -> list[dict]:
    """[{name, path}] for every runtime that can be launched from this process."""
    out = []
    for name, cls in RUNNERS.items():
        path = resolve_binary(cls.binary, _configured(name))
        if path:
            out.append({"name": name, "path": path})
    return out


def get_runner(name: str = "auto", model: str = "") -> Runner:
    found = detect_runtimes()
    if name in (None, "", "auto"):
        if not found:
            tried = ", ".join(str(c) for cls in RUNNERS.values() for c in _known_locations(cls.binary))
            raise RunnerError("no runtime found: neither `claude` nor `codex` is on this process's PATH or at "
                              f"{tried}. Install Claude Code or Codex, then run `uapply-agent setup` from a terminal "
                              "where `claude --version` works so its path is recorded.")
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


__all__ = ["Runner", "RunResult", "RunnerError", "PlanLimited", "get_runner", "detect_runtimes", "resolve_binary", "RUNNERS"]
