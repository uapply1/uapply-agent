import json
import time

import pytest

from uapply_agent import updater as up

A, B = "a" * 40, "b" * 40


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv(up.DELEGATED, raising=False)
    monkeypatch.delenv("UAPPLY_NO_UPDATE", raising=False)
    return tmp_path


def settings(sha=A, auto=True):
    return type("S", (), {"installed_sha": sha, "auto_update": auto})()


def cache_latest(sha):
    p = up.data_dir() / "latest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"sha": sha, "at": time.time()}))


def test_up_to_date_starts_current(home):
    cache_latest(A)
    assert up.resolve_target(settings(A)) == (None, f"up to date ({A[:7]})")


def test_offline_starts_current(home, monkeypatch):
    monkeypatch.setattr(up, "latest_sha", lambda: None)
    target, reason = up.resolve_target(settings(A))
    assert target is None and "offline" in reason


def test_newer_already_installed_is_used_without_reinstalling(home, monkeypatch):
    cache_latest(B)
    monkeypatch.setattr(up, "works", lambda exe: exe == up.version_exe(B))
    monkeypatch.setattr(up, "start_install", lambda sha: pytest.fail("must not reinstall"))
    assert up.resolve_target(settings(A)) == (up.version_exe(B), f"updated to {B[:7]}")


def test_newer_is_installed_then_used(home, monkeypatch):
    cache_latest(B)
    installed = []

    class Proc:
        def wait(self, timeout):
            installed.append(B)
            return 0
    monkeypatch.setattr(up, "start_install", lambda sha: Proc())
    monkeypatch.setattr(up, "works", lambda exe: bool(installed) and exe == up.version_exe(B))
    target, reason = up.resolve_target(settings(A))
    assert target == up.version_exe(B) and installed == [B]


def test_slow_install_keeps_current_and_finishes_later(home, monkeypatch):
    import subprocess
    cache_latest(B)

    class Slow:
        def wait(self, timeout):
            raise subprocess.TimeoutExpired("uv", timeout)
    monkeypatch.setattr(up, "start_install", lambda sha: Slow())
    monkeypatch.setattr(up, "works", lambda exe: False)
    target, reason = up.resolve_target(settings(A), wait_s=1)
    assert target is None and "next start" in reason


def test_delegated_child_and_opt_out_do_not_check(home, monkeypatch):
    monkeypatch.setattr(up, "resolve_target", lambda *a, **k: pytest.fail("must not check"))
    monkeypatch.setenv(up.DELEGATED, "1")
    up.maybe_delegate(["mcp"], settings())
    monkeypatch.delenv(up.DELEGATED)
    up.maybe_delegate(["mcp"], settings(auto=False))


def test_running_sha_from_version_folder(home, monkeypatch):
    monkeypatch.setattr(up.sys, "prefix", str(up.versions_dir() / B / "tools" / "uapply-agent"))
    assert up.running_sha(settings(A)) == B
    monkeypatch.setattr(up.sys, "prefix", "/usr")
    assert up.running_sha(settings(A)) == A
