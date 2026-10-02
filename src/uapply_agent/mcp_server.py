"""stdio MCP server: the tools a chat model uses to drive a uApply case from a client folder.

Tools validate their input, call a service module and shape the result as
`{ok: true, ...}` or `{ok: false, error: {code, message, hint}}`. Model work never happens in the
conversation: `run_tasks` runs each task in a fresh headless runtime process.
"""
from __future__ import annotations

import functools
import json
import logging
import os
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import Context, MCPServer

from . import briefs, cases, playbook, uploads
from .acrobat import detect as detect_acrobat
from .api import NO_AGENT_API_HINT, ApiError
from .autofill import AutoFiller
from .chat import filing
from .chat.anychat import AnyChatSource
from .chat.base import ChatError
from .chat.store import ChatStore, strip_chat_ids
from .constants import DONE_ANALYSIS, HEIC_EXTENSIONS, PREVIEWABLE_IMAGES
from .context import ServerContext, ToolError, ok, runner_mode
from .executor import Executor
from .folder import WorkingFolder
from .local_ops import heic_to_jpeg, pdf_page_count, render_pdf_page
from .runners import RunnerError, detect_runtimes, get_runner
from .runners.claude_code import claude_logged_in
from .session_runner import RUNTIME as SESSION_RUNTIME
from .session_runner import SessionRunner

logger = logging.getLogger(__name__)

MAX_PREVIEW_PAGES = 3
TIME_BOX_S = (20, 300)          # bounds for run_tasks / autofill_forms budgets
LONG_POLL_MAX_S = 60

server = MCPServer("uapply", instructions=playbook.instructions())
ctx = ServerContext.from_environment(os.environ.get("UAPPLY_FOLDER") or os.getcwd())


def _time_box(budget_s: int | None, default: int = 90) -> int:
    lo, hi = TIME_BOX_S
    return max(lo, min(int(budget_s or default), hi))


def _require(requires: str | None) -> None:
    if requires in ("case", "agent_api"):
        _ = ctx.survey_id                           # raises NO_CASE
    if requires == "agent_api" and not ctx.api.agent_api_available():
        raise ToolError("AGENT_API_UNAVAILABLE", "this backend has no local-agent API", NO_AGENT_API_HINT)


def tool(requires: Literal["case", "agent_api"] | None = None):
    """Register an MCP tool. `requires`: "case" = the folder must be bound; "agent_api" = also the
    backend must serve the local-agent API. Exceptions become error results, never stack traces."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                _require(requires)
                return fn(*args, **kwargs)
            except ToolError as e:
                return e.as_result()
            except ApiError as e:
                hint = e.hint or ("log in with `uapply-agent login`" if e.status == 401 else "")
                return ToolError(f"HTTP_{e.status}", str(e), hint).as_result()
            except ChatError as e:
                return ToolError(e.code.upper(), str(e), e.hint).as_result()
            except Exception as e:  # keep the conversation informative
                logger.exception("tool %s failed", fn.__name__)
                return ToolError(type(e).__name__.upper(), str(e)[:300]).as_result()
        return server.tool()(wrapper)
    return decorate


def _client_name(mcp_ctx: Context | None) -> str | None:
    try:
        return mcp_ctx.session.client_params.client_info.name
    except AttributeError:
        return None


def _mode(mcp_ctx: Context | None) -> str:
    return runner_mode(ctx.settings.task_runner, _client_name(mcp_ctx))


def _with_progress(out: dict) -> dict:
    try:
        out["progress"] = cases.progress(ctx.api.survey(ctx.survey_id))
    except ApiError:
        logger.debug("progress unavailable", exc_info=True)
    return out


# ---------- session ----------

_claude_signed_in: set[str] = set()


def _claude_login_state(path: str) -> bool | None:
    """Cached once signed in; a signed-out CLI is checked again on the next call."""
    if path in _claude_signed_in:
        return True
    state = claude_logged_in(path)
    if state:
        _claude_signed_in.add(path)
    return state


@tool()
def whoami(mcp_ctx: Context | None = None) -> dict:
    """Backend URL, login state, whether the backend serves the local-agent API, where model tasks run
    (`task_runner`: "session" = subagents of this session on your login, "cli" = a headless Claude Code /
    Codex CLI), detected CLI runtimes, Adobe Acrobat availability, and the working folder."""
    api = ctx.api
    mode = _mode(mcp_ctx)
    runtimes = detect_runtimes(ctx.settings)
    for r in runtimes:
        if r["name"] == "claude-code" and not r.get("error"):
            r["logged_in"] = _claude_login_state(r["path"])
    out = ok(backend=ctx.settings.backend_url, logged_in=api.logged_in,
             agent_api=api.agent_api_available() if api.logged_in else None, task_runner=mode, runtimes=runtimes,
             acrobat=detect_acrobat(probe=False), folder=str(ctx.folder.root), case=ctx.folder.case or None)
    if mode == "session":
        return out                       # tasks run in this session; a CLI is not needed
    if runtimes and all(r.get("error") for r in runtimes):
        out["hint"] = (f"{runtimes[0]['name']} is installed but does not start on this machine "
                       f"({runtimes[0]['error']}); local tasks cannot run — tell the RCIC, do not retry")
    elif not runtimes:
        out["hint"] = "no Claude Code / Codex CLI: local tasks cannot run; rerun the uApply installer"
    elif all(r.get("logged_in") is False for r in runtimes):
        out["hint"] = "the Claude Code CLI is not signed in: ask the RCIC to run `claude auth login` in a terminal"
    return out


@tool()
def set_folder(path: str) -> dict:
    """Point the server at a different client folder (absolute path). Use when the RCIC switches cases."""
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ToolError("NO_FOLDER", f"{p} is not a directory")
    ctx.folder = WorkingFolder(p)
    return ok(folder=str(ctx.folder.root), case=ctx.folder.case or None)


@tool(requires="case")
def case_status() -> dict:
    """Server-truth status of this folder's case: documents by status, agent tasks, failures. Call first.
    On a backend without the local-agent API it reports the survey's own document counts."""
    if not ctx.api.agent_api_available():
        return ok(**cases.server_mode_status(ctx.api.survey(ctx.survey_id)))
    return ok(**ctx.api.agent_status(ctx.survey_id))


