"""Codex headless: `codex exec` with --output-schema and image inputs.

Codex has no system-prompt flag, so the system prompt is prepended to the
prompt under an Instructions heading. Untested against a live Codex install;
the flag set follows the current `codex exec --help`.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .base import Runner, RunnerError, RunResult, extract_json


class CodexRunner(Runner):
    name = "codex"
    binary = "codex"

    def run(self, *, system_prompt, user_prompt, schema, images, cwd, timeout_s=300) -> RunResult:
        prompt = f"## Instructions\n{system_prompt}\n\n## Task\n{user_prompt}\n\nAnswer with JSON only."
        with tempfile.TemporaryDirectory() as td:
            schema_path = Path(td) / "schema.json"
            schema_path.write_text(json.dumps(schema))
            last_msg = Path(td) / "last.txt"
            cmd = [self.binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                   "--output-schema", str(schema_path), "--output-last-message", str(last_msg)]
            for img in images:
                cmd += ["--image", str(img)]
            if self.model:
                cmd += ["--model", self.model]
            cmd.append(prompt)
            proc = self._exec(cmd, cwd, timeout_s)
            text = last_msg.read_text() if last_msg.exists() else proc.stdout
        if proc.returncode != 0 and not text.strip():
            self._raise_if_limited(proc.stderr)
            raise RunnerError(f"codex exited {proc.returncode}: {proc.stderr.strip()[:300]}")
        self._raise_if_limited(proc.stderr)
        return RunResult(output=extract_json(text), model=self.model or "codex-default", usage={}, raw=text)
