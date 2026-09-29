"""Preparing a pulled task for a model: inputs on disk, page images, the brief a session
subagent reads, and the checks a result goes through before it is sent to uApply.

Shared by the two task runners: the headless CLI executor and the session runner (subagents of
the RCIC's own Claude Code session).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .api import UApplyApi
from .constants import IMAGE_EXTENSIONS
from .folder import WorkingFolder
from .local_ops import pdf_page_count, pdf_pages_text, render_pdf_pages, to_image_for_model

logger = logging.getLogger(__name__)

CLASSIFY_PAGES = 3          # a PDF's first pages are enough to classify it
FINAL_TASK_STATUSES = ("failed", "cancelled", "expired")
TASK_FILE = "task.json"     # what submit_task needs to check a result: schema, kind, document
BRIEF_FILE = "task.md"


def schema_check(schema: dict, output: dict) -> str | None:
    """Cheap local check so an obviously wrong answer is retried before a round-trip."""
    if not isinstance(output, dict):
        return "output is not an object"
    for key in schema.get("required", []):
        if key not in output:
            return f"missing required field {key!r}"
    return None


def task_dir(folder: WorkingFolder, task_id: str) -> Path:
    d = folder.cache / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_inputs(api: UApplyApi, folder: WorkingFolder, task: dict) -> list[Path]:
    """Download each input document into the task's cache dir; return the raw files."""
    cache = task_dir(folder, task["id"])
    files = []
    for inp in task["payload"].get("inputs", []):
        name = inp.get("file_name") or f"{inp.get('document_id') or 'input'}.bin"
        dest = cache / Path(name).name
        if not dest.exists():
            # llm_call inputs carry a short-lived URL; document inputs are fetched by id
            url = inp.get("url") or api.document_download_url(inp["document_id"])
            api.download_to(url, dest)
        files.append(dest)
    return files


def write_text_files(task: dict, cwd: Path) -> list[Path]:
    """Long text inputs (transcripts, extracted documents) as files the model reads."""
    out = []
    for tf in task["payload"].get("text_files", []) or []:
        p = cwd / Path(tf.get("file_name") or "input.txt").name
        p.write_text(tf.get("text", ""), encoding="utf-8")
        out.append(p)
    return out


def images_for_classification(files: list[Path], cwd: Path) -> list[Path]:
    """What a classification/section task looks at: a PDF's first pages, images as they are."""
    out = []
    for path in files:
        if path.suffix.lower() == ".pdf":
            out += render_pdf_pages(path, cwd, 1, min(CLASSIFY_PAGES, pdf_page_count(path)))
        else:
            out.append(to_image_for_model(path, cwd))
    return out


def text_layer_pages(src: Path, force_ocr: bool = False) -> list[dict] | None:
    """The pages of a PDF with a usable text layer (no model needed), else None."""
    if src.suffix.lower() != ".pdf":
        return None
    pages = pdf_pages_text(src, force_ocr=force_ocr)
    if pages is None:
        return None
    return [{"n": i, "text": t} for i, t in enumerate(pages, start=1)]


def images_for_ocr(src: Path, cwd: Path) -> list[Path]:
    """Every page of a scanned document, in order."""
    if src.suffix.lower() == ".pdf":
        return render_pdf_pages(src, cwd, 1, pdf_page_count(src))
    if src.suffix.lower() in IMAGE_EXTENSIONS:
        return [to_image_for_model(src, cwd)]
    raise ValueError(f"extract_content: unsupported file type {src.suffix}")


def user_prompt_with_inputs(task: dict) -> str:
    """The task's user prompt plus inline text inputs and the server's rejection feedback."""
    payload = task["payload"]
    prompt = payload.get("user_prompt", "")
    for t in payload.get("text_inputs", []):
        prompt += f"\n\n--- document {t.get('document_id')} ---\n{t.get('text', '')}"
    rejection = task.get("rejection") or {}
    if rejection.get("code") == "SCHEMA_INVALID":   # handed back by the server with the reason
        prompt += f"\n\nA previous answer was rejected: {rejection.get('message')}. Fix that."
    return prompt


