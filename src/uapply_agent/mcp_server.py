"""stdio MCP server: the orchestration tools a chat model uses to drive a case.

Launch inside the client folder (or set UAPPLY_FOLDER). Task execution never
happens in the chat — `run_tasks` spawns the runtime headless per task.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer

from . import playbook
from .api import NO_AGENT_API_HINT, ApiError, UApplyApi
from .config import Settings
from .executor import Executor
from .folder import WorkingFolder
from .chat.anychat import AnyChatSource
from .chat.base import ChatError
from .chat.store import ChatStore, redact
from .local_ops import heic_to_jpeg, pdf_page_count, render_pdf_page
from .runners import RunnerError, detect_runtimes, get_runner

logging.basicConfig(level=os.environ.get("UAPPLY_LOG", "WARNING"), format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

server = MCPServer("uapply", instructions=playbook.instructions())

_settings = Settings.load()
_folder = WorkingFolder(os.environ.get("UAPPLY_FOLDER") or os.getcwd())
_api: Optional[UApplyApi] = None
AGENT_SURVEY_USE = ("IMM forms only: filled intake, draft or previous IMM forms (e.g. IMM 5709, 5257, 5645, 5406), "
                    "including screenshots or scans. Not for other unmatched documents: ask the RCIC.")
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


_claude_login_cache: dict[str, Optional[bool]] = {}


def _claude_login_state(path: str) -> Optional[bool]:
    if path not in _claude_login_cache or _claude_login_cache[path] is not True:
        from .integrate import claude_logged_in
        _claude_login_cache[path] = claude_logged_in(path)
    return _claude_login_cache[path]


def _need_case():
    if not _folder.survey_id:
        return _err("NO_CASE", "this folder is not bound to a case",
                    "ask the RCIC: create a new case (list_application_types → confirmation → create_case) "
                    "or bind an existing one (they paste the survey id → init_case)")
    return None


def _need_agent_api():
    """Local-agent tools need the backend branch; production without it must not be half-driven."""
    if e := _need_case():
        return e
    try:
        if not api().agent_api_available():
            return _err("AGENT_API_UNAVAILABLE", "this backend has no local-agent API", NO_AGENT_API_HINT)
    except ApiError as e:
        return _err(f"HTTP_{e.status}", str(e), e.hint)
    return None


def _bind(survey_id: str, llm_mode: str, name: str, dependents=None) -> tuple[dict, str]:
    """Bind the folder; on a backend without the agent API the case stays in server mode."""
    warning = ""
    if llm_mode == "local_agent" and not api().agent_api_available():
        llm_mode, warning = "server", NO_AGENT_API_HINT
    elif llm_mode in ("local_agent", "server"):
        api().set_llm_mode(survey_id, llm_mode)
    case = _folder.init_case(survey_id, _settings.backend_url, llm_mode, name=name, dependents=dependents)
    return case, warning


def _wrap(fn):
    try:
        return fn()
    except ApiError as e:
        return _err(f"HTTP_{e.status}", str(e), e.hint or ("log in with `uapply-agent login`" if e.status == 401 else ""))
    except ChatError as e:
        return _err(e.code, str(e), e.hint)
    except Exception as e:  # keep the chat informative, never a stack trace
        logger.exception("tool failed")
        return _err(type(e).__name__, str(e)[:300])


# ---------- session ----------

@server.tool()
def whoami() -> dict:
    """Backend URL, login state, whether the backend serves the local-agent API, detected runtimes
    (name + absolute path), and the working folder."""
    def go():
        agent_api = api().agent_api_available() if api().logged_in else None
        runtimes = detect_runtimes()
        for r in runtimes:
            if r["name"] == "claude-code" and not r.get("error"):
                r["logged_in"] = _claude_login_state(r["path"])
        out = _ok(backend=_settings.backend_url, logged_in=api().logged_in, agent_api=agent_api,
                  runtimes=runtimes, folder=str(_folder.root), case=_folder.case or None)
        if runtimes and all(r.get("error") for r in runtimes):
            out["hint"] = (f"{runtimes[0]['name']} is installed but does not start on this machine "
                           f"({runtimes[0]['error']}); local tasks cannot run — tell the RCIC, do not retry")
        elif not runtimes:
            out["hint"] = "no Claude Code / Codex CLI: local tasks cannot run; rerun the uApply installer"
        elif all(r.get("logged_in") is False for r in runtimes):
            out["hint"] = "the Claude Code CLI is not signed in: ask the RCIC to run `claude auth login` in a terminal"
        return out
    return _wrap(go)


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
    """Server-truth status of this folder's case: documents by status, tasks, failures. Call first.
    On a backend without the local-agent API it falls back to the survey's own document list."""
    if e := _need_case():
        return e

    def go():
        if not api().agent_api_available():
            s = api().survey(_folder.survey_id)
            docs = s.get("documents") or []
            by_status: dict[str, int] = {}
            for d in docs:
                if not d.get("old_doc_id"):
                    by_status[d.get("status", "?")] = by_status.get(d.get("status", "?"), 0) + 1
            return _ok(survey_id=_folder.survey_id, name=s.get("name"), llm_mode="server", agent_api=False,
                       documents=by_status, warning=NO_AGENT_API_HINT)
        return _ok(**api().agent_status(_folder.survey_id))
    return _wrap(go)


