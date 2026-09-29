"""Where transcripts live: .uapply/chat/. Nothing here talks to the network.

Rows are keyed by the transcript's path relative to the client folder, so a folder that is moved
or synced (e.g. OneDrive) keeps its index and upload queue.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..folder import WorkingFolder
from ..util import now_iso, read_json, write_text_atomic
from .base import Transcript

RAW_ID = re.compile(r"\bwxid_[A-Za-z0-9_-]+\b|\b[A-Za-z0-9_-]{6,}@chatroom\b")
INDEX, PENDING = "index.json", "pending_uploads.json"


def strip_chat_ids(text: str) -> str:
    """Replace raw WeChat account and group ids; names and message content are left as they are."""
    return RAW_ID.sub("[id]", text or "")


class ChatStore:
    def __init__(self, folder: WorkingFolder):
        self.folder = folder
        self.dir = folder.state / "chat"

    def key(self, md_path: Path | str) -> str:
        p = Path(md_path)
        try:
            return p.resolve().relative_to(self.folder.root).as_posix()
        except ValueError:
            return p.as_posix()

    def path_of(self, key: str) -> Path:
        p = Path(key)
        return p if p.is_absolute() else self.folder.root / p

    def _same(self, row_path: str, md_path: Path | str) -> bool:
        return self.key(row_path) == self.key(md_path)

    def _read(self, name: str, default):
        return read_json(self.dir / name, default)

    def _write(self, name: str, data) -> None:
        write_text_atomic(self.dir / name, json.dumps(data, indent=2, ensure_ascii=False))

    def index(self) -> list:
        return self._read(INDEX, [])

    def row(self, md_path: Path) -> dict:
        return next((r for r in self.index() if self._same(r.get("path", ""), md_path)), {})

    def record(self, t: Transcript, extra: dict | None = None) -> dict:
        rows = self.index()
        previous = self.row(t.path)
        rows = [r for r in rows if not self._same(r.get("path", ""), t.path)]
        kept = {k: previous[k] for k in ("document_id", "md_sha256", "pdf", "uploaded_at") if k in previous}
        row = {**kept, **t.public(), "path": self.key(t.path), "fetched_at": now_iso(), **(extra or {})}
        rows.append(row)
        self._write(INDEX, rows)
        return row

    def filed(self, md_path: Path, md_sha256: str) -> str | None:
        """Document id if this exact transcript content was already filed on the case."""
        r = self.row(md_path)
        return r.get("document_id") if r.get("md_sha256") == md_sha256 else None

    def mark_uploaded(self, md_path: Path, document_id: str, md_sha256: str, pdf: str) -> None:
        rows = self.index()
        for r in rows:
            if self._same(r.get("path", ""), md_path):
                r.update({"path": self.key(md_path), "document_id": document_id, "md_sha256": md_sha256,
                          "pdf": pdf, "uploaded_at": now_iso()})
        self._write(INDEX, rows)

    def save_intake(self, t: Transcript, hints: dict) -> Path:
        p = self.dir / (t.path.stem + ".intake.json")
        write_text_atomic(p, json.dumps(hints, indent=2, ensure_ascii=False))
        return p

    # ---- uploads queued until a case exists ----

    def pending_uploads(self) -> list[Path]:
        return [self.path_of(k) for k in self._read(PENDING, [])]

    def queue_upload(self, md_path: Path) -> None:
        keys = [self.key(p) for p in self._read(PENDING, [])]
        if self.key(md_path) not in keys:
            keys.append(self.key(md_path))
        self._write(PENDING, keys)

    def clear_pending(self, md_path: Path) -> None:
        self._write(PENDING, [k for k in self._read(PENDING, []) if not self._same(k, md_path)])
