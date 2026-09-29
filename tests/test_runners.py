import json
import subprocess
from pathlib import Path

import pytest

from uapply_agent.runners.base import PlanLimited, RunnerError, extract_json
from uapply_agent.runners.claude_code import ClaudeCodeRunner


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"file_types": ["Visa"]} done') == {"file_types": ["Visa"]}
    with pytest.raises(RunnerError):
        extract_json("no json here")


def _completed(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


def test_claude_runner_parses_structured_output(monkeypatch, tmp_path):
    envelope = {"type": "result", "is_error": False, "result": "{\"file_types\": [\"Passport\"]}",
                "structured_output": {"file_types": ["Passport"]}, "duration_ms": 1200, "total_cost_usd": 0.01,
                "usage": {"input_tokens": 500, "output_tokens": 12}, "modelUsage": {"claude-fable-5-1": {}}}
    seen = {}

    def fake_exec(cmd, cwd, timeout_s, stdin=None, env=None):
        seen["cmd"] = cmd
        seen["env"] = env
        seen["prompt"] = stdin
        return _completed(stdout=json.dumps(envelope))

    monkeypatch.setattr(ClaudeCodeRunner, "_exec", staticmethod(fake_exec))
    img = tmp_path / "page.jpg"
    img.write_bytes(b"x")
    rr = ClaudeCodeRunner(model="haiku").run(system_prompt="SYS", user_prompt="classify",
                                             schema={"type": "object"}, images=[img], cwd=tmp_path)
    assert rr.output == {"file_types": ["Passport"]}
    assert rr.model == "claude-fable-5-1"
    assert rr.usage["input_tokens"] == 500 and rr.usage["duration_s"] == 1.2
    cmd = seen["cmd"]
    assert cmd[:2] == ["claude", "-p"] and not any("page.jpg" in c for c in cmd)
    assert "page.jpg" in seen["prompt"] and "--system-prompt=SYS" in cmd
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert "--json-schema" in cmd
    # --bare would lock out Claude Pro/Max sign-ins (API key only); isolation comes from explicit flags
    assert "--bare" not in cmd
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--disable-slash-commands" in cmd and cmd[cmd.index("--setting-sources") + 1] == ""
    assert cmd[cmd.index("--tools") + 1] == "Read"
    assert "CLAUDECODE" not in seen["env"]
    assert seen["env"]["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1" and seen["env"]["UAPPLY_NO_UPDATE"] == "1"


def test_claude_runner_falls_back_to_result_text(monkeypatch, tmp_path):
    envelope = {"is_error": False, "result": "```json\n{\"file_types\": [\"Visa\"]}\n```", "usage": {}}
    monkeypatch.setattr(ClaudeCodeRunner, "_exec", lambda *a, **k: _completed(stdout=json.dumps(envelope)))
    rr = ClaudeCodeRunner().run(system_prompt="s", user_prompt="u", schema={}, images=[], cwd=tmp_path)
    assert rr.output == {"file_types": ["Visa"]}


def test_claude_runner_detects_plan_limit(monkeypatch, tmp_path):
    envelope = {"is_error": True, "result": "You've hit your usage limit. Try again at 3pm."}
    monkeypatch.setattr(ClaudeCodeRunner, "_exec", lambda *a, **k: _completed(stdout=json.dumps(envelope)))
    with pytest.raises(PlanLimited):
        ClaudeCodeRunner().run(system_prompt="s", user_prompt="u", schema={}, images=[], cwd=tmp_path)


def test_claude_runner_reports_other_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(ClaudeCodeRunner, "_exec", lambda *a, **k: _completed(stderr="boom", code=1))
    with pytest.raises(RunnerError):
        ClaudeCodeRunner().run(system_prompt="s", user_prompt="u", schema={}, images=[], cwd=tmp_path)




def test_resolve_binary_order(tmp_path, monkeypatch):
    from uapply_agent import runners as r
    monkeypatch.setattr(r.shutil, "which", lambda _: None)
    monkeypatch.setattr(r.Path, "home", classmethod(lambda cls: tmp_path))
    assert r.resolve_binary("claude") is None
    local = tmp_path / ".local" / "bin" / "claude"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    assert r.resolve_binary("claude") == str(local)           # known location, not on PATH
    conf = tmp_path / "elsewhere" / "claude"
    conf.parent.mkdir()
    conf.write_text("")
    assert r.resolve_binary("claude", str(conf)) == str(conf)  # configured path wins
    assert r.resolve_binary("claude", str(tmp_path / "gone")) is None


def test_get_runner_uses_resolved_path_and_explains_when_missing(tmp_path, monkeypatch):
    from uapply_agent import runners as r
    monkeypatch.setattr(r, "detect_runtimes", lambda settings=None: [{"name": "claude-code", "path": "/opt/claude"}])
    assert r.get_runner("auto").binary == "/opt/claude"
    monkeypatch.setattr(r, "detect_runtimes", lambda settings=None: [{"name": "claude-code", "path": "/opt/claude",
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


def test_claude_prompts_starting_with_a_dash_are_not_options(tmp_path, monkeypatch):
    """The prompt goes on a stdin pipe of its own (never the MCP protocol pipe), not argv."""
    import subprocess as sp
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw, cmd=cmd)
        return _completed(stdout=json.dumps({"type": "result", "is_error": False, "structured_output": {"a": 1},
                                             "result": "{}", "usage": {}}))
    monkeypatch.setattr(sp, "run", fake_run)
    ClaudeCodeRunner().run(system_prompt="- rule", user_prompt="- Family Name in English: LI", schema={},
                           images=[], cwd=tmp_path)
    assert seen["input"] == "- Family Name in English: LI" and "stdin" not in seen and seen["encoding"] == "utf-8"
    assert "--system-prompt=- rule" in seen["cmd"] and not any(a.startswith("- ") for a in seen["cmd"])


def test_not_signed_in_stops_as_runtime_unavailable(tmp_path, monkeypatch):
    from uapply_agent.runners import RuntimeUnavailable
    envelope = {"type": "result", "is_error": True, "result": "Not logged in · Please run /login", "usage": {}}
    monkeypatch.setattr(ClaudeCodeRunner, "_exec", lambda *a, **k: _completed(stdout=json.dumps(envelope)))
    with pytest.raises(RuntimeUnavailable, match="claude auth login"):
        ClaudeCodeRunner().run(system_prompt="s", user_prompt="u", schema={}, images=[], cwd=tmp_path)


def test_schema_drops_2020_12_meta_schema_for_claude(monkeypatch, tmp_path):
    """Claude Code's draft-07 Ajv rejects a 2020-12 `$schema` ("--json-schema is not a valid JSON Schema")."""
    seen = {}
    envelope = {"type": "result", "is_error": False, "structured_output": {"a": 1}, "result": "{}", "usage": {}}

    def fake_exec(cmd, cwd, timeout_s, stdin=None, env=None):
        seen["schema"] = json.loads(cmd[cmd.index("--json-schema") + 1])
        return _completed(stdout=json.dumps(envelope))
    monkeypatch.setattr(ClaudeCodeRunner, "_exec", staticmethod(fake_exec))
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
              "properties": {"langs": {"type": "array", "items": {"$ref": "#/$defs/L"}}}, "$defs": {"L": {"type": "string"}}}
    ClaudeCodeRunner().run(system_prompt="s", user_prompt="u", schema=schema, images=[], cwd=tmp_path)
    assert seen["schema"] == {"type": "object", "properties": {"langs": {"type": "array", "items": {"type": "string"}}}}


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


def test_reported_model_is_the_one_that_answered():
    from uapply_agent.runners.claude_code import _main_model
    usage = {"claude-haiku-4-5": {"outputTokens": 12}, "claude-fable-5-1": {"outputTokens": 900}}
    assert _main_model(usage) == "claude-fable-5-1" and _main_model({}) == ""


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
