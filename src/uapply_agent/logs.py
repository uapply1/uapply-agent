"""The agent's own log files (~/.config/uapply-agent/logs): one file per process, redacted, pruned.

They let uApply see why a run failed on the RCIC's machine; `send_logs` uploads them with consent.
Prompts, answers and document text are never logged; secrets are masked on the way to the file.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from .config import config_dir

KEEP_DAYS = 14
KEEP_BYTES = 50 * 1024 * 1024
MAX_MESSAGE = 2000
FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s [case=%(case)s] %(message)s"

_case = "-"
_configured: list = []   # [log file path] once setup_logging ran in this process

_SECRETS = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)\b((?:access|refresh|id)_token|client_secret|password|api_key)(['\"]?\s*[:=]\s*['\"]?)[^\s'\",&}]+"),
     r"\1\2<redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"), "<jwt>"),
    # Presigned storage URLs: the query string is the credential
    (re.compile(r"(https?://[^\s?'\"]+)\?[^\s'\"]*(?:X-Amz-|Signature=|AWSAccessKeyId=)[^\s'\"]*"), r"\1?<redacted>"),
]


def logs_dir() -> Path:
    return config_dir() / "logs"


def set_case(survey_id: str | None) -> None:
    """The case later log lines belong to (the MCP server serves one folder at a time)."""
    global _case
    _case = survey_id or "-"


def redact(text: str) -> str:
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Masks secrets, caps a record's size and stamps the case id; for the file handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if len(message) > MAX_MESSAGE:
            message = message[:MAX_MESSAGE] + f" …[{len(message) - MAX_MESSAGE} chars cut]"
        record.msg, record.args = redact(message), None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        record.case = _case
        return True


class _AgentOrWarning(logging.Filter):
    """INFO and above from the agent itself; only WARNING and above from libraries."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith("uapply_agent") or record.levelno >= logging.WARNING


def prune(directory: Path, keep_days: int = KEEP_DAYS, keep_bytes: int = KEEP_BYTES) -> None:
    files = sorted(directory.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    cutoff, total = time.time() - keep_days * 86400, 0
    for path in files:
        try:
            st = path.stat()
            total += st.st_size
            if st.st_mtime < cutoff or total > keep_bytes:
                path.unlink()
        except OSError:
            pass   # another process may hold it open (Windows) or have removed it


def setup_logging(component: str, console_level: str | int = logging.WARNING) -> Path | None:
    """Console at `console_level` (as before), plus this process's log file. Returns the file, or None
    when the logs folder cannot be written (logging must never stop the agent). Once per process."""
    if _configured:
        return _configured[0]
    _configured.append(None)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)
    try:
        directory = logs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        prune(directory)
        path = directory / f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}-{component}.log"
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as e:
        logging.getLogger(__name__).warning("no log file: %s", e)
        return None
    handler.setLevel(logging.INFO)
    handler.addFilter(_AgentOrWarning())
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter(FILE_FORMAT))
    root.addHandler(handler)
    _configured[0] = path
    return path


def current_file() -> Path | None:
    return _configured[0] if _configured else None


def recent_files(hours: int) -> list[Path]:
    """Log files written in the last `hours`, newest first."""
    cutoff = time.time() - hours * 3600
    directory = logs_dir()
    if not directory.is_dir():
        return []
    files = [p for p in directory.glob("*.log") if p.stat().st_mtime >= cutoff]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
