"""The uapply-agent command: settings and exit codes."""
from uapply_agent import cli
from uapply_agent.cli import ExitCode


def test_bool_config_parses_false(tmp_path, monkeypatch):
    from uapply_agent import cli
    from uapply_agent.config import Settings
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cli.main(["config", "--set", "auto_update=false"])
    assert Settings.load().auto_update is False


def test_config_rejects_unknown_keys_and_missing_values(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cli.main(["config", "--set", "nope=1"]) == ExitCode.USAGE
    assert cli.main(["config", "--set", "workers"]) == ExitCode.USAGE
    assert cli.main(["config", "--set", "workers=many"]) == ExitCode.USAGE
    assert "unknown setting" in capsys.readouterr().err


def test_status_without_a_case_is_a_usage_error(tmp_path, capsys):
    assert cli.main(["status", "--folder", str(tmp_path)]) == ExitCode.USAGE
    assert "init --survey" in capsys.readouterr().err


def test_config_values_keep_their_types(tmp_path, monkeypatch):
    from uapply_agent.config import Settings
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cli.main(["config", "--set", "workers=3", "chat_upload=no", "model=haiku"])
    s = Settings.load()
    assert (s.workers, s.chat_upload, s.model) == (3, False, "haiku")


def test_retired_claude_settings_are_ignored(tmp_path, monkeypatch):
    import json

    from uapply_agent.config import Settings
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "uapply-agent").mkdir()
    (tmp_path / "uapply-agent" / "config.json").write_text(json.dumps({"runtime": "claude-code", "claude_bin": "/c"}))
    s = Settings.load()
    assert s.runtime == "auto" and not hasattr(s, "claude_bin")
