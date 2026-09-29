from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

LIMIT_PATTERNS = re.compile(
    r"\b(usage limit|rate limit|limit reached|too many requests|quota exceeded|exceeded your .*quota|"
    r"out of credits|HTTP 429|status 429)\b", re.I)


class RunnerError(RuntimeError):
    pass


class PlanLimited(RunnerError):
    """The subscription's usage window is exhausted; stop pulling work."""


class RuntimeUnavailable(RunnerError):
    """The runtime cannot work at all (e.g. not signed in): stop the run instead of failing every task."""


@dataclass
class RunResult:
    output: dict
    model: str = ""
    usage: dict = field(default_factory=dict)
    raw: str = ""


def inline_schema_refs(schema: dict) -> dict:
    """Resolve local `$ref`s into place and drop `$defs` and `$schema`, for runtimes that only take flat
    schemas. Claude Code checks schemas with a draft-07 Ajv, which rejects a 2020-12 `$schema`."""
    defs = schema.get("$defs") or {}

    def walk(node, depth=0):
        if depth > 30:
            return node
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = defs.get(ref.split("/")[-1], {})
                merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
                return walk(merged, depth + 1)
            return {k: walk(v, depth + 1) for k, v in node.items() if k not in ("$defs", "$schema")}
        if isinstance(node, list):
            return [walk(v, depth + 1) for v in node]
        return node

    return walk(schema)


def extract_json(text: str) -> dict:
    """Parse the model's answer: bare JSON, fenced JSON, or JSON embedded in prose."""
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.splitlines() if not l.strip().startswith("```"))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        return json.loads(m.group(0))
    raise RunnerError(f"no JSON object in runtime output: {text[:200]!r}")


class Runner:
    name = "base"
    binary = ""
    pages_per_call = 20    # how many pages one headless call may look at

    def __init__(self, model: str = ""):
        self.model = model

    def run(self, *, system_prompt: str, user_prompt: str, schema: dict,
            images: list[Path], cwd: Path, timeout_s: int = 300,
            text_files: list[Path] = ()) -> RunResult:
        """`text_files` are long inputs the model reads from disk (argv is limited on Windows)."""
        raise NotImplementedError

    @staticmethod
    def _exec(cmd: list[str], cwd: Path, timeout_s: int, stdin: Optional[str] = None,
              env: Optional[dict] = None) -> subprocess.CompletedProcess:
        """Run the CLI. `stdin` is sent on a pipe of our own: never inherit the MCP server's stdin,
        which is the protocol channel."""
        feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        try:
            return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout_s, env=env, check=False, **feed)
        except FileNotFoundError as e:
            raise RunnerError(f"{cmd[0]} not found: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise RunnerError(f"{Path(cmd[0]).name} timed out after {timeout_s}s") from e

    @staticmethod
    def _raise_if_limited(text: str) -> None:
        if LIMIT_PATTERNS.search(text or ""):
            raise PlanLimited(text.strip()[:300])