@server.tool()
def init_case(survey_id: str, llm_mode: str = "local_agent") -> dict:
    """Bind this folder to an existing uApply survey and set its LLM mode (local_agent | server)."""
    def go():
        s = api().survey(survey_id)
        deps = [{"survey_id": d.get("id"), "name": d.get("name"), "relationship": d.get("relationship")}
                for d in (s.get("dependents") or []) if isinstance(d, dict)]
        case, warning = _bind(survey_id, llm_mode, s.get("name", ""), deps)
        out = _ok(case=case, chat_uploads=_flush_pending_uploads())
        if warning:
            out["warning"] = warning
        return out
    return _wrap(go)


# ---------- files ----------

@server.tool()
def scan_folder(include_manifested: bool = False) -> dict:
    """Files in the working folder with hash, kind, applicant hint and whether they were already uploaded."""
    return _wrap(lambda: _ok(files=[f.as_dict() for f in _folder.scan(include_manifested)]))


@server.tool()
def preview_document(path: str, pages: str = "1") -> dict:
    """Look at a folder file before classifying it: renders PDF pages (e.g. "1", "1-3", at most 3) to PNG
    under .uapply/cache/preview/ and returns the image paths to Read. Images are returned as-is (HEIC
    converted). Nothing leaves the machine."""
    def go():
        local = _folder.resolve(path)
        if not local.is_file():
            return _err("NO_FILE", f"{path} is not a file in the working folder")
        suffix = local.suffix.lower()
        out_dir = _folder.cache / "preview"
        if suffix == ".pdf":
            total = pdf_page_count(local)
            first, _, last = pages.partition("-")
            a = max(1, int(first or 1)); b = min(total, int(last or a), a + 2)
            imgs = [render_pdf_page(local, n, out_dir, dpi=110) for n in range(a, b + 1)]
            return _ok(kind="pdf", page_count=total, pages=f"{a}-{b}", images=[str(i) for i in imgs])
        if suffix in (".heic", ".heif"):
            return _ok(kind="image", images=[str(heic_to_jpeg(local, out_dir))])
        if suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"):
            return _ok(kind="image", images=[str(local)])
        return _err("UNSUPPORTED", f"cannot preview {suffix or 'this'} files")
    return _wrap(go)


@server.tool()
def list_document_types(query: str = "") -> dict:
    """Document types uploads accept on this case, with their category. The row with `generic: true`
    (Agent Survey) is for IMM forms only: filled intake, draft or previous IMM forms."""
    if e := _need_case():
        return e

    def go():
        q = query.lower()
        types = api().survey_document_types(_folder.survey_id)
        rows = [{"id": t["id"], "name": t["name"], "file_name": t.get("file_name"), "category": t.get("category"),
                 "requirement": t.get("requirement"), "can_process": t.get("can_process")} for t in types]
        if not any(r["file_name"] == "agent_survey" for r in rows):
            try:
                g = api().agent_survey_type()
                rows.append({"id": g["id"], "name": g.get("name"), "file_name": "agent_survey", "category": "other",
                             "requirement": None, "can_process": True, "generic": True, "use_for": AGENT_SURVEY_USE})
            except ApiError:
                pass
        rows = [r for r in rows if not q or q in (str(r["name"]) + " " + str(r["file_name"])).lower()]
        return _ok(document_types=rows)
    return _wrap(go)


@server.tool()
def sync_documents(document_type_id: str, document_category: str = "", paths: Optional[list[str]] = None,
                   applicant: str = "principal", archive_name: str = "") -> dict:
    """Upload folder files under one document type. Skips files already in the manifest.
    document_category defaults to the type's category from list_document_types; otherwise one of
    identity, school, financial, spouse, parent, child, language, employment, proof_of_capability,
    immigration, inviter, education, other. archive_name defaults to the type's own archive (the
    dashboard's default folder, named after the type); set it only to file into another folder."""
    if e := _need_case():
        return e

    def go():
        match = [t for t in api().survey_document_types(_folder.survey_id) if str(t.get("id")) == document_type_id]
        dtype = match[0] if match else {}
        if not dtype:
            # Not on the case: only the generic Agent Survey type is accepted (backend rule).
            g = api().agent_survey_type()
            if str(g.get("id")) != document_type_id:
                return _err("UNKNOWN_TYPE", f"document type {document_type_id} is not on this case",
                            "pick an id from list_document_types")
            dtype = {**g, "category": "other"}
        category = document_category or dtype.get("category") or "other"
        archive = archive_name or dtype.get("name") or ""
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
                docs = api().bulk_upload(_folder.survey_id, category, document_type_id, [send], archive_name=archive)
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
    if e := _need_agent_api():
        return e

    def go():
        ex = Executor(api(), _folder, runtime=_settings.runtime, model=_settings.model, force_ocr=_settings.force_ocr)
        stats = ex.run(max_tasks=max_tasks, workers=workers or _settings.workers, kinds=kinds)
        return _ok(runtime=ex.runner.name, **stats.as_dict())
    return _wrap(go)


