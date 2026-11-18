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
    monkeypatch.setattr(up, "works", lambda exe, *a: exe == up.version_exe(B))
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
    monkeypatch.setattr(up, "works", lambda exe, *a: bool(installed) and exe == up.version_exe(B))
    target, reason = up.resolve_target(settings(A))
    assert target == up.version_exe(B) and installed == [B]


def test_slow_install_keeps_current_and_finishes_later(home, monkeypatch):
    import subprocess
    cache_latest(B)

    class Slow:
        def wait(self, timeout):
            raise subprocess.TimeoutExpired("uv", timeout)
    monkeypatch.setattr(up, "start_install", lambda sha: Slow())
    monkeypatch.setattr(up, "works", lambda exe, *a: False)
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


def test_mcp_start_never_waits_for_an_install(home, monkeypatch):
    """MCP clients give the server ~30 s: the update is started in the background instead."""
    cache_latest(B)
    started = []

    class Proc:
        def wait(self, timeout):
            pytest.fail("the MCP start must not wait for the install")
    monkeypatch.setattr(up, "start_install", lambda sha: started.append(sha) or Proc())
    monkeypatch.setattr(up, "works", lambda exe, *a: False)
    target, reason = up.resolve_target(settings(A), wait_s=0)
    assert target is None and started == [B] and "next start" in reason


def test_an_install_in_progress_is_not_started_twice(home, monkeypatch):
    cache_latest(B)
    marker = up.versions_dir() / B / up.INSTALLING
    marker.parent.mkdir(parents=True)
    marker.write_text("now")
    monkeypatch.setattr(up, "works", lambda exe, *a: False)
    monkeypatch.setattr(up, "start_install", lambda sha: pytest.fail("already installing"))
    target, reason = up.resolve_target(settings(A), wait_s=0)
    assert target is None and "installing" in reason


def test_a_verified_version_is_not_started_again_to_check_it(home, monkeypatch):
    cache_latest(B)
    exe = up.version_exe(B)
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    (up.versions_dir() / B / up.READY).write_text("now")
    monkeypatch.setattr(up, "works", lambda exe, *a: pytest.fail("must not spawn --version"))
    assert up.resolve_target(settings(A), wait_s=0) == (exe, f"updated to {B[:7]}")



def make_version(sha, age_s, files=3):
    """A version dir with a few real files (so a deletion can be cut short), aged by its mtime."""
    root = up.versions_dir() / sha
    exe = up.version_exe(sha)
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("")
    (root / up.READY).write_text("now")
    for i in range(files):
        (root / f"f{i}.py").write_text("x")
    t = time.time() - age_s
    import os
    os.utime(root, (t, t))
    return root


def names():
    return sorted(d.name for d in up.versions_dir().iterdir())


C, D, E, F = "c" * 40, "d" * 40, "e" * 40, "f" * 40


def test_mcp_start_prunes_without_waiting_for_an_update(home, monkeypatch):
    """`uapply-agent mcp` (wait_s=0) never reaches the post-install prune, so cleanup runs on its own."""
    for i, sha in enumerate([F, E, D, C, B]):     # F newest ... B oldest
        make_version(sha, age_s=100 * (i + 1))
    make_version(A, age_s=10_000)                  # the running version, oldest of all
    cache_latest(A)
    up.maybe_delegate(["mcp"], settings(A), wait_s=0)
    for t in [t for t in __import__("threading").enumerate() if t.name == "uapply-prune"]:
        t.join(5)
    assert names() == sorted([A, D, E, F])


def test_cleanup_runs_even_when_updates_are_off(home, monkeypatch):
    for i, sha in enumerate([F, E, D, C, B]):
        make_version(sha, age_s=100 * (i + 1))
    monkeypatch.setenv("UAPPLY_NO_UPDATE", "1")
    monkeypatch.setattr(up, "resolve_target", lambda *a, **k: pytest.fail("must not check"))
    up.maybe_delegate(["mcp"], settings(A))
    for t in [t for t in __import__("threading").enumerate() if t.name == "uapply-prune"]:
        t.join(5)
    assert names() == sorted([D, E, F])


def test_cleanup_keeps_the_last_known_latest_offline(home, monkeypatch):
    for i, sha in enumerate([F, E, D, C, B]):
        make_version(sha, age_s=100 * (i + 1))
    p = up.data_dir() / "latest.json"
    p.write_text(json.dumps({"sha": B, "at": 0}))   # stale cache: still the best we know
    monkeypatch.setattr(up.httpx, "get", lambda *a, **k: pytest.fail("cleanup must not use the network"))
    t = up.cleanup(settings(A))
    t.join(5)
    assert names() == sorted([B, D, E, F])


def test_cleanup_returns_before_the_deletion_finishes(home, monkeypatch):
    import threading
    for i, sha in enumerate([F, E, D, C]):
        make_version(sha, age_s=100 * (i + 1))
    gate = threading.Event()
    real = up.shutil.rmtree
    monkeypatch.setattr(up.shutil, "rmtree", lambda p, **k: (gate.wait(5), real(p, **k)))
    t = up.cleanup(settings(A))
    assert t.daemon and t.is_alive()                # the caller (MCP start) is not held
    gate.set()
    t.join(5)
    assert names() == sorted([D, E, F])


def test_an_install_in_progress_is_not_pruned(home):
    for i, sha in enumerate([F, E, D, C]):
        make_version(sha, age_s=100 * (i + 1))
    root = up.versions_dir() / C
    (root / up.INSTALLING).write_text("now")
    t = time.time() - 400
    import os
    os.utime(root, (t, t))                          # writing the marker bumped the dir: put it back last
    up.prune(set())
    assert names() == sorted([C, D, E, F])


def test_a_crashed_or_future_dated_install_marker_expires(home):
    import os
    for i, sha in enumerate([F, E, D, C, B]):
        make_version(sha, age_s=100 * (i + 1))
    old = time.time() - up.INSTALL_GRACE_S - 1
    future = time.time() + 3600
    for sha, t in [(C, old), (B, future)]:
        m = up.versions_dir() / sha / up.INSTALLING
        m.write_text("then")
        os.utime(m, (t, t))
    for i, sha in enumerate([C, B]):
        t = time.time() - 400 - 100 * i
        os.utime(up.versions_dir() / sha, (t, t))
    up.prune(set())
    assert names() == sorted([D, E, F])


def test_an_interrupted_deletion_never_leaves_a_ready_looking_version(home, monkeypatch):
    """A cut-off delete leaves only `.trash-*`, which is not a version; the next start empties it."""
    for i, sha in enumerate([F, E, D, C]):
        make_version(sha, age_s=100 * (i + 1))

    def cut_off(path, **k):                          # delete one file, then the process "dies"
        next(p for p in sorted(__import__("pathlib").Path(path).rglob("*")) if p.is_file()).unlink()
    monkeypatch.setattr(up.shutil, "rmtree", cut_off)
    up.prune(set())
    left = names()
    assert C not in left and any(n.startswith(up.TRASH) for n in left)
    assert not up.ready(C)
    monkeypatch.undo()
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    up.cleanup(settings(F)).join(5)
    assert names() == sorted([D, E, F])