@tool()
def init_case(survey_id: str, llm_mode: Literal["local_agent", "server"] = "local_agent") -> dict:
    """Bind this folder to an existing uApply survey and set its LLM mode."""
    case, warning = cases.bind_existing(ctx.api, ctx.settings, ctx.folder, survey_id, llm_mode)
    out = ok(case=case, chat_uploads=filing.upload_pending(ctx.api, ctx.folder))
    if warning:
        out["warning"] = warning
    return out


# ---------- files ----------

@tool()
def scan_folder(include_manifested: bool = False) -> dict:
    """Files in the working folder with hash, kind, applicant hint and whether they were already uploaded."""
    return ok(files=[f.as_dict() for f in ctx.folder.scan(include_manifested)])


def _page_range(pages: str, total: int) -> tuple[int, int]:
    first, _, last = pages.partition("-")
    try:
        a = int(first or 1)
        b = int(last or a)
    except ValueError:
        raise ToolError("BAD_PAGES", f"pages must look like \"1\" or \"1-3\", not {pages!r}") from None
    if a < 1 or b < a or a > total:
        raise ToolError("BAD_PAGES", f"pages {pages!r} is outside 1-{total}")
    return a, min(total, b, a + MAX_PREVIEW_PAGES - 1)


@tool()
def preview_document(path: str, pages: str = "1") -> dict:
    """Look at a folder file before classifying it: renders PDF pages (e.g. "1", "1-3", at most 3) to PNG
    under .uapply/cache/preview/ and returns the image paths to Read. Images are returned as they are
    (HEIC converted). Nothing leaves the machine."""
    local = ctx.folder.resolve(path)
    if not local.is_file():
        raise ToolError("NO_FILE", f"{path} is not a file in the working folder")
    suffix = local.suffix.lower()
    out_dir = ctx.folder.cache / "preview"
    if suffix == ".pdf":
        total = pdf_page_count(local)
        a, b = _page_range(pages, total)
        images = [render_pdf_page(local, n, out_dir, dpi=110) for n in range(a, b + 1)]
        return ok(kind="pdf", page_count=total, pages=f"{a}-{b}", images=[str(i) for i in images])
    if suffix in HEIC_EXTENSIONS:
        return ok(kind="image", images=[str(heic_to_jpeg(local, out_dir))])
    if suffix in PREVIEWABLE_IMAGES:
        return ok(kind="image", images=[str(local)])
    raise ToolError("UNSUPPORTED", f"cannot preview {suffix or 'this'} files")


