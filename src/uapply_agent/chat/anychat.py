"""AnyChat CLI wrapper (github.com/jackyzhang69/plugins, plugins/anychat).

The CLI owns the archive; we only run its documented commands. Tokens never go
on the command line, and nothing here reads WeChat storage directly.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from .base import Availability, ChatError, Contact, Transcript

PLATFORM_DIRS = {("Darwin", "arm64"): "darwin-arm64", ("Windows", "AMD64"): "win32-x64"}


def platform_dir() -> Optional[str]:
    return PLATFORM_DIRS.get((platform.system(), platform.machine()))


def find_binary(explicit: str = "") -> Optional[Path]:
    """Same lookup order as the AnyChat skill: $ANYCHAT_BIN → plugin install → ~/.local/bin → PATH."""
    candidates = [explicit, os.environ.get("ANYCHAT_BIN", "")]
    pd = platform_dir()
    exe = "anychat.exe" if platform.system() == "Windows" else "anychat"
    if pd:
        candidates.append(str(Path.home() / ".jackyzhang.app" / "plugins" / "anychat" / "current" / "bin" / pd / exe))
    candidates.append(str(Path.home() / ".local" / "bin" / exe))
    for c in candidates:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return Path(c)
    found = shutil.which("anychat")
    return Path(found) if found else None


class AnyChatSource:
    name = "anychat"
    INSTALL_HINT = ("install with `claude plugin marketplace add jackyzhang69/plugins && "
                    "claude plugin install anychat@jacky-plugins`, then log in with AnyChat's own login")

    def __init__(self, binary: str = "", timeout_s: int = 600):
        self.binary = find_binary(binary)
        self.timeout_s = timeout_s

    # ---- process plumbing ----

    def _run(self, *args: str, timeout_s: Optional[int] = None) -> subprocess.CompletedProcess:
        if not self.binary:
            raise ChatError("not_installed", "anychat CLI not found", self.INSTALL_HINT)
        try:
            return subprocess.run([str(self.binary), *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout_s or self.timeout_s)
        except subprocess.TimeoutExpired:
            raise ChatError("cli_error", f"anychat {args[0]} timed out")
        except OSError as e:
            raise ChatError("cli_error", f"cannot run anychat: {e}")

    @staticmethod
    def _json(proc: subprocess.CompletedProcess, what: str):
        text = (proc.stdout or "").strip()
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
            raise ChatError("cli_error", f"anychat {what}: unreadable output: {text[:200]}")

    # ---- ChatSource ----

    def available(self) -> Availability:
        if not self.binary:
            state = "not_installed" if platform_dir() else "unsupported_platform"
            detail = "anychat CLI not found" if platform_dir() else f"AnyChat ships for macOS arm64 and Windows x64 only ({platform.system()} {platform.machine()})"
            return Availability(self.name, False, state, detail, self.INSTALL_HINT if platform_dir() else "")
        try:
            proc = self._run("whoami", "--json", timeout_s=30)
        except ChatError as e:
            return Availability(self.name, False, e.code, str(e), e.hint)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:200]
            state = "not_logged_in" if re.search(r"log ?in|token|unauthori", err, re.I) else "cli_error"
            return Availability(self.name, False, state, err, "run AnyChat's own login (token via stdin) and retry")
        data = self._json(proc, "whoami")
        who = data.get("email") or data.get("user") or data.get("account") or "logged in"
        return Availability(self.name, True, "ok", str(who))

    def resolve(self, name: str) -> list[Contact]:
        proc = self._run("resolve", "--query", name, "--json", timeout_s=60)
        if proc.returncode != 0:
            raise ChatError("cli_error", f"anychat resolve failed: {(proc.stderr or proc.stdout).strip()[:200]}")
        data = self._json(proc, "resolve")
        rows = data.get("candidates") or data.get("results") or data.get("matches") or (data if isinstance(data, list) else [])
        out = []
        for r in rows:
            if isinstance(r, str):
                out.append(Contact(r))
            elif isinstance(r, dict):
                label = r.get("display_name") or r.get("name") or r.get("nickname") or r.get("remark") or ""
                if label:
                    out.append(Contact(str(label), kind=str(r.get("kind") or r.get("type") or "friend"),
                                       raw_id=r.get("wxid") or r.get("id") or r.get("raw_id")))
        return out

    def fetch(self, contact: str, days: int, out_dir: Path) -> Transcript:
        out_dir.mkdir(parents=True, exist_ok=True)
        today = date.today()
        date_from, date_to = (today - timedelta(days=days)).isoformat(), today.isoformat()
        slug = re.sub(r"[^\w一-鿿-]+", "_", contact).strip("_") or "contact"
        out = out_dir / f"{self.name}_{slug}_{date_from}_{date_to}.md"
        proc = self._run("query", "--mode", "friend", "--target", contact, "--days", str(days),
                         "--format", "md", "-o", str(out))
        if proc.returncode != 0 or not out.exists():
            raise ChatError("cli_error", f"anychat query failed: {(proc.stderr or proc.stdout).strip()[:300]}")
        text = out.read_text(encoding="utf-8", errors="replace")
        lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
        return Transcript(self.name, contact, date_from, date_to, out, message_count=len(lines), chars=len(text))
