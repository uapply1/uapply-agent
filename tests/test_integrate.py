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
    assert it.register_claude("/x", tmp_path, which=lambda _: None).startswith("skipped")
    assert not (tmp_path / ".claude.json").exists()


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


def test_codex_skipped_without_cli_or_dir(tmp_path):
    assert it.register_codex("/x", tmp_path, which=lambda _: None).startswith("skipped")


def test_cli_fallback_when_claude_add_fails(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nexit 1\n"); fake.chmod(0o755)
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