@server.tool()
def wait_for_stage(stage: str = "processing", timeout_s: int = 45) -> dict:
    """Long-poll (≤ 60 s) until processing/analysis is done for the case; returns status either way."""
    if e := _need_agent_api():
        return e
    return _wrap(lambda: _ok(**api().agent_wait(_folder.survey_id, stage, max(1, min(int(timeout_s), 60)))))


@server.tool()
def task_stats() -> dict:
    """Agent task counts by status for the case."""
    if e := _need_agent_api():
        return e
    return _wrap(lambda: _ok(stats=api().task_stats(_folder.survey_id)))


@server.tool()
def set_llm_mode(llm_mode: str) -> dict:
    """Switch the case between local_agent and server. Only after the RCIC asked for it."""
    if e := _need_agent_api():
        return e

    def go():
        r = api().set_llm_mode(_folder.survey_id, llm_mode)
        case = _folder.case
        case["llm_mode"] = r.get("llm_mode", llm_mode)
        _folder.save_case(case)
        return _ok(llm_mode=case["llm_mode"])
    return _wrap(go)


# ---------- chat history (optional, AnyChat) ----------

CREATE_CONFIRMATIONS = ("create case", "确认创建")


def _chat_source():
    if _settings.chat_source != "anychat":
        return None
    return AnyChatSource(binary=_settings.anychat_bin)


def _upload_transcript(md_path: Path) -> dict:
    """Render the transcript to a text PDF and file it on the case as an agent_survey document."""
    from .chat.render import transcript_to_pdf
    from .folder import sha256_of
    store = ChatStore(_folder)
    row = next((r for r in store.index() if r.get("path") == str(md_path)), {})
    md_sha = sha256_of(md_path)
    if existing := store.filed(md_path, md_sha):
        store.clear_pending(md_path)
        return {"document_id": existing, "pdf": row.get("pdf"), "already_filed": True}
    pdf = md_path.with_suffix(".pdf")
    transcript_to_pdf(md_path, pdf, f"Chat history: {row.get('contact', md_path.stem)}", [
        f"Source: {row.get('source', 'chat')} (self-reported, unverified)",
        f"Period: {row.get('from', '?')} to {row.get('to', '?')}",
        f"Fetched: {row.get('fetched_at', '')}",
        f"Case: {_folder.case.get('name', '')} ({_folder.survey_id})",
    ])
    dtype = api().agent_survey_type()
    # The type's default archive, so the transcript sits next to RCIC uploads of the same kind.
    docs = api().bulk_upload(_folder.survey_id, "other", dtype["id"], [pdf], archive_name=dtype.get("name") or "")
    d = next((d for d in docs if isinstance(d, dict) and d.get("id")), None)
    if not d:
        raise ChatError("upload_failed", "empty upload response for the transcript PDF")
    _folder.record_upload(sha256_of(pdf), str(pdf.relative_to(_folder.root)), d["id"], "principal",
                          uploaded_path=str(pdf.relative_to(_folder.root)))
    store.mark_uploaded(md_path, d["id"], md_sha, str(pdf.relative_to(_folder.root)))
    store.clear_pending(md_path)
    return {"document_id": d["id"], "pdf": str(pdf.relative_to(_folder.root))}


def _flush_pending_uploads() -> list:
    store = ChatStore(_folder)
    out = []
    for p in store.pending_uploads():
        try:
            out.append({"path": p, **_upload_transcript(Path(p))})
        except Exception as ex:  # keep the rest going; the RCIC can re-run chat_upload
            out.append({"path": p, "error": str(ex)[:200]})
    return out


@server.tool()
def chat_sources() -> dict:
    """Which local chat archives the agent can read (AnyChat CLI), and whether they are ready."""
    src = _chat_source()
    if src is None:
        return _ok(sources=[{"source": "anychat", "ok": False, "state": "disabled", "hint": "set chat_source=anychat"}])
    return _wrap(lambda: _ok(sources=[src.available().as_dict()]))


