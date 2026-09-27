"""Load sections of playbook/SOURCE.md (bundled copy) for instructions and prompts."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_BUNDLED = Path(__file__).parent / "SOURCE.md"
_REPO = Path(__file__).resolve().parents[2] / "playbook" / "SOURCE.md"


@lru_cache(maxsize=1)
def _sections() -> dict[str, str]:
    src = _REPO if _REPO.exists() else _BUNDLED
    text = src.read_text() if src.exists() else ""
    out: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^## (.+)$", line)
        if m:
            current = m.group(1).strip()
            out[current] = ""
        elif current:
            out[current] += line + "\n"
    return {k: v.strip() for k, v in out.items()}


def instructions() -> str:
    return _sections().get("instructions", "")


def prompt(name: str) -> str:
    return _sections().get(f"prompt: {name}", "")


PROMPT_DESCRIPTIONS = {
    "run": "Run the uApply case in this folder end to end (upload, local tasks, wait)",
    "status": "Summarise the uApply case status",
    "intake-from-chat": "Set up a uApply case from the client's chat history (AnyChat)",
}


def prompt_names() -> list[str]:
    return [k[len("prompt: "):] for k in _sections() if k.startswith("prompt: ")]


def plugin_files(version: str) -> dict[str, str]:
    """A Claude Code plugin (`/uapply:<prompt>`) generated from the same prompts the MCP server serves."""
    import json
    files = {".claude-plugin/plugin.json": json.dumps({
        "name": "uapply", "version": version,
        "description": "uApply case commands; requires the `uapply` MCP server (uapply-agent setup)",
    }, indent=2)}
    for name in prompt_names():
        desc = PROMPT_DESCRIPTIONS.get(name, f"uApply: {name}")
        files[f"commands/{name}.md"] = f"---\ndescription: {desc}\n---\n\n{prompt(name)}\n"
    return files
