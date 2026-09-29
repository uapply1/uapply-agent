"""Where transcripts live: .uapply/chat/. Nothing here talks to the network."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..folder import WorkingFolder, now_iso, read_json, write_text_atomic
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
        return read_json(p, default)

    def _write(self, name: str, data) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self.dir / name, json.dumps(data, indent=2, ensure_ascii=False))

    def index(self) -> list:
        return self._read("index.json", [])

    def record(self, t: Transcript, extra: dict | None = None) -> dict:
        rows = self.index()
        previous = next((r for r in rows if r.get("path") == str(t.path)), {})
        rows = [r for r in rows if r.get("path") != str(t.path)]
        kept = {k: previous[k] for k in ("document_id", "md_sha256", "pdf", "uploaded_at") if k in previous}
        row = {**kept, **t.public(), "fetched_at": now_iso(), **(extra or {})}
        rows.append(row)
        self._write("index.json", rows)
        return row

    def filed(self, md_path: Path, md_sha256: str) -> str | None:
        """Document id if this exact transcript content was already filed on the case."""
        for r in self.index():
            if r.get("path") == str(md_path) and r.get("md_sha256") == md_sha256 and r.get("document_id"):
                return r["document_id"]
        return None

    def mark_uploaded(self, md_path: Path, document_id: str, md_sha256: str, pdf: str) -> None:
        rows = self.index()
        for r in rows:
            if r.get("path") == str(md_path):
                r.update({"document_id": document_id, "md_sha256": md_sha256, "pdf": pdf, "uploaded_at": now_iso()})
        self._write("index.json", rows)

    def save_intake(self, t: Transcript, hints: dict) -> Path:
        p = self.dir / (t.path.stem + ".intake.json")
        self.dir.mkdir(parents=True, exist_ok=True)
        write_text_atomic(p, json.dumps(hints, indent=2, ensure_ascii=False))
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
