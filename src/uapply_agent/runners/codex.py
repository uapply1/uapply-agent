"""Codex headless: `codex exec` with --output-schema and image inputs.

Codex has no system-prompt flag, so the system prompt is prepended to the
prompt under an Instructions heading. Untested against a live Codex install;
the flag set follows the current `codex exec --help`.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .base import Runner, RunnerError, RunResult, extract_json, inline_schema_refs


class CodexRunner(Runner):
    name = "codex"
    binary = "codex"
    supports_pdf = False   # images only; the executor renders PDF pages
    pages_per_call = 10

    def run(self, *, system_prompt, user_prompt, schema, images, cwd, timeout_s=300, text_files=()) -> RunResult:
        prompt = f"## Instructions\n{system_prompt}\n\n## Task\n{user_prompt}\n\nAnswer with JSON only."
        for tf in text_files:  # no file-read tool in exec mode: inline the text
            prompt += f"\n\n## File {Path(tf).name}\n{Path(tf).read_text(encoding='utf-8', errors='replace')}"
        with tempfile.TemporaryDirectory() as td:
            schema_path = Path(td) / "schema.json"
            schema_path.write_text(json.dumps(inline_schema_refs(schema)), encoding="utf-8")
            last_msg = Path(td) / "last.txt"
            cmd = [self.binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                   "--output-schema", str(schema_path), "--output-last-message", str(last_msg)]
            for img in images:
                cmd += ["--image", str(img)]
            if self.model:
                cmd += ["--model", self.model]
            # Prompt on stdin: argv is capped at ~32 KB on Windows and transcripts are longer.
            cmd.append("-")
            proc = self._exec(cmd, cwd, timeout_s, stdin=prompt)
            text = last_msg.read_text(encoding="utf-8", errors="replace") if last_msg.exists() else proc.stdout
        if proc.returncode != 0 and not text.strip():
            self._raise_if_limited(proc.stderr)
            raise RunnerError(f"codex exited {proc.returncode}: {proc.stderr.strip()[:300]}")
        self._raise_if_limited(proc.stderr)
        return RunResult(output=extract_json(text), model=self.model or "codex-default", usage={}, raw=text)
