"""stdio MCP server: the orchestration tools a chat model uses to drive a case.

Launch inside the client folder (or set UAPPLY_FOLDER). Task execution never
happens in the chat — `run_tasks` spawns the runtime headless per task.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer

from . import playbook
from .api import ApiError, UApplyApi
from .config import Settings
from .executor import Executor
from .folder import WorkingFolder
from .local_ops import heic_to_jpeg
from .runners import detect_runtimes

logging.basicConfig(level=os.environ.get("UAPPLY_LOG", "WARNING"), format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

server = MCPServer("uapply", instructions=playbook.instructions())

_settings = Settings.load()
_folder = WorkingFolder(os.environ.get("UAPPLY_FOLDER") or os.getcwd())
_api: Optional[UApplyApi] = None
DOCUMENT_CATEGORIES = ("identity", "school", "financial", "spouse", "parent", "child", "language",
                       "employment", "proof_of_capability", "immigration", "inviter", "education", "other")


def api() -> UApplyApi:
    global _api
    if _api is None:
        _api = UApplyApi(_settings)
    return _api


def _err(code: str, message: str, hint: str = "") -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "hint": hint}}


def _ok(**data) -> dict:
    return {"ok": True, **data}


def _need_case():
    if not _folder.survey_id:
        return _err("NO_CASE", "this folder is not bound to a case",
                    "ask the RCIC for the survey id and call init_case")
    return None


def _wrap(fn):
    try:
        return fn()
    except ApiError as e:
        return _err(f"HTTP_{e.status}", str(e), e.hint or ("log in with `uapply-agent login`" if e.status == 401 else ""))
    except Exception as e:  # keep the chat informative, never a stack trace
        logger.exception("tool failed")
        return _err(type(e).__name__, str(e)[:300])


# ---------- session ----------

@server.tool()
def whoami() -> dict:
    """Backend URL, login state, detected runtimes, and the working folder."""
    return _ok(backend=_settings.backend_url, logged_in=api().logged_in,
               runtimes=detect_runtimes(), folder=str(_folder.root), case=_folder.case or None)


@server.tool()
def set_folder(path: str) -> dict:
    """Point the server at a different client folder (absolute path). Use when the RCIC switches cases."""
    global _folder
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        return _err("NO_FOLDER", f"{p} is not a directory")
    _folder = WorkingFolder(p)
    return _ok(folder=str(_folder.root), case=_folder.case or None)


@server.tool()
def case_status() -> dict:
    """Server-truth status of this folder's case: documents by status, tasks, failures. Call first."""
    if e := _need_case():
        return e
    return _wrap(lambda: _ok(**api().agent_status(_folder.survey_id)))


@server.tool()
def init_case(survey_id: str, llm_mode: str = "local_agent") -> dict:
    """Bind this folder to an existing uApply survey and set its LLM mode (local_agent | server)."""
    def go():
        s = api().survey(survey_id)
        api().set_llm_mode(survey_id, llm_mode)
        deps = [{"survey_id": d.get("id"), "name": d.get("name"), "relationship": d.get("relationship")}
                for d in (s.get("dependents") or []) if isinstance(d, dict)]
        case = _folder.init_case(survey_id, _settings.backend_url, llm_mode, name=s.get("name", ""), dependents=deps)
        return _ok(case=case)
    return _wrap(go)


# ---------- files ----------

@server.tool()
def scan_folder(include_manifested: bool = False) -> dict:
    """Files in the working folder with hash, kind, applicant hint and whether they were already uploaded."""
    return _wrap(lambda: _ok(files=[f.as_dict() for f in _folder.scan(include_manifested)]))


@server.tool()
def list_document_types(query: str = "") -> dict:
    """Document types attached to this case (the only ones uploads accept), with their category."""
    if e := _need_case():
        return e

    def go():
        q = query.lower()
        rows = [{"id": t["id"], "name": t["name"], "file_name": t.get("file_name"), "category": t.get("category"),
                 "requirement": t.get("requirement"), "can_process": t.get("can_process")}
                for t in api().survey_document_types(_folder.survey_id)
                if not q or q in (str(t.get("name")) + " " + str(t.get("file_name"))).lower()]
        return _ok(document_types=rows)
    return _wrap(go)


