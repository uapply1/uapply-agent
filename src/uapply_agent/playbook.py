"""The playbook (SOURCE.md, shipped in the package): server instructions and the /uapply:* prompts.

`## instructions` becomes the MCP server instructions; each `## prompt: <name>` section becomes a
prompt, served by the MCP server and written into the Claude Code plugin by `setup`.
"""
from __future__ import annotations

import json
import re
from functools import cache
from importlib.resources import files

PROMPTS = {
    "run": "Run the uApply case in this folder end to end (upload, local tasks, analysis, forms, report)",
    "status": "Summarise the uApply case status",
    "intake-from-chat": "Set up a uApply case from the client's chat history (AnyChat)",
}


@cache
def _sections() -> dict[str, str]:
    text = files(__package__).joinpath("SOURCE.md").read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if m := re.match(r"^## (.+)$", line):
            current = m.group(1).strip()
            out[current] = []
        elif current:
            out[current].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def instructions() -> str:
    return _sections()["instructions"]


def prompt(name: str) -> str:
    return _sections()[f"prompt: {name}"]


def plugin_files(version: str) -> dict[str, str]:
    """A Claude Code plugin (`/uapply:<prompt>`) generated from the same prompts the MCP server serves."""
    out = {".claude-plugin/plugin.json": json.dumps({
        "name": "uapply", "version": version,
        "description": "uApply case commands; requires the `uapply` MCP server (uapply-agent setup)",
    }, indent=2)}
    for name, description in PROMPTS.items():
        out[f"commands/{name}.md"] = f"---\ndescription: {description}\n---\n\n{prompt(name)}\n"
    return out
