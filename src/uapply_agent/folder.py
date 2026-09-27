"""The client working folder: scan, hash, manifest and case.json under .uapply/."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

STATE_DIR = ".uapply"
SUPPORTED = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".docx", ".doc", ".xlsx", ".xls"}
IGNORED_PREFIXES = (".", "~$")


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ScannedFile:
    path: str          # relative, posix
    size: int
    sha256: str
    kind: str          # pdf | image | office
    applicant_hint: Optional[str]
    manifested: bool
    document_id: Optional[str] = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in (".jpg", ".jpeg", ".png", ".heic", ".heif"):
        return "image"
    return "office"


class WorkingFolder:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.state = self.root / STATE_DIR
        self.cache = self.state / "cache"
        self.output = self.state / "output"
        self.chat = self.state / "chat"

    # ---- state files ----

    def _read(self, name: str, default):
        p = self.state / name
        if not p.exists():
            return default
        return json.loads(p.read_text())

    def _write(self, name: str, data) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        gi = self.state / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n")
        (self.state / name).write_text(json.dumps(data, indent=2, ensure_ascii=False))

    @property
    def case(self) -> dict:
        return self._read("case.json", {})

    def save_case(self, case: dict) -> None:
        self._write("case.json", case)

    @property
    def manifest(self) -> dict:
        return self._read("manifest.json", {"schema": 1, "files": {}})

    def save_manifest(self, manifest: dict) -> None:
        self._write("manifest.json", manifest)

    def init_case(self, survey_id: str, backend_url: str, llm_mode: str = "local_agent",
                  name: str = "", dependents: Optional[list] = None) -> dict:
        case = self.case
        case.update({
            "schema": 1, "backend": backend_url, "survey_id": survey_id, "name": name,
            "llm_mode": llm_mode, "dependents": dependents or case.get("dependents", []),
        })
        case.setdefault("stage_history", []).append({"stage": "init", "at": now_iso()})
        self.save_case(case)
        return case

    @property
    def survey_id(self) -> Optional[str]:
        return self.case.get("survey_id")

    @property
    def family_survey_ids(self) -> list:
        c = self.case
        return [c["survey_id"]] + [d["survey_id"] for d in c.get("dependents", []) if d.get("survey_id")] if c.get("survey_id") else []

    # ---- files ----

    def iter_files(self) -> Iterable[Path]:
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith(IGNORED_PREFIXES) or fn.endswith(".tmp"):
                    continue
                p = Path(dirpath) / fn
                if p.suffix.lower() in SUPPORTED:
                    yield p

    def applicant_hint(self, rel: Path) -> Optional[str]:
        return rel.parts[0] if len(rel.parts) > 1 else None

    def scan(self, include_manifested: bool = True) -> list[ScannedFile]:
        files = self.manifest["files"]
        out = []
        for p in sorted(self.iter_files()):
            rel = p.relative_to(self.root)
            digest = sha256_of(p)
            entry = files.get(digest)
            if entry and not include_manifested:
                continue
            out.append(ScannedFile(
                path=rel.as_posix(), size=p.stat().st_size, sha256=digest, kind=kind_of(p),
                applicant_hint=self.applicant_hint(rel), manifested=bool(entry),
                document_id=entry.get("document_id") if entry else None,
            ))
        return out

    def record_upload(self, sha256: str, path: str, document_id: str, applicant: str = "principal",
                      uploaded_path: Optional[str] = None) -> None:
        m = self.manifest
        m["files"][sha256] = {
            "path": path, "document_id": document_id, "applicant": applicant,
            "uploaded_at": now_iso(), "uploaded_path": uploaded_path,
        }
        self.save_manifest(m)

    def resolve(self, rel: str) -> Path:
        """A path inside the folder, or ValueError. Symlinks out of the folder are refused."""
        p = (self.root / rel).resolve()
        if self.root != p and self.root not in p.parents:
            raise ValueError(f"{rel} is outside the working folder")
        return p

    def clean(self) -> int:
        n = 0
        for d in (self.cache, self.output):
            if d.exists():
                for p in d.rglob("*"):
                    if p.is_file():
                        p.unlink()
                        n += 1
        return n