@tool(requires="case")
def list_document_types(query: str = "") -> dict:
    """Document types uploads accept on this case, with their category. The row with `generic: true`
    (Agent Survey) is for IMM forms only: filled intake, draft or previous IMM forms."""
    return ok(document_types=uploads.document_types(ctx.api, ctx.survey_id, query))


@tool(requires="case")
def sync_documents(document_type_id: str, document_category: str = "", paths: list[str] | None = None,
                   applicant: str = "principal", archive_name: str = "") -> dict:
    """Upload folder files under one document type; files already uploaded are skipped, and a file
    already on the case (same type, name and size) is recorded instead of uploaded again.
    document_category defaults to the type's category from list_document_types; otherwise one of
    identity, school, financial, spouse, parent, child, language, employment, proof_of_capability,
    immigration, inviter, education, other. archive_name defaults to the type's own archive (the
    dashboard's default folder, named after the type); set it only to file into another folder."""
    return ok(**uploads.sync(ctx.api, ctx.folder, ctx.survey_id, document_type_id, document_category,
                             paths, applicant, archive_name))


@tool(requires="case")
def list_documents() -> dict:
    """Documents on the case with type and status."""
    rows = [{"id": d["id"], "file_name": d.get("file_name"), "status": d.get("status"),
             "document_type_id": d.get("document_type_id"), "page_of": d.get("old_doc_id"),
             "error": d.get("error")} for d in ctx.api.documents(ctx.survey_id)]
    return ok(documents=rows)


# ---------- pipeline ----------

@tool(requires="case")
def start_processing(document_ids: list[str] | None = None) -> dict:
    """Start processing the uploaded documents (uploads alone never start it for agent cases), or retry
    failed ones. Documents of stored-only types (see `progress.not_processed`) are skipped with the
    reason: the pipeline never processes them and they do not block the analysis."""
    survey = ctx.api.survey(ctx.survey_id)
    skipped = cases.not_processable(survey, document_ids or [])
    ids = [d for d in document_ids if d not in skipped] if document_ids else cases.startable_document_ids(survey)
    started = [{"document_id": did, "ok": False, "skipped": True, "message": why} for did, why in skipped.items()]
    for did in ids:
        try:
            r = ctx.api.start_document_processing(ctx.survey_id, did)
            started.append({"document_id": did, "ok": True, "message": r.get("message")})
        except ApiError as e:
            started.append({"document_id": did, "ok": False, "message": str(e)})
    return ok(started=started)


@tool(requires="agent_api")
def start_analysis() -> dict:
    """Start the case analysis once every document is processed (uploads never start it for agent
    cases), or run it again after sections failed. Every model call it makes runs on this machine
    through run_tasks."""
    p = cases.progress(ctx.api.survey(ctx.survey_id))
    if p["in_progress"] or p["by_status"].get("uploaded"):
        raise ToolError("PROCESSING_NOT_DONE", "documents are still being processed or not started",
                        "start_processing, then loop run_tasks / wait_for_stage('processing') until done")
    r = ctx.api.start_analysis(ctx.survey_id)
    return ok(started=True, message=r.get("message") if isinstance(r, dict) else None, progress=p)


@tool(requires="agent_api")
def confirm_documents(continue_with_failed_sections: bool = False) -> dict:
    """After the analysis: the dashboard's Confirm step — merge each archive into one PDF and queue
    compression on uApply (no AI). Moves the case on from "Not started"; then call autofill_forms.
    Refused while analysis sections have failed, unless the RCIC chose to continue with them
    (continue_with_failed_sections=true)."""
    survey = ctx.api.survey(ctx.survey_id)
    status = survey.get("analyzing_status")
    if status not in DONE_ANALYSIS:
        raise ToolError("ANALYSIS_NOT_DONE", f"analysis is {status}",
                        "loop run_tasks / wait_for_stage('analysis') until done, then confirm_documents")
    outcome = cases.analysis_outcome(survey) or {}
    if outcome.get("failed") and not continue_with_failed_sections:
        names = ", ".join(f["section"] for f in outcome["failed"])
        raise ToolError("ANALYSIS_INCOMPLETE",
                        f"{len(outcome['failed'])} of {outcome['sections']} analysis sections failed ({names}); "
                        "the forms would be filled without that data",
                        "ask the RCIC: run the analysis again (start_analysis, then run_tasks / "
                        "wait_for_stage('analysis')), or continue anyway (confirm_documents with "
                        "continue_with_failed_sections=true)")
    r = ctx.api.generate_archive_files(ctx.survey_id) or {}
    return ok(archives=len(r.get("results") or []), failed_documents=r.get("failed_documents") or [],
              status=r.get("status"))


