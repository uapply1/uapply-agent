import json
import subprocess
from pathlib import Path

import pytest

from uapply_agent import playbook
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

    def fake_exec(self, cmd, cwd, timeout_s, env):
        seen["cmd"] = cmd
        seen["env"] = env
        return _completed(stdout=json.dumps(envelope))

    monkeypatch.setattr(ClaudeCodeRunner, "_exec_env", fake_exec)
    img = tmp_path / "page.jpg"
    img.write_bytes(b"x")
    rr = ClaudeCodeRunner(model="haiku").run(system_prompt="SYS", user_prompt="classify",
                                             schema={"type": "object"}, images=[img], cwd=tmp_path)
    assert rr.output == {"file_types": ["Passport"]}
    assert rr.model == "claude-fable-5-1"
    assert rr.usage["input_tokens"] == 500 and rr.usage["duration_s"] == 1.2
    cmd = seen["cmd"]
    assert cmd[:3] == ["claude", "-p", cmd[2]] and "page.jpg" in cmd[2]
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS"
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
    monkeypatch.setattr(ClaudeCodeRunner, "_exec_env", lambda self, c, cwd, t, env: _completed(stdout=json.dumps(envelope)))
    rr = ClaudeCodeRunner().run(system_prompt="s", user_prompt="u", schema={}, images=[], cwd=tmp_path)
    assert rr.output == {"file_types": ["Visa"]}


def test_claude_runner_detects_plan_limit(monkeypatch, tmp_path):
    envelope = {"is_error": True, "result": "You've hit your usage limit. Try again at 3pm."}
    monkeypatch.setattr(ClaudeCodeRunner, "_exec_env", lambda self, c, cwd, t, env: _completed(stdout=json.dumps(envelope)))
    with pytest.raises(PlanLimited):
        ClaudeCodeRunner().run(system_prompt="s", user_prompt="u", schema={}, images=[], cwd=tmp_path)


def test_claude_runner_reports_other_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(ClaudeCodeRunner, "_exec_env", lambda self, c, cwd, t, env: _completed(stderr="boom", code=1))
    with pytest.raises(RunnerError):
        ClaudeCodeRunner().run(system_prompt="s", user_prompt="u", schema={}, images=[], cwd=tmp_path)


def test_playbook_sections_load():
    assert "case_status" in playbook.instructions()
    assert playbook.prompt("run") and playbook.prompt("status")
    assert playbook.prompt("missing") == ""


def test_resolve_binary_order(tmp_path, monkeypatch):
    from uapply_agent import runners as r
    monkeypatch.setattr(r.shutil, "which", lambda _: None)
    monkeypatch.setattr(r.Path, "home", classmethod(lambda cls: tmp_path))
    assert r.resolve_binary("claude") is None
    local = tmp_path / ".local" / "bin" / "claude"
    local.parent.mkdir(parents=True); local.write_text("#!/bin/sh\n")
    assert r.resolve_binary("claude") == str(local)           # known location, not on PATH
    conf = tmp_path / "elsewhere" / "claude"; conf.parent.mkdir(); conf.write_text("")
    assert r.resolve_binary("claude", str(conf)) == str(conf)  # configured path wins
    assert r.resolve_binary("claude", str(tmp_path / "gone")) is None


def test_get_runner_uses_resolved_path_and_explains_when_missing(tmp_path, monkeypatch):
    from uapply_agent import runners as r
    monkeypatch.setattr(r, "detect_runtimes", lambda: [{"name": "claude-code", "path": "/opt/claude"}])
    assert r.get_runner("auto").binary == "/opt/claude"
    monkeypatch.setattr(r, "detect_runtimes", lambda: [{"name": "claude-code", "path": "/opt/claude",
                                                        "error": "This version is not compatible with the version of Windows"}])
    with pytest.raises(r.RunnerError, match="does not start on this machine"):
        r.get_runner("auto")
    monkeypatch.setattr(r, "detect_runtimes", lambda: [])
    with pytest.raises(r.RunnerError, match="uapply-agent setup"):
        r.get_runner("auto")


def test_runtime_error_reports_a_binary_that_does_not_start(tmp_path):
    from uapply_agent import runners as r
    good = tmp_path / "good"; good.write_text("#!/bin/sh\necho 2.1.0\n"); good.chmod(0o755)
    bad = tmp_path / "bad"; bad.write_text("#!/bin/sh\necho 'not compatible with this Windows' >&2; exit 216\n"); bad.chmod(0o755)
    assert r.runtime_error(str(good)) == ""
    assert "not compatible" in r.runtime_error(str(bad))
    assert r.runtime_error(str(tmp_path / "missing"))


def test_claude_headless_gets_a_closed_stdin(tmp_path, monkeypatch):
    """Under the MCP server stdin is the protocol pipe; `claude -p` must never read it."""
    import subprocess as sp
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return _completed(stdout=json.dumps({"type": "result", "is_error": False, "structured_output": {"a": 1},
                                             "result": "{}", "usage": {}}))
    monkeypatch.setattr(sp, "run", fake_run)
    ClaudeCodeRunner().run(system_prompt="s", user_prompt="u", schema={}, images=[], cwd=tmp_path)
    assert seen["stdin"] is sp.DEVNULL and seen["encoding"] == "utf-8"


def test_not_signed_in_stops_as_runtime_unavailable(tmp_path, monkeypatch):
    from uapply_agent.runners import RuntimeUnavailable
    envelope = {"type": "result", "is_error": True, "result": "Not logged in · Please run /login", "usage": {}}
    monkeypatch.setattr(ClaudeCodeRunner, "_exec_env", lambda self, cmd, cwd, t, env: _completed(stdout=json.dumps(envelope)))
    with pytest.raises(RuntimeUnavailable, match="claude auth login"):
        ClaudeCodeRunner().run(system_prompt="s", user_prompt="u", schema={}, images=[], cwd=tmp_path)
