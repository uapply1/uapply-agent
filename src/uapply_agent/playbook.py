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


TASK_RUNNER_AGENT = """---
name: task-runner
description: Runs one uApply task from a brief file. Only for /uapply:run and /uapply:intake-from-chat.
tools: Read, mcp__uapply__submit_task, mcp__uapply__release_task, mcp__uapply__submit_intake
permissionMode: dontAsk
omitClaudeMd: true
maxTurns: 40
background: true
---

You run one uApply task. The prompt names a brief file (`task.md`). Read the whole brief first,
then every file it lists, in the order listed; pages of a document are numbered in that order.
Follow the brief's instructions exactly and produce the JSON object its schema describes.

Submit it with the tool the brief names (`submit_task`, or `submit_intake` for a chat intake).
If the tool answers `accepted: false` with feedback, fix the answer and submit once more.
If the task cannot be done (unreadable files, missing input), call `release_task` with the reason.

Never write or edit files. Never quote the documents in your reply. Your final message is one line:
the task id followed by accepted, rejected or released.
"""


def plugin_files(version: str) -> dict[str, str]:
    """A Claude Code plugin generated from the playbook: the `/uapply:<prompt>` commands and the
    `uapply:task-runner` subagent that runs tasks inside the RCIC's own session."""
    out = {".claude-plugin/plugin.json": json.dumps({
        "name": "uapply", "version": version,
        "description": "uApply case commands and task runner; requires the `uapply` MCP server (uapply-agent setup)",
        "author": {"name": "uApply", "email": "contact@uapply.io"},
    }, indent=2)}
    for name, description in PROMPTS.items():
        out[f"commands/{name}.md"] = f"---\ndescription: {description}\n---\n\n{prompt(name)}\n"
    out["agents/task-runner.md"] = TASK_RUNNER_AGENT
    return out