@tool(requires="agent_api")
def autofill_forms(budget_s: int = 90) -> dict:
    """After confirm_documents: fill the case's IMM PDFs. With Adobe Acrobat Pro on this PC they are
    filled here (copies in .uapply/output/imm_pdfs) and uploaded; otherwise uApply's platform fills them
    (no AI). Call again while `remaining` > 0; with mode=platform loop wait_for_stage('filling')."""
    return ok(**AutoFiller(ctx.api, ctx.folder).run(budget_s=_time_box(budget_s)))


@tool(requires="agent_api")
def final_report(download: bool = True) -> dict:
    """The case's end-of-run report — what the dashboard's Submit step shows: status, AI Check
    conflicts/doubtful/missing, documents, IMM forms with fill rate, archives, and links to the Submit
    step and to start the online portal. With `download` the final package is saved to
    "uApply output/" in the client folder. Show `report_markdown` to the RCIC as is."""
    from .report import build
    return ok(**build(ctx.api, ctx.folder, ctx.settings, download=bool(download)))


@tool(requires="agent_api")
def run_tasks(max_tasks: int | None = None, workers: int = 2, kinds: list[str] | None = None,
              budget_s: int = 90, mcp_ctx: Context | None = None) -> dict:
    """Run the case's queued model tasks. `mode: "session"` (Claude Code): the result lists task briefs;
    spawn one `uapply:task-runner` subagent per brief (prompt: "Task brief: <path>"), all at once, wait
    for them, then call again — tasks that need no model are finished here. `mode: "cli"`: tasks ran
    in headless runtime processes for up to `budget_s` seconds. Either way: counts, `remaining` and
    per-document `progress`; call again while `remaining` > 0, one progress line to the RCIC between calls."""
    s = ctx.settings
    if _mode(mcp_ctx) == "session":
        rnd = SessionRunner(ctx.api, ctx.folder, force_ocr=s.force_ocr).prepare_round(workers or s.workers, kinds)
        out = ok(**rnd.as_dict(), remaining=_remaining())
        return _with_progress(out)
    ex = Executor(ctx.api, ctx.folder, runner=get_runner(s.runtime, s.model, s), force_ocr=s.force_ocr,
                  timeout_s=s.task_timeout_s)
    stats = ex.run(max_tasks=max_tasks, workers=workers or s.workers, kinds=kinds, budget_s=_time_box(budget_s))
    return _with_progress(ok(mode="cli", runtime=ex.runner.name, **stats.as_dict()))


def _remaining() -> int:
    st = ctx.api.task_stats(ctx.survey_id)
    return int(st.get("queued", 0)) + int(st.get("leased", 0))


@tool(requires="agent_api")
def submit_task(task_id: str, result: dict) -> dict:
    """For the task-runner subagent: send a task's JSON result to uApply. Returns `accepted: true`, or
    `accepted: false` with `feedback` to fix (submit once more), or `final: true` when the task is closed."""
    info = briefs.read_task_file(ctx.folder, task_id)
    if info is None:
        raise ToolError("UNKNOWN_TASK", f"no brief for task {task_id} in this folder")
    if err := briefs.schema_check(info["schema"], result):
        return ok(accepted=False, final=False,
                  feedback=f"the result is invalid ({err}); return JSON matching the schema")
    verdict = briefs.submit(ctx.api, task_id, info["kind"], result, "session", SESSION_RUNTIME, {}, "session")
    return ok(accepted=verdict.accepted, final=verdict.final, feedback=verdict.feedback,
              task_status=verdict.task_status)


@tool(requires="agent_api")
def release_task(task_id: str, reason: str) -> dict:
    """For the task-runner subagent: give a task back to the queue when it cannot be done, with the reason."""
    ctx.api.release_task(task_id, reason)
    return ok(released=True)


