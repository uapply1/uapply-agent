"""The client working folder: scan, hash, manifest and case.json under .uapply/."""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .constants import IMAGE_EXTENSIONS, UPLOADABLE_EXTENSIONS
from .util import now_iso, read_json, sha256_of, write_text_atomic

logger = logging.getLogger(__name__)

STATE_DIR = ".uapply"
OUTPUT_DIR = "uApply output"   # what the agent saves for the RCIC (final package, report); never uploaded
IGNORED_PREFIXES = (".", "~$")
MANIFEST_SCHEMA = 2


@dataclass
class ScannedFile:
    path: str          # relative, posix
    size: int
    sha256: str
    kind: str          # pdf | image | office
    applicant_hint: str | None
    manifested: bool                  # already on the case under this path (uploaded or renamed)
    document_id: str | None = None
    # uploaded: same path, same content. modified: same path, new content (document_id is the stale
    # copy on the case). renamed: an uploaded file moved here (previous_path is gone). duplicate: the
    # same content as another file of the same applicant folder (duplicate_of). new: anything else.
    status: str = "new"
    previous_path: str | None = None
    duplicate_of: str | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in IMAGE_EXTENSIONS:
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

    def read_state(self, name: str, default):
        """A JSON state file under .uapply/ (e.g. case.json, autofill.json)."""
        return read_json(self.state / name, default)

    def write_state(self, name: str, data) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        gi = self.state / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
        write_text_atomic(self.state / name, json.dumps(data, indent=2, ensure_ascii=False))

    def remove_state(self, name: str) -> None:
        (self.state / name).unlink(missing_ok=True)

    @property
    def case(self) -> dict:
        return self.read_state("case.json", {})

    def save_case(self, case: dict) -> None:
        self.write_state("case.json", case)

    @property
    def manifest(self) -> dict:
        """Uploaded files keyed by relative path; each entry keeps the sha256 it was uploaded with.
        Schema 1 manifests (keyed by sha256) are upgraded on read and saved as schema 2 on the next write."""
        m = self.read_state("manifest.json", {"schema": MANIFEST_SCHEMA, "files": {}})
        if m.get("schema") != MANIFEST_SCHEMA:
            files = {}
            for digest, entry in (m.get("files") or {}).items():
                if isinstance(entry, dict) and entry.get("path"):
                    rest = {k: v for k, v in entry.items() if k != "path"}
                    files[entry["path"]] = {"sha256": digest, **rest}
            m = {"schema": MANIFEST_SCHEMA, "files": files}
        return m

    def save_manifest(self, manifest: dict) -> None:
        self.write_state("manifest.json", manifest)

    def init_case(self, survey_id: str, backend_url: str, llm_mode: str = "local_agent",
                  name: str = "", dependents: list | None = None) -> dict:
        case = self.case
        case.update({
            "schema": 1, "backend": backend_url, "survey_id": survey_id, "name": name,
            "llm_mode": llm_mode, "dependents": dependents or case.get("dependents", []),
        })
        case.setdefault("stage_history", []).append({"stage": "init", "at": now_iso()})
        self.save_case(case)
        return case

    @property
    def survey_id(self) -> str | None:
        return self.case.get("survey_id")

    @property
    def family_survey_ids(self) -> list:
        c = self.case
        if not c.get("survey_id"):
            return []
        return [c["survey_id"], *(d["survey_id"] for d in c.get("dependents", []) if d.get("survey_id"))]

    # ---- files ----

    def iter_files(self) -> Iterable[Path]:
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and not (Path(dirpath) == self.root and d == OUTPUT_DIR)]
            for fn in filenames:
                if fn.startswith(IGNORED_PREFIXES) or fn.endswith(".tmp"):
                    continue
                p = Path(dirpath) / fn
                if p.suffix.lower() in UPLOADABLE_EXTENSIONS:
                    yield p

    def applicant_hint(self, rel: Path) -> str | None:
        return rel.parts[0] if len(rel.parts) > 1 else None

    @staticmethod
    def content_group(path: str) -> str:
        """Identical content counts as one document only within the same first-level folder (one
        applicant); files at the folder root form their own group."""
        parts = path.split("/")
        return parts[0] if len(parts) > 1 else ""

    def scan(self, include_manifested: bool = True) -> list[ScannedFile]:
        files = self.manifest["files"]
        rows = []
        for p in sorted(self.iter_files()):
            rel = p.relative_to(self.root)
            rows.append((rel, p, sha256_of(p)))
        present = {rel.as_posix() for rel, _, _ in rows}
        # Recorded paths that are gone, by content: a file with that content elsewhere was renamed.
        gone: dict[str, list[str]] = {}
        for path in sorted(files):
            if path not in present and files[path].get("sha256"):
                gone.setdefault(files[path]["sha256"], []).append(path)
        # The first path holding each (applicant folder, content): already uploaded paths win.
        first: dict[tuple[str, str], str] = {}
        for rel, _, digest in rows:
            path = rel.as_posix()
            if files.get(path, {}).get("sha256") == digest:
                first.setdefault((self.content_group(path), digest), path)

        out = []
        for rel, p, digest in rows:
            path = rel.as_posix()
            key = (self.content_group(path), digest)
            entry = files.get(path)
            previous = None
            if entry and entry.get("sha256") == digest:
                status, doc_id = "uploaded", entry.get("document_id")
            elif entry:
                status, doc_id = "modified", entry.get("document_id")
            elif gone.get(digest):
                previous = gone[digest].pop(0)
                status, doc_id = "renamed", files[previous].get("document_id")
            elif first.get(key, path) != path:
                status, doc_id = "duplicate", files.get(first[key], {}).get("document_id")
            else:
                status, doc_id = "new", None
            first.setdefault(key, path)
            manifested = status in ("uploaded", "renamed")
            if manifested and not include_manifested:
                continue
            dup_of = first[key] if first[key] != path else None
            out.append(ScannedFile(
                path=path, size=p.stat().st_size, sha256=digest, kind=kind_of(p),
                applicant_hint=self.applicant_hint(rel), manifested=manifested, document_id=doc_id,
                status=status, previous_path=previous, duplicate_of=dup_of,
            ))
        return out

    def record_upload(self, path: str, sha256: str, document_id: str, applicant: str = "principal",
                      uploaded_path: str | None = None, superseded: list[str] | None = None) -> None:
        m = self.manifest
        entry = {
            "sha256": sha256, "document_id": document_id, "applicant": applicant,
            "uploaded_at": now_iso(), "uploaded_path": uploaded_path,
        }
        if superseded:
            entry["superseded"] = superseded
        m["files"][path] = entry
        self.save_manifest(m)

    def move_record(self, old_path: str, new_path: str) -> None:
        """A renamed file keeps its upload record (and document id) under its new path."""
        m = self.manifest
        if old_path in m["files"]:
            m["files"][new_path] = m["files"].pop(old_path)
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
