
from pathlib import Path

from uapply_agent import integrate as it


def test_claude_json_written_when_desktop_config_exists(tmp_path):
    home = tmp_path
    (home / ".claude.json").write_text('{"mcpServers": {"other": {"command": "x"}}, "keep": 1}')
    msg = it.register_claude("/opt/bin/uapply-agent", home, which=lambda _: None)
    assert msg.startswith("written")
    import json
    data = json.loads((home / ".claude.json").read_text())
    assert data["keep"] == 1 and "other" in data["mcpServers"]
    assert data["mcpServers"]["uapply"] == {"type": "stdio", "command": "/opt/bin/uapply-agent", "args": ["mcp"], "env": {}}


def test_claude_skipped_without_cli_or_config(tmp_path):
    msg = it.register_claude("/x", tmp_path, which=lambda _: None, desktop=lambda _: False)
    assert msg.startswith("skipped")
    assert not (tmp_path / ".claude.json").exists()


def test_claude_json_created_when_only_desktop_app_installed(tmp_path):
    (tmp_path / "Library" / "Application Support" / "Claude").mkdir(parents=True)
    msg = it.register_claude("/x/uapply-agent", tmp_path, which=lambda _: None)
    assert msg.startswith("written")
    import json
    assert json.loads((tmp_path / ".claude.json").read_text())["mcpServers"]["uapply"]["command"] == "/x/uapply-agent"