@server.tool()
def chat_find_contact(name: str) -> dict:
    """Resolve a contact name in the chat archive. Returns display names only."""
    src = _chat_source()
    if src is None:
        return _err("disabled", "chat_source is none")
    return _wrap(lambda: _ok(candidates=[c.public() for c in src.resolve(name)]))


@server.tool()
def chat_fetch(contact: str, days: Optional[int] = None) -> dict:
    """Fetch the chat history with one contact the RCIC named, derive intake hints locally
    (headless runtime, RCIC's plan), and file the transcript on the case as an agent_survey
    document (queued until a case is bound). Message bodies never enter this conversation."""
    src = _chat_source()
    if src is None:
        return _err("disabled", "chat_source is none")

    def go():
        avail = src.available()
        if not avail.ok:
            return _err(avail.state, avail.detail, avail.hint)
        t = src.fetch(contact, int(days or _settings.chat_default_days), _folder.chat)
        store = ChatStore(_folder)
        store.record(t)
        hints, usage, intake_error = None, {}, ""
        try:
            from .chat.intake import run_intake
            runner = get_runner(_settings.runtime, _settings.model)
            types = api().application_types()
            h, usage = run_intake(runner, t, types, _folder.cache / "chat", max_chars=_settings.chat_max_chars)
            hints = h.model_dump()
            store.save_intake(t, hints)
        except (RunnerError, ApiError, Exception) as ex:
            intake_error = f"{type(ex).__name__}: {str(ex)[:200]}"
        if not _settings.chat_upload:
            upload = "disabled"
        elif _folder.survey_id:
            upload = _upload_transcript(t.path)
        else:
            store.queue_upload(t.path)
            upload = "queued"
        out = {"transcript": t.public(), "intake": hints, "upload": upload, "usage": usage}
        if intake_error:
            out["intake_error"] = intake_error
        return _ok(**json.loads(redact(json.dumps(out, ensure_ascii=False))))
    return _wrap(go)


@server.tool()
def chat_upload(path: Optional[str] = None) -> dict:
    """Upload a fetched transcript (or every queued one) to the bound case as an agent_survey document."""
    if e := _need_case():
        return e
    if path:
        return _wrap(lambda: _ok(**_upload_transcript(_folder.resolve(path))))
    return _wrap(lambda: _ok(uploads=_flush_pending_uploads()))


@server.tool()
def list_application_types(query: str = "") -> dict:
    """Application types the RCIC can open a case under (id, code, name, program, visa_type, visa_location)."""
    def go():
        q = query.lower()
        rows = [t for t in api().application_types()
                if not q or q in " ".join(str(v) for v in t.values() if v).lower()]
        return _ok(application_types=rows)
    return _wrap(go)


@server.tool()
def create_case(name: str, application_type_id: str, confirmation: str = "") -> dict:
    """Create the survey (this charges the RCIC's account), set llm_mode=local_agent, bind this folder,
    and upload any queued chat transcripts. Refused unless `confirmation` is exactly "create case" or
    "确认创建" — pass it only after the RCIC typed those words in this conversation."""
    if confirmation.strip().lower() not in CREATE_CONFIRMATIONS:
        return _err("CONFIRMATION_REQUIRED", "the RCIC must type 'create case' (or 确认创建) first",
                    "show the proposed name and application type, wait for those words, then call again")
    if _folder.survey_id:
        return _err("CASE_EXISTS", f"this folder is already bound to survey {_folder.survey_id}",
                    "use set_folder for a different client, or init_case to rebind")

    def go():
        types = {t["id"]: t for t in api().application_types()}
        if application_type_id not in types:
            return _err("BAD_APPLICATION_TYPE", "unknown application_type_id", "pick one from list_application_types")
        team_id = _settings.team_id or None
        if not team_id:
            teams = api().teams()
            if len(teams) == 1:            # a member of exactly one team: the case belongs there
                team_id = teams[0]["id"]
        s = api().create_survey(name, application_type_id, team_id=team_id,
                                imm_pdf_types=types[application_type_id].get("default_imm_pdf_types") or [])
        survey_id = s.get("id")
        # The create body already asked for local_agent; a branch backend honoured it.
        case, warning = _bind(survey_id, "local_agent" if api().agent_api_available() else "server",
                              s.get("name", name))
        if not api().agent_api_available():
            warning = NO_AGENT_API_HINT
        out = _ok(survey_id=survey_id, team_id=team_id, case=case, chat_uploads=_flush_pending_uploads())
        if warning:
            out["warning"] = warning
        return out
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


@server.prompt(name="intake-from-chat")
def prompt_intake_from_chat() -> str:
    """Pull a client's chat history, propose the case, create it on the RCIC's confirmation."""
    return playbook.prompt("intake-from-chat")


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
