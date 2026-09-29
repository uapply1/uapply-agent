"""Claude Code headless: `claude -p` with a real system prompt and a JSON schema.

Images are given as file paths the model reads with its Read tool; the process
runs with the cache directory as cwd so those reads need no permission prompt.
"""
from __future__ import annotations

import json
import re
import os
from pathlib import Path

from .base import Runner, RunnerError, RunResult, extract_json, inline_schema_refs, RuntimeUnavailable


NOT_SIGNED_IN = re.compile(r"not logged in|please run /login|invalid api key|oauth token (has )?expired", re.I)


class ClaudeCodeRunner(Runner):
    name = "claude-code"
    binary = "claude"
    pages_per_call = 20

    def run(self, *, system_prompt, user_prompt, schema, images, cwd, timeout_s=300, text_files=()) -> RunResult:
        prompt = user_prompt
        files = list(images) + list(text_files)
        if files:
            listing = "\n".join(f"- {p.name}" for p in files)
            prompt = (f"{user_prompt}\n\nThe file(s) to look at are in the current directory:\n{listing}\n"
                      f"Read each one with the Read tool (a long text file may need several Reads with offset/limit), "
                      f"then answer with JSON only.")
        # Not --bare: bare mode only accepts ANTHROPIC_API_KEY, so a Claude Pro/Max sign-in is "Not logged
        # in". The isolation it gave comes from explicit flags: no MCP servers (else each task would start
        # the uapply server again), no skills/plugins, no user/project settings or hooks, Read only.
        cmd = [
            self.binary, "-p", prompt,
            "--no-session-persistence",
            "--output-format", "json",
            "--json-schema", json.dumps(inline_schema_refs(schema)),
            "--system-prompt", system_prompt,
            "--tools", "Read",
            "--allowedTools", "Read",
            "--permission-mode", "dontAsk",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--setting-sources", "",
        ]
        if self.model:
            cmd += ["--model", self.model]
        # A nested Claude Code session would otherwise inherit the parent's env.
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
        env.update({"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
                    "UAPPLY_NO_UPDATE": "1"})
        proc = self._exec_env(cmd, cwd, timeout_s, env)
        raw = proc.stdout.strip()
        if proc.returncode != 0 and not raw:
            self._raise_if_limited(proc.stderr)
            raise RunnerError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:300]}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._raise_if_limited(raw)
            raise RunnerError(f"claude returned non-JSON: {raw[:200]!r}")
        if data.get("is_error"):
            msg = str(data.get("result", ""))
            self._raise_if_limited(msg)
            if NOT_SIGNED_IN.search(msg):
                raise RuntimeUnavailable(f"the Claude Code CLI is not signed in ({msg.strip()[:120]}): run "
                                         "`claude auth login` in a terminal, then run the tasks again")
            raise RunnerError(f"claude error: {msg[:300]}")
        output = data.get("structured_output")
        if not isinstance(output, dict):
            output = extract_json(str(data.get("result", "")))
        usage = data.get("usage") or {}
        model = next(iter((data.get("modelUsage") or {}).keys()), "") or self.model
        return RunResult(output=output, model=model,
                         usage={"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                                "duration_s": (data.get("duration_ms") or 0) / 1000.0,
                                "cost_usd": data.get("total_cost_usd")},
                         raw=raw)

    def _exec_env(self, cmd, cwd, timeout_s, env):
        import subprocess
        try:
            # stdin closed: `claude -p` otherwise waits 3 s on a non-TTY stdin and reads whatever arrives,
            # which under the MCP server is the server's own protocol pipe.
            return subprocess.run(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout_s, env=env)
        except FileNotFoundError as e:
            raise RunnerError(f"claude not found: {e}")
        except subprocess.TimeoutExpired:
            raise RunnerError(f"claude timed out after {timeout_s}s")