@server.tool()
def sync_documents(document_type_id: str, document_category: str = "", paths: Optional[list[str]] = None,
                   applicant: str = "principal") -> dict:
    """Upload folder files under one document type. Skips files already in the manifest.
    document_category defaults to the type's category from list_document_types; otherwise one of
    identity, school, financial, spouse, parent, child, language, employment, proof_of_capability,
    immigration, inviter, education, other."""
    if e := _need_case():
        return e

    def go():
        category = document_category
        if not category:
            match = [t for t in api().survey_document_types(_folder.survey_id) if str(t.get("id")) == document_type_id]
            category = (match[0].get("category") if match else "") or "other"
        if category not in DOCUMENT_CATEGORIES:
            return _err("BAD_CATEGORY", f"document_category must be one of {DOCUMENT_CATEGORIES}")
        scanned = _folder.scan(include_manifested=True)
        chosen = [f for f in scanned if (not paths or f.path in paths)]
        todo = [f for f in chosen if not f.manifested]
        skipped = [f.path for f in chosen if f.manifested]
        uploaded, failed = [], []
        # One request per file: the response maps to the file with no name matching,
        # so HEIC conversions and duplicate basenames in subfolders cannot mix up ids.
        for f in todo:
            try:
                local = _folder.resolve(f.path)
                send = heic_to_jpeg(local, _folder.cache) if local.suffix.lower() in (".heic", ".heif") else local
                docs = api().bulk_upload(_folder.survey_id, category, document_type_id, [send])
                d = next((d for d in docs if isinstance(d, dict) and d.get("id")), None)
                if not d:
                    failed.append({"path": f.path, "reason": "empty upload response"})
                    continue
                _folder.record_upload(f.sha256, f.path, d["id"], applicant,
                                      uploaded_path=str(send.relative_to(_folder.root)) if send != local else None)
                uploaded.append({"path": f.path, "document_id": d["id"], "file_name": d.get("file_name")})
            except (ApiError, OSError, ValueError) as ex:
                failed.append({"path": f.path, "reason": str(ex)[:300]})
        return _ok(uploaded=len(uploaded), documents=uploaded, skipped=skipped, failed=failed)
    return _wrap(go)


@server.tool()
def list_documents() -> dict:
    """Documents on the case with type and status."""
    if e := _need_case():
        return e

    def go():
        rows = [{"id": d["id"], "file_name": d.get("file_name"), "status": d.get("status"),
                 "document_type_id": d.get("document_type_id"), "page_of": d.get("old_doc_id"),
                 "error": d.get("error")} for d in api().documents(_folder.survey_id)]
        return _ok(documents=rows)
    return _wrap(go)


# ---------- pipeline ----------

@server.tool()
def start_processing(document_ids: Optional[list[str]] = None) -> dict:
    """(Re)start processing for documents. Uploads already start processing; use this to retry failed ones."""
    if e := _need_case():
        return e

    def go():
        # Only top-level documents: pages of a split PDF mirror their parent (D12).
        ids = document_ids or [d["id"] for d in api().documents(_folder.survey_id)
                               if d.get("status") in ("uploaded", "failed", "stopped") and not d.get("old_doc_id")]
        out = []
        for did in ids:
            try:
                r = api().start_document_processing(_folder.survey_id, did)
                out.append({"document_id": did, "ok": True, "message": r.get("message")})
            except ApiError as ex:
                out.append({"document_id": did, "ok": False, "message": str(ex)})
        return _ok(started=out)
    return _wrap(go)


@server.tool()
def run_tasks(max_tasks: Optional[int] = None, workers: int = 2, kinds: Optional[list[str]] = None) -> dict:
    """Execute queued agent tasks in fresh headless runtime processes. Returns counts only."""
    if e := _need_case():
        return e

    def go():
        ex = Executor(api(), _folder, runtime=_settings.runtime, model=_settings.model, force_ocr=_settings.force_ocr)
        stats = ex.run(max_tasks=max_tasks, workers=workers or _settings.workers, kinds=kinds)
        return _ok(runtime=ex.runner.name, **stats.as_dict())
    return _wrap(go)


@server.tool()
def wait_for_stage(stage: str = "processing", timeout_s: int = 45) -> dict:
    """Long-poll (≤ 60 s) until processing/analysis is done for the case; returns status either way."""
    if e := _need_case():
        return e
    return _wrap(lambda: _ok(**api().agent_wait(_folder.survey_id, stage, max(1, min(int(timeout_s), 60)))))


@server.tool()
def task_stats() -> dict:
    """Agent task counts by status for the case."""
    if e := _need_case():
        return e
    return _wrap(lambda: _ok(stats=api().task_stats(_folder.survey_id)))


@server.tool()
def set_llm_mode(llm_mode: str) -> dict:
    """Switch the case between local_agent and server. Only after the RCIC asked for it."""
    if e := _need_case():
        return e

    def go():
        r = api().set_llm_mode(_folder.survey_id, llm_mode)
        case = _folder.case
        case["llm_mode"] = r.get("llm_mode", llm_mode)
        _folder.save_case(case)
        return _ok(llm_mode=case["llm_mode"])
    return _wrap(go)


# ---------- prompts ----------

@server.prompt(name="run")
def prompt_run() -> str:
    """Run the case in this folder end to end (Phase 1 scope)."""
    return playbook.prompt("run")


@server.prompt(name="status")
def prompt_status() -> str:
    """Summarise the case status."""
    return playbook.prompt("status")


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