@tool(requires="agent_api")
def wait_for_stage(stage: Literal["processing", "analysis", "filling"] = "processing", timeout_s: int = 25) -> dict:
    """Long-poll (at most 60 s) until a stage is done for the case — processing, analysis or filling
    (the platform auto-fill) — and return the status either way."""
    timeout = max(1, min(int(timeout_s), LONG_POLL_MAX_S))
    try:
        res = ctx.api.agent_wait(ctx.survey_id, stage, timeout)
    except ApiError as e:
        if e.status != 504:
            raise
        # A proxy in front of uApply cut the long poll short: not done yet, same as a timeout.
        res = {**ctx.api.agent_status(ctx.survey_id), "done": False, "stage": stage}
    return _with_progress(ok(**res))


@tool(requires="agent_api")
def task_stats() -> dict:
    """Agent task counts by status for the case."""
    return ok(stats=ctx.api.task_stats(ctx.survey_id))


@tool(requires="agent_api")
def set_llm_mode(llm_mode: Literal["local_agent", "server"]) -> dict:
    """Switch the case between local_agent and server. Only after the RCIC asked for it."""
    r = ctx.api.set_llm_mode(ctx.survey_id, llm_mode)
    case = ctx.folder.case
    case["llm_mode"] = r.get("llm_mode", llm_mode)
    ctx.folder.save_case(case)
    return ok(llm_mode=case["llm_mode"])


# ---------- case creation ----------

@tool()
def list_application_types(query: str = "") -> dict:
    """Application types the RCIC can open a case under (id, code, name, program, visa_type, visa_location)."""
    q = query.lower()
    rows = [t for t in ctx.api.application_types() if not q or q in " ".join(str(v) for v in t.values() if v).lower()]
    return ok(application_types=rows)


@tool()
def create_case(name: str, application_type_id: str, confirmation: str = "") -> dict:
    """Create the survey (this charges the RCIC's account), set llm_mode=local_agent, bind this folder,
    and upload any queued chat transcripts. Refused unless `confirmation` is exactly "create case" or
    "确认创建" — pass it only after the RCIC picked "Create case" in your question tool or typed those
    words in this conversation."""
    created = cases.create(ctx.api, ctx.settings, ctx.folder, name, application_type_id, confirmation)
    warning = created.pop("warning")
    out = ok(**created, chat_uploads=filing.upload_pending(ctx.api, ctx.folder))
    if warning:
        out["warning"] = warning
    return out


# ---------- chat history (optional, AnyChat) ----------

def _chat_source() -> AnyChatSource:
    if ctx.settings.chat_source != "anychat":
        raise ToolError("CHAT_DISABLED", "chat_source is none", "set chat_source=anychat with `uapply-agent config`")
    return AnyChatSource(binary=ctx.settings.anychat_bin)


@tool()
def chat_sources() -> dict:
    """Which local chat archives the agent can read (AnyChat CLI), and whether they are ready."""
    if ctx.settings.chat_source != "anychat":
        return ok(sources=[{"source": "anychat", "ok": False, "state": "disabled", "hint": "set chat_source=anychat"}])
    return ok(sources=[_chat_source().available().as_dict()])


@tool()
def chat_find_contact(name: str) -> dict:
    """Resolve a contact name in the chat archive. Returns display names only."""
    return ok(candidates=[c.public() for c in _chat_source().resolve(name)])


@tool()
def chat_fetch(contact: str, days: int | None = None, mcp_ctx: Context | None = None) -> dict:
    """Fetch the chat history with one contact the RCIC named, file the transcript on the case as an
    Agent Survey document (queued until a case is bound), and derive intake hints on the RCIC's plan:
    in session mode the result carries `intake_brief` — spawn a `uapply:task-runner` subagent with
    "Task brief: <path>", then call `intake_hints`; in cli mode `intake` is filled here.
    Message bodies never enter this conversation."""
    from .chat.intake import intake_prompts, run_intake
    src = _chat_source()
    avail = src.available()
    if not avail.ok:
        raise ToolError(avail.state.upper(), avail.detail, avail.hint)
    s = ctx.settings
    transcript = src.fetch(contact, int(days or s.chat_default_days), ctx.folder.chat)
    store = ChatStore(ctx.folder)
    store.record(transcript)
    hints, usage, intake_error, brief = None, {}, "", None
    if _mode(mcp_ctx) == "session":
        brief = _intake_brief(transcript, intake_prompts)
    else:
        try:
            runner = get_runner(s.runtime, s.model, s)
            h, usage = run_intake(runner, transcript, ctx.api.application_types(), ctx.folder.cache / "chat",
                                  max_chars=s.chat_max_chars)
            hints = h.model_dump()
            store.save_intake(transcript, hints)
        except (RunnerError, ApiError, ValueError) as e:  # the transcript is still useful without hints
            logger.warning("chat intake failed", exc_info=True)
            intake_error = f"{type(e).__name__}: {str(e)[:200]}"
    if not s.chat_upload:
        upload = "disabled"
    elif ctx.folder.survey_id:
        upload = filing.upload_transcript(ctx.api, ctx.folder, transcript.path)
    else:
        store.queue_upload(transcript.path)
        upload = "queued"
    out = {"transcript": transcript.public(), "intake": hints, "upload": upload, "usage": usage}
    if brief:
        out["intake_brief"] = str(brief)
    if intake_error:
        out["intake_error"] = intake_error
    return ok(**json.loads(strip_chat_ids(json.dumps(out, ensure_ascii=False))))


