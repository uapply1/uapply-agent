"""`uapply-agent setup`: register the MCP server with Claude Code and Codex, by absolute path."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from collections.abc import Callable
from typing import Optional

SERVER = "uapply"
PLUGIN_DIR = Path(".claude") / "skills" / "uapply"   # auto-loaded by Claude Code as uapply@skills-dir


def own_executable() -> str:
    """Absolute path of the `uapply-agent` command; GUI apps run with a PATH that lacks ~/.local/bin."""
    # Keep the launcher path (~/.local/bin/...), not the symlink target inside uv's tool dir.
    exe = Path(sys.argv[0])
    if exe.name.startswith("uapply-agent") and exe.exists():
        return str(exe.absolute())
    found = shutil.which("uapply-agent")
    if found:
        return str(Path(found).absolute())
    raise RuntimeError("cannot locate the uapply-agent executable; run `uapply-agent setup` from the installed command")


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


# ---- Claude Code / Claude desktop (Code tab reads ~/.claude.json) ----

def _claude_json_entry(exe: str) -> dict:
    return {"type": "stdio", "command": exe, "args": ["mcp"], "env": {}}


def write_claude_json(exe: str, path: Path) -> None:
    data = json.loads(path.read_text()) if path.exists() else {}
    data.setdefault("mcpServers", {})[SERVER] = _claude_json_entry(exe)
    path.write_text(json.dumps(data, indent=2))


def register_claude(exe: str, home: Path, which: Callable[[str], Optional[str]] = shutil.which) -> str:
    claude = which("claude")
    cfg = home / ".claude.json"
    if claude:
        _run([claude, "mcp", "remove", "--scope", "user", SERVER])
        r = _run([claude, "mcp", "add", "--scope", "user", SERVER, "--", exe, "mcp"])
        if r.returncode == 0:
            return "registered via `claude mcp add` (user scope)"
    if cfg.exists() or claude:
        write_claude_json(exe, cfg)
        return f"written to {cfg}"
    return "skipped: Claude Code not found (no `claude` command, no ~/.claude.json)"


def verify_claude(which: Callable[[str], Optional[str]] = shutil.which) -> Optional[bool]:
    claude = which("claude")
    if not claude:
        return None
    try:
        r = _run([claude, "mcp", "list"], timeout=90)
    except subprocess.TimeoutExpired:
        return None
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith(f"{SERVER}:")), "")
    return ("Connected" in line) if line else False


def install_claude_plugin(home: Path) -> str:
    """Write the generated plugin so `/uapply:run` is a real slash command (MCP prompts show as `/uapply:run (MCP)`)."""
    from . import __version__, playbook
    root = home / PLUGIN_DIR
    if root.exists():
        shutil.rmtree(root)
    for rel, content in playbook.plugin_files(__version__).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content, encoding="utf-8")
    return f"commands written to {root}"


# ---- Codex (~/.codex/config.toml) ----

_SECTION = re.compile(r"^\[mcp_servers\.uapply\][^\[]*", re.M | re.S)


def write_codex_toml(exe: str, path: Path) -> None:
    text = path.read_text() if path.exists() else ""
    block = f'[mcp_servers.{SERVER}]\ncommand = {json.dumps(exe)}\nargs = ["mcp"]\n'
    if _SECTION.search(text):
        text = _SECTION.sub(block, text, count=1)
    else:
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def register_codex(exe: str, home: Path, which: Callable[[str], Optional[str]] = shutil.which) -> str:
    codex = which("codex")
    cfg = home / ".codex" / "config.toml"
    if codex:
        _run([codex, "mcp", "remove", SERVER])
        r = _run([codex, "mcp", "add", SERVER, "--", exe, "mcp"])
        if r.returncode == 0:
            return "registered via `codex mcp add`"
    if codex or cfg.parent.exists():
        write_codex_toml(exe, cfg)
        return f"written to {cfg}"
    return "skipped: Codex not found (no `codex` command, no ~/.codex)"


def run_setup(say: Callable[[str], None] = print, home: Optional[Path] = None) -> dict:
    home = home or Path(os.environ.get("UAPPLY_HOME") or Path.home())
    exe = own_executable()
    say(f"uapply-agent: {exe}")
    out = {"executable": exe}
    out["claude"] = register_claude(exe, home)
    say(f"Claude Code: {out['claude']}")
    if not out["claude"].startswith("skipped"):
        out["claude_plugin"] = install_claude_plugin(home)
        say(f"Claude Code: {out['claude_plugin']}")
    out["codex"] = register_codex(exe, home)
    say(f"Codex: {out['codex']}")
    if not out["claude"].startswith("skipped"):
        ok = verify_claude()
        out["claude_connected"] = ok
        if ok is False:
            say("Claude Code: server registered but `claude mcp list` does not report it connected")
    return out
