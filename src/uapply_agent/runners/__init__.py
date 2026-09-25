"""Per-runtime headless drivers. Flags live here and nowhere else."""
from __future__ import annotations

import shutil
from typing import Optional

from .base import PlanLimited, Runner, RunnerError, RunResult
from .claude_code import ClaudeCodeRunner
from .codex import CodexRunner

RUNNERS = {"claude-code": ClaudeCodeRunner, "codex": CodexRunner}


def detect_runtimes() -> list[str]:
    return [name for name, cls in RUNNERS.items() if shutil.which(cls.binary)]


def get_runner(name: str = "auto", model: str = "") -> Runner:
    if name in (None, "", "auto"):
        found = detect_runtimes()
        if not found:
            raise RunnerError("no runtime found: install Claude Code (`claude`) or Codex (`codex`) and log in")
        name = found[0]
    if name not in RUNNERS:
        raise RunnerError(f"unknown runtime {name!r}; choose from {list(RUNNERS)}")
    cls = RUNNERS[name]
    if not shutil.which(cls.binary):
        raise RunnerError(f"{name}: `{cls.binary}` not found on PATH")
    return cls(model=model)


__all__ = ["Runner", "RunResult", "RunnerError", "PlanLimited", "get_runner", "detect_runtimes", "RUNNERS"]