def rejection_feedback(out: dict) -> str:
    rej = out.get("rejection") or {}
    return f"Your previous answer was rejected: {rej.get('code')}: {rej.get('message')}. Fix it and return JSON only."


@dataclass
class Submission:
    accepted: bool
    final: bool = False          # the server closed the task: nothing more to do
    feedback: str = ""           # what to fix, when rejected and not final
    task_status: str = ""


def submit(api: UApplyApi, task_id: str, kind: str, result: dict, model: str, runtime: str, usage: dict | None,
           session_id: str) -> Submission:
    """Send a result to uApply and read its verdict."""
    out = api.submit_result(task_id, result, model, runtime, usage or {}, session_id)
    if out.get("accepted"):
        logger.info("task %s (%s) accepted: %s", task_id, kind, json.dumps(result)[:120])
        return Submission(accepted=True, final=True, task_status="accepted")
    status = out.get("task_status", "")
    if status in FINAL_TASK_STATUSES:
        return Submission(accepted=False, final=True, task_status=status,
                          feedback=(out.get("rejection") or {}).get("message", status))
    return Submission(accepted=False, final=False, task_status=status, feedback=rejection_feedback(out))


# ---- briefs for session subagents ----

def _document_name(task: dict) -> str:
    inputs = task["payload"].get("inputs") or []
    return str(inputs[0].get("file_name") or inputs[0].get("document_id") or "") if inputs else ""


def write_brief(folder: WorkingFolder, task: dict, images: list[Path], text_files: list[Path],
                extra_instruction: str = "", submit_lines: list[str] | None = None) -> Path:
    """The whole task as one Markdown file a subagent reads: instructions, the task, the schema,
    the files to read and how to submit. Nothing of it enters the RCIC's conversation."""
    cwd = task_dir(folder, task["id"])
    payload = task["payload"]
    schema = payload["output_schema"]
    files = [f"- `{p.name}`" for p in images] + [f"- `{p.name}` (text)" for p in text_files]
    task_text = user_prompt_with_inputs(task)
    if extra_instruction:
        task_text += f"\n\n{extra_instruction}"
    brief = "\n".join([
        f"# uApply task {task['id']}",
        "",
        f"Kind: `{task['kind']}`" + (f" · Document: `{_document_name(task)}`" if _document_name(task) else ""),
        "",
        "## Instructions",
        "",
        payload.get("system_prompt", ""),
        "",
        "## Task",
        "",
        task_text,
        "",
        "## Files to read (this directory, in this order)",
        "",
        "\n".join(files) or "- (none)",
        "",
        "## Answer format",
        "",
        "Your answer is a JSON object matching this schema:",
        "",
        "```json",
        json.dumps(schema, ensure_ascii=False, indent=1),
        "```",
        "",
        "## How to submit",
        "",
        *(submit_lines or [
            f"Call the `submit_task` tool with `task_id: \"{task['id']}\"` and your JSON object as `result`.",
            "If it answers `accepted: false` with `feedback`, fix the answer and submit once more.",
            f"If the task cannot be done, call `release_task` with `task_id: \"{task['id']}\"` and the reason.",
            "Do not write files. Your final message is one line: the task id and accepted, rejected or released.",
        ]),
        "",
    ])
    (cwd / BRIEF_FILE).write_text(brief, encoding="utf-8")
    (cwd / TASK_FILE).write_text(json.dumps({"task_id": task["id"], "kind": task["kind"], "schema": schema,
                                             "document": _document_name(task)}, ensure_ascii=False),
                                 encoding="utf-8")
    return cwd / BRIEF_FILE


def read_task_file(folder: WorkingFolder, task_id: str) -> dict | None:
    p = folder.cache / task_id / TASK_FILE
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))
