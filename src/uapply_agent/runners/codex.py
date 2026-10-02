"""Codex headless: `codex exec` with --output-schema and image inputs.

Codex has no system-prompt flag, so the system prompt is prepended to the prompt under an
Instructions heading. Experimental: Claude Code is the supported runtime; the flags follow
`codex exec --help`.
"""
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from .base import Runner, RunnerError, RunResult, extract_json, inline_schema_refs

logger = logging.getLogger(__name__)

# Keywords OpenAI strict mode accepts; the rest (title, default, minLength, ...) are dropped and left to
# uApply's own validation of the result.
_STRICT_KEYS = {"type", "properties", "required", "items", "enum", "const", "anyOf", "description"}


class _NotStrict(ValueError):
    pass


def strict_schema(schema: dict) -> dict | None:
    """The schema in OpenAI's strict dialect, which `codex exec --output-schema` requires (it rejects
    plain Pydantic schemas); None when strict mode cannot express it (free-form objects, non-object root)."""
    try:
        out = _strict(inline_schema_refs(schema))
    except _NotStrict as e:
        logger.debug("no --output-schema for codex: %s", e)
        return None
    return out if out.get("type") == "object" else None


def _strict(node):
    if not isinstance(node, dict):
        return node
    node = dict(node)
    if "oneOf" in node:
        node["anyOf"] = node.pop("oneOf")
    if "allOf" in node:
        parts = node.pop("allOf")
        if len(parts) != 1:
            raise _NotStrict("allOf")
        node = {**parts[0], **node}
    out = {k: v for k, v in node.items() if k in _STRICT_KEYS}
    if "anyOf" in out:
        out["anyOf"] = [_strict(b) for b in out["anyOf"]]
    if "items" in out:
        if not isinstance(out["items"], dict):
            raise _NotStrict("tuple items")
        out["items"] = _strict(out["items"])
    if node.get("type") == "object" or "properties" in node:
        props = node.get("properties")
        if not props:
            raise _NotStrict("object without properties")
        required = set(node.get("required") or [])
        out["type"] = "object"
        out["properties"] = {k: _strict(v) if k in required else _nullable(_strict(v)) for k, v in props.items()}
        out["required"] = list(props)        # strict: every key required, optional ones nullable
        out["additionalProperties"] = False
    elif not ({"type", "anyOf", "enum", "const"} & out.keys()):
        raise _NotStrict("untyped value")
    return out


def _nullable(s: dict) -> dict:
    t = s.get("type")
    if isinstance(t, str) and t != "null":
        s = {**s, "type": [t, "null"]}
    elif isinstance(t, list) and "null" not in t:
        s = {**s, "type": [*t, "null"]}
    elif "anyOf" in s and not t:
        if {"type": "null"} not in s["anyOf"]:
            s = {**s, "anyOf": [*s["anyOf"], {"type": "null"}]}
        return s
    elif not t:
        return {"anyOf": [s, {"type": "null"}]}
    if "enum" in s and None not in s["enum"]:
        s["enum"] = [*s["enum"], None]
    return s


def drop_unset_nulls(value, schema):
    """Strict mode answers optional fields with null; drop those so defaults apply as in the original schema."""
    if isinstance(value, dict):
        props, required = _object_shape(schema)
        return {k: drop_unset_nulls(v, props.get(k, {})) for k, v in value.items()
                if not (v is None and k in props and k not in required)}
    if isinstance(value, list):
        items = schema.get("items") if isinstance(schema, dict) else None
        return [drop_unset_nulls(v, items if isinstance(items, dict) else {}) for v in value]
    return value


def _object_shape(schema) -> tuple[dict, set]:
    props, required = {}, set()
    if isinstance(schema, dict):
        props.update(schema.get("properties") or {})
        required |= set(schema.get("required") or [])
        for key in ("anyOf", "oneOf", "allOf"):
            for branch in schema.get(key) or []:
                p, r = _object_shape(branch)
                props.update(p)
                required |= r
    return props, required


class CodexRunner(Runner):
    name = "codex"
    binary = "codex"
    pages_per_call = 10

    def run(self, *, system_prompt, user_prompt, schema, images, cwd, timeout_s=300, text_files=()) -> RunResult:
        prompt = f"## Instructions\n{system_prompt}\n\n## Task\n{user_prompt}\n\nAnswer with JSON only."
        for tf in text_files:  # no file-read tool in exec mode: inline the text
            prompt += f"\n\n## File {Path(tf).name}\n{Path(tf).read_text(encoding='utf-8', errors='replace')}"
        flat = inline_schema_refs(schema or {})
        strict = strict_schema(flat)
        with tempfile.TemporaryDirectory() as td:
            last_msg = Path(td) / "last.txt"
            cmd = [self.binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                   "--output-last-message", str(last_msg)]
            if strict:   # otherwise the prompt asks for JSON and uApply validates the answer
                schema_path = Path(td) / "schema.json"
                schema_path.write_text(json.dumps(strict), encoding="utf-8")
                cmd += ["--output-schema", str(schema_path)]
            for img in images:
                cmd += ["--image", str(img)]
            if self.model:
                cmd += ["--model", self.model]
            # Prompt on stdin: argv is capped at ~32 KB on Windows and transcripts are longer.
            cmd.append("-")
            proc = self._exec(cmd, cwd, timeout_s, stdin=prompt)
            answer = last_msg.read_text(encoding="utf-8", errors="replace") if last_msg.exists() else ""
        if proc.returncode != 0 and not answer.strip():
            # Codex prints API errors (e.g. a rejected schema) at the end of stdout or stderr.
            err = (proc.stderr.strip() + "\n" + proc.stdout.strip()).strip()
            self._raise_if_limited(err)
            raise RunnerError(f"codex exited {proc.returncode}: {err[-300:]}")
        text = answer if answer.strip() else proc.stdout
        return RunResult(output=drop_unset_nulls(extract_json(text), flat), model=self.model or "codex-default",
                         usage={}, raw=text)
