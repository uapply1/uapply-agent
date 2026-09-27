"""Where transcripts live: .uapply/chat/. Nothing here talks to the network."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..folder import WorkingFolder, now_iso
from .base import Transcript

RAW_ID = re.compile(r"\bwxid_[A-Za-z0-9_-]+\b|\b[A-Za-z0-9_-]{6,}@chatroom\b")


def redact(text: str) -> str:
    """Strip raw chat ids from anything that reaches the chat model."""
    return RAW_ID.sub("[id]", text or "")


class ChatStore:
    def __init__(self, folder: WorkingFolder):
        self.folder = folder
        self.dir = folder.state / "chat"

    def _read(self, name: str, default):
        p = self.dir / name
        return json.loads(p.read_text()) if p.exists() else default

    def _write(self, name: str, data) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / name).write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def index(self) -> list:
        return self._read("index.json", [])

    def record(self, t: Transcript, extra: dict | None = None) -> dict:
        rows = [r for r in self.index() if r.get("path") != str(t.path)]
        row = {**t.public(), "fetched_at": now_iso(), **(extra or {})}
        rows.append(row)
        self._write("index.json", rows)
        return row

    def save_intake(self, t: Transcript, hints: dict) -> Path:
        p = self.dir / (t.path.stem + ".intake.json")
        self.dir.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(hints, indent=2, ensure_ascii=False))
        return p

    # ---- uploads queued until a case exists ----

    def pending_uploads(self) -> list:
        return self._read("pending_uploads.json", [])

    def queue_upload(self, md_path: Path) -> None:
        q = self.pending_uploads()
        if str(md_path) not in q:
            q.append(str(md_path))
        self._write("pending_uploads.json", q)

    def clear_pending(self, md_path: Path) -> None:
        self._write("pending_uploads.json", [p for p in self.pending_uploads() if p != str(md_path)])
