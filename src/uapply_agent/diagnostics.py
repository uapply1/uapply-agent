"""The log bundle the RCIC can send to uApply after a failure: recent log files, versions,
settings and the case's state. Built in memory; sent only with the RCIC's consent (send_logs)."""
from __future__ import annotations

import io
import json
import logging
import platform
import sys
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime

from . import __version__
from .logs import logs_dir, recent_files, redact

logger = logging.getLogger(__name__)

MAX_BUNDLE = 10 * 1024 * 1024


def about(settings) -> dict:
    """Versions and settings (they hold no secrets: tokens live in the keychain)."""
    from .runners import detect_runtimes
    try:
        runtimes = detect_runtimes(settings)
    except Exception as e:  # noqa: BLE001  (diagnostics must not fail on a broken runtime)
        runtimes = [{"error": str(e)[:300]}]
    return {
        "agent_version": __version__,
        "installed_sha": getattr(settings, "installed_sha", ""),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "created_at": datetime.now(UTC).isoformat(),
        "settings": asdict(settings),
        "runtimes": runtimes,
    }


def build_bundle(settings, hours: int = 72, note: str = "", case: dict | None = None,
                 status: dict | None = None, max_bytes: int = MAX_BUNDLE) -> bytes:
    """A zip of logs/<file>, about.json, case.json, status.json and note.txt. When the logs do not
    fit in `max_bytes`, the oldest files are left out."""
    meta = {"about.json": about(settings), "note.txt": note or ""}
    if case:
        meta["case.json"] = case
    if status:
        meta["status.json"] = status
    files = recent_files(hours)
    while True:
        data = _zip(meta, files)
        if len(data) <= max_bytes or not files:
            return data
        files = files[:-1]   # newest first: drop the oldest


def _zip(meta: dict, files: list) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, value in meta.items():
            text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False, default=str)
            zf.writestr(name, redact(text))
        for path in files:
            try:
                # Already redacted when written; again for lines from older agents.
                zf.writestr(f"logs/{path.name}", redact(path.read_text(encoding="utf-8", errors="replace")))
            except OSError as e:
                logger.warning("could not add %s: %s", path.name, e)
    return buf.getvalue()


def where() -> str:
    return str(logs_dir())
