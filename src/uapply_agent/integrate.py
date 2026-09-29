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


def _find(binary: str) -> Optional[str]:
    from .runners import resolve_binary
    return resolve_binary(binary)


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
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, check=False)


# ---- Claude Code / Claude desktop (Code tab reads ~/.claude.json) ----

def _claude_json_entry(exe: str) -> dict:
    return {"type": "stdio", "command": exe, "args": ["mcp"], "env": {}}


def write_claude_json(exe: str, path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.setdefault("mcpServers", {})[SERVER] = _claude_json_entry(exe)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def register_claude(exe: str, home: Path, which: Callable[[str], Optional[str]] = _find) -> str:
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


def verify_claude(which: Callable[[str], Optional[str]] = _find) -> Optional[bool]:
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


def claude_logged_in(claude: str) -> Optional[bool]:
    """`claude auth status --json` → loggedIn; None when the CLI cannot tell us."""
    try:
        r = _run([claude, "auth", "status", "--json"], timeout=30)
        return bool(json.loads(r.stdout or "{}").get("loggedIn"))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def ensure_claude_login(claude: str, say: Callable[[str], None] = print, interactive: bool = True) -> Optional[bool]:
    """The CLI has its own login, separate from the desktop app; headless tasks fail without it."""
    state = claude_logged_in(claude)
    if state is False and interactive:
        say("Claude Code CLI: not signed in. Opening the Claude sign-in (use your Claude Pro/Max account)...")
        kw = {}
        if os.name == "nt":
            # Own console window: under `irm | iex` the child's stdin is not the keyboard, so the
            # "Paste code here" prompt read an empty line and the token exchange failed with 400.
            kw["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            say("  A new window opens for the Claude sign-in. If no browser appears, open the link shown "
                "there, sign in, and paste the code into that window.")
        try:
            subprocess.run([claude, "auth", "login"], check=False, timeout=900, **kw)
        except (OSError, subprocess.TimeoutExpired):
            pass
        state = claude_logged_in(claude)
    return state


# ---- Codex (~/.codex/config.toml) ----

_SECTION = re.compile(r"^\[mcp_servers\.uapply\][^\[]*", re.M | re.S)


def write_codex_toml(exe: str, path: Path) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    block = f'[mcp_servers.{SERVER}]\ncommand = {json.dumps(exe)}\nargs = ["mcp"]\n'
    if _SECTION.search(text):
        text = _SECTION.sub(block, text, count=1)
    else:
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def register_codex(exe: str, home: Path, which: Callable[[str], Optional[str]] = _find) -> str:
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


def record_runtimes(settings, which: Callable[[str], Optional[str]] = _find) -> dict:
    """Remember where `claude` / `codex` are: the terminal running setup has the full PATH, the
    desktop app that later launches the MCP server usually does not."""
    from .runners import _known_locations, runtime_error
    found = {}
    for attr, binary in (("claude_bin", "claude"), ("codex_bin", "codex")):
        path = which(binary)
        if path and runtime_error(path):
            # e.g. an npm build Windows refuses to start while the native build in ~/.local/bin works
            path = next((str(c) for c in _known_locations(binary) if c.exists() and not runtime_error(str(c))), path)
        if path:
            setattr(settings, attr, str(Path(path).absolute()))
            found[binary] = getattr(settings, attr)
    if found:
        settings.save()
    return found


def broken_runtimes(found: dict, check: Callable[[str], str] | None = None) -> dict:
    """{binary: error} for recorded runtimes that do not start (`--version` fails)."""
    if check is None:
        from .runners import runtime_error as check
    return {b: err for b, p in found.items() if (err := check(p))}


def run_setup(say: Callable[[str], None] = print, home: Optional[Path] = None, settings=None,
              login: bool = True) -> dict:
    home = home or Path(os.environ.get("UAPPLY_HOME") or Path.home())
    exe = own_executable()
    say(f"uapply-agent: {exe}")
    out = {"executable": exe}
    if settings is not None:
        out["runtimes"] = record_runtimes(settings)
        if out["runtimes"]:
            say("Runtimes: " + ", ".join(f"{k} = {v}" for k, v in out["runtimes"].items()))
            out["broken"] = broken_runtimes(out["runtimes"])
            for b, err in out["broken"].items():
                say(f"Runtimes: {b} at {out['runtimes'][b]} does NOT start on this machine: {err}")
                if "not compatible" in err.lower():
                    say("  Windows reports this build is incompatible with this Windows version: Claude Code needs "
                        "a newer Windows (see the README), or use the Codex CLI instead.")
        else:
            say("Runtimes: neither `claude` nor `codex` found — rerun the uApply installer (it installs the Claude "
                "Code CLI) or install Claude Code, then run `uapply-agent setup` again")
        if (claude := out["runtimes"].get("claude")) and "claude" not in out.get("broken", {}):
            state = ensure_claude_login(claude, say, interactive=login and sys.stdin.isatty())
            out["claude_logged_in"] = state
            say({True: "Claude Code CLI: signed in",
                 False: "Claude Code CLI: NOT signed in — run `claude auth login`, or local tasks cannot run",
                 None: "Claude Code CLI: sign-in state unknown — run `claude auth status`"}[state])
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