def test_codex_toml_appended_and_replaced(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('model = "o3"\n\n[mcp_servers.other]\ncommand = "y"\n')
    it.register_codex("/a/uapply-agent", tmp_path, which=lambda _: None)
    text = cfg.read_text()
    assert '[mcp_servers.uapply]\ncommand = "/a/uapply-agent"\nargs = ["mcp"]' in text
    assert "[mcp_servers.other]" in text and 'model = "o3"' in text
    it.register_codex("/b/uapply-agent", tmp_path, which=lambda _: None)
    text = cfg.read_text()
    assert text.count("[mcp_servers.uapply]") == 1 and '"/b/uapply-agent"' in text and "/a/" not in text
    import tomllib
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["uapply"]["args"] == ["mcp"] and parsed["mcp_servers"]["other"]["command"] == "y"
    assert "mcp" not in parsed          # no stray `["mcp"]` line left behind (it parses as a table)


def test_codex_toml_rewrite_is_idempotent_between_tables(tmp_path):
    import tomllib
    cfg = tmp_path / "config.toml"
    cfg.write_text('[mcp_servers.uapply]\ncommand = "old"\nargs = ["mcp"]\n\n[profiles.fast]\nmodel = "mini"\n')
    for _ in range(3):
        it.write_codex_toml("/new/uapply-agent", cfg)
    parsed = tomllib.loads(cfg.read_text())
    assert parsed["mcp_servers"]["uapply"] == {"command": "/new/uapply-agent", "args": ["mcp"]}
    assert parsed["profiles"]["fast"]["model"] == "mini" and set(parsed) == {"mcp_servers", "profiles"}


def test_malformed_claude_json_is_reported_not_overwritten(tmp_path):
    import pytest
    cfg = tmp_path / ".claude.json"
    cfg.write_text("{not json")
    with pytest.raises(it.SetupError, match="not valid JSON"):
        it.write_claude_json("/x", cfg)
    assert cfg.read_text() == "{not json"


def test_codex_skipped_without_cli_or_dir(tmp_path):
    assert it.register_codex("/x", tmp_path, which=lambda _: None).startswith("skipped")


def test_cli_fallback_when_claude_add_fails(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    msg = it.register_claude("/x/uapply-agent", tmp_path, which=lambda n: str(fake) if n == "claude" else None)
    assert msg.startswith("written") and (tmp_path / ".claude.json").exists()


def test_plugin_generated_from_playbook(tmp_path):
    import json
    msg = it.install_claude_plugin(tmp_path)
    root = tmp_path / ".claude" / "skills" / "uapply"
    assert msg.endswith(str(root))
    assert json.loads((root / ".claude-plugin" / "plugin.json").read_text())["name"] == "uapply"
    from uapply_agent import playbook
    for name in ("run", "status", "intake-from-chat"):
        body = (root / "commands" / f"{name}.md").read_text()
        assert body.startswith("---\ndescription: ") and playbook.prompt(name) in body
    it.install_claude_plugin(tmp_path)  # idempotent
    assert sorted(p.name for p in (root / "commands").iterdir()) == ["intake-from-chat.md", "run.md", "status.md"]


def test_record_runtimes_saves_absolute_paths(tmp_path, monkeypatch):
    from uapply_agent.config import Settings
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from uapply_agent import runners
    monkeypatch.setattr(runners.Path, "home", classmethod(lambda cls: tmp_path))
    s = Settings()
    found = it.record_runtimes(s, which=lambda n: "/usr/local/bin/claude" if n == "claude" else None)
    assert found == {"claude": "/usr/local/bin/claude"} and s.claude_bin == "/usr/local/bin/claude" and s.codex_bin == ""
    assert Settings.load().claude_bin == "/usr/local/bin/claude"


def _fake_claude(tmp_path, logged_in_after_login: bool, start_logged_in: bool = False):
    """A `claude` stand-in: `auth status --json` reads a state file, `auth login` flips it."""
    state = tmp_path / "state"
    state.write_text("1" if start_logged_in else "0")
    exe = tmp_path / "claude"
    exe.write_text(f"""#!/bin/sh
if [ "$1 $2" = "auth status" ]; then
  if [ "$(cat {state})" = "1" ]; then echo '{{"loggedIn": true}}'; else echo '{{"loggedIn": false}}'; fi
elif [ "$1 $2" = "auth login" ]; then
  echo login >> {tmp_path}/calls; echo {"1" if logged_in_after_login else "0"} > {state}
fi
""")
    exe.chmod(0o755)
    return str(exe)


def test_claude_login_runs_when_signed_out(tmp_path):
    exe = _fake_claude(tmp_path, logged_in_after_login=True)
    assert it.claude_logged_in(exe) is False
    assert it.ensure_claude_login(exe, say=lambda _: None) is True
    assert (tmp_path / "calls").read_text().count("login") == 1


def test_claude_login_skipped_when_signed_in_or_non_interactive(tmp_path):
    exe = _fake_claude(tmp_path, logged_in_after_login=True, start_logged_in=True)
    assert it.ensure_claude_login(exe, say=lambda _: None) is True
    exe2 = _fake_claude(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, logged_in_after_login=True)
    assert it.ensure_claude_login(exe2, say=lambda _: None, interactive=False) is False
    assert not (tmp_path / "x" / "calls").exists()


def test_claude_login_state_unknown_when_cli_missing(tmp_path):
    assert it.claude_logged_in(str(tmp_path / "nope")) is None


def test_broken_runtime_is_reported_and_skips_login(tmp_path):
    assert it.broken_runtimes({"claude": "/c"}, check=lambda p: "not compatible") == {"claude": "not compatible"}
    assert it.broken_runtimes({"claude": "/c"}, check=lambda p: "") == {}


def test_record_runtimes_prefers_a_build_that_starts(tmp_path, monkeypatch):
    """An npm claude that Windows refuses to start loses to the native build in ~/.local/bin."""
    from uapply_agent import runners
    from uapply_agent.config import Settings
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(runners.Path, "home", classmethod(lambda cls: tmp_path))
    broken = tmp_path / "nodejs" / "claude"
    broken.parent.mkdir()
    broken.write_text("#!/bin/sh\nexit 216\n")
    broken.chmod(0o755)
    native = tmp_path / ".local" / "bin" / "claude"
    native.parent.mkdir(parents=True)
    native.write_text("#!/bin/sh\necho 2.1.0\n")
    native.chmod(0o755)
    runners._WORKING.clear()
    found = it.record_runtimes(Settings(), which=lambda n: str(broken) if n == "claude" else None)
    assert found == {"claude": str(native)}


def test_claude_login_gets_its_own_console_on_windows(tmp_path, monkeypatch):
    calls = []
    states = iter([False, True])
    monkeypatch.setattr(it, "claude_logged_in", lambda exe: next(states))
    monkeypatch.setattr(it.os, "name", "nt")
    monkeypatch.setattr(it.subprocess, "CREATE_NEW_CONSOLE", 0x10, raising=False)
    monkeypatch.setattr(it.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    assert it.ensure_claude_login("C:/claude.exe", say=lambda _: None) is True
    assert calls[0][0][1:] == ["auth", "login"] and calls[0][1]["creationflags"] == 0x10


def test_codex_skills_generated_from_playbook(tmp_path):
    it.install_codex_skills(tmp_path)
    skill = (tmp_path / ".agents" / "skills" / "uapply-run" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: uapply-run\ndescription: ")
    assert (tmp_path / ".agents" / "skills" / "uapply-status" / "SKILL.md").exists()


def test_next_step_names_only_registered_runtimes():
    codex_only = it.next_step({"claude": "skipped: Claude Code not found", "codex": "written to x"})
    assert "$uapply-run" in codex_only and "Claude" not in codex_only
    both = it.next_step({"claude": "written to y", "codex": "written to x"})
    assert "/uapply:run" in both and "$uapply-run" in both
    assert "not found" in it.next_step({"claude": "skipped", "codex": "skipped"})


def test_codex_app_bundle_is_a_known_location(monkeypatch, tmp_path):
    from uapply_agent import runners
    monkeypatch.setattr(runners.sys, "platform", "darwin")
    assert Path("/Applications/Codex.app/Contents/Resources/codex") in runners._codex_locations(tmp_path)
