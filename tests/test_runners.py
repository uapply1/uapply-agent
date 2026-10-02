import json
import subprocess
from pathlib import Path

import pytest

from uapply_agent.runners.base import RunnerError, extract_json


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"file_types": ["Visa"]} done') == {"file_types": ["Visa"]}
    with pytest.raises(RunnerError):
        extract_json("no json here")


def _completed(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


def test_resolve_binary_order(tmp_path, monkeypatch):
    from uapply_agent import runners as r
    monkeypatch.setattr(r.shutil, "which", lambda _: None)
    monkeypatch.setattr(r.Path, "home", classmethod(lambda cls: tmp_path))
    assert r.resolve_binary("codex") is None
    local = tmp_path / ".local" / "bin" / "codex"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    assert r.resolve_binary("codex") == str(local)           # known location, not on PATH
    conf = tmp_path / "elsewhere" / "codex"
    conf.parent.mkdir()
    conf.write_text("")
    assert r.resolve_binary("codex", str(conf)) == str(conf)  # configured path wins
    assert r.resolve_binary("codex", str(tmp_path / "gone")) is None


def test_get_runner_uses_resolved_path_and_explains_when_missing(tmp_path, monkeypatch):
    from uapply_agent import runners as r
    monkeypatch.setattr(r, "detect_runtimes", lambda settings=None: [{"name": "codex", "path": "/opt/codex"}])
    assert r.get_runner("auto").binary == "/opt/codex"
    monkeypatch.setattr(r, "detect_runtimes", lambda settings=None: [{"name": "codex", "path": "/opt/codex",
                                                        "error": "This version is not compatible with the version of Windows"}])
    with pytest.raises(r.RunnerError, match="does not start on this machine"):
        r.get_runner("auto")
    monkeypatch.setattr(r, "detect_runtimes", lambda settings=None: [])
    with pytest.raises(r.RunnerError, match="uapply-agent setup"):
        r.get_runner("auto")


def test_runtime_error_reports_a_binary_that_does_not_start(tmp_path):
    from uapply_agent import runners as r
    good = tmp_path / "good"
    good.write_text("#!/bin/sh\necho 2.1.0\n")
    good.chmod(0o755)
    bad = tmp_path / "bad"
    bad.write_text("#!/bin/sh\necho 'not compatible with this Windows' >&2; exit 216\n")
    bad.chmod(0o755)
    assert r.runtime_error(str(good)) == ""
    assert "not compatible" in r.runtime_error(str(bad))
    assert r.runtime_error(str(tmp_path / "missing"))


def test_limit_patterns_match_real_messages_only():
    from uapply_agent.runners.base import LIMIT_PATTERNS
    for msg in ("Claude AI usage limit reached|1760000000", "You've hit your usage limit.",
                "Error: 429 Too Many Requests", "You exceeded your current quota, please check your plan",
                "rate limit exceeded"):
        assert LIMIT_PATTERNS.search(msg), msg
    for msg in ("retrying request 1/3 after 1s", "section quotas_table parsed", "invoice 4291 loaded"):
        assert not LIMIT_PATTERNS.search(msg), msg


def test_codex_success_with_noisy_stderr_is_not_a_plan_limit(tmp_path, monkeypatch):
    from uapply_agent.runners.codex import CodexRunner

    def fake_exec(cmd, cwd, timeout_s, stdin=None):
        out = cmd[cmd.index("--output-last-message") + 1]
        Path(out).write_text('{"file_types": ["Passport"]}', encoding="utf-8")
        return _completed(stderr="warning: rate limit headroom low, request 2 retried")
    monkeypatch.setattr(CodexRunner, "_exec", staticmethod(fake_exec))
    rr = CodexRunner().run(system_prompt="s", user_prompt="u", schema={}, images=[], cwd=tmp_path)
    assert rr.output == {"file_types": ["Passport"]}


def test_schema_refs_are_inlined_for_the_runtime():
    from uapply_agent.chat.intake import IntakeHints
    from uapply_agent.runners.base import inline_schema_refs
    flat = inline_schema_refs(IntakeHints.model_json_schema())
    assert "$defs" not in flat and "$ref" not in json.dumps(flat)
    assert flat["properties"]["applicant"]["properties"]["native_name"]["anyOf"][0]["type"] == "string"
    assert flat["properties"]["family"]["items"]["properties"]["relationship"]["type"] == "string"


def test_codex_runner_sends_prompt_on_stdin(monkeypatch, tmp_path):
    import subprocess

    from uapply_agent.runners.codex import CodexRunner
    seen = {}

    def fake_exec(self, cmd, cwd, timeout_s, stdin=None):
        seen["cmd"], seen["stdin"] = cmd, stdin
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='{"file_types": ["Visa"]}', stderr="")

    monkeypatch.setattr(CodexRunner, "_exec", fake_exec)
    big = tmp_path / "big.md"
    big.write_text("x" * 100_000)
    rr = CodexRunner().run(system_prompt="S", user_prompt="U", schema={"type": "object"}, images=[], cwd=tmp_path, text_files=[big])
    assert rr.output == {"file_types": ["Visa"]}
    assert seen["cmd"][-1] == "-" and "x" * 100_000 in seen["stdin"] and "## Instructions" in seen["stdin"]
    assert all(len(a) < 1000 for a in seen["cmd"])


def test_codex_gets_a_strict_schema_and_optional_nulls_are_dropped(monkeypatch, tmp_path):
    import subprocess

    from pydantic import BaseModel, Field

    from uapply_agent.runners.codex import CodexRunner

    class Page(BaseModel):
        n: int = Field(ge=1)
        text: str
        note: str = ""

    class Pages(BaseModel):
        pages: list[Page] = Field(min_length=1)
        language: str | None = None

    seen = {}

    def fake_exec(self, cmd, cwd, timeout_s, stdin=None):
        seen["schema"] = json.loads(Path(cmd[cmd.index("--output-schema") + 1]).read_text())
        out = cmd[cmd.index("--output-last-message") + 1]
        Path(out).write_text('{"pages": [{"n": 1, "text": "hi", "note": null}], "language": null}')
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(CodexRunner, "_exec", fake_exec)
    rr = CodexRunner().run(system_prompt="S", user_prompt="U", schema=Pages.model_json_schema(), images=[], cwd=tmp_path)
    s = seen["schema"]
    page = s["properties"]["pages"]["items"]
    assert s["additionalProperties"] is False and s["required"] == ["pages", "language"]
    assert page["additionalProperties"] is False and page["required"] == ["n", "text", "note"]
    assert page["properties"]["note"]["type"] == ["string", "null"]
    assert "minimum" not in json.dumps(s) and "title" not in json.dumps(s) and "$defs" not in s
    assert rr.output == {"pages": [{"n": 1, "text": "hi"}]}
    Pages.model_validate(rr.output)


def test_codex_without_strict_schema_and_error_from_stdout(monkeypatch, tmp_path):
    import subprocess

    from uapply_agent.runners.base import RunnerError
    from uapply_agent.runners.codex import CodexRunner, strict_schema
    assert strict_schema({"type": "object", "additionalProperties": {"type": "string"}}) is None
    seen = {}

    def fake_exec(self, cmd, cwd, timeout_s, stdin=None):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="ERROR: Invalid schema for response_format",
                                           stderr="")

    monkeypatch.setattr(CodexRunner, "_exec", fake_exec)
    with pytest.raises(RunnerError, match="Invalid schema"):
        CodexRunner().run(system_prompt="S", user_prompt="U", schema={"type": "object"}, images=[], cwd=tmp_path)
    assert "--output-schema" not in seen["cmd"]
