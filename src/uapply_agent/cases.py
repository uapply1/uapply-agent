"""Binding a client folder to a uApply case, creating cases, and reading their progress."""
from __future__ import annotations

from collections import Counter

from .api import NO_AGENT_API_HINT, UApplyApi
from .config import Settings
from .constants import FINISHED_STATUSES, RUNNING_STATUSES, STARTABLE_STATUSES, DocStatus
from .context import ToolError
from .folder import WorkingFolder

CREATE_CONFIRMATIONS = ("create case", "确认创建")   # what the RCIC picks or types to approve a new case
LLM_MODES = ("local_agent", "server")


def dependents_of(survey: dict) -> list[dict]:
    return [{"survey_id": d.get("id"), "name": d.get("name"), "relationship": d.get("relationship")}
            for d in (survey.get("dependents") or []) if isinstance(d, dict)]


def bind(api: UApplyApi, settings: Settings, folder: WorkingFolder, survey_id: str, llm_mode: str,
         name: str, dependents: list | None = None) -> tuple[dict, str]:
    """Bind the folder to the case and store its LLM mode. The mode lives behind the agent API; on a
    backend without it the case is processed by uApply's servers, which the returned warning says."""
    if llm_mode not in LLM_MODES:
        raise ToolError("BAD_MODE", f"llm_mode must be one of {LLM_MODES}")
    warning = ""
    if api.agent_api_available():
        api.set_llm_mode(survey_id, llm_mode)
    else:
        llm_mode, warning = "server", NO_AGENT_API_HINT
    case = folder.init_case(survey_id, settings.backend_url, llm_mode, name=name, dependents=dependents)
    return case, warning


def bind_existing(api: UApplyApi, settings: Settings, folder: WorkingFolder, survey_id: str,
                  llm_mode: str = "local_agent") -> tuple[dict, str]:
    survey = api.survey(survey_id)
    return bind(api, settings, folder, survey_id, llm_mode, survey.get("name", ""), dependents_of(survey))


def create(api: UApplyApi, settings: Settings, folder: WorkingFolder, name: str, application_type_id: str,
           confirmation: str) -> dict:
    """Create the survey (charged to the RCIC) and bind the folder. Refused without the RCIC's
    explicit confirmation phrase, or when the folder already has a case."""
    if confirmation.strip().lower() not in CREATE_CONFIRMATIONS:
        raise ToolError("CONFIRMATION_REQUIRED", "the RCIC must confirm creating the case first",
                        "ask with your question tool (first option 'Create case', with name, type and the charge), "
                        "then call again with confirmation='create case'")
    if folder.survey_id:
        raise ToolError("CASE_EXISTS", f"this folder is already bound to survey {folder.survey_id}",
                        "use set_folder for a different client, or init_case to rebind")
    types = {t["id"]: t for t in api.application_types()}
    if application_type_id not in types:
        raise ToolError("BAD_APPLICATION_TYPE", "unknown application_type_id", "pick one from list_application_types")
    team_id = settings.team_id or None
    if not team_id:
        teams = api.teams()
        if len(teams) == 1:            # a member of exactly one team: the case belongs there
            team_id = teams[0]["id"]
    survey = api.create_survey(name, application_type_id, team_id=team_id,
                               imm_pdf_types=types[application_type_id].get("default_imm_pdf_types") or [])
    case, warning = bind(api, settings, folder, survey["id"], "local_agent", survey.get("name", name))
    return {"survey_id": survey["id"], "team_id": team_id, "case": case, "warning": warning}


def unprocessable_types(survey: dict) -> set[str]:
    """Document types the pipeline never processes (e.g. Digital Photo): their files stay 'uploaded'."""
    return {str(t.get("id")) for t in (survey.get("document_types") or []) if t.get("can_process") is False}


def startable_document_ids(survey: dict) -> list[str]:
    """Top-level documents of processable types that are waiting or can be retried. Pages of a split
    PDF follow their parent document and are never started on their own."""
    skip = unprocessable_types(survey)
    return [d["id"] for d in (survey.get("documents") or [])
            if d.get("status") in STARTABLE_STATUSES and not d.get("old_doc_id")
            and str(d.get("document_type_id")) not in skip]


def progress(survey: dict) -> dict:
    """Per-document progress of the case (top-level documents only). Files of types that are never
    processed are listed apart, not counted as pending."""
    skip = unprocessable_types(survey)
    top = [d for d in (survey.get("documents") or []) if not d.get("old_doc_id")]
    docs = [d for d in top if str(d.get("document_type_id")) not in skip]
    by_status = Counter(d.get("status") or "?" for d in docs)
    return {
        "documents_total": len(docs),
        "documents_finished": sum(by_status[s] for s in FINISHED_STATUSES),
        "by_status": dict(by_status),
        "in_progress": [d.get("file_name") for d in docs if d.get("status") in RUNNING_STATUSES][:10],
        "failed": [{"file_name": d.get("file_name"), "error": (d.get("error") or "")[:160]}
                   for d in docs if d.get("status") == DocStatus.FAILED][:10],
        "analysis": survey.get("analyzing_status") or "none",
        "analysis_failed_sections": (analysis_outcome(survey) or {}).get("failed", []),
        "not_processed": [d.get("file_name") for d in top if str(d.get("document_type_id")) in skip][:10],
    }


def analysis_outcome(survey: dict) -> dict | None:
    """The latest analysis job in brief: section counts and the sections that failed, with the
    reason. A job can finish as "completed" with most of its sections failed."""
    job = survey.get("analysis_job")
    if not isinstance(job, dict):
        return None
    sections = job.get("sections_progress") or {}
    failed = [{"section": name, "error": (s.get("error") or "")[:200]}
              for name, s in sections.items() if isinstance(s, dict) and s.get("status") == "failed"]
    no_data = sorted(name for name, s in sections.items() if isinstance(s, dict) and s.get("status") == "no_data")
    return {"status": job.get("status"), "sections": job.get("total_sections") or len(sections),
            "completed": job.get("completed_sections") or 0, "failed": failed, "no_data": no_data}


def server_mode_status(survey: dict) -> dict:
    """case_status on a backend without the agent API: counts from the survey's own document list."""
    docs = [d for d in (survey.get("documents") or []) if not d.get("old_doc_id")]
    return {"survey_id": survey.get("id"), "name": survey.get("name"), "llm_mode": "server", "agent_api": False,
            "documents": dict(Counter(d.get("status", "?") for d in docs)), "warning": NO_AGENT_API_HINT}
