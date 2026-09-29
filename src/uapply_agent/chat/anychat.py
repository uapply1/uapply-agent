"""AnyChat CLI adapter (the `anychat` plugin from the jackyzhang69/plugins marketplace).

The CLI owns the chat archive and its login; this module only runs its commands. No token is
passed on the command line, and nothing here reads WeChat storage directly.
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

from .base import Availability, ChatError, Contact, Transcript

PLATFORM_DIRS = {("Darwin", "arm64"): "darwin-arm64", ("Windows", "AMD64"): "win32-x64"}
PLUGIN_BIN_DIR = Path(".jackyzhang.app") / "plugins" / "anychat" / "current" / "bin"   # under the home folder
INSTALL_HINT = ("install with `claude plugin marketplace add jackyzhang69/plugins && "
                "claude plugin install anychat@jacky-plugins`, then log in with AnyChat's own login")
# Field names accepted from the CLI's JSON; it has used more than one name for the same field.
CANDIDATE_LISTS = ("candidates", "results", "matches")
LABEL_FIELDS = ("display_name", "name", "nickname", "remark")
ACCOUNT_FIELDS = ("email", "user", "account")


def platform_dir() -> str | None:
    return PLATFORM_DIRS.get((platform.system(), platform.machine()))


def find_binary(explicit: str = "") -> Path | None:
    """Same lookup order as the AnyChat skill: $ANYCHAT_BIN → plugin install → ~/.local/bin → PATH."""
    candidates = [explicit, os.environ.get("ANYCHAT_BIN", "")]
    pd = platform_dir()
    exe = "anychat.exe" if platform.system() == "Windows" else "anychat"
    if pd:
        candidates.append(str(Path.home() / PLUGIN_BIN_DIR / pd / exe))
    candidates.append(str(Path.home() / ".local" / "bin" / exe))
    for c in candidates:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return Path(c)
    found = shutil.which("anychat")
    return Path(found) if found else None


class AnyChatSource:
    name = "anychat"
    INSTALL_HINT = INSTALL_HINT

    def __init__(self, binary: str = "", timeout_s: int = 600):
        self.binary = find_binary(binary)
        self.timeout_s = timeout_s

    # ---- process plumbing ----

    def _run(self, *args: str, timeout_s: int | None = None) -> subprocess.CompletedProcess:
        if not self.binary:
            raise ChatError("not_installed", "anychat CLI not found", self.INSTALL_HINT)
        try:
            return subprocess.run([str(self.binary), *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout_s or self.timeout_s)
        except subprocess.TimeoutExpired as e:
            raise ChatError("cli_error", f"anychat {args[0]} timed out") from e
        except OSError as e:
            raise ChatError("cli_error", f"cannot run anychat: {e}") from e

    @staticmethod
    def _json(proc: subprocess.CompletedProcess, what: str):
        """The command's JSON output; a log line printed before it is skipped."""
        text = (proc.stdout or "").strip()
        if not text:
            return {}
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=0)
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            raise ChatError("cli_error", f"anychat {what}: unreadable output: {text[:200]}") from None

    @staticmethod
    def _first(row: dict, fields: tuple[str, ...], default=""):
        return next((row[f] for f in fields if row.get(f)), default)

    # ---- ChatSource ----

    def available(self) -> Availability:
        if not self.binary:
            if platform_dir():
                return Availability(self.name, False, "not_installed", "anychat CLI not found", self.INSTALL_HINT)
            where = f"{platform.system()} {platform.machine()}"
            return Availability(self.name, False, "unsupported_platform",
                                f"AnyChat ships for macOS arm64 and Windows x64 only ({where})", "")
        try:
            proc = self._run("whoami", "--json", timeout_s=30)
        except ChatError as e:
            return Availability(self.name, False, e.code, str(e), e.hint)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:200]
            state = "not_logged_in" if re.search(r"log ?in|token|unauthori", err, re.I) else "cli_error"
            return Availability(self.name, False, state, err, "run AnyChat's own login (token via stdin) and retry")
        data = self._json(proc, "whoami")
        who = self._first(data, ACCOUNT_FIELDS, "logged in") if isinstance(data, dict) else "logged in"
        return Availability(self.name, True, "ok", str(who))

    def resolve(self, name: str) -> list[Contact]:
        proc = self._run("resolve", "--query", name, "--json", timeout_s=60)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:200]
            raise ChatError("cli_error", f"anychat resolve failed: {detail}")
        data = self._json(proc, "resolve")
        rows = data if isinstance(data, list) else self._first(data, CANDIDATE_LISTS, [])
        out = []
        for r in rows:
            if isinstance(r, str):
                out.append(Contact(r))
            elif isinstance(r, dict):
                label = self._first(r, LABEL_FIELDS)
                if label:
                    out.append(Contact(str(label), kind=str(r.get("kind") or r.get("type") or "friend"),
                                       raw_id=r.get("wxid") or r.get("id") or r.get("raw_id")))
        return out

    def fetch(self, contact: str, days: int, out_dir: Path) -> Transcript:
        out_dir.mkdir(parents=True, exist_ok=True)
        today = date.today()
        date_from, date_to = (today - timedelta(days=days)).isoformat(), today.isoformat()
        slug = re.sub(r"[^\w-]+", "_", contact).strip("_") or "contact"
        out = out_dir / f"{self.name}_{slug}_{date_from}_{date_to}.md"
        proc = self._run("query", "--mode", "friend", "--target", contact, "--days", str(days),
                         "--format", "md", "-o", str(out))
        if proc.returncode != 0 or not out.exists():
            raise ChatError("cli_error", f"anychat query failed: {(proc.stderr or proc.stdout).strip()[:300]}")
        text = out.read_text(encoding="utf-8", errors="replace")
        messages = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
        return Transcript(self.name, contact, date_from, date_to, out, message_count=len(messages), chars=len(text))