def _intake_brief(transcript, intake_prompts) -> Path:
    """The intake call as a brief for the task-runner subagent (session mode)."""
    from .chat.intake import IntakeHints
    s = ctx.settings
    cwd = ctx.folder.cache / "chat"
    system, user, src_file = intake_prompts(transcript, ctx.api.application_types(), cwd, max_chars=s.chat_max_chars)
    task = {"id": f"intake-{transcript.path.stem}", "kind": "chat_intake",
            "payload": {"system_prompt": system, "user_prompt": user, "output_schema": IntakeHints.model_json_schema()}}
    return briefs.write_brief(ctx.folder, task, [], [src_file], submit_lines=[
        f"Call the `submit_intake` tool with `transcript: \"{transcript.path}\"` and your JSON object as `hints`.",
        "If it answers `accepted: false` with `feedback`, fix the answer and submit once more.",
        "Do not write files. Your final message is one line: the task id and accepted or rejected.",
    ])


@tool()
def submit_intake(transcript: str, hints: dict) -> dict:
    """For the task-runner subagent: store the intake hints derived from a fetched transcript."""
    from .chat.base import Transcript
    from .chat.intake import validate_hints
    path = Path(transcript)
    if not path.is_file():
        raise ToolError("UNKNOWN_TRANSCRIPT", f"{transcript} is not a fetched transcript")
    try:
        valid = validate_hints(hints, ctx.api.application_types())
    except ValueError as e:
        return ok(accepted=False, feedback=f"the hints are invalid ({str(e)[:300]}); return JSON matching the schema")
    store = ChatStore(ctx.folder)
    row = store.row(path)
    store.save_intake(Transcript(row.get("source", "chat"), row.get("contact", path.stem), row.get("from", ""),
                                 row.get("to", ""), path), valid.model_dump())
    return ok(accepted=True)


@tool()
def intake_hints(transcript: str) -> dict:
    """The intake hints stored for a fetched transcript (after the task-runner subagent submitted them)."""
    path = Path(transcript)
    p = ChatStore(ctx.folder).dir / (path.stem + ".intake.json")
    if not p.exists():
        raise ToolError("NO_HINTS", f"no intake hints for {path.name} yet",
                        "spawn the task-runner with the intake brief first")
    return ok(**json.loads(strip_chat_ids(json.dumps({"intake": json.loads(p.read_text(encoding="utf-8"))},
                                                        ensure_ascii=False))))


@tool(requires="case")
def chat_upload(path: str | None = None) -> dict:
    """Upload a fetched transcript (or every queued one) to the bound case as an Agent Survey document."""
    if path:
        return ok(**filing.upload_transcript(ctx.api, ctx.folder, ctx.folder.resolve(path)))
    return ok(uploads=filing.upload_pending(ctx.api, ctx.folder))


# ---------- prompts (the /uapply:* commands) ----------

def _prompt(name: str):
    def render() -> str:
        return playbook.prompt(name)
    render.__name__ = f"prompt_{name.replace('-', '_')}"
    return render


def _register_prompts() -> None:
    for name, description in playbook.PROMPTS.items():
        server.prompt(name=name, description=description)(_prompt(name))


_register_prompts()


def main() -> None:
    logging.basicConfig(level=os.environ.get("UAPPLY_LOG", "WARNING"), format="%(levelname)s %(name)s: %(message)s")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
