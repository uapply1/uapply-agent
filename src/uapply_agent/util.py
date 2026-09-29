"""Small file helpers shared by the config, the working folder and the chat store."""
from __future__ import annotations

import hashlib
import json
import locale
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def write_text_atomic(path: Path, text: str) -> None:
    """Write UTF-8 (never the Windows code page) through a unique temp file in the same directory,
    so a failed write cannot truncate the original and concurrent writers do not collide."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path, default):
    """UTF-8 JSON. Files from older builds may be in the local code page, or truncated by a failed
    write; those are set aside as `<name>.corrupt-<time>` and `default` is returned."""
    if not path.exists():
        return default
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode(locale.getpreferredencoding(False), errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        aside = path.with_name(f"{path.name}.corrupt-{datetime.now(timezone.utc):%Y%m%d%H%M%S}")
        os.replace(path, aside)
        logger.warning("unreadable %s moved to %s", path, aside)
        return default


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
