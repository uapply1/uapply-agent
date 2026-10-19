"""Identify folder files in a helper, so their page images never enter the RCIC's conversation.

`prepare` renders the first pages of each file into one brief; a task-runner subagent (session mode)
or the headless runtime (cli mode) answers with a document type and the key facts per file, and the
conversation reads only that text."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import briefs
from .constants import HEIC_EXTENSIONS, PREVIEWABLE_IMAGES
from .folder import WorkingFolder
from .local_ops import heic_to_jpeg, pdf_page_count, render_pdf_page

TASK_ID = "identify"
RESULT_FILE = "result.json"
MAX_FILES = 20
PAGES_PER_PDF = 2
TEXT_MAX = 4000

SYSTEM_PROMPT = """You identify the files in a Canadian immigration client's folder for an RCIC.
For every file listed in the task, look at its pages and report:
- document_type_id: the id from the document types list whose name and description fit the content,
  or null when none fits or no list is given. Filled IMM forms (intake, draft or earlier IMM forms,
  including screenshots or scans of them) take the type marked `generic`.
- document_type: what the document is, in a few words (e.g. "Passport", "Study permit", "Bank statement").
- holder: whose document it is, the name as printed; null when it names no person.
- key_facts: at most 200 characters: document number, issue and expiry dates, amounts or anything
  else that matters to the application.
- confidence: high, medium or low.
Also report the principal applicant's identity as printed on their passport or permit, and in `notes`
(at most 500 characters) anything the RCIC must know: expired status, documents of other people or of
another application, unreadable files. Report facts only; never copy whole passages."""

SCHEMA = {
    "type": "object",
    "required": ["files", "applicant", "notes"],
    "properties": {
        "files": {"type": "array", "items": {
            "type": "object",
            "required": ["path", "document_type_id", "document_type", "holder", "key_facts", "confidence"],
            "properties": {
                "path": {"type": "string"},
                "document_type_id": {"type": ["string", "null"]},
                "document_type": {"type": "string"},
                "holder": {"type": ["string", "null"]},
                "key_facts": {"type": "string", "maxLength": 200},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            }}},
        "applicant": {"type": "object", "properties": {
            "family_name": {"type": ["string", "null"]}, "given_names": {"type": ["string", "null"]},
            "date_of_birth": {"type": ["string", "null"]}, "citizenship": {"type": ["string", "null"]},
            "passport_number": {"type": ["string", "null"]}, "uci": {"type": ["string", "null"]}}},
        "notes": {"type": "string", "maxLength": 500},
    },
}


def task_dir(folder: WorkingFolder) -> Path:
    """A fresh directory: images of an earlier identification must not mix with this one."""
    d = folder.cache / TASK_ID
    shutil.rmtree(d, ignore_errors=True)
    return briefs.task_dir(folder, TASK_ID)


def prepare(folder: WorkingFolder, paths: list[str], document_types: list[dict]) -> dict:
    """Render the files' first pages into the task directory and build the identification task.
    Returns {task, images, text_files, files, skipped}."""
    cwd = task_dir(folder)
    images, text_files, lines, files, skipped = [], [], [], [], []
    for path in paths:
        if len(files) >= MAX_FILES:
            skipped.append({"path": path, "reason": f"more than {MAX_FILES} files; identify the rest in another call"})
            continue
        local = folder.resolve(path)
        if not local.is_file():
            skipped.append({"path": path, "reason": "not a file in the working folder"})
            continue
        n = len(files) + 1
        got, note = _inputs(local, cwd, f"f{n:02d}")
        if not got:
            skipped.append({"path": path, "reason": note})
            continue
        files.append(path)
        for p in got:
            (text_files if p.suffix == ".txt" else images).append(p)
        lines.append(f"{n}. `{path}` → {', '.join(f'`{p.name}`' for p in got)}{note}")
    types = [{k: t[k] for k in ("id", "name", "description", "generic", "use_for") if t.get(k)}
             for t in document_types]
    user_prompt = "\n".join([
        "Files (path → its pages in this directory):", *lines, "",
        "Document types:" if types else "No case is bound yet: set every document_type_id to null.",
        json.dumps(types, ensure_ascii=False) if types else "",
    ])
    task = {"id": TASK_ID, "kind": "identify_files",
            "payload": {"system_prompt": SYSTEM_PROMPT, "user_prompt": user_prompt, "output_schema": SCHEMA}}
    return {"task": task, "images": images, "text_files": text_files, "files": files, "skipped": skipped}


def _inputs(local: Path, cwd: Path, prefix: str) -> tuple[list[Path], str]:
    """The pages a model looks at for one file, named after its number in the list; ([], reason) when none."""
    suffix = local.suffix.lower()
    if suffix == ".pdf":
        total = pdf_page_count(local)
        out = []
        for page in range(1, min(total, PAGES_PER_PDF) + 1):
            src = render_pdf_page(local, page, cwd, dpi=110)
            out.append(src.rename(cwd / f"{prefix}_p{page}.png"))
        more = f" (pages 1-{len(out)} of {total})" if total > len(out) else ""
        return out, more
    if suffix in HEIC_EXTENSIONS:
        return [heic_to_jpeg(local, cwd).rename(cwd / f"{prefix}.jpg")], ""
    if suffix in PREVIEWABLE_IMAGES:
        return [Path(shutil.copy(local, cwd / f"{prefix}{suffix}"))], ""
    read = briefs.local_pages(local)
    if read:
        text = "\n".join(p["text"] for p in read[0])[:TEXT_MAX]
        dest = cwd / f"{prefix}.txt"
        dest.write_text(text, encoding="utf-8")
        return [dest], " (text)"
    return [], f"cannot read {suffix or 'this'} files"


def check(result: dict, files: list[str], document_types: list[dict]) -> str | None:
    """Why the answer cannot be used, or None."""
    if err := briefs.schema_check(SCHEMA, result):
        return err
    got = [f.get("path") for f in result.get("files") or []]
    if missing := [p for p in files if p not in got]:
        return f"no entry for {missing[:5]}"
    if document_types:
        ids = {str(t["id"]) for t in document_types}
        bad = [f["path"] for f in result["files"] if f.get("document_type_id") and str(f["document_type_id"]) not in ids]
        if bad:
            return f"document_type_id is not in the list for {bad[:5]}"
    return None


def save(folder: WorkingFolder, result: dict) -> None:
    (folder.cache / TASK_ID / RESULT_FILE).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


def load(folder: WorkingFolder) -> dict | None:
    p = folder.cache / TASK_ID / RESULT_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def state(folder: WorkingFolder) -> dict:
    """What `prepare` asked about, for checking a submission."""
    p = folder.cache / TASK_ID / "identify.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_state(folder: WorkingFolder, files: list[str], document_types: list[dict]) -> None:
    (folder.cache / TASK_ID / "identify.json").write_text(
        json.dumps({"files": files, "document_types": [{"id": t["id"]} for t in document_types]}), encoding="utf-8")
