"""Self-update at start: install the latest `main` side by side and hand the process over to it.

The registered command (`~/.local/bin/uapply-agent`) never replaces itself: on Windows a running
program's files are locked. Each release goes to `versions/<sha>/` and the launcher delegates to it.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REPO = "uapply1/uapply-agent"
BRANCH = "main"
ARCHIVE = "https://github.com/{repo}/archive/{sha}.zip"
DELEGATED = "UAPPLY_AGENT_DELEGATED"      # set in the child so it does not check again
CHECK_CACHE_S = 300
_WIN = sys.platform.startswith("win")


def data_dir() -> Path:
    if _WIN and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "uapply-agent"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "uapply-agent"


def versions_dir() -> Path:
    return data_dir() / "versions"


def version_exe(sha: str) -> Path:
    return versions_dir() / sha / "bin" / ("uapply-agent.exe" if _WIN else "uapply-agent")


def running_sha(settings=None) -> Optional[str]:
    """This process's commit: from its versions/<sha>/ location, else what the installer recorded."""
    prefix = Path(sys.prefix).absolute()
    root = versions_dir().absolute()
    if root in prefix.parents:
        return prefix.relative_to(root).parts[0]
    return getattr(settings, "installed_sha", "") or None


def latest_sha(timeout_s: float = 4.0) -> Optional[str]:
    """Latest commit on main (cached for a few minutes); None when offline or rate-limited."""
    cache = data_dir() / "latest.json"
    try:
        c = json.loads(cache.read_text(encoding="utf-8"))
        if time.time() - c["at"] < CHECK_CACHE_S:
            return c["sha"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        import httpx
        r = httpx.get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}", timeout=timeout_s,
                      headers={"Accept": "application/vnd.github.sha", "User-Agent": "uapply-agent"})
        sha = r.text.strip() if r.status_code == 200 else ""
    except Exception as e:  # offline, DNS, TLS: never block the start
        logger.info("update check failed: %s", e)
        return None
    if len(sha) != 40:
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"sha": sha, "at": time.time()}), encoding="utf-8")
    return sha


def works(exe: Path) -> bool:
    if not exe.exists():
        return False
    try:
        r = subprocess.run([str(exe), "--version"], stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
                           check=False, env={**os.environ, DELEGATED: "1"})
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def find_uv() -> Optional[str]:
    found = shutil.which("uv")
    if found:
        return found
    for c in (Path.home() / ".local" / "bin" / ("uv.exe" if _WIN else "uv"), Path.home() / ".cargo" / "bin" / "uv"):
        if c.exists():
            return str(c)
    return None


def start_install(sha: str) -> Optional[subprocess.Popen]:
    """`uv tool install` of that commit into versions/<sha>/, in the background (it outlives a timeout)."""
    uv = find_uv()
    if not uv:
        logger.info("update skipped: uv not found")
        return None
    root = versions_dir() / sha
    root.mkdir(parents=True, exist_ok=True)
    source = os.environ.get("UAPPLY_AGENT_ARCHIVE", ARCHIVE).format(repo=REPO, sha=sha)
    env = {**os.environ, "UV_TOOL_DIR": str(root / "tools"), "UV_TOOL_BIN_DIR": str(root / "bin"),
           "UV_NO_MODIFY_PATH": "1"}
    log = open(root / "install.log", "ab")
    detach = {"creationflags": subprocess.CREATE_NO_WINDOW} if _WIN else {"start_new_session": True}
    return subprocess.Popen([uv, "tool", "install", "--force", "--quiet", f"uapply-agent @ {source}"],
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, **detach)


def prune(keep: set[str], max_versions: int = 3) -> None:
    """Drop old versions (best effort: a version still running stays locked on Windows)."""
    try:
        dirs = sorted((d for d in versions_dir().iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime,
                      reverse=True)
    except OSError:
        return
    for d in dirs[max_versions:]:
        if d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)


def resolve_target(settings=None, wait_s: float = 25.0, say=None) -> tuple[Optional[Path], str]:
    """Which executable should serve this start, and a one-line reason (for stderr / `update`)."""
    say = say or (lambda _m: None)
    mine = running_sha(settings)
    latest = latest_sha()
    if not latest:
        return None, "update check unavailable (offline?); starting the installed version"
    if latest == mine:
        return None, f"up to date ({latest[:7]})"
    exe = version_exe(latest)
    if works(exe):
        return exe, f"updated to {latest[:7]}"
    say(f"uapply-agent: updating to {latest[:7]}...")
    proc = start_install(latest)
    if proc is None:
        return None, "update needs uv; starting the installed version"
    try:
        code = proc.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        return None, f"update to {latest[:7]} is still installing; it is used from the next start"
    if code == 0 and works(exe):
        prune({latest, mine or ""})
        return exe, f"updated to {latest[:7]}"
    return None, f"update to {latest[:7]} failed (see {versions_dir() / latest / 'install.log'}); starting the installed version"


def maybe_delegate(argv: list[str], settings=None, wait_s: float = 25.0) -> None:
    """Called at the start of long-lived commands. Returns to run the current version, or hands the
    process to the newer one and exits with its code. Nothing is written to stdout (MCP protocol)."""
    if os.environ.get(DELEGATED) or os.environ.get("UAPPLY_NO_UPDATE") or getattr(settings, "auto_update", True) is False:
        return
    err = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    target, reason = resolve_target(settings, wait_s=wait_s, say=err)
    err(f"uapply-agent: {reason}")
    if target is None:
        return
    env = {**os.environ, DELEGATED: "1"}
    if not _WIN and hasattr(os, "execve"):
        os.execve(str(target), [str(target), *argv], env)   # same PID and stdio: the client never notices
    code = subprocess.run([str(target), *argv], env=env, check=False).returncode   # stdio inherited
    sys.exit(code)
